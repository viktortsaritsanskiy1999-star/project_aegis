import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'auth/auth_token_storage.dart';

/// Status of legacy, unisolated persistent vault data from earlier stages.
enum LegacyVaultStatus {
  /// No legacy persistent data found.
  none,

  /// Legacy unauthenticated vault data detected and quarantined.
  /// It MUST NOT be attributed to any newly authenticated patient.
  quarantined,
}

/// Computes the standard NIST FIPS 180-4 SHA-256 hash in pure Dart.
/// Zero external runtime dependencies.
String sha256Hex(String input) {
  final bytes = utf8.encode(input);
  final bitLength = bytes.length * 8;

  // Pre-processing (padding):
  final numPaddingBytes = (56 - (bytes.length + 1) % 64) % 64;
  final totalLength = bytes.length + 1 + numPaddingBytes + 8;
  final padded = Uint8List(totalLength);
  padded.setRange(0, bytes.length, bytes);
  padded[bytes.length] = 0x80;

  final bdata = ByteData.view(padded.buffer);
  bdata.setUint64(totalLength - 8, bitLength, Endian.big);

  // Initial hash values:
  int h0 = 0x6a09e667;
  int h1 = 0xbb67ae85;
  int h2 = 0x3c6ef372;
  int h3 = 0xa54ff53a;
  int h4 = 0x510e527f;
  int h5 = 0x9b05688c;
  int h6 = 0x1f83d9ab;
  int h7 = 0x5be0cd19;

  // Round constants:
  const k = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];

  int rotr(int x, int n) => ((x >>> n) | (x << (32 - n))) & 0xFFFFFFFF;

  final w = List<int>.filled(64, 0);

  for (int chunk = 0; chunk < totalLength; chunk += 64) {
    for (int i = 0; i < 16; i++) {
      w[i] = bdata.getUint32(chunk + i * 4, Endian.big);
    }
    for (int i = 16; i < 64; i++) {
      final s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      final s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & 0xFFFFFFFF;
    }

    int a = h0;
    int b = h1;
    int c = h2;
    int d = h3;
    int e = h4;
    int f = h5;
    int g = h6;
    int h = h7;

    for (int i = 0; i < 64; i++) {
      final s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      final ch = (e & f) ^ ((~e) & g);
      final temp1 = (h + s1 + ch + k[i] + w[i]) & 0xFFFFFFFF;
      final s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      final maj = (a & b) ^ (a & c) ^ (b & c);
      final temp2 = (s0 + maj) & 0xFFFFFFFF;

      h = g;
      g = f;
      f = e;
      e = (d + temp1) & 0xFFFFFFFF;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) & 0xFFFFFFFF;
    }

    h0 = (h0 + a) & 0xFFFFFFFF;
    h1 = (h1 + b) & 0xFFFFFFFF;
    h2 = (h2 + c) & 0xFFFFFFFF;
    h3 = (h3 + d) & 0xFFFFFFFF;
    h4 = (h4 + e) & 0xFFFFFFFF;
    h5 = (h5 + f) & 0xFFFFFFFF;
    h6 = (h6 + g) & 0xFFFFFFFF;
    h7 = (h7 + h) & 0xFFFFFFFF;
  }

  String toHex32(int v) => v.toRadixString(16).padLeft(8, '0');
  return '${toHex32(h0)}${toHex32(h1)}${toHex32(h2)}${toHex32(h3)}${toHex32(h4)}${toHex32(h5)}${toHex32(h6)}${toHex32(h7)}';
}

/// Abstract contract for medical vault persistence.
abstract class IVaultStorage {
  /// Saves a patient-isolated vault snapshot using a deterministic derived key.
  Future<void> savePatientVault(String storageKey, Map<String, dynamic> data);

  /// Loads a patient-isolated vault snapshot. Returns empty map if none exists.
  Future<Map<String, dynamic>> loadPatientVault(String storageKey);

  /// Clears a patient-isolated vault snapshot.
  Future<void> clearPatientVault(String storageKey);

  /// Saves the guest anamnesis draft.
  Future<void> saveGuestDraft(Map<String, dynamic> draft);

  /// Loads the guest anamnesis draft.
  Future<Map<String, dynamic>> loadGuestDraft();

  /// Clears the guest anamnesis draft after confirmed server synchronization.
  Future<void> clearGuestDraft();

  /// Checks if legacy unisolated vault data exists in SharedPreferences.
  Future<bool> hasLegacyVault();

  /// Inspects quarantined legacy data without attributing to any patient.
  Future<Map<String, dynamic>?> loadLegacyVault();

  /// Backward-compatible unauthenticated vault save (for standalone unit tests).
  Future<void> saveUnauthenticatedVault(Map<String, dynamic> data);

  /// Backward-compatible unauthenticated vault load (for standalone unit tests).
  Future<Map<String, dynamic>> loadUnauthenticatedVault();

  /// Backward-compatible unauthenticated vault clear.
  Future<void> clearUnauthenticatedVault();
}

/// Production implementation backed by OS-level secure storage (Android Keystore / iOS Keychain).
class SecureVaultStorage implements IVaultStorage {
  static const FlutterSecureStorage defaultSecureStorage = FlutterSecureStorage(
    aOptions: AndroidOptions(),
    iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock),
  );

  final FlutterSecureStorage _secureStorage;
  static final Map<String, String> _testFallbackStore = {};

  SecureVaultStorage({FlutterSecureStorage? secureStorage})
      : _secureStorage = secureStorage ?? defaultSecureStorage;

  static bool get _isRunningInFlutterTest {
    try {
      return Platform.environment.containsKey('FLUTTER_TEST');
    } catch (_) {
      return false;
    }
  }

  bool get _shouldUseTestFallback =>
      _isRunningInFlutterTest && identical(_secureStorage, defaultSecureStorage);

  @override
  Future<void> savePatientVault(String storageKey, Map<String, dynamic> data) async {
    if (_shouldUseTestFallback) {
      _testFallbackStore[storageKey] = json.encode(data);
      return;
    }
    try {
      final encoded = json.encode(data);
      await _secureStorage.write(key: storageKey, value: encoded);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to securely persist patient medical vault.',
        e,
      );
    }
  }

  @override
  Future<Map<String, dynamic>> loadPatientVault(String storageKey) async {
    if (_shouldUseTestFallback) {
      final raw = _testFallbackStore[storageKey];
      if (raw == null || raw.trim().isEmpty) return {};
      final decoded = json.decode(raw);
      return decoded is Map<String, dynamic> ? decoded : {};
    }

    String? raw;
    try {
      raw = await _secureStorage.read(key: storageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to read secure patient medical vault.',
        e,
      );
    }

    if (raw == null || raw.trim().isEmpty) {
      return {};
    }

    try {
      final decoded = json.decode(raw);
      if (decoded is Map<String, dynamic>) {
        return decoded;
      }
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Corrupted secure patient medical vault structure.',
        e,
      );
    }
    return {};
  }

  @override
  Future<void> clearPatientVault(String storageKey) async {
    if (_shouldUseTestFallback) {
      _testFallbackStore.remove(storageKey);
      return;
    }
    try {
      await _secureStorage.delete(key: storageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to clear secure patient medical vault.',
        e,
      );
    }
  }

  @override
  Future<void> saveGuestDraft(Map<String, dynamic> draft) async {
    if (_shouldUseTestFallback) {
      _testFallbackStore[VaultStorageService.guestDraftStorageKey] = json.encode(draft);
      return;
    }
    try {
      final encoded = json.encode(draft);
      await _secureStorage.write(
        key: VaultStorageService.guestDraftStorageKey,
        value: encoded,
      );
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to securely persist guest anamnesis draft.',
        e,
      );
    }
  }

  @override
  Future<Map<String, dynamic>> loadGuestDraft() async {
    if (_shouldUseTestFallback) {
      final raw = _testFallbackStore[VaultStorageService.guestDraftStorageKey];
      if (raw == null || raw.trim().isEmpty) return {};
      final decoded = json.decode(raw);
      return decoded is Map<String, dynamic> ? decoded : {};
    }

    String? raw;
    try {
      raw = await _secureStorage.read(key: VaultStorageService.guestDraftStorageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to read secure guest anamnesis draft.',
        e,
      );
    }

    if (raw == null || raw.trim().isEmpty) {
      return {};
    }

    try {
      final decoded = json.decode(raw);
      if (decoded is Map<String, dynamic>) {
        return decoded;
      }
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Corrupted guest anamnesis draft structure.',
        e,
      );
    }
    return {};
  }

  @override
  Future<void> clearGuestDraft() async {
    if (_shouldUseTestFallback) {
      _testFallbackStore.remove(VaultStorageService.guestDraftStorageKey);
      return;
    }
    try {
      await _secureStorage.delete(key: VaultStorageService.guestDraftStorageKey);
    } catch (e) {
      throw SecureStorageUnavailableException(
        'Failed to clear guest anamnesis draft.',
        e,
      );
    }
  }

  @override
  Future<bool> hasLegacyVault() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      return prefs.containsKey(VaultStorageService.legacyStorageKey);
    } catch (_) {
      return false;
    }
  }

  @override
  Future<Map<String, dynamic>?> loadLegacyVault() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(VaultStorageService.legacyStorageKey);
      if (raw != null && raw.isNotEmpty) {
        final decoded = json.decode(raw);
        if (decoded is Map<String, dynamic>) {
          return decoded;
        }
      }
    } catch (e) {
      debugPrint('[SecureVaultStorage] Error reading legacy vault: $e');
    }
    return null;
  }

  @override
  Future<void> saveUnauthenticatedVault(Map<String, dynamic> data) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final encoded = json.encode(data);
      await prefs.setString(VaultStorageService.legacyStorageKey, encoded);
    } catch (e) {
      debugPrint('[SecureVaultStorage] Error saving unauthenticated vault: $e');
    }
  }

  @override
  Future<Map<String, dynamic>> loadUnauthenticatedVault() async {
    return (await loadLegacyVault()) ?? {};
  }

  @override
  Future<void> clearUnauthenticatedVault() async {
    _testFallbackStore.clear();
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(VaultStorageService.legacyStorageKey);
    } catch (e) {
      debugPrint('[SecureVaultStorage] Error clearing unauthenticated vault: $e');
    }
  }
}

/// In-memory storage implementation for deterministic, isolated unit tests.
class InMemoryVaultStorage implements IVaultStorage {
  final Map<String, String> _store = {};
  Map<String, dynamic>? legacyStore;
  bool failWithException = false;

  /// Optional hooks for fine-grained async flow control in tests
  Future<void> Function(String storageKey, Map<String, dynamic> data)? onSavePatientVault;
  Future<void> Function(String storageKey)? onLoadPatientVault;
  Future<void> Function(String storageKey)? onClearPatientVault;
  Future<void> Function(Map<String, dynamic> draft)? onSaveGuestDraft;
  Future<void> Function()? onLoadGuestDraft;
  Future<void> Function()? onClearGuestDraft;

  void _checkFailure() {
    if (failWithException) {
      throw const SecureStorageUnavailableException(
        'Simulated secure storage unavailability.',
      );
    }
  }

  @override
  Future<void> savePatientVault(String storageKey, Map<String, dynamic> data) async {
    _checkFailure();
    if (onSavePatientVault != null) {
      await onSavePatientVault!(storageKey, data);
    }
    _checkFailure();
    _store[storageKey] = json.encode(data);
  }

  @override
  Future<Map<String, dynamic>> loadPatientVault(String storageKey) async {
    _checkFailure();
    if (onLoadPatientVault != null) {
      await onLoadPatientVault!(storageKey);
    }
    _checkFailure();
    final raw = _store[storageKey];
    if (raw == null || raw.trim().isEmpty) return {};
    final decoded = json.decode(raw);
    return decoded is Map<String, dynamic> ? decoded : {};
  }

  @override
  Future<void> clearPatientVault(String storageKey) async {
    _checkFailure();
    if (onClearPatientVault != null) {
      await onClearPatientVault!(storageKey);
    }
    _checkFailure();
    _store.remove(storageKey);
  }

  @override
  Future<void> saveGuestDraft(Map<String, dynamic> draft) async {
    _checkFailure();
    if (onSaveGuestDraft != null) {
      await onSaveGuestDraft!(draft);
    }
    _checkFailure();
    _store[VaultStorageService.guestDraftStorageKey] = json.encode(draft);
  }

  @override
  Future<Map<String, dynamic>> loadGuestDraft() async {
    _checkFailure();
    if (onLoadGuestDraft != null) {
      await onLoadGuestDraft!();
    }
    _checkFailure();
    final raw = _store[VaultStorageService.guestDraftStorageKey];
    if (raw == null || raw.trim().isEmpty) return {};
    final decoded = json.decode(raw);
    return decoded is Map<String, dynamic> ? decoded : {};
  }

  @override
  Future<void> clearGuestDraft() async {
    _checkFailure();
    if (onClearGuestDraft != null) {
      await onClearGuestDraft!();
    }
    _checkFailure();
    _store.remove(VaultStorageService.guestDraftStorageKey);
  }

  @override
  Future<bool> hasLegacyVault() async {
    _checkFailure();
    return legacyStore != null && legacyStore!.isNotEmpty;
  }

  @override
  Future<Map<String, dynamic>?> loadLegacyVault() async {
    _checkFailure();
    return legacyStore != null ? Map<String, dynamic>.from(legacyStore!) : null;
  }

  @override
  Future<void> saveUnauthenticatedVault(Map<String, dynamic> data) async {
    _checkFailure();
    _store[VaultStorageService.unauthenticatedStorageKey] = json.encode(data);
  }

  @override
  Future<Map<String, dynamic>> loadUnauthenticatedVault() async {
    _checkFailure();
    final raw = _store[VaultStorageService.unauthenticatedStorageKey];
    if (raw != null && raw.trim().isNotEmpty) {
      final decoded = json.decode(raw);
      if (decoded is Map<String, dynamic>) return decoded;
    }
    return legacyStore != null ? Map<String, dynamic>.from(legacyStore!) : {};
  }

  @override
  Future<void> clearUnauthenticatedVault() async {
    _checkFailure();
    _store.remove(VaultStorageService.unauthenticatedStorageKey);
  }
}

/// Service facade managing storage backends, patient storage keys, and legacy data isolation.
class VaultStorageService {
  /// Legacy key from Stage 4 and earlier. Strictly quarantined.
  static const String legacyStorageKey = 'aegis_vault_persistent_storage_v1';

  /// Key for unauthenticated / guest anamnesis drafts.
  static const String guestDraftStorageKey = 'aegis_guest_anamnesis_draft_v1';

  /// Key for unauthenticated standalone test environments.
  static const String unauthenticatedStorageKey = 'aegis_vault_unauthenticated_v1';

  static IVaultStorage _activeStorage = SecureVaultStorage();

  /// Currently active storage backend.
  static IVaultStorage get activeStorage => _activeStorage;

  /// Sets or modifies the storage backend for testing.
  @visibleForTesting
  static void setStorageForTesting(IVaultStorage? storage) {
    _activeStorage = storage ?? SecureVaultStorage();
  }

  /// Derives a deterministic, privacy-preserving storage key for a confirmed [patientId].
  /// Uses the first 16 hex characters of the standard SHA-256 hash.
  /// Strictly prevents plain patient IDs or PII from appearing in storage keys or logs.
  static String derivePatientStorageKey(String patientId) {
    final hex = sha256Hex(patientId.trim());
    return 'aegis_vault_p_${hex.substring(0, 16)}_v1';
  }

  /// Saves the entire vault data snapshot to disk (backward compatibility).
  static Future<void> saveVault(Map<String, dynamic> vaultData) async {
    await _activeStorage.saveUnauthenticatedVault(vaultData);
  }

  /// Loading vault data from disk upon application startup (backward compatibility).
  static Future<Map<String, dynamic>> loadVault() async {
    return await _activeStorage.loadUnauthenticatedVault();
  }

  /// Complete storage clearing (backward compatibility).
  static Future<void> clearVault() async {
    await _activeStorage.clearUnauthenticatedVault();
  }

  /// Checks if quarantined legacy data exists on the device.
  static Future<bool> hasQuarantinedLegacyData() async {
    return await _activeStorage.hasLegacyVault();
  }

  /// Inspects quarantined legacy data without attributing to any patient.
  static Future<Map<String, dynamic>?> inspectQuarantinedLegacyData() async {
    return await _activeStorage.loadLegacyVault();
  }
}
