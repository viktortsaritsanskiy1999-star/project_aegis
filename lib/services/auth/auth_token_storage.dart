import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'auth_models.dart';

/// Exception thrown when secure storage fails or is inaccessible.
/// Designed to be strictly safe: toString() and message never leak sensitive credentials,
/// tokens, or platform exception message strings containing secrets.
class SecureStorageUnavailableException implements Exception {
  final String message;
  final dynamic cause;

  const SecureStorageUnavailableException(this.message, [this.cause]);

  @override
  String toString() => 'SecureStorageUnavailableException: $message';
}

/// Immutable record representing a refresh token dispatch state in persistent storage.
/// Used to prevent replay of single-use refresh tokens across app restarts and storage failures.
@immutable
class RefreshInFlightRecord {
  final String deviceId;
  final String dispatchedRefreshToken;
  final DateTime dispatchedAt;

  const RefreshInFlightRecord({
    required this.deviceId,
    required this.dispatchedRefreshToken,
    required this.dispatchedAt,
  });

  Map<String, dynamic> toJson() => {
    'device_id': deviceId,
    'dispatched_refresh_token': dispatchedRefreshToken,
    'dispatched_at': dispatchedAt.toUtc().toIso8601String(),
  };

  factory RefreshInFlightRecord.fromJson(Map<String, dynamic> json) {
    final deviceId = json['device_id'] as String?;
    final token = json['dispatched_refresh_token'] as String?;
    final atStr = json['dispatched_at'] as String?;
    if (deviceId == null || token == null || atStr == null) {
      throw const FormatException('Invalid RefreshInFlightRecord payload.');
    }
    final at = DateTime.tryParse(atStr)?.toUtc();
    if (at == null) {
      throw const FormatException('Invalid datetime format in RefreshInFlightRecord.');
    }
    return RefreshInFlightRecord(
      deviceId: deviceId,
      dispatchedRefreshToken: token,
      dispatchedAt: at,
    );
  }

  @override
  String toString() =>
      'RefreshInFlightRecord(deviceId: $deviceId, dispatchedRefreshToken: [REDACTED], dispatchedAt: ${dispatchedAt.toIso8601String()})';
}

/// Abstract contract for storing and retrieving authenticated device session credentials
/// and persistent in-flight refresh guards.
abstract class IAuthTokenStorage {
  Future<void> saveSession(AuthSession session);
  Future<AuthSession?> loadSession();
  Future<void> clearSession();

  /// Persistently records that a single-use refresh token has been dispatched
  /// BEFORE sending the HTTP `POST /api/v1/auth/refresh` request.
  Future<void> markRefreshInFlight({
    required String deviceId,
    required String dispatchedRefreshToken,
  });

  /// Reads any persistent in-flight refresh record from storage.
  /// Fails closed if the record is corrupted or unreadable.
  Future<RefreshInFlightRecord?> loadRefreshInFlightRecord();

  /// Clears the persistent in-flight refresh record after new session is committed.
  Future<void> clearRefreshInFlightRecord();
}

/// Production implementation backed strictly by OS-level secure storage (Android Keystore / iOS Keychain).
/// Does NOT fallback silently to SharedPreferences or InMemory storage under any circumstances.
class SecureAuthTokenStorage implements IAuthTokenStorage {
  static const String sessionStorageKey = 'aegis_auth_session_v1';
  static const String inFlightStorageKey = 'aegis_auth_refresh_inflight_v1';
  final FlutterSecureStorage _storage;

  SecureAuthTokenStorage({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(),
              iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock),
            );

  @override
  Future<void> saveSession(AuthSession session) async {
    try {
      final jsonStr = json.encode(session.toJson());
      await _storage.write(key: sessionStorageKey, value: jsonStr);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to securely persist auth session.',
        e,
      );
    }
  }

  @override
  Future<AuthSession?> loadSession() async {
    String? raw;
    try {
      raw = await _storage.read(key: sessionStorageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to read auth session from secure storage.',
        e,
      );
    }

    if (raw == null || raw.trim().isEmpty) {
      return null;
    }

    dynamic decoded;
    try {
      decoded = json.decode(raw);
    } catch (e) {
      // Corrupted JSON syntax: clean up corrupted record and fail-closed if delete fails
      await clearSession();
      return null;
    }

    if (decoded is! Map<String, dynamic>) {
      // Incompatible root payload type: clean up and fail-closed if delete fails
      await clearSession();
      return null;
    }

    try {
      return AuthSession.fromJson(decoded);
    } catch (e) {
      // Incompatible session schema or missing required fields: clean up and fail-closed if delete fails
      await clearSession();
      return null;
    }
  }

  @override
  Future<void> clearSession() async {
    try {
      await _storage.delete(key: sessionStorageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to delete auth session from secure storage.',
        e,
      );
    }
  }

  @override
  Future<void> markRefreshInFlight({
    required String deviceId,
    required String dispatchedRefreshToken,
  }) async {
    try {
      final record = RefreshInFlightRecord(
        deviceId: deviceId,
        dispatchedRefreshToken: dispatchedRefreshToken,
        dispatchedAt: DateTime.now().toUtc(),
      );
      final jsonStr = json.encode(record.toJson());
      await _storage.write(key: inFlightStorageKey, value: jsonStr);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to persist in-flight refresh record in secure storage.',
        e,
      );
    }
  }

  @override
  Future<RefreshInFlightRecord?> loadRefreshInFlightRecord() async {
    String? raw;
    try {
      raw = await _storage.read(key: inFlightStorageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to read in-flight refresh record from secure storage.',
        e,
      );
    }

    if (raw == null) {
      return null;
    }

    if (raw.trim().isEmpty) {
      // Empty or whitespace-only record: DO NOT delete automatically! Fail closed.
      throw const SecureStorageUnavailableException(
        'Empty or corrupted persistent refresh guard record.',
      );
    }

    dynamic decoded;
    try {
      decoded = json.decode(raw);
    } catch (e) {
      // Corrupted syntax in guard record: DO NOT delete automatically! Fail closed.
      throw SecureStorageUnavailableException(
        'Corrupted syntax in persistent refresh guard record.',
        e,
      );
    }

    if (decoded is! Map<String, dynamic>) {
      // Incompatible structure: DO NOT delete automatically! Fail closed.
      throw const SecureStorageUnavailableException(
        'Incompatible payload structure in persistent refresh guard record.',
      );
    }

    try {
      return RefreshInFlightRecord.fromJson(decoded);
    } catch (e) {
      // Corrupted or missing fields: DO NOT delete automatically! Fail closed.
      throw SecureStorageUnavailableException(
        'Invalid or corrupted fields in persistent refresh guard record.',
        e,
      );
    }
  }

  @override
  Future<void> clearRefreshInFlightRecord() async {
    try {
      await _storage.delete(key: inFlightStorageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to delete in-flight refresh record from secure storage.',
        e,
      );
    }
  }
}

/// In-memory storage implementation for deterministic, isolated unit tests.
/// Explicitly prohibited from automatic production fallback.
class InMemoryAuthTokenStorage implements IAuthTokenStorage {
  AuthSession? _session;
  RefreshInFlightRecord? _inFlightRecord;
  String? _rawInFlightRecord;
  bool shouldThrow = false;
  bool shouldThrowOnSave = false;
  bool shouldThrowOnLoad = false;
  bool shouldThrowOnClear = false;
  bool shouldThrowOnInFlight = false;
  bool shouldThrowOnInFlightLoad = false;
  bool shouldThrowOnInFlightClear = false;
  String? failureMessage;

  InMemoryAuthTokenStorage({
    AuthSession? initialSession,
    RefreshInFlightRecord? initialInFlightRecord,
    String? rawInFlightRecord,
  })  : _session = initialSession,
        _inFlightRecord = initialInFlightRecord,
        _rawInFlightRecord = rawInFlightRecord;

  RefreshInFlightRecord? get inFlightRecord => _inFlightRecord;
  set rawInFlightRecord(String? value) => _rawInFlightRecord = value;

  @override
  Future<void> saveSession(AuthSession session) async {
    if (shouldThrow || shouldThrowOnSave) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage write failure.',
      );
    }
    _session = session;
  }

  @override
  Future<AuthSession?> loadSession() async {
    if (shouldThrow || shouldThrowOnLoad) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage read failure.',
      );
    }
    return _session;
  }

  @override
  Future<void> clearSession() async {
    if (shouldThrow || shouldThrowOnClear) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage clear failure.',
      );
    }
    _session = null;
  }

  @override
  Future<void> markRefreshInFlight({
    required String deviceId,
    required String dispatchedRefreshToken,
  }) async {
    if (shouldThrow || shouldThrowOnInFlight) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage in-flight write failure.',
      );
    }
    _inFlightRecord = RefreshInFlightRecord(
      deviceId: deviceId,
      dispatchedRefreshToken: dispatchedRefreshToken,
      dispatchedAt: DateTime.now().toUtc(),
    );
    _rawInFlightRecord = null;
  }

  @override
  Future<RefreshInFlightRecord?> loadRefreshInFlightRecord() async {
    if (shouldThrow || shouldThrowOnLoad || shouldThrowOnInFlightLoad) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage in-flight read failure.',
      );
    }

    if (_rawInFlightRecord != null) {
      if (_rawInFlightRecord!.trim().isEmpty) {
        throw const SecureStorageUnavailableException(
          'Empty or corrupted persistent refresh guard record.',
        );
      }
      dynamic decoded;
      try {
        decoded = json.decode(_rawInFlightRecord!);
      } catch (e) {
        throw SecureStorageUnavailableException(
          'Corrupted syntax in persistent refresh guard record.',
          e,
        );
      }
      if (decoded is! Map<String, dynamic>) {
        throw const SecureStorageUnavailableException(
          'Incompatible payload structure in persistent refresh guard record.',
        );
      }
      try {
        return RefreshInFlightRecord.fromJson(decoded);
      } catch (e) {
        throw SecureStorageUnavailableException(
          'Invalid or corrupted fields in persistent refresh guard record.',
          e,
        );
      }
    }

    return _inFlightRecord;
  }

  @override
  Future<void> clearRefreshInFlightRecord() async {
    if (shouldThrow || shouldThrowOnClear || shouldThrowOnInFlightClear) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage in-flight clear failure.',
      );
    }
    _inFlightRecord = null;
    _rawInFlightRecord = null;
  }
}
