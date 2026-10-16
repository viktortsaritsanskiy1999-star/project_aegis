from enum import Enum


class PrivacyLevel(str, Enum):
    """
    Strict privacy classification for AI model reports in Project Aegis:
    - SERVER_ONLY: Deep clinical reasoning, raw training logs, differential hypotheses,
                   internal biomechanics, and Athena handoff directives.
                   Must NEVER be delivered to client devices.
    - CLIENT_FACING: Filtered user-friendly wellness roadmap, Atlas organ summaries,
                     and user workout metrics.
    """
    SERVER_ONLY = "server_only"
    CLIENT_FACING = "client_facing"

    @classmethod
    def from_str(cls, value: str) -> "PrivacyLevel":
        try:
            return cls(value.strip().lower())
        except ValueError:
            raise ValueError(
                f"Invalid privacy level '{value}'. Allowed levels: "
                f"{[level.value for level in cls]}"
            )
