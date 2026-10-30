import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'api/aegis_api_client.dart';
import 'api/medical_ai_api_models.dart';
import 'api/vitals_api_models.dart';
import 'auth/auth_models.dart';
import 'auth/auth_session_manager.dart';
import 'vault_storage_service.dart';
import 'vitals_alert_context.dart';

/// Synchronization status of raw anamnesis questionnaire with Aegis Server M1.
enum AnamnesisSyncStatus {
  idle,
  draftSaved,
  syncing,
  synced,
  offline,
  unauthorized,
  insufficientScopes,
  error,
}

/// Result of an anamnesis synchronization attempt with server M1.
class AnamnesisSyncResult {
  final bool isSuccess;
  final AnamnesisSyncStatus status;
  final AnamnesisIntakeClientResponse? response;
  final String? errorMessage;

  const AnamnesisSyncResult._({
    required this.isSuccess,
    required this.status,
    this.response,
    this.errorMessage,
  });

  factory AnamnesisSyncResult.success(AnamnesisIntakeClientResponse response) =>
      AnamnesisSyncResult._(
        isSuccess: true,
        status: AnamnesisSyncStatus.synced,
        response: response,
      );

  factory AnamnesisSyncResult.failure(AnamnesisSyncStatus status, String errorMessage) =>
      AnamnesisSyncResult._(
        isSuccess: false,
        status: status,
        errorMessage: errorMessage,
      );
}

class VaultService extends ChangeNotifier {
  // Your data for Atlas
  final Map<String, dynamic> _data = {
    "health_systems": {
      "cvs": {
        "name": "Cardiovascular",
        "completeness": 0.0,
        "color": "0xFFFF0000",
        "markers": {
          "smart_watch": {
            "workout_sessions": <Map<String, dynamic>>[],
            "heart_rate_samples": <Map<String, dynamic>>[],
            "workout_context": <String, dynamic>{},
            "workout_analysis_reports": <Map<String, dynamic>>[],
          }
        }
      },
      "gi_tract": {"name": "GI Tract", "completeness": 0.0, "color": "0xFFFFA500", "markers": {}},
      "respiratory": {"name": "Respiratory", "completeness": 0.0, "color": "0xFF00FFFF", "markers": {}},
      "nervous": {"name": "Nervous", "completeness": 0.0, "color": "0xFF8A2BE2", "markers": {}},
      "musculoskeletal": {"name": "Musculoskeletal", "completeness": 0.0, "color": "0xFFB0C4DE", "markers": {}},
      "kidneys": {"name": "Renal", "completeness": 0.0, "color": "0xFF0000FF", "markers": {}},
      "immune": {"name": "Immune and Blood", "completeness": 0.0, "color": "0xFF32CD32", "markers": {}},
      "endocrine": {"name": "Endocrine", "completeness": 0.0, "color": "0xFFEE82EE", "markers": {}},
    }
  };

  // Flag for the red badge "1" and Athena notifications
  bool _hasUnreadAthenaMessage = false;
  bool get hasUnreadAthenaMessage => _hasUnreadAthenaMessage;

  // Storage for Curator chat history
  final List<Map<String, String>> _curatorChatHistory = [];

  // System folders + Training sandbox + New shelves according to cascade plan + DEDICATED FOLDER FOR NOTES
  final Map<String, Map<String, dynamic>> _systemFolders = {
    'respiratory': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'immune': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'cvs': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'gi_tract': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'kidneys': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'endocrine': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'nervous': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'musculoskeletal': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },

    // COMMON FOLDER FOR MAIN MODELS (4 new shelves at the top of the interface)
    'core_cascade': {
      'flash_general_shelf': 'Waiting for Gemini Flash output...',
      'flash_training_shelf': 'Gemini Flash deficiency logs...',
      'pro_general_shelf': 'Waiting for Gemini Pro output...',
      'pro_training_shelf': 'Gemini Pro deficiency logs...',
    },

    // DEDICATED FOLDER FOR NOTES MIRRORING
    'notes_export': <String, dynamic>{},

    'global': {'labs': <String, dynamic>{}},
    'training_sandbox': {
      'raw_predictions': [],
      'missing_data': [],
      'risk_analysis': "PENDING...",
    },
  };

  String? _selectedSystem;

  final IVaultStorage _storage;
  AuthSessionManager? _authSessionManager;
  String? _activePatientId;
  int _sessionEpoch = 0;
  LegacyVaultStatus _legacyVaultStatus = LegacyVaultStatus.none;
  bool _isInitialized = false;
  bool _isDisposed = false;

  /// Monotonic write sequence counter across instances.
  static int _writeSequence = 0;

  /// Chains of pending/active writes per storage key to guarantee sequential execution across instances.
  static final Map<String, Future<void>> _writeChains = {};

  /// Tracks highest committed write sequence number per storage key to prevent stale overwrites across instances.
  static final Map<String, int> _lastCommittedSeqByKey = {};

  /// Map of errors by storage key to ensure independent error accounting across keys.
  static final Map<String, Object> _persistenceErrorsByKey = {};

  Future<void>? _initFuture;
  Future<void>? get initFuture => _initFuture;

  String? get activePatientId => _activePatientId;
  bool get isAuthenticated => _activePatientId != null;
  int get sessionEpoch => _sessionEpoch;

  // Client-facing Primary Consilium reports
  final Map<String, ClientModelReportResponse> _clientSystemReports = {};
  ClientModelReportResponse? _clientOverallReport;
  bool _isConsiliumCompleted = false;
  String? _consiliumCycleId;
  final Map<String, Map<String, dynamic>> _consiliumHistory = {};

  // Client-facing Weekly Consilium reports
  String? _activeWeeklySprintId;
  int? _activeWeeklySprintNumber;
  String? _activeWeeklyReportDate;
  ClientModelReportResponse? _activeWeeklyOverallReport;
  final Map<String, ClientModelReportResponse> _activeWeeklySystemReports = {};
  final Map<String, Map<String, dynamic>> _weeklyConsiliumHistory = {};

  String? get activeWeeklySprintId => _activeWeeklySprintId;
  int? get activeWeeklySprintNumber => _activeWeeklySprintNumber;
  String? get activeWeeklyReportDate => _activeWeeklyReportDate;
  ClientModelReportResponse? get activeWeeklyOverallReport => _activeWeeklyOverallReport;
  Map<String, ClientModelReportResponse> get activeWeeklySystemReports =>
      Map.unmodifiable(_activeWeeklySystemReports);
  Map<String, Map<String, dynamic>> get weeklyConsiliumHistory =>
      Map.unmodifiable(_weeklyConsiliumHistory);
  bool get isWeeklyConsiliumCompleted =>
      _activeWeeklyOverallReport != null && _activeWeeklySystemReports.length == 8;
  LegacyVaultStatus get legacyVaultStatus => _legacyVaultStatus;
  bool get hasQuarantinedLegacyData => _legacyVaultStatus == LegacyVaultStatus.quarantined;
  bool get isInitialized => _isInitialized;

  AnamnesisSyncStatus _anamnesisSyncStatus = AnamnesisSyncStatus.idle;
  String? _anamnesisCycleId;
  int? _anamnesisIntakeVersion;
  DateTime? _anamnesisSyncedAt;
  String? _anamnesisSyncError;
  bool _isAnamnesisSyncing = false;

  /// Current synchronization status of anamnesis with Aegis Server M1.
  AnamnesisSyncStatus get anamnesisSyncStatus => _anamnesisSyncStatus;

  /// Active medical cycle ID returned by server intake confirmation.
  String? get anamnesisCycleId => _anamnesisCycleId;

  /// Intake version returned by server intake confirmation.
  int? get anamnesisIntakeVersion => _anamnesisIntakeVersion;

  /// UTC timestamp of confirmed server intake synchronization.
  DateTime? get anamnesisSyncedAt => _anamnesisSyncedAt;

  /// Most recent anamnesis sync error message, if any.
  String? get anamnesisSyncError => _anamnesisSyncError;

  /// Whether an anamnesis synchronization request is currently in-flight.
  bool get isAnamnesisSyncing => _isAnamnesisSyncing;

  /// Whether anamnesis has been confirmed by the server for the active patient profile.
  bool get isAnamnesisSynced => _anamnesisSyncStatus == AnamnesisSyncStatus.synced;

  /// Most recent persistence error encountered for active patient or unauthenticated storage.
  Object? get lastPersistenceError {
    if (_activePatientId != null) {
      final key = VaultStorageService.derivePatientStorageKey(_activePatientId!);
      return _persistenceErrorsByKey[key];
    }
    return _persistenceErrorsByKey[VaultStorageService.unauthenticatedStorageKey] ??
        _persistenceErrorsByKey[VaultStorageService.guestDraftStorageKey];
  }

  /// Whether the active patient or unauthenticated storage encountered a persistence error.
  bool get hasPersistenceError {
    if (_activePatientId != null) {
      final key = VaultStorageService.derivePatientStorageKey(_activePatientId!);
      return _persistenceErrorsByKey.containsKey(key);
    }
    return _persistenceErrorsByKey.containsKey(VaultStorageService.unauthenticatedStorageKey) ||
        _persistenceErrorsByKey.containsKey(VaultStorageService.guestDraftStorageKey);
  }

  /// Whether any storage key has an unhandled persistence error.
  bool get hasAnyPersistenceError => _persistenceErrorsByKey.isNotEmpty;

  /// Gets the persistence error for a specific [patientId] or [storageKey].
  Object? getPersistenceError({String? patientId, String? storageKey}) {
    if (storageKey != null) return _persistenceErrorsByKey[storageKey];
    if (patientId != null) {
      final key = VaultStorageService.derivePatientStorageKey(patientId);
      return _persistenceErrorsByKey[key];
    }
    return lastPersistenceError;
  }

  /// Clears recorded persistence errors.
  void clearPersistenceError({String? patientId, String? storageKey}) {
    if (storageKey != null) {
      _persistenceErrorsByKey.remove(storageKey);
    } else if (patientId != null) {
      final key = VaultStorageService.derivePatientStorageKey(patientId);
      _persistenceErrorsByKey.remove(key);
    } else {
      _persistenceErrorsByKey.clear();
    }
  }

  /// Resets write queue state for tests.
  @visibleForTesting
  static void resetWriteQueueForTesting() {
    _writeChains.clear();
    _lastCommittedSeqByKey.clear();
    _persistenceErrorsByKey.clear();
    _writeSequence = 0;
  }

  /// Awaits all pending writes for all storage keys or a specific patient.
  /// Throws the persistence error for the target patient/storage key if one occurred.
  Future<void> flushPendingWrites({String? patientId}) async {
    if (patientId != null) {
      final key = VaultStorageService.derivePatientStorageKey(patientId);
      final chain = _writeChains[key];
      if (chain != null) {
        try {
          await chain;
        } catch (_) {}
      }
      final error = _persistenceErrorsByKey[key];
      if (error != null) {
        throw error;
      }
    } else {
      final chains = _writeChains.values.toList();
      if (chains.isNotEmpty) {
        await Future.wait(chains.map((c) => c.catchError((_) {})));
      }
      if (_activePatientId != null) {
        final activeKey = VaultStorageService.derivePatientStorageKey(_activePatientId!);
        final activeError = _persistenceErrorsByKey[activeKey];
        if (activeError != null) throw activeError;
      } else {
        final unauthKey = VaultStorageService.unauthenticatedStorageKey;
        final guestKey = VaultStorageService.guestDraftStorageKey;
        final error = _persistenceErrorsByKey[unauthKey] ?? _persistenceErrorsByKey[guestKey];
        if (error != null) throw error;
      }
    }
  }

  VaultService({
    AuthSessionManager? authSessionManager,
    IVaultStorage? storage,
  })  : _storage = storage ?? VaultStorageService.activeStorage,
        _authSessionManager = authSessionManager {
    if (_authSessionManager != null) {
      _authSessionManager!.addListener(_onAuthSessionChanged);
      _activePatientId = _authSessionManager!.confirmedAttributionPatientId;
    }
    _initFuture = _initVault();
  }

  /// Binds an [AuthSessionManager] to synchronize active patient attribution.
  void bindAuthSessionManager(AuthSessionManager? manager) {
    if (identical(_authSessionManager, manager)) return;
    _authSessionManager?.removeListener(_onAuthSessionChanged);
    _authSessionManager = manager;
    if (manager != null) {
      manager.addListener(_onAuthSessionChanged);
      _onAuthSessionChanged();
    }
  }

  void _onAuthSessionChanged() {
    final manager = _authSessionManager;
    if (manager == null) return;
    final confirmedId = manager.confirmedAttributionPatientId;
    setActivePatientId(confirmedId);
  }

  /// Switches active patient profile with immediate synchronous memory wipe
  /// and epoch-fenced asynchronous restoration from secure storage.
  Future<void> setActivePatientId(String? newPatientId) async {
    if (_isDisposed) return;
    final trimmed = (newPatientId != null && newPatientId.trim().isNotEmpty)
        ? newPatientId.trim()
        : null;

    if (_activePatientId == trimmed) {
      return;
    }

    _sessionEpoch++;
    final currentEpoch = _sessionEpoch;
    _activePatientId = trimmed;

    // 1. SYNCHRONOUS PURGE: Old patient data MUST IMMEDIATELY disappear from memory
    _clearSensitiveMemoryData();
    notifyListeners();

    // 2. ASYNCHRONOUS LOAD for the new profile
    if (trimmed != null) {
      await _loadPatientVaultAsync(trimmed, currentEpoch);
    } else {
      await _loadGuestVaultAsync(currentEpoch);
    }
  }

  static Map<String, dynamic> _createDefaultHealthSystems() => {
    "cvs": {
      "name": "Cardiovascular",
      "completeness": 0.0,
      "color": "0xFFFF0000",
      "markers": {
        "smart_watch": {
          "workout_sessions": <Map<String, dynamic>>[],
          "heart_rate_samples": <Map<String, dynamic>>[],
          "workout_context": <String, dynamic>{},
          "workout_analysis_reports": <Map<String, dynamic>>[],
        }
      }
    },
    "gi_tract": {"name": "GI Tract", "completeness": 0.0, "color": "0xFFFFA500", "markers": {}},
    "respiratory": {"name": "Respiratory", "completeness": 0.0, "color": "0xFF00FFFF", "markers": {}},
    "nervous": {"name": "Nervous", "completeness": 0.0, "color": "0xFF8A2BE2", "markers": {}},
    "musculoskeletal": {"name": "Musculoskeletal", "completeness": 0.0, "color": "0xFFB0C4DE", "markers": {}},
    "kidneys": {"name": "Renal", "completeness": 0.0, "color": "0xFF0000FF", "markers": {}},
    "immune": {"name": "Immune and Blood", "completeness": 0.0, "color": "0xFF32CD32", "markers": {}},
    "endocrine": {"name": "Endocrine", "completeness": 0.0, "color": "0xFFEE82EE", "markers": {}},
  };

  static Map<String, Map<String, dynamic>> _createDefaultSystemFolders() => {
    'respiratory': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'immune': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'cvs': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'gi_tract': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'kidneys': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'endocrine': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'nervous': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'musculoskeletal': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'core_cascade': {
      'flash_general_shelf': 'Waiting for Gemini Flash output...',
      'flash_training_shelf': 'Gemini Flash deficiency logs...',
      'pro_general_shelf': 'Waiting for Gemini Pro output...',
      'pro_training_shelf': 'Gemini Pro deficiency logs...',
    },
    'notes_export': <String, dynamic>{},
    'global': {'labs': <String, dynamic>{}},
    'training_sandbox': {
      'raw_predictions': [],
      'missing_data': [],
      'risk_analysis': "PENDING...",
    },
  };

  void _clearSensitiveMemoryData() {
    _selectedSystem = null;
    _hasUnreadAthenaMessage = false;
    _curatorChatHistory.clear();
    _athenaClinicalLogs.clear();

    _data.clear();
    _data.addAll({
      'health_systems': _createDefaultHealthSystems(),
    });

    _systemFolders.clear();
    _systemFolders.addAll(_createDefaultSystemFolders());

    _anamnesisSyncStatus = AnamnesisSyncStatus.idle;
    _anamnesisCycleId = null;
    _anamnesisIntakeVersion = null;
    _anamnesisSyncedAt = null;
    _anamnesisSyncError = null;
    _isAnamnesisSyncing = false;
    _cachedLabLedgerState = null;
    _clientSystemReports.clear();
    _clientOverallReport = null;
    _isConsiliumCompleted = false;
    _consiliumCycleId = null;
    _consiliumHistory.clear();
    _activeWeeklySprintId = null;
    _activeWeeklySprintNumber = null;
    _activeWeeklyReportDate = null;
    _activeWeeklyOverallReport = null;
    _activeWeeklySystemReports.clear();
    _weeklyConsiliumHistory.clear();
  }

  Future<void> _initVault() async {
    final hasLegacy = await _storage.hasLegacyVault();
    if (hasLegacy) {
      _legacyVaultStatus = LegacyVaultStatus.quarantined;
    }

    final inMemorySessions = getWorkoutSessions();
    final inMemorySamples = getHeartRateSamples();
    final currentEpoch = _sessionEpoch;

    if (_activePatientId != null) {
      await _loadPatientVaultAsync(_activePatientId!, currentEpoch);
    } else {
      final pendingUnauthed = _writeChains[VaultStorageService.unauthenticatedStorageKey];
      if (pendingUnauthed != null) {
        try {
          await pendingUnauthed;
        } catch (_) {}
      }
      final unauthed = await _storage.loadUnauthenticatedVault();
      if (_sessionEpoch == currentEpoch && _activePatientId == null && unauthed.isNotEmpty) {
        _applyLoadedSnapshot(unauthed);
      } else {
        await _loadGuestVaultAsync(currentEpoch);
      }
    }

    _ensureSmartWatchStructure();

    // Preserve any in-memory workout sessions/samples if created before storage finished loading
    if (inMemorySessions.isNotEmpty) {
      final sw = _smartWatchData;
      final existingSessions = (sw['workout_sessions'] as List<dynamic>? ?? []).toList();
      for (var s in inMemorySessions) {
        if (!existingSessions.any((e) => e is Map && e['session_id'] == s['session_id'])) {
          existingSessions.add(s);
        }
      }
      sw['workout_sessions'] = existingSessions;
    }
    if (inMemorySamples.isNotEmpty) {
      final sw = _smartWatchData;
      final existingSamples = (sw['heart_rate_samples'] as List<dynamic>? ?? []).toList();
      for (var s in inMemorySamples) {
        existingSamples.add(s);
      }
      sw['heart_rate_samples'] = existingSamples;
    }

    if (_isDisposed) return;
    _isInitialized = true;
    notifyListeners();
  }

  Future<void> _loadPatientVaultAsync(String patientId, int epoch) async {
    final storageKey = VaultStorageService.derivePatientStorageKey(patientId);

    // Drain any pending write for this patient first to ensure we read the latest state
    final pendingChain = _writeChains[storageKey];
    if (pendingChain != null) {
      try {
        await pendingChain;
      } catch (_) {}
    }

    // FENCE CHECK before read
    if (_isDisposed || _sessionEpoch != epoch || _activePatientId != patientId) {
      return;
    }

    Map<String, dynamic> loadedData = {};
    try {
      loadedData = await _storage.loadPatientVault(storageKey);
    } catch (e) {
      _persistenceErrorsByKey[storageKey] = e;
      debugPrint('[VaultService] Error loading patient vault: $e');
    }

    // FENCE CHECK after read: Discard stale completion if patient or epoch changed or disposed
    if (_isDisposed || _sessionEpoch != epoch || _activePatientId != patientId) {
      return;
    }

    if (loadedData.isNotEmpty) {
      _applyLoadedSnapshot(loadedData);
    }
    _ensureSmartWatchStructure();
    notifyListeners();
  }

  Future<void> _loadGuestVaultAsync(int epoch) async {
    final pendingChain = _writeChains[VaultStorageService.guestDraftStorageKey];
    if (pendingChain != null) {
      try {
        await pendingChain;
      } catch (_) {}
    }

    // FENCE CHECK before read
    if (_isDisposed || _sessionEpoch != epoch || _activePatientId != null) {
      return;
    }

    Map<String, dynamic> guestDraft = {};
    try {
      guestDraft = await _storage.loadGuestDraft();
    } catch (e) {
      _persistenceErrorsByKey[VaultStorageService.guestDraftStorageKey] = e;
      debugPrint('[VaultService] Error loading guest draft: $e');
    }

    // FENCE CHECK after read: Discard stale completion if patient or epoch changed or disposed
    if (_isDisposed || _sessionEpoch != epoch || _activePatientId != null) {
      return;
    }

    if (guestDraft.isNotEmpty) {
      _applyGuestDraft(guestDraft);
    }
    _ensureSmartWatchStructure();
    notifyListeners();
  }

  void _applyLoadedSnapshot(Map<String, dynamic> loadedData) {
    if (loadedData.containsKey('health_systems')) {
      _data['health_systems'] = loadedData['health_systems'];
    }
    if (loadedData.containsKey('system_folders')) {
      final savedFolders = loadedData['system_folders'] as Map<String, dynamic>;
      savedFolders.forEach((key, value) {
        if (_systemFolders.containsKey(key) && value is Map) {
          _systemFolders[key] = Map<String, dynamic>.from(value);
        }
      });
    }
    if (loadedData.containsKey('curator_chat_history')) {
      final savedChat = loadedData['curator_chat_history'] as List;
      _curatorChatHistory.clear();
      for (var item in savedChat) {
        if (item is Map) {
          _curatorChatHistory.add(Map<String, String>.from(item));
        }
      }
    }
    if (loadedData.containsKey('athena_clinical_logs')) {
      final savedLogs = loadedData['athena_clinical_logs'] as List;
      _athenaClinicalLogs.clear();
      for (var item in savedLogs) {
        if (item is Map) {
          _athenaClinicalLogs.add(Map<String, dynamic>.from(item));
        }
      }
    }

    if (loadedData.containsKey('vitals_thresholds')) {
      _systemFolders['global']?['vitals_thresholds'] = loadedData['vitals_thresholds'];
    }

    if (loadedData.containsKey('anamnesis_sync') && loadedData['anamnesis_sync'] is Map) {
      final syncMap = loadedData['anamnesis_sync'] as Map;
      _anamnesisCycleId = syncMap['cycle_id'] as String?;
      _anamnesisIntakeVersion = syncMap['intake_version'] as int?;
      if (syncMap['synced_at'] != null) {
        _anamnesisSyncedAt = DateTime.tryParse(syncMap['synced_at'].toString());
      }
      final statusStr = syncMap['status'] as String?;
      if (statusStr != null) {
        _anamnesisSyncStatus = AnamnesisSyncStatus.values.firstWhere(
          (e) => e.name == statusStr,
          orElse: () => AnamnesisSyncStatus.idle,
        );
      }
    } else {
      _anamnesisCycleId = null;
      _anamnesisIntakeVersion = null;
      _anamnesisSyncedAt = null;
      _anamnesisSyncStatus = AnamnesisSyncStatus.idle;
    }

    if (_systemFolders['global']?['lab_ledger_snapshot'] is Map) {
      try {
        _cachedLabLedgerState = LabLedgerStateResponse.fromJson(
          Map<String, dynamic>.from(_systemFolders['global']!['lab_ledger_snapshot'] as Map),
        );
      } catch (_) {
        _cachedLabLedgerState = null;
      }
    } else {
      _cachedLabLedgerState = null;
    }

    if (loadedData.containsKey('client_consilium') && loadedData['client_consilium'] is Map) {
      final consiliumMap = loadedData['client_consilium'] as Map;
      _isConsiliumCompleted = consiliumMap['is_completed'] == true;
      _consiliumCycleId = consiliumMap['cycle_id'] as String?;
      if (consiliumMap['overall_report'] is Map) {
        try {
          _clientOverallReport = ClientModelReportResponse.fromJson(
            Map<String, dynamic>.from(consiliumMap['overall_report'] as Map),
          );
        } catch (_) {
          _clientOverallReport = null;
        }
      } else {
        _clientOverallReport = null;
      }

      _clientSystemReports.clear();
      if (consiliumMap['system_reports'] is Map) {
        (consiliumMap['system_reports'] as Map).forEach((k, v) {
          if (v is Map) {
            try {
              _clientSystemReports[k.toString()] = ClientModelReportResponse.fromJson(
                Map<String, dynamic>.from(v),
              );
            } catch (_) {}
          }
        });
      }

      _consiliumHistory.clear();
      if (consiliumMap['history'] is Map) {
        (consiliumMap['history'] as Map).forEach((k, v) {
          if (v is Map) {
            _consiliumHistory[k.toString()] = Map<String, dynamic>.from(v);
          }
        });
      }
    } else {
      _isConsiliumCompleted = false;
      _consiliumCycleId = null;
      _clientOverallReport = null;
      _clientSystemReports.clear();
      _consiliumHistory.clear();
    }

    if (loadedData.containsKey('weekly_consilium') && loadedData['weekly_consilium'] is Map) {
      final weeklyMap = loadedData['weekly_consilium'] as Map;
      _activeWeeklySprintId = weeklyMap['active_sprint_id'] as String?;
      _activeWeeklySprintNumber = weeklyMap['active_sprint_number'] as int?;
      _activeWeeklyReportDate = weeklyMap['active_report_date'] as String?;

      if (weeklyMap['overall_report'] is Map) {
        try {
          _activeWeeklyOverallReport = ClientModelReportResponse.fromJson(
            Map<String, dynamic>.from(weeklyMap['overall_report'] as Map),
          );
        } catch (_) {
          _activeWeeklyOverallReport = null;
        }
      } else {
        _activeWeeklyOverallReport = null;
      }

      _activeWeeklySystemReports.clear();
      if (weeklyMap['system_reports'] is Map) {
        (weeklyMap['system_reports'] as Map).forEach((k, v) {
          if (v is Map) {
            try {
              _activeWeeklySystemReports[k.toString()] =
                  ClientModelReportResponse.fromJson(
                Map<String, dynamic>.from(v),
              );
            } catch (_) {}
          }
        });
      }

      _weeklyConsiliumHistory.clear();
      if (weeklyMap['history'] is Map) {
        (weeklyMap['history'] as Map).forEach((k, v) {
          if (v is Map) {
            _weeklyConsiliumHistory[k.toString()] =
                Map<String, dynamic>.from(v);
          }
        });
      }
    } else {
      _activeWeeklySprintId = null;
      _activeWeeklySprintNumber = null;
      _activeWeeklyReportDate = null;
      _activeWeeklyOverallReport = null;
      _activeWeeklySystemReports.clear();
      _weeklyConsiliumHistory.clear();
    }
  }

  Map<String, dynamic> _createSnapshot() {
    final raw = {
      'health_systems': _data['health_systems'],
      'system_folders': _systemFolders,
      'curator_chat_history': _curatorChatHistory,
      'vitals_thresholds': _systemFolders['global']?['vitals_thresholds'],
      'athena_clinical_logs': _athenaClinicalLogs,
      'anamnesis_sync': {
        'status': _anamnesisSyncStatus.name,
        'cycle_id': _anamnesisCycleId,
        'intake_version': _anamnesisIntakeVersion,
        'synced_at': _anamnesisSyncedAt?.toIso8601String(),
      },
      'client_consilium': {
        'is_completed': _isConsiliumCompleted,
        'cycle_id': _consiliumCycleId,
        'overall_report': _clientOverallReport?.toJson(),
        'system_reports': _clientSystemReports.map((k, v) => MapEntry(k, v.toJson())),
        'history': _consiliumHistory,
      },
      'weekly_consilium': {
        'active_sprint_id': _activeWeeklySprintId,
        'active_sprint_number': _activeWeeklySprintNumber,
        'active_report_date': _activeWeeklyReportDate,
        'overall_report': _activeWeeklyOverallReport?.toJson(),
        'system_reports': _activeWeeklySystemReports.map((k, v) => MapEntry(k, v.toJson())),
        'history': _weeklyConsiliumHistory,
      },
    };
    // Deep clone to isolate snapshot from subsequent in-memory mutations (e.g. profile wipe)
    return json.decode(json.encode(raw)) as Map<String, dynamic>;
  }

  /// Persists current vault snapshot to secure storage.
  /// Enforces sequential write ordering per storage key and prevents stale overwrites.
  Future<void> _persist() {
    if (_isDisposed) return Future.value();

    final currentPatientId = _activePatientId;
    final int currentEpoch = _sessionEpoch;
    final int writeSeq = ++_writeSequence;
    final snapshot = _createSnapshot();

    if (currentPatientId != null) {
      final storageKey = VaultStorageService.derivePatientStorageKey(currentPatientId);
      return _enqueueWrite(
        storageKey: storageKey,
        writeSeq: writeSeq,
        patientId: currentPatientId,
        epoch: currentEpoch,
        writeAction: () => _storage.savePatientVault(storageKey, snapshot),
        errorContext: 'patient vault ($currentPatientId)',
      );
    } else {
      final draft = _createGuestDraftSnapshot();
      Future<void> guestFuture = Future.value();
      if (draft.isNotEmpty) {
        guestFuture = _enqueueWrite(
          storageKey: VaultStorageService.guestDraftStorageKey,
          writeSeq: writeSeq,
          patientId: null,
          epoch: currentEpoch,
          writeAction: () => _storage.saveGuestDraft(draft),
          errorContext: 'guest draft',
        );
      }
      final unauthedFuture = _enqueueWrite(
        storageKey: VaultStorageService.unauthenticatedStorageKey,
        writeSeq: writeSeq,
        patientId: null,
        epoch: currentEpoch,
        writeAction: () => _storage.saveUnauthenticatedVault(snapshot),
        errorContext: 'unauthenticated vault',
      );
      return Future.wait([guestFuture, unauthedFuture]);
    }
  }

  Future<void> _enqueueWrite({
    required String storageKey,
    required int writeSeq,
    required String? patientId,
    required int epoch,
    required Future<void> Function() writeAction,
    required String errorContext,
  }) {
    final completer = Completer<void>();
    // Attach error listener so unawaited futures never produce unhandled errors in the zone
    completer.future.catchError((_) {});

    Future<void> runTask() async {
      final lastCommitted = _lastCommittedSeqByKey[storageKey] ?? 0;
      if (writeSeq <= lastCommitted) {
        debugPrint(
          '[VaultService] Skipping obsolete write #$writeSeq for $storageKey (already committed #$lastCommitted)',
        );
        if (!completer.isCompleted) completer.complete();
        return;
      }

      try {
        await writeAction();
        _lastCommittedSeqByKey[storageKey] = writeSeq;
        _persistenceErrorsByKey.remove(storageKey);
        if (!completer.isCompleted) completer.complete();
      } catch (e) {
        _persistenceErrorsByKey[storageKey] = e;
        debugPrint('[VaultService] Failed to persist $errorContext: $e');
        if (!completer.isCompleted) completer.completeError(e);
      }
    }

    final activeChain = _writeChains[storageKey];
    if (activeChain == null) {
      // No write in flight for this storage key: start immediately
      late final Future<void> immediateFuture;
      immediateFuture = runTask().whenComplete(() {
        if (identical(_writeChains[storageKey], immediateFuture)) {
          _writeChains.remove(storageKey);
        }
      });
      _writeChains[storageKey] = immediateFuture;
    } else {
      // Write already in flight: serialize behind previous write
      late final Future<void> nextChain;
      nextChain = activeChain
          .catchError((_) {})
          .then((_) => runTask())
          .whenComplete(() {
            if (identical(_writeChains[storageKey], nextChain)) {
              _writeChains.remove(storageKey);
            }
          });
      _writeChains[storageKey] = nextChain;
    }

    return completer.future;
  }

  Map<String, dynamic> _createGuestDraftSnapshot() {
    final global = _systemFolders['global'] ?? {};
    final notes = _systemFolders['notes_export'] ?? {};

    final anamnesisGlobal = <String, dynamic>{};
    for (final entry in global.entries) {
      final k = entry.key;
      if (k == 'vitals_thresholds' || k == 'athena_sprint_day' || k == 'athena_last_prompt_time') {
        continue;
      }
      anamnesisGlobal[k] = entry.value;
    }

    if (anamnesisGlobal.isEmpty) {
      return {};
    }

    final raw = {
      'version': 1,
      'global': anamnesisGlobal,
      'notes_export': notes,
      'timestamp': DateTime.now().toUtc().toIso8601String(),
    };
    return json.decode(json.encode(raw)) as Map<String, dynamic>;
  }

  void _applyGuestDraft(Map<String, dynamic> draft) {
    final globalDraft = draft['global'];
    if (globalDraft is Map) {
      final currentGlobal = _systemFolders['global'] ??= <String, dynamic>{};
      globalDraft.forEach((k, v) {
        final keyStr = k.toString();
        currentGlobal[keyStr] = v;
        _applySingleAnamnesisDerivation(keyStr, v);
      });
    }
    final notesDraft = draft['notes_export'];
    if (notesDraft is Map) {
      final currentNotes = _systemFolders['notes_export'] ??= <String, dynamic>{};
      notesDraft.forEach((k, v) {
        currentNotes[k.toString()] = v;
      });
    }
  }

  /// Checks if a guest anamnesis draft is stored locally.
  Future<bool> hasGuestAnamnesisDraft() async {
    final pendingChain = _writeChains[VaultStorageService.guestDraftStorageKey];
    if (pendingChain != null) {
      try {
        await pendingChain;
      } catch (_) {}
    }
    final draft = await _storage.loadGuestDraft();
    return draft.isNotEmpty;
  }

  /// Retrieves the guest anamnesis draft, if any.
  Future<Map<String, dynamic>?> getGuestAnamnesisDraft() async {
    final pendingChain = _writeChains[VaultStorageService.guestDraftStorageKey];
    if (pendingChain != null) {
      try {
        await pendingChain;
      } catch (_) {}
    }
    final draft = await _storage.loadGuestDraft();
    return draft.isNotEmpty ? draft : null;
  }

  /// Associates the existing guest anamnesis draft with the currently active confirmed patient.
  /// Does NOT delete the original draft until confirmed server synchronization.
  Future<bool> associateGuestDraftToActivePatient({bool overwriteExisting = false}) async {
    if (_isDisposed) return false;

    // 1. Capture confirmed patientId and sessionEpoch BEFORE await
    final confirmedPatientId = _activePatientId;
    final capturedEpoch = _sessionEpoch;

    if (confirmedPatientId == null) {
      return false;
    }

    // Await any pending guest draft write to ensure reading the latest state
    final pendingGuestChain = _writeChains[VaultStorageService.guestDraftStorageKey];
    if (pendingGuestChain != null) {
      try {
        await pendingGuestChain;
      } catch (_) {}
    }

    final draft = await _storage.loadGuestDraft();

    // 2. RE-VERIFY BOTH VALUES AFTER ASYNC READ
    // If session changed, user logged out, pairing cancelled, or switched to another patient:
    if (_isDisposed ||
        _sessionEpoch != capturedEpoch ||
        _activePatientId != confirmedPatientId) {
      debugPrint('[VaultService] Aborting guest draft association: session changed during read.');
      return false;
    }

    if (draft.isEmpty) {
      return false;
    }
    final globalDraft = draft['global'];
    if (globalDraft is! Map) {
      return false;
    }

    final currentGlobal = _systemFolders['global'] ??= <String, dynamic>{};
    globalDraft.forEach((k, v) {
      final keyStr = k.toString();
      if (overwriteExisting || !currentGlobal.containsKey(keyStr)) {
        currentGlobal[keyStr] = v;
        _mirrorToNotesExport('anamnesis_$keyStr', v);
        _applySingleAnamnesisDerivation(keyStr, v);
      }
    });

    // 3. Persist to active patient secure vault and verify success
    final activeKey = VaultStorageService.derivePatientStorageKey(confirmedPatientId);
    try {
      await _persist();
    } catch (e) {
      debugPrint('[VaultService] Storage failure during guest draft association: $e');
      return false;
    }

    if (_persistenceErrorsByKey.containsKey(activeKey)) {
      return false;
    }

    notifyListeners();
    return true;
  }

  /// Validates and constructs typed [AnamnesisQuestionnaireAnswers] from vault data.
  /// Returns null if any of the mandatory 17 card answers are missing or invalid.
  AnamnesisQuestionnaireAnswers? buildAnamnesisAnswers() {
    final global = _systemFolders['global'];
    if (global == null) return null;

    final ageConf = global['age_confirmation']?.toString().trim();
    if (ageConf == null || ageConf.isEmpty) return null;

    final gender = global['gender']?.toString().trim();
    if (gender == null || gender.isEmpty) return null;

    final ageRaw = global['age'];
    final int? age = ageRaw is int ? ageRaw : int.tryParse(ageRaw?.toString().trim() ?? '');
    if (age == null || age < 18 || age > 120) return null;

    final heightRaw = global['height'];
    final int? height = heightRaw is int ? heightRaw : int.tryParse(heightRaw?.toString().trim() ?? '');
    if (height == null || height < 50 || height > 260) return null;

    final weightRaw = global['weight'];
    final int? weight = weightRaw is int ? weightRaw : int.tryParse(weightRaw?.toString().trim() ?? '');
    if (weight == null || weight < 30 || weight > 350) return null;

    final nicotineStatus = global['nicotine_status']?.toString().trim();
    if (nicotineStatus == null || nicotineStatus.isEmpty) return null;

    final alcoholStatus = global['alcohol_status']?.toString().trim();
    if (alcoholStatus == null || alcoholStatus.isEmpty) return null;

    final specFactors = global['spec_factors']?.toString().trim();
    if (specFactors == null || specFactors.isEmpty) return null;

    final sleepQuality = global['sleep_quality']?.toString().trim();
    if (sleepQuality == null || sleepQuality.isEmpty) return null;

    final dietTypes = global['diet_types']?.toString().trim();
    if (dietTypes == null || dietTypes.isEmpty) return null;

    final dietHabits = global['diet_habits']?.toString().trim();
    if (dietHabits == null || dietHabits.isEmpty) return null;

    final giSignals = global['gi_signals']?.toString().trim();
    if (giSignals == null || giSignals.isEmpty) return null;

    final workType = global['work_type']?.toString().trim();
    if (workType == null || workType.isEmpty) return null;

    final chronicDiseases = global['chronic_diseases']?.toString().trim();
    if (chronicDiseases == null || chronicDiseases.isEmpty) return null;

    final recentIllness = global['recent_illness']?.toString().trim();
    if (recentIllness == null || recentIllness.isEmpty) return null;

    final criticalInfections = global['critical_infections']?.toString().trim();
    if (criticalInfections == null || criticalInfections.isEmpty) return null;

    final heredityRisks = global['heredity_risks']?.toString().trim();
    if (heredityRisks == null || heredityRisks.isEmpty) return null;

    final medications = global['medications']?.toString().trim();
    if (medications == null || medications.isEmpty) return null;

    return AnamnesisQuestionnaireAnswers(
      ageConfirmation: ageConf,
      gender: gender,
      age: age,
      height: height,
      weight: weight,
      nicotineStatus: nicotineStatus,
      nicotineYears: global['nicotine_years']?.toString().trim(),
      nicotineFreq: global['nicotine_freq']?.toString().trim(),
      alcoholStatus: alcoholStatus,
      alcoholType: global['alcohol_type']?.toString().trim(),
      specFactors: specFactors,
      sleepQuality: sleepQuality,
      dietTypes: dietTypes,
      dietHabits: dietHabits,
      giSignals: giSignals,
      workType: workType,
      workSchedule: global['work_schedule']?.toString().trim(),
      sportLevel: global['sport_level']?.toString().trim(),
      chronicDiseases: chronicDiseases,
      oncoStatus: global['onco_status']?.toString().trim(),
      recentIllness: recentIllness,
      criticalInfections: criticalInfections,
      heredityRisks: heredityRisks,
      medications: medications,
      userTextMedications: global['text_medications']?.toString().trim(),
      userTextHeredityRisks: global['text_heredity_risks']?.toString().trim(),
      userTextChronicDiseases: global['text_chronic_diseases']?.toString().trim(),
      userTextRecentIllness: global['text_recent_illness']?.toString().trim(),
      userTextCriticalInfections: global['text_critical_infections']?.toString().trim(),
      userTextSpecFactors: global['text_spec_factors']?.toString().trim(),
      userTextDietHabits: global['text_diet_habits']?.toString().trim(),
      userTextGiSignals: global['text_gi_signals']?.toString().trim(),
    );
  }

  /// Whether the anamnesis questionnaire is fully answered and ready for server intake.
  bool get isAnamnesisComplete => buildAnamnesisAnswers() != null;

  /// Synchronizes complete raw anamnesis questionnaire with Aegis Server M1.
  /// Follows strict Project Aegis Stage 5.4D-3 protocol:
  /// 1. Concurrency guard prevents duplicate in-flight requests.
  /// 2. Completeness validation: rejects submission if any of the 17 cards are incomplete.
  /// 3. Confirmed patient authorization: checks active session binding.
  /// 4. Scope verification: requires `cascade:intake` permission.
  /// 5. Epoch fencing: verifies patient attribution both before and after network dispatch.
  /// 6. Confirmation: updates sync status and clears guest draft ONLY upon HTTP 200 confirmation.
  Future<AnamnesisSyncResult> syncAnamnesisToServer({
    required AegisApiClient apiClient,
    AuthSessionManager? authSessionManager,
  }) async {
    // 1. Guard against parallel concurrent submissions
    if (_isAnamnesisSyncing) {
      debugPrint('[VaultService] Anamnesis sync already in progress. Ignoring concurrent call.');
      return AnamnesisSyncResult.failure(
        _anamnesisSyncStatus,
        'Sync already in progress',
      );
    }

    // 2. Completeness validation: do not submit incomplete anamnesis
    final answers = buildAnamnesisAnswers();
    if (answers == null) {
      _anamnesisSyncStatus = AnamnesisSyncStatus.draftSaved;
      _anamnesisSyncError = 'Anamnesis questionnaire is incomplete.';
      notifyListeners();
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.draftSaved,
        'Cannot submit incomplete anamnesis: all 17 cards must be completed.',
      );
    }

    // 3. Authorization check
    final sessionManager = authSessionManager ?? _authSessionManager;
    final String? confirmedPatientId = sessionManager?.confirmedAttributionPatientId ?? _activePatientId;
    final bool isAuthenticated = (sessionManager?.isAuthenticated ?? false) && confirmedPatientId != null;

    if (!isAuthenticated) {
      _anamnesisSyncStatus = AnamnesisSyncStatus.unauthorized;
      _anamnesisSyncError = 'Device pairing required to synchronize anamnesis with server.';
      notifyListeners();
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.unauthorized,
        'Device pairing required to synchronize anamnesis with Aegis server.',
      );
    }

    // 4. Capture session epoch and patient attribution
    final int capturedEpoch = _sessionEpoch;
    final String capturedPatientId = confirmedPatientId;

    // ACQUIRE SINGLE-FLIGHT LOCK BEFORE ANY ASYNC OPERATIONS (including scope upgrade)
    _isAnamnesisSyncing = true;
    _anamnesisSyncStatus = AnamnesisSyncStatus.syncing;
    _anamnesisSyncError = null;
    notifyListeners();

    try {
      // 5. Scope check & planned upgrade: cascade:intake required
      final session = sessionManager?.currentSession;
      if (session != null && !session.hasScope('cascade:intake')) {
        if (sessionManager != null) {
          // Attempt planned one-off token rotation via ensureRequiredScopes
          final upgraded = await sessionManager.ensureRequiredScopes(
            requiredScopes: const [AegisScopes.cascadeIntake],
          );
          // Epoch fencing check after async scope upgrade
          if (_isDisposed ||
              _sessionEpoch != capturedEpoch ||
              _activePatientId != capturedPatientId) {
            return AnamnesisSyncResult.failure(
              AnamnesisSyncStatus.idle,
              'Patient switched during scope upgrade; aborting sync.',
            );
          }
          if (!upgraded ||
              sessionManager.currentSession == null ||
              !sessionManager.currentSession!.hasScope('cascade:intake')) {
            _anamnesisSyncStatus = AnamnesisSyncStatus.insufficientScopes;
            _anamnesisSyncError = 'Access denied: session lacks required scope (cascade:intake).';
            notifyListeners();
            return AnamnesisSyncResult.failure(
              AnamnesisSyncStatus.insufficientScopes,
              'Access denied: session lacks required scope (cascade:intake).',
            );
          }
        } else {
          _anamnesisSyncStatus = AnamnesisSyncStatus.insufficientScopes;
          _anamnesisSyncError = 'Access denied: session lacks required scope (cascade:intake).';
          notifyListeners();
          return AnamnesisSyncResult.failure(
            AnamnesisSyncStatus.insufficientScopes,
            'Access denied: session lacks required scope (cascade:intake).',
          );
        }
      }

      // 6. Ensure all pending local storage writes are committed before network call
      await flushPendingWrites(patientId: capturedPatientId);

      // Epoch fencing check after local write flush
      if (_isDisposed ||
          _sessionEpoch != capturedEpoch ||
          _activePatientId != capturedPatientId) {
        return AnamnesisSyncResult.failure(
          AnamnesisSyncStatus.idle,
          'Patient switched during local write flush; aborting sync.',
        );
      }

      // 7. Dispatch to server
      final request = AnamnesisIntakeRequest(answers: answers);
      final response = await apiClient.submitAnamnesisIntake(request);

      // 8. Re-verify epoch and patient attribution after async network call
      if (_isDisposed ||
          _sessionEpoch != capturedEpoch ||
          _activePatientId != capturedPatientId) {
        debugPrint('[VaultService] Discarding anamnesis response: active patient or session epoch changed during request.');
        return AnamnesisSyncResult.failure(
          AnamnesisSyncStatus.idle,
          'Patient switched during network dispatch; response discarded.',
        );
      }

      // 9. Server confirmed 200 OK:
      // Update in-memory sync metadata and establish final synced status
      _anamnesisCycleId = response.cycleId;
      _anamnesisIntakeVersion = response.intakeVersion;
      _anamnesisSyncedAt = DateTime.now().toUtc();
      _anamnesisSyncStatus = AnamnesisSyncStatus.synced;
      _anamnesisSyncError = null;

      // Update shelf for consilium status in UI
      _systemFolders['core_cascade']?['flash_general_shelf'] =
          'Anamnesis processed by server (Cycle ${response.cycleId}, v${response.intakeVersion})';

      // 10. Persist confirmed sync state in patient secure vault and VERIFY PERSISTENCE!
      // This ensures status: 'synced' is written to storage BEFORE guest draft deletion.
      try {
        await _persist();
        await flushPendingWrites(patientId: capturedPatientId);
      } catch (storageError) {
        debugPrint('[VaultService] Secure storage failed to persist sync metadata: $storageError');
        _anamnesisSyncStatus = AnamnesisSyncStatus.error;
        _anamnesisSyncError = 'Failed to securely persist sync confirmation: $storageError';
        notifyListeners();
        // CRITICAL: Return failure; guest draft is NOT cleared!
        return AnamnesisSyncResult.failure(
          AnamnesisSyncStatus.error,
          'Failed to securely persist intake confirmation: $storageError',
        );
      }

      // 11. Re-verify epoch after patient vault persistence
      if (_isDisposed ||
          _sessionEpoch != capturedEpoch ||
          _activePatientId != capturedPatientId) {
        debugPrint('[VaultService] Patient changed during persistence; aborting draft cleanup.');
        return AnamnesisSyncResult.failure(
          AnamnesisSyncStatus.idle,
          'Patient switched during storage write; cleanup aborted.',
        );
      }

      // 12. Local persistence confirmed! NOW AND ONLY NOW: clear guest draft
      await clearGuestDraftAfterServerSync();

      // 13. Re-verify epoch after draft cleanup
      if (_isDisposed ||
          _sessionEpoch != capturedEpoch ||
          _activePatientId != capturedPatientId) {
        return AnamnesisSyncResult.failure(
          AnamnesisSyncStatus.idle,
          'Patient switched after draft clear; response discarded.',
        );
      }

      notifyListeners();
      return AnamnesisSyncResult.success(response);
    } on AegisForbiddenException catch (e) {
      if (_sessionEpoch == capturedEpoch) {
        _anamnesisSyncStatus = AnamnesisSyncStatus.insufficientScopes;
        _anamnesisSyncError = 'Access denied: ${e.message}';
        notifyListeners();
      }
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.insufficientScopes,
        'Forbidden: missing cascade:intake scope.',
      );
    } on AegisAuthException catch (e) {
      if (_sessionEpoch == capturedEpoch) {
        _anamnesisSyncStatus = AnamnesisSyncStatus.unauthorized;
        _anamnesisSyncError = 'Session expired: ${e.message}';
        notifyListeners();
      }
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.unauthorized,
        'Unauthorized: session expired or token invalid.',
      );
    } on AegisTimeoutException catch (e) {
      if (_sessionEpoch == capturedEpoch) {
        _anamnesisSyncStatus = AnamnesisSyncStatus.offline;
        _anamnesisSyncError = 'Connection timed out. Draft saved securely. Tap to retry.';
        notifyListeners();
      }
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.offline,
        'Timeout during intake submission: ${e.message}',
      );
    } on AegisNetworkException catch (e) {
      if (_sessionEpoch == capturedEpoch) {
        _anamnesisSyncStatus = AnamnesisSyncStatus.offline;
        _anamnesisSyncError = 'Network connection failed. Draft saved securely. Tap to retry.';
        notifyListeners();
      }
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.offline,
        'Network error: ${e.message}',
      );
    } catch (e) {
      if (_sessionEpoch == capturedEpoch) {
        _anamnesisSyncStatus = AnamnesisSyncStatus.error;
        _anamnesisSyncError = 'Sync failed: $e';
        notifyListeners();
      }
      return AnamnesisSyncResult.failure(
        AnamnesisSyncStatus.error,
        'Intake submission failed: $e',
      );
    } finally {
      _isAnamnesisSyncing = false;
    }
  }

  /// Clears the guest anamnesis draft ONLY after confirmed server persistence.
  /// Enqueued through the write queue so any in-flight write finishes before clearing.
  Future<void> clearGuestDraftAfterServerSync() async {
    final clearSeq = ++_writeSequence;
    await _enqueueWrite(
      storageKey: VaultStorageService.guestDraftStorageKey,
      writeSeq: clearSeq,
      patientId: null,
      epoch: _sessionEpoch,
      writeAction: () => _storage.clearGuestDraft(),
      errorContext: 'clear guest draft',
    );
  }

  /// Clears persisted vault data for [patientId] (or active patient).
  /// Serialized through write queue: awaits pending writes before clearing,
  /// and prevents subsequent writes from bypassing the clear operation.
  Future<void> clearPatientVault([String? patientId]) async {
    final targetId = patientId ?? _activePatientId;
    final String storageKey = targetId != null
        ? VaultStorageService.derivePatientStorageKey(targetId)
        : VaultStorageService.unauthenticatedStorageKey;

    final clearSeq = ++_writeSequence;
    await _enqueueWrite(
      storageKey: storageKey,
      writeSeq: clearSeq,
      patientId: targetId,
      epoch: _sessionEpoch,
      writeAction: () async {
        if (targetId != null) {
          await _storage.clearPatientVault(storageKey);
        } else {
          await _storage.clearUnauthenticatedVault();
        }
      },
      errorContext: 'clear patient vault ($storageKey)',
    );

    if (targetId == _activePatientId) {
      _clearSensitiveMemoryData();
      notifyListeners();
    }
  }

  /// Inspects quarantined legacy vault data without attributing it to any patient.
  Future<Map<String, dynamic>?> inspectQuarantinedLegacyData() async {
    return await _storage.loadLegacyVault();
  }

  @override
  void dispose() {
    _isDisposed = true;
    _isAnamnesisSyncing = false;
    _authSessionManager?.removeListener(_onAuthSessionChanged);
    super.dispose();
  }

  String? get selectedSystem => _selectedSystem;
  Map<String, dynamic> get vaultData => _data;

  // Internal helper for automatic mirroring to the notes folder
  void _mirrorToNotesExport(String key, dynamic value) {
    _systemFolders['notes_export']?[key] = {
      'value': value,
      'timestamp': DateTime.now().toIso8601String(),
    };
  }

  // Getter for retrieving context
  Map<String, dynamic> getSystemContext(String systemKey) {
    return _systemFolders[systemKey] ?? {};
  }

  void selectSystem(String? key) {
    _selectedSystem = key;
    notifyListeners();
  }

  // Getters for quick reading of vital markers from smart watch / Atlas
  Map<String, dynamic> get _smartWatchData {
    try {
      final cvs = _data['health_systems']?['cvs'] as Map<String, dynamic>?;
      final markers = cvs?['markers'] as Map<String, dynamic>?;
      return markers?['smart_watch'] as Map<String, dynamic>? ?? {};
    } catch (_) {
      return {};
    }
  }

  int? get systolic => _smartWatchData['pressure']?['systolic'] as int?;
  int? get diastolic => _smartWatchData['pressure']?['diastolic'] as int?;
  int? get pulse => _smartWatchData['pulse']?['value'] as int?;
  int? get spo2 => _smartWatchData['spo2']?['value'] as int?;
  String? get ecgSummary => _smartWatchData['ecg_status']?['value'] as String?;

  // METHOD FOR SAVING ANAMNESIS
  void saveAnamnesis(String cardId, dynamic value, {String userText = ""}) {
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']![cardId] = value;
    if (userText.isNotEmpty) {
      _systemFolders['global']!['text_$cardId'] = userText;
    }

    _mirrorToNotesExport('anamnesis_$cardId', value);
    if (userText.isNotEmpty) {
      _mirrorToNotesExport('anamnesis_text_$cardId', userText);
    }

    _applySingleAnamnesisDerivation(cardId, value);

    _persist();
    notifyListeners();
  }

  void _applySingleAnamnesisDerivation(String cardId, dynamic value) {
    final String valueString = value.toString().toLowerCase();

    switch (cardId) {
      case 'gender':
        _systemFolders['endocrine']?['user_gender'] = value;
        _systemFolders['kidneys']?['user_gender'] = value;
        _systemFolders['cvs']?['user_gender'] = value;
        break;

      case 'age':
        break;

      case 'height':
      case 'weight':
        _systemFolders['endocrine']?['physical_$cardId'] = value;
        _systemFolders['cvs']?['load_$cardId'] = value;
        _systemFolders['musculoskeletal']?['axial_load_$cardId'] = value;
        break;

      case 'nicotine_status':
      case 'nicotine_years':
      case 'nicotine_freq':
        _systemFolders['respiratory']?['smoking_$cardId'] = value;
        _systemFolders['cvs']?['vessel_$cardId'] = value;
        _systemFolders['immune']?['broncho_local_imm_disruption'] = true;
        break;

      case 'alcohol_status':
      case 'alcohol_type':
        _systemFolders['gi_tract']?['liver_tox_load'] = "Alcohol consumption recorded";
        _systemFolders['nervous']?['gaba_neuro_load'] = "Ethanol-induced GABA modulation";

        if (valueString.contains('пиво') || valueString.contains('beer')) {
          _systemFolders['kidneys']?['uric_acid_risk'] = true;
          _systemFolders['endocrine']?['visceral_adipose_trigger'] = true;
        }
        if (valueString.contains('крепкое') || valueString.contains('вино') || valueString.contains('vodka')) {
          _systemFolders['cvs']?['cardiomyopathy_risk'] = true;
        }
        break;

      case 'spec_factors':
        if (valueString.contains('стимуляция') || valueString.contains('кофеин') || valueString.contains('предтреник')) {
          _systemFolders['nervous']?['synaptic_overdrive'] = "Stimulation (caffeine, pre-workouts)";
          _systemFolders['cvs']?['vasoconstriction_tachy_response'] = "Vasoconstrictor potential";
          _systemFolders['gi_tract']?['mucosal_ischemia_hyperacidity'] = "Risk of hyperacidity / mucosal ischemia";
        }
        if (valueString.contains('седация') || valueString.contains('антидепрессант')) {
          _systemFolders['nervous']?['cns_stabilization_mod'] = "Pharmacological sedation (BP)";
        }
        if (valueString.contains('ингаляции') || valueString.contains('вейп')) {
          _systemFolders['respiratory']?['alveolar_stress'] = "Aerosol / respiratory alveolar stress";
          _systemFolders['nervous']?['heb_psych_effect'] = "Active inhalation components";
        }
        if (valueString.contains('восстановление')) {
          _systemFolders['nervous']?['receptor_downregulation_history'] = "Receptor apparatus recovery period";
        }
        break;

      case 'sleep_quality':
        _systemFolders['endocrine']?['circadian_desynchronosis'] = "Circadian desynchronosis (Sleep quality: $value)";
        _systemFolders['nervous']?['glymphatic_clearance_disruption'] = (value == 'Бессонница' || value == 'Сбит режим');
        break;

      case 'diet_types':
        if (valueString.contains('мясо') || valueString.contains('meat')) {
          _systemFolders['gi_tract']?['protease_load'] = "High protease demand (meat diet)";
          _systemFolders['kidneys']?['nitrogen_waste_load'] = "Increased nitrogen waste load";
        }
        if (valueString.contains('овощи') || valueString.contains('vegetables')) {
          _systemFolders['gi_tract']?['microbiome_substrate'] = "Sufficient dietary fiber";
        }
        if (valueString.contains('фастфуд') || valueString.contains('fastfood')) {
          _systemFolders['endocrine']?['insulin_spike_risk'] = "High glycemic load index (fast food)";
          _systemFolders['cvs']?['endothelial_lipid_damage'] = "Atherogenic dietary profile (trans fats)";
        }
        if (valueString.contains('рыба') || valueString.contains('fish')) {
          _systemFolders['cvs']?['omega3_cardioprotection'] = "Omega-3 protection active";
        }
        if (valueString.contains('молочн') || valueString.contains('milk')) {
          _systemFolders['gi_tract']?['lactase_check'] = "Consumption of lactose-containing products";
          _systemFolders['musculoskeletal']?['minor_calcium_source'] = "Dietary calcium source in the diet";
        }
        if (valueString.contains('vegan') || valueString.contains('веган')) {
          _systemFolders['immune']?['leukopoiesis_deficiency_risk'] = true;
          _systemFolders['cvs']?['hyperhomocysteinemia_endothelial_destruction'] = true;
        }
        break;

      case 'diet_habits':
        if (valueString.contains('сладкое') || valueString.contains('sugar')) {
          _systemFolders['endocrine']?['carb_metabolism_glycation'] = "Excess simple carbohydrates (glycation risk)";
        }
        if (valueString.contains('солен') || valueString.contains('salt')) {
          _systemFolders['cvs']?['volemic_hypertension_load'] = "Sodium-dependent volemic load";
          _systemFolders['kidneys']?['tubular_osmotic_load'] = "Increased tubular osmotic load";
        }
        if (valueString.contains('кофе') || valueString.contains('coffee')) {
          _systemFolders['nervous']?['adenosine_receptor_blockade'] = "Chronic adenosine receptor blockade";
          _systemFolders['cvs']?['arrhythmogenic_potential_bp_fluctuation'] = "Catecholamine blood pressure shift (caffeine)";
          _systemFolders['gi_tract']?['gastric_acid_stimulation'] = "Stimulation of HCl secretion by parietal cells";
        }
        if (valueString.contains('дожор') || valueString.contains('night')) {
          _systemFolders['gi_tract']?['nocturnal_reflux_risk'] = "Risk of nocturnal reflux";
          _systemFolders['endocrine']?['nocturnal_sth_blockade'] = "Risk of suppressing nocturnal GH (STH) secretion";
        }
        if (valueString.contains('мало воды') || valueString.contains('water')) {
          _systemFolders['kidneys']?['hypersthenuria_urolithiasis_risk'] = "Risk of hypersthenuria and urolithiasis";
          _systemFolders['cvs']?['hemoconcentration_viscosity_load'] = "Hemoconcentration syndrome (circulating blood volume deficit)";
        }
        break;

      case 'gi_signals':
        _systemFolders['gi_tract']?['symptoms_motility_sibr'] = "Dyspepsia symptoms / GI motility disorders recorded";
        break;

      case 'work_type':
      case 'work_schedule':
      case 'sport_level':
        _systemFolders['musculoskeletal']?['biomechanics_$cardId'] = "Biomechanics specificity: $value";
        _systemFolders['cvs']?['cardio_adaptation_index'] = "Physiological adaptation level: $value";
        if (valueString.contains('смен') || valueString.contains('ноч') || valueString.contains('без выходных')) {
          _systemFolders['nervous']?['sympathicotonia_stress'] = "Chronic sympathicotonia (stress-induced)";
          _systemFolders['endocrine']?['adrenal_distress_cortisol'] = "Signs of adrenal distress (cortisol shift)";
        }
        break;

      case 'chronic_diseases':
        if (valueString.contains('диабет') || valueString.contains('diabetes')) {
          _systemFolders['endocrine']?['primary_defect_db'] = "History of diabetes mellitus";
          _systemFolders['cvs']?['macroangiopathy_accelerated_atherosclerosis'] = "Accelerated atherosclerosis (diabetic macroangiopathy)";
          _systemFolders['kidneys']?['diabetic_nephropathy_hyperfiltration'] = "Risk of diabetic nephropathy / hyperfiltration";
        }
        if (valueString.contains('давлен') || valueString.contains('гипертенз') || valueString.contains('сердце')) {
          _systemFolders['cvs']?['chronic_cvs_pathology'] = "Arterial hypertension / Chronic cardiovascular pathology";
        }
        if (valueString.contains('астма') || valueString.contains('аллерг')) {
          _systemFolders['respiratory']?['bronchial_hyperreactivity'] = "Bronchial hyperreactivity";
          _systemFolders['immune']?['th2_atopic_shif_ige'] = "Atopic immune formula shift (Th2-profile)";
        }
        if (valueString.contains('сустав') || valueString.contains('спина') || valueString.contains('позвоночник')) {
          _systemFolders['musculoskeletal']?['chronic_degradation_inflammation'] = "Chronic degenerative-inflammatory musculoskeletal lesion";
        }
        if (valueString.contains('невролог') || valueString.contains('эпилепси')) {
          _systemFolders['nervous']?['chronic_cns_pns_pathology'] = "Organic or functional CNS/PNS pathology";
        }
        break;

      case 'onco_status':
        _systemFolders['immune']?['antitumor_surveillance_deficit'] = "Increased oncological alertness";
        break;

      case 'recent_illness':
        _systemFolders['immune']?['postviral_exhaustion_formula_shift'] = "Post-viral asthenia syndrome / leukogram shift";
        _systemFolders['respiratory']?['residual_lung_markers'] = "Residual lung tissue changes";
        if (valueString.contains('отравление') || valueString.contains('кишечная')) {
          _systemFolders['gi_tract']?['mucosal_barrier_destruction_malabsorption'] = "Destruction of the small intestine mucosal barrier";
          _systemFolders['kidneys']?['prerenal_dehydration_risk'] = true;
        }
        break;

      case 'critical_infections':
        if (valueString.contains('гепатит') || valueString.contains('hepatitis')) {
          _systemFolders['gi_tract']?['hepatocyte_cytolysis_fibrosis'] = "Chronic viral hepatitis (risk of liver fibrosis)";
          _systemFolders['immune']?['chronic_viral_persistence_hep'] = "Prolonged viral persistence";
        }
        if (valueString.contains('вич') || valueString.contains('hiv')) {
          _systemFolders['immune']?['cd4_destruction_immunodeficiency'] = "HIV-positive status (risk of T-cell deficiency)";
        }
        if (valueString.contains('туберкулез') || valueString.contains('tbc')) {
          _systemFolders['respiratory']?['tbc_lung_lesions'] = "History of specific pulmonary tuberculosis";
          _systemFolders['immune']?['specific_cellular_response'] = "Activation of specific cellular response";
        }
        if (valueString.contains('сифилис') || valueString.contains('syphilis')) {
          _systemFolders['immune']?['specific_treponema_antibodies'] = "Specific antibodies to Treponema pallidum";
          _systemFolders['nervous']?['neurosyphilis_organic_risk'] = true;
          _systemFolders['cvs']?['mesoaortitis_vascular_risk'] = true;
          _systemFolders['kidneys']?['membranous_nephropathy_immune_complex'] = true;
        }
        break;

      case 'heredity_risks':
        if (valueString.contains('инфаркт') || valueString.contains('инсульт')) {
          _systemFolders['cvs']?['genetic_dyslipidemia_atherosclerosis'] = "Burdened family history of CVD (early myocardial infarctions/strokes)";
        }
        if (valueString.contains('онко') || valueString.contains('рак')) {
          _systemFolders['immune']?['genetic_tumor_surveillance_weakness'] = "Family oncological history";
        }
        if (valueString.contains('диабет')) {
          _systemFolders['endocrine']?['polygenic_diabetes_predisposition'] = "Polygenic predisposition to carbohydrate metabolism disorders";
        }
        if (valueString.contains('альцгеймер') || valueString.contains('деменци')) {
          _systemFolders['nervous']?['tau_amyloidosis_genetic_risk'] = "Genetic risk of early neurodegeneration";
        }
        break;

      case 'medications':
        if (valueString.contains('кок') || valueString.contains('гормон')) {
          _systemFolders['endocrine']?['exogenous_hormonal_background'] = "Exogenous steroid intake / hormone therapy";
          _systemFolders['cvs']?['estrogen_hypercoagulation_thrombosis_risk'] = "Estrogen-induced hypercoagulability (risk of thrombosis)";
        }
        if (valueString.contains('статин')) {
          _systemFolders['cvs']?['lipid_management_pharmacology'] = "Pharmacological lipid profile control (statins)";
          _systemFolders['gi_tract']?['statin_liver_transaminase_control'] = "Transaminase monitoring (risk of drug-induced hepatitis)";
        }
        if (valueString.contains('нпвс') || valueString.contains('обезбол') || valueString.contains('ибупрофен') || valueString.contains('аспирин')) {
          _systemFolders['gi_tract']?['cox1_inhibition_gastropathy_erosion'] = "NSAID-induced gastropathy (COX-1 blockade)";
          _systemFolders['kidneys']?['prostaglandin_block_afferent_arteriole_spasm'] = "Afferent arteriole spasm (risk of GFR decrease)";
          _systemFolders['cvs']?['nsaid_hypertension_thrombotic_shift'] = "Prohypertensive effect of NSAIDs / sodium retention";
        }
        break;
    }
  }

// ==========================================
// CHIEF PHYSICIAN THRESHOLD METHODS
// ==========================================

  /// Saving individual vital sign thresholds from the Chief Physician
  void saveVitalsThresholds(Map<String, dynamic> thresholdsJson) {
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['vitals_thresholds'] = thresholdsJson;
    _mirrorToNotesExport('vitals_thresholds', thresholdsJson);
    _persist();
    notifyListeners();
    debugPrint("✅ [VaultService]: Chief Physician vital sign thresholds saved!");
  }

  /// Retrieving established thresholds (returns null if the Chief Physician hasn't issued a verdict yet)
  VitalsThresholds? getVitalsThresholds() {
    final rawData = _systemFolders['global']?['vitals_thresholds'];
    if (rawData == null || rawData is! Map<String, dynamic>) {
      return null;
    }

    try {
      return VitalsThresholds.fromJson(rawData);
    } catch (e) {
      debugPrint("❌ [VaultService Error]: VitalsThresholds parsing error: $e");
      return null;
    }
  }

// METHOD FOR GEMINI
  void updateSystemFolder(String systemKey, Map<String, dynamic> data) {
    if (_systemFolders.containsKey(systemKey)) {
      _systemFolders[systemKey]!.addAll(data);
      _mirrorToNotesExport('system_update_$systemKey', data);
      _persist();
      notifyListeners();
    }
  }

// Progress calculation
  void calculateProgress(String key) {
    var markers = _data['health_systems'][key]['markers'] as Map;
    if (markers.isEmpty) return;
    int filled = markers.values.where((v) => v != 0 && v != null).length;
    _data['health_systems'][key]['completeness'] = (filled / markers.length) * 100;
  }

// Method for automatic injection of lab markers
  void injectLabMarker(String systemKey, String markerKey, double value, String unit) {
    final systems = _data['health_systems'] as Map<String, dynamic>;
    if (systems.containsKey(systemKey)) {
      final markers = systems[systemKey]['markers'] as Map<String, dynamic>;
      if (markers.containsKey(markerKey)) {
        markers[markerKey]['value'] = value;
        markers[markerKey]['unit'] = unit;
        markers[markerKey]['date'] = DateTime.now().toIso8601String().substring(0, 10);
      }
    }

    if (_systemFolders.containsKey(systemKey)) {
      final markerData = {
        'value': value,
        'unit': unit,
        'timestamp': DateTime.now().toIso8601String(),
      };
      _systemFolders[systemKey]!['lab_$markerKey'] = markerData;
      _mirrorToNotesExport('lab_${systemKey}_$markerKey', markerData);
    }
    notifyListeners();
  }

  /// Records a confirmed electrocardiogram Vision intake result into the patient vault.
  /// Preserves previous ECG history records without overwriting past versions.
  /// Returns `true` only after local secure persistence is fully confirmed.
  /// Returns `false` if local secure persistence fails.
  Future<bool> recordEcgIntake({
    required EcgVisionIntakeClientResponse response,
    String? recordDate,
    String? localFilename,
  }) async {
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});

    final Map<String, dynamic> smartWatch = markers.containsKey('smart_watch') && markers['smart_watch'] != null
        ? Map<String, dynamic>.from(markers['smart_watch'])
        : <String, dynamic>{};

    final qualityLabel = response.isUnreadable
        ? 'Unreadable'
        : (response.isPoorQuality ? 'Poor quality' : 'Accepted');
    final statusSummary = 'ECG $qualityLabel (v${response.intakeVersion})';

    smartWatch['ecg_status'] = <String, dynamic>{
      "value": statusSummary,
      "date": recordDate ?? DateTime.now().toIso8601String().split('T')[0],
      "intake_version": response.intakeVersion,
      "recording_type": response.recordingType,
      "recording_quality": response.recordingQuality,
      "created_at_utc": response.createdAtUtc,
      "primary_consilium_concluded": response.primaryConsiliumConcluded,
      ...? (localFilename != null ? {"local_path": localFilename} : null),
    };

    // Append to versioned history (ecg_records)
    final List<dynamic> existingHistory = smartWatch['ecg_records'] is List
        ? List<dynamic>.from(smartWatch['ecg_records'])
        : <dynamic>[];

    final newEntry = {
      'version': response.intakeVersion,
      'recording_type': response.recordingType,
      'recording_quality': response.recordingQuality,
      'created_at_utc': response.createdAtUtc,
      ...? (recordDate != null ? {'record_date': recordDate} : null),
      ...? (localFilename != null ? {'local_filename': localFilename} : null),
      'status': response.status,
    };

    final existingIndex = existingHistory.indexWhere(
      (e) => e is Map && e['version'] == response.intakeVersion,
    );
    if (existingIndex >= 0) {
      existingHistory[existingIndex] = newEntry;
    } else {
      existingHistory.add(newEntry);
    }
    smartWatch['ecg_records'] = existingHistory;

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _mirrorToNotesExport('vitals_smart_watch', smartWatch);

    try {
      await _persist();
      await flushPendingWrites(patientId: _activePatientId);
      if (hasPersistenceError) {
        return false;
      }
    } catch (e) {
      debugPrint('[VaultService] Failed to persist ECG intake to storage: $e');
      return false;
    }

    notifyListeners();
    return true;
  }

  /// Returns versioned history of recorded ECG intake documents.
  List<Map<String, dynamic>> get ecgRecords {
    final sw = _smartWatchData;
    final records = sw['ecg_records'];
    if (records is List) {
      return records.whereType<Map<String, dynamic>>().toList();
    }
    return [];
  }

  /// Returns latest recorded ECG status metadata or null if not yet uploaded.
  Map<String, dynamic>? get latestEcgRecord {
    final sw = _smartWatchData;
    final status = sw['ecg_status'];
    if (status is Map<String, dynamic>) {
      return status;
    }
    return null;
  }

  // =========================================================================
  // PRIMARY CONSILIUM CLIENT REPORTS (Stage 5.4E-2)
  // =========================================================================

  /// Whether Primary Consilium has been completed on the server and synced to vault.
  bool get isConsiliumCompleted => _isConsiliumCompleted;

  /// Identifier of the medical cycle for which consilium was generated.
  String? get consiliumCycleId => _consiliumCycleId;

  /// Cached client-facing overall report (`user_overall_report`).
  ClientModelReportResponse? get cachedOverallReport => _clientOverallReport;

  /// Read-only map of system reports (`system_user_report_<systemKey>`).
  Map<String, ClientModelReportResponse> get cachedSystemReports =>
      Map.unmodifiable(_clientSystemReports);

  /// Retrieves client-facing report for a specific organ system (e.g. 'cvs', 'gi_tract').
  ClientModelReportResponse? getClientReportForSystem(String systemKey) =>
      _clientSystemReports[systemKey];

  /// Read-only map of historical consilium conclusions by cycleId.
  Map<String, Map<String, dynamic>> get consiliumHistory =>
      Map.unmodifiable(_consiliumHistory);

  /// Retrieves historical consilium snapshot for a past medical cycle if available.
  Map<String, dynamic>? getHistoricalConsilium(String cycleId) =>
      _consiliumHistory[cycleId];

  /// Retrieves client-facing Weekly Consilium report for a specific organ system.
  /// If [sprintId] is omitted or matches the active sprint, returns from active weekly reports.
  /// If a specific [sprintId] is requested, looks up historical reports for that sprint.
  /// Never falls back to a different sprint's data.
  ClientModelReportResponse? getWeeklyReportForSystem(String systemKey, {String? sprintId}) {
    if (sprintId == null || sprintId == _activeWeeklySprintId) {
      return _activeWeeklySystemReports[systemKey];
    }
    final hist = _weeklyConsiliumHistory[sprintId];
    if (hist != null && hist['system_reports'] is Map) {
      final sysMap = hist['system_reports'] as Map;
      final raw = sysMap[systemKey];
      if (raw is Map) {
        try {
          return ClientModelReportResponse.fromJson(Map<String, dynamic>.from(raw));
        } catch (_) {}
      }
    }
    return null;
  }

  /// Retrieves historical weekly consilium snapshot for a past sprint if available.
  Map<String, dynamic>? getHistoricalWeeklyConsilium(String sprintId) =>
      _weeklyConsiliumHistory[sprintId];

  /// Atomically records published client-facing Weekly Consilium reports in local vault.
  /// Enforces epoch check and patient attribution fencing.
  /// Strictly requires weekly overall report and all 8 system reports.
  /// If weekly reports are incomplete, rejects persistence and NEVER substitutes data from prior weeks.
  /// Isolates weekly sprints from Primary Consilium and from other weekly sprints.
  /// Returns true only after local secure persistence is fully confirmed.
  Future<bool> recordWeeklyClientReports({
    required String cycleId,
    required String sprintId,
    required int sprintNumber,
    required String reportDate,
    required ClientModelReportResponse overallReport,
    required Map<String, ClientModelReportResponse> systemReports,
    String? expectedPatientId,
  }) async {
    if (_isDisposed) return false;

    // 1. Patient fencing: verify attribution if provided or active
    final activeId = _activePatientId;
    if (expectedPatientId != null && expectedPatientId != activeId) {
      debugPrint('[VaultService] Patient attribution mismatch when recording weekly reports. Discarding.');
      return false;
    }

    final int capturedEpoch = _sessionEpoch;

    // 2. Strict completeness validation:
    // Requires overall client-facing report
    if (overallReport.privacyLevel != 'client_facing') {
      debugPrint('[VaultService] Security violation: non-client_facing weekly overall report.');
      return false;
    }

    // Require strictly: overallReport.sprintId == sprintId (null sprintId rejected)
    if (overallReport.sprintId != sprintId) {
      debugPrint(
        '[VaultService] Weekly overall report sprint mismatch: report has "${overallReport.sprintId}", expected "$sprintId". Null or mismatched sprintId rejected.',
      );
      return false;
    }

    // Must contain exactly all 8 system reports; do not accept partial or substitute previous week
    const requiredSystems = [
      'cvs',
      'gi_tract',
      'respiratory',
      'nervous',
      'musculoskeletal',
      'kidneys',
      'immune',
      'endocrine',
    ];

    if (systemReports.length != 8) {
      debugPrint(
        '[VaultService] Incomplete weekly reports rejected: received ${systemReports.length} systems, expected 8.',
      );
      return false;
    }

    for (final sysKey in requiredSystems) {
      final rep = systemReports[sysKey];
      if (rep == null || rep.privacyLevel != 'client_facing') {
        debugPrint(
          '[VaultService] Missing or non-client_facing report for system "$sysKey". Weekly reports rejected.',
        );
        return false;
      }
      // Require strictly: rep.sprintId == sprintId for all 8 reports (null sprintId rejected)
      if (rep.sprintId != sprintId) {
        debugPrint(
          '[VaultService] System report for "$sysKey" has sprintId "${rep.sprintId}", expected "$sprintId". Null or cross-sprint sprintId strictly forbidden.',
        );
        return false;
      }
    }

    // 3. Preserve history: archive previous active sprint if different
    if (_activeWeeklySprintId != null &&
        _activeWeeklySprintId != sprintId &&
        _activeWeeklyOverallReport != null) {
      _weeklyConsiliumHistory[_activeWeeklySprintId!] = {
        'cycle_id': cycleId,
        'sprint_id': _activeWeeklySprintId,
        'sprint_number': _activeWeeklySprintNumber,
        'report_date': _activeWeeklyReportDate,
        'overall_report': _activeWeeklyOverallReport!.toJson(),
        'system_reports': _activeWeeklySystemReports.map((k, v) => MapEntry(k, v.toJson())),
        'archived_at': DateTime.now().toUtc().toIso8601String(),
      };
    }

    // 4. Update in-memory state: isolate new sprint
    _activeWeeklySprintId = sprintId;
    _activeWeeklySprintNumber = sprintNumber;
    _activeWeeklyReportDate = reportDate;
    _activeWeeklyOverallReport = overallReport;
    _activeWeeklySystemReports.clear();
    _activeWeeklySystemReports.addAll(systemReports);

    // Record new sprint into history as well
    _weeklyConsiliumHistory[sprintId] = {
      'cycle_id': cycleId,
      'sprint_id': sprintId,
      'sprint_number': sprintNumber,
      'report_date': reportDate,
      'overall_report': overallReport.toJson(),
      'system_reports': systemReports.map((k, v) => MapEntry(k, v.toJson())),
      'recorded_at': DateTime.now().toUtc().toIso8601String(),
    };

    // 5. Epoch fencing check before persistence
    if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
      return false;
    }

    // 6. Persist to storage with post-persist fence
    try {
      await _persist();
      if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
        return false;
      }
      if (activeId != null) {
        await flushPendingWrites(patientId: activeId);
        if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
          return false;
        }
      }
      notifyListeners();
      return true;
    } catch (e) {
      debugPrint('[VaultService] Failed to securely persist weekly client reports: $e');
      notifyListeners();
      return false;
    }
  }

  /// Atomically records published client-facing Primary Consilium reports in local vault.
  /// Enforces epoch check and patient attribution fencing.
  /// Requires user_overall_report to mark consilium completed.
  /// Archives previous cycle consilium and cleans old active shelves to avoid cross-cycle mixing.
  /// Returns 	rue only after local secure persistence is fully confirmed.
  Future<bool> recordClientReports({
    required String cycleId,
    required List<ClientModelReportResponse> reports,
    String? expectedPatientId,
  }) async {
    if (_isDisposed) return false;

    // 1. Patient fencing: verify attribution if provided or active
    final activeId = _activePatientId;
    if (expectedPatientId != null && expectedPatientId != activeId) {
      debugPrint('[VaultService] Patient attribution mismatch when recording client reports. Discarding.');
      return false;
    }

    final int capturedEpoch = _sessionEpoch;

    // 2. Parse and assign reports
    ClientModelReportResponse? overall;
    final Map<String, ClientModelReportResponse> systems = {};

    for (final rep in reports) {
      if (rep.privacyLevel != 'client_facing') {
        debugPrint('[VaultService] Security violation: non-client_facing report ignored: ');
        continue;
      }

      if (rep.reportType == 'user_overall_report') {
        overall = rep;
      } else if (rep.reportType.startsWith('system_user_report_')) {
        final sysKey = rep.reportType.substring('system_user_report_'.length);
        systems[sysKey] = rep;
      } else if (rep.content.systemKey != null && rep.content.systemKey!.isNotEmpty) {
        systems[rep.content.systemKey!] = rep;
      }
    }

    // Require user_overall_report: consilium cannot be marked completed without overall synthesis
    if (overall == null) {
      debugPrint('[VaultService] Cannot record consilium: missing required user_overall_report.');
      return false;
    }

    // 3. Preserve history: archive previous completed cycle if different
    if (_isConsiliumCompleted && _consiliumCycleId != null && _consiliumCycleId != cycleId && _clientOverallReport != null) {
      _consiliumHistory[_consiliumCycleId!] = {
        'cycle_id': _consiliumCycleId,
        'is_completed': true,
        'overall_report': _clientOverallReport!.toJson(),
        'system_reports': _clientSystemReports.map((k, v) => MapEntry(k, v.toJson())),
        'archived_at': DateTime.now().toUtc().toIso8601String(),
      };
    }

    // 4. Update in-memory state: isolate new cycle and clear previous active shelves
    _clientSystemReports.clear();
    _clientOverallReport = overall;
    _isConsiliumCompleted = true;
    _consiliumCycleId = cycleId;

    // Clean all 8 organ shelves and core cascade to prevent stale remnants of previous cycles
    for (final sysKey in _systemFolders.keys) {
      if (sysKey != 'notes_export' && sysKey != 'global' && sysKey != 'core_cascade') {
        _systemFolders[sysKey]?.remove('system_user_report');
        _systemFolders[sysKey]?['system_general_shelf'] = null;
      }
    }

    _systemFolders['core_cascade']?['pro_general_shelf'] = overall.content.summary;
    _systemFolders['core_cascade']?['flash_general_shelf'] = overall.content.summary;

    if (overall.content.metrics.isNotEmpty) {
      _systemFolders['global']?['vitals_thresholds'] = overall.content.metrics;
    }

    for (final entry in systems.entries) {
      final key = entry.key;
      final rep = entry.value;
      _clientSystemReports[key] = rep;
      if (_systemFolders.containsKey(key)) {
        _systemFolders[key]!['system_general_shelf'] = rep.content.summary;
        _systemFolders[key]!['system_user_report'] = rep.toJson();
      }
    }

    // Record new cycle to history as well
    _consiliumHistory[cycleId] = {
      'cycle_id': cycleId,
      'is_completed': true,
      'overall_report': overall.toJson(),
      'system_reports': systems.map((k, v) => MapEntry(k, v.toJson())),
      'recorded_at': DateTime.now().toUtc().toIso8601String(),
    };

    // 5. Epoch fencing check before persistence
    if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
      return false;
    }

    // 6. Persist to storage with post-persist fence
    try {
      await _persist();
      if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
        return false;
      }
      if (activeId != null) {
        await flushPendingWrites(patientId: activeId);
        if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
          return false;
        }
      }
      notifyListeners();
      return true;
    } catch (e) {
      debugPrint('[VaultService] Failed to securely persist client reports: $e');
      notifyListeners();
      return false;
    }
  }

  /// Safely transitions the active consilium view to pending state for a cycle.
  /// Archives any previous completed consilium into history to prevent mixing cycles
  /// or showing stale conclusions for a pending cycle.
  Future<bool> markConsiliumPending({
    required String cycleId,
    String? expectedPatientId,
  }) async {
    if (_isDisposed) return false;

    final activeId = _activePatientId;
    if (expectedPatientId != null && expectedPatientId != activeId) {
      debugPrint('[VaultService] Patient attribution mismatch when marking consilium pending. Discarding.');
      return false;
    }

    final int capturedEpoch = _sessionEpoch;

    // Archive previous completed consilium if present
    if (_isConsiliumCompleted && _consiliumCycleId != null && _clientOverallReport != null) {
      _consiliumHistory[_consiliumCycleId!] = {
        'cycle_id': _consiliumCycleId,
        'is_completed': true,
        'overall_report': _clientOverallReport!.toJson(),
        'system_reports': _clientSystemReports.map((k, v) => MapEntry(k, v.toJson())),
        'archived_at': DateTime.now().toUtc().toIso8601String(),
      };
    }

    _isConsiliumCompleted = false;
    _consiliumCycleId = cycleId.isNotEmpty ? cycleId : null;
    _clientOverallReport = null;
    _clientSystemReports.clear();

    // Reset active shelves
    for (final sysKey in _systemFolders.keys) {
      if (sysKey != 'notes_export' && sysKey != 'global' && sysKey != 'core_cascade') {
        _systemFolders[sysKey]?.remove('system_user_report');
        _systemFolders[sysKey]?['system_general_shelf'] = null;
      }
    }
    _systemFolders['core_cascade']?['pro_general_shelf'] = null;
    _systemFolders['core_cascade']?['flash_general_shelf'] = null;

    if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
      return false;
    }

    try {
      await _persist();
      if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
        return false;
      }
      if (activeId != null) {
        await flushPendingWrites(patientId: activeId);
        if (_isDisposed || _sessionEpoch != capturedEpoch || _activePatientId != activeId) {
          return false;
        }
      }
      notifyListeners();
      return true;
    } catch (e) {
      debugPrint('[VaultService] Failed to securely persist pending consilium state: $e');
      notifyListeners();
      return false;
    }
  }

  // Direct saving method for vital parameters
  void updateSmartWatchMetrics({
    int? systolic,
    int? diastolic,
    int? pulse,
    int? spo2,
    String? ecgSummary,
  }) {
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});

    final Map<String, dynamic> smartWatch = markers.containsKey('smart_watch') && markers['smart_watch'] != null
        ? Map<String, dynamic>.from(markers['smart_watch'])
        : <String, dynamic>{};

    final String currentDate = DateTime.now().toIso8601String().split('T')[0];

    if (systolic != null && diastolic != null) {
      smartWatch['pressure'] = <String, dynamic>{
        "systolic": systolic,
        "diastolic": diastolic,
        "date": currentDate,
      };
    }

    if (pulse != null) {
      smartWatch['pulse'] = <String, dynamic>{
        "value": pulse,
        "date": currentDate,
        "unit": "bpm",
      };
    }

    if (spo2 != null) {
      smartWatch['spo2'] = <String, dynamic>{
        "value": spo2,
        "date": currentDate,
        "unit": "%",
      };
    }

    if (ecgSummary != null) {
      smartWatch['ecg_status'] = <String, dynamic>{
        "value": ecgSummary,
        "date": currentDate,
      };
    }

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _mirrorToNotesExport('vitals_smart_watch', smartWatch);
    _persist();
    notifyListeners();
  }

  // ==========================================
  // SMART WATCH WORKOUT STORAGE (CVS MARKERS)
  // Section: health_systems -> cvs -> markers -> smart_watch
  // ==========================================

  void _ensureSmartWatchStructure() {
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});

    smartWatch.putIfAbsent('workout_sessions', () => <Map<String, dynamic>>[]);
    smartWatch.putIfAbsent('heart_rate_samples', () => <Map<String, dynamic>>[]);
    smartWatch.putIfAbsent('workout_context', () => <String, dynamic>{});
    smartWatch.putIfAbsent('workout_analysis_reports', () => <Map<String, dynamic>>[]);

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;
  }

  Map<String, dynamic> getSmartWatchData() {
    _ensureSmartWatchStructure();
    return _smartWatchData;
  }

  List<Map<String, dynamic>> getWorkoutSessions() {
    _ensureSmartWatchStructure();
    final sw = _smartWatchData;
    final list = sw['workout_sessions'] as List<dynamic>? ?? [];
    return list.map((e) => Map<String, dynamic>.from(e as Map)).toList();
  }

  Map<String, dynamic>? getWorkoutSession(String sessionId) {
    final sessions = getWorkoutSessions();
    for (var s in sessions) {
      if (s['session_id'] == sessionId) {
        return s;
      }
    }
    return null;
  }

  List<Map<String, dynamic>> getHeartRateSamples({String? sessionId}) {
    _ensureSmartWatchStructure();
    final sw = _smartWatchData;
    final list = sw['heart_rate_samples'] as List<dynamic>? ?? [];
    final mapped = list.map((e) => Map<String, dynamic>.from(e as Map));
    if (sessionId != null) {
      return mapped.where((e) => e['session_id'] == sessionId).toList();
    }
    return mapped.toList();
  }

  Map<String, dynamic> getWorkoutContext() {
    _ensureSmartWatchStructure();
    final sw = _smartWatchData;
    return Map<String, dynamic>.from(sw['workout_context'] as Map<String, dynamic>? ?? {});
  }

  List<Map<String, dynamic>> getWorkoutAnalysisReports({String? sessionId}) {
    _ensureSmartWatchStructure();
    final sw = _smartWatchData;
    final list = sw['workout_analysis_reports'] as List<dynamic>? ?? [];
    final mapped = list.map((e) => Map<String, dynamic>.from(e as Map));
    if (sessionId != null) {
      return mapped.where((e) => e['session_id'] == sessionId).toList();
    }
    return mapped.toList();
  }

  Map<String, dynamic>? getLatestWorkoutReport(String sessionId) {
    final reports = getWorkoutAnalysisReports(sessionId: sessionId);
    if (reports.isEmpty) return null;
    reports.sort((a, b) => ((b['version'] as num?) ?? 0).compareTo((a['version'] as num?) ?? 0));
    return reports.first;
  }

  void createWorkoutSession({
    required String sessionId,
    required DateTime anomalyDetectedAt,
    required DateTime activityConfirmedAt,
    int? baselinePulse,
    int? peakPulse,
    String? triggerReason,
    Map<String, dynamic>? doctorThresholds,
    DateTime? initialTimestamp,
    int? initialPulse,
    String? initialPhase,
    String initialSource = 'watch_sensor',
    bool initialIsSimulated = false,
    String initialNote = '',
  }) {
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();

    final sessionContext = <String, dynamic>{
      'baseline_pulse': baselinePulse,
      'peak_pulse': peakPulse,
      'trigger_reason': triggerReason,
      'doctor_thresholds': doctorThresholds ?? <String, dynamic>{},
      'activity_confirmed': true,
      'confirmed_at': activityConfirmedAt.toIso8601String(),
    };

    final newSession = <String, dynamic>{
      'session_id': sessionId,
      'anomaly_detected_at': anomalyDetectedAt.toIso8601String(),
      'activity_confirmed_at': activityConfirmedAt.toIso8601String(),
      'workout_completed_at': null,
      'physiological_end_time': null,
      'status': 'active',
      'is_data_collection_finished': false,
      'workout_context': sessionContext,
      'workout_samples': <Map<String, dynamic>>[],
      'recovery_samples': <Map<String, dynamic>>[],
      'missed_samples': <Map<String, dynamic>>[],
      'reports': <Map<String, dynamic>>[],
    };

    sessions.add(newSession);
    smartWatch['workout_sessions'] = sessions;
    smartWatch['workout_context'] = sessionContext;

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;
    debugPrint("💾 [createWorkoutSession] saved session count = ${sessions.length}, _data sessions = ${((_data['health_systems'] as Map)['cvs']['markers']['smart_watch']['workout_sessions'] as List).length}");

    if (initialTimestamp != null && initialPulse != null) {
      addHeartRateSample(
        sessionId: sessionId,
        timestamp: initialTimestamp,
        pulse: initialPulse,
        phase: initialPhase ?? 'workout',
        source: initialSource,
        isSimulated: initialIsSimulated,
        note: initialNote,
      );
    } else {
      _persist();
      notifyListeners();
    }
  }

  void addHeartRateSample({
    String? sessionId,
    required DateTime timestamp,
    required int pulse,
    required String phase,
    String source = 'watch_sensor',
    bool isSimulated = false,
    String note = '',
    DateTime? ingestedAt,
  }) {
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> samples = (smartWatch['heart_rate_samples'] as List<dynamic>? ?? []).toList();

    final sampleJson = <String, dynamic>{
      'timestamp': timestamp.toIso8601String(),
      'ingested_at': (ingestedAt ?? DateTime.now()).toIso8601String(),
      'pulse': pulse,
      'phase': phase,
      'source': source,
      'is_simulated': isSimulated,
      'note': note,
    };
    if (sessionId != null) {
      sampleJson['session_id'] = sessionId;
    }

    samples.add(sampleJson);
    smartWatch['heart_rate_samples'] = samples;

    if (sessionId != null) {
      final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();
      for (int i = 0; i < sessions.length; i++) {
        final s = Map<String, dynamic>.from(sessions[i] as Map);
        if (s['session_id'] == sessionId) {
          if (phase.startsWith('recovery')) {
            final recList = (s['recovery_samples'] as List<dynamic>? ?? []).toList();
            recList.add(sampleJson);
            s['recovery_samples'] = recList;
          } else {
            final wList = (s['workout_samples'] as List<dynamic>? ?? []).toList();
            wList.add(sampleJson);
            s['workout_samples'] = wList;
          }
          sessions[i] = s;
          break;
        }
      }
      smartWatch['workout_sessions'] = sessions;
    }

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _persist();
    notifyListeners();
  }

  void recordMissedSamples({
    required String sessionId,
    required DateTime from,
    required DateTime to,
    required int expectedIntervalMinutes,
    required int missedCount,
    String reason = 'time_jump_or_no_signal',
  }) {
    if (missedCount <= 0) return;
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();

    for (int i = 0; i < sessions.length; i++) {
      final s = Map<String, dynamic>.from(sessions[i] as Map);
      if (s['session_id'] == sessionId) {
        final missedList = (s['missed_samples'] as List<dynamic>? ?? []).toList();
        missedList.add(<String, dynamic>{
          'from': from.toIso8601String(),
          'to': to.toIso8601String(),
          'expected_interval_minutes': expectedIntervalMinutes,
          'missed_count': missedCount,
          'reason': reason,
        });
        s['missed_samples'] = missedList;
        sessions[i] = s;
        break;
      }
    }

    smartWatch['workout_sessions'] = sessions;
    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _persist();
    notifyListeners();
  }

  void completeWorkoutSession({
    required String sessionId,
    required DateTime workoutCompletedAt,
  }) {
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();

    for (int i = 0; i < sessions.length; i++) {
      final s = Map<String, dynamic>.from(sessions[i] as Map);
      if (s['session_id'] == sessionId) {
        s['workout_completed_at'] = workoutCompletedAt.toIso8601String();
        s['status'] = 'recovering';
        sessions[i] = s;
        break;
      }
    }

    smartWatch['workout_sessions'] = sessions;
    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _persist();
    notifyListeners();
  }

  void recordPhysiologicalEnd({
    required String sessionId,
    required DateTime physiologicalEndTime,
  }) {
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();

    for (int i = 0; i < sessions.length; i++) {
      final s = Map<String, dynamic>.from(sessions[i] as Map);
      if (s['session_id'] == sessionId) {
        s['physiological_end_time'] = physiologicalEndTime.toIso8601String();
        sessions[i] = s;
        break;
      }
    }

    smartWatch['workout_sessions'] = sessions;
    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _persist();
    notifyListeners();
  }

  void finishDataCollection({
    required String sessionId,
    String reason = 'recovery_period_elapsed',
  }) {
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();

    for (int i = 0; i < sessions.length; i++) {
      final s = Map<String, dynamic>.from(sessions[i] as Map);
      if (s['session_id'] == sessionId) {
        s['is_data_collection_finished'] = true;
        s['status'] = 'completed';
        s['completion_reason'] = reason;
        sessions[i] = s;
        break;
      }
    }

    smartWatch['workout_sessions'] = sessions;
    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _persist();
    notifyListeners();
  }

  void saveWorkoutReport({
    required String sessionId,
    Map<String, dynamic>? internalAnalysis,
    Map<String, dynamic>? userWorkoutReport,
    Map<String, dynamic>? athenaHandoff,
  }) {
    _ensureSmartWatchStructure();
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});
    final smartWatch = Map<String, dynamic>.from(markers['smart_watch'] ?? <String, dynamic>{});
    final List<dynamic> reports = (smartWatch['workout_analysis_reports'] as List<dynamic>? ?? []).toList();

    final existingCount = reports.where((r) => r is Map && r['session_id'] == sessionId).length;
    final int newVersion = existingCount + 1;

    final reportMap = <String, dynamic>{
      'report_id': 'rep_${sessionId}_v$newVersion',
      'session_id': sessionId,
      'version': newVersion,
      'created_at': DateTime.now().toIso8601String(),
      'internal_analysis': internalAnalysis,
      'user_workout_report': userWorkoutReport,
      'athena_handoff': athenaHandoff,
    };

    reports.add(reportMap);
    smartWatch['workout_analysis_reports'] = reports;

    final List<dynamic> sessions = (smartWatch['workout_sessions'] as List<dynamic>? ?? []).toList();
    for (int i = 0; i < sessions.length; i++) {
      final s = Map<String, dynamic>.from(sessions[i] as Map);
      if (s['session_id'] == sessionId) {
        final sessionReports = (s['reports'] as List<dynamic>? ?? []).toList();
        sessionReports.add(reportMap);
        s['reports'] = sessionReports;
        sessions[i] = s;
        break;
      }
    }
    smartWatch['workout_sessions'] = sessions;

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _persist();
    notifyListeners();
  }

// LABORATORIES
  LabLedgerStateResponse? _cachedLabLedgerState;

  /// Authoritative laboratory ledger state snapshot retrieved from the server.
  LabLedgerStateResponse? get cachedLabLedgerState => _cachedLabLedgerState;

  /// Complete list of laboratory observations across all historical studies.
  List<LabObservationEntry> get labObservationsHistory =>
      _cachedLabLedgerState?.observationsHistory ?? const [];

  /// Current active laboratory markers from the authoritative ledger.
  Map<String, LabObservationEntry> get activeLabMarkers =>
      _cachedLabLedgerState?.activeMarkers ?? const {};

  /// Updates the local Vault cache of the server-authoritative laboratory ledger state.
  /// Preserves patient isolation and applies session epoch fencing.
  /// Updates shared_labs_shelf with active markers for backward compatibility with local consumers (BrainService).
  void updateLabLedgerSnapshot(LabLedgerStateResponse ledgerState) {
    _cachedLabLedgerState = ledgerState;

    final sharedLabs = <String, dynamic>{};
    for (final entry in ledgerState.activeMarkers.entries) {
      sharedLabs[entry.key] = entry.value.toJson();
    }
    _systemFolders['global']!['shared_labs_shelf'] = sharedLabs;
    _systemFolders['global']!['lab_ledger_snapshot'] = ledgerState.toJson();
    _mirrorToNotesExport('shared_labs_shelf', sharedLabs);
    _persist();
    notifyListeners();
  }

  /// Safely updates the local Vault cache from a successful [LabVisionIntakeClientResponse]
  /// when fetching the full ledger state fails or is deferred.
  /// Preserves multi-study history by appending new confirmed markers to [labObservationsHistory].
  void updateLabLedgerFromIntakeResponse(LabVisionIntakeClientResponse intake) {
    final existingHistory = labObservationsHistory;
    final activeMap = Map<String, LabObservationEntry>.from(_cachedLabLedgerState?.activeMarkers ?? {});

    final newEntries = <LabObservationEntry>[];
    final nowIso = DateTime.now().toUtc().toIso8601String();
    for (final m in intake.confirmedMarkers) {
      final entry = LabObservationEntry(
        observationId: 'obs_ocr__',
        markerKey: m.markerKey,
        displayName: m.originalName,
        value: m.rawValue,
        numericValue: m.numericValue,
        unit: m.unit,
        sampleDate: m.sampleDate,
        referenceRange: m.referenceRange,
        eventType: 'initial',
        isActive: true,
        isCriticalFlag: m.isCriticalFlag,
        isUncertain: m.isUncertain,
        uncertaintyNote: m.uncertaintyNote,
        isRecognitionAmbiguous: m.isRecognitionAmbiguous,
        recordedAt: m.sampleDate ?? nowIso,
      );
      activeMap[m.markerKey] = entry;
      newEntries.add(entry);
    }

    final fallbackState = LabLedgerStateResponse(
      cycleId: intake.cycleId,
      ledgerVersion: intake.ledgerVersion,
      lastEvaluatedLedgerVersion: _cachedLabLedgerState?.lastEvaluatedLedgerVersion ?? 0,
      hasUnreviewedLabChanges: intake.hasUnreviewedLabChanges,
      evaluationStatus: 'pending',
      evaluationNotice: 'Extracted from OCR intake confirmation',
      hasCriticalFlags: intake.confirmedMarkers.any((m) => m.isCriticalFlag),
      safetyNotice: 'All parameters extracted from intake confirmation',
      activeMarkers: activeMap,
      observationsHistory: [...existingHistory, ...newEntries],
    );

    updateLabLedgerSnapshot(fallbackState);
  }

  void injectParsedLaboratories(Map<String, dynamic> parsedJson) {
    _systemFolders['global']!['shared_labs_shelf'] = parsedJson;
    _mirrorToNotesExport('shared_labs_shelf', parsedJson);
    notifyListeners();
  }

  Map<String, dynamic> getSharedLabs() {
    return Map<String, dynamic>.from(_systemFolders['global']?['shared_labs_shelf'] ?? <String, dynamic>{});
  }

// Distribute data across specific system shelves
  void updateSystemCascadeShelves({
    required String systemKey,
    required Map<String, dynamic> selectedLabs,
    required String generalOutput,
    required String trainingOutput,
  }) {
    if (_systemFolders.containsKey(systemKey)) {
      _systemFolders[systemKey]!['labs'] = selectedLabs;
      _systemFolders[systemKey]!['system_general_shelf'] = generalOutput;
      _systemFolders[systemKey]!['system_training_shelf'] = trainingOutput;

      _mirrorToNotesExport('cascade_system_$systemKey', {
        'labs': selectedLabs,
        'general': generalOutput,
        'training': trainingOutput,
      });

      notifyListeners();
    }
  }

// ==========================================
// MODEL CASCADE (FINAL VAULT LOGIC)
// ==========================================

// Recording Gemini Flash results (Deputy Chief Physician)
  void updateFlashShelves({required String general, required String training}) {
    if (_systemFolders.containsKey('core_cascade')) {
      _systemFolders['core_cascade']!['flash_general_shelf'] = general;
      _systemFolders['core_cascade']!['flash_training_shelf'] = training;

      _mirrorToNotesExport('cascade_flash', {
        'general': general,
        'training': training,
      });

      _persist();
      notifyListeners();
    }
  }
  // Recording Gemini Pro results (Chief Physician)
  void updateProShelves({required String general, required String training}) {
    if (_systemFolders.containsKey('core_cascade')) {
      _systemFolders['core_cascade']!['pro_general_shelf'] = general;
      _systemFolders['core_cascade']!['pro_training_shelf'] = training;
      _mirrorToNotesExport('cascade_pro', {'general': general, 'training': training});
      _persist();
      notifyListeners();
    }
  }

  // Assembly of basic context for the master
  String getAggregatedContextForMaster() {
    final StringBuffer buffer = StringBuffer();
    buffer.writeln("=== ANAMNESIS AND INPUT DATA ===");
    final globalData = getSystemContext('global');
    buffer.writeln(globalData.toString());
    buffer.writeln();
    buffer.writeln("=== VITAL MARKERS (SMART WATCH / LIVE) ===");
    buffer.writeln("Blood pressure: ${systolic ?? '--'}/${diastolic ?? '--'} mmHg");
    buffer.writeln("Heart rate (Pulse): ${pulse ?? '--'} bpm");
    buffer.writeln("Oxygen saturation (SpO2): ${spo2 ?? '--'}%");
    buffer.writeln("ECG status: ${ecgSummary ?? 'Not performed'}");
    buffer.writeln();
    buffer.writeln("=== CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===");
    final List<String> systemKeys = ['cvs', 'gi_tract', 'kidneys', 'endocrine', 'immune', 'nervous', 'respiratory', 'musculoskeletal'];
    for (String key in systemKeys) {
      final folder = getSystemContext(key);
      buffer.writeln("--- BLOCK: ${key.toUpperCase()} ---");
      buffer.writeln("Main conclusion: ${folder['system_general_shelf'] ?? 'No data'}");
      buffer.writeln("Training log: ${folder['system_training_shelf'] ?? 'No data'}");
      buffer.writeln();
    }
    return buffer.toString();
  }

  // Assembly of extended context, including Gemini Flash outputs from the vault
  String getAggregatedContextForMasterWithFlash() {
    final StringBuffer buffer = StringBuffer();
    buffer.writeln(getAggregatedContextForMaster());
    final coreCascade = getSystemContext('core_cascade');
    buffer.writeln("=== SYNTHESIS (GEMINI FLASH) ===");
    buffer.writeln("Main conclusion:");
    buffer.writeln(coreCascade['flash_general_shelf'] ?? 'Missing');
    buffer.writeln();
    buffer.writeln("Training output:");
    buffer.writeln(coreCascade['flash_training_shelf'] ?? 'Missing');
    buffer.writeln();
    return buffer.toString();
  }

  // Context for Curator / Pro
  String getCuratorContext() {
    final coreCascade = getSystemContext('core_cascade');
    final StringBuffer buffer = StringBuffer();
    buffer.writeln("=== GUIDELINES AND CLINICAL ROUTE FROM CHIEF PHYSICIAN (PRO) ===");
    buffer.writeln(coreCascade['pro_general_shelf'] ?? 'Route not formed');
    buffer.writeln();
    buffer.writeln("=== DEEP CLINICAL ANALYSIS AND SYNDROMES (PRO TRAINING) ===");
    buffer.writeln(coreCascade['pro_training_shelf'] ?? 'No data available');
    buffer.writeln();
    buffer.writeln("=== PRIMARY SYNTHESIS (FLASH) ===");
    buffer.writeln(coreCascade['flash_general_shelf'] ?? 'No data available');
    buffer.writeln(getAthenaLogsSummaryForPro());
    return buffer.toString();
  }

  String? get lastProGeneralOutput => _systemFolders['core_cascade']?['pro_general_shelf'] as String?;
  String? get lastProTrainingOutput => _systemFolders['core_cascade']?['pro_training_shelf'] as String?;

  // ==========================================
  // CURATOR CHAT METHODS
  // ==========================================

  List<Map<String, String>> getCuratorChatHistory() {
    return List.unmodifiable(_curatorChatHistory);
  }

  void saveCuratorChatMessage(Map<String, String> message) {
    _curatorChatHistory.add(message);
    _persist();
    notifyListeners();
  }

  /// Synchronizes sanitized curator chat history from server for active patient.
  /// Strictly checks [expectedPatientId] against active patient attribution to prevent cross-patient contamination.
  /// Preserves local history if incoming history is empty or if patient attribution does not match.
  bool syncCuratorChatHistory(List<Map<String, String>> history, {String? expectedPatientId}) {
    if (expectedPatientId != null && _activePatientId != expectedPatientId) {
      debugPrint('[VaultService] Patient attribution mismatch when syncing curator chat history. Discarding.');
      return false;
    }
    if (history.isEmpty && _curatorChatHistory.isNotEmpty) {
      debugPrint('[VaultService] Incoming history is empty while local vault has history. Preserving local history.');
      return false;
    }
    _curatorChatHistory.clear();
    _curatorChatHistory.addAll(history);
    _persist();
    notifyListeners();
    return true;
  }

  // ==========================================
  // STORAGE FOR ATHENA'S HIDDEN CLINICAL REPORTS
  // ==========================================
  final List<Map<String, dynamic>> _athenaClinicalLogs = [];

  List<Map<String, dynamic>> getAthenaClinicalLogs() {
    return List.unmodifiable(_athenaClinicalLogs);
  }

  String getAthenaLogsSummaryForPro() {
    if (_athenaClinicalLogs.isEmpty) {
      return "Athena's hidden clinical logs are missing.";
    }
    final buffer = StringBuffer();
    buffer.writeln("=== CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS) ===");
    for (var log in _athenaClinicalLogs) {
      buffer.writeln("⏱ Timestamp: ${log['timestamp']}");
      buffer.writeln("📊 Status: ${log['status']}");
      buffer.writeln("🚩 Symptoms: ${(log['symptoms_mentioned'] as List?)?.join(', ') ?? 'None'}");
      buffer.writeln("⚠️ Red Flags: ${log['red_flags']}");
      buffer.writeln("📝 Summary for Chief Physician: ${log['summary_for_master']}");
      buffer.writeln("--------------------------------------------------");
    }
    return buffer.toString();
  }

  void saveAthenaClinicalReport(Map<String, dynamic> reportJson) {
    reportJson['timestamp'] = DateTime.now().toIso8601String();
    _athenaClinicalLogs.add(reportJson);
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['latest_clinical_report'] = reportJson;
    _mirrorToNotesExport('athena_daily_report', reportJson);
    _persist();
    notifyListeners();
  }

  // 🗑️ Absolute reset of all device data and memory
  Future<void> resetVault({bool keepWorkoutHistory = true}) async {
    List<dynamic> preservedWorkoutSessions = [];
    List<dynamic> preservedHeartRateSamples = [];
    Map<String, dynamic> preservedWorkoutContext = {};
    List<dynamic> preservedWorkoutReports = [];

    if (keepWorkoutHistory) {
      _ensureSmartWatchStructure();
      final sw = _smartWatchData;
      preservedWorkoutSessions = (sw['workout_sessions'] as List<dynamic>? ?? []).toList();
      preservedHeartRateSamples = (sw['heart_rate_samples'] as List<dynamic>? ?? []).toList();
      preservedWorkoutContext = Map<String, dynamic>.from(sw['workout_context'] as Map<String, dynamic>? ?? {});
      preservedWorkoutReports = (sw['workout_analysis_reports'] as List<dynamic>? ?? []).toList();
    }

    _athenaClinicalLogs.clear();
    _systemFolders['global']?['athena_sprint_day'] = 1;
    _hasUnreadAthenaMessage = false;
    if (_activePatientId != null) {
      final key = VaultStorageService.derivePatientStorageKey(_activePatientId!);
      final clearSeq = ++_writeSequence;
      await _enqueueWrite(
        storageKey: key,
        writeSeq: clearSeq,
        patientId: _activePatientId,
        epoch: _sessionEpoch,
        writeAction: () => _storage.clearPatientVault(key),
        errorContext: 'clear patient vault ($_activePatientId)',
      );
    } else {
      final clearSeq = ++_writeSequence;
      await _enqueueWrite(
        storageKey: VaultStorageService.unauthenticatedStorageKey,
        writeSeq: clearSeq,
        patientId: null,
        epoch: _sessionEpoch,
        writeAction: () => _storage.clearUnauthenticatedVault(),
        errorContext: 'clear unauthenticated vault',
      );
    }
    _selectedSystem = null;
    _curatorChatHistory.clear();
    _athenaClinicalLogs.clear();
    _data.clear();
    _data.addAll({
      "health_systems": {
        "cvs": {
          "name": "Cardiovascular",
          "completeness": 0.0,
          "color": "0xFFFF0000",
          "markers": {
            "smart_watch": {
              "workout_sessions": preservedWorkoutSessions,
              "heart_rate_samples": preservedHeartRateSamples,
              "workout_context": preservedWorkoutContext,
              "workout_analysis_reports": preservedWorkoutReports,
            }
          }
        },
        "gi_tract": {"name": "GI Tract", "completeness": 0.0, "color": "0xFFFFA500", "markers": {}},
        "respiratory": {"name": "Respiratory", "completeness": 0.0, "color": "0xFF00FFFF", "markers": {}},
        "nervous": {"name": "Nervous", "completeness": 0.0, "color": "0xFF8A2BE2", "markers": {}},
        "musculoskeletal": {"name": "Musculoskeletal", "completeness": 0.0, "color": "0xFFB0C4DE", "markers": {}},
        "kidneys": {"name": "Renal", "completeness": 0.0, "color": "0xFF0000FF", "markers": {}},
        "immune": {"name": "Immune and Blood", "completeness": 0.0, "color": "0xFF32CD32", "markers": {}},
        "endocrine": {"name": "Endocrine", "completeness": 0.0, "color": "0xFFEE82EE", "markers": {}},
      }
    });
    _systemFolders.clear();
    _systemFolders.addAll({
      'respiratory': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'immune': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'cvs': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'gi_tract': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'kidneys': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'endocrine': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'nervous': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'musculoskeletal': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'core_cascade': {
        'flash_general_shelf': 'Waiting for Gemini Flash output...',
        'flash_training_shelf': 'Gemini Flash deficiency logs...',
        'pro_general_shelf': 'Waiting for Gemini Pro output...',
        'pro_training_shelf': 'Gemini Pro deficiency logs...',
      },
      'notes_export': <String, dynamic>{},
      'global': {'labs': <String, dynamic>{}},
      'training_sandbox': {'raw_predictions': [], 'missing_data': [], 'risk_analysis': "PENDING..."},
    });
    await _persist();
    notifyListeners();
  }

  // ==========================================
  // METHODS FOR ATHENA'S DAILY TRIGGER
  // ==========================================

  int? getAthenaLastPromptTime() {
    return _systemFolders['global']?['athena_last_prompt_time'] as int?;
  }

  void updateAthenaLastPromptTime(int timestamp) {
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['athena_last_prompt_time'] = timestamp;
    _persist();
    notifyListeners();
  }

  void setUnreadAthenaMessage(bool value) {
    _hasUnreadAthenaMessage = value;
    notifyListeners();
  }

  // ==========================================
  // SPRINT COUNTER METHODS (1..7 DAYS)
  // ==========================================

  int getAthenaSprintDay() {
    return (_systemFolders['global']?['athena_sprint_day'] as int?) ?? 1;
  }

  int incrementAthenaSprintDay() {
    int currentDay = getAthenaSprintDay();
    if (currentDay >= 7) {
      currentDay = 1;
    } else {
      currentDay++;
    }
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['athena_sprint_day'] = currentDay;
    _persist();
    notifyListeners();
    return currentDay;
  }
}