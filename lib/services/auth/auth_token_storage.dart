import 'dart:convert';
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

/// Abstract contract for storing and retrieving authenticated device session credentials.
abstract class IAuthTokenStorage {
  Future<void> saveSession(AuthSession session);
  Future<AuthSession?> loadSession();
  Future<void> clearSession();
}

/// Production implementation backed strictly by OS-level secure storage (Android Keystore / iOS Keychain).
/// Does NOT fallback silently to SharedPreferences or InMemory storage under any circumstances.
class SecureAuthTokenStorage implements IAuthTokenStorage {
  static const String sessionStorageKey = 'aegis_auth_session_v1';
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
}

/// In-memory storage implementation for deterministic, isolated unit tests.
/// Explicitly prohibited from automatic production fallback.
class InMemoryAuthTokenStorage implements IAuthTokenStorage {
  AuthSession? _session;
  bool shouldThrow = false;
  String? failureMessage;

  InMemoryAuthTokenStorage({AuthSession? initialSession}) : _session = initialSession;

  @override
  Future<void> saveSession(AuthSession session) async {
    if (shouldThrow) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage write failure.',
      );
    }
    _session = session;
  }

  @override
  Future<AuthSession?> loadSession() async {
    if (shouldThrow) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage read failure.',
      );
    }
    return _session;
  }

  @override
  Future<void> clearSession() async {
    if (shouldThrow) {
      throw SecureStorageUnavailableException(
        failureMessage ?? 'Simulated secure storage clear failure.',
      );
    }
    _session = null;
  }
}
