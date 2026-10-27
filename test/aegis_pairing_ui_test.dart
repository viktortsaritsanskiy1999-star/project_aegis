// ---------------------------------------------------------------------------
// PROJECT AEGIS — STAGE 5.3C-3B-3B
// PAIRING UI SAFETY, SECURITY (P1–P7) & INTEGRATION WIDGET TESTS
// ---------------------------------------------------------------------------

import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/widgets/aegis_device_pairing_modal.dart';

String _buildTestJwt({
  required String patientId,
  required String deviceId,
}) {
  final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
  final payloadJson = json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'did': deviceId,
    'type': 'access',
  });
  final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
  final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
  const s = 'c2lnbmF0dXJlX2Zha2Vfc2VjcmV0X2FkZHM';
  return '$h.$p.$s';
}

class PairingTestHarness {
  final InMemoryAuthTokenStorage storage;
  final AegisApiClient apiClient;
  final AuthSessionManager authManager;
  final VitalsSyncQueue syncQueue;
  final HealthDataProvider healthProvider;
  final VaultService vaultService;

  PairingTestHarness({
    required this.storage,
    required this.apiClient,
    required this.authManager,
    required this.syncQueue,
    required this.healthProvider,
    required this.vaultService,
  });

  static Future<PairingTestHarness> create({
    required MockClientHandler handler,
    AuthSession? initialSession,
  }) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});
    final storage = InMemoryAuthTokenStorage();
    if (initialSession != null) {
      await storage.saveSession(initialSession);
    }
    final mockClient = MockClient(handler);
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitorService = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(
      apiClient: apiClient,
      authSessionManager: authManager,
      enforcePatientIsolation: true,
    );
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitorService,
      syncQueue: syncQueue,
    );

    return PairingTestHarness(
      storage: storage,
      apiClient: apiClient,
      authManager: authManager,
      syncQueue: syncQueue,
      healthProvider: healthProvider,
      vaultService: vaultService,
    );
  }

  void dispose() {
    authManager.dispose();
    syncQueue.dispose();
    healthProvider.dispose();
    AuthSessionManager.resetActiveInstancesForTesting();
  }
}

Widget _wrapWidget({
  required PairingTestHarness harness,
  required Widget child,
}) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: harness.vaultService),
      ChangeNotifierProvider<AuthSessionManager>.value(value: harness.authManager),
      ChangeNotifierProvider<HealthDataProvider>.value(value: harness.healthProvider),
    ],
    child: MaterialApp(
      home: Scaffold(
        body: child,
      ),
    ),
  );
}

void main() {
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  tearDown(() {
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  // =========================================================================
  // GROUP 1: STAGE 5.3C-3B-3A BASELINE SMOKE & STATE TESTS (1–7)
  // =========================================================================
  group('Stage 5.3C-3B-3A: Baseline Smoke & State Tests', () {
    testWidgets('1. Modal opens via AegisDevicePairingModal.show() and displays unpaired form',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async => http.Response('{}', 200),
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: Builder(
            builder: (ctx) => ElevatedButton(
              key: const Key('trigger_button'),
              onPressed: () => AegisDevicePairingModal.show(ctx),
              child: const Text('Open'),
            ),
          ),
        ),
      );

      expect(find.byKey(const Key('trigger_button')), findsOneWidget);
      await tester.tap(find.byKey(const Key('trigger_button')));
      await tester.pumpAndSettle();

      expect(find.text('PAIR AEGIS PROFILE'), findsOneWidget);
      expect(find.byKey(const Key('pairing_code_input')), findsOneWidget);
      expect(find.byKey(const Key('device_name_input')), findsOneWidget);
      expect(find.byKey(const Key('pair_device_button')), findsOneWidget);

      harness.dispose();
    });

    testWidgets('2. Code input enforces 6 digits and preserves leading zeros', (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async => http.Response('{}', 200),
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      final pairButtonFinder = find.byKey(const Key('pair_device_button'));
      final ElevatedButton initialButton = tester.widget(pairButtonFinder);
      expect(initialButton.onPressed, isNull, reason: 'Button must be disabled when empty');

      // Enter letters/symbols (should be filtered out by digitsOnly)
      await tester.enterText(find.byKey(const Key('pairing_code_input')), 'abc!@#');
      await tester.pump();
      expect(find.text('abc!@#'), findsNothing);

      // Enter 4 digits (insufficient)
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '0042');
      await tester.pump();
      final ElevatedButton partialButton = tester.widget(pairButtonFinder);
      expect(partialButton.onPressed, isNull, reason: 'Button must be disabled for <6 digits');

      // Enter 6 digits with leading zeros
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.pump();
      final ElevatedButton readyButton = tester.widget(pairButtonFinder);
      expect(readyButton.onPressed, isNotNull, reason: 'Button must be enabled for exactly 6 digits');

      // Attempt to enter 8 digits (length limit of 6 must prevent extra digits)
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '00421899');
      await tester.pump();
      expect(find.text('004218'), findsOneWidget);
      expect(find.text('00421899'), findsNothing);

      harness.dispose();
    });

    testWidgets('3. Successful pairing submits request and shows success view with active profile',
        (tester) async {
      late String receivedCode;
      late String receivedDeviceName;

      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/pair') {
            final body = json.decode(req.body) as Map<String, dynamic>;
            receivedCode = body['pairing_code'] as String;
            receivedDeviceName = body['device_name'] as String;

            final jwt = _buildTestJwt(patientId: 'patient_test_42', deviceId: 'dev_test_99');
            final resp = {
              'access_token': jwt,
              'token_type': 'Bearer',
              'expires_in': 3600,
              'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
              'refresh_token': 'rt_secret_token_abc123',
              'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'patient_test_42',
              'device_id': 'dev_test_99',
              'scope': 'vitals:write vitals:read',
            };
            return http.Response(json.encode(resp), 200, headers: {'content-type': 'application/json'});
          }
          return http.Response('{"detail": "Not found"}', 404);
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.enterText(find.byKey(const Key('device_name_input')), 'Pixel 8 Pro');
      await tester.pump();

      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pump(); // Render in-flight state

      expect(receivedCode, equals('004218'));
      expect(receivedDeviceName, equals('Pixel 8 Pro'));

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('pairing_success_banner')), findsOneWidget);
      expect(find.text('DEVICE PAIRED SUCCESSFULLY'), findsOneWidget);
      expect(find.textContaining('patient_test_42'), findsOneWidget);
      expect(harness.authManager.isAuthenticated, isTrue);
      expect(harness.authManager.confirmedAttributionPatientId, equals('patient_test_42'));

      harness.dispose();
    });

    testWidgets('4. 401 response safely displays invalid code message and clears input',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/pair') {
            return http.Response(
              json.encode({'detail': 'Invalid or unacceptable pairing credentials.'}),
              401,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('{"detail": "Not found"}', 404);
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '123456');
      await tester.pump();

      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);
      expect(
        find.textContaining('Invalid, expired, or already used pairing code'),
        findsOneWidget,
      );
      expect(find.text('123456'), findsNothing);
      expect(harness.authManager.isAuthenticated, isFalse);

      harness.dispose();
    });

    testWidgets('5. 429 response safely activates rate limit banner and disables submit',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/pair') {
            return http.Response(
              json.encode({'detail': 'Too many failed pairing attempts. Please retry later.'}),
              429,
              headers: {
                'content-type': 'application/json',
                'retry-after': '45',
              },
            );
          }
          return http.Response('{"detail": "Not found"}', 404);
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '999999');
      await tester.pump();

      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pump();

      expect(find.byKey(const Key('pairing_rate_limit_banner')), findsOneWidget);
      expect(find.textContaining('Rate limit active'), findsOneWidget);

      final pairButton = tester.widget<ElevatedButton>(find.byKey(const Key('pair_device_button')));
      expect(pairButton.onPressed, isNull, reason: 'Submit must be disabled during cooldown');

      harness.dispose();
    });

    testWidgets('6. Existing session shows paired management view with diagnostics and locked re-pair',
        (tester) async {
      final initialSession = AuthSession(
        accessToken: _buildTestJwt(patientId: 'patient_alpha', deviceId: 'device_alpha'),
        refreshToken: 'rt_existing_123',
        expiresIn: 3600,
        expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'patient_alpha',
        deviceId: 'device_alpha',
        scope: 'vitals:write vitals:read',
      );

      final harness = await PairingTestHarness.create(
        handler: (req) async => http.Response('{}', 200),
        initialSession: initialSession,
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      expect(find.byKey(const Key('paired_status_view')), findsOneWidget);
      expect(find.text('AEGIS CLOUD PROFILE'), findsOneWidget);
      expect(find.textContaining('Connected & Active'), findsOneWidget);
      expect(find.textContaining('patient_alpha'), findsOneWidget);
      expect(find.text('QUEUE DIAGNOSTICS'), findsOneWidget);
      expect(find.byKey(const Key('disconnect_device_button')), findsOneWidget);

      final switchBtn = tester.widget<OutlinedButton>(find.byKey(const Key('switch_profile_button')));
      expect(switchBtn.onPressed, isNull, reason: 'Atomic re-pair without disconnect is disabled');

      harness.dispose();
    });

    testWidgets('7. Disconnect shows confirmation dialog and revokes session on confirm',
        (tester) async {
      bool revokeCalled = false;
      final initialSession = AuthSession(
        accessToken: _buildTestJwt(patientId: 'patient_alpha', deviceId: 'device_alpha'),
        refreshToken: 'rt_existing_123',
        expiresIn: 3600,
        expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'patient_alpha',
        deviceId: 'device_alpha',
        scope: 'vitals:write vitals:read',
      );

      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/revoke') {
            revokeCalled = true;
            return http.Response(
              json.encode({'status': 'revoked', 'device_id': 'device_alpha', 'revoked': true}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('{}', 200);
        },
        initialSession: initialSession,
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.tap(find.byKey(const Key('disconnect_device_button')));
      await tester.pumpAndSettle();

      expect(find.text('Disconnect Profile?'), findsOneWidget);
      expect(find.byKey(const Key('cancel_disconnect_button')), findsOneWidget);
      expect(find.byKey(const Key('confirm_disconnect_button')), findsOneWidget);

      await tester.tap(find.byKey(const Key('confirm_disconnect_button')));
      await tester.pumpAndSettle();

      expect(revokeCalled, isTrue);
      expect(harness.authManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(find.text('PAIR AEGIS PROFILE'), findsOneWidget);

      harness.dispose();
    });
  });

  // =========================================================================
  // GROUP 2: CRITICAL SECURITY & PRIVACY INVARIANTS (P1–P7)
  // =========================================================================
  group('Stage 5.3C-3B-3B: Security & Privacy Invariants (P1–P7)', () {
    testWidgets('P1: Code Privacy — pairing code and raw server exceptions never leak into UI',
        (tester) async {
      const sensitiveCode = '042918';
      const rawServerTrace = 'FATAL_TRACE: secret_db_key_at_line_4281';

      final harness = await PairingTestHarness.create(
        handler: (req) async {
          return http.Response(
            json.encode({
              'detail': rawServerTrace,
              'internal_error': 'db_crash',
            }),
            500,
            headers: {'content-type': 'application/json'},
          );
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), sensitiveCode);
      await tester.pump();

      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);

      // Verify that raw exception and internal trace do NOT appear in the widget tree
      expect(find.textContaining(rawServerTrace), findsNothing);
      expect(find.textContaining('db_crash'), findsNothing);

      // Verify error message is sanitized and neutral
      expect(
        find.textContaining('Connection to Aegis server failed'),
        findsOneWidget,
      );

      harness.dispose();
    });

    testWidgets('P2: Token Privacy — raw JWT and refresh tokens never appear in UI',
        (tester) async {
      const confidentialJwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwYXRpZW50X3g5OSJ9.c2lnbmF0dXJlX3hzZWNyZXQ';
      const confidentialRefreshToken = 'rt_super_secret_refresh_token_vault_token_999';

      final harness = await PairingTestHarness.create(
        handler: (req) async {
          final resp = {
            'access_token': confidentialJwt,
            'token_type': 'Bearer',
            'expires_in': 3600,
            'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
            'refresh_token': confidentialRefreshToken,
            'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
            'patient_id': 'patient_confidential_99',
            'device_id': 'device_confidential_77',
            'scope': 'vitals:write vitals:read',
          };
          return http.Response(json.encode(resp), 200, headers: {'content-type': 'application/json'});
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      // UI must show success view
      expect(find.byKey(const Key('pairing_success_banner')), findsOneWidget);

      // Invariant: Raw JWT tokens and raw refresh tokens must NEVER exist as visible text widgets
      expect(find.textContaining('eyJhbGciOiJIUzI1NiJ9'), findsNothing);
      expect(find.textContaining(confidentialRefreshToken), findsNothing);
      expect(find.textContaining('c2lnbmF0dXJl'), findsNothing);

      harness.dispose();
    });

    testWidgets('P3: Code Format & Double-Tap Guard — blocks double submission in-flight',
        (tester) async {
      int pairCalls = 0;
      final completer = Completer<http.Response>();

      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/pair') {
            pairCalls++;
            return completer.future;
          }
          return http.Response('{}', 200);
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.pump();

      // First tap
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pump(); // In-flight state rendered

      expect(pairCalls, equals(1));
      expect(find.byKey(const Key('pairing_in_flight_indicator')), findsOneWidget);

      // Attempt second tap during in-flight: button must be disabled
      final pairBtn = tester.widget<ElevatedButton>(find.byKey(const Key('pair_device_button')));
      expect(pairBtn.onPressed, isNull, reason: 'Double submission must be impossible while in-flight');

      // Complete HTTP request
      final jwt = _buildTestJwt(patientId: 'patient_dt', deviceId: 'dev_dt');
      completer.complete(
        http.Response(
          json.encode({
            'access_token': jwt,
            'token_type': 'Bearer',
            'expires_in': 3600,
            'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
            'refresh_token': 'rt_dt_123',
            'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
            'patient_id': 'patient_dt',
            'device_id': 'dev_dt',
            'scope': 'vitals:write vitals:read',
          }),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );

      await tester.pumpAndSettle();
      expect(pairCalls, equals(1), reason: 'Only exactly one pair call must have been made');
      expect(find.byKey(const Key('pairing_success_banner')), findsOneWidget);

      harness.dispose();
    });

    testWidgets('P4: Quarantine Protection — pre-pairing measurements remain strictly quarantined',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/pair') {
            final jwt = _buildTestJwt(patientId: 'patient_quarantine_test', deviceId: 'dev_q');
            return http.Response(
              json.encode({
                'access_token': jwt,
                'token_type': 'Bearer',
                'expires_in': 3600,
                'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
                'refresh_token': 'rt_q_123',
                'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
                'patient_id': 'patient_quarantine_test',
                'device_id': 'dev_q',
                'scope': 'vitals:write vitals:read',
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (req.url.path == '/api/v1/cycle/current') {
            return http.Response(json.encode({'cycle_id': 'cycle_q_active'}), 200);
          }
          return http.Response('{}', 200);
        },
      );

      // Pre-pairing: Dispatch vitals while unauthenticated
      await harness.healthProvider.dispatchManualInput(
        pulse: 78,
        systolic: 122,
        diastolic: 81,
        spo2: 99,
      );

      // Verify that measurement was safely quarantined
      expect(harness.syncQueue.quarantinedCount, equals(1));
      expect(harness.syncQueue.activePatientPendingItems.length, equals(0));
      expect(harness.syncQueue.quarantinedItems.first.patientId, isNull);

      // Now pair device through UI
      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      expect(harness.authManager.isAuthenticated, isTrue);

      // P4 Invariant: Pre-pairing data must NEVER be attributed to new patient
      expect(harness.syncQueue.quarantinedCount, equals(1));
      expect(harness.syncQueue.activePatientPendingItems.length, equals(0));
      expect(harness.syncQueue.quarantinedItems.first.patientId, isNull);

      harness.dispose();
    });

    testWidgets('P5: Patient Switch Isolation — Disconnect Patient A -> Pair Patient B preserves patient isolation',
        (tester) async {
      final sessionA = AuthSession(
        accessToken: _buildTestJwt(patientId: 'patient_A', deviceId: 'dev_A'),
        refreshToken: 'rt_A_123',
        expiresIn: 3600,
        expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'patient_A',
        deviceId: 'dev_A',
        scope: 'vitals:write vitals:read',
      );

      final harness = await PairingTestHarness.create(
        initialSession: sessionA,
        handler: (req) async {
          if (req.url.path == '/api/v1/auth/revoke') {
            return http.Response(
              json.encode({'status': 'revoked', 'device_id': 'dev_A', 'revoked': true}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (req.url.path == '/api/v1/auth/pair') {
            final jwtB = _buildTestJwt(patientId: 'patient_B', deviceId: 'dev_B');
            return http.Response(
              json.encode({
                'access_token': jwtB,
                'token_type': 'Bearer',
                'expires_in': 3600,
                'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
                'refresh_token': 'rt_B_123',
                'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
                'patient_id': 'patient_B',
                'device_id': 'dev_B',
                'scope': 'vitals:write vitals:read',
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('{}', 200);
        },
      );

      // Verify Patient A active and enqueue pending item for Patient A
      expect(harness.authManager.confirmedAttributionPatientId, equals('patient_A'));
      await harness.healthProvider.dispatchManualInput(pulse: 74, systolic: 120, diastolic: 80, spo2: 98);
      expect(harness.syncQueue.pendingCount, equals(1));
      expect(harness.syncQueue.pendingItems.first.patientId, equals('patient_A'));

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      // Paired state for Patient A is displayed
      expect(find.byKey(const Key('paired_status_view')), findsOneWidget);
      expect(find.textContaining('patient_A'), findsOneWidget);

      // 1. Disconnect Patient A
      await tester.tap(find.byKey(const Key('disconnect_device_button')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirm_disconnect_button')));
      await tester.pumpAndSettle();

      expect(harness.authManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(find.text('PAIR AEGIS PROFILE'), findsOneWidget);

      // 2. Intermediate measurement before B is paired -> must be quarantined
      await harness.healthProvider.dispatchManualInput(pulse: 70, systolic: 120, diastolic: 80, spo2: 98);
      expect(harness.syncQueue.quarantinedCount, equals(1));

      // 3. Pair Patient B
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      // P5 Invariants:
      expect(harness.authManager.confirmedAttributionPatientId, equals('patient_B'));
      expect(find.textContaining('patient_B'), findsOneWidget);
      // Quarantined item from intermediate state must remain in quarantine
      expect(harness.syncQueue.quarantinedCount, equals(1));
      // Patient A's pending item remains isolated and is NOT active for Patient B
      expect(harness.syncQueue.activePatientPendingItems.length, equals(0));
      expect(harness.syncQueue.pendingItems.any((i) => i.patientId == 'patient_A'), isTrue);

      harness.dispose();
    });

    testWidgets('P6: Fail-Closed Invariant — network/422/503 errors never create partial sessions',
        (tester) async {
      int testCase = 0;

      final harness = await PairingTestHarness.create(
        handler: (req) async {
          if (testCase == 0) {
            // 422 format error
            return http.Response(
              json.encode({'detail': 'Validation failed: code length must be 6'}),
              422,
              headers: {'content-type': 'application/json'},
            );
          } else if (testCase == 1) {
            // 503 service unavailable
            return http.Response(
              json.encode({'detail': 'Service temporarily unavailable'}),
              503,
              headers: {'content-type': 'application/json'},
            );
          } else {
            // Network connection drop
            throw http.ClientException('Connection refused');
          }
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      // Test Case 0: 422
      testCase = 0;
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '111111');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      expect(harness.authManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(harness.authManager.currentSession, isNull);
      expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);

      // Test Case 1: 503
      testCase = 1;
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '222222');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      expect(harness.authManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(harness.authManager.currentSession, isNull);
      expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);

      // Test Case 2: Network drop
      testCase = 2;
      await tester.enterText(find.byKey(const Key('pairing_code_input')), '333333');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pumpAndSettle();

      expect(harness.authManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(harness.authManager.currentSession, isNull);
      expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);

      harness.dispose();
    });

    testWidgets('P7: Rate Limiting & Cooldown Lifecycle — respects Retry-After, unlocks after cooldown',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async {
          return http.Response(
            json.encode({'detail': 'Rate limited'}),
            429,
            headers: {'content-type': 'application/json', 'retry-after': '3'},
          );
        },
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '999999');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pump(); // Triggers 429 and starts 3s cooldown

      expect(find.byKey(const Key('pairing_rate_limit_banner')), findsOneWidget);
      final initialButton = tester.widget<ElevatedButton>(find.byKey(const Key('pair_device_button')));
      expect(initialButton.onPressed, isNull, reason: 'Must be disabled during cooldown');

      // Deterministically advance time by 2 seconds (still locked)
      await tester.pump(const Duration(seconds: 2));
      final midButton = tester.widget<ElevatedButton>(find.byKey(const Key('pair_device_button')));
      expect(midButton.onPressed, isNull, reason: 'Must still be disabled at 2s');

      // Advance by another 2 seconds (cooldown expires: 2 + 2 = 4s > 3s)
      await tester.pump(const Duration(seconds: 2));
      final unlockedButton = tester.widget<ElevatedButton>(find.byKey(const Key('pair_device_button')));
      expect(unlockedButton.onPressed, isNotNull, reason: 'Must be enabled after cooldown completes');
      expect(find.byKey(const Key('pairing_rate_limit_banner')), findsNothing);

      harness.dispose();
    });
  });

  // =========================================================================
  // GROUP 3: UI INTEGRATION & LIFECYCLE INVARIANTS (L1–L6)
  // =========================================================================
  group('Stage 5.3C-3B-3B: UI Integration & Lifecycle Invariants (L1–L6)', () {
    testWidgets('L1: Modal launches seamlessly from VitalsHeaderCard.showQuickEditModal via Aegis Cloud Profile tile',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async => http.Response('{}', 200),
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: Builder(
            builder: (ctx) => ElevatedButton(
              key: const Key('open_quick_edit_button'),
              onPressed: () => VitalsHeaderCard.showQuickEditModal(ctx),
              child: const Text('Open Vitals Sheet'),
            ),
          ),
        ),
      );

      // Open quick edit sheet
      await tester.tap(find.byKey(const Key('open_quick_edit_button')));
      await tester.pumpAndSettle();

      // Find Aegis Cloud Profile tile and its button
      expect(find.text('Aegis Cloud Profile'), findsOneWidget);
      expect(find.byKey(const Key('open_pairing_modal_button')), findsOneWidget);

      // Tap Pair on the tile
      await tester.tap(find.byKey(const Key('open_pairing_modal_button')));
      await tester.pumpAndSettle();

      // AegisDevicePairingModal is opened
      expect(find.text('PAIR AEGIS PROFILE'), findsOneWidget);
      expect(find.byKey(const Key('pairing_code_input')), findsOneWidget);

      harness.dispose();
    });

    testWidgets('L2: Smart Watch oval dot reacts dynamically to auth status changes',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async => http.Response('{}', 200),
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const VitalsHeaderCard(),
        ),
      );

      // 1. Unauthenticated state: dot is white24
      final dotFinder = find.byKey(const Key('vitals_card_auth_dot'));
      expect(dotFinder, findsOneWidget);
      Container dotContainer = tester.widget(dotFinder);
      BoxDecoration deco = dotContainer.decoration as BoxDecoration;
      expect(deco.color, equals(Colors.white24));

      // 2. Transition to authenticated: dot becomes greenAccent
      harness.authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_dot', deviceId: 'dev_dot'),
          refreshToken: 'rt_dot',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_dot',
          deviceId: 'dev_dot',
          scope: 'vitals:write vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await tester.pump();

      dotContainer = tester.widget(dotFinder);
      deco = dotContainer.decoration as BoxDecoration;
      expect(deco.color, equals(Colors.greenAccent));

      // 3. Transition to expired: dot becomes amberAccent
      harness.authManager.setSessionForTesting(
        harness.authManager.currentSession,
        status: AuthSessionStatus.expired,
      );
      await tester.pump();

      dotContainer = tester.widget(dotFinder);
      deco = dotContainer.decoration as BoxDecoration;
      expect(deco.color, equals(Colors.amberAccent));

      harness.dispose();
    });

    testWidgets('L3: Compact screen with keyboard insets renders without RenderFlex overflow',
        (tester) async {
      final harness = await PairingTestHarness.create(
        handler: (req) async => http.Response('{}', 200),
      );

      tester.view.physicalSize = const Size(360, 640);
      tester.view.devicePixelRatio = 1.0;

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const MediaQuery(
            data: MediaQueryData(
              size: Size(360, 640),
              viewInsets: EdgeInsets.only(bottom: 260), // Keyboard simulated
            ),
            child: AegisDevicePairingModal(),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Invariant: Must render without Flutter error or overflow
      expect(find.byKey(const Key('pairing_code_input')), findsOneWidget);
      expect(find.byKey(const Key('pair_device_button')), findsOneWidget);
      expect(tester.takeException(), isNull);

      // Reset test surface
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
      harness.dispose();
    });

    testWidgets('L4: Closing modal while pairing is in-flight handles cancellation without setState after dispose',
        (tester) async {
      final completer = Completer<http.Response>();

      final harness = await PairingTestHarness.create(
        handler: (req) async => completer.future,
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: Builder(
            builder: (ctx) => ElevatedButton(
              key: const Key('open_sheet_btn'),
              onPressed: () => AegisDevicePairingModal.show(ctx),
              child: const Text('Open Sheet'),
            ),
          ),
        ),
      );

      await tester.tap(find.byKey(const Key('open_sheet_btn')));
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('pairing_code_input')), '004218');
      await tester.pump();
      await tester.tap(find.byKey(const Key('pair_device_button')));
      await tester.pump(); // In-flight

      // Close modal before server responds
      Navigator.of(tester.element(find.byKey(const Key('pairing_code_input')))).pop();
      await tester.pumpAndSettle();

      // Complete HTTP request after modal is disposed
      final jwt = _buildTestJwt(patientId: 'patient_late', deviceId: 'dev_late');
      completer.complete(
        http.Response(
          json.encode({
            'access_token': jwt,
            'token_type': 'Bearer',
            'expires_in': 3600,
            'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
            'refresh_token': 'rt_late',
            'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
            'patient_id': 'patient_late',
            'device_id': 'dev_late',
            'scope': 'vitals:write vitals:read',
          }),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );

      await tester.pump();

      // Invariant: No setState after dispose exception
      expect(tester.takeException(), isNull);

      harness.dispose();
    });

    testWidgets('L5: Canceling disconnect dialog preserves active session state',
        (tester) async {
      final initialSession = AuthSession(
        accessToken: _buildTestJwt(patientId: 'patient_cancel', deviceId: 'dev_cancel'),
        refreshToken: 'rt_cancel',
        expiresIn: 3600,
        expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'patient_cancel',
        deviceId: 'dev_cancel',
        scope: 'vitals:write vitals:read',
      );

      final harness = await PairingTestHarness.create(
        initialSession: initialSession,
        handler: (req) async => http.Response('{}', 200),
      );

      await tester.pumpWidget(
        _wrapWidget(
          harness: harness,
          child: const AegisDevicePairingModal(),
        ),
      );

      expect(find.byKey(const Key('paired_status_view')), findsOneWidget);

      // Open Disconnect dialog
      await tester.tap(find.byKey(const Key('disconnect_device_button')));
      await tester.pumpAndSettle();

      // Tap Cancel in dialog
      await tester.tap(find.byKey(const Key('cancel_disconnect_button')));
      await tester.pumpAndSettle();

      // Invariant: Session remains fully active and authenticated
      expect(harness.authManager.status, equals(AuthSessionStatus.authenticated));
      expect(harness.authManager.confirmedAttributionPatientId, equals('patient_cancel'));
      expect(find.byKey(const Key('paired_status_view')), findsOneWidget);

      harness.dispose();
    });
  });
}
