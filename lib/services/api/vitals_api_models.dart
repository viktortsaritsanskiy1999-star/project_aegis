import 'package:flutter/foundation.dart';
import '../health_data_provider.dart';

/// Exceptions hierarchy for Aegis API communication
@immutable
abstract class AegisApiException implements Exception {
  final String message;
  final int? statusCode;
  final dynamic responseBody;

  const AegisApiException(this.message, {this.statusCode, this.responseBody});

  @override
  String toString() => '$runtimeType: $message (status: $statusCode)';
}

class AegisNetworkException extends AegisApiException {
  const AegisNetworkException(super.message, {super.statusCode, super.responseBody});
}

class AegisTimeoutException extends AegisApiException {
  const AegisTimeoutException(super.message, {super.statusCode, super.responseBody});
}

class AegisAuthException extends AegisApiException {
  const AegisAuthException(super.message, {super.statusCode, super.responseBody});
}

class AegisForbiddenException extends AegisApiException {
  const AegisForbiddenException(
    super.message, {
    super.statusCode = 403,
    super.responseBody,
  });
}

class AegisBadRequestException extends AegisApiException {
  const AegisBadRequestException(super.message, {super.statusCode, super.responseBody});
}

class AegisCycleNotFoundException extends AegisApiException {
  const AegisCycleNotFoundException(super.message, {super.statusCode, super.responseBody});
}

class AegisConflictException extends AegisApiException {
  const AegisConflictException(super.message, {super.statusCode, super.responseBody});
}

class AegisValidationException extends AegisApiException {
  const AegisValidationException(super.message, {super.statusCode, super.responseBody});
}

class AegisServerException extends AegisApiException {
  const AegisServerException(super.message, {super.statusCode, super.responseBody});
}

class AegisRateLimitException extends AegisApiException {
  final Duration? retryAfter;

  const AegisRateLimitException(
    super.message, {
    super.statusCode = 429,
    super.responseBody,
    this.retryAfter,
  });

  @override
  String toString() =>
      'AegisRateLimitException: $message (retryAfter: ${retryAfter != null ? "${retryAfter!.inSeconds}s" : "null"}, status: $statusCode)';
}

/// DTO corresponding to backend HealthMeasurementCreate schema
class HealthMeasurementCreate {
  final String? cycleId;
  final DateTime measuredAt;
  final String source;
  final int? pulse;
  final int? systolic;
  final int? diastolic;
  final int? spo2;
  final String? sessionId;
  final bool isSimulated;
  final bool? isHistorical;
  final String phase;
  final String note;
  final Map<String, dynamic>? rawMetadata;

  const HealthMeasurementCreate({
    this.cycleId,
    required this.measuredAt,
    required this.source,
    this.pulse,
    this.systolic,
    this.diastolic,
    this.spo2,
    this.sessionId,
    this.isSimulated = false,
    this.isHistorical,
    this.phase = 'standby',
    this.note = '',
    this.rawMetadata,
  });

  Map<String, dynamic> toJson() {
    final map = <String, dynamic>{
      'measured_at': measuredAt.toUtc().toIso8601String(),
      'source': source,
      'is_simulated': isSimulated,
      'phase': phase,
      'note': note,
    };
    if (cycleId != null) map['cycle_id'] = cycleId;
    if (pulse != null) map['pulse'] = pulse;
    if (systolic != null) map['systolic'] = systolic;
    if (diastolic != null) map['diastolic'] = diastolic;
    if (spo2 != null) map['spo2'] = spo2;
    if (sessionId != null) map['session_id'] = sessionId;
    if (isHistorical != null) map['is_historical'] = isHistorical;
    if (rawMetadata != null && rawMetadata!.isNotEmpty) {
      map['raw_metadata'] = rawMetadata;
    }
    return map;
  }

  factory HealthMeasurementCreate.fromJson(Map<String, dynamic> json) {
    return HealthMeasurementCreate(
      cycleId: json['cycle_id'] as String?,
      measuredAt: DateTime.parse(json['measured_at'] as String),
      source: json['source'] as String? ?? 'unknown',
      pulse: json['pulse'] as int?,
      systolic: json['systolic'] as int?,
      diastolic: json['diastolic'] as int?,
      spo2: json['spo2'] as int?,
      sessionId: json['session_id'] as String?,
      isSimulated: json['is_simulated'] == true,
      isHistorical: json['is_historical'] as bool?,
      phase: json['phase'] as String? ?? 'standby',
      note: json['note'] as String? ?? '',
      rawMetadata: json['raw_metadata'] != null
          ? Map<String, dynamic>.from(json['raw_metadata'] as Map)
          : null,
    );
  }

  HealthMeasurementCreate copyWith({
    String? cycleId,
    DateTime? measuredAt,
    String? source,
    int? pulse,
    int? systolic,
    int? diastolic,
    int? spo2,
    String? sessionId,
    bool? isSimulated,
    bool? isHistorical,
    String? phase,
    String? note,
    Map<String, dynamic>? rawMetadata,
  }) {
    return HealthMeasurementCreate(
      cycleId: cycleId ?? this.cycleId,
      measuredAt: measuredAt ?? this.measuredAt,
      source: source ?? this.source,
      pulse: pulse ?? this.pulse,
      systolic: systolic ?? this.systolic,
      diastolic: diastolic ?? this.diastolic,
      spo2: spo2 ?? this.spo2,
      sessionId: sessionId ?? this.sessionId,
      isSimulated: isSimulated ?? this.isSimulated,
      isHistorical: isHistorical ?? this.isHistorical,
      phase: phase ?? this.phase,
      note: note ?? this.note,
      rawMetadata: rawMetadata ?? this.rawMetadata,
    );
  }

  factory HealthMeasurementCreate.fromHealthMeasurement(
    HealthMeasurement measurement, {
    String? cycleId,
    String? idempotencyKey,
    String? sessionId,
    String? phase,
  }) {
    final metadata = Map<String, dynamic>.from(measurement.metadata);
    if (idempotencyKey != null) {
      metadata['idempotency_key'] = idempotencyKey;
    }
    return HealthMeasurementCreate(
      cycleId: cycleId,
      measuredAt: measurement.timestamp.toUtc(),
      source: measurement.source.rawValue,
      pulse: measurement.pulse,
      systolic: measurement.systolic,
      diastolic: measurement.diastolic,
      spo2: measurement.spo2,
      sessionId: sessionId,
      isSimulated: measurement.isSimulated ||
          measurement.source == HealthDataSource.testChip ||
          measurement.source == HealthDataSource.simulated,
      isHistorical: measurement.isHistorical,
      phase: phase ?? (measurement.isHistorical ? 'historical' : 'standby'),
      note: measurement.note,
      rawMetadata: metadata.isNotEmpty ? metadata : null,
    );
  }
}

/// DTO corresponding to backend HealthMeasurementCreateBatch schema
class HealthMeasurementCreateBatch {
  final String cycleId;
  final String? sessionId;
  final bool? asHistorical;
  final List<HealthMeasurementCreate> measurements;

  const HealthMeasurementCreateBatch({
    required this.cycleId,
    this.sessionId,
    this.asHistorical,
    required this.measurements,
  });

  Map<String, dynamic> toJson() {
    final map = <String, dynamic>{
      'cycle_id': cycleId,
      'measurements': measurements.map((m) => m.toJson()).toList(),
    };
    if (sessionId != null) map['session_id'] = sessionId;
    if (asHistorical != null) map['as_historical'] = asHistorical;
    return map;
  }
}

/// DTO corresponding to backend HealthMeasurementResponse schema
class HealthMeasurementResponse {
  final int id;
  final String cycleId;
  final String? sessionId;
  final int? pulse;
  final int? systolic;
  final int? diastolic;
  final int? spo2;
  final String source;
  final bool isSimulated;
  final bool isHistorical;
  final DateTime measuredAt;
  final DateTime ingestedAt;
  final String phase;
  final String note;
  final String status;

  const HealthMeasurementResponse({
    required this.id,
    required this.cycleId,
    this.sessionId,
    this.pulse,
    this.systolic,
    this.diastolic,
    this.spo2,
    required this.source,
    required this.isSimulated,
    required this.isHistorical,
    required this.measuredAt,
    required this.ingestedAt,
    required this.phase,
    required this.note,
    required this.status,
  });

  factory HealthMeasurementResponse.fromJson(Map<String, dynamic> json) {
    return HealthMeasurementResponse(
      id: json['id'] as int? ?? 0,
      cycleId: json['cycle_id'] as String? ?? '',
      sessionId: json['session_id'] as String?,
      pulse: json['pulse'] as int?,
      systolic: json['systolic'] as int?,
      diastolic: json['diastolic'] as int?,
      spo2: json['spo2'] as int?,
      source: json['source'] as String? ?? 'unknown',
      isSimulated: json['is_simulated'] == true,
      isHistorical: json['is_historical'] == true,
      measuredAt: DateTime.parse(json['measured_at'] as String),
      ingestedAt: DateTime.parse(json['ingested_at'] as String),
      phase: json['phase'] as String? ?? 'standby',
      note: json['note'] as String? ?? '',
      status: json['status'] as String? ?? 'recorded',
    );
  }
}

/// DTO corresponding to backend HealthMeasurementBatchResponse schema
class HealthMeasurementBatchResponse {
  final String cycleId;
  final int totalReceived;
  final int recordedCount;
  final int deduplicatedCount;
  final List<HealthMeasurementResponse> items;

  const HealthMeasurementBatchResponse({
    required this.cycleId,
    required this.totalReceived,
    required this.recordedCount,
    required this.deduplicatedCount,
    required this.items,
  });

  factory HealthMeasurementBatchResponse.fromJson(Map<String, dynamic> json) {
    return HealthMeasurementBatchResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      totalReceived: json['total_received'] as int? ?? 0,
      recordedCount: json['recorded_count'] as int? ?? 0,
      deduplicatedCount: json['deduplicated_count'] as int? ?? 0,
      items: (json['items'] as List<dynamic>? ?? [])
          .map((i) => HealthMeasurementResponse.fromJson(i as Map<String, dynamic>))
          .toList(),
    );
  }
}

/// DTO corresponding to backend VitalsStreamResponse schema
class VitalsStreamResponse {
  final String cycleId;
  final String bloodPressure;
  final String heartRate;
  final String spo2;
  final DateTime? latestMeasuredAt;
  final String? source;
  final bool? isLive;
  final String recencyStatus;

  const VitalsStreamResponse({
    required this.cycleId,
    required this.bloodPressure,
    required this.heartRate,
    required this.spo2,
    this.latestMeasuredAt,
    this.source,
    this.isLive,
    required this.recencyStatus,
  });

  factory VitalsStreamResponse.fromJson(Map<String, dynamic> json) {
    return VitalsStreamResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      bloodPressure: json['blood_pressure'] as String? ?? '--/-- mmHg',
      heartRate: json['heart_rate'] as String? ?? '-- bpm',
      spo2: json['spo2'] as String? ?? '--%',
      latestMeasuredAt: json['latest_measured_at'] != null
          ? DateTime.tryParse(json['latest_measured_at'] as String)
          : null,
      source: json['source'] as String?,
      isLive: json['is_live'] as bool?,
      recencyStatus: json['recency_status'] as String? ?? 'unknown',
    );
  }
}

/// DTO corresponding to backend MedicalCycleResponse schema
class MedicalCycleResponse {
  final String cycleId;
  final String? patientId;
  final String status;
  final DateTime? startedAt;
  final DateTime? closedAt;

  const MedicalCycleResponse({
    required this.cycleId,
    this.patientId,
    required this.status,
    this.startedAt,
    this.closedAt,
  });

  factory MedicalCycleResponse.fromJson(Map<String, dynamic> json) {
    final rawPatientId = json['patient_id'] as String?;
    return MedicalCycleResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      patientId: (rawPatientId != null && rawPatientId.trim().isNotEmpty)
          ? rawPatientId.trim()
          : null,
      status: json['status'] as String? ?? '',
      startedAt: json['started_at'] != null
          ? DateTime.tryParse(json['started_at'] as String)
          : null,
      closedAt: json['closed_at'] != null
          ? DateTime.tryParse(json['closed_at'] as String)
          : null,
    );
  }
}
