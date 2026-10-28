import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryStorage;

  setUp(() {
    VaultService.resetWriteQueueForTesting();
    memoryStorage = InMemoryVaultStorage();
    VaultStorageService.setStorageForTesting(memoryStorage);
  });

  tearDown(() {
    VaultService.resetWriteQueueForTesting();
    VaultStorageService.setStorageForTesting(null);
  });

  group('PROJECT AEGIS — STAGE 5.4D-2: PATIENT ISOLATION & MEMORY PURGE', () {
    test('1. Patient A -> Patient B transition: immediate synchronous memory wipe and zero data leakage', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      // 1. Activate Patient A
      await vault.setActivePatientId('patient_A');
      expect(vault.activePatientId, 'patient_A');
      expect(vault.isAuthenticated, isTrue);

      // Save Patient A specific clinical data
      vault.saveAnamnesis('gender', 'Male');
      vault.saveAnamnesis('age', '45');
      vault.saveAnamnesis('chronic_diseases', 'Артериальная гипертензия');
      vault.saveVitalsThresholds({
        'min_pessimum_bpm': 55,
        'max_pessimum_bpm': 135,
        'max_spike_delta_bpm': 35,
      });
      vault.saveCuratorChatMessage({'role': 'curator', 'text': 'Curator note for Patient A'});
      vault.saveAthenaClinicalReport({
        'consultation_id': 'ath_log_A1',
        'rationale': 'Patient A cardiac review',
      });
      vault.createWorkoutSession(
        sessionId: 'ws_pat_A',
        anomalyDetectedAt: DateTime.parse('2026-10-28T10:00:00Z'),
        activityConfirmedAt: DateTime.parse('2026-10-28T10:00:00Z'),
      );

      // Verify Patient A memory state
      expect(vault.getSystemContext('global')['gender'], 'Male');
      expect(vault.getSystemContext('global')['age'], '45');
      expect(vault.getSystemContext('cvs')['chronic_cvs_pathology'], isNotNull);
      expect(vault.getCuratorChatHistory(), isNotEmpty);
      expect(vault.getAthenaClinicalLogs(), isNotEmpty);
      expect(vault.getWorkoutSessions(), hasLength(1));

      // Check listener notifications on switch
      int notifyCount = 0;
      bool verifiedPurgeDuringNotify = false;

      void listener() {
        notifyCount++;
        // On the very first notifyListener after switch, memory MUST already be wiped clean!
        if (notifyCount == 1) {
          final global = vault.getSystemContext('global');
          if (global['gender'] == null &&
              global['age'] == null &&
              vault.getCuratorChatHistory().isEmpty &&
              vault.getAthenaClinicalLogs().isEmpty &&
              vault.getWorkoutSessions().isEmpty) {
            verifiedPurgeDuringNotify = true;
          }
        }
      }

      vault.addListener(listener);

      // 2. Switch to Patient B
      await vault.setActivePatientId('patient_B');

      vault.removeListener(listener);

      expect(verifiedPurgeDuringNotify, isTrue,
          reason: 'Old patient data must be synchronously purged before any UI render!');
      expect(vault.activePatientId, 'patient_B');
      expect(vault.getSystemContext('global')['gender'], isNull);
      expect(vault.getSystemContext('global')['age'], isNull);
      expect(vault.getSystemContext('cvs')['chronic_cvs_pathology'], isNull);
      expect(vault.getCuratorChatHistory(), isEmpty);
      expect(vault.getAthenaClinicalLogs(), isEmpty);
      expect(vault.getWorkoutSessions(), isEmpty);

      // 3. Populate Patient B data
      vault.saveAnamnesis('gender', 'Female');
      vault.saveAnamnesis('age', '32');
      vault.saveVitalsThresholds({
        'min_pessimum_bpm': 60,
        'max_pessimum_bpm': 140,
        'max_spike_delta_bpm': 30,
      });

      // 4. Verify secure storage key isolation
      final keyA = VaultStorageService.derivePatientStorageKey('patient_A');
      final keyB = VaultStorageService.derivePatientStorageKey('patient_B');

      expect(keyA, isNot(equals(keyB)));
      expect(keyA, isNot(contains('patient_A')));
      expect(keyB, isNot(contains('patient_B')));

      final storedA = await memoryStorage.loadPatientVault(keyA);
      final storedB = await memoryStorage.loadPatientVault(keyB);

      expect(storedA['system_folders']['global']['gender'], 'Male');
      expect(storedB['system_folders']['global']['gender'], 'Female');
      expect(storedB['curator_chat_history'], isEmpty);

      // 5. Switch back to Patient A -> restored from secure storage
      await vault.setActivePatientId('patient_A');
      expect(vault.getSystemContext('global')['gender'], 'Male');
      expect(vault.getSystemContext('global')['age'], '45');
      expect(vault.getCuratorChatHistory(), hasLength(1));
      expect(vault.getAthenaClinicalLogs(), hasLength(1));
      expect(vault.getWorkoutSessions(), hasLength(1));

      vault.dispose();
    });

    test('2. Logout / Revoke / Unauthenticated: immediate memory purge and unload', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      await vault.setActivePatientId('patient_authenticated');
      vault.saveAnamnesis('gender', 'Male');
      vault.saveCuratorChatMessage({'role': 'curator', 'text': 'Sensitive info'});

      expect(vault.isAuthenticated, isTrue);
      expect(vault.getCuratorChatHistory(), isNotEmpty);

      // Simulate logout/revoke
      await vault.setActivePatientId(null);

      expect(vault.activePatientId, isNull);
      expect(vault.isAuthenticated, isFalse);
      expect(vault.getSystemContext('global')['gender'], isNull);
      expect(vault.getCuratorChatHistory(), isEmpty);
      expect(vault.getAthenaClinicalLogs(), isEmpty);
      expect(vault.getWorkoutSessions(), isEmpty);

      vault.dispose();
    });

    test('3. App restart: cold start restores correct patient data from secure storage', () async {
      // Step 1: Patient A saves data in Session 1
      final vault1 = VaultService(storage: memoryStorage);
      await vault1.initFuture;
      await vault1.setActivePatientId('patient_cold_start');
      vault1.saveAnamnesis('gender', 'Female');
      vault1.saveAnamnesis('weight', '58');
      vault1.saveVitalsThresholds({
        'min_pessimum_bpm': 52,
        'max_pessimum_bpm': 128,
        'max_spike_delta_bpm': 35,
      });
      vault1.dispose();

      // Step 2: New VaultService instance simulates app restart with same patient
      final vault2 = VaultService(storage: memoryStorage);
      await vault2.initFuture;
      await vault2.setActivePatientId('patient_cold_start');

      expect(vault2.getSystemContext('global')['gender'], 'Female');
      expect(vault2.getSystemContext('global')['weight'], '58');
      final thresholds = vault2.getVitalsThresholds();
      expect(thresholds, isNotNull);
      expect(thresholds!.minPessimumBpm, 52);
      expect(thresholds.maxPessimumBpm, 128);
      expect(thresholds.maxSpikeDeltaBpm, 35);

      vault2.dispose();
    });

    test('4. Parallel async race & Stale callbacks (epoch fencing): stale load from Patient A is discarded', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      // Prepare Patient A with existing data in storage
      final keyA = VaultStorageService.derivePatientStorageKey('patient_A');
      await memoryStorage.savePatientVault(keyA, {
        'system_folders': {
          'global': {'gender': 'Male', 'tag': 'from_patient_A'},
        },
        'health_systems': {},
        'curator_chat_history': [],
        'athena_clinical_logs': [],
      });

      // Prepare Patient B with distinct data in storage
      final keyB = VaultStorageService.derivePatientStorageKey('patient_B');
      await memoryStorage.savePatientVault(keyB, {
        'system_folders': {
          'global': {'gender': 'Female', 'tag': 'from_patient_B'},
        },
        'health_systems': {},
        'curator_chat_history': [],
        'athena_clinical_logs': [],
      });

      // Start loading Patient A, then immediately switch to Patient B
      final futureA = vault.setActivePatientId('patient_A');
      final futureB = vault.setActivePatientId('patient_B');

      await Future.wait([futureA, futureB]);

      // Patient B MUST be the active profile, with Patient B data ONLY
      expect(vault.activePatientId, 'patient_B');
      expect(vault.getSystemContext('global')['gender'], 'Female');
      expect(vault.getSystemContext('global')['tag'], 'from_patient_B');

      vault.dispose();
    });
  });

  group('PROJECT AEGIS — STAGE 5.4D-2: GUEST ANAMNESIS DRAFT', () {
    test('1. Guest draft saves in unauthenticated mode and survives simulated app restart', () async {
      // Step 1: User enters anamnesis as a guest (before pairing)
      final guestVault1 = VaultService(storage: memoryStorage);
      await guestVault1.initFuture;
      expect(guestVault1.activePatientId, isNull);

      guestVault1.saveAnamnesis('gender', 'Female');
      guestVault1.saveAnamnesis('age', '29');
      guestVault1.saveAnamnesis('height', '168');
      guestVault1.saveAnamnesis('weight', '55');

      // Verify draft is securely stored under guest draft key
      final hasDraft = await guestVault1.hasGuestAnamnesisDraft();
      expect(hasDraft, isTrue);

      final rawDraft = await guestVault1.getGuestAnamnesisDraft();
      expect(rawDraft, isNotNull);
      expect(rawDraft!['global']['gender'], 'Female');
      expect(rawDraft['global']['age'], '29');
      guestVault1.dispose();

      // Step 2: App restarts (new VaultService instance in unauthenticated mode)
      final guestVault2 = VaultService(storage: memoryStorage);
      await guestVault2.initFuture;

      expect(guestVault2.activePatientId, isNull);
      expect(guestVault2.getSystemContext('global')['gender'], 'Female');
      expect(guestVault2.getSystemContext('global')['age'], '29');
      expect(guestVault2.getSystemContext('global')['height'], '168');
      expect(guestVault2.getSystemContext('global')['weight'], '55');

      guestVault2.dispose();
    });

    test('2. Guest draft survives pairing cancellation', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      vault.saveAnamnesis('gender', 'Male');
      vault.saveAnamnesis('age', '50');

      // User initiates pairing, but cancels or pairing fails
      // Session status remains unauthenticated (patientId stays null)
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      // Verify draft remains 100% intact
      final draft = await vault.getGuestAnamnesisDraft();
      expect(draft!['global']['gender'], 'Male');
      expect(draft['global']['age'], '50');

      vault.dispose();
    });

    test('3. Successful pairing associates guest draft with confirmed patient and does not delete until server sync', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      // Guest creates draft
      vault.saveAnamnesis('gender', 'Male');
      vault.saveAnamnesis('age', '42');
      vault.saveAnamnesis('critical_infections', 'хронический гепатит B');

      // Pairing succeeds: active patient confirmed
      await vault.setActivePatientId('patient_confirmed_01');
      expect(vault.activePatientId, 'patient_confirmed_01');

      // Associate guest draft to active patient
      final associated = await vault.associateGuestDraftToActivePatient();
      expect(associated, isTrue);

      // Patient now has the anamnesis
      expect(vault.getSystemContext('global')['gender'], 'Male');
      expect(vault.getSystemContext('global')['age'], '42');
      expect(vault.getSystemContext('gi_tract')['hepatocyte_cytolysis_fibrosis'], isNotNull);

      // IMPORTANT: Draft is NOT deleted yet from secure storage!
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      // Simulate server confirmation of anamnesis intake sync
      await vault.clearGuestDraftAfterServerSync();
      expect(await vault.hasGuestAnamnesisDraft(), isFalse);

      // Patient vault still holds the data
      expect(vault.getSystemContext('global')['gender'], 'Male');

      vault.dispose();
    });

    test('4. Guest draft cannot be associated without an active authenticated patient', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      vault.saveAnamnesis('gender', 'Female');
      final result = await vault.associateGuestDraftToActivePatient();

      expect(result, isFalse, reason: 'Cannot associate draft to null patient');
      vault.dispose();
    });
  });

  group('PROJECT AEGIS — STAGE 5.4D-2: LEGACY VAULT ISOLATION', () {
    test('1. Existing legacy data in aegis_vault_persistent_storage_v1 is quarantined and NOT attributed to newly paired patient', () async {
      // Simulate legacy unauthenticated data from prior stages
      memoryStorage.legacyStore = {
        'health_systems': {
          'cvs': {
            'name': 'Cardiovascular',
            'completeness': 75.0,
            'color': '0xFFFF0000',
            'markers': {
              'smart_watch': {
                'pressure': {'systolic': 130, 'diastolic': 85},
              }
            }
          }
        },
        'system_folders': {
          'global': {'legacy_data': 'unverified_patient_records'},
        },
        'curator_chat_history': [
          {'role': 'curator', 'content': 'Old unverified legacy message'}
        ],
      };

      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      // Status must indicate quarantined legacy data
      expect(vault.hasQuarantinedLegacyData, isTrue);
      expect(vault.legacyVaultStatus, LegacyVaultStatus.quarantined);

      // When Patient A logs in / pairs:
      await vault.setActivePatientId('patient_clean_A');

      // Patient A MUST NOT receive the legacy data
      expect(vault.getSystemContext('global')['legacy_data'], isNull);
      expect(vault.getCuratorChatHistory(), isEmpty);
      expect(vault.systolic, isNull);

      // Legacy data remains quarantined and preserved in storage
      final inspected = await vault.inspectQuarantinedLegacyData();
      expect(inspected, isNotNull);
      expect(inspected!['system_folders']['global']['legacy_data'], 'unverified_patient_records');

      // Legacy data is NOT deleted
      expect(memoryStorage.legacyStore, isNotNull);

      vault.dispose();
    });
  });

  group('PROJECT AEGIS — STAGE 5.4D-2: RUNTIME INTEGRATION & AUTH SESSION MANAGER', () {
    test('1. VaultService dynamically synchronizes with AuthSessionManager lifecycle events', () async {
      final mockClient = MockClient((request) async => http.Response('{"ok":true}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authStorage = InMemoryAuthTokenStorage();
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: authStorage,
      );

      final vault = VaultService(storage: memoryStorage);
      vault.bindAuthSessionManager(authManager);
      await vault.initFuture;

      // Initially unauthenticated
      expect(vault.activePatientId, isNull);
      expect(vault.isAuthenticated, isFalse);

      // Simulate pairing event via AuthSessionManager
      final now = DateTime.now().toUtc();
      final sessionA = AuthSession(
        accessToken: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwYXRpZW50X2FscGhhIiwiZXhwIjoyNTM0MDIzMDA3OTl9.sig',
        expiresIn: 3600,
        expiresAt: now.add(const Duration(hours: 1)),
        refreshToken: 'rt_alpha_1',
        refreshExpiresAt: now.add(const Duration(days: 30)),
        patientId: 'patient_alpha',
        deviceId: 'dev_001',
        scope: 'vitals:read vitals:write cascade:intake cascade:run reports:read chat:read chat:write cycle:read',
      );
      authManager.setSessionForTesting(sessionA, status: AuthSessionStatus.authenticated);

      expect(vault.activePatientId, 'patient_alpha');
      expect(vault.isAuthenticated, isTrue);

      vault.saveAnamnesis('gender', 'Male');
      expect(vault.getSystemContext('global')['gender'], 'Male');

      // Revoke / disconnect session via AuthSessionManager
      authManager.setSessionForTesting(null, status: AuthSessionStatus.unauthenticated);

      expect(vault.activePatientId, isNull);
      expect(vault.isAuthenticated, isFalse);
      expect(vault.getSystemContext('global')['gender'], isNull);

      await Future.delayed(const Duration(milliseconds: 20));
      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('2. Secure storage failure handled fail-closed without corrupting state', () async {
      final failingStorage = InMemoryVaultStorage();
      final vault = VaultService(storage: failingStorage);
      await vault.initFuture;

      await vault.setActivePatientId('patient_safe');
      failingStorage.failWithException = true;

      // Attempting to save should not crash or corrupt in-memory state
      expect(
        () => vault.saveAnamnesis('gender', 'Female'),
        returnsNormally,
      );

      vault.dispose();
    });
  });

  group('PROJECT AEGIS — STAGE 5.4D-2: TARGETED VAULT PERSISTENCE HARDENING', () {
    test('1. Write serialization & stale overwrite prevention: first write completing later than second write does not overwrite newer state', () async {
      final controlledStorage = InMemoryVaultStorage();
      final vault = VaultService(storage: controlledStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_ordered');

      final completer1 = Completer<void>();
      final completer2 = Completer<void>();
      int saveCount = 0;
      final List<String> executionLog = [];

      controlledStorage.onSavePatientVault = (storageKey, data) async {
        saveCount++;
        final currentCall = saveCount;
        if (currentCall == 1) {
          executionLog.add('write_1_invoked');
          await completer1.future;
          executionLog.add('write_1_finished');
        } else if (currentCall == 2) {
          executionLog.add('write_2_invoked');
          await completer2.future;
          executionLog.add('write_2_finished');
        }
      };

      // 1. Dispatch first write: sets gender = 'Male'
      vault.saveAnamnesis('gender', 'Male');
      expect(executionLog, contains('write_1_invoked'));
      expect(executionLog, isNot(contains('write_2_invoked')));

      // 2. Dispatch second write immediately: sets gender = 'Female'
      vault.saveAnamnesis('gender', 'Female');

      // Crucial verification: Write 2 MUST BE QUEUED behind Write 1, NOT invoked concurrently!
      expect(executionLog, contains('write_1_invoked'));
      expect(executionLog, isNot(contains('write_2_invoked')),
          reason: 'Write 2 must wait for Write 1 to finish before invoking storage');

      // 3. Complete Write 1 (which was delayed)
      completer1.complete();
      await Future.delayed(const Duration(milliseconds: 10));

      // Now Write 1 finished, and Write 2 is invoked
      expect(executionLog, contains('write_1_finished'));
      expect(executionLog, contains('write_2_invoked'));

      // 4. Complete Write 2
      completer2.complete();
      await vault.flushPendingWrites();

      expect(executionLog, contains('write_2_finished'));

      // 5. Verify final storage snapshot: must contain 'Female' (the latest state), never overwritten by 'Male'
      final key = VaultStorageService.derivePatientStorageKey('patient_ordered');
      final stored = await controlledStorage.loadPatientVault(key);
      expect(stored['system_folders']['global']['gender'], 'Female');

      vault.dispose();
    });

    test('2. Patient profile switch during in-flight write: Patient A write finishes to Patient A key and does not leak or contaminate Patient B', () async {
      final controlledStorage = InMemoryVaultStorage();
      final vault = VaultService(storage: controlledStorage);
      await vault.initFuture;

      final completerA = Completer<void>();
      bool writeAPaused = false;

      final keyA = VaultStorageService.derivePatientStorageKey('patient_alpha');
      final keyB = VaultStorageService.derivePatientStorageKey('patient_beta');

      controlledStorage.onSavePatientVault = (storageKey, data) async {
        if (storageKey == keyA) {
          writeAPaused = true;
          await completerA.future;
        }
      };

      // Step 1: Start on Patient Alpha and initiate write
      await vault.setActivePatientId('patient_alpha');
      vault.saveAnamnesis('gender', 'Male');
      expect(writeAPaused, isTrue);

      // Step 2: Switch to Patient Beta while Alpha write is still in flight
      // Synchronous purge MUST happen immediately
      final switchFuture = vault.setActivePatientId('patient_beta');
      expect(vault.activePatientId, 'patient_beta');
      expect(vault.getSystemContext('global')['gender'], isNull,
          reason: 'Memory must be wiped immediately on switch');

      // In Patient Beta, enter different anamnesis
      vault.saveAnamnesis('gender', 'Female');
      expect(vault.getSystemContext('global')['gender'], 'Female');

      // Step 3: Now complete Patient Alpha\'s write
      completerA.complete();
      await switchFuture;
      await vault.flushPendingWrites();

      // Step 4: Verify storage separation at rest
      final storedA = await controlledStorage.loadPatientVault(keyA);
      final storedB = await controlledStorage.loadPatientVault(keyB);

      expect(storedA['system_folders']['global']['gender'], 'Male');
      expect(storedB['system_folders']['global']['gender'], 'Female');

      // Verify active memory is STILL Patient Beta with 'Female'
      expect(vault.getSystemContext('global')['gender'], 'Female');

      vault.dispose();
    });

    test('3. Patient switch during guest draft read: session epoch fencing aborts association and prevents draft leakage to another patient', () async {
      final controlledStorage = InMemoryVaultStorage();
      // Pre-seed guest draft with sensitive guest intake
      await controlledStorage.saveGuestDraft({
        'global': {'gender': 'Non-binary', 'age': '27', 'spec_factors': 'Night shift'},
        'notes_export': {'anamnesis_gender': 'Non-binary'},
        'version': 1,
      });

      final vault = VaultService(storage: controlledStorage);
      await vault.initFuture;

      final readCompleter = Completer<void>();
      bool readStarted = false;

      controlledStorage.onLoadGuestDraft = () async {
        readStarted = true;
        await readCompleter.future;
      };

      // Authenticate Patient A
      await vault.setActivePatientId('patient_intended_recipient');
      expect(vault.activePatientId, 'patient_intended_recipient');

      // Start associateGuestDraftToActivePatient() which begins async read
      final associateFuture = vault.associateGuestDraftToActivePatient();
      expect(readStarted, isTrue);

      // User switches to Patient B (or logs out) while read is in-flight!
      await vault.setActivePatientId('patient_unintended_recipient');
      expect(vault.activePatientId, 'patient_unintended_recipient');

      // Complete the delayed read
      readCompleter.complete();
      final associationSuccess = await associateFuture;

      // Association MUST FAIL (aborted due to epoch fencing)
      expect(associationSuccess, isFalse,
          reason: 'Association must fail when session changes during async read');

      // Patient B must NOT receive the guest draft in memory
      expect(vault.getSystemContext('global')['gender'], isNull);
      expect(vault.getSystemContext('global')['spec_factors'], isNull);

      // Patient B storage must NOT contain the guest draft
      final keyB = VaultStorageService.derivePatientStorageKey('patient_unintended_recipient');
      final storedB = await controlledStorage.loadPatientVault(keyB);
      expect(storedB['system_folders']?['global']?['gender'], isNull);

      // The guest draft in storage must NOT be deleted
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      vault.dispose();
    });

    test('4. Secure storage failure propagation: fail-closed error reporting, no false confirmation, guest draft preserved', () async {
      final controlledStorage = InMemoryVaultStorage();
      await controlledStorage.saveGuestDraft({
        'global': {'gender': 'Male', 'age': '48'},
        'notes_export': {},
        'version': 1,
      });

      final vault = VaultService(storage: controlledStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_fail_test');

      // Storage fails during patient vault write
      controlledStorage.onSavePatientVault = (key, data) async {
        throw const SecureStorageUnavailableException(
          'OS Keystore hardware module locked or unavailable.',
        );
      };

      // Association attempt
      final success = await vault.associateGuestDraftToActivePatient();

      // 1. MUST NOT confirm save
      expect(success, isFalse, reason: 'Must not report success when storage write fails');

      // 2. Error state recorded
      expect(vault.hasPersistenceError, isTrue);
      expect(vault.lastPersistenceError, isA<SecureStorageUnavailableException>());

      // 3. Guest draft in storage MUST NOT be deleted
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      // 4. flushPendingWrites rethrows the exception
      expect(
        () => vault.flushPendingWrites(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // 5. In-memory data is not silently wiped or corrupted
      expect(vault.getSystemContext('global')['gender'], 'Male');

      vault.dispose();
    });

    test('5. Cold restart data preservation: complete medical history and derived structures restored after VaultService re-creation', () async {
      final sharedStorage = InMemoryVaultStorage();

      // Session 1: Patient fills out extensive anamnesis
      final vaultSession1 = VaultService(storage: sharedStorage);
      await vaultSession1.initFuture;
      await vaultSession1.setActivePatientId('patient_full_lifecycle');

      vaultSession1.saveAnamnesis('gender', 'Female');
      vaultSession1.saveAnamnesis('age', '38');
      vaultSession1.saveAnamnesis('height', '172');
      vaultSession1.saveAnamnesis('weight', '64');
      vaultSession1.saveAnamnesis('nicotine_status', 'Non-user');
      vaultSession1.saveAnamnesis('chronic_diseases', 'Бронхиальная астма');
      vaultSession1.saveVitalsThresholds({
        'min_pessimum_bpm': 50,
        'max_pessimum_bpm': 130,
        'max_spike_delta_bpm': 30,
      });
      vaultSession1.saveCuratorChatMessage({'role': 'patient', 'text': 'My breathing is stable.'});

      await vaultSession1.flushPendingWrites();
      vaultSession1.dispose();

      // Session 2: Cold restart with new VaultService instance
      final vaultSession2 = VaultService(storage: sharedStorage);
      await vaultSession2.initFuture;
      await vaultSession2.setActivePatientId('patient_full_lifecycle');

      // Verify all data restored accurately
      expect(vaultSession2.getSystemContext('global')['gender'], 'Female');
      expect(vaultSession2.getSystemContext('global')['age'], '38');
      expect(vaultSession2.getSystemContext('global')['height'], '172');
      expect(vaultSession2.getSystemContext('global')['weight'], '64');
      expect(vaultSession2.getSystemContext('global')['nicotine_status'], 'Non-user');
      expect(vaultSession2.getSystemContext('respiratory')['smoking_nicotine_status'], 'Non-user');
      expect(vaultSession2.getCuratorChatHistory(), hasLength(1));
      expect(vaultSession2.getCuratorChatHistory().first['text'], 'My breathing is stable.');

      final thresholds = vaultSession2.getVitalsThresholds();
      expect(thresholds, isNotNull);
      expect(thresholds!.minPessimumBpm, 50);
      expect(thresholds.maxPessimumBpm, 130);

      vaultSession2.dispose();
    });

    test('6. Clear vs pending write serialization: in-flight write finishes before clear, clear wipes it, no stale resurrection', () async {
      final controlledStorage = InMemoryVaultStorage();
      final vault = VaultService(storage: controlledStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_clear_race');

      final key = VaultStorageService.derivePatientStorageKey('patient_clear_race');

      // 1. Initial write to establish baseline
      vault.saveAnamnesis('initial_key', 'initial_val');
      await vault.flushPendingWrites();
      expect((await controlledStorage.loadPatientVault(key))['system_folders']?['global']?['initial_key'], 'initial_val');

      // 2. Prepare completer to pause in-flight write
      final writePauseCompleter = Completer<void>();
      final writeStartedCompleter = Completer<void>();

      controlledStorage.onSavePatientVault = (storageKey, data) async {
        if (!writeStartedCompleter.isCompleted) {
          writeStartedCompleter.complete();
        }
        await writePauseCompleter.future;
      };

      // 3. Initiate second write (will be paused)
      vault.saveAnamnesis('pending_key', 'stale_val_that_should_be_wiped');
      await writeStartedCompleter.future; // ensure write is actively in-flight

      // 4. Request clear while write is still in-flight
      // Clear MUST be enqueued behind the in-flight write
      final clearFuture = vault.clearPatientVault('patient_clear_race');

      // 5. Release the in-flight write so it completes
      writePauseCompleter.complete();

      // 6. Await clear to finish
      await clearFuture;

      // 7. Verify storage is completely empty: stale write did not resurrect or survive clear
      final reloaded = await controlledStorage.loadPatientVault(key);
      expect(reloaded, isEmpty, reason: 'Vault must be completely empty after clear; in-flight write must not resurrect data');

      vault.dispose();
    });

    test('7. Persistence error accounting: failing write on Patient A does not mask or affect Patient B, error remains observable', () async {
      final controlledStorage = InMemoryVaultStorage();
      final vault = VaultService(storage: controlledStorage);
      await vault.initFuture;

      final keyA = VaultStorageService.derivePatientStorageKey('patient_fail_A');
      final keyB = VaultStorageService.derivePatientStorageKey('patient_success_B');

      // Configure storage: writes for Patient A fail, writes for Patient B succeed
      controlledStorage.onSavePatientVault = (storageKey, data) async {
        if (storageKey == keyA) {
          throw const SecureStorageUnavailableException('Simulated disk error on patient A');
        }
      };

      // 1. Write for Patient A fails
      await vault.setActivePatientId('patient_fail_A');
      vault.saveAnamnesis('symptom', 'migraine');

      // Await Patient A write chain to record error
      try {
        await vault.flushPendingWrites(patientId: 'patient_fail_A');
      } catch (_) {}

      // Verify Patient A error recorded
      expect(vault.hasPersistenceError, isTrue);
      expect(vault.lastPersistenceError, isA<SecureStorageUnavailableException>());
      expect(vault.getPersistenceError(patientId: 'patient_fail_A'), isA<SecureStorageUnavailableException>());

      // 2. Write for Patient B succeeds
      await vault.setActivePatientId('patient_success_B');
      expect(vault.hasPersistenceError, isFalse, reason: 'Active patient B must not inherit patient A error');
      expect(vault.lastPersistenceError, isNull);

      vault.saveAnamnesis('symptom', 'healthy');
      // Flushing patient B MUST succeed without throwing
      await vault.flushPendingWrites(patientId: 'patient_success_B');
      await vault.flushPendingWrites(); // active patient B flush

      // 3. Patient B storage is saved successfully
      final storedB = await controlledStorage.loadPatientVault(keyB);
      expect(storedB['system_folders']?['global']?['symptom'], 'healthy');

      // 4. Patient A error MUST STILL BE OBSERVABLE (not masked or cleared by patient B success!)
      expect(vault.getPersistenceError(patientId: 'patient_fail_A'), isA<SecureStorageUnavailableException>());
      expect(
        () => vault.flushPendingWrites(patientId: 'patient_fail_A'),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // 5. Subsequent successful write on Patient A clears ONLY Patient A's error
      controlledStorage.onSavePatientVault = null; // restore storage health
      await vault.setActivePatientId('patient_fail_A');
      vault.saveAnamnesis('recovery', 'confirmed');
      await vault.flushPendingWrites();

      expect(vault.hasPersistenceError, isFalse);
      expect(vault.lastPersistenceError, isNull);
      expect(vault.getPersistenceError(patientId: 'patient_fail_A'), isNull);

      vault.dispose();
    });
  });
}
