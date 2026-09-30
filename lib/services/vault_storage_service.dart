import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

class VaultStorageService {
  static const String _storageKey = 'aegis_vault_persistent_storage_v1';

  /// Saving the entire vault data snapshot to disk
  static Future<void> saveVault(Map<String, dynamic> vaultData) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final String encodedData = json.encode(vaultData);
      await prefs.setString(_storageKey, encodedData);
    } catch (e) {
      print("⚠️ [VaultStorage] Error saving data to disk: $e");
    }
  }

  /// Loading vault data from disk upon application startup
  static Future<Map<String, dynamic>> loadVault() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final String? rawData = prefs.getString(_storageKey);

      if (rawData != null && rawData.isNotEmpty) {
        final decoded = json.decode(rawData);
        if (decoded is Map<String, dynamic>) {
          return decoded;
        }
      }
    } catch (e) {
      print("⚠️ [VaultStorage] Error reading data from disk: $e");
    }
    return {};
  }

  /// Complete storage clearing (if a reset is required)
  static Future<void> clearVault() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_storageKey);
    } catch (e) {
      print("⚠️ [VaultStorage] Error clearing storage: $e");
    }
  }
}