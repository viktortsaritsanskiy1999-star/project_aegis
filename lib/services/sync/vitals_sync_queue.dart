import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../api/aegis_api_client.dart';
import '../api/vitals_api_models.dart';
import '../health_data_provider.dart';

/// Exception thrown when local queue state persistence fails
class VitalsQueuePersistenceException implements Exception {
  final String message;
  final dynamic cause;

  const VitalsQueuePersistenceException(this.message, [this.cause]);

  @override
  String toString() => 'VitalsQueuePersistenceException: $message${cause != null ? ' ($cause)' : ''}';
}

/// Status of the sync flush operation
enum SyncStatus {
  idle,
  success,
  retryableError,
  paused,
  conflict,
  persistenceError,
}

class SyncResult {
  final SyncStatus status;
  final int processedCount;
  final String? message;
  final bool persistenceFailed;

  const SyncResult({
    required this.status,
    this.processedCount = 0,
    this.message,
    this.persistenceFailed = false,
  });

  static const SyncResult idle = SyncResult(status: SyncStatus.idle);
}

/// An immutable item in the persistent sync queue
@immutable
class VitalsQueueItem {
  final String eventId;
  final String? idempotencyKey;
  final String? cycleId;
  final HealthMeasurementCreate dto;
  final DateTime enqueuedAt;
  final int retryCount;
  final String? lastError;
  final bool isQuarantined;

  const VitalsQueueItem({
    required this.eventId,
    this.idempotencyKey,
    this.cycleId,
    required this.dto,
    required this.enqueuedAt,
    this.retryCount = 0,
    this.lastError,
    this.isQuarantined = false,
  });

  Map<String, dynamic> toJson() => {
    'event_id': eventId,
    'idempotency_key': idempotencyKey,
    'cycle_id': cycleId,
    'dto': dto.toJson(),
    'enqueued_at': enqueuedAt.toIso8601String(),
    'retry_count': retryCount,
    'last_error': lastError,
    'is_quarantined': isQuarantined,
  };

  factory VitalsQueueItem.fromJson(Map<String, dynamic> json) => VitalsQueueItem(
    eventId: json['event_id'] as String,
    idempotencyKey: json['idempotency_key'] as String?,
    cycleId: json['cycle_id'] as String?,
    dto: HealthMeasurementCreate.fromJson(json['dto'] as Map<String, dynamic>),
    enqueuedAt: DateTime.parse(json['enqueued_at'] as String),
    retryCount: json['retry_count'] as int? ?? 0,
    lastError: json['last_error'] as String?,
    isQuarantined: json['is_quarantined'] == true,
  );

  VitalsQueueItem copyWith({
    String? idempotencyKey,
    String? cycleId,
    HealthMeasurementCreate? dto,
    int? retryCount,
    String? lastError,
    bool? isQuarantined,
  }) {
    return VitalsQueueItem(
      eventId: eventId,
      idempotencyKey: idempotencyKey ?? this.idempotencyKey,
      cycleId: cycleId ?? this.cycleId,
      dto: dto ?? this.dto,
      enqueuedAt: enqueuedAt,
      retryCount: retryCount ?? this.retryCount,
      lastError: lastError ?? this.lastError,
      isQuarantined: isQuarantined ?? this.isQuarantined,
    );
  }
}

/// Persistent, resilient offline-first telemetry synchronization queue
/// Backed by a single unified storage snapshot in SharedPreferences.
class VitalsSyncQueue extends ChangeNotifier {
  static const String _snapshotStorageKey = 'aegis_vitals_sync_snapshot_v2';
  static const String _legacyPendingKey = 'aegis_vitals_sync_pending_v1';
  static const String _legacyQuarantineKey = 'aegis_vitals_sync_quarantine_v1';

  final AegisApiClient _apiClient;
  String? _activeCycleId;

  final List<VitalsQueueItem> _pendingItems = [];
  final List<VitalsQueueItem> _quarantinedItems = [];

  bool _isFlushing = false;
  bool _isPaused = false;
  String? _pauseReason;
  DateTime? _nextRetryAllowedAfter;
  int _consecutiveFailures = 0;

  static int _eventCounter = 0;

  // Mutual exclusion lock for serializing asynchronous queue and storage operations
  Future<void> _lock = Future.value();

  Future<T> _synchronized<T>(Future<T> Function() action) async {
    final previousLock = _lock;
    final completer = Completer<void>();
    _lock = completer.future;

    try {
      await previousLock;
      return await action();
    } finally {
      completer.complete();
    }
  }

  List<VitalsQueueItem> get pendingItems => List.unmodifiable(_pendingItems);
  List<VitalsQueueItem> get quarantinedItems => List.unmodifiable(_quarantinedItems);
  bool get isPaused => _isPaused;
  String? get pauseReason => _pauseReason;
  String? get activeCycleId => _activeCycleId;

  VitalsSyncQueue({
    required AegisApiClient apiClient,
    String? activeCycleId,
  })  : _apiClient = apiClient,
        _activeCycleId = (activeCycleId != null && activeCycleId.isNotEmpty) ? activeCycleId : null;

  /// Set or update the active medical cycle ID.
  /// Binds unresolved pending events to this cycle without altering already bound events.
  Future<void> setActiveCycleId(String? cycleId) => _synchronized(() async {
    await _resolveActiveCycleLocked(cycleId);
  });

  Future<void> _resolveActiveCycleLocked(String? cycleId) async {
    final cleanId = (cycleId != null && cycleId.isNotEmpty) ? cycleId : null;

    final prevActiveCycleId = _activeCycleId;
    final prevPendingItems = List<VitalsQueueItem>.from(_pendingItems);
    final prevIsPaused = _isPaused;
    final prevPauseReason = _pauseReason;

    bool modified = cleanId != _activeCycleId;

    try {
      _activeCycleId = cleanId;

      if (cleanId != null) {
        for (int i = 0; i < _pendingItems.length; i++) {
          final item = _pendingItems[i];
          // Only bind items that do NOT have a resolved cycle yet
          if (item.cycleId == null) {
            final newKey = 'idem_${cleanId}_${item.eventId}';
            final updatedDto = item.dto.copyWith(
              cycleId: cleanId,
              rawMetadata: {
                ...?item.dto.rawMetadata,
                'idempotency_key': newKey,
                'event_id': item.eventId,
              },
            );
            _pendingItems[i] = item.copyWith(
              cycleId: cleanId,
              idempotencyKey: newKey,
              dto: updatedDto,
            );
            modified = true;
          }
        }

        if (_isPaused && _pauseReason != null && _pauseReason!.contains('cycle')) {
          _isPaused = false;
          _pauseReason = null;
          modified = true;
        }
      }

      if (modified) {
        await _persist();
      }

      notifyListeners();
    } catch (e) {
      _activeCycleId = prevActiveCycleId;
      _pendingItems.clear();
      _pendingItems.addAll(prevPendingItems);
      _isPaused = prevIsPaused;
      _pauseReason = prevPauseReason;
      notifyListeners();
      rethrow;
    }
  }

  void resume() {
    _isPaused = false;
    _pauseReason = null;
    _consecutiveFailures = 0;
    _nextRetryAllowedAfter = null;
    notifyListeners();
  }

  /// Load persisted queue snapshot from local storage under serialized lock.
  /// Automatically migrates from legacy dual keys if present, preserving old data until migration succeeds.
  Future<void> init() => _synchronized(() async {
    try {
      final prefs = await SharedPreferences.getInstance();

      final snapshotRaw = prefs.getString(_snapshotStorageKey);
      if (snapshotRaw != null && snapshotRaw.isNotEmpty) {
        final decoded = json.decode(snapshotRaw) as Map<String, dynamic>;
        final pendingList = (decoded['pending'] as List<dynamic>?) ?? [];
        final quarantineList = (decoded['quarantine'] as List<dynamic>?) ?? [];

        _pendingItems.clear();
        for (final item in pendingList) {
          _pendingItems.add(VitalsQueueItem.fromJson(item as Map<String, dynamic>));
        }

        _quarantinedItems.clear();
        for (final item in quarantineList) {
          _quarantinedItems.add(VitalsQueueItem.fromJson(item as Map<String, dynamic>));
        }

        if (decoded['active_cycle_id'] != null && _activeCycleId == null) {
          _activeCycleId = decoded['active_cycle_id'] as String?;
        }
        if (decoded['event_counter'] != null) {
          final savedCounter = decoded['event_counter'] as int;
          if (savedCounter > _eventCounter) {
            _eventCounter = savedCounter;
          }
        }
      } else {
        // Migration from legacy dual keys if single snapshot does not exist yet
        bool migrationNeeded = false;
        final pendingRaw = prefs.getString(_legacyPendingKey);
        if (pendingRaw != null && pendingRaw.isNotEmpty) {
          final decoded = json.decode(pendingRaw) as List<dynamic>;
          _pendingItems.clear();
          for (final item in decoded) {
            _pendingItems.add(VitalsQueueItem.fromJson(item as Map<String, dynamic>));
          }
          migrationNeeded = true;
        }

        final quarantineRaw = prefs.getString(_legacyQuarantineKey);
        if (quarantineRaw != null && quarantineRaw.isNotEmpty) {
          final decoded = json.decode(quarantineRaw) as List<dynamic>;
          _quarantinedItems.clear();
          for (final item in decoded) {
            _quarantinedItems.add(VitalsQueueItem.fromJson(item as Map<String, dynamic>));
          }
          migrationNeeded = true;
        }

        if (migrationNeeded) {
          debugPrint('[VitalsSyncQueue] Migrating legacy dual keys to single snapshot v2...');
          await _persist();
          // Remove legacy keys ONLY after snapshot write succeeds
          await prefs.remove(_legacyPendingKey);
          await prefs.remove(_legacyQuarantineKey);
          debugPrint('[VitalsSyncQueue] Migration to single snapshot v2 complete.');
        }
      }

      // If activeCycleId is known, bind any unresolved items
      if (_activeCycleId != null) {
        await _resolveActiveCycleLocked(_activeCycleId);
      }

      debugPrint('[VitalsSyncQueue] Initialized snapshot: ${_pendingItems.length} pending, ${_quarantinedItems.length} quarantined');
    } catch (e) {
      debugPrint('[VitalsSyncQueue] Error loading snapshot from storage: $e');
      if (e is VitalsQueuePersistenceException) rethrow;
      throw VitalsQueuePersistenceException('Failed to initialize queue snapshot from storage: $e', e);
    }
  });

  /// Atomic sequential persistence of single snapshot to local SharedPreferences.
  /// Stores pending, quarantine, activeCycleId, and technical metadata in a single key.
  Future<void> _persist() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final snapshot = {
        'version': 2,
        'saved_at': DateTime.now().toUtc().toIso8601String(),
        'active_cycle_id': _activeCycleId,
        'event_counter': _eventCounter,
        'pending': _pendingItems.map((i) => i.toJson()).toList(),
        'quarantine': _quarantinedItems.map((i) => i.toJson()).toList(),
      };
      final encoded = json.encode(snapshot);
      final ok = await prefs.setString(_snapshotStorageKey, encoded);

      if (!ok) {
        throw const VitalsQueuePersistenceException('SharedPreferences returned false on snapshot write');
      }
    } catch (e) {
      if (e is VitalsQueuePersistenceException) rethrow;
      throw VitalsQueuePersistenceException('Error persisting queue snapshot: $e', e);
    }
  }

  /// Enqueue an incoming HealthMeasurement with atomic single-snapshot persistence.
  /// If cycleId is unknown, the item is persisted locally without a fictitious fallback cycle.
  Future<VitalsQueueItem> enqueue(
    HealthMeasurement measurement, {
    String? cycleId,
    String? sessionId,
    String? phase,
  }) => _synchronized(() async {
    final effectiveCycleId = (cycleId != null && cycleId.isNotEmpty)
        ? cycleId
        : _activeCycleId;

    final nowMicro = DateTime.now().microsecondsSinceEpoch;
    _eventCounter++;
    final eventId = 'evt_${nowMicro}_$_eventCounter';

    final idempotencyKey = effectiveCycleId != null
        ? 'idem_${effectiveCycleId}_$eventId'
        : null;

    final metadata = Map<String, dynamic>.from(measurement.metadata);
    metadata['event_id'] = eventId;
    if (idempotencyKey != null) {
      metadata['idempotency_key'] = idempotencyKey;
    }

    final dto = HealthMeasurementCreate(
      cycleId: effectiveCycleId,
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

    final queueItem = VitalsQueueItem(
      eventId: eventId,
      idempotencyKey: idempotencyKey,
      cycleId: effectiveCycleId,
      dto: dto,
      enqueuedAt: DateTime.now(),
    );

    final prevPending = List<VitalsQueueItem>.from(_pendingItems);
    _pendingItems.add(queueItem);

    // Atomically persist snapshot. If persistence fails, roll back in-memory addition!
    try {
      await _persist();
    } catch (e) {
      _pendingItems.clear();
      _pendingItems.addAll(prevPending);
      rethrow;
    }

    notifyListeners();
    debugPrint('[VitalsSyncQueue] Enqueued item $eventId (cycle: $effectiveCycleId, key: $idempotencyKey). Pending: ${_pendingItems.length}');
    return queueItem;
  });

  /// Flush pending items sequentially in batches with deterministic 409 conflict isolation.
  Future<SyncResult> flush({int maxBatchSize = 50}) => _synchronized(() async {
    if (_isFlushing) return SyncResult.idle;
    if (_isPaused) {
      debugPrint('[VitalsSyncQueue] Flush skipped: Queue is paused ($_pauseReason)');
      return SyncResult(status: SyncStatus.paused, message: _pauseReason);
    }
    if (_pendingItems.isEmpty) return SyncResult.idle;

    if (_nextRetryAllowedAfter != null && DateTime.now().isBefore(_nextRetryAllowedAfter!)) {
      debugPrint('[VitalsSyncQueue] Flush delayed due to backoff until $_nextRetryAllowedAfter');
      return const SyncResult(status: SyncStatus.retryableError, message: 'Backoff active cooldown');
    }

    _isFlushing = true;
    notifyListeners();

    List<VitalsQueueItem> batchItems = const [];

    try {
      // If activeCycleId is unknown, try to query backend /api/v1/cycle/current
      if (_activeCycleId == null) {
        try {
          final cycle = await _apiClient.getCurrentCycle();
          if (cycle.status == 'active') {
            await _resolveActiveCycleLocked(cycle.cycleId);
          }
        } on AegisCycleNotFoundException catch (e) {
          _isPaused = true;
          _pauseReason = '404 Cycle Not Found: ${e.message}';
          debugPrint('[VitalsSyncQueue] Paused: No active cycle available on server');
          notifyListeners();
          return SyncResult(status: SyncStatus.paused, message: _pauseReason);
        } on AegisAuthException catch (e) {
          _isPaused = true;
          _pauseReason = '401 Unauthorized: ${e.message}';
          debugPrint('[VitalsSyncQueue] Paused on auth failure during cycle resolution');
          notifyListeners();
          return SyncResult(status: SyncStatus.paused, message: _pauseReason);
        } catch (e) {
          debugPrint('[VitalsSyncQueue] Could not resolve current cycle: $e');
        }
      }

      // Filter only items that have an authoritative cycleId
      final readyItems = _pendingItems.where((item) => item.cycleId != null).toList();
      if (readyItems.isEmpty) {
        debugPrint('[VitalsSyncQueue] No items with resolved cycleId available for flush');
        return const SyncResult(status: SyncStatus.paused, message: 'No items with resolved medical cycle');
      }

      final targetCycle = readyItems.first.cycleId!;
      batchItems = readyItems
          .where((item) => item.cycleId == targetCycle)
          .take(maxBatchSize)
          .toList();

      if (batchItems.isEmpty) {
        return SyncResult.idle;
      }

      // Execute transmission with deterministic conflict isolation
      final result = await _sendBatchWithConflictIsolation(batchItems, targetCycle);
      return result;
    } on AegisAuthException catch (e) {
      _isPaused = true;
      _pauseReason = '401 Unauthorized: ${e.message}';
      debugPrint('[VitalsSyncQueue] Paused on auth failure: $_pauseReason');
      notifyListeners();
      return SyncResult(status: SyncStatus.paused, message: _pauseReason);
    } on AegisCycleNotFoundException catch (e) {
      _isPaused = true;
      _pauseReason = '404 Cycle Not Found: ${e.message}';
      debugPrint('[VitalsSyncQueue] Paused on missing cycle: $_pauseReason');
      notifyListeners();
      return SyncResult(status: SyncStatus.paused, message: _pauseReason);
    } on AegisBadRequestException catch (e) {
      _isPaused = true;
      _pauseReason = '400 Bad Request: ${e.message}';
      debugPrint('[VitalsSyncQueue] Paused on bad request: $_pauseReason');
      notifyListeners();
      return SyncResult(status: SyncStatus.paused, message: _pauseReason);
    } on AegisServerException catch (e) {
      return await _handleRetryableError(batchItems, e.message);
    } on AegisNetworkException catch (e) {
      return await _handleRetryableError(batchItems, e.message);
    } on AegisTimeoutException catch (e) {
      return await _handleRetryableError(batchItems, e.message);
    } finally {
      _isFlushing = false;
      notifyListeners();
    }
  });

  /// Deterministic batch splitting algorithm to isolate 409 conflicting items.
  /// Uses single-snapshot persistence to guarantee atomic transitions between pending and quarantine.
  Future<SyncResult> _sendBatchWithConflictIsolation(
    List<VitalsQueueItem> items,
    String targetCycle,
  ) async {
    if (items.isEmpty) return SyncResult.idle;

    try {
      final batchDto = HealthMeasurementCreateBatch(
        cycleId: targetCycle,
        measurements: items.map((i) => i.dto).toList(),
      );

      final response = await _apiClient.postVitalsBatch(batchDto);

      // Server successfully acknowledged: remove from pending
      final prevPending = List<VitalsQueueItem>.from(_pendingItems);
      _pendingItems.removeWhere((item) => items.any((b) => b.eventId == item.eventId));
      _consecutiveFailures = 0;
      _nextRetryAllowedAfter = null;

      try {
        await _persist();
      } catch (persistErr) {
        debugPrint('[VitalsSyncQueue] Server acknowledged delivery, but snapshot persist failed: $persistErr');
        // Restore pending items in memory so they are not lost if process is killed or retried
        _pendingItems.clear();
        _pendingItems.addAll(prevPending);
        notifyListeners();
        return SyncResult(
          status: SyncStatus.persistenceError,
          persistenceFailed: true,
          processedCount: items.length,
          message: 'Server acknowledged delivery (${items.length} items), but snapshot persist failed: $persistErr',
        );
      }
      notifyListeners();

      debugPrint('[VitalsSyncQueue] Successfully flushed ${items.length} items (recorded: ${response.recordedCount}, deduplicated: ${response.deduplicatedCount}). Pending: ${_pendingItems.length}');

      return SyncResult(
        status: SyncStatus.success,
        processedCount: items.length,
        message: 'Successfully synchronized ${items.length} items',
      );
    } on AegisConflictException catch (e) {
      // If this was an individual measurement, it is confirmed conflicting!
      if (items.length == 1) {
        final conflictingItem = items.first;
        debugPrint('[VitalsSyncQueue] 409 Conflict isolated to single item ${conflictingItem.eventId}. Quarantining.');

        final prevPending = List<VitalsQueueItem>.from(_pendingItems);
        final prevQuarantine = List<VitalsQueueItem>.from(_quarantinedItems);

        _quarantinedItems.add(conflictingItem.copyWith(
          isQuarantined: true,
          lastError: '409 Conflict: ${e.message}',
        ));
        _pendingItems.removeWhere((item) => item.eventId == conflictingItem.eventId);

        try {
          await _persist();
        } catch (persistErr) {
          debugPrint('[VitalsSyncQueue] Failed to persist quarantine snapshot: $persistErr');
          _pendingItems.clear();
          _pendingItems.addAll(prevPending);
          _quarantinedItems.clear();
          _quarantinedItems.addAll(prevQuarantine);
          notifyListeners();
          return SyncResult(
            status: SyncStatus.persistenceError,
            persistenceFailed: true,
            processedCount: 0,
            message: 'Conflict isolated for ${conflictingItem.eventId}, but quarantine persistence failed: $persistErr',
          );
        }

        notifyListeners();

        return SyncResult(
          status: SyncStatus.conflict,
          processedCount: 1,
          message: 'Conflict 409 isolated for item ${conflictingItem.eventId}: ${e.message}',
        );
      }

      // If batch had multiple items, split deterministically into smaller chunks
      debugPrint('[VitalsSyncQueue] Batch of ${items.length} items returned 409. Splitting to isolate conflict.');
      final mid = items.length ~/ 2;
      final leftGroup = items.sublist(0, mid);
      final rightGroup = items.sublist(mid);

      final leftResult = await _sendBatchWithConflictIsolation(leftGroup, targetCycle);
      final rightResult = await _sendBatchWithConflictIsolation(rightGroup, targetCycle);

      final totalProcessed = leftResult.processedCount + rightResult.processedCount;

      if (leftResult.persistenceFailed || rightResult.persistenceFailed) {
        return SyncResult(
          status: SyncStatus.persistenceError,
          persistenceFailed: true,
          processedCount: totalProcessed,
          message: 'Batch split processed items, but snapshot persistence failed in sub-batch',
        );
      }

      if (leftResult.status == SyncStatus.conflict || rightResult.status == SyncStatus.conflict) {
        return SyncResult(
          status: SyncStatus.conflict,
          processedCount: totalProcessed,
          message: 'Batch split successfully isolated conflicts and delivered healthy measurements',
        );
      }

      return SyncResult(
        status: SyncStatus.success,
        processedCount: totalProcessed,
        message: 'Batch split delivered all remaining items',
      );
    }
  }

  Future<SyncResult> _handleRetryableError(List<VitalsQueueItem> batchItems, String errorMsg) async {
    final prevPending = List<VitalsQueueItem>.from(_pendingItems);
    final prevConsecutiveFailures = _consecutiveFailures;
    final prevNextRetryAllowedAfter = _nextRetryAllowedAfter;

    _consecutiveFailures++;
    final delaySeconds = (2 << (_consecutiveFailures.clamp(0, 5))).clamp(2, 60);
    _nextRetryAllowedAfter = DateTime.now().add(Duration(seconds: delaySeconds));

    for (int i = 0; i < _pendingItems.length; i++) {
      if (batchItems.any((b) => b.eventId == _pendingItems[i].eventId)) {
        _pendingItems[i] = _pendingItems[i].copyWith(
          retryCount: _pendingItems[i].retryCount + 1,
          lastError: errorMsg,
        );
      }
    }

    try {
      await _persist();
    } catch (persistErr) {
      debugPrint('[VitalsSyncQueue] Failed to persist retry state snapshot: $persistErr');
      _pendingItems.clear();
      _pendingItems.addAll(prevPending);
      _consecutiveFailures = prevConsecutiveFailures;
      _nextRetryAllowedAfter = prevNextRetryAllowedAfter;
      notifyListeners();
      return SyncResult(
        status: SyncStatus.persistenceError,
        persistenceFailed: true,
        processedCount: 0,
        message: 'Retryable error ($errorMsg), but snapshot persistence failed: $persistErr',
      );
    }

    debugPrint('[VitalsSyncQueue] Retryable Error: $errorMsg. Retry #$_consecutiveFailures scheduled in ${delaySeconds}s.');
    notifyListeners();
    return SyncResult(status: SyncStatus.retryableError, message: errorMsg);
  }

  /// Clear all queue data (for test isolation)
  Future<void> clearAll() => _synchronized(() async {
    _pendingItems.clear();
    _quarantinedItems.clear();
    _isPaused = false;
    _pauseReason = null;
    _consecutiveFailures = 0;
    _nextRetryAllowedAfter = null;
    await _persist();
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_legacyPendingKey);
    await prefs.remove(_legacyQuarantineKey);
    notifyListeners();
  });
}
