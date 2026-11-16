import 'package:flutter/material.dart';

import 'prototypes/aegis_splash_hologram_prototype.dart';

/// Isolated entry point for manual Android visual evaluation of the
/// AEGIS Splash Video -> ECG Hologram Transition -> Demo Consent Screen prototype.
///
/// Run locally with:
/// `flutter run -t lib/main_splash_prototype.dart`
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const AegisSplashPrototypeApp());
}
