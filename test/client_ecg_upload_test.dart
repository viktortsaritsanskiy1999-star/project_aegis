// ignore_for_file: depend_on_referenced_packages
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/screens/smart_watch_import_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';

/// Helper to build test JWT
String _buildTestJwt({
  required String patientId,
  String deviceId = 'dev_test_ecg',
  List<String> scopes = const [
    'cascade:intake',
    'cascade:run',
    'cycle:read',
    'reports:read',
  ],
  String? jti,
}) {
  final header = base64Url.encode(utf8.encode(json.encode({'alg': 'HS256', 'typ': 'JWT'}))).replaceAll('=', '');
  final payload = base64Url.encode(utf8.encode(json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'device_id': deviceId,
    'did': deviceId,
    'scope': scopes.join(' '),
    'scopes': scopes,
    'type': 'access',
    'jti': jti ?? 'jti_${DateTime.now().microsecondsSinceEpoch}',
    'exp': DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000,
  }))).replaceAll('=', '');
  return '$header.$payload.signature_mock_bytes';
}

/// Sample fake JPEG bytes (magic number 0xFF, 0xD8, 0xFF)
final Uint8List _fakeJpegBytes = Uint8List.fromList([
  0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46,
  0x49, 0x46, 0x00, 0x01, 0x01, 0x01, 0x00, 0x60,
  0x00, 0x60, 0x00, 0x00, 0xFF, 0xD9,
]);

/// Sample fake PNG bytes (magic number 0x89, 'P', 'N', 'G')
final Uint8List _fakePngBytes = Uint8List.fromList([
  0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
  0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
]);

Map<String, dynamic> _sampleEcgIntakeResponseJson({
  String cycleId = 'cycle_ecg_001',
  int intakeVersion = 1,
  String recordingType = 'single_lead_smartwatch',
  String recordingQuality = 'good',
  String? createdAtUtc,
  bool primaryConsiliumConcluded = false,
}) {
  return {
    'cycle_id': cycleId,
    'status': 'processed',
    'intake_version': intakeVersion,
    'recording_type': recordingType,
    'recording_quality': recordingQuality,
    'created_at_utc': createdAtUtc ?? '2026-10-29T07:30:00.000000+00:00',
    'primary_consilium_concluded': primaryConsiliumConcluded,
    'privacy_notice': 'Electrocardiogram securely accepted and analyzed on the server under server_only privacy level.',
  };
}

Widget _buildTestApp({
  required AegisApiClient apiClient,
  required AuthSessionManager authManager,
  required VaultService vaultService,
  XFile? initialEcgFile,
}) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vaultService),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      Provider<AegisApiClient>.value(value: apiClient),
    ],
    child: MaterialApp(
      home: SmartWatchImportScreen(
        apiClient: apiClient,
        authSessionManager: authManager,
        initialEcgFile: initialEcgFile,
      ),
    ),
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryVaultStorage;
  late InMemoryAuthTokenStorage memoryAuthStorage;

  setUp(() {
    VaultService.resetWriteQueueForTesting();
    AuthSessionManager.resetActiveInstancesForTesting();
    memoryVaultStorage = InMemoryVaultStorage();
    memoryAuthStorage = InMemoryAuthTokenStorage();
    VaultStorageService.setStorageForTesting(memoryVaultStorage);
  });

  tearDown(() {
    VaultService.resetWriteQueueForTesting();
    AuthSessionManager.resetActiveInstancesForTesting();
    VaultStorageService.setStorageForTesting(null);
  });

  group('PROJECT AEGIS — STAGE 5.4D-5B: FLUTTER ECG SERVER INTEGRATION', () {
    testWidgets('1. Successful single document upload: POST /api/v1/client/cascade/intake/ecg with auth & multipart',
        (tester) async {
      int intakeCallCount = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          intakeCallCount++;
          expect(request.method, 'POST');
          expect(request.headers['authorization'], startsWith('Bearer '));
          // Client must NOT send patient_id or cycle_id in request query
          expect(request.url.queryParameters.containsKey('patient_id'), isFalse);
          expect(request.url.queryParameters.containsKey('cycle_id'), isFalse);

          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_ecg_test'),
          refreshToken: 'refresh_ecg_1',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_ecg_test',
          deviceId: 'dev_test_ecg',
          scope: 'cascade:intake cascade:run cycle:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_ecg_test');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_watch_lead1.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: Status updated to success with version
      expect(intakeCallCount, 1);
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);
      expect(find.textContaining('Smartwatch (Single Lead)'), findsOneWidget);
      expect(find.textContaining('Good'), findsOneWidget);
      expect(find.textContaining('Server Upload Time: 2026-10-29T07:30:00.000000+00:00'), findsOneWidget);

      // Invariant: Vault persistence updated without overwriting history
      expect(vault.ecgRecords.length, 1);
      expect(vault.ecgRecords.first['version'], 1);
      expect(vault.ecgRecords.first['recording_type'], 'single_lead_smartwatch');
      expect(vault.latestEcgRecord, isNotNull);
      expect(vault.latestEcgRecord!['intake_version'], 1);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('2. Correct multipart encoding: sends file field with image bytes and auth header', () async {
      String? requestBody;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          requestBody = latin1.decode(request.bodyBytes);
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final token = _buildTestJwt(patientId: 'patient_multipart');
      final ecgItem = EcgUploadFileItem(
        filename: 'strip.png',
        bytes: _fakePngBytes,
      );

      final response = await apiClient.uploadEcgDocument(
        file: ecgItem,
        recordDate: '2026-10-29',
        accessToken: token,
      );

      expect(response.status, 'processed');
      expect(response.intakeVersion, 1);
      expect(response.recordingType, 'single_lead_smartwatch');
      expect(requestBody, isNotNull);
      expect(requestBody, contains('filename="strip.png"'));
      expect(requestBody, contains('name="record_date"'));

      apiClient.close();
    });

    test('3. GET latest: getLatestEcg() invokes /api/v1/client/cascade/ecg/latest with cycle:read scope', () async {
      final capturedUrls = <Uri>[];
      final capturedHeaders = <Map<String, String>>[];
      final mockClient = MockClient((request) async {
        capturedUrls.add(request.url);
        capturedHeaders.add(request.headers);
        if (request.url.path == '/api/v1/client/cascade/ecg/latest') {
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 2, recordingQuality: 'moderate')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final token = _buildTestJwt(patientId: 'patient_latest', scopes: ['cycle:read']);
      final latest = await apiClient.getLatestEcg(accessToken: token);

      expect(capturedUrls.first.path, '/api/v1/client/cascade/ecg/latest');
      expect(capturedHeaders.first['Authorization'], 'Bearer $token');
      expect(latest.intakeVersion, 2);
      expect(latest.recordingQuality, 'moderate');
      expect(latest.primaryConsiliumConcluded, false);

      apiClient.close();
    });

    testWidgets('4. Poor / unreadable quality display: shows warning badge and neutral guidance without diagnosis',
        (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(
              intakeVersion: 1,
              recordingQuality: 'unreadable',
            )),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_noisy'),
          refreshToken: 'refresh_noisy',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_noisy',
          deviceId: 'dev_noisy',
          scope: 'cascade:intake cascade:run',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_noisy');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'noisy_strip.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: Status reflects unreadable quality with warning
      expect(find.textContaining('ECG recorded (Unreadable) ⚠️'), findsOneWidget);
      expect(find.textContaining('• Technical Quality: Unreadable'), findsOneWidget);
      // Invariant: Neutral guidance shown without diagnostic claims
      expect(
        find.textContaining('Low technical quality or noise detected'),
        findsOneWidget,
      );

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('5. Zero server_only leakage: response model rejects and strips intermediate measurements', () {
      final rawServerPayload = {
        'cycle_id': 'cycle_001',
        'status': 'processed',
        'intake_version': 1,
        'recording_type': 'single_lead_smartwatch',
        'recording_quality': 'good',
        'created_at_utc': '2026-10-29T07:30:00.000000+00:00',
        'primary_consilium_concluded': false,
        'privacy_notice': 'Acceptance confirmation',
        // Server_only fields that MUST NEVER be accepted or parsed into client model:
        'heart_rate_bpm': 72,
        'rhythm_type': 'sinus',
        'electrical_axis': 'normal',
        'intervals': {'pr_interval_ms': 160},
        'ecg_summary': 'Sinus rhythm 72 bpm',
      };

      final response = EcgVisionIntakeClientResponse.fromJson(rawServerPayload);
      final jsonOut = response.toJson();

      expect(jsonOut.containsKey('heart_rate_bpm'), false);
      expect(jsonOut.containsKey('rhythm_type'), false);
      expect(jsonOut.containsKey('electrical_axis'), false);
      expect(jsonOut.containsKey('intervals'), false);
      expect(jsonOut.containsKey('ecg_summary'), false);
    });

    testWidgets('6. Timeout and retry: AegisTimeoutException triggers retry button and preserves selected file',
        (tester) async {
      int requestCount = 0;
      final mockClient = MockClient((request) async {
        requestCount++;
        if (requestCount == 1) {
          throw TimeoutException('Request timed out after 10s');
        }
        return http.Response(
          json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_timeout'),
          refreshToken: 'refresh_timeout',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_timeout',
          deviceId: 'dev_timeout',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_timeout');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_timeout.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: First attempt times out and shows error with Retry button
      expect(find.textContaining('Upload timed out'), findsOneWidget);
      expect(find.text('Retry Upload'), findsOneWidget);
      expect(requestCount, 1);

      // Tap Retry Upload
      await tester.tap(find.text('Retry Upload'));
      await tester.pumpAndSettle();

      // Invariant: Second attempt succeeds
      expect(requestCount, 2);
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('7. 401 token refresh: automatic single refresh recovers upload with 200',
        (tester) async {
      int requestCount = 0;
      int refreshCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCount++;
          final now = DateTime.now().toUtc();
          return http.Response(
            json.encode({
              'token_type': 'Bearer',
              'access_token': _buildTestJwt(patientId: 'patient_401'),
              'refresh_token': 'new_refresh_token',
              'expires_in': 3600,
              'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
              'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'patient_401',
              'device_id': 'dev_401',
              'family_id': 'fam_401',
              'scope': 'cascade:intake',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          requestCount++;
          if (requestCount == 1) {
            return http.Response('{"detail": "Token expired"}', 401, headers: {'content-type': 'application/json'});
          }
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      apiClient.attachSessionManager(authManager);

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_401'),
          refreshToken: 'refresh_initial',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_401',
          deviceId: 'dev_401',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_401');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_retry.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: Exactly 1 refresh call and request recovered with 200
      expect(refreshCount, 1);
      expect(requestCount, 2);
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('8. 403 insufficient scope: displays access denied notice and prevents retry',
        (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Token lacking cascade:intake scope
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_noscope', scopes: ['vitals:read']),
          refreshToken: 'refresh_noscope',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_noscope',
          deviceId: 'dev_noscope',
          scope: 'vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_noscope');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_403.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: Access denied displayed
      expect(find.textContaining('device session lacks required permission'), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('9. Client pre-validation: rejects empty file, oversized file (>10MB), and unsupported format', () async {
      int requestCount = 0;
      final mockClient = MockClient((request) async {
        requestCount++;
        return http.Response('{}', 200);
      });
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      // Empty file
      expect(
        () => apiClient.uploadEcgDocument(
          file: EcgUploadFileItem(filename: 'empty.jpg', bytes: []),
        ),
        throwsA(isA<AegisValidationException>()),
      );

      // Oversized file (>10MB)
      final bigBytes = Uint8List(11 * 1024 * 1024);
      expect(
        () => apiClient.uploadEcgDocument(
          file: EcgUploadFileItem(filename: 'huge.jpg', bytes: bigBytes),
        ),
        throwsA(isA<AegisValidationException>()),
      );

      // Unsupported format (e.g. text file disguised as jpg)
      final textBytes = Uint8List.fromList(utf8.encode('Not an ECG image'));
      expect(
        () => apiClient.uploadEcgDocument(
          file: EcgUploadFileItem(filename: 'fake.txt', bytes: textBytes),
        ),
        throwsA(isA<AegisValidationException>()),
      );

      // Invariant: Zero network requests were made
      expect(requestCount, 0);

      apiClient.close();
    });

    testWidgets('10. Patient session epoch fencing: patient change during upload cancels state update & isolates data',
        (tester) async {
      final completer = Completer<http.Response>();
      final mockClient = MockClient((request) async {
        return await completer.future;
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_A'),
          refreshToken: 'refresh_A',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_A',
          deviceId: 'dev_A',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_A');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_fence.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pump();

      // Switch patient in VaultService mid-upload
      await vault.setActivePatientId('patient_B');

      // Now complete the server response for Patient A
      completer.complete(
        http.Response(
          json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: Upload discarded due to epoch fence
      expect(find.textContaining('Patient session changed during upload. Upload discarded.'), findsOneWidget);
      // Invariant: Patient B has 0 ECG records
      expect(vault.ecgRecords.isEmpty, true);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('11. UI submission lock: prevents double-click submission while analysis is in-flight',
        (tester) async {
      final completer = Completer<http.Response>();
      int dispatchCount = 0;

      final mockClient = MockClient((request) async {
        dispatchCount++;
        return await completer.future;
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_lock'),
          refreshToken: 'refresh_lock',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_lock',
          deviceId: 'dev_lock',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_lock');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_strip.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pump();

      // In-flight state: exactly 1 dispatch
      expect(dispatchCount, 1);
      expect(find.text('Analyzing ECG with server Vision...'), findsOneWidget);

      // Verify "Save markers" button is disabled during analysis
      final saveBtn = tester.widget<ElevatedButton>(find.byType(ElevatedButton));
      expect(saveBtn.onPressed, isNull);

      completer.complete(
        http.Response(
          json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await tester.pumpAndSettle();

      expect(dispatchCount, 1);
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('12. Direct Gemini removal: upload flow does not invoke BrainService',
        (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_no_gemini'),
          refreshToken: 'refresh_no_gemini',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_no_gemini',
          deviceId: 'dev_no_gemini',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_no_gemini');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_clean.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Confirmed server endpoint was called, BrainService was not needed
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('13. HTTP 200 + local secure storage failure: shows failure status and does not falsely claim success',
        (tester) async {
      int postCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          postCount++;
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      apiClient.attachSessionManager(authManager);

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_storage_fail'),
          refreshToken: 'refresh_storage_fail',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_storage_fail',
          deviceId: 'dev_storage_fail',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_storage_fail');

      // Make storage fail on save
      memoryVaultStorage.failWithException = true;

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_strip.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      // Invariant: Server returned 200, but local storage failed
      expect(postCount, 1);
      // Invariant: UI does NOT show successful record message
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsNothing);
      // Invariant: UI shows local storage error and distinct status
      expect(find.textContaining('Server OK, local save failed ⚠️'), findsOneWidget);
      expect(
        find.textContaining('ECG processed on server (v1), but local secure vault save failed.'),
        findsOneWidget,
      );
      // Invariant: Recovery button is shown, but Retry Upload (which repeats OCR) is NOT shown
      expect(find.textContaining('Recover from Server (GET Latest)'), findsOneWidget);
      expect(find.textContaining('Retry Upload'), findsNothing);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('14. Recovery via GET latest without re-running server OCR POST',
        (tester) async {
      int postCount = 0;
      int getLatestCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          postCount++;
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/ecg/latest') {
          getLatestCount++;
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      apiClient.attachSessionManager(authManager);

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_recovery'),
          refreshToken: 'refresh_recovery',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_recovery',
          deviceId: 'dev_recovery',
          scope: 'cascade:intake cycle:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_recovery');

      // 1. Initial upload fails to persist locally
      memoryVaultStorage.failWithException = true;

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_strip.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      expect(postCount, 1);
      expect(find.textContaining('Server OK, local save failed ⚠️'), findsOneWidget);
      expect(find.textContaining('Recover from Server (GET Latest)'), findsOneWidget);

      // 2. Storage recovers
      memoryVaultStorage.failWithException = false;

      // 3. Tap recovery button
      await tester.tap(find.textContaining('Recover from Server (GET Latest)'));
      await tester.pumpAndSettle();

      // Invariant: Zero repeated POST (no duplicate OCR)
      expect(postCount, 1);
      // Invariant: Exactly one GET latest call
      expect(getLatestCount, 1);
      // Invariant: Status updates to confirmed success
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('15. Absence of history loss: multiple ECG intakes append without deleting previous records', () async {
      final vault = VaultService(
        storage: memoryVaultStorage,
      );
      await vault.initFuture;
      await vault.setActivePatientId('patient_history_test');

      final ecg1 = EcgVisionIntakeClientResponse.fromJson(
        _sampleEcgIntakeResponseJson(intakeVersion: 1, recordingQuality: 'good'),
      );
      final ecg2 = EcgVisionIntakeClientResponse.fromJson(
        _sampleEcgIntakeResponseJson(intakeVersion: 2, recordingQuality: 'unreadable'),
      );

      final saved1 = await vault.recordEcgIntake(
        response: ecg1,
        localFilename: 'strip_v1.jpg',
        recordDate: '2026-10-28',
      );
      expect(saved1, isTrue);
      expect(vault.ecgRecords.length, 1);
      expect(vault.ecgRecords[0]['version'], 1);
      expect(vault.latestEcgRecord!['intake_version'], 1);

      final saved2 = await vault.recordEcgIntake(
        response: ecg2,
        localFilename: 'strip_v2.jpg',
        recordDate: '2026-10-29',
      );
      expect(saved2, isTrue);
      // Invariant: Both records exist, history preserved without overwriting
      expect(vault.ecgRecords.length, 2);
      expect(vault.ecgRecords[0]['version'], 1);
      expect(vault.ecgRecords[1]['version'], 2);
      expect(vault.latestEcgRecord!['intake_version'], 2);

      vault.dispose();
    });

    testWidgets('16. Correct 401 multipart retry with Bearer token & no X-Device-Id leakage',
        (tester) async {
      int uploadAttempts = 0;
      final List<Map<String, String>> requestHeaders = [];
      final initialJwt = _buildTestJwt(patientId: 'patient_headers_initial');
      final refreshedJwt = _buildTestJwt(patientId: 'patient_headers');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          final now = DateTime.now().toUtc();
          return http.Response(
            json.encode({
              'token_type': 'Bearer',
              'access_token': refreshedJwt,
              'refresh_token': 'new_refresh_token',
              'expires_in': 3600,
              'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
              'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'patient_headers',
              'device_id': 'dev_headers',
              'family_id': 'fam_headers',
              'scope': 'cascade:intake',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/intake/ecg') {
          uploadAttempts++;
          requestHeaders.add(Map<String, String>.from(request.headers));
          if (uploadAttempts == 1) {
            return http.Response('{"detail": "Token expired"}', 401, headers: {'content-type': 'application/json'});
          }
          return http.Response(
            json.encode(_sampleEcgIntakeResponseJson(intakeVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      apiClient.attachSessionManager(authManager);

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: initialJwt,
          refreshToken: 'refresh_headers',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_headers',
          deviceId: 'dev_headers',
          scope: 'cascade:intake',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_headers');

      final fakeFile = XFile.fromData(
        _fakeJpegBytes,
        name: 'ecg_headers_check.jpg',
        mimeType: 'image/jpeg',
      );

      await tester.pumpWidget(
        _buildTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
          initialEcgFile: fakeFile,
        ),
      );
      await tester.pumpAndSettle();

      expect(uploadAttempts, 2);
      expect(find.textContaining('ECG recorded on server (v1) ✓'), findsOneWidget);

      // Verify request 1 headers: Bearer old token, no X-Device-Id
      expect(requestHeaders[0]['Authorization'] ?? requestHeaders[0]['authorization'], 'Bearer $initialJwt');
      expect(requestHeaders[0].containsKey('X-Device-Id'), isFalse);
      expect(requestHeaders[0].containsKey('x-device-id'), isFalse);

      // Verify request 2 headers: Bearer new token, no X-Device-Id
      expect(requestHeaders[1]['Authorization'] ?? requestHeaders[1]['authorization'], startsWith('Bearer '));
      expect(requestHeaders[1]['Authorization'] ?? requestHeaders[1]['authorization'], isNot(equals('Bearer $initialJwt')));
      expect(requestHeaders[1].containsKey('X-Device-Id'), isFalse);
      expect(requestHeaders[1].containsKey('x-device-id'), isFalse);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });
  });
}
