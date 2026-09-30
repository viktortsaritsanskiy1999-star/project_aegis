/// Vital signs thresholds calculated by the Aegis Chief Doctor
class VitalsThresholds {
  final int minPessimumBpm; // Below this — abnormal bradycardia
  final int maxPessimumBpm; // Above this — abnormal tachycardia
  final int maxSpikeDeltaBpm; // Maximum sharp spike delta from baseline level
  final String clinicalRationale; // Clinical rationale from the Chief Doctor

  const VitalsThresholds({
    required this.minPessimumBpm,
    required this.maxPessimumBpm,
    required this.maxSpikeDeltaBpm,
    required this.clinicalRationale,
  });

  /// Parsing JSON response STRICTLY from the Chief Doctor
  factory VitalsThresholds.fromJson(Map<String, dynamic> json) {
    return VitalsThresholds(
      minPessimumBpm: (json['min_pessimum_bpm'] as num).toInt(),
      maxPessimumBpm: (json['max_pessimum_bpm'] as num).toInt(),
      maxSpikeDeltaBpm: (json['max_spike_delta_bpm'] as num).toInt(),
      clinicalRationale: json['clinical_rationale'] as String? ?? "Aegis Chief Doctor thresholds",
    );
  }

  Map<String, dynamic> toJson() => {
    'min_pessimum_bpm': minPessimumBpm,
    'max_pessimum_bpm': maxPessimumBpm,
    'max_spike_delta_bpm': maxSpikeDeltaBpm,
    'clinical_rationale': clinicalRationale,
  };

  /// Check method: whether the pulse is an anomaly/spike
  bool isSpikeDetected({required int currentPulse, required int baselinePulse}) {
    // 1. Exceeding absolute pessimum boundaries (too low or too high)
    if (currentPulse <= minPessimumBpm || currentPulse >= maxPessimumBpm) {
      return true;
    }

    // 2. Sharp delta from the baseline normal level
    final delta = (currentPulse - baselinePulse).abs();
    if (delta >= maxSpikeDeltaBpm) {
      return true;
    }

    return false;
  }
}

/// Context model for recording an abnormal pulse spike
class VitalsAlertContext {
  final DateTime timestamp;
  final int baselinePulse; // Baseline pulse before the spike
  final int currentPulse; // Peak pulse upon detection
  final List<int> pulseHistoryLast5Min; // History for the last 5 minutes

  // Personal thresholds from the Chief Doctor that triggered the detection
  final VitalsThresholds thresholds;

  // Primary user status
  bool? isPhysicalActivity; // true - active, false - no
  String? userStatusDescription; // "All good", "Aura/strange sensations", "Malaise"

  // Clarifications from Athena (if transitioned to chat)
  String? painLocation; // "Chest pressure", "Head", "No pain", etc.
  bool? hadBefore; // Whether something similar happened before

  VitalsAlertContext({
    required this.timestamp,
    required this.baselinePulse,
    required this.currentPulse,
    required this.pulseHistoryLast5Min,
    required this.thresholds,
    this.isPhysicalActivity,
    this.userStatusDescription,
    this.painLocation,
    this.hadBefore,
  });

  /// Serialization to JSON for transmission to Athena's prompt or backend
  Map<String, dynamic> toJson() => {
    "event_type": "PULSE_SPIKE_DETECTED",
    "timestamp": timestamp.toIso8601String(),
    "vitals": {
      "baseline_bpm": baselinePulse,
      "peak_bpm": currentPulse,
      "trend_5min": pulseHistoryLast5Min,
    },
    "active_thresholds": thresholds.toJson(),
    "user_feedback": {
      "is_physical_activity": isPhysicalActivity,
      "status_description": userStatusDescription,
      "pain_location": painLocation,
      "had_before": hadBefore,
    }
  };

  /// Creating a copy with updated fields at each survey step
  VitalsAlertContext copyWith({
    DateTime? timestamp,
    int? baselinePulse,
    int? currentPulse,
    List<int>? pulseHistoryLast5Min,
    VitalsThresholds? thresholds,
    bool? isPhysicalActivity,
    String? userStatusDescription,
    String? painLocation,
    bool? hadBefore,
  }) {
    return VitalsAlertContext(
      timestamp: timestamp ?? this.timestamp,
      baselinePulse: baselinePulse ?? this.baselinePulse,
      currentPulse: currentPulse ?? this.currentPulse,
      pulseHistoryLast5Min:
      pulseHistoryLast5Min ?? List.from(this.pulseHistoryLast5Min),
      thresholds: thresholds ?? this.thresholds,
      isPhysicalActivity: isPhysicalActivity ?? this.isPhysicalActivity,
      userStatusDescription:
      userStatusDescription ?? this.userStatusDescription,
      painLocation: painLocation ?? this.painLocation,
      hadBefore: hadBefore ?? this.hadBefore,
    );
  }
}