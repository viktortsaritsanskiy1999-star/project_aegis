import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:google_sign_in/google_sign_in.dart';

/// Abstract contract for Google Sign-In interaction in Project Aegis.
/// Allows clean dependency injection and mocking for unit/widget tests.
abstract class IGoogleAuthService {
  /// Initiates the Google Sign-In flow and returns the OpenID Connect ID Token.
  /// Returns `null` if the user cancelled the selection dialog.
  Future<String?> signInAndGetIdToken();

  /// Signs the user out of Google Sign-In.
  Future<void> signOut();
}

/// Production implementation of [IGoogleAuthService] wrapping [GoogleSignIn].
class GoogleAuthService implements IGoogleAuthService {
  static IGoogleAuthService? _customInstance;

  /// Global or mock instance used across the app.
  static IGoogleAuthService get instance => _customInstance ?? _defaultInstance;

  /// Sets a custom or mock instance (e.g. during testing).
  static set instance(IGoogleAuthService? custom) {
    _customInstance = custom;
  }

  static const String environmentServerClientId =
      String.fromEnvironment('AEGIS_GOOGLE_SERVER_CLIENT_ID');

  static final GoogleAuthService _defaultInstance = GoogleAuthService._();

  final GoogleSignIn _googleSignIn;
  bool _isInitialized = false;
  final String? _serverClientId;

  GoogleAuthService._({GoogleSignIn? googleSignIn, String? serverClientId})
      : _googleSignIn = googleSignIn ?? GoogleSignIn.instance,
        _serverClientId = (serverClientId != null && serverClientId.trim().isNotEmpty)
            ? serverClientId.trim()
            : (environmentServerClientId.trim().isNotEmpty
                ? environmentServerClientId.trim()
                : null);

  /// Factory constructor allowing injection of custom [GoogleSignIn] client or [serverClientId].
  factory GoogleAuthService({GoogleSignIn? googleSignIn, String? serverClientId}) {
    return GoogleAuthService._(
      googleSignIn: googleSignIn,
      serverClientId: serverClientId,
    );
  }

  /// Ensures [GoogleSignIn.instance.initialize] has been called.
  Future<void> ensureInitialized() async {
    if (_isInitialized) return;
    try {
      await _googleSignIn.initialize(
        serverClientId: _serverClientId,
      );
      _isInitialized = true;
    } catch (e) {
      debugPrint('[GoogleAuthService] Initialize notice: $e');
      _isInitialized = true; // Avoid repeated failing calls
    }
  }

  @override
  Future<String?> signInAndGetIdToken() async {
    try {
      debugPrint('[GoogleAuthService] Initiating Google Sign-In...');
      await ensureInitialized();

      final account = await _googleSignIn.authenticate(
        scopeHint: const <String>['email', 'openid'],
      );

      final idToken = account.authentication.idToken;

      if (idToken == null || idToken.trim().isEmpty) {
        debugPrint('[GoogleAuthService] Warning: GoogleSignIn did not return an idToken.');
        return null;
      }

      debugPrint('[GoogleAuthService] Successfully acquired Google ID Token.');
      return idToken.trim();
    } on GoogleSignInException catch (e) {
      if (e.code == GoogleSignInExceptionCode.canceled ||
          e.code == GoogleSignInExceptionCode.interrupted) {
        debugPrint('[GoogleAuthService] User cancelled Google Sign-In (${e.code}).');
        return null;
      }
      debugPrint('[GoogleAuthService] Google Sign-In exception: ${e.code} - ${e.description}');
      rethrow;
    } catch (e) {
      debugPrint('[GoogleAuthService] Google Sign-In failed with error: $e');
      rethrow;
    }
  }

  @override
  Future<void> signOut() async {
    try {
      await _googleSignIn.signOut();
      debugPrint('[GoogleAuthService] Signed out of Google.');
    } catch (e) {
      debugPrint('[GoogleAuthService] Google signOut error: $e');
    }
  }
}
