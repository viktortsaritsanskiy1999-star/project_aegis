// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/screens/smart_watch_import_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';

String _buildTestJwt({
  String patientId = 'patient_A',
  String deviceId = 'dev_test_01',
  List<String> scopes = const ['*'],
}) {
  final header = base64Url.encode(utf8.encode(json.encode({'alg': 'HS256', 'typ': 'JWT'}))).replaceAll('=', '');
  final payload = base64Url.encode(utf8.encode(json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'device_id': deviceId,
    'scopes': scopes,
    'type': 'access',
    'exp': DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000,
  }))).replaceAll('=', '');
  return '$header.$payload.fake_sig';
}

Widget _buildScreenApp({
  required VaultService vaultService,
  required AuthSessionManager authManager,
  required AegisApiClient apiClient,
  required VitalsSyncQueue syncQueue,
  required HealthDataProvider healthProvider,
  VoidCallback? onNavigateToAtlas,
}) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vaultService),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      Provider<AegisApiClient>.value(value: apiClient),
      ChangeNotifierProvider<VitalsSyncQueue>.value(value: syncQueue),
      ChangeNotifierProvider<HealthDataProvider>.value(value: healthProvider),
    ],
    child: MaterialApp(
      home: SmartWatchImportScreen(
        apiClient: apiClient,
        authSessionManager: authManager,
        healthDataProvider: healthProvider,
        syncQueue: syncQueue,
        onNavigateToAtlas: onNavigateToAtlas ?? () {},
      ),
    ),
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryStorage;
  late TimeService timeService;
  late VaultService vaultService;
  late VitalsMonitorService vitalsService;
  late InMemoryAuthTokenStorage tokenStorage;
  late AuthSessionManager authManager;
  late List<http.Request> capturedBatchRequests;
  late bool simulateNetworkError;

  setUp(() async {
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});
    AuthSessionManager.resetActiveInstancesForTesting();
    VaultService.resetWriteQueueForTesting();

    memoryStorage = InMemoryVaultStorage();
    VaultStorageService.setStorageForTesting(memoryStorage);

    timeService = TimeService()..resetToRealTime();
    vaultService = VaultService(storage: memoryStorage);
    await vaultService.initFuture;
    await vaultService.setActivePatientId('patient_A');

    vitalsService = VitalsMonitorService(timeService, vaultService);

    final sessionA = AuthSession(
      accessToken: _buildTestJwt(patientId: 'patient_A'),
      refreshToken: 'refresh_token_A',
      tokenType: 'Bearer',
      expiresIn: 3600,
      expiresAt: DateTime.now().add(const Duration(hours: 1)),
      refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
      deviceId: 'dev_test_01',
      patientId: 'patient_A',
      scope: '*',
    );
    tokenStorage = InMemoryAuthTokenStorage(initialSession: sessionA);
    authManager = AuthSessionManager(
      storage: tokenStorage,
      apiClient: AegisApiClient(baseUrl: 'http://127.0.0.1:8000'),
    );
    await authManager.initialize();

    capturedBatchRequests = [];
    simulateNetworkError = false;
  });

  tearDown(() {
    VaultService.resetWriteQueueForTesting();
    VaultStorageService.setStorageForTesting(null);
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  AegisApiClient createMockClient() {
    return AegisApiClient(
      baseUrl: 'http://127.0.0.1:8000',
      client: MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/vitals/batch')) {
          capturedBatchRequests.add(request);
          if (simulateNetworkError) {
            throw http.ClientException('Simulated network offline');
          }
          final body = json.decode(request.body) as Map<String, dynamic>;
          final measurements = body['measurements'] as List;
          return http.Response(
            json.encode({
              'cycle_id': body['cycle_id'] ?? 'cycle_01',
              'total_received': measurements.length,
              'recorded_count': measurements.length,
              'deduplicated_count': 0,
              'items': measurements.asMap().entries.map((entry) => {
                'id': entry.key + 1,
                'cycle_id': body['cycle_id'] ?? 'cycle_01',
                'status': 'recorded',
                'source': 'manual_input',
                'is_simulated': false,
                'is_historical': false,
                'measured_at': DateTime.now().toUtc().toIso8601String(),
                'ingested_at': DateTime.now().toUtc().toIso8601String(),
                'phase': 'standby',
                'note': '',
              }).toList(),
            }),
            201,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.endsWith('/api/v1/cycle/current')) {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_01',
              'cycle_index': 1,
              'state': 'active',
              'patient_id': 'patient_A',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('{"detail":"Not found"}', 404);
      }),
    );
  }

  group('PROJECT AEGIS — ARGON BUG-11: MANUAL VITALS SERVER SYNCHRONIZATION', () {
    testWidgets('1. Valid BP, pulse and SpO2 submission: dispatched to VitalsSyncQueue and local Vault', (tester) async {
      final client = createMockClient();
      final syncQueue = VitalsSyncQueue(
        apiClient: client,
        activeCycleId: 'cycle_01',
        authSessionManager: authManager,
        activePatientId: 'patient_A',
        enforcePatientIsolation: true,
      );
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      await tester.pumpWidget(
        _buildScreenApp(
          vaultService: vaultService,
          authManager: authManager,
          apiClient: client,
          syncQueue: syncQueue,
          healthProvider: healthProvider,
        ),
      );
      await tester.pumpAndSettle();

      // Enter valid vitals
      await tester.enterText(find.byType(TextField).at(0), '120'); // Systolic
      await tester.enterText(find.byType(TextField).at(1), '80');  // Diastolic
      await tester.enterText(find.byType(TextField).at(2), '72');  // Pulse
      await tester.enterText(find.byType(TextField).at(3), '98');  // SpO2
      await tester.pumpAndSettle();

      // Tap Save markers
      await tester.tap(find.text('Save markers ⌚'));
      await tester.pumpAndSettle();

      // Flush queue
      syncQueue.cancelScheduledFlush();
      await syncQueue.flush();

      // Verify VitalsSyncQueue dispatched measurement to backend
      expect(capturedBatchRequests.length, equals(1));

      final batchBody = json.decode(capturedBatchRequests.first.body) as Map<String, dynamic>;
      final items = batchBody['measurements'] as List;
      expect(items.length, equals(1));
      final measurement = items.first as Map<String, dynamic>;
      expect(measurement['systolic'], equals(120));
      expect(measurement['diastolic'], equals(80));
      expect(measurement['pulse'], equals(72));
      expect(measurement['spo2'], equals(98));
      expect(measurement['source'], equals('manual_input'));
      expect(measurement['cycle_id'], equals('cycle_01'));

      // Verify VaultService local metrics updated
      expect(vaultService.systolic, equals(120));
      expect(vaultService.diastolic, equals(80));
      expect(vaultService.pulse, equals(72));
      expect(vaultService.spo2, equals(98));

      syncQueue.cancelScheduledFlush();
    });

    testWidgets('2. Partial manual input: only BP, only Pulse, only SpO2 are validly accepted and submitted', (tester) async {
      // Subtest A: Only BP (130/85)
      final client = createMockClient();
      final syncQueue = VitalsSyncQueue(
        apiClient: client,
        activeCycleId: 'cycle_01',
        authSessionManager: authManager,
        activePatientId: 'patient_A',
      );
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      await tester.pumpWidget(
        _buildScreenApp(
          vaultService: vaultService,
          authManager: authManager,
          apiClient: client,
          syncQueue: syncQueue,
          healthProvider: healthProvider,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(0), '130');
      await tester.enterText(find.byType(TextField).at(1), '85');
      await tester.pumpAndSettle();

      await tester.tap(find.text('Save markers ⌚'));
      await tester.pumpAndSettle();

      syncQueue.cancelScheduledFlush();
      await syncQueue.flush();

      expect(capturedBatchRequests.length, equals(1));
      var items = (json.decode(capturedBatchRequests.first.body) as Map<String, dynamic>)['measurements'] as List;
      expect(items.first['systolic'], equals(130));
      expect(items.first['diastolic'], equals(85));
      expect(items.first['pulse'], isNull);
      expect(items.first['spo2'], isNull);

      // Subtest B: Direct helper validation check
      expect(SmartWatchImportScreen.isValidBloodPressure(120, 80), isTrue);
      expect(SmartWatchImportScreen.isValidBloodPressure(120, null), isFalse);
      expect(SmartWatchImportScreen.isValidBloodPressure(80, 120), isFalse); // inverted
      expect(SmartWatchImportScreen.isValidPulse(75), isTrue);
      expect(SmartWatchImportScreen.isValidPulse(15), isFalse); // below MIN
      expect(SmartWatchImportScreen.isValidPulse(400), isFalse); // above MAX
      expect(SmartWatchImportScreen.isValidSpo2(97), isTrue);
      expect(SmartWatchImportScreen.isValidSpo2(40), isFalse); // below MIN

      syncQueue.cancelScheduledFlush();
    });

    testWidgets('3. Invalid or empty values: blank, incomplete or non-physiologic inputs are not sent to server', (tester) async {
      final client = createMockClient();
      final syncQueue = VitalsSyncQueue(
        apiClient: client,
        activeCycleId: 'cycle_01',
        authSessionManager: authManager,
        activePatientId: 'patient_A',
      );
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      await tester.pumpWidget(
        _buildScreenApp(
          vaultService: vaultService,
          authManager: authManager,
          apiClient: client,
          syncQueue: syncQueue,
          healthProvider: healthProvider,
        ),
      );
      await tester.pumpAndSettle();

      // Incomplete BP (sys entered, dia empty) + out-of-range pulse
      await tester.enterText(find.byType(TextField).at(0), '120');
      await tester.enterText(find.byType(TextField).at(1), '');
      await tester.enterText(find.byType(TextField).at(2), '999'); // invalid pulse
      await tester.enterText(find.byType(TextField).at(3), 'abc'); // invalid SpO2
      await tester.pumpAndSettle();

      await tester.tap(find.text('Save markers ⌚'));
      await tester.pumpAndSettle();

      syncQueue.cancelScheduledFlush();
      await syncQueue.flush();

      // Zero items should be queued or sent to the server
      expect(capturedBatchRequests.length, equals(0));
      expect(syncQueue.pendingCount, equals(0));

      syncQueue.cancelScheduledFlush();
    });

    testWidgets('4. Offline queue and retry: offline state preserves measurement and retries successfully', (tester) async {
      simulateNetworkError = true; // Offline!
      final client = createMockClient();
      final syncQueue = VitalsSyncQueue(
        apiClient: client,
        activeCycleId: 'cycle_01',
        authSessionManager: authManager,
        activePatientId: 'patient_A',
      );
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      await tester.pumpWidget(
        _buildScreenApp(
          vaultService: vaultService,
          authManager: authManager,
          apiClient: client,
          syncQueue: syncQueue,
          healthProvider: healthProvider,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(0), '118');
      await tester.enterText(find.byType(TextField).at(1), '76');
      await tester.enterText(find.byType(TextField).at(2), '70');
      await tester.enterText(find.byType(TextField).at(3), '99');
      await tester.pumpAndSettle();

      await tester.tap(find.text('Save markers ⌚'));
      await tester.pumpAndSettle();

      // Since offline, the item must remain safely enqueued in pendingItems
      expect(syncQueue.activePatientPendingItems.length, equals(1));
      final pendingItem = syncQueue.activePatientPendingItems.first;
      expect(pendingItem.dto.pulse, equals(70));
      expect(pendingItem.dto.systolic, equals(118));
      expect(pendingItem.dto.diastolic, equals(76));
      expect(pendingItem.dto.spo2, equals(99));

      // Network restored: flush succeeds and clears the queue
      simulateNetworkError = false;
      syncQueue.cancelScheduledFlush();
      final retryResult = await syncQueue.flush();
      expect(retryResult.status, equals(SyncStatus.success));
      expect(syncQueue.activePatientPendingItems.length, equals(0));

      syncQueue.cancelScheduledFlush();
    });

    testWidgets('5. Patient switch during synchronization: Patient A vitals are never submitted for Patient B', (tester) async {
      simulateNetworkError = true; // Queue offline for Patient A
      final client = createMockClient();
      final syncQueue = VitalsSyncQueue(
        apiClient: client,
        activeCycleId: 'cycle_01',
        authSessionManager: authManager,
        activePatientId: 'patient_A',
        enforcePatientIsolation: true,
      );
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      await tester.pumpWidget(
        _buildScreenApp(
          vaultService: vaultService,
          authManager: authManager,
          apiClient: client,
          syncQueue: syncQueue,
          healthProvider: healthProvider,
        ),
      );
      await tester.pumpAndSettle();

      // Patient A enters vitals
      await tester.enterText(find.byType(TextField).at(2), '75'); // Pulse 75
      await tester.tap(find.text('Save markers ⌚'));
      await tester.pumpAndSettle();

      expect(syncQueue.activePatientPendingItems.length, equals(1));
      expect(syncQueue.activePatientPendingItems.first.patientId, equals('patient_A'));

      // Switch to Patient B in AuthSessionManager and VaultService
      simulateNetworkError = false;
      capturedBatchRequests.clear();
      syncQueue.cancelScheduledFlush();
      await vaultService.setActivePatientId('patient_B');
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_B'),
          refreshToken: 'refresh_token_B',
          expiresIn: 3600,
          expiresAt: DateTime.now().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
          deviceId: 'dev_test_01',
          patientId: 'patient_B',
          scope: '*',
        ),
        status: AuthSessionStatus.authenticated,
      );

      // Flush while Patient B is active must NOT submit Patient A's item
      final flushResult = await syncQueue.flush();
      expect(flushResult.processedCount, equals(0));
      expect(capturedBatchRequests.length, equals(0));

      // Patient A item remains quarantined/isolated to Patient A
      expect(syncQueue.activePatientPendingItems.length, equals(0));

      syncQueue.cancelScheduledFlush();
    });

    testWidgets('6. No duplicate measurements after repeated save: identical input tapped repeatedly produces 1 enqueue', (tester) async {
      simulateNetworkError = true; // Keep in queue so we can count pending items
      final client = createMockClient();
      final syncQueue = VitalsSyncQueue(
        apiClient: client,
        activeCycleId: 'cycle_01',
        authSessionManager: authManager,
        activePatientId: 'patient_A',
      );
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      await tester.pumpWidget(
        _buildScreenApp(
          vaultService: vaultService,
          authManager: authManager,
          apiClient: client,
          syncQueue: syncQueue,
          healthProvider: healthProvider,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(0), '120');
      await tester.enterText(find.byType(TextField).at(1), '80');
      await tester.enterText(find.byType(TextField).at(2), '72');
      await tester.enterText(find.byType(TextField).at(3), '98');
      await tester.pumpAndSettle();

      // First call
      final state = tester.state<SmartWatchImportScreenState>(find.byType(SmartWatchImportScreen));
      await state.saveMarkersAndProceed();
      await tester.pumpAndSettle();
      expect(syncQueue.activePatientPendingItems.length, equals(1));

      // Repeated call on state with identical inputs: skipped by signature deduplication
      await state.saveMarkersAndProceed();
      await tester.pumpAndSettle();

      // Queue still contains exactly 1 item!
      expect(syncQueue.activePatientPendingItems.length, equals(1));

      syncQueue.cancelScheduledFlush();
    });
  });
}
