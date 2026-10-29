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

import 'package:aegis_health/screens/upload_analysis_screen.dart';
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
  String deviceId = 'dev_test_lab',
  List<String> scopes = const [
    'cascade:intake',
    'cascade:run',
    'labs:read',
    'labs:write',
    'reports:read',
    'chat:read',
    'chat:write'
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

Map<String, dynamic> _sampleIntakeResponseJson({
  String cycleId = 'cycle_lab_001',
  int ledgerVersion = 1,
  List<Map<String, dynamic>>? confirmedMarkers,
  List<Map<String, dynamic>>? unconfirmedMarkers,
}) {
  final conf = confirmedMarkers ?? [
    {
      'original_name': 'Glucose',
      'marker_key': 'glucose',
      'raw_value': '5.2',
      'numeric_value': 5.2,
      'unit': 'mmol/L',
      'reference_range': {'low': 3.9, 'high': 6.1},
      'sample_date': '2026-10-28',
      'category': 'biochemistry',
      'is_uncertain': false,
      'is_critical_flag': false,
      'requires_user_verification': false,
      'is_recognition_ambiguous': false,
    },
    {
      'original_name': 'Cholesterol',
      'marker_key': 'cholesterol',
      'raw_value': '4.8',
      'numeric_value': 4.8,
      'unit': 'mmol/L',
      'reference_range': {'low': 3.0, 'high': 5.2},
      'sample_date': '2026-10-28',
      'category': 'lipid_panel',
      'is_uncertain': false,
      'is_critical_flag': false,
      'requires_user_verification': false,
      'is_recognition_ambiguous': false,
    },
  ];
  final unconf = unconfirmedMarkers ?? [];

  return {
    'cycle_id': cycleId,
    'status': 'processed',
    'ledger_version': ledgerVersion,
    'extracted_count': conf.length + unconf.length,
    'requires_verification_count': unconf.length,
    'document_metadata': {
      'document_id': 'doc_lab_001',
      'page_count': 1,
      'mime_types': ['image/jpeg'],
      'file_sizes': [1024],
      'laboratory_name': 'INVITRO',
      'sample_date': '2026-10-28',
      'created_at': '2026-10-28T12:00:00Z',
    },
    'markers': [...conf, ...unconf],
    'confirmed_markers': conf,
    'unconfirmed_markers': unconf,
    'has_unreviewed_lab_changes': true,
    'privacy_notice': 'Processed in strict patient-isolated blind mode',
  };
}

Map<String, dynamic> _sampleLedgerResponseJson({
  String cycleId = 'cycle_lab_001',
  int ledgerVersion = 1,
  List<Map<String, dynamic>>? history,
  Map<String, dynamic>? activeMarkers,
}) {
  return {
    'cycle_id': cycleId,
    'ledger_version': ledgerVersion,
    'last_evaluated_ledger_version': 0,
    'has_unreviewed_lab_changes': true,
    'evaluation_status': 'pending',
    'evaluation_notice': 'New unreviewed markers present',
    'has_critical_flags': false,
    'safety_notice': 'All parameters within safety thresholds',
    'active_markers': activeMarkers ?? {
      'glucose': {
        'observation_id': 'obs_glu_1',
        'marker_key': 'glucose',
        'display_name': 'Glucose',
        'value': '5.2',
        'numeric_value': 5.2,
        'unit': 'mmol/L',
        'sample_date': '2026-10-28',
        'reference_range': {'low': 3.9, 'high': 6.1},
        'event_type': 'initial',
        'is_active': true,
        'is_critical_flag': false,
        'is_uncertain': false,
        'is_recognition_ambiguous': false,
        'recorded_at': '2026-10-28T12:00:00Z',
      },
    },
    'observations_history': history ?? [
      {
        'observation_id': 'obs_glu_1',
        'marker_key': 'glucose',
        'display_name': 'Glucose',
        'value': '5.2',
        'numeric_value': 5.2,
        'unit': 'mmol/L',
        'sample_date': '2026-10-28',
        'reference_range': {'low': 3.9, 'high': 6.1},
        'event_type': 'initial',
        'is_active': true,
        'is_critical_flag': false,
        'is_uncertain': false,
        'is_recognition_ambiguous': false,
        'recorded_at': '2026-10-28T12:00:00Z',
      },
    ],
  };
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

  group('PROJECT AEGIS — STAGE 5.4D-4B: FLUTTER LAB DOCUMENT UPLOAD & VERSIONED HISTORY', () {
    test('1. Successful single document upload: POST /api/v1/client/cascade/intake/labs with auth & multipart', () async {
      int intakeCallCount = 0;
      http.Request? capturedRequest;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          intakeCallCount++;
          capturedRequest = request;
          expect(request.method, 'POST');
          expect(request.headers['authorization'], startsWith('Bearer '));
          // Client must NOT send patient_id or cycle_id in request query
          expect(request.url.queryParameters.containsKey('patient_id'), isFalse);
          expect(request.url.queryParameters.containsKey('cycle_id'), isFalse);

          return http.Response(
            json.encode(_sampleIntakeResponseJson(ledgerVersion: 1)),
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

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_lab_01'),
          refreshToken: 'rt_lab_01',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_lab_01',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final files = [
        LabUploadFileItem(
          filename: 'report_page1.jpg',
          bytes: _fakeJpegBytes,
        ),
      ];

      final response = await apiClient.uploadLabDocuments(files: files);

      expect(intakeCallCount, 1);
      expect(response.status, 'processed');
      expect(response.ledgerVersion, 1);
      expect(response.confirmedMarkers.length, 2);
      expect(response.confirmedMarkers.first.markerKey, 'glucose');
      expect(response.confirmedMarkers.first.numericValue, 5.2);
      expect(response.documentMetadata?.laboratoryName, 'INVITRO');

      // Verify multipart content
      expect(capturedRequest, isNotNull);
      expect(capturedRequest!.headers['content-type'], contains('multipart/form-data'));
      final decodedBody = latin1.decode(capturedRequest!.bodyBytes);
      expect(decodedBody, contains('filename="report_page1.jpg"'));
      expect(decodedBody, contains('name="files"'));

      apiClient.close();
      authManager.dispose();
    });

    test('2. Multi-page document upload: multiple pages sent as files multipart parts', () async {
      String? requestBody;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          requestBody = latin1.decode(request.bodyBytes);
          return http.Response(
            json.encode(_sampleIntakeResponseJson(ledgerVersion: 1)),
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

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_multi'),
          refreshToken: 'rt_multi',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_multi',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final files = [
        LabUploadFileItem(filename: 'page1.jpg', bytes: _fakeJpegBytes),
        LabUploadFileItem(filename: 'page2.png', bytes: _fakePngBytes),
        LabUploadFileItem(filename: 'page3.jpg', bytes: _fakeJpegBytes),
      ];

      final response = await apiClient.uploadLabDocuments(
        files: files,
        sampleDate: '2026-10-28',
      );

      expect(response.status, 'processed');
      expect(requestBody, isNotNull);
      expect(requestBody, contains('filename="page1.jpg"'));
      expect(requestBody, contains('filename="page2.png"'));
      expect(requestBody, contains('filename="page3.jpg"'));
      expect(requestBody, contains('name="sample_date"'));

      apiClient.close();
      authManager.dispose();
    });

    test('3. History preservation: getLabLedgerState() invoked and VaultService.updateLabLedgerSnapshot() preserves observationsHistory', () async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          return http.Response(
            json.encode(_sampleIntakeResponseJson(ledgerVersion: 2)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/labs/state') {
          return http.Response(
            json.encode(_sampleLedgerResponseJson(
              ledgerVersion: 2,
              history: [
                // Study 1 (historical baseline from September 2026)
                {
                  'observation_id': 'obs_alt_hist_01',
                  'marker_key': 'alt',
                  'display_name': 'ALT',
                  'value': '22.0',
                  'numeric_value': 22.0,
                  'unit': 'U/L',
                  'sample_date': '2026-09-01',
                  'reference_range': {'low': 0.0, 'high': 41.0},
                  'event_type': 'initial',
                  'is_active': false,
                  'is_critical_flag': false,
                  'is_uncertain': false,
                  'recorded_at': '2026-09-01T10:00:00Z',
                  'study_id': 'study_sep_2026',
                },
                // Study 2 (newly uploaded October 2026)
                {
                  'observation_id': 'obs_alt_new_02',
                  'marker_key': 'alt',
                  'display_name': 'ALT',
                  'value': '28.5',
                  'numeric_value': 28.5,
                  'unit': 'U/L',
                  'sample_date': '2026-10-28',
                  'reference_range': {'low': 0.0, 'high': 41.0},
                  'event_type': 'initial',
                  'is_active': true,
                  'is_critical_flag': false,
                  'is_uncertain': false,
                  'recorded_at': '2026-10-28T12:00:00Z',
                  'study_id': 'study_oct_2026',
                },
              ],
              activeMarkers: {
                'alt': {
                  'observation_id': 'obs_alt_new_02',
                  'marker_key': 'alt',
                  'display_name': 'ALT',
                  'value': '28.5',
                  'numeric_value': 28.5,
                  'unit': 'U/L',
                  'sample_date': '2026-10-28',
                  'reference_range': {'low': 0.0, 'high': 41.0},
                  'event_type': 'initial',
                  'is_active': true,
                  'is_critical_flag': false,
                  'is_uncertain': false,
                  'recorded_at': '2026-10-28T12:00:00Z',
                },
              },
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
          accessToken: _buildTestJwt(patientId: 'patient_hist'),
          refreshToken: 'rt_hist',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_hist',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_hist');

      // 1. Upload new report
      final uploadResponse = await apiClient.uploadLabDocuments(
        files: [LabUploadFileItem(filename: 'alt_oct.jpg', bytes: _fakeJpegBytes)],
      );
      expect(uploadResponse.ledgerVersion, 2);

      // 2. Fetch authoritative ledger state
      final ledgerState = await apiClient.getLabLedgerState();
      expect(ledgerState.ledgerVersion, 2);
      expect(ledgerState.observationsHistory.length, 2);

      // 3. Update VaultService snapshot and flush
      vault.updateLabLedgerSnapshot(ledgerState);
      await vault.flushPendingWrites();

      // 4. Invariant: Multi-study history is preserved and accessible
      expect(vault.cachedLabLedgerState?.ledgerVersion, 2);
      expect(vault.labObservationsHistory.length, 2);
      expect(vault.labObservationsHistory[0].studyId, 'study_sep_2026');
      expect(vault.labObservationsHistory[0].value, '22.0');
      expect(vault.labObservationsHistory[1].studyId, 'study_oct_2026');
      expect(vault.labObservationsHistory[1].value, '28.5');

      // Active marker is the newest one
      expect(vault.activeLabMarkers['alt']?.value, '28.5');

      // Verify secure storage persistence
      final storageKey = VaultStorageService.derivePatientStorageKey('patient_hist');
      final storedData = await memoryVaultStorage.loadPatientVault(storageKey);
      expect(storedData['system_folders']?['global']?['lab_ledger_snapshot']?['ledger_version'], 2);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('4. Confirmed vs unconfirmed markers separated: unconfirmed flagged with verification notice', (tester) async {
      final intakeJson = _sampleIntakeResponseJson(
        ledgerVersion: 1,
        confirmedMarkers: [
          {
            'original_name': 'Hemoglobin',
            'marker_key': 'hemoglobin',
            'raw_value': '145',
            'numeric_value': 145.0,
            'unit': 'g/L',
            'reference_range': {'low': 130.0, 'high': 170.0},
            'sample_date': '2026-10-28',
            'category': 'hematology',
            'is_uncertain': false,
            'is_critical_flag': false,
            'requires_user_verification': false,
            'is_recognition_ambiguous': false,
          },
        ],
        unconfirmedMarkers: [
          {
            'original_name': 'Ferritin',
            'marker_key': 'ferritin',
            'raw_value': '24 (?)',
            'numeric_value': 24.0,
            'unit': 'ng/mL',
            'reference_range': {'low': 30.0, 'high': 400.0},
            'sample_date': '2026-10-28',
            'category': 'iron_metabolism',
            'is_uncertain': true,
            'uncertainty_note': 'Value partially smudged on paper document',
            'is_critical_flag': false,
            'requires_user_verification': true,
            'is_recognition_ambiguous': true,
          },
        ],
      );

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          return http.Response(json.encode(intakeJson), 200, headers: {'content-type': 'application/json'});
        }
        if (request.url.path == '/api/v1/client/labs/state') {
          return http.Response(json.encode(_sampleLedgerResponseJson()), 200, headers: {'content-type': 'application/json'});
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
          accessToken: _buildTestJwt(patientId: 'patient_verify'),
          refreshToken: 'rt_verify',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_verify',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_verify');

      final fakeFile = XFile.fromData(_fakeJpegBytes, name: 'lab_results.jpg');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: UploadAnalysisScreen(
              apiClient: apiClient,
              authSessionManager: authManager,
              initialFiles: [fakeFile],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Tap Analyze button
      final analyzeBtn = find.text("Analyze 🤖");
      expect(analyzeBtn, findsOneWidget);
      await tester.tap(analyzeBtn);
      await tester.pumpAndSettle();

      // Dialog must appear with "Processed with Notices"
      expect(find.text("Processed with Notices"), findsOneWidget);

      // Confirmed indicator count: 1
      expect(find.text("✅ Confirmed indicators: 1"), findsOneWidget);
      expect(find.text("Hemoglobin: 145 g/L"), findsOneWidget);

      // Unconfirmed section must explicitly alert that verification is required
      expect(find.text("Requires Verification (1)"), findsOneWidget);
      expect(find.textContaining("NOT confirmed in the active ledger history"), findsOneWidget);
      expect(find.textContaining("Ferritin: 24 (?) (Value partially smudged on paper document)"), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('5. Timeout and retry: AegisTimeoutException triggers retryable UI state and Retry re-executes', (tester) async {
      int intakeAttempts = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          intakeAttempts++;
          if (intakeAttempts == 1) {
            throw TimeoutException('Lab intake timed out after 30s');
          }
          return http.Response(
            json.encode(_sampleIntakeResponseJson(ledgerVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/labs/state') {
          return http.Response(json.encode(_sampleLedgerResponseJson()), 200, headers: {'content-type': 'application/json'});
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
          accessToken: _buildTestJwt(patientId: 'patient_timeout'),
          refreshToken: 'rt_timeout',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_timeout',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_timeout');

      final fakeFile = XFile.fromData(_fakeJpegBytes, name: 'timeout_test.jpg');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: UploadAnalysisScreen(
              apiClient: apiClient,
              authSessionManager: authManager,
              initialFiles: [fakeFile],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Attempt 1: Taps Analyze -> Timeout
      await tester.tap(find.text("Analyze 🤖"));
      await tester.pumpAndSettle();

      expect(intakeAttempts, 1);
      expect(find.textContaining("Upload timed out"), findsOneWidget);
      expect(find.text("Retry Upload"), findsOneWidget);

      // Attempt 2: Tap "Retry Upload" -> Succeeds
      await tester.tap(find.text("Retry Upload"));
      await tester.pumpAndSettle();

      expect(intakeAttempts, 2);
      expect(find.text("Success!"), findsOneWidget);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('6. 401 token refresh: automatic single refresh recovers request with 200', () async {
      int intakeAttempts = 0;
      int refreshCalls = 0;
      final capturedTokens = <String>[];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          intakeAttempts++;
          final authHeader = request.headers['authorization'] ?? '';
          capturedTokens.add(authHeader);

          if (intakeAttempts == 1) {
            // First attempt: Token expired
            return http.Response(
              json.encode({'detail': 'TOKEN_EXPIRED', 'message': 'Token has expired'}),
              401,
              headers: {'content-type': 'application/json'},
            );
          }
          // Second attempt with refreshed token: success
          return http.Response(
            json.encode(_sampleIntakeResponseJson(ledgerVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          final newJwt = _buildTestJwt(patientId: 'patient_refresh_test');
          final now = DateTime.now().toUtc();
          return http.Response(
            json.encode({
              'access_token': newJwt,
              'token_type': 'Bearer',
              'expires_in': 3600,
              'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
              'refresh_token': 'new_refresh_token_01',
              'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'patient_refresh_test',
              'device_id': 'dev_test_lab',
              'scope': 'cascade:intake cascade:run labs:read labs:write',
            }),
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

      final initialJwt = _buildTestJwt(patientId: 'patient_refresh_test');
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: initialJwt,
          refreshToken: 'initial_rt',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_refresh_test',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final files = [LabUploadFileItem(filename: 'refresh_test.jpg', bytes: _fakeJpegBytes)];

      final response = await apiClient.uploadLabDocuments(files: files);

      expect(intakeAttempts, 2);
      expect(refreshCalls, 1);
      expect(response.status, 'processed');
      // Verify second request used the new token
      expect(capturedTokens.length, 2);
      expect(capturedTokens[0], isNot(equals(capturedTokens[1])));

      apiClient.close();
      authManager.dispose();
    });

    testWidgets('7. 403 missing scope: displays access denied notice and prevents retry', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          return http.Response(
            json.encode({'detail': 'INSUFFICIENT_SCOPES', 'message': 'Missing required scope cascade:intake'}),
            403,
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
          accessToken: _buildTestJwt(patientId: 'patient_noscope', scopes: ['reports:read']),
          refreshToken: 'rt_noscope',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_noscope',
          deviceId: 'dev_test_lab',
          scope: 'reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_noscope');

      final fakeFile = XFile.fromData(_fakeJpegBytes, name: 'noscope.jpg');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: UploadAnalysisScreen(
              apiClient: apiClient,
              authSessionManager: authManager,
              initialFiles: [fakeFile],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text("Analyze 🤖"));
      await tester.pumpAndSettle();

      expect(find.textContaining("Access denied"), findsOneWidget);
      // No retry button for permission failure
      expect(find.text("Retry Upload"), findsNothing);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('8. Client pre-validation: rejects >15 pages, >10MB file, >30MB total, invalid format before network call', () async {
      int networkCalls = 0;
      final mockClient = MockClient((request) async {
        networkCalls++;
        return http.Response('{}', 200);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_val'),
          refreshToken: 'rt_val',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_val',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );

      // a) > 15 files
      final sixteenFiles = List.generate(
        16,
        (i) => LabUploadFileItem(filename: 'page_$i.jpg', bytes: _fakeJpegBytes),
      );
      expect(
        () => apiClient.uploadLabDocuments(files: sixteenFiles),
        throwsA(isA<AegisValidationException>().having((e) => e.message, 'message', contains('Maximum 15 pages'))),
      );

      // b) Single file > 10MB
      final largeBytes = Uint8List(10 * 1024 * 1024 + 1);
      final largeFile = [LabUploadFileItem(filename: 'big.jpg', bytes: largeBytes)];
      expect(
        () => apiClient.uploadLabDocuments(files: largeFile),
        throwsA(isA<AegisValidationException>().having((e) => e.message, 'message', contains('10 MB'))),
      );

      // c) Total upload > 30MB
      final mediumBytes1 = Uint8List(9 * 1024 * 1024);
      final mediumBytes2 = Uint8List(9 * 1024 * 1024);
      final mediumBytes3 = Uint8List(9 * 1024 * 1024);
      final mediumBytes4 = Uint8List(5 * 1024 * 1024); // total = 32MB > 30MB
      final multiOver30 = [
        LabUploadFileItem(filename: 'p1.jpg', bytes: mediumBytes1),
        LabUploadFileItem(filename: 'p2.jpg', bytes: mediumBytes2),
        LabUploadFileItem(filename: 'p3.jpg', bytes: mediumBytes3),
        LabUploadFileItem(filename: 'p4.jpg', bytes: mediumBytes4),
      ];
      expect(
        () => apiClient.uploadLabDocuments(files: multiOver30),
        throwsA(isA<AegisValidationException>().having((e) => e.message, 'message', contains('30 MB'))),
      );

      // d) Invalid extension (.pdf or .txt)
      final pdfFile = [LabUploadFileItem(filename: 'report.pdf', bytes: Uint8List.fromList([1, 2, 3]))];
      expect(
        () => apiClient.uploadLabDocuments(files: pdfFile),
        throwsA(isA<AegisValidationException>().having((e) => e.message, 'message', contains('unsupported format'))),
      );

      // Verify zero network calls were made
      expect(networkCalls, 0);

      apiClient.close();
      authManager.dispose();
    });

    testWidgets('9. Patient epoch fencing: patient change during upload cancels state update & isolates data', (tester) async {
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          return await completer.future;
        }
        if (request.url.path == '/api/v1/client/labs/state') {
          return http.Response(json.encode(_sampleLedgerResponseJson()), 200, headers: {'content-type': 'application/json'});
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

      // 1. Start with Patient Alpha
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_alpha'),
          refreshToken: 'rt_alpha',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_alpha',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_alpha');

      final fakeFile = XFile.fromData(_fakeJpegBytes, name: 'alpha_doc.jpg');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: UploadAnalysisScreen(
              apiClient: apiClient,
              authSessionManager: authManager,
              initialFiles: [fakeFile],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Tap Analyze -> in flight
      await tester.tap(find.text("Analyze 🤖"));
      await tester.pump(); // Advance past setState

      // 2. Switch patient to Patient Beta while upload is in flight!
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_beta'),
          refreshToken: 'rt_beta',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_beta',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_beta');

      // 3. Resolve upload response
      completer.complete(
        http.Response(
          json.encode(_sampleIntakeResponseJson(ledgerVersion: 99)),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await tester.pumpAndSettle();

      // UI safely halted due to epoch fencing check
      expect(find.textContaining("Patient profile changed during upload"), findsOneWidget);
      expect(find.text("Success!"), findsNothing);

      // Verify Patient Beta's vault memory was NOT contaminated with Alpha's upload
      expect(vault.cachedLabLedgerState, isNull);
      expect(vault.labObservationsHistory, isEmpty);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    test('10. Direct Gemini removal: LabParserService is NOT called, zero client Gemini requests', () async {
      final contactedHosts = <String>[];

      final mockClient = MockClient((request) async {
        contactedHosts.add(request.url.host);
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          return http.Response(json.encode(_sampleIntakeResponseJson()), 200, headers: {'content-type': 'application/json'});
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_no_gemini'),
          refreshToken: 'rt_no_gemini',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_no_gemini',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final files = [LabUploadFileItem(filename: 'test.jpg', bytes: _fakeJpegBytes)];
      await apiClient.uploadLabDocuments(files: files);

      // Invariant: Zero calls to Google Gemini / generativelanguage endpoints
      expect(contactedHosts, isNot(contains('generativelanguage.googleapis.com')));
      expect(contactedHosts, isNot(contains('googleapis.com')));

      apiClient.close();
      authManager.dispose();
    });

    testWidgets('11. UI submission lock: prevents double-click submission while upload is in-flight', (tester) async {
      int intakeCallCount = 0;
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          final currentCount = ++intakeCallCount;
          if (currentCount == 1) {
            return await completer.future;
          }
        }
        if (request.url.path == '/api/v1/client/labs/state') {
          return http.Response(json.encode(_sampleLedgerResponseJson()), 200, headers: {'content-type': 'application/json'});
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
          accessToken: _buildTestJwt(patientId: 'patient_lock'),
          refreshToken: 'rt_lock',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_lock',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_lock');

      final fakeFile = XFile.fromData(_fakeJpegBytes, name: 'lock_test.jpg');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: UploadAnalysisScreen(
              apiClient: apiClient,
              authSessionManager: authManager,
              initialFiles: [fakeFile],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Tap 1: Triggers first upload
      await tester.tap(find.text("Analyze 🤖"));
      await tester.pump();

      // Button is now showing CircularProgressIndicator and disabled
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(intakeCallCount, 1);

      // Attempt second tap on button area while still in-flight
      await tester.tap(find.byType(ElevatedButton), warnIfMissed: false);
      await tester.pump();

      // Call count remains exactly 1
      expect(intakeCallCount, 1);

      // Resolve the request
      completer.complete(
        http.Response(
          json.encode(_sampleIntakeResponseJson(ledgerVersion: 1)),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text("Success!"), findsOneWidget);
      expect(intakeCallCount, 1);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });

    testWidgets('12. Resilience: getLabLedgerState() error after successful OCR intake falls back to intake response with zero data loss', (tester) async {
      int intakeCallCount = 0;
      int ledgerCallCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/labs') {
          intakeCallCount++;
          return http.Response(
            json.encode(_sampleIntakeResponseJson(ledgerVersion: 1)),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/labs/state') {
          ledgerCallCount++;
          // Simulate transient server error or timeout on ledger query
          return http.Response('Internal Server Error', 500);
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
          accessToken: _buildTestJwt(patientId: 'patient_resilience'),
          refreshToken: 'rt_resilience',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_resilience',
          deviceId: 'dev_test_lab',
          scope: 'cascade:intake cascade:run labs:read labs:write',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_resilience');

      final fakeFile = XFile.fromData(_fakeJpegBytes, name: 'resilience_test.jpg');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: UploadAnalysisScreen(
              apiClient: apiClient,
              authSessionManager: authManager,
              initialFiles: [fakeFile],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Tap Analyze
      await tester.tap(find.text("Analyze 🤖"));
      await tester.pumpAndSettle();

      // Verify both endpoints were called
      expect(intakeCallCount, 1);
      expect(ledgerCallCount, 1);

      // Invariant: UI does NOT show a false upload error; shows success dialog
      expect(find.text("Success!"), findsOneWidget);
      expect(find.text("✅ Confirmed indicators: 2"), findsOneWidget);

      // Invariant: Vault memory contains confirmed markers from intake response fallback
      expect(vault.cachedLabLedgerState, isNotNull);
      expect(vault.cachedLabLedgerState?.ledgerVersion, 1);
      expect(vault.activeLabMarkers.containsKey('glucose'), isTrue);
      expect(vault.activeLabMarkers['glucose']?.value, '5.2');
      expect(vault.activeLabMarkers.containsKey('cholesterol'), isTrue);
      expect(vault.activeLabMarkers['cholesterol']?.value, '4.8');
      expect(vault.labObservationsHistory.length, 2);

      // Invariant: Secure storage persisted the data
      await vault.flushPendingWrites();
      final storageKey = VaultStorageService.derivePatientStorageKey('patient_resilience');
      final storedData = await memoryVaultStorage.loadPatientVault(storageKey);
      expect(storedData['system_folders']?['global']?['lab_ledger_snapshot']?['ledger_version'], 1);

      apiClient.close();
      authManager.dispose();
      vault.dispose();
    });
  });
}
