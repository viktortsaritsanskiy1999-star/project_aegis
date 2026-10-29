import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:image_picker/image_picker.dart';

/// Raw answers submitted from the 17 Flutter anamnesis cards.
/// Matches backend AnamnesisQuestionnaireAnswers schema.
@immutable
class AnamnesisQuestionnaireAnswers {
  final String ageConfirmation;
  final String gender;
  final int age;
  final int height;
  final int weight;
  final String nicotineStatus;
  final String? nicotineYears;
  final String? nicotineFreq;
  final String alcoholStatus;
  final String? alcoholType;
  final String specFactors;
  final String sleepQuality;
  final String dietTypes;
  final String dietHabits;
  final String giSignals;
  final String workType;
  final String? workSchedule;
  final String? sportLevel;
  final String chronicDiseases;
  final String? oncoStatus;
  final String recentIllness;
  final String criticalInfections;
  final String heredityRisks;
  final String medications;
  final String? userTextMedications;
  final String? userTextHeredityRisks;
  final String? userTextChronicDiseases;
  final String? userTextRecentIllness;
  final String? userTextCriticalInfections;
  final String? userTextSpecFactors;
  final String? userTextDietHabits;
  final String? userTextGiSignals;

  const AnamnesisQuestionnaireAnswers({
    required this.ageConfirmation,
    required this.gender,
    required this.age,
    required this.height,
    required this.weight,
    required this.nicotineStatus,
    this.nicotineYears,
    this.nicotineFreq,
    required this.alcoholStatus,
    this.alcoholType,
    required this.specFactors,
    required this.sleepQuality,
    required this.dietTypes,
    required this.dietHabits,
    required this.giSignals,
    required this.workType,
    this.workSchedule,
    this.sportLevel,
    required this.chronicDiseases,
    this.oncoStatus,
    required this.recentIllness,
    required this.criticalInfections,
    required this.heredityRisks,
    required this.medications,
    this.userTextMedications,
    this.userTextHeredityRisks,
    this.userTextChronicDiseases,
    this.userTextRecentIllness,
    this.userTextCriticalInfections,
    this.userTextSpecFactors,
    this.userTextDietHabits,
    this.userTextGiSignals,
  });

  factory AnamnesisQuestionnaireAnswers.fromMap(Map<String, dynamic> map) {
    return AnamnesisQuestionnaireAnswers(
      ageConfirmation: map['age_confirmation'] as String? ?? 'Confirmed',
      gender: map['gender'] as String? ?? 'Male',
      age: map['age'] as int? ?? 30,
      height: map['height'] as int? ?? 175,
      weight: map['weight'] as int? ?? 70,
      nicotineStatus: map['nicotine_status'] as String? ?? 'Non-user',
      nicotineYears: map['nicotine_years'] as String?,
      nicotineFreq: map['nicotine_freq'] as String?,
      alcoholStatus: map['alcohol_status'] as String? ?? 'Non-drinker',
      alcoholType: map['alcohol_type'] as String?,
      specFactors: map['spec_factors'] as String? ?? 'None',
      sleepQuality: map['sleep_quality'] as String? ?? 'Normal',
      dietTypes: map['diet_types'] as String? ?? 'Balanced',
      dietHabits: map['diet_habits'] as String? ?? 'Regular',
      giSignals: map['gi_signals'] as String? ?? 'None',
      workType: map['work_type'] as String? ?? 'Sedentary',
      workSchedule: map['work_schedule'] as String?,
      sportLevel: map['sport_level'] as String?,
      chronicDiseases: map['chronic_diseases'] as String? ?? 'None',
      oncoStatus: map['onco_status'] as String?,
      recentIllness: map['recent_illness'] as String? ?? 'None',
      criticalInfections: map['critical_infections'] as String? ?? 'None',
      heredityRisks: map['heredity_risks'] as String? ?? 'None',
      medications: map['medications'] as String? ?? 'None',
      userTextMedications: map['user_text_medications'] as String?,
      userTextHeredityRisks: map['user_text_heredity_risks'] as String?,
      userTextChronicDiseases: map['user_text_chronic_diseases'] as String?,
      userTextRecentIllness: map['user_text_recent_illness'] as String?,
      userTextCriticalInfections: map['user_text_critical_infections'] as String?,
      userTextSpecFactors: map['user_text_spec_factors'] as String?,
      userTextDietHabits: map['user_text_diet_habits'] as String?,
      userTextGiSignals: map['user_text_gi_signals'] as String?,
    );
  }

  Map<String, dynamic> toJson() {
    final map = <String, dynamic>{
      'age_confirmation': ageConfirmation,
      'gender': gender,
      'age': age,
      'height': height,
      'weight': weight,
      'nicotine_status': nicotineStatus,
      'alcohol_status': alcoholStatus,
      'spec_factors': specFactors,
      'sleep_quality': sleepQuality,
      'diet_types': dietTypes,
      'diet_habits': dietHabits,
      'gi_signals': giSignals,
      'work_type': workType,
      'chronic_diseases': chronicDiseases,
      'recent_illness': recentIllness,
      'critical_infections': criticalInfections,
      'heredity_risks': heredityRisks,
      'medications': medications,
    };
    if (nicotineYears != null) map['nicotine_years'] = nicotineYears;
    if (nicotineFreq != null) map['nicotine_freq'] = nicotineFreq;
    if (alcoholType != null) map['alcohol_type'] = alcoholType;
    if (workSchedule != null) map['work_schedule'] = workSchedule;
    if (sportLevel != null) map['sport_level'] = sportLevel;
    if (oncoStatus != null) map['onco_status'] = oncoStatus;
    if (userTextMedications != null) map['user_text_medications'] = userTextMedications;
    if (userTextHeredityRisks != null) map['user_text_heredity_risks'] = userTextHeredityRisks;
    if (userTextChronicDiseases != null) map['user_text_chronic_diseases'] = userTextChronicDiseases;
    if (userTextRecentIllness != null) map['user_text_recent_illness'] = userTextRecentIllness;
    if (userTextCriticalInfections != null) map['user_text_critical_infections'] = userTextCriticalInfections;
    if (userTextSpecFactors != null) map['user_text_spec_factors'] = userTextSpecFactors;
    if (userTextDietHabits != null) map['user_text_diet_habits'] = userTextDietHabits;
    if (userTextGiSignals != null) map['user_text_gi_signals'] = userTextGiSignals;
    return map;
  }
}

/// Request payload for submitting anamnesis answers.
/// Matches backend ClientAnamnesisIntakeRequest schema.
/// Strictly excludes client-side cycle assignment: server resolves active cycle for authenticated DeviceSession.
@immutable
class AnamnesisIntakeRequest {
  final AnamnesisQuestionnaireAnswers answers;

  const AnamnesisIntakeRequest({
    required this.answers,
  });

  Map<String, dynamic> toJson() => {
    'answers': answers.toJson(),
  };

  factory AnamnesisIntakeRequest.fromJson(Map<String, dynamic> json) {
    return AnamnesisIntakeRequest(
      answers: AnamnesisQuestionnaireAnswers.fromMap(
        Map<String, dynamic>.from(json['answers'] as Map? ?? {}),
      ),
    );
  }
}

/// Confirmation response after submitting raw anamnesis questionnaire.
/// Matches backend AnamnesisIntakeClientResponse schema.
@immutable
class AnamnesisIntakeClientResponse {
  final String cycleId;
  final String status;
  final int intakeVersion;
  final String createdAtUtc;
  final String privacyNotice;

  const AnamnesisIntakeClientResponse({
    required this.cycleId,
    this.status = 'processed',
    required this.intakeVersion,
    required this.createdAtUtc,
    this.privacyNotice =
        'Anamnesis processed in strict blind mode; raw answers stored securely under server_only privacy level.',
  });

  factory AnamnesisIntakeClientResponse.fromJson(Map<String, dynamic> json) {
    return AnamnesisIntakeClientResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      status: json['status'] as String? ?? 'processed',
      intakeVersion: json['intake_version'] as int? ?? 1,
      createdAtUtc: json['created_at_utc'] as String? ?? '',
      privacyNotice: json['privacy_notice'] as String? ?? '',
    );
  }

  Map<String, dynamic> toJson() => {
    'cycle_id': cycleId,
    'status': status,
    'intake_version': intakeVersion,
    'created_at_utc': createdAtUtc,
    'privacy_notice': privacyNotice,
  };
}

/// Clinical pipeline readiness status summary for the authenticated patient.
/// Matches backend ClientCascadeStatusResponse schema.
@immutable
class ClientCascadeStatusResponse {
  final String patientId;
  final String deviceId;
  final bool hasActiveCycle;
  final String? activeCycleId;
  final bool hasPreparatoryAnamnesis;
  final bool hasPreparatoryLabs;
  final bool hasPreparatoryEcg;
  final bool isPrimaryConsiliumCompleted;
  final bool hasUnreviewedLabChanges;

  const ClientCascadeStatusResponse({
    required this.patientId,
    required this.deviceId,
    required this.hasActiveCycle,
    this.activeCycleId,
    this.hasPreparatoryAnamnesis = false,
    this.hasPreparatoryLabs = false,
    this.hasPreparatoryEcg = false,
    this.isPrimaryConsiliumCompleted = false,
    this.hasUnreviewedLabChanges = false,
  });

  factory ClientCascadeStatusResponse.fromJson(Map<String, dynamic> json) {
    return ClientCascadeStatusResponse(
      patientId: json['patient_id'] as String? ?? '',
      deviceId: json['device_id'] as String? ?? '',
      hasActiveCycle: json['has_active_cycle'] == true,
      activeCycleId: json['active_cycle_id'] as String?,
      hasPreparatoryAnamnesis: json['has_preparatory_anamnesis'] == true,
      hasPreparatoryLabs: json['has_preparatory_labs'] == true,
      hasPreparatoryEcg: json['has_preparatory_ecg'] == true,
      isPrimaryConsiliumCompleted: json['is_primary_consilium_completed'] == true,
      hasUnreviewedLabChanges: json['has_unreviewed_lab_changes'] == true,
    );
  }

  Map<String, dynamic> toJson() => {
    'patient_id': patientId,
    'device_id': deviceId,
    'has_active_cycle': hasActiveCycle,
    if (activeCycleId != null) 'active_cycle_id': activeCycleId,
    'has_preparatory_anamnesis': hasPreparatoryAnamnesis,
    'has_preparatory_labs': hasPreparatoryLabs,
    'has_preparatory_ecg': hasPreparatoryEcg,
    'is_primary_consilium_completed': isPrimaryConsiliumCompleted,
    'has_unreviewed_lab_changes': hasUnreviewedLabChanges,
  };
}

/// Reference interval for a laboratory marker.
/// Matches backend LabReferenceRange schema.
@immutable
class LabReferenceRange {
  final double? low;
  final double? high;
  final String? textRange;

  const LabReferenceRange({this.low, this.high, this.textRange});

  factory LabReferenceRange.fromJson(Map<String, dynamic> json) {
    return LabReferenceRange(
      low: (json['low'] as num?)?.toDouble(),
      high: (json['high'] as num?)?.toDouble(),
      textRange: json['text_range'] as String?,
    );
  }

  Map<String, dynamic> toJson() => {
    if (low != null) 'low': low,
    if (high != null) 'high': high,
    if (textRange != null) 'text_range': textRange,
  };
}

/// Single immutable observation record in the laboratory ledger.
/// Matches backend LabObservationEntry schema.
@immutable
class LabObservationEntry {
  final String observationId;
  final String markerKey;
  final String displayName;
  final dynamic value;
  final double? numericValue;
  final String? unit;
  final String? sampleDate;
  final LabReferenceRange? referenceRange;
  final String eventType;
  final String? supersedesObservationId;
  final String? supersededByObservationId;
  final String? correctionReason;
  final bool isActive;
  final bool isCriticalFlag;
  final bool isUncertain;
  final String? uncertaintyNote;
  final bool isRecognitionAmbiguous;
  final String recordedAt;
  final String? studyId;

  const LabObservationEntry({
    required this.observationId,
    required this.markerKey,
    required this.displayName,
    this.value,
    this.numericValue,
    this.unit,
    this.sampleDate,
    this.referenceRange,
    this.eventType = 'initial',
    this.supersedesObservationId,
    this.supersededByObservationId,
    this.correctionReason,
    this.isActive = true,
    this.isCriticalFlag = false,
    this.isUncertain = false,
    this.uncertaintyNote,
    this.isRecognitionAmbiguous = false,
    required this.recordedAt,
    this.studyId,
  });

  factory LabObservationEntry.fromJson(Map<String, dynamic> json) {
    return LabObservationEntry(
      observationId: json['observation_id'] as String? ?? '',
      markerKey: json['marker_key'] as String? ?? '',
      displayName: json['display_name'] as String? ?? '',
      value: json['value'],
      numericValue: (json['numeric_value'] as num?)?.toDouble(),
      unit: json['unit'] as String?,
      sampleDate: json['sample_date'] as String?,
      referenceRange: json['reference_range'] != null
          ? LabReferenceRange.fromJson(
              Map<String, dynamic>.from(json['reference_range'] as Map),
            )
          : null,
      eventType: json['event_type'] as String? ?? 'initial',
      supersedesObservationId: json['supersedes_observation_id'] as String?,
      supersededByObservationId: json['superseded_by_observation_id'] as String?,
      correctionReason: json['correction_reason'] as String?,
      isActive: json['is_active'] != false,
      isCriticalFlag: json['is_critical_flag'] == true,
      isUncertain: json['is_uncertain'] == true,
      uncertaintyNote: json['uncertainty_note'] as String?,
      isRecognitionAmbiguous: json['is_recognition_ambiguous'] == true,
      recordedAt: json['recorded_at'] as String? ?? '',
      studyId: json['study_id'] as String?,
    );
  }

  Map<String, dynamic> toJson() => {
    'observation_id': observationId,
    'marker_key': markerKey,
    'display_name': displayName,
    if (value != null) 'value': value,
    if (numericValue != null) 'numeric_value': numericValue,
    if (unit != null) 'unit': unit,
    if (sampleDate != null) 'sample_date': sampleDate,
    if (referenceRange != null) 'reference_range': referenceRange!.toJson(),
    'event_type': eventType,
    if (supersedesObservationId != null)
      'supersedes_observation_id': supersedesObservationId,
    if (supersededByObservationId != null)
      'superseded_by_observation_id': supersededByObservationId,
    if (correctionReason != null) 'correction_reason': correctionReason,
    'is_active': isActive,
    'is_critical_flag': isCriticalFlag,
    'is_uncertain': isUncertain,
    if (uncertaintyNote != null) 'uncertainty_note': uncertaintyNote,
    'is_recognition_ambiguous': isRecognitionAmbiguous,
    'recorded_at': recordedAt,
    if (studyId != null) 'study_id': studyId,
  };
}

/// Authoritative laboratory ledger state and unreviewed changes indicator.
/// Matches backend LabLedgerStateResponse schema.
@immutable
class LabLedgerStateResponse {
  final String cycleId;
  final int ledgerVersion;
  final int lastEvaluatedLedgerVersion;
  final bool hasUnreviewedLabChanges;
  final String evaluationStatus;
  final String evaluationNotice;
  final bool hasCriticalFlags;
  final String safetyNotice;
  final Map<String, LabObservationEntry> activeMarkers;
  final List<LabObservationEntry> observationsHistory;

  const LabLedgerStateResponse({
    required this.cycleId,
    required this.ledgerVersion,
    required this.lastEvaluatedLedgerVersion,
    required this.hasUnreviewedLabChanges,
    required this.evaluationStatus,
    required this.evaluationNotice,
    this.hasCriticalFlags = false,
    required this.safetyNotice,
    this.activeMarkers = const {},
    this.observationsHistory = const [],
  });

  factory LabLedgerStateResponse.fromJson(Map<String, dynamic> json) {
    final activeMap = <String, LabObservationEntry>{};
    if (json['active_markers'] is Map) {
      (json['active_markers'] as Map).forEach((k, v) {
        if (v is Map) {
          activeMap[k.toString()] =
              LabObservationEntry.fromJson(Map<String, dynamic>.from(v));
        }
      });
    }

    final historyList = <LabObservationEntry>[];
    if (json['observations_history'] is List) {
      for (final item in json['observations_history'] as List) {
        if (item is Map) {
          historyList.add(
            LabObservationEntry.fromJson(Map<String, dynamic>.from(item)),
          );
        }
      }
    }

    return LabLedgerStateResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      ledgerVersion: json['ledger_version'] as int? ?? 0,
      lastEvaluatedLedgerVersion: json['last_evaluated_ledger_version'] as int? ?? 0,
      hasUnreviewedLabChanges: json['has_unreviewed_lab_changes'] == true,
      evaluationStatus: json['evaluation_status'] as String? ?? '',
      evaluationNotice: json['evaluation_notice'] as String? ?? '',
      hasCriticalFlags: json['has_critical_flags'] == true,
      safetyNotice: json['safety_notice'] as String? ?? '',
      activeMarkers: activeMap,
      observationsHistory: historyList,
    );
  }

  Map<String, dynamic> toJson() => {
    'cycle_id': cycleId,
    'ledger_version': ledgerVersion,
    'last_evaluated_ledger_version': lastEvaluatedLedgerVersion,
    'has_unreviewed_lab_changes': hasUnreviewedLabChanges,
    'evaluation_status': evaluationStatus,
    'evaluation_notice': evaluationNotice,
    'has_critical_flags': hasCriticalFlags,
    'safety_notice': safetyNotice,
    'active_markers': activeMarkers.map((k, v) => MapEntry(k, v.toJson())),
    'observations_history': observationsHistory.map((v) => v.toJson()).toList(),
  };
}

/// Numeric vitals thresholds derived from Chief Physician consilium evaluation.
/// Matches backend ClientVitalsThresholds schema.
@immutable
class ClientVitalsThresholds {
  final int minPessimumBpm;
  final int maxPessimumBpm;
  final int maxSpikeDeltaBpm;

  const ClientVitalsThresholds({
    required this.minPessimumBpm,
    required this.maxPessimumBpm,
    required this.maxSpikeDeltaBpm,
  });

  factory ClientVitalsThresholds.fromJson(Map<String, dynamic> json) {
    return ClientVitalsThresholds(
      minPessimumBpm: json['min_pessimum_bpm'] as int? ?? 45,
      maxPessimumBpm: json['max_pessimum_bpm'] as int? ?? 115,
      maxSpikeDeltaBpm: json['max_spike_delta_bpm'] as int? ?? 25,
    );
  }

  Map<String, dynamic> toJson() => {
    'min_pessimum_bpm': minPessimumBpm,
    'max_pessimum_bpm': maxPessimumBpm,
    'max_spike_delta_bpm': maxSpikeDeltaBpm,
  };
}

/// Dated organ entry for client silhouette visualization.
/// Matches backend ClientDatedSystemEntry schema.
@immutable
class ClientDatedSystemEntry {
  final String date;
  final String summary;
  final bool hasChanges;

  const ClientDatedSystemEntry({
    required this.date,
    required this.summary,
    this.hasChanges = true,
  });

  factory ClientDatedSystemEntry.fromJson(Map<String, dynamic> json) {
    return ClientDatedSystemEntry(
      date: json['date'] as String? ?? '',
      summary: json['summary'] as String? ?? '',
      hasChanges: json['has_changes'] != false,
    );
  }

  Map<String, dynamic> toJson() => {
    'date': date,
    'summary': summary,
    'has_changes': hasChanges,
  };
}

/// Client-facing user report content.
/// Matches backend ClientReportContent schema (strictly forbids internal fields).
@immutable
class ClientReportContent {
  final String title;
  final String summary;
  final String? reportDate;
  final String? systemKey;
  final bool? hasChanges;
  final List<String> highlights;
  final Map<String, dynamic> metrics;
  final List<ClientDatedSystemEntry> entries;

  const ClientReportContent({
    this.title = 'Aegis Wellness Summary',
    required this.summary,
    this.reportDate,
    this.systemKey,
    this.hasChanges,
    this.highlights = const [],
    this.metrics = const {},
    this.entries = const [],
  });

  factory ClientReportContent.fromJson(Map<String, dynamic> json) {
    final highlightsList = (json['highlights'] as List<dynamic>? ?? [])
        .map((e) => e.toString())
        .toList();
    final metricsMap = json['metrics'] is Map
        ? Map<String, dynamic>.from(json['metrics'] as Map)
        : <String, dynamic>{};
    final entriesList = (json['entries'] as List<dynamic>? ?? [])
        .whereType<Map>()
        .map((e) => ClientDatedSystemEntry.fromJson(Map<String, dynamic>.from(e)))
        .toList();

    return ClientReportContent(
      title: json['title'] as String? ?? 'Aegis Wellness Summary',
      summary: json['summary'] as String? ?? '',
      reportDate: json['report_date'] as String?,
      systemKey: json['system_key'] as String?,
      hasChanges: json['has_changes'] as bool?,
      highlights: highlightsList,
      metrics: metricsMap,
      entries: entriesList,
    );
  }

  Map<String, dynamic> toJson() => {
    'title': title,
    'summary': summary,
    if (reportDate != null) 'report_date': reportDate,
    if (systemKey != null) 'system_key': systemKey,
    if (hasChanges != null) 'has_changes': hasChanges,
    'highlights': highlights,
    'metrics': metrics,
    'entries': entries.map((e) => e.toJson()).toList(),
  };
}

/// Client-safe model report representation.
/// Matches backend ClientModelReportResponse schema.
@immutable
class ClientModelReportResponse {
  final String reportId;
  final String cycleId;
  final String? sprintId;
  final String? sessionId;
  final String model;
  final String reportType;
  final String privacyLevel;
  final int version;
  final ClientReportContent content;
  final DateTime createdAt;

  const ClientModelReportResponse({
    required this.reportId,
    required this.cycleId,
    this.sprintId,
    this.sessionId,
    required this.model,
    required this.reportType,
    this.privacyLevel = 'client_facing',
    required this.version,
    required this.content,
    required this.createdAt,
  });

  factory ClientModelReportResponse.fromJson(Map<String, dynamic> json) {
    final privacy = json['privacy_level'] as String? ?? 'client_facing';
    if (privacy != 'client_facing') {
      throw FormatException(
        'Security Violation: Received report with non-client_facing privacy_level ($privacy)',
      );
    }
    return ClientModelReportResponse(
      reportId: json['report_id'] as String? ?? '',
      cycleId: json['cycle_id'] as String? ?? '',
      sprintId: json['sprint_id'] as String?,
      sessionId: json['session_id'] as String?,
      model: json['model'] as String? ?? '',
      reportType: json['report_type'] as String? ?? '',
      privacyLevel: privacy,
      version: json['version'] as int? ?? 1,
      content: ClientReportContent.fromJson(
        Map<String, dynamic>.from(json['content'] as Map? ?? {}),
      ),
      createdAt: DateTime.tryParse(json['created_at'] as String? ?? '')?.toUtc() ??
          DateTime.now().toUtc(),
    );
  }

  Map<String, dynamic> toJson() => {
    'report_id': reportId,
    'cycle_id': cycleId,
    if (sprintId != null) 'sprint_id': sprintId,
    if (sessionId != null) 'session_id': sessionId,
    'model': model,
    'report_type': reportType,
    'privacy_level': privacyLevel,
    'version': version,
    'content': content.toJson(),
    'created_at': createdAt.toUtc().toIso8601String(),
  };
}

/// Result returned from Primary Consilium execution or recovery.
/// Matches backend PrimaryConsiliumClientResponse schema.
@immutable
class PrimaryConsiliumClientResponse {
  final String cycleId;
  final String reportDate;
  final ClientModelReportResponse overallReport;
  final Map<String, ClientModelReportResponse> systemReports;
  final ClientVitalsThresholds vitalsThresholds;

  const PrimaryConsiliumClientResponse({
    required this.cycleId,
    required this.reportDate,
    required this.overallReport,
    this.systemReports = const {},
    required this.vitalsThresholds,
  });

  factory PrimaryConsiliumClientResponse.fromJson(Map<String, dynamic> json) {
    final sysReports = <String, ClientModelReportResponse>{};
    if (json['system_reports'] is Map) {
      (json['system_reports'] as Map).forEach((k, v) {
        if (v is Map) {
          sysReports[k.toString()] =
              ClientModelReportResponse.fromJson(Map<String, dynamic>.from(v));
        }
      });
    }

    return PrimaryConsiliumClientResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      reportDate: json['report_date'] as String? ?? '',
      overallReport: ClientModelReportResponse.fromJson(
        Map<String, dynamic>.from(json['overall_report'] as Map? ?? {}),
      ),
      systemReports: sysReports,
      vitalsThresholds: ClientVitalsThresholds.fromJson(
        Map<String, dynamic>.from(json['vitals_thresholds'] as Map? ?? {}),
      ),
    );
  }

  Map<String, dynamic> toJson() => {
    'cycle_id': cycleId,
    'report_date': reportDate,
    'overall_report': overallReport.toJson(),
    'system_reports': systemReports.map((k, v) => MapEntry(k, v.toJson())),
    'vitals_thresholds': vitalsThresholds.toJson(),
  };
}

/// Pulse spike context for Athena curator Mode 2 intervention.
/// Matches backend AthenaVitalsAlertContext schema.
@immutable
class AthenaVitalsAlertContext {
  final int currentPulse;
  final int baselinePulse;
  final String? measuredAt;
  final String? notes;

  const AthenaVitalsAlertContext({
    required this.currentPulse,
    required this.baselinePulse,
    this.measuredAt,
    this.notes,
  });

  factory AthenaVitalsAlertContext.fromJson(Map<String, dynamic> json) {
    return AthenaVitalsAlertContext(
      currentPulse: json['current_pulse'] as int? ?? 0,
      baselinePulse: json['baseline_pulse'] as int? ?? 0,
      measuredAt: json['measured_at'] as String?,
      notes: json['notes'] as String?,
    );
  }

  Map<String, dynamic> toJson() => {
    'current_pulse': currentPulse,
    'baseline_pulse': baselinePulse,
    if (measuredAt != null) 'measured_at': measuredAt,
    if (notes != null) 'notes': notes,
  };
}

/// Request payload for Athena conversational turn.
/// Matches backend AthenaChatRequest schema (forbids foreign patient/cycle overrides).
/// Strictly requires a stable, non-empty [idempotencyKey] for every logical turn to ensure
/// deterministic replay protection across network retries, timeouts, and auth refreshes.
@immutable
class AthenaChatRequest {
  final String? message;
  final String eventType;
  final AthenaVitalsAlertContext? vitalsAlert;
  final int? sprintDay;
  final String idempotencyKey;

  const AthenaChatRequest({
    this.message,
    this.eventType = 'user_message',
    this.vitalsAlert,
    this.sprintDay,
    required this.idempotencyKey,
  }) : assert(idempotencyKey.length > 0, 'idempotencyKey must be non-empty');

  /// Creates a new logical chat turn with an explicitly minted or supplied [idempotencyKey].
  /// If omitted, generates a cryptographically random RFC 4122 compliant UUID v4 string.
  factory AthenaChatRequest.createTurn({
    String? message,
    String eventType = 'user_message',
    AthenaVitalsAlertContext? vitalsAlert,
    int? sprintDay,
    String? idempotencyKey,
  }) {
    return AthenaChatRequest(
      message: message,
      eventType: eventType,
      vitalsAlert: vitalsAlert,
      sprintDay: sprintDay,
      idempotencyKey: idempotencyKey ?? generateUuidV4(),
    );
  }

  static final Random _secureRandom = Random.secure();

  /// Generates a cryptographically random RFC 4122 compliant UUID v4 string (36 chars).
  /// Contains zero patient identifiers, zero medical context, and does not depend on system time.
  static String generateUuidV4() {
    final bytes = List<int>.generate(16, (_) => _secureRandom.nextInt(256));
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // Version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // Variant 10xx
    final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).toList();
    return '${hex[0]}${hex[1]}${hex[2]}${hex[3]}-'
        '${hex[4]}${hex[5]}-'
        '${hex[6]}${hex[7]}-'
        '${hex[8]}${hex[9]}-'
        '${hex[10]}${hex[11]}${hex[12]}${hex[13]}${hex[14]}${hex[15]}';
  }

  /// Validates whether [value] is a valid RFC 4122 UUID v4 string.
  static bool isValidUuidV4(String? value) {
    if (value == null) return false;
    final uuidRegex = RegExp(
      r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
      caseSensitive: false,
    );
    return uuidRegex.hasMatch(value.trim());
  }

  AthenaChatRequest copyWith({
    String? message,
    String? eventType,
    AthenaVitalsAlertContext? vitalsAlert,
    int? sprintDay,
    String? idempotencyKey,
  }) {
    return AthenaChatRequest(
      message: message ?? this.message,
      eventType: eventType ?? this.eventType,
      vitalsAlert: vitalsAlert ?? this.vitalsAlert,
      sprintDay: sprintDay ?? this.sprintDay,
      idempotencyKey: idempotencyKey ?? this.idempotencyKey,
    );
  }

  factory AthenaChatRequest.fromJson(Map<String, dynamic> json) {
    final rawKey = json['idempotency_key'];
    if (rawKey == null || rawKey is! String || rawKey.trim().isEmpty) {
      throw const FormatException(
        'Missing or invalid required "idempotency_key" in AthenaChatRequest payload.',
      );
    }
    return AthenaChatRequest(
      message: json['message'] as String?,
      eventType: json['event_type'] as String? ?? 'user_message',
      vitalsAlert: json['vitals_alert'] != null
          ? AthenaVitalsAlertContext.fromJson(
              Map<String, dynamic>.from(json['vitals_alert'] as Map),
            )
          : null,
      sprintDay: json['sprint_day'] as int?,
      idempotencyKey: rawKey.trim(),
    );
  }

  Map<String, dynamic> toJson() {
    final map = <String, dynamic>{
      'event_type': eventType,
      'idempotency_key': idempotencyKey,
    };
    if (message != null) map['message'] = message;
    if (vitalsAlert != null) map['vitals_alert'] = vitalsAlert!.toJson();
    if (sprintDay != null) map['sprint_day'] = sprintDay;
    return map;
  }
}

/// Sanitized response from Athena conversational turn.
/// Matches backend AthenaChatResponse schema.
@immutable
class AthenaChatResponse {
  final String reply;
  final bool emergencyTrigger;
  final String? emergencyNotice;
  final String cycleId;
  final int? sprintDay;
  final String messageId;
  final String createdAt;

  const AthenaChatResponse({
    required this.reply,
    this.emergencyTrigger = false,
    this.emergencyNotice,
    required this.cycleId,
    this.sprintDay,
    required this.messageId,
    required this.createdAt,
  });

  factory AthenaChatResponse.fromJson(Map<String, dynamic> json) {
    return AthenaChatResponse(
      reply: json['reply'] as String? ?? '',
      emergencyTrigger: json['emergency_trigger'] == true,
      emergencyNotice: json['emergency_notice'] as String?,
      cycleId: json['cycle_id'] as String? ?? '',
      sprintDay: json['sprint_day'] as int?,
      messageId: json['message_id'] as String? ?? '',
      createdAt: json['created_at'] as String? ?? '',
    );
  }

  Map<String, dynamic> toJson() => {
    'reply': reply,
    'emergency_trigger': emergencyTrigger,
    if (emergencyNotice != null) 'emergency_notice': emergencyNotice,
    'cycle_id': cycleId,
    if (sprintDay != null) 'sprint_day': sprintDay,
    'message_id': messageId,
    'created_at': createdAt,
  };
}

/// Single sanitized dialogue history message.
/// Matches backend AthenaChatMessageItem schema.
@immutable
class AthenaChatMessageItem {
  final String sender; // 'user' or 'curator'
  final String text;
  final String timestamp;

  const AthenaChatMessageItem({
    required this.sender,
    required this.text,
    required this.timestamp,
  });

  factory AthenaChatMessageItem.fromJson(Map<String, dynamic> json) {
    return AthenaChatMessageItem(
      sender: json['sender'] as String? ?? '',
      text: json['text'] as String? ?? '',
      timestamp: json['timestamp'] as String? ?? '',
    );
  }

  Map<String, dynamic> toJson() => {
    'sender': sender,
    'text': text,
    'timestamp': timestamp,
  };
}

/// Dialogue history of active cycle.
/// Matches backend AthenaChatHistoryResponse schema.
@immutable
class AthenaChatHistoryResponse {
  final String cycleId;
  final List<AthenaChatMessageItem> messages;

  const AthenaChatHistoryResponse({
    required this.cycleId,
    this.messages = const [],
  });

  factory AthenaChatHistoryResponse.fromJson(Map<String, dynamic> json) {
    final list = <AthenaChatMessageItem>[];
    if (json['messages'] is List) {
      for (final item in json['messages'] as List) {
        if (item is Map) {
          list.add(
            AthenaChatMessageItem.fromJson(Map<String, dynamic>.from(item)),
          );
        }
      }
    }
    return AthenaChatHistoryResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      messages: list,
    );
  }

  Map<String, dynamic> toJson() => {
    'cycle_id': cycleId,
    'messages': messages.map((m) => m.toJson()).toList(),
  };
}

/// Represents a laboratory document page/file prepared for upload.
@immutable
class LabUploadFileItem {
  final String filename;
  final List<int> bytes;
  final String mimeType;

  LabUploadFileItem({
    required this.filename,
    required this.bytes,
    String? mimeType,
  }) : mimeType = mimeType ?? resolveMimeType(filename, bytes);

  static String resolveMimeType(String filename, List<int> bytes) {
    if (bytes.length >= 3 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[2] == 0xFF) {
      return 'image/jpeg';
    }
    if (bytes.length >= 8 &&
        bytes[0] == 0x89 && bytes[1] == 0x50 && bytes[2] == 0x4E && bytes[3] == 0x47 &&
        bytes[4] == 0x0D && bytes[5] == 0x0A && bytes[6] == 0x1A && bytes[7] == 0x0A) {
      return 'image/png';
    }
    if (bytes.length >= 12 &&
        bytes[0] == 0x52 && bytes[1] == 0x49 && bytes[2] == 0x46 && bytes[3] == 0x46 &&
        bytes[8] == 0x57 && bytes[9] == 0x45 && bytes[10] == 0x42 && bytes[11] == 0x50) {
      return 'image/webp';
    }
    if (bytes.length >= 8 &&
        bytes[4] == 0x66 && bytes[5] == 0x74 && bytes[6] == 0x79 && bytes[7] == 0x70) {
      return 'image/heic';
    }
    final lower = filename.toLowerCase();
    if (lower.endsWith('.png')) return 'image/png';
    if (lower.endsWith('.webp')) return 'image/webp';
    if (lower.endsWith('.heic') || lower.endsWith('.heif')) return 'image/heic';
    return 'image/jpeg';
  }

  static bool isSupportedFormat(String filename, List<int> bytes) {
    final lower = filename.toLowerCase();
    final hasValidExt = lower.endsWith('.jpg') ||
        lower.endsWith('.jpeg') ||
        lower.endsWith('.png') ||
        lower.endsWith('.webp') ||
        lower.endsWith('.heic') ||
        lower.endsWith('.heif');
    if (!hasValidExt) return false;
    if (bytes.isEmpty) return false;
    final mime = resolveMimeType(filename, bytes);
    return mime == 'image/jpeg' ||
        mime == 'image/png' ||
        mime == 'image/webp' ||
        mime == 'image/heic';
  }

  static Future<LabUploadFileItem> fromXFile(XFile file) async {
    final fileBytes = await file.readAsBytes();
    return LabUploadFileItem(
      filename: file.name.isNotEmpty ? file.name : (file.path.isNotEmpty ? file.path.split(RegExp(r'[\\/]')).last : 'page.jpg'),
      bytes: fileBytes,
    );
  }
}

/// Metadata describing the recognized laboratory document.
@immutable
class VisionOcrDocumentMetadata {
  final String? sampleDate;
  final String? issueDate;
  final String? laboratoryName;
  final String? orderId;
  final String? documentFingerprint;
  final int pageCount;
  final String? notes;

  const VisionOcrDocumentMetadata({
    this.sampleDate,
    this.issueDate,
    this.laboratoryName,
    this.orderId,
    this.documentFingerprint,
    this.pageCount = 1,
    this.notes,
  });

  factory VisionOcrDocumentMetadata.fromJson(Map<String, dynamic> json) {
    return VisionOcrDocumentMetadata(
      sampleDate: json['sample_date'] as String?,
      issueDate: json['issue_date'] as String?,
      laboratoryName: json['laboratory_name'] as String?,
      orderId: json['order_id'] as String?,
      documentFingerprint: json['document_fingerprint'] as String?,
      pageCount: json['page_count'] as int? ?? 1,
      notes: json['notes'] as String?,
    );
  }

  Map<String, dynamic> toJson() => {
    if (sampleDate != null) 'sample_date': sampleDate,
    if (issueDate != null) 'issue_date': issueDate,
    if (laboratoryName != null) 'laboratory_name': laboratoryName,
    if (orderId != null) 'order_id': orderId,
    if (documentFingerprint != null) 'document_fingerprint': documentFingerprint,
    'page_count': pageCount,
    if (notes != null) 'notes': notes,
  };
}

/// Single recognized laboratory marker from Vision OCR.
@immutable
class VisionOcrMarkerItem {
  final String originalName;
  final String markerKey;
  final String rawValue;
  final double? numericValue;
  final String? unit;
  final LabReferenceRange? referenceRange;
  final String? sampleDate;
  final String? category;
  final bool isUncertain;
  final String? uncertaintyNote;
  final bool isCriticalFlag;
  final bool requiresUserVerification;
  final bool isRecognitionAmbiguous;

  const VisionOcrMarkerItem({
    required this.originalName,
    required this.markerKey,
    required this.rawValue,
    this.numericValue,
    this.unit,
    this.referenceRange,
    this.sampleDate,
    this.category,
    this.isUncertain = false,
    this.uncertaintyNote,
    this.isCriticalFlag = false,
    this.requiresUserVerification = false,
    this.isRecognitionAmbiguous = false,
  });

  factory VisionOcrMarkerItem.fromJson(Map<String, dynamic> json) {
    return VisionOcrMarkerItem(
      originalName: json['original_name'] as String? ?? '',
      markerKey: json['marker_key'] as String? ?? '',
      rawValue: json['raw_value']?.toString() ?? '',
      numericValue: (json['numeric_value'] as num?)?.toDouble(),
      unit: json['unit'] as String?,
      referenceRange: json['reference_range'] != null
          ? LabReferenceRange.fromJson(
              Map<String, dynamic>.from(json['reference_range'] as Map),
            )
          : null,
      sampleDate: json['sample_date'] as String?,
      category: json['category'] as String?,
      isUncertain: json['is_uncertain'] == true,
      uncertaintyNote: json['uncertainty_note'] as String?,
      isCriticalFlag: json['is_critical_flag'] == true,
      requiresUserVerification: json['requires_user_verification'] == true,
      isRecognitionAmbiguous: json['is_recognition_ambiguous'] == true,
    );
  }

  Map<String, dynamic> toJson() => {
    'original_name': originalName,
    'marker_key': markerKey,
    'raw_value': rawValue,
    if (numericValue != null) 'numeric_value': numericValue,
    if (unit != null) 'unit': unit,
    if (referenceRange != null) 'reference_range': referenceRange!.toJson(),
    if (sampleDate != null) 'sample_date': sampleDate,
    if (category != null) 'category': category,
    'is_uncertain': isUncertain,
    if (uncertaintyNote != null) 'uncertainty_note': uncertaintyNote,
    'is_critical_flag': isCriticalFlag,
    'requires_user_verification': requiresUserVerification,
    'is_recognition_ambiguous': isRecognitionAmbiguous,
  };
}

/// Safe client confirmation response after Vision OCR processing and ledger synchronization.
@immutable
class LabVisionIntakeClientResponse {
  final String cycleId;
  final String status;
  final int ledgerVersion;
  final int extractedCount;
  final int requiresVerificationCount;
  final VisionOcrDocumentMetadata? documentMetadata;
  final List<VisionOcrMarkerItem> markers;
  final List<VisionOcrMarkerItem> confirmedMarkers;
  final List<VisionOcrMarkerItem> unconfirmedMarkers;
  final bool hasUnreviewedLabChanges;
  final String? privacyNotice;

  const LabVisionIntakeClientResponse({
    required this.cycleId,
    this.status = 'processed',
    required this.ledgerVersion,
    required this.extractedCount,
    this.requiresVerificationCount = 0,
    this.documentMetadata,
    this.markers = const [],
    this.confirmedMarkers = const [],
    this.unconfirmedMarkers = const [],
    this.hasUnreviewedLabChanges = true,
    this.privacyNotice,
  });

  factory LabVisionIntakeClientResponse.fromJson(Map<String, dynamic> json) {
    final markersList = <VisionOcrMarkerItem>[];
    if (json['markers'] is List) {
      for (final item in json['markers'] as List) {
        if (item is Map) {
          markersList.add(VisionOcrMarkerItem.fromJson(Map<String, dynamic>.from(item)));
        }
      }
    }

    final confirmedList = <VisionOcrMarkerItem>[];
    if (json['confirmed_markers'] is List) {
      for (final item in json['confirmed_markers'] as List) {
        if (item is Map) {
          confirmedList.add(VisionOcrMarkerItem.fromJson(Map<String, dynamic>.from(item)));
        }
      }
    }

    final unconfirmedList = <VisionOcrMarkerItem>[];
    if (json['unconfirmed_markers'] is List) {
      for (final item in json['unconfirmed_markers'] as List) {
        if (item is Map) {
          unconfirmedList.add(VisionOcrMarkerItem.fromJson(Map<String, dynamic>.from(item)));
        }
      }
    }

    return LabVisionIntakeClientResponse(
      cycleId: json['cycle_id'] as String? ?? '',
      status: json['status'] as String? ?? 'processed',
      ledgerVersion: json['ledger_version'] as int? ?? 0,
      extractedCount: json['extracted_count'] as int? ?? markersList.length,
      requiresVerificationCount: json['requires_verification_count'] as int? ?? unconfirmedList.length,
      documentMetadata: json['document_metadata'] != null
          ? VisionOcrDocumentMetadata.fromJson(Map<String, dynamic>.from(json['document_metadata'] as Map))
          : null,
      markers: markersList,
      confirmedMarkers: confirmedList,
      unconfirmedMarkers: unconfirmedList,
      hasUnreviewedLabChanges: json['has_unreviewed_lab_changes'] != false,
      privacyNotice: json['privacy_notice'] as String?,
    );
  }

  Map<String, dynamic> toJson() => {
    'cycle_id': cycleId,
    'status': status,
    'ledger_version': ledgerVersion,
    'extracted_count': extractedCount,
    'requires_verification_count': requiresVerificationCount,
    if (documentMetadata != null) 'document_metadata': documentMetadata!.toJson(),
    'markers': markers.map((m) => m.toJson()).toList(),
    'confirmed_markers': confirmedMarkers.map((m) => m.toJson()).toList(),
    'unconfirmed_markers': unconfirmedMarkers.map((m) => m.toJson()).toList(),
    'has_unreviewed_lab_changes': hasUnreviewedLabChanges,
    if (privacyNotice != null) 'privacy_notice': privacyNotice,
  };
}
