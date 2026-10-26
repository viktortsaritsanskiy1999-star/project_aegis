import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Callable, List, Optional, Sequence
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.pairing_code import PairingCode
from app.models.patient import Patient
from app.repositories.cycle_repository import (
    CycleOwnershipConflictError,
    MedicalCycleRepository,
)
from app.repositories.device_repository import DeviceRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository


class CliConfigurationError(RuntimeError):
    """Raised when CLI database or secret configuration is missing or unsafe."""


class CliConfirmationAbortedError(RuntimeError):
    """Raised when a mutating operation on a non-test/production database is not confirmed."""


def _is_production_like_db(db_url: str, env_name: str) -> bool:
    clean_url = db_url.strip().lower()
    if "aegis_private_vault.db" in clean_url:
        return True
    if env_name.strip().lower() not in ("testing", "test"):
        return True
    return False


def _validate_and_create_engine(db_url: Optional[str]) -> Engine:
    """
    Validates that an explicit, safe database URL is provided and creates an isolated Engine.
    Never implicitly falls back to the default production SQLite file without explicit specification.
    """
    effective_url = (db_url or os.environ.get("AEGIS_CLI_DATABASE_URL") or "").strip()
    if not effective_url:
        raise CliConfigurationError(
            "Explicit database URL is required (--db-url or AEGIS_CLI_DATABASE_URL). "
            "Implicit fallback to default database is forbidden in Admin CLI."
        )

    if effective_url.startswith("sqlite:///") and ":memory:" not in effective_url:
        raw_path = effective_url[len("sqlite:///"):]
        db_file = Path(raw_path).resolve()
        if not db_file.exists() or not db_file.is_file():
            raise CliConfigurationError(
                f"Target SQLite database file does not exist: '{db_file}'. "
                "Admin CLI refuses to create unmigrated database files."
            )

    connect_args = {"check_same_thread": False} if effective_url.startswith("sqlite") else {}
    cli_engine = create_engine(effective_url, connect_args=connect_args)

    @event.listens_for(cli_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
        if isinstance(dbapi_conn, sqlite3.Connection):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return cli_engine


def _require_mutation_confirmation(
    *,
    db_url: str,
    env_name: str,
    confirm_production: bool,
    input_fn: Callable[[str], str] = input,
) -> None:
    """
    Enforces an explicit two-step safety guard before mutating any production or non-test database.
    Requires both `--confirm-production` and typing `CONFIRM` at the interactive prompt.
    """
    if not _is_production_like_db(db_url, env_name):
        return

    if not confirm_production:
        raise CliConfirmationAbortedError(
            "Refusing to mutate production/non-test database without --confirm-production flag."
        )

    response = input_fn(
        "WARNING: You are about to modify a production/non-test database. "
        "Type 'CONFIRM' to proceed: "
    )
    if response.strip() != "CONFIRM":
        raise CliConfirmationAbortedError("Operation aborted by operator (confirmation did not match 'CONFIRM').")


def cmd_create_patient(
    session: Session,
    *,
    patient_id: Optional[str] = None,
    status: str = "active",
) -> dict:
    if status not in ("active", "suspended", "archived"):
        raise ValueError("Patient status must be one of: active, suspended, archived.")
    repo = PatientRepository(session)
    patient = repo.create(patient_id=patient_id, status=status)
    return {
        "action": "create-patient",
        "patient_id": patient.patient_id,
        "status": patient.status,
        "created_at": patient.created_at.isoformat(),
    }


def cmd_issue_code(
    session: Session,
    *,
    patient_id: str,
    ttl_minutes: int = 10,
    max_attempts: int = 5,
    pairing_secret: Optional[str] = None,
) -> dict:
    """
    Issues a single-use 6-digit pairing code for an active patient.
    Returns the plaintext 6-digit code in the output dict for stdout display ONLY;
    never logs the plaintext code or secret.
    """
    patient_repo = PatientRepository(session)
    patient = patient_repo.get(patient_id.strip())
    if patient is None:
        raise ValueError(f"Patient '{patient_id}' not found.")
    if patient.status != "active":
        raise ValueError(f"Cannot issue pairing code: patient '{patient_id}' status is '{patient.status}'.")

    resolved_secret = PairingCode.resolve_secret(pairing_secret)
    pairing_repo = PairingRepository(session, secret=resolved_secret)
    raw_code, code_entity = pairing_repo.create_code(
        patient_id=patient.patient_id,
        ttl_minutes=ttl_minutes,
        max_attempts=max_attempts,
        secret=resolved_secret,
        max_global_active_codes=settings.max_active_pairing_codes,
    )
    return {
        "action": "issue-code",
        "patient_id": patient.patient_id,
        "code_id": code_entity.code_id,
        "pairing_code": raw_code,
        "ttl_minutes": ttl_minutes,
        "max_attempts": code_entity.max_attempts,
        "expires_at": code_entity.expires_at.isoformat(),
    }


def cmd_assign_cycle(
    session: Session,
    *,
    cycle_id: str,
    patient_id: str,
) -> dict:
    """
    Explicitly links a legacy unassigned MedicalCycle (patient_id IS NULL) to an existing Patient.
    Never overwrites a cycle already owned by another patient.
    """
    patient_repo = PatientRepository(session)
    patient = patient_repo.get(patient_id.strip())
    if patient is None:
        raise ValueError(f"Patient '{patient_id}' not found.")

    cycle_repo = MedicalCycleRepository(session)
    updated_cycle = cycle_repo.assign_patient(cycle_id=cycle_id, patient_id=patient.patient_id)
    if updated_cycle is None:
        raise ValueError(f"Medical cycle '{cycle_id}' not found.")

    return {
        "action": "assign-cycle",
        "cycle_id": updated_cycle.cycle_id,
        "patient_id": updated_cycle.patient_id,
        "status": updated_cycle.status,
    }


def cmd_revoke_device(
    session: Session,
    *,
    device_id: str,
) -> dict:
    """Atomically revokes a specific device and all its active refresh tokens."""
    device_repo = DeviceRepository(session)
    device = device_repo.revoke_device(device_id=device_id)
    if device is None:
        raise ValueError(f"Device '{device_id}' not found.")

    return {
        "action": "revoke-device",
        "device_id": device.device_id,
        "patient_id": device.patient_id,
        "is_active": device.is_active,
        "revoked_at": device.revoked_at.isoformat() if device.revoked_at else None,
    }


def cmd_list_devices(
    session: Session,
    *,
    patient_id: Optional[str] = None,
) -> dict:
    """
    Lists minimal operational status of registered devices without exposing
    any secrets, pairing codes, or token hashes.
    """
    stmt = select(Device).order_by(Device.paired_at.desc())
    if patient_id is not None and patient_id.strip():
        stmt = stmt.where(Device.patient_id == patient_id.strip())

    devices: List[Device] = list(session.execute(stmt).scalars().all())
    items = []
    for d in devices:
        active_tokens_count = session.execute(
            select(func.count(DeviceRefreshToken.token_id)).where(
                DeviceRefreshToken.device_id == d.device_id,
                DeviceRefreshToken.revoked_at.is_(None),
            )
        ).scalar_one()
        items.append(
            {
                "device_id": d.device_id,
                "patient_id": d.patient_id,
                "device_name": d.device_name,
                "platform": d.platform,
                "is_active": d.is_active,
                "paired_at": d.paired_at.isoformat() if d.paired_at else None,
                "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                "active_refresh_tokens": int(active_tokens_count),
            }
        )

    return {
        "action": "list-devices",
        "count": len(items),
        "devices": items,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.pairing",
        description="Project Aegis Local Administrative CLI for Patient & Device Pairing Management",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        help="Explicit SQLAlchemy database URL (or set AEGIS_CLI_DATABASE_URL).",
    )
    parser.add_argument(
        "--env",
        type=str,
        default=None,
        help="Execution environment override (default: settings.env).",
    )
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="Required flag before mutating a non-test or production database.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. create-patient
    p_create = subparsers.add_parser("create-patient", help="Create a new clinical patient identity")
    p_create.add_argument("--patient-id", type=str, default=None, help="Optional explicit patient UUID")
    p_create.add_argument("--status", type=str, default="active", help="Initial patient status (default: active)")

    # 2. issue-code
    p_code = subparsers.add_parser("issue-code", help="Issue a single-use 6-digit pairing code (10m TTL)")
    p_code.add_argument("--patient-id", type=str, required=True, help="Target patient_id")
    p_code.add_argument("--ttl-minutes", type=int, default=10, help="Code TTL in minutes (default: 10)")
    p_code.add_argument("--max-attempts", type=int, default=5, help="Max verification attempts (default: 5)")

    # 3. assign-cycle
    p_assign = subparsers.add_parser(
        "assign-cycle",
        help="Explicitly assign a legacy unassigned MedicalCycle (patient_id IS NULL) to a Patient",
    )
    p_assign.add_argument("--cycle-id", type=str, required=True, help="Target legacy cycle_id")
    p_assign.add_argument("--patient-id", type=str, required=True, help="Target patient_id")

    # 4. revoke-device
    p_revoke = subparsers.add_parser("revoke-device", help="Revoke a registered device and all its refresh tokens")
    p_revoke.add_argument("--device-id", type=str, required=True, help="Target device_id")

    # 5. list-devices
    p_list = subparsers.add_parser("list-devices", help="View minimal status of devices without secrets")
    p_list.add_argument("--patient-id", type=str, default=None, help="Optional filter by patient_id")

    return parser


def run_cli(
    argv: Optional[Sequence[str]] = None,
    *,
    session_override: Optional[Session] = None,
    pairing_secret_override: Optional[str] = None,
    input_fn: Callable[[str], str] = input,
    stdout=None,
    stderr=None,
) -> int:
    """
    Entry point for the local administrative CLI.
    Returns 0 on success, non-zero exit code on validation/configuration/confirmation error.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    effective_env = args.env if args.env is not None else settings.env
    effective_db_url = (args.db_url or os.environ.get("AEGIS_CLI_DATABASE_URL") or "").strip()
    is_mutating = args.command in ("create-patient", "issue-code", "assign-cycle", "revoke-device")

    try:
        if session_override is None and not effective_db_url:
            raise CliConfigurationError(
                "Explicit database URL is required (--db-url or AEGIS_CLI_DATABASE_URL)."
            )

        if is_mutating:
            _require_mutation_confirmation(
                db_url=effective_db_url or "sqlite:///session_override",
                env_name=effective_env,
                confirm_production=bool(args.confirm_production),
                input_fn=input_fn,
            )

        owned_engine: Optional[Engine] = None
        if session_override is not None:
            session = session_override
            close_session = False
        else:
            owned_engine = _validate_and_create_engine(effective_db_url)
            factory = sessionmaker(autocommit=False, autoflush=False, bind=owned_engine)
            session = factory()
            close_session = True

        try:
            if args.command == "create-patient":
                result = cmd_create_patient(
                    session,
                    patient_id=args.patient_id,
                    status=args.status,
                )
            elif args.command == "issue-code":
                result = cmd_issue_code(
                    session,
                    patient_id=args.patient_id,
                    ttl_minutes=args.ttl_minutes,
                    max_attempts=args.max_attempts,
                    pairing_secret=pairing_secret_override,
                )
            elif args.command == "assign-cycle":
                result = cmd_assign_cycle(
                    session,
                    cycle_id=args.cycle_id,
                    patient_id=args.patient_id,
                )
            elif args.command == "revoke-device":
                result = cmd_revoke_device(
                    session,
                    device_id=args.device_id,
                )
            elif args.command == "list-devices":
                result = cmd_list_devices(
                    session,
                    patient_id=args.patient_id,
                )
            else:
                raise ValueError(f"Unknown command: {args.command}")

            print(json.dumps(result, ensure_ascii=False), file=out)
            return 0
        finally:
            if close_session:
                session.close()
            if owned_engine is not None:
                owned_engine.dispose()

    except (CliConfigurationError, CliConfirmationAbortedError) as exc:
        print(f"CLI Security/Config Error: {exc}", file=err)
        return 2
    except CycleOwnershipConflictError as exc:
        print(f"Cycle Ownership Conflict: {exc}", file=err)
        return 3
    except Exception as exc:
        print(f"CLI Execution Error: {exc}", file=err)
        return 1


if __name__ == "__main__":
    sys.exit(run_cli())
