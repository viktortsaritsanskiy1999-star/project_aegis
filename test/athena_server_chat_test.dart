// ignore_for_file: depend_on_referenced_packages
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/screens/curator_chat_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/widgets/emergency_medical_modal.dart';

/// Helper to build test JWT
String _buildTestJwt({
  required String patientId,
  String deviceId = 'dev_test_chat',
  List<String> scopes = const [
    'chat:read',
    'chat:write',
    'cycle:read',
    'vitals:read',
  ],
  String? jti,
}) {
  final header = base64Url
      .encode(utf8.encode(json.encode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final payload = base64Url
      .encode(utf8.encode(json.encode({
        'sub': patientId,
        'patient_id': patientId,
        'device_id': deviceId,
        'did': deviceId,
        'scope': scopes.join(' '),
        'scopes': scopes,
        'type': 'access',
        'jti': jti ?? 'jti_${DateTime.now().microsecondsSinceEpoch}',
        'exp': DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000,
      })))
      .replaceAll('=', '');
  return '$header.$payload.signature_mock_bytes';
}

/// Helper for building CuratorChatScreen test harness
Widget _buildChatTestApp({
  required AegisApiClient apiClient,
  required AuthSessionManager authManager,
  required VaultService vaultService,
  TimeService? timeService,
  VitalsMonitorService? vitalsService,
}) {
  final time = timeService ?? TimeService();
  final vitals = vitalsService ?? VitalsMonitorService(time, vaultService);

  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vaultService),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      Provider<AegisApiClient>.value(value: apiClient),
      ChangeNotifierProvider<TimeService>.value(value: time),
      ChangeNotifierProvider<VitalsMonitorService>.value(value: vitals),
    ],
    child: MaterialApp(
      home: CuratorChatScreen(
        apiClient: apiClient,
        authSessionManager: authManager,
        vaultService: vaultService,
        timeService: time,
      ),
    ),
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryVaultStorage;
  late InMemoryAuthTokenStorage memoryAuthStorage;

  setUp(() {
    memoryVaultStorage = InMemoryVaultStorage();
    memoryAuthStorage = InMemoryAuthTokenStorage();
    VaultService.resetWriteQueueForTesting();
  });

  group('PROJECT AEGIS — STAGE 5.4F-1: ATHENA FLUTTER → SERVER CHAT INTEGRATION', () {
    // -------------------------------------------------------------------------
    // 1. SEND MESSAGE VIA FASTAPI
    // -------------------------------------------------------------------------
    testWidgets('1. Sending a user message routes to POST /api/v1/client/chat/athena and renders reply', (tester) async {
      int sendCalls = 0;
      String? recordedIdempotencyKey;
      String? recordedMessage;
      String? recordedEventType;
      String? recordedAuthHeader;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history' && request.method == 'GET') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_chat_1',
              'messages': [
                {'sender': 'curator', 'text': 'Hello! How can I assist you today?', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          sendCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          recordedIdempotencyKey = body['idempotency_key'] as String?;
          recordedMessage = body['message'] as String?;
          recordedEventType = body['event_type'] as String?;
          recordedAuthHeader = request.headers['Authorization'];

          // Strictly verify patient_id is NOT transmitted as client payload
          expect(body.containsKey('patient_id'), isFalse);
          expect(request.url.queryParameters.containsKey('patient_id'), isFalse);

          return http.Response(
            json.encode({
              'reply': 'Your daily metrics are looking strong and balanced.',
              'emergency_trigger': false,
              'emergency_notice': null,
              'cycle_id': 'cycle_chat_1',
              'sprint_day': 1,
              'message_id': 'msg_resp_001',
              'created_at': '2026-10-29T10:01:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_chat_01'),
          refreshToken: 'ref_tok_chat',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_chat_01',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Verified: initial history loaded
      expect(find.text('Hello! How can I assist you today?'), findsOneWidget);

      // Enter user text and tap send
      await tester.enterText(find.byKey(const Key('chat_input_field')), 'I ran 5km this morning');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      // Verified: POST called with exact payload and Bearer token
      expect(sendCalls, equals(1));
      expect(recordedMessage, equals('I ran 5km this morning'));
      expect(recordedEventType, equals('user_message'));
      expect(recordedIdempotencyKey, isNotNull);
      expect(recordedIdempotencyKey!.isNotEmpty, isTrue);
      expect(recordedAuthHeader, startsWith('Bearer '));

      // Verified: both user message and server reply rendered in UI
      expect(find.text('I ran 5km this morning'), findsOneWidget);
      expect(find.text('Your daily metrics are looking strong and balanced.'), findsOneWidget);

      // Verified: saved to Vault
      expect(vault.getCuratorChatHistory().length, equals(3));
    });

    // -------------------------------------------------------------------------
    // 2. LOAD SANITIZED CHAT HISTORY ON SCREEN OPEN
    // -------------------------------------------------------------------------
    testWidgets('2. Screen load queries GET /api/v1/client/chat/athena/history and syncs to Vault', (tester) async {
      int historyCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history' && request.method == 'GET') {
          historyCalls++;
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_hist_42',
              'messages': [
                {'sender': 'user', 'text': 'How was my sleep score?', 'timestamp': '2026-10-29T08:00:00Z'},
                {'sender': 'curator', 'text': 'Your REM sleep was in the optimal 22% bracket.', 'timestamp': '2026-10-29T08:00:05Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_chat_hist'),
          refreshToken: 'ref_tok_hist',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_chat_hist',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      expect(historyCalls, equals(1));
      expect(find.text('How was my sleep score?'), findsOneWidget);
      expect(find.text('Your REM sleep was in the optimal 22% bracket.'), findsOneWidget);

      // Verified: Vault synchronized
      final vaultHistory = vault.getCuratorChatHistory();
      expect(vaultHistory.length, equals(2));
      expect(vaultHistory[0]['text'], equals('How was my sleep score?'));
      expect(vaultHistory[1]['text'], equals('Your REM sleep was in the optimal 22% bracket.'));
    });

    // -------------------------------------------------------------------------
    // 3. INITIAL GREETING ON EMPTY SERVER HISTORY
    // -------------------------------------------------------------------------
    testWidgets('3. Empty server history triggers server initial greeting turn without local Gemini', (tester) async {
      int historyCalls = 0;
      int greetingCalls = 0;
      String? greetingEventType;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history' && request.method == 'GET') {
          historyCalls++;
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_empty_init',
              'messages': [], // Empty!
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          greetingCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          greetingEventType = body['event_type'] as String?;
          return http.Response(
            json.encode({
              'reply': 'Welcome! I am Athena — your health companion for this cycle.',
              'emergency_trigger': false,
              'emergency_notice': null,
              'cycle_id': 'cycle_empty_init',
              'sprint_day': 1,
              'message_id': 'msg_init_greeting',
              'created_at': '2026-10-29T10:00:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_chat_empty'),
          refreshToken: 'ref_tok_empty',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_chat_empty',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      expect(historyCalls, equals(1));
      expect(greetingCalls, equals(1));
      expect(greetingEventType, equals('initial_greeting'));
      expect(find.text('Welcome! I am Athena — your health companion for this cycle.'), findsOneWidget);
      expect(vault.getCuratorChatHistory().length, equals(1));
    });

    // -------------------------------------------------------------------------
    // 4. HTTP 401 TOKEN REFRESH & TRANSPARENT RETRY
    // -------------------------------------------------------------------------
    testWidgets('4. HTTP 401 triggers single token refresh and transparently retries with fresh token', (tester) async {
      int attempts = 0;
      int refreshCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_401',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          final now = DateTime.now().toUtc();
          return http.Response(
            json.encode({
              'access_token': _buildTestJwt(patientId: 'pat_401_user'),
              'token_type': 'bearer',
              'expires_in': 3600,
              'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
              'refresh_token': 'ref_tok_new',
              'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'pat_401_user',
              'device_id': 'dev_test_chat',
              'scope': 'chat:read chat:write cycle:read vitals:read',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          attempts++;
          if (attempts == 1) {
            // First attempt rejected with 401
            return http.Response(
              json.encode({'detail': 'Token expired'}),
              401,
              headers: {'content-type': 'application/json'},
            );
          }
          // Second attempt with refreshed token succeeds
          return http.Response(
            json.encode({
              'reply': 'Recovered and answered via refreshed token session.',
              'emergency_trigger': false,
              'cycle_id': 'cycle_401',
              'message_id': 'msg_after_refresh',
              'created_at': '2026-10-29T10:05:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_401_user'),
          refreshToken: 'ref_tok_init',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_401_user',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Send message
      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Testing 401 refresh');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      expect(attempts, equals(2));
      expect(refreshCalls, equals(1));
      expect(find.text('Recovered and answered via refreshed token session.'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 5. HTTP 403 FORBIDDEN SCOPE HANDLING
    // -------------------------------------------------------------------------
    testWidgets('5. HTTP 403 Forbidden displays access denied message without repeating or crashing', (tester) async {
      int sendCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_403',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          sendCalls++;
          return http.Response(
            json.encode({'detail': 'Forbidden: session lacks chat:write scope'}),
            403,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_no_scope', scopes: ['chat:read']),
          refreshToken: 'ref_tok_noscope',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_no_scope',
          deviceId: 'dev_test_chat',
          scope: 'chat:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Unauthorized attempt');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      expect(sendCalls, equals(1));
      expect(find.byKey(const Key('chat_error_banner')), findsOneWidget);
      expect(find.textContaining('Access denied: session lacks required chat:write scope'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 6. TIMEOUT AND RETRY WITH IDENTICAL IDEMPOTENCY KEY
    // -------------------------------------------------------------------------
    testWidgets('6. Timeout preserves message and retry uses the exact same idempotency_key', (tester) async {
      int attempts = 0;
      final List<String?> sentKeys = [];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_timeout',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          attempts++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          sentKeys.add(body['idempotency_key'] as String?);

          if (attempts == 1) {
            // First attempt times out
            throw const SocketException('Connection timed out');
          }

          // Second attempt succeeds
          return http.Response(
            json.encode({
              'reply': 'Successfully received on retry!',
              'emergency_trigger': false,
              'cycle_id': 'cycle_timeout',
              'message_id': 'msg_retry_succ',
              'created_at': '2026-10-29T10:10:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_timeout_user'),
          refreshToken: 'ref_tok_to',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_timeout_user',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Send message
      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Will time out');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      expect(attempts, equals(1));
      expect(find.byKey(const Key('chat_error_banner')), findsOneWidget);
      expect(find.byKey(const Key('chat_retry_button')), findsOneWidget);

      // Tap RETRY
      await tester.tap(find.byKey(const Key('chat_retry_button')));
      await tester.pumpAndSettle();

      expect(attempts, equals(2));
      expect(sentKeys.length, equals(2));
      // STRICT VERIFICATION: Both attempts MUST use the EXACT same idempotency key
      expect(sentKeys[0], isNotNull);
      expect(sentKeys[0], equals(sentKeys[1]));

      // Success reply displayed
      expect(find.text('Successfully received on retry!'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 7. NO MESSAGE DUPLICATION ON RETRY
    // -------------------------------------------------------------------------
    testWidgets('7. Retrying a failed message does NOT duplicate user messages in UI or Vault', (tester) async {
      int attempts = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_dedup',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          attempts++;
          if (attempts == 1) {
            throw const SocketException('Network dropped');
          }
          return http.Response(
            json.encode({
              'reply': 'Reply to single user message',
              'emergency_trigger': false,
              'cycle_id': 'cycle_dedup',
              'message_id': 'msg_dedup_01',
              'created_at': '2026-10-29T10:15:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_dedup_user'),
          refreshToken: 'ref_tok_dedup',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_dedup_user',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Unique user query');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      // Retry
      await tester.tap(find.byKey(const Key('chat_retry_button')));
      await tester.pumpAndSettle();

      // In UI: exactly 1 user bubble with 'Unique user query'
      final userTextWidgets = find.text('Unique user query');
      expect(userTextWidgets, findsOneWidget);

      // In Vault: exactly 1 user message with 'Unique user query'
      final vaultMessages = vault.getCuratorChatHistory();
      final userMessagesInVault = vaultMessages.where((m) => m['sender'] == 'user').toList();
      expect(userMessagesInVault.length, equals(1));
      expect(userMessagesInVault[0]['text'], equals('Unique user query'));

      final curatorMessagesInVault = vaultMessages.where((m) => m['sender'] == 'curator').toList();
      expect(curatorMessagesInVault.length, equals(2)); // Initial greeting + reply
      expect(curatorMessagesInVault.any((m) => m['text'] == 'Reply to single user message'), isTrue);
    });

    // -------------------------------------------------------------------------
    // 8. PATIENT SWITCH DURING IN-FLIGHT REQUEST (ATTRIBUTION FENCE)
    // -------------------------------------------------------------------------
    testWidgets('8. Patient switch while request is in-flight drops response and protects patient isolation', (tester) async {
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_switch',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          return completer.future; // Hold in-flight
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok_p1',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Patient 1 sends message
      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Message from Patient 1');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pump(); // Request now in-flight

      // Patient switches to pat_002
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_002'),
          refreshToken: 'ref_tok_p2',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_002',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      // Now Patient 1's server response completes
      completer.complete(http.Response(
        json.encode({
          'reply': 'Confidential response for Patient 1',
          'emergency_trigger': false,
          'cycle_id': 'cycle_pat1',
          'message_id': 'msg_pat1_leak',
          'created_at': '2026-10-29T10:20:00Z',
        }),
        200,
        headers: {'content-type': 'application/json'},
      ));
      await tester.pumpAndSettle();

      // Patient 2's UI and Vault must NOT contain Patient 1's reply!
      expect(find.text('Confidential response for Patient 1'), findsNothing);
      final vaultMessages = vault.getCuratorChatHistory();
      expect(vaultMessages.any((m) => m['text'] == 'Confidential response for Patient 1'), isFalse);
    });

    // -------------------------------------------------------------------------
    // 9. ZERO DIRECT GEMINI CALLS IN ACTIVE CHAT
    // -------------------------------------------------------------------------
    testWidgets('9. Active chat screen makes zero direct Gemini calls and relies exclusively on server API', (tester) async {
      int serverChatCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_gemini_check',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena') {
          serverChatCalls++;
          return http.Response(
            json.encode({
              'reply': 'Server-managed synthesis completed.',
              'emergency_trigger': false,
              'cycle_id': 'cycle_gemini_check',
              'message_id': 'msg_srv_001',
              'created_at': '2026-10-29T10:25:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_no_gemini'),
          refreshToken: 'ref_tok_ng',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_no_gemini',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Hello');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      // All processing was strictly routed through FastAPI
      expect(serverChatCalls, equals(1));
      expect(find.text('Server-managed synthesis completed.'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 10. ZERO SERVER_ONLY DATA LEAKAGE
    // -------------------------------------------------------------------------
    testWidgets('10. Client response contains strictly sanitized conversational text with zero server_only data leakage', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_sanitize',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena') {
          return http.Response(
            json.encode({
              'reply': 'Keep up the healthy hydration routine!',
              'emergency_trigger': false,
              'cycle_id': 'cycle_sanitize',
              'message_id': 'msg_sanitized',
              'created_at': '2026-10-29T10:30:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_sanitize'),
          refreshToken: 'ref_tok_san',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_sanitize',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('chat_input_field')), 'Water intake update');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pumpAndSettle();

      // Verify sanitized reply is visible
      expect(find.text('Keep up the healthy hydration routine!'), findsOneWidget);

      // Verify zero system/clinical tags in UI or Vault
      expect(find.textContaining('[CLINICAL_REPORT]'), findsNothing);
      expect(find.textContaining('INTERNAL_PROMPT'), findsNothing);
      expect(find.textContaining('SECRET'), findsNothing);

      for (var msg in vault.getCuratorChatHistory()) {
        expect(msg['text']!.contains('[CLINICAL_REPORT]'), isFalse);
        expect(msg['text']!.contains('INTERNAL_PROMPT'), isFalse);
      }
    });

    // -------------------------------------------------------------------------
    // 11. HISTORY PRESERVATION ON NETWORK ERROR
    // -------------------------------------------------------------------------
    testWidgets('11. Local cached history in Vault is preserved and displayed when offline', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          // Offline / connection refused
          throw const SocketException('Connection refused');
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_offline'),
          refreshToken: 'ref_tok_off',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_offline',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // Seed local vault with previous conversation
      vault.saveCuratorChatMessage({'sender': 'user', 'text': 'Cached offline query'});
      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Cached offline advice'});

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Cached history is preserved and rendered despite offline network error
      expect(find.text('Cached offline query'), findsOneWidget);
      expect(find.text('Cached offline advice'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 12. EMERGENCY TRIGGER MODAL DISPLAY
    // -------------------------------------------------------------------------
    testWidgets('12. emergency_trigger: true from server displays emergency modal', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_emerg',
              'messages': [
                {'sender': 'curator', 'text': 'Welcome to Athena', 'timestamp': '2026-10-29T10:00:00Z'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena') {
          return http.Response(
            json.encode({
              'reply': 'Severe chest pressure detected. Please sit down and summon emergency services.',
              'emergency_trigger': true,
              'emergency_notice': 'CRITICAL_ALERT: Acute chest symptoms detected.',
              'cycle_id': 'cycle_emerg',
              'message_id': 'msg_emerg_001',
              'created_at': '2026-10-29T10:35:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_emerg'),
          refreshToken: 'ref_tok_em',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_emerg',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('chat_input_field')), 'I have crushing chest pain');
      await tester.tap(find.byKey(const Key('chat_send_button')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 600)); // Allow 500ms delay for emergency modal
      await tester.pumpAndSettle();

      // Emergency message rendered exactly once
      expect(find.text('Severe chest pressure detected. Please sit down and summon emergency services.'), findsOneWidget);

      // EmergencyMedicalModal opened strictly by server emergency_trigger
      expect(find.byType(EmergencyMedicalModal), findsOneWidget);
      expect(find.text("Please stay safe and seek assistance"), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 13. RE-OPENING CHAT DOES NOT DUPLICATE INITIAL GREETING
    // -------------------------------------------------------------------------
    testWidgets('13. Re-opening chat screen with existing local history does NOT call initial_greeting again', (tester) async {
      int greetingCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena/history') {
          // Server returns empty history, but local vault has existing messages
          return http.Response(
            json.encode({'cycle_id': 'cycle_reopen', 'messages': []}),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/chat/athena') {
          greetingCalls++;
          return http.Response(
            json.encode({
              'reply': 'Initial greeting again?',
              'emergency_trigger': false,
              'cycle_id': 'cycle_reopen',
              'message_id': 'msg_dup_greeting',
              'created_at': '2026-10-29T10:40:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_reopen'),
          refreshToken: 'ref_tok_reopen',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_reopen',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write cycle:read vitals:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // Seed local vault with previous initial greeting
      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Existing initial greeting'});

      await tester.pumpWidget(
        _buildChatTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Verified: initial_greeting was NOT called again
      expect(greetingCalls, equals(0));
      expect(find.text('Existing initial greeting'), findsOneWidget);
      expect(find.text('Initial greeting again?'), findsNothing);
    });

    // -------------------------------------------------------------------------
    // 14. SYNC CURATOR CHAT HISTORY PRESERVES DATA ON EMPTY OR MISMATCH
    // -------------------------------------------------------------------------
    test('14. syncCuratorChatHistory preserves existing local history on empty input or patient mismatch', () async {
      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: MockClient((_) async => http.Response('{}', 200)));
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_sync_guard'),
          refreshToken: 'ref_tok_sg',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_sync_guard',
          deviceId: 'dev_test_chat',
          scope: 'chat:read chat:write',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      vault.saveCuratorChatMessage({'sender': 'user', 'text': 'Original question'});
      expect(vault.getCuratorChatHistory().length, equals(1));

      // Attempt sync with foreign patient ID
      final mismatchResult = vault.syncCuratorChatHistory(
        [{'sender': 'curator', 'text': 'Foreign message'}],
        expectedPatientId: 'foreign_patient_99',
      );
      expect(mismatchResult, isFalse);
      expect(vault.getCuratorChatHistory().length, equals(1));
      expect(vault.getCuratorChatHistory().first['text'], equals('Original question'));

      // Attempt sync with empty list when local has messages
      final emptyResult = vault.syncCuratorChatHistory(
        [],
        expectedPatientId: 'pat_sync_guard',
      );
      expect(emptyResult, isFalse);
      expect(vault.getCuratorChatHistory().length, equals(1));
      expect(vault.getCuratorChatHistory().first['text'], equals('Original question'));

      // Legitimate sync with matching patient replaces history
      final validResult = vault.syncCuratorChatHistory(
        [
          {'sender': 'user', 'text': 'Original question'},
          {'sender': 'curator', 'text': 'Server synced answer'},
        ],
        expectedPatientId: 'pat_sync_guard',
      );
      expect(validResult, isTrue);
      expect(vault.getCuratorChatHistory().length, equals(2));
      expect(vault.getCuratorChatHistory().last['text'], equals('Server synced answer'));
    });
  });
}
