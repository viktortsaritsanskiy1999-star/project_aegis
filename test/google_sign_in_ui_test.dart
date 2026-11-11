import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_models.dart' show AuthSessionStatus;
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/auth/google_auth_service.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/widgets/aegis_device_pairing_modal.dart';

/// Mock implementation of [IGoogleAuthService] for deterministic UI testing.
class MockGoogleAuthService implements IGoogleAuthService {
  String? Function()? onSignIn;
  bool wasSignedOut = false;

  @override
  Future<String?> signInAndGetIdToken() async {
    if (onSignIn != null) {
      return onSignIn!();
    }
    return 'mock.google.id_token_payload';
  }

  @override
  Future<void> signOut() async {
    wasSignedOut = true;
  }
}

/// Helper that creates a test JWT string.
String _buildTestJwt({
  required String patientId,
  required String deviceId,
  String scope = 'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write',
  int expOffset = 3600,
}) {
  final header = base64Url.encode(utf8.encode(json.encode({'alg': 'HS256', 'typ': 'JWT'}))).replaceAll('=', '');
  final payload = base64Url.encode(utf8.encode(json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'device_id': deviceId,
    'scope': scope,
    'type': 'access',
    'exp': DateTime.now().toUtc().millisecondsSinceEpoch ~/ 1000 + expOffset,
  }))).replaceAll('=', '');
  return '$header.$payload.mock_signature_test';
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late MockGoogleAuthService mockGoogleAuth;

  setUp(() {
    mockGoogleAuth = MockGoogleAuthService();
    GoogleAuthService.instance = mockGoogleAuth;
  });

  tearDown(() {
    GoogleAuthService.instance = null;
  });

  Widget buildTestApp({
    required AuthSessionManager authManager,
    required HealthDataProvider healthProvider,
  }) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
        ChangeNotifierProvider<HealthDataProvider>.value(value: healthProvider),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) {
              return ElevatedButton(
                key: const Key('open_modal_button'),
                onPressed: () => AegisDevicePairingModal.show(context),
                child: const Text('OPEN MODAL'),
              );
            },
          ),
        ),
      ),
    );
  }

  testWidgets('1. Continue with Google button is rendered alongside 6-digit code entry', (tester) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});

    final storage = InMemoryAuthTokenStorage();
    final mockClient = MockClient((request) async => http.Response('{}', 200));
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitor = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(apiClient: apiClient, authSessionManager: authManager);
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitor,
      syncQueue: syncQueue,
    );

    await tester.pumpWidget(buildTestApp(authManager: authManager, healthProvider: healthProvider));
    await tester.tap(find.byKey(const Key('open_modal_button')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('continue_with_google_button')), findsOneWidget);
    expect(find.text('Continue with Google'), findsOneWidget);
    expect(find.byKey(const Key('pairing_code_input')), findsOneWidget);
    expect(find.byKey(const Key('pair_device_button')), findsOneWidget);

    authManager.dispose();
  });

  testWidgets('2. User cancellation of Google Sign-In does not show error and leaves modal ready', (tester) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});

    mockGoogleAuth.onSignIn = () => null; // User cancelled account picker

    final storage = InMemoryAuthTokenStorage();
    final mockClient = MockClient((request) async => http.Response('{}', 200));
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitor = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(apiClient: apiClient, authSessionManager: authManager);
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitor,
      syncQueue: syncQueue,
    );

    await tester.pumpWidget(buildTestApp(authManager: authManager, healthProvider: healthProvider));
    await tester.tap(find.byKey(const Key('open_modal_button')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('continue_with_google_button')));
    await tester.pumpAndSettle();

    // No error banner, still on unpaired view, button enabled
    expect(find.byKey(const Key('pairing_error_banner')), findsNothing);
    expect(find.byKey(const Key('continue_with_google_button')), findsOneWidget);
    expect(authManager.status, AuthSessionStatus.unauthenticated);

    authManager.dispose();
  });

  testWidgets('3. Successful Google Sign-In authenticates device, transitions UI, and never stores Google ID Token', (tester) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});

    const mockGoogleIdToken = 'header.google_identity_payload_mock_secret.sig';
    mockGoogleAuth.onSignIn = () => mockGoogleIdToken;

    final testJwt = _buildTestJwt(
      patientId: 'patient_goog_777',
      deviceId: 'dev_goog_999',
    );

    final mockClient = MockClient((request) async {
      if (request.url.path == '/api/v1/auth/google') {
        final body = json.decode(request.body) as Map<String, dynamic>;
        expect(body['id_token'], mockGoogleIdToken);
        expect(body['platform'], 'mobile');

        return http.Response(
          json.encode({
            'access_token': testJwt,
            'token_type': 'Bearer',
            'expires_in': 1800,
            'expires_at': DateTime.now().toUtc().add(const Duration(minutes: 30)).toIso8601String(),
            'refresh_token': 'aegis_rt_google_pair_refresh_token_123',
            'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
            'patient_id': 'patient_goog_777',
            'device_id': 'dev_goog_999',
            'scope': 'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write',
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      }
      return http.Response('Not found', 404);
    });

    final storage = InMemoryAuthTokenStorage();
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitor = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(apiClient: apiClient, authSessionManager: authManager);
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitor,
      syncQueue: syncQueue,
    );

    await tester.pumpWidget(buildTestApp(authManager: authManager, healthProvider: healthProvider));
    await tester.tap(find.byKey(const Key('open_modal_button')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('continue_with_google_button')));
    await tester.pump(); // Start async work
    await tester.pumpAndSettle();

    // Verification: Success banner is displayed
    expect(find.byKey(const Key('pairing_success_banner')), findsOneWidget);
    expect(find.text('DEVICE PAIRED SUCCESSFULLY'), findsOneWidget);
    expect(find.textContaining('patient_goog_777'), findsOneWidget);

    // Manager state
    expect(authManager.status, AuthSessionStatus.authenticated);
    expect(authManager.currentSession?.patientId, 'patient_goog_777');
    expect(authManager.currentSession?.deviceId, 'dev_goog_999');

    // Storage verification: Contains Aegis session
    final storedSession = await storage.loadSession();
    expect(storedSession, isNotNull);
    expect(storedSession!.accessToken, testJwt);
    expect(storedSession.refreshToken, 'aegis_rt_google_pair_refresh_token_123');

    // CRITICAL SECURITY INVARIANT: Google ID Token is NEVER saved in persistent storage
    final rawStoredJson = json.encode(storedSession.toJson());
    expect(rawStoredJson.contains(mockGoogleIdToken), isFalse);

    authManager.dispose();
  });

  testWidgets('4. Closed Beta restriction (403 Forbidden) displays clear message without authentication', (tester) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});

    mockGoogleAuth.onSignIn = () => 'header.unwhitelisted_user.sig';

    final mockClient = MockClient((request) async {
      if (request.url.path == '/api/v1/auth/google') {
        return http.Response(
          json.encode({
            'detail': 'Access denied. Project Aegis is currently in closed beta. Please request access from an administrator.',
          }),
          403,
          headers: {'content-type': 'application/json'},
        );
      }
      return http.Response('Not found', 404);
    });

    final storage = InMemoryAuthTokenStorage();
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitor = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(apiClient: apiClient, authSessionManager: authManager);
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitor,
      syncQueue: syncQueue,
    );

    await tester.pumpWidget(buildTestApp(authManager: authManager, healthProvider: healthProvider));
    await tester.tap(find.byKey(const Key('open_modal_button')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('continue_with_google_button')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);
    expect(find.textContaining('closed beta'), findsOneWidget);
    expect(authManager.status, AuthSessionStatus.unauthenticated);

    final storedSession = await storage.loadSession();
    expect(storedSession, isNull);

    authManager.dispose();
  });

  testWidgets('5. Server 401 Unauthorized displays Google authentication rejected message', (tester) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});

    mockGoogleAuth.onSignIn = () => 'header.bad_token.sig';

    final mockClient = MockClient((request) async {
      return http.Response(
        json.encode({'detail': 'Invalid Google ID token'}),
        401,
        headers: {'content-type': 'application/json'},
      );
    });

    final storage = InMemoryAuthTokenStorage();
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitor = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(apiClient: apiClient, authSessionManager: authManager);
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitor,
      syncQueue: syncQueue,
    );

    await tester.pumpWidget(buildTestApp(authManager: authManager, healthProvider: healthProvider));
    await tester.tap(find.byKey(const Key('open_modal_button')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('continue_with_google_button')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);
    expect(find.textContaining('rejected by server'), findsOneWidget);
    expect(authManager.status, AuthSessionStatus.unauthenticated);

    authManager.dispose();
  });

  testWidgets('6. Network failure gracefully handled with connection error message', (tester) async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});

    mockGoogleAuth.onSignIn = () => 'header.net_token.sig';

    final mockClient = MockClient((request) async {
      throw http.ClientException('Connection refused');
    });

    final storage = InMemoryAuthTokenStorage();
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
    await authManager.initialize();

    final timeService = TimeService();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitor = VitalsMonitorService(timeService, vaultService);
    final syncQueue = VitalsSyncQueue(apiClient: apiClient, authSessionManager: authManager);
    final healthProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitor,
      syncQueue: syncQueue,
    );

    await tester.pumpWidget(buildTestApp(authManager: authManager, healthProvider: healthProvider));
    await tester.tap(find.byKey(const Key('open_modal_button')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('continue_with_google_button')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('pairing_error_banner')), findsOneWidget);
    expect(find.textContaining('Connection to Aegis server failed'), findsOneWidget);
    expect(authManager.status, AuthSessionStatus.unauthenticated);

    authManager.dispose();
  });
}
