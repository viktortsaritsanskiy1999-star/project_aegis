import 'package:flutter/foundation.dart';
import '../api/vitals_api_models.dart';

String _validatePairingCode(String code) {
  if (!RegExp(r'^[0-9]{6}$').hasMatch(code)) {
    throw const AegisValidationException(
      'pairing_code must be an exact 6-digit numeric string.',
    );
  }
  return code;
}

String _validateRequiredNonBlank(String value, String fieldName) {
  final clean = value.trim();
  if (clean.isEmpty) {
    throw AegisValidationException('$fieldName must not be blank.');
  }
  return clean;
}

String? _validateDeviceId(String? id) {
  if (id == null) return null;
  final clean = id.trim();
  if (clean.isEmpty) {
    throw const AegisValidationException('device_id must not be blank when provided.');
  }
  if (clean.length > 64) {
    throw const AegisValidationException('device_id must not exceed 64 characters.');
  }
  for (final unit in clean.codeUnits) {
    if (unit < 32 || unit > 126) {
      throw const AegisValidationException('device_id contains invalid characters (printable ASCII required).');
    }
  }
  return clean;
}

/// Request payload corresponding to backend `POST /api/v1/auth/pair`.
@immutable
class DevicePairRequest {
  final String pairingCode;
  final String deviceName;
  final String? deviceId;
  final String platform;

  DevicePairRequest({
    required String pairingCode,
    required String deviceName,
    String? deviceId,
    String platform = 'android',
  })  : pairingCode = _validatePairingCode(pairingCode),
        deviceName = _validateRequiredNonBlank(deviceName, 'device_name'),
        deviceId = _validateDeviceId(deviceId),
        platform = _validateRequiredNonBlank(platform, 'platform');

  Map<String, dynamic> toJson() => {
    'pairing_code': pairingCode,
    'device_name': deviceName,
    'platform': platform,
    if (deviceId != null) 'device_id': deviceId,
  };

  @override
  String toString() =>
      'DevicePairRequest(deviceName: $deviceName, platform: $platform, deviceId: $deviceId, pairingCode: [REDACTED])';
}

/// Response payload corresponding to backend `POST /api/v1/auth/pair`.
@immutable
class DevicePairResponse {
  final String accessToken;
  final String tokenType;
  final int expiresIn;
  final DateTime expiresAt;
  final String refreshToken;
  final DateTime refreshExpiresAt;
  final String patientId;
  final String deviceId;
  final String scope;

  const DevicePairResponse({
    required this.accessToken,
    this.tokenType = 'Bearer',
    required this.expiresIn,
    required this.expiresAt,
    required this.refreshToken,
    required this.refreshExpiresAt,
    required this.patientId,
    required this.deviceId,
    required this.scope,
  });

  factory DevicePairResponse.fromJson(Map<String, dynamic> json) {
    final accessToken = json['access_token'] as String?;
    final refreshToken = json['refresh_token'] as String?;
    final patientId = json['patient_id'] as String?;
    final deviceId = json['device_id'] as String?;
    final scope = json['scope'] as String?;
    final expiresIn = json['expires_in'] as int?;
    final expiresAtRaw = json['expires_at'] as String?;
    final refreshExpiresAtRaw = json['refresh_expires_at'] as String?;

    if (accessToken == null || accessToken.trim().isEmpty ||
        refreshToken == null || refreshToken.trim().isEmpty ||
        patientId == null || patientId.trim().isEmpty ||
        deviceId == null || deviceId.trim().isEmpty ||
        scope == null ||
        expiresIn == null ||
        expiresAtRaw == null ||
        refreshExpiresAtRaw == null) {
      throw const AegisValidationException('DevicePairResponse payload is missing required fields.');
    }

    final expiresAt = DateTime.tryParse(expiresAtRaw)?.toUtc();
    final refreshExpiresAt = DateTime.tryParse(refreshExpiresAtRaw)?.toUtc();

    if (expiresAt == null || refreshExpiresAt == null) {
      throw const AegisValidationException('Invalid ISO-8601 datetime format in DevicePairResponse.');
    }

    return DevicePairResponse(
      accessToken: accessToken.trim(),
      tokenType: json['token_type'] as String? ?? 'Bearer',
      expiresIn: expiresIn,
      expiresAt: expiresAt,
      refreshToken: refreshToken.trim(),
      refreshExpiresAt: refreshExpiresAt,
      patientId: patientId.trim(),
      deviceId: deviceId.trim(),
      scope: scope.trim(),
    );
  }

  Map<String, dynamic> toJson() => {
    'access_token': accessToken,
    'token_type': tokenType,
    'expires_in': expiresIn,
    'expires_at': expiresAt.toUtc().toIso8601String(),
    'refresh_token': refreshToken,
    'refresh_expires_at': refreshExpiresAt.toUtc().toIso8601String(),
    'patient_id': patientId,
    'device_id': deviceId,
    'scope': scope,
  };

  @override
  String toString() =>
      'DevicePairResponse(patientId: $patientId, deviceId: $deviceId, scope: $scope, tokenType: $tokenType, expiresIn: $expiresIn, expiresAt: ${expiresAt.toIso8601String()}, refreshExpiresAt: ${refreshExpiresAt.toIso8601String()}, accessToken: [REDACTED], refreshToken: [REDACTED])';
}

/// Request payload corresponding to backend `POST /api/v1/auth/refresh`.
@immutable
class TokenRefreshRequest {
  final String deviceId;
  final String refreshToken;

  TokenRefreshRequest({
    required String deviceId,
    required String refreshToken,
  })  : deviceId = _validateRequiredNonBlank(deviceId, 'device_id'),
        refreshToken = _validateRequiredNonBlank(refreshToken, 'refresh_token');

  Map<String, dynamic> toJson() => {
    'device_id': deviceId,
    'refresh_token': refreshToken,
  };

  @override
  String toString() =>
      'TokenRefreshRequest(deviceId: $deviceId, refreshToken: [REDACTED])';
}

/// Response payload corresponding to backend `POST /api/v1/auth/refresh`.
@immutable
class TokenRefreshResponse {
  final String accessToken;
  final String tokenType;
  final int expiresIn;
  final DateTime expiresAt;
  final String refreshToken;
  final DateTime refreshExpiresAt;
  final String patientId;
  final String deviceId;
  final String scope;

  const TokenRefreshResponse({
    required this.accessToken,
    this.tokenType = 'Bearer',
    required this.expiresIn,
    required this.expiresAt,
    required this.refreshToken,
    required this.refreshExpiresAt,
    required this.patientId,
    required this.deviceId,
    required this.scope,
  });

  factory TokenRefreshResponse.fromJson(Map<String, dynamic> json) {
    final accessToken = json['access_token'] as String?;
    final refreshToken = json['refresh_token'] as String?;
    final patientId = json['patient_id'] as String?;
    final deviceId = json['device_id'] as String?;
    final scope = json['scope'] as String?;
    final expiresIn = json['expires_in'] as int?;
    final expiresAtRaw = json['expires_at'] as String?;
    final refreshExpiresAtRaw = json['refresh_expires_at'] as String?;

    if (accessToken == null || accessToken.trim().isEmpty ||
        refreshToken == null || refreshToken.trim().isEmpty ||
        patientId == null || patientId.trim().isEmpty ||
        deviceId == null || deviceId.trim().isEmpty ||
        scope == null ||
        expiresIn == null ||
        expiresAtRaw == null ||
        refreshExpiresAtRaw == null) {
      throw const AegisValidationException('TokenRefreshResponse payload is missing required fields.');
    }

    final expiresAt = DateTime.tryParse(expiresAtRaw)?.toUtc();
    final refreshExpiresAt = DateTime.tryParse(refreshExpiresAtRaw)?.toUtc();

    if (expiresAt == null || refreshExpiresAt == null) {
      throw const AegisValidationException('Invalid ISO-8601 datetime format in TokenRefreshResponse.');
    }

    return TokenRefreshResponse(
      accessToken: accessToken.trim(),
      tokenType: json['token_type'] as String? ?? 'Bearer',
      expiresIn: expiresIn,
      expiresAt: expiresAt,
      refreshToken: refreshToken.trim(),
      refreshExpiresAt: refreshExpiresAt,
      patientId: patientId.trim(),
      deviceId: deviceId.trim(),
      scope: scope.trim(),
    );
  }

  Map<String, dynamic> toJson() => {
    'access_token': accessToken,
    'token_type': tokenType,
    'expires_in': expiresIn,
    'expires_at': expiresAt.toUtc().toIso8601String(),
    'refresh_token': refreshToken,
    'refresh_expires_at': refreshExpiresAt.toUtc().toIso8601String(),
    'patient_id': patientId,
    'device_id': deviceId,
    'scope': scope,
  };

  @override
  String toString() =>
      'TokenRefreshResponse(patientId: $patientId, deviceId: $deviceId, scope: $scope, tokenType: $tokenType, expiresIn: $expiresIn, expiresAt: ${expiresAt.toIso8601String()}, refreshExpiresAt: ${refreshExpiresAt.toIso8601String()}, accessToken: [REDACTED], refreshToken: [REDACTED])';
}

/// Request payload corresponding to backend `POST /api/v1/auth/revoke`.
@immutable
class DeviceRevokeRequest {
  final String? deviceId;
  final String? refreshToken;

  const DeviceRevokeRequest({
    this.deviceId,
    this.refreshToken,
  });

  Map<String, dynamic> toJson() {
    final map = <String, dynamic>{};
    if (deviceId != null && deviceId!.trim().isNotEmpty) {
      map['device_id'] = deviceId!.trim();
    }
    if (refreshToken != null && refreshToken!.trim().isNotEmpty) {
      map['refresh_token'] = refreshToken!.trim();
    }
    return map;
  }

  @override
  String toString() =>
      'DeviceRevokeRequest(deviceId: $deviceId, refreshToken: ${refreshToken != null ? "[REDACTED]" : "null"})';
}

/// Response payload corresponding to backend `POST /api/v1/auth/revoke`.
@immutable
class DeviceRevokeResponse {
  final String status;
  final String deviceId;
  final bool revoked;

  const DeviceRevokeResponse({
    this.status = 'revoked',
    required this.deviceId,
    this.revoked = true,
  });

  factory DeviceRevokeResponse.fromJson(Map<String, dynamic> json) {
    final deviceId = json['device_id'] as String?;
    if (deviceId == null || deviceId.trim().isEmpty) {
      throw const AegisValidationException('DeviceRevokeResponse payload is missing required device_id.');
    }
    return DeviceRevokeResponse(
      status: json['status'] as String? ?? 'revoked',
      deviceId: deviceId.trim(),
      revoked: json['revoked'] as bool? ?? true,
    );
  }

  Map<String, dynamic> toJson() => {
    'status': status,
    'device_id': deviceId,
    'revoked': revoked,
  };

  @override
  String toString() =>
      'DeviceRevokeResponse(status: $status, deviceId: $deviceId, revoked: $revoked)';
}

/// Standard permissions scopes in Project Aegis.
abstract final class AegisScopes {
  static const String vitalsRead = 'vitals:read';
  static const String vitalsWrite = 'vitals:write';
  static const String cycleRead = 'cycle:read';
  static const String cascadeIntake = 'cascade:intake';
  static const String cascadeRun = 'cascade:run';
  static const String reportsRead = 'reports:read';
  static const String chatRead = 'chat:read';
  static const String chatWrite = 'chat:write';

  /// Standard permissions issued to paired mobile devices in Stage 5.4.
  static const Set<String> standardDeviceScopes = {
    vitalsRead,
    vitalsWrite,
    cycleRead,
    cascadeIntake,
    cascadeRun,
    reportsRead,
    chatRead,
    chatWrite,
  };

  /// Legacy Stage 5.3 baseline scopes.
  static const Set<String> legacyStage53Scopes = {
    vitalsRead,
    vitalsWrite,
    cycleRead,
  };
}

/// Immutable client-side active session model.
@immutable
class AuthSession {
  final String accessToken;
  final String tokenType;
  final int expiresIn;
  final DateTime expiresAt;
  final String refreshToken;
  final DateTime refreshExpiresAt;
  final String patientId;
  final String deviceId;
  final String scope;

  const AuthSession({
    required this.accessToken,
    this.tokenType = 'Bearer',
    required this.expiresIn,
    required this.expiresAt,
    required this.refreshToken,
    required this.refreshExpiresAt,
    required this.patientId,
    required this.deviceId,
    required this.scope,
  });

  Set<String> get scopes =>
      scope.split(RegExp(r'\s+')).where((s) => s.isNotEmpty).toSet();

  bool hasScope(String requiredScope) => scopes.contains(requiredScope);

  bool hasAllScopes(Iterable<String> requiredScopes) =>
      scopes.containsAll(requiredScopes);

  /// Whether this session holds all [AegisScopes.standardDeviceScopes].
  bool get hasStandardDeviceScopes =>
      hasAllScopes(AegisScopes.standardDeviceScopes);

  /// Scopes that are missing from this session relative to [requiredScopes]
  /// (or [AegisScopes.standardDeviceScopes] if not specified).
  Set<String> missingScopes([Iterable<String>? requiredScopes]) {
    final target = requiredScopes ?? AegisScopes.standardDeviceScopes;
    return target.where((s) => !scopes.contains(s)).toSet();
  }

  /// Checks if access token has expired or is about to expire within [skewBuffer] (default: 60s).
  bool isAccessTokenExpired({
    Duration skewBuffer = const Duration(seconds: 60),
    DateTime? now,
  }) {
    final effectiveThreshold = expiresAt.subtract(skewBuffer);
    final currentUtc = (now ?? DateTime.now()).toUtc();
    return currentUtc.isAfter(effectiveThreshold);
  }

  /// Checks if the long-lived refresh token has expired.
  bool isRefreshTokenExpired({
    Duration skewBuffer = Duration.zero,
    DateTime? now,
  }) {
    final effectiveThreshold = refreshExpiresAt.subtract(skewBuffer);
    final currentUtc = (now ?? DateTime.now()).toUtc();
    return currentUtc.isAfter(effectiveThreshold);
  }

  Duration get accessTokenRemaining => expiresAt.difference(DateTime.now().toUtc());

  Duration get refreshTokenRemaining => refreshExpiresAt.difference(DateTime.now().toUtc());

  factory AuthSession.fromPairResponse(DevicePairResponse response) {
    return AuthSession(
      accessToken: response.accessToken,
      tokenType: response.tokenType,
      expiresIn: response.expiresIn,
      expiresAt: response.expiresAt,
      refreshToken: response.refreshToken,
      refreshExpiresAt: response.refreshExpiresAt,
      patientId: response.patientId,
      deviceId: response.deviceId,
      scope: response.scope,
    );
  }

  factory AuthSession.fromRefreshResponse(TokenRefreshResponse response) {
    return AuthSession(
      accessToken: response.accessToken,
      tokenType: response.tokenType,
      expiresIn: response.expiresIn,
      expiresAt: response.expiresAt,
      refreshToken: response.refreshToken,
      refreshExpiresAt: response.refreshExpiresAt,
      patientId: response.patientId,
      deviceId: response.deviceId,
      scope: response.scope,
    );
  }

  factory AuthSession.fromJson(Map<String, dynamic> json) {
    final accessToken = json['access_token'] as String?;
    final refreshToken = json['refresh_token'] as String?;
    final patientId = json['patient_id'] as String?;
    final deviceId = json['device_id'] as String?;
    final scope = json['scope'] as String?;
    final expiresIn = json['expires_in'] as int?;
    final expiresAtRaw = json['expires_at'] as String?;
    final refreshExpiresAtRaw = json['refresh_expires_at'] as String?;

    if (accessToken == null || accessToken.trim().isEmpty ||
        refreshToken == null || refreshToken.trim().isEmpty ||
        patientId == null || patientId.trim().isEmpty ||
        deviceId == null || deviceId.trim().isEmpty ||
        scope == null ||
        expiresIn == null ||
        expiresAtRaw == null ||
        refreshExpiresAtRaw == null) {
      throw const AegisValidationException('Stored AuthSession payload is missing required fields or corrupted.');
    }

    final expiresAt = DateTime.tryParse(expiresAtRaw)?.toUtc();
    final refreshExpiresAt = DateTime.tryParse(refreshExpiresAtRaw)?.toUtc();

    if (expiresAt == null || refreshExpiresAt == null) {
      throw const AegisValidationException('Invalid datetime format in stored AuthSession.');
    }

    return AuthSession(
      accessToken: accessToken.trim(),
      tokenType: json['token_type'] as String? ?? 'Bearer',
      expiresIn: expiresIn,
      expiresAt: expiresAt,
      refreshToken: refreshToken.trim(),
      refreshExpiresAt: refreshExpiresAt,
      patientId: patientId.trim(),
      deviceId: deviceId.trim(),
      scope: scope.trim(),
    );
  }

  Map<String, dynamic> toJson() => {
    'access_token': accessToken,
    'token_type': tokenType,
    'expires_in': expiresIn,
    'expires_at': expiresAt.toUtc().toIso8601String(),
    'refresh_token': refreshToken,
    'refresh_expires_at': refreshExpiresAt.toUtc().toIso8601String(),
    'patient_id': patientId,
    'device_id': deviceId,
    'scope': scope,
  };

  AuthSession copyWith({
    String? accessToken,
    String? tokenType,
    int? expiresIn,
    DateTime? expiresAt,
    String? refreshToken,
    DateTime? refreshExpiresAt,
    String? patientId,
    String? deviceId,
    String? scope,
  }) {
    return AuthSession(
      accessToken: accessToken ?? this.accessToken,
      tokenType: tokenType ?? this.tokenType,
      expiresIn: expiresIn ?? this.expiresIn,
      expiresAt: expiresAt ?? this.expiresAt,
      refreshToken: refreshToken ?? this.refreshToken,
      refreshExpiresAt: refreshExpiresAt ?? this.refreshExpiresAt,
      patientId: patientId ?? this.patientId,
      deviceId: deviceId ?? this.deviceId,
      scope: scope ?? this.scope,
    );
  }

  @override
  String toString() =>
      'AuthSession(patientId: $patientId, deviceId: $deviceId, scope: $scope, expiresAt: ${expiresAt.toIso8601String()}, refreshExpiresAt: ${refreshExpiresAt.toIso8601String()}, accessToken: [REDACTED], refreshToken: [REDACTED])';
}
