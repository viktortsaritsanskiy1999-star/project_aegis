import 'dart:async';
import 'package:flutter/foundation.dart';
import 'vault_service.dart';
import 'vitals_monitor_service.dart';

/// Explicit sources for all incoming biometric health data
enum HealthDataSource {
  watchSensor('watch_sensor'),
  manualInput('manual_input'),
  testChip('test_chip'),
  simulated('simulated');

  final String rawValue;
  const HealthDataSource(this.rawValue);

  static HealthDataSource fromString(String? value) {
    switch (value) {
      case 'manual_input':
        return HealthDataSource.manualInput;
      case 'test_chip':
        return HealthDataSource.testChip;
      case 'simulated':
        return HealthDataSource.simulated;
      case 'watch_sensor':
      default:
        return HealthDataSource.watchSensor;
    }
  }
}

/// Standardized model representing a single or multi-metric biometric measurement
class HealthMeasurement {
  final DateTime timestamp;
  final int? pulse;
  final int? systolic;
  final int? diastolic;
  final int? spo2;
  final String? ecgStatus;
  final HealthDataSource source;
  final bool isSimulated;
  final String note;
  final Map<String, dynamic> metadata;

  const HealthMeasurement({
    required this.timestamp,
    this.pulse,
    this.systolic,
    this.diastolic,
    this.spo2,
    this.ecgStatus,
    required this.source,
    this.isSimulated = false,
    this.note = '',
    this.metadata = const {},
  });

  Map<String, dynamic> toJson() => {
    'timestamp': timestamp.toIso8601String(),
    if (pulse != null) 'pulse': pulse,
    if (systolic != null) 'systolic': systolic,
    if (diastolic != null) 'diastolic': diastolic,
    if (spo2 != null) 'spo2': spo2,
    if (ecgStatus != null) 'ecg_status': ecgStatus,
    'source': source.rawValue,
    'is_simulated': isSimulated,
    'note': note,
    if (metadata.isNotEmpty) 'metadata': metadata,
  };

  factory HealthMeasurement.fromJson(Map<String, dynamic> json) => HealthMeasurement(
    timestamp: DateTime.tryParse(json['timestamp']?.toString() ?? '') ?? DateTime.now(),
    pulse: (json['pulse'] as num?)?.toInt(),
    systolic: (json['systolic'] as num?)?.toInt(),
    diastolic: (json['diastolic'] as num?)?.toInt(),
    spo2: (json['spo2'] as num?)?.toInt(),
    ecgStatus: json['ecg_status']?.toString(),
    source: HealthDataSource.fromString(json['source']?.toString()),
    isSimulated: json['is_simulated'] == true,
    note: json['note']?.toString() ?? '',
    metadata: Map<String, dynamic>.from(json['metadata'] as Map? ?? {}),
  );
}

/// Universal HealthDataProvider
/// Coordinates ingestion of biometric data from smartwatch sensors, user manual entry,
/// UI test chips, and simulation, without duplicating the persistent SmartWatch Vault storage.
class HealthDataProvider extends ChangeNotifier {
  VaultService _vaultService;
  VitalsMonitorService _vitalsMonitorService;

  final StreamController<HealthMeasurement> _vitalsStreamController =
      StreamController<HealthMeasurement>.broadcast();
  StreamSubscription<HealthMeasurement>? _activeSensorSubscription;

  HealthMeasurement? _latestMeasurement;
  HealthMeasurement? get latestMeasurement => _latestMeasurement;

  /// Asynchronous broadcast stream for future smartwatch sensors and UI listeners
  Stream<HealthMeasurement> get vitalsStream => _vitalsStreamController.stream;

  HealthDataProvider({
    required VaultService vaultService,
    required VitalsMonitorService vitalsMonitorService,
  })  : _vaultService = vaultService,
        _vitalsMonitorService = vitalsMonitorService;

  /// Updates service references on Provider rebuild
  void updateDependencies({
    required VaultService vaultService,
    required VitalsMonitorService vitalsMonitorService,
  }) {
    _vaultService = vaultService;
    _vitalsMonitorService = vitalsMonitorService;
  }

  /// Core ingestion pipeline for all incoming biometric measurements
  Future<void> ingestMeasurement(HealthMeasurement measurement) async {
    _latestMeasurement = measurement;

    // 1. Broadcast to any active async stream subscribers
    if (!_vitalsStreamController.isClosed) {
      _vitalsStreamController.add(measurement);
    }

    // 2. Route pulse into VitalsMonitorService (for threshold analysis, workout tracking, anomaly detection)
    if (measurement.pulse != null) {
      _vitalsMonitorService.onNewPulseRead(
        measurement.pulse!,
        source: measurement.source.rawValue,
        isSimulated: measurement.isSimulated,
      );
    }

    // 3. Update top-level smart watch metrics in VaultService if pressure, SpO2, or ECG are present
    if (measurement.systolic != null ||
        measurement.diastolic != null ||
        measurement.pulse != null ||
        measurement.spo2 != null ||
        measurement.ecgStatus != null) {
      _vaultService.updateSmartWatchMetrics(
        systolic: measurement.systolic,
        diastolic: measurement.diastolic,
        pulse: measurement.pulse,
        spo2: measurement.spo2,
        ecgSummary: measurement.ecgStatus,
      );
    }

    notifyListeners();
  }

  /// Ingest a batch of measurements asynchronously (e.g. historical sync from watch)
  Future<void> ingestBatch(List<HealthMeasurement> batch) async {
    for (final measurement in batch) {
      await ingestMeasurement(measurement);
    }
  }

  /// Bind an external asynchronous stream of measurements (e.g. future smartwatch Privileged SDK or mock stream)
  void bindSensorStream(Stream<HealthMeasurement> stream) {
    _activeSensorSubscription?.cancel();
    _activeSensorSubscription = stream.listen(
      (measurement) => ingestMeasurement(measurement),
      onError: (error) {
        debugPrint("⚠️ [HealthDataProvider]: Stream error encountered: $error");
      },
    );
  }

  /// Cancel any active sensor stream subscription
  void unbindSensorStream() {
    _activeSensorSubscription?.cancel();
    _activeSensorSubscription = null;
  }

  /// Convenience dispatch for manual user inputs (e.g. manual vitals dialogs, forms)
  /// Explicitly marks source as HealthDataSource.manualInput, never defaulting to watch_sensor!
  Future<void> dispatchManualInput({
    int? pulse,
    int? systolic,
    int? diastolic,
    int? spo2,
    String? ecgStatus,
    DateTime? timestamp,
    String note = '',
    bool isSimulated = false,
  }) async {
    final measurement = HealthMeasurement(
      timestamp: timestamp ?? DateTime.now(),
      pulse: pulse,
      systolic: systolic,
      diastolic: diastolic,
      spo2: spo2,
      ecgStatus: ecgStatus,
      source: HealthDataSource.manualInput,
      isSimulated: isSimulated,
      note: note,
    );
    await ingestMeasurement(measurement);
  }

  /// Convenience dispatch for quick test chips on UI
  Future<void> dispatchTestChip({
    required int pulse,
    DateTime? timestamp,
    String note = '',
  }) async {
    final measurement = HealthMeasurement(
      timestamp: timestamp ?? DateTime.now(),
      pulse: pulse,
      source: HealthDataSource.testChip,
      isSimulated: true,
      note: note,
    );
    await ingestMeasurement(measurement);
  }

  /// Convenience dispatch for simulated / virtual time measurements
  Future<void> dispatchSimulated({
    required int pulse,
    DateTime? timestamp,
    String note = '',
  }) async {
    final measurement = HealthMeasurement(
      timestamp: timestamp ?? DateTime.now(),
      pulse: pulse,
      source: HealthDataSource.simulated,
      isSimulated: true,
      note: note,
    );
    await ingestMeasurement(measurement);
  }

  /// Convenience dispatch for real smartwatch sensor readings
  Future<void> dispatchWatchSensor({
    required int pulse,
    DateTime? timestamp,
    int? systolic,
    int? diastolic,
    int? spo2,
    String? ecgStatus,
    String note = '',
    Map<String, dynamic> metadata = const {},
  }) async {
    final measurement = HealthMeasurement(
      timestamp: timestamp ?? DateTime.now(),
      pulse: pulse,
      systolic: systolic,
      diastolic: diastolic,
      spo2: spo2,
      ecgStatus: ecgStatus,
      source: HealthDataSource.watchSensor,
      isSimulated: false,
      note: note,
      metadata: metadata,
    );
    await ingestMeasurement(measurement);
  }

  @override
  void dispose() {
    _activeSensorSubscription?.cancel();
    _vitalsStreamController.close();
    super.dispose();
  }
}
