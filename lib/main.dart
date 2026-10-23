import 'package:aegis_health/screens/curator_chat_screen.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'services/vault_service.dart';
import 'services/brain_service.dart';
import 'widgets/atlas_painter.dart';
import 'screens/athena_welcome_sheet.dart';
import 'anamnesis_screen.dart';
import 'services/time_service.dart';
import 'widgets/debug_time_panel.dart';
import 'services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/widgets/vitals_activity_check_modal.dart';
import 'widgets/workout_session_modal.dart';
import 'widgets/emergency_medical_modal.dart';
import 'services/health_data_provider.dart';
import 'services/api/aegis_api_client.dart';
import 'services/sync/vitals_sync_queue.dart';

/// Safe testable TokenProvider abstraction for standard Aegis mobile runtime.
/// In standard mobile builds prior to Stage 5.3 user authentication, returns null.
/// Compile-time embedding of global AEGIS_API_SECRET is strictly forbidden.
Future<String?> standardRuntimeTokenProvider() async => null;

void main() => runApp(
  MultiProvider(
    providers: [
      ChangeNotifierProvider(create: (_) => VaultService()),
      ChangeNotifierProvider(create: (_) => BrainService()),
      ChangeNotifierProvider(create: (_) => TimeService()),

      // 🔗 Athena emergency and daily triggers service
      Provider(create: (_) => AthenaTriggerService()),

      // 🔗 AegisCascadeController с доступом к VaultService
      ChangeNotifierProxyProvider<VaultService, AegisCascadeController>(
        create: (context) => AegisCascadeController(
          vaultService: context.read<VaultService>(),
        ),
        update: (context, vaultService, previous) =>
        previous ?? AegisCascadeController(vaultService: vaultService),
      ),

      // 🔗 VitalsMonitorService
      // VitalsMonitorService с доступом к TimeService и VaultService
      ChangeNotifierProxyProvider2<TimeService, VaultService, VitalsMonitorService>(
        create: (context) => VitalsMonitorService(
          context.read<TimeService>(),
          context.read<VaultService>(),
        ),
        update: (context, timeService, vaultService, previous) =>
        previous!..updateTimeService(timeService),
      ),

      // 🔗 HealthDataProvider: Universal health data ingestion
      Provider<AegisApiClient>(
        create: (_) => AegisApiClient(
          tokenProvider: standardRuntimeTokenProvider,
        ),
        dispose: (_, client) => client.close(),
      ),

      // 🔗 Persistent outbound vitals telemetry queue
      ChangeNotifierProxyProvider<AegisApiClient, VitalsSyncQueue>(
        create: (context) {
          final queue = VitalsSyncQueue(
            apiClient: context.read<AegisApiClient>(),
          );
          // Asynchronously restore queue from persistent storage on startup
          // Network errors or backend unavailability MUST NOT crash or block Flutter startup!
          queue.init().then((_) {
            if (!queue.isCycleConfirmedByServer) {
              queue.resolveActiveCycle().catchError((error) {
                debugPrint('[AegisApp] Warning: initial cycle resolution check: $error');
                return false;
              });
            }
          }).catchError((error) {
            debugPrint('[AegisApp] Warning: VitalsSyncQueue startup restoration failed: $error');
          });
          queue.startPeriodicSync();
          return queue;
        },
        update: (context, apiClient, previous) {
          if (previous != null) {
            previous.updateApiClient(apiClient);
            return previous;
          }
          final queue = VitalsSyncQueue(apiClient: apiClient);
          queue.init().then((_) {
            if (!queue.isCycleConfirmedByServer) {
              queue.resolveActiveCycle().catchError((error) {
                debugPrint('[AegisApp] Warning: update cycle resolution check: $error');
                return false;
              });
            }
          }).catchError((error) {
            debugPrint('[AegisApp] Warning: VitalsSyncQueue update init failed: $error');
          });
          queue.startPeriodicSync();
          return queue;
        },
      ),

      // 🔗 HealthDataProvider: Universal health data ingestion with persistent sync queue
      ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService, VitalsSyncQueue, HealthDataProvider>(
        create: (context) => HealthDataProvider(
          vaultService: context.read<VaultService>(),
          vitalsMonitorService: context.read<VitalsMonitorService>(),
          syncQueue: context.read<VitalsSyncQueue>(),
        ),
        update: (context, vaultService, vitalsService, syncQueue, previous) =>
            (previous ??
                HealthDataProvider(
                  vaultService: vaultService,
                  vitalsMonitorService: vitalsService,
                  syncQueue: syncQueue,
                ))
              ..updateDependencies(
                vaultService: vaultService,
                vitalsMonitorService: vitalsService,
                syncQueue: syncQueue,
              ),
      ),
    ],
    child: const AegisApp(),
  ),
);

class AegisApp extends StatefulWidget {
  const AegisApp({super.key});

  @override
  State<AegisApp> createState() => _AegisAppState();
}

class _AegisAppState extends State<AegisApp> with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);
    try {
      final queue = context.read<VitalsSyncQueue>();
      switch (state) {
        case AppLifecycleState.resumed:
          queue.startPeriodicSync();
          if (queue.isInitialized && !queue.isPaused && !queue.isSyncing && queue.pendingCount > 0) {
            queue.flush().catchError((error) {
              debugPrint('[AegisApp] Lifecycle resume flush error: $error');
              return SyncResult(status: SyncStatus.retryableError, message: '$error');
            });
          }
          break;
        case AppLifecycleState.inactive:
        case AppLifecycleState.paused:
        case AppLifecycleState.detached:
        case AppLifecycleState.hidden:
          queue.stopPeriodicSync();
          queue.cancelScheduledFlush();
          break;
      }
    } catch (e) {
      debugPrint('[AegisApp] Lifecycle state transition handling skipped: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark(),
      home: const RootGateScreen(),
    );
  }
}

/// 🚦 ROUTE GATEWAY: Checks for data in the Vault before starting
class RootGateScreen extends StatelessWidget {
  const RootGateScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final vault = Provider.of<VaultService>(context);

    // 1. Берем данные главного Каскада (Консилиума)
    final cascadeData = vault.getSystemContext('core_cascade');

    // 2. Проверяем: ЗАПУЩЕН / ПРОЙДЕН ЛИ КОНСИЛИУМ?
    final String? flashOutput = cascadeData['flash_general_shelf']?.toString();
    final String? proOutput = cascadeData['pro_general_shelf']?.toString();

    final bool isConsiliumDone = (flashOutput != null &&
        flashOutput.isNotEmpty &&
        !flashOutput.contains('Waiting')) ||
        (proOutput != null &&
            proOutput.isNotEmpty &&
            !proOutput.contains('Waiting'));

    if (isConsiliumDone) {
      return const AtlasScreen();
    } else {
      return AnamnesisScreen();
    }
  }
}

class AtlasScreen extends StatefulWidget {
  const AtlasScreen({super.key});

  @override
  State<AtlasScreen> createState() => _AtlasScreenState();
}

class _AtlasScreenState extends State<AtlasScreen> {
  final AthenaTriggerService _athenaTriggerService = AthenaTriggerService();

  // 1. Храним ссылку на сервис для отписки в dispose
  VitalsMonitorService? _vitalsService;
  bool _isActivityModalOpen = false;
  bool _isWorkoutModalOpen = false;
  bool _isCriticalModalOpen = false;

  @override
  void initState() {
    super.initState();

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;

      final vaultService = context.read<VaultService>();
      final timeService = context.read<TimeService>();
      final cascadeController = AegisCascadeController(vaultService: vaultService);

      _athenaTriggerService.startMonitoring(
        vaultService: vaultService,
        timeService: timeService,
        cascadeController: cascadeController,
      );

      _checkAndShowAthenaWelcome();

      // 2. Сохраняем экземпляр и подписываемся
      _vitalsService = context.read<VitalsMonitorService>();
      _vitalsService?.addListener(_onVitalsStateChanged);
    });
  }

  void _onVitalsStateChanged() {
    if (!mounted) return;

    final state = _vitalsService?.currentState;

    if (state == AlertState.waitingUserResponse && !_isActivityModalOpen) {
      _isActivityModalOpen = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          VitalsActivityCheckModal.show(context).then((_) {
            _isActivityModalOpen = false;
          });
        }
      });
    } else if (state == AlertState.workoutActive && !_isWorkoutModalOpen) {
      _isWorkoutModalOpen = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          WorkoutSessionModal.show(context).then((_) {
            _isWorkoutModalOpen = false;
          });
        }
      });
    } else if (state == AlertState.criticalAnomaly && !_isCriticalModalOpen) {
      _isCriticalModalOpen = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          _showCriticalAnomalyDialog().then((_) {
            _isCriticalModalOpen = false;
          });
        }
      });
    }
  }

  Future<void> _showCriticalAnomalyDialog() async {
    final reason = _vitalsService?.criticalAnomalyReason ??
        "Возможная потеря сигнала: пульс 0 уд/мин зафиксирован более 2 минут.";

    await showDialog(
      context: context,
      barrierDismissible: false,
      builder: (dialogCtx) => AlertDialog(
        backgroundColor: const Color(0xFF161B22),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: const BorderSide(color: Colors.amberAccent, width: 2),
        ),
        title: const Row(
          children: [
            Icon(Icons.sensors_off_rounded, color: Colors.amberAccent, size: 28),
            SizedBox(width: 10),
            Expanded(
              child: Text(
                "ПРОВЕРКА СВЯЗИ С ДАТЧИКОМ",
                style: TextStyle(
                  color: Colors.amberAccent,
                  fontWeight: FontWeight.bold,
                  fontSize: 16,
                ),
              ),
            ),
          ],
        ),
        content: Text(
          "$reason\n\nУбедитесь, что смарт-часы плотно прилегают к запястью и датчик чист. Автоматический вызов экстренных служб отключен. При ухудшении самочувствия вы можете вызвать помощь вручную.",
          style: const TextStyle(color: Colors.white, fontSize: 14),
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.of(dialogCtx).pop();
              _vitalsService?.resetToIdle();
            },
            child: const Text("Датчик поправлен / Сброс", style: TextStyle(color: Colors.cyanAccent)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFF21262D),
              foregroundColor: Colors.white70,
            ),
            onPressed: () {
              Navigator.of(dialogCtx).pop();
              _vitalsService?.resetToIdle();
              EmergencyMedicalModal.show(context);
            },
            child: const Text("Нужна помощь"),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    // 4. Обязательно отписываемся от слушателя
    _vitalsService?.removeListener(_onVitalsStateChanged);
    _athenaTriggerService.dispose();
    super.dispose();
  }

  // 🕒 TIME CONTROL MODAL SHEET
  void _openTimeControls(BuildContext context) {
    showModalBottomSheet(
      context: context,
      backgroundColor: const Color(0xFF0D1117),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
        side: BorderSide(color: Colors.cyanAccent, width: 1),
      ),
      builder: (context) {
        return Padding(
          padding: const EdgeInsets.all(20.0),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: Colors.white24,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 15),
              const Text(
                "VIRTUAL TIME CONTROL",
                style: TextStyle(
                  color: Colors.cyanAccent,
                  fontSize: 12,
                  fontWeight: FontWeight.bold,
                  letterSpacing: 1.2,
                ),
              ),
              const SizedBox(height: 20),

              // Our existing debug panel
              const DebugTimePanel(),

              const SizedBox(height: 20),
            ],
          ),
        );
      },
    );
  }
  void _openVitalsSheet(BuildContext context) {
    showModalBottomSheet(
      context: context,
      backgroundColor: const Color(0xFF0D1117),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
        side: BorderSide(color: Colors.greenAccent, width: 1),
      ),
      builder: (context) {
        return Padding(
          padding: const EdgeInsets.all(20.0),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: Colors.white24,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 15),
              const Text(
                "SMART WATCH VITALS",
                style: TextStyle(
                  color: Colors.greenAccent,
                  fontSize: 12,
                  fontWeight: FontWeight.bold,
                  letterSpacing: 1.2,
                ),
              ),
              const SizedBox(height: 20),

              // Insert vitals header card here
              const VitalsHeaderCard(),

              const SizedBox(height: 20),
            ],
          ),
        );
      },
    );
  }

  /// Automatic sheet show on first visit to the main screen after Consilium
  void _checkAndShowAthenaWelcome() {
    final vault = Provider.of<VaultService>(context, listen: false);

    final Map<String, dynamic> globalFolder = vault.getSystemContext('global');
    final bool alreadyShown = globalFolder['athena_welcomed'] == true;

    // Check that the Chief Doctor (Pro) has actually formed the primary route
    final bool isCascadeFinished = vault.lastProGeneralOutput != null &&
        vault.lastProGeneralOutput != 'Маршрут не сформирован';

    // The sheet is shown ONLY if the cascade is completed AND the sheet hasn't been shown yet
    if (isCascadeFinished && !alreadyShown) {
      vault.updateSystemFolder('global', {
        'athena_welcomed': true,
      });

      AthenaWelcomeSheet.show(
        context,
        onConnect: () {
          if (mounted) {
            Navigator.push(
              context,
              MaterialPageRoute(builder: (context) => const CuratorChatScreen()),
            );
          }
        },
      );
    }
  }

  String _getSystemFileName(String key) {
    switch (key) {
      case 'cvs': return 'assets/images/circulationsystem.png';
      case 'gi_tract': return 'assets/images/stomack.png';
      case 'respiratory': return 'assets/images/lungs.png';
      case 'nervous': return 'assets/images/nervesystem.png';
      case 'musculoskeletal': return 'assets/images/bonesystem.png';
      case 'kidneys': return 'assets/images/kidneys.png';
      case 'immune_blood': return 'assets/images/lymphaticsystem.png';
      case 'endocrine': return 'assets/images/endocrynesystem.png';
      default: return 'assets/images/allsystems.png';
    }
  }

  void _openCuratorVault(BuildContext context) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (context) => const CuratorVaultSheet(),
    );
  }

  @override
  Widget build(BuildContext context) {
    final vault = Provider.of<VaultService>(context);
    final brain = Provider.of<BrainService>(context);
    final systemsData = vault.vaultData['health_systems'] as Map<String, dynamic>?;

    return Scaffold(
      backgroundColor: Colors.black,
      body: LayoutBuilder(
        builder: (context, constraints) {
          final allKeys = systemsData?.keys.toList() ?? [];
          final leftSystems = allKeys.take(4).toList();
          final rightSystems = allKeys.skip(4).take(4).toList();

          return Stack(
            children: [
              // 1️⃣ BACKGROUND HUMAN SILHOUETTE (INTERACTIVE CLICK ON SILHOUETTE)
              Center(
                child: GestureDetector(
                  onTap: () => _openCuratorVault(context),
                  behavior: HitTestBehavior.opaque,
                  child: SizedBox(
                    width: constraints.maxWidth * 0.8,
                    height: constraints.maxHeight * 0.8,
                    child: Stack(
                      alignment: Alignment.center,
                      children: [
                        CustomPaint(size: Size.infinite, painter: AtlasPainter(vault)),
                        AnimatedOpacity(
                          duration: const Duration(milliseconds: 800),
                          opacity: vault.selectedSystem != null ? 0.3 : 0.6,
                          child: Image.asset('assets/images/allsystems.png', fit: BoxFit.contain),
                        ),
                        if (vault.selectedSystem != null)
                          AnimatedOpacity(
                            duration: const Duration(milliseconds: 800),
                            opacity: 1.0,
                            child: Image.asset(_getSystemFileName(vault.selectedSystem!), fit: BoxFit.contain),
                          ),
                      ],
                    ),
                  ),
                ),
              ),

              // 2️⃣ TOP BAR: 2 COMPACT OVALS (SMART WATCH + TIME)
              Positioned(
                top: 50,
                left: 15,
                right: 15,
                child: SafeArea(
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      // ⌚ Smart Watch Oval (Opens vitals sheet on tap)
                      GestureDetector(
                        onTap: () {
                          // 👈 Call static method directly via class name
                          VitalsHeaderCard.showQuickEditModal(context);
                        },
                        child: Container(
                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                          decoration: BoxDecoration(
                            color: const Color(0xFF0D1117).withOpacity(0.85),
                            borderRadius: BorderRadius.circular(16),
                            border: Border.all(
                              color: Colors.cyanAccent.withOpacity(0.6),
                              width: 1,
                            ),
                            boxShadow: [
                              BoxShadow(
                                color: Colors.cyanAccent.withOpacity(0.15),
                                blurRadius: 6,
                                spreadRadius: 1,
                              ),
                            ],
                          ),
                          child: const Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Icon(
                                Icons.watch_rounded,
                                color: Colors.cyanAccent,
                                size: 14,
                              ),
                              SizedBox(width: 6),
                              Text(
                                "Smart Watch",
                                style: TextStyle(
                                  color: Colors.white,
                                  fontSize: 11,
                                  fontWeight: FontWeight.bold,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),

                      // 🕒 Time Oval (Opens time control sheet on tap)
                      Consumer<TimeService>(
                        builder: (context, timeService, child) {
                          final String formattedTime =
                              "${timeService.now.hour.toString().padLeft(2, '0')}:${timeService.now.minute.toString().padLeft(2, '0')}";

                          return GestureDetector(
                            onTap: () => _openTimeControls(context),
                            child: Container(
                              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                              decoration: BoxDecoration(
                                color: const Color(0xFF0D1117).withOpacity(0.85),
                                borderRadius: BorderRadius.circular(16),
                                border: Border.all(
                                  color: Colors.cyanAccent.withOpacity(0.6),
                                  width: 1,
                                ),
                                boxShadow: [
                                  BoxShadow(
                                    color: Colors.cyanAccent.withOpacity(0.15),
                                    blurRadius: 6,
                                    spreadRadius: 1,
                                  ),
                                ],
                              ),
                              child: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  const Icon(
                                    Icons.access_time_rounded,
                                    color: Colors.cyanAccent,
                                    size: 14,
                                  ),
                                  const SizedBox(width: 6),
                                  Text(
                                    formattedTime,
                                    style: const TextStyle(
                                      color: Colors.white,
                                      fontSize: 11,
                                      fontWeight: FontWeight.bold,
                                      fontFamily: 'Courier',
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          );
                        },
                      ),
                    ],
                  ),
                ),
              ),

              // 3️⃣ LEFT COLUMN OF ORGAN FOLDERS
              Positioned(
                  left: 20, top: 0, bottom: 0, width: 120,
                  child: _buildColumn(context, leftSystems, systemsData, vault, true)
              ),

              // 4️⃣ RIGHT COLUMN OF ORGAN FOLDERS
              Positioned(
                  right: 20, top: 0, bottom: 0, width: 120,
                  child: _buildColumn(context, rightSystems, systemsData, vault, false)
              ),

              // PROGRESS INDICATOR
              Positioned(
                top: 60, right: 20,
                child: vault.selectedSystem == null ? const SizedBox() : Column(
                  children: [
                    CircularProgressIndicator(value: brain.analysisProgress, color: Colors.orangeAccent),
                    Text("${(brain.analysisProgress * 100).toInt()}%", style: const TextStyle(fontSize: 10)),
                  ],
                ),
              ),

              // 🔥 LEFT: Native folder of the first two anamnesis models
              Positioned(
                bottom: 30, left: 20,
                child: FloatingActionButton.small(
                  heroTag: 'anamnesis_vault_fab',
                  backgroundColor: Colors.purpleAccent.withOpacity(0.1),
                  elevation: 0,
                  shape: CircleBorder(side: BorderSide(color: Colors.purpleAccent.withOpacity(0.5))),
                  onPressed: () => _showAnamnesisCoreVault(context),
                  child: const Icon(Icons.psychology_outlined, color: Colors.purpleAccent, size: 20),
                ),
              ),

              // 🤖 CENTER: TRANSITION TO CURATOR CHAT WITH NOTIFICATION INDICATOR
              Positioned(
                bottom: 25,
                left: 0,
                right: 0,
                child: Center(
                  child: Consumer<VaultService>(
                    builder: (context, vault, child) {
                      final bool hasUnread = vault.hasUnreadAthenaMessage;

                      return GestureDetector(
                        onTap: () {
                          // 1. Clear the unread message flag on click
                          if (hasUnread) {
                            vault.setUnreadAthenaMessage(false);
                          }

                          // 2. Navigate to the chat screen
                          Navigator.push(
                            context,
                            MaterialPageRoute(
                              builder: (context) => const CuratorChatScreen(),
                            ),
                          );
                        },
                        child: Stack(
                          clipBehavior: Clip.none,
                          children: [
                            Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 16,
                                vertical: 8,
                              ),
                              decoration: BoxDecoration(
                                color: const Color(0xFF0D1117).withOpacity(0.9),
                                borderRadius: BorderRadius.circular(20),
                                border: Border.all(
                                  color: hasUnread
                                      ? Colors.redAccent
                                      : Colors.blueAccent,
                                  width: 1.2,
                                ),
                                boxShadow: [
                                  BoxShadow(
                                    color: (hasUnread
                                        ? Colors.redAccent
                                        : Colors.blueAccent)
                                        .withOpacity(0.3),
                                    blurRadius: 10,
                                    spreadRadius: 1,
                                  )
                                ],
                              ),
                              child: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Icon(
                                    Icons.support_agent_rounded,
                                    color: hasUnread
                                        ? Colors.redAccent
                                        : Colors.blueAccent,
                                    size: 18,
                                  ),
                                  const SizedBox(width: 8),
                                  const Text(
                                    "CURATOR CHAT",
                                    style: TextStyle(
                                      color: Colors.white,
                                      fontSize: 11,
                                      fontWeight: FontWeight.bold,
                                      letterSpacing: 1.2,
                                    ),
                                  ),
                                ],
                              ),
                            ),

                            // 🔴 RED BADGE DOT ON THE TOP RIGHT OF THE BUTTON
                            if (hasUnread)
                              Positioned(
                                top: -3,
                                right: -3,
                                child: Container(
                                  width: 12,
                                  height: 12,
                                  decoration: BoxDecoration(
                                    color: Colors.redAccent,
                                    shape: BoxShape.circle,
                                    border: Border.all(
                                      color: const Color(0xFF0D1117),
                                      width: 2,
                                    ),
                                    boxShadow: const [
                                      BoxShadow(
                                        color: Colors.redAccent,
                                        blurRadius: 6,
                                        spreadRadius: 1,
                                      ),
                                    ],
                                  ),
                                ),
                              ),
                          ],
                        ),
                      );
                    },
                  ),
                ),
              ),

              // 🔥 RIGHT: Shared folder of main cascade models
              Positioned(
                bottom: 30, right: 20,
                child: FloatingActionButton.small(
                  heroTag: 'cascade_vault_fab',
                  backgroundColor: Colors.blueAccent.withOpacity(0.1),
                  elevation: 0,
                  shape: CircleBorder(side: BorderSide(color: Colors.blueAccent.withOpacity(0.5))),
                  onPressed: () => _showCascadeCoreVault(context),
                  child: const Icon(Icons.analytics_outlined, color: Colors.blueAccent, size: 20),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _buildColumn(BuildContext context, List<dynamic> keys, dynamic data, VaultService vault, bool isLeft) {
    return Column(
      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
      crossAxisAlignment: isLeft ? CrossAxisAlignment.start : CrossAxisAlignment.end,
      children: keys.map((key) {
        var system = data[key];
        Color color = Color(int.parse(system['color']));
        bool isSelected = vault.selectedSystem == key;
        String folderKey = key == 'immune_blood' ? 'immune' : key;

        return GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTap: () => vault.selectSystem(key),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 8.0),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (!isLeft)
                  IconButton(
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                    icon: Icon(Icons.folder_open, color: color.withOpacity(isSelected ? 1.0 : 0.3), size: 16),
                    onPressed: () => _showSystemLog(context, folderKey),
                  ),
                if (!isLeft) const SizedBox(width: 6),
                Flexible(
                  child: Text(
                    system['name'],
                    textAlign: isLeft ? TextAlign.left : TextAlign.right,
                    style: TextStyle(
                      color: color,
                      fontSize: 11,
                      fontWeight: isSelected ? FontWeight.bold : FontWeight.normal,
                    ),
                    overflow: TextOverflow.visible,
                  ),
                ),
                if (isLeft) const SizedBox(width: 6),
                if (isLeft)
                  IconButton(
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                    icon: Icon(Icons.folder_open, color: color.withOpacity(isSelected ? 1.0 : 0.3), size: 16),
                    onPressed: () => _showSystemLog(context, folderKey),
                  ),
              ],
            ),
          ),
        );
      }).toList(),
    );
  }

  void _showAnamnesisCoreVault(BuildContext context) {
    final vault = Provider.of<VaultService>(context, listen: false);
    final Map<String, dynamic> globalData = vault.getSystemContext('global');
    final Map<String, dynamic> sharedLabs = vault.getSharedLabs();

    final Map<String, dynamic> sandbox = (globalData['training_sandbox'] ?? vault.getSystemContext('training_sandbox')) is Map
        ? Map<String, dynamic>.from(globalData['training_sandbox'] ?? vault.getSystemContext('training_sandbox'))
        : {};

    final Map<String, dynamic> cleanTextAnamnesis = Map.from(globalData)
      ..remove('labs')
      ..remove('shared_labs_shelf')
      ..remove('training_sandbox');

    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF0D1117),
        shape: RoundedRectangleBorder(
            side: const BorderSide(color: Colors.purpleAccent, width: 1),
            borderRadius: BorderRadius.circular(12)
        ),
        title: const Text("CORE_SYSTEM :: DATA_LOG",
            style: TextStyle(color: Colors.purpleAccent, fontSize: 16, fontWeight: FontWeight.bold, letterSpacing: 1.5)),
        content: SizedBox(
          width: double.maxFinite,
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text("📂 [STRATUM_1]: USER_VIEW (STRUCTURED)", style: TextStyle(color: Colors.orangeAccent, fontSize: 12, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                if (cleanTextAnamnesis.isEmpty)
                  const Text("> WAITING FOR PREPROCESSING...", style: TextStyle(color: Colors.white38, fontSize: 11, fontFamily: 'Courier'))
                else
                  ...cleanTextAnamnesis.entries.map((entry) => Padding(
                    padding: const EdgeInsets.symmetric(vertical: 2.0),
                    child: Text("> ${entry.key.toUpperCase()}: ${entry.value}", style: const TextStyle(color: Colors.white, fontSize: 11, fontFamily: 'Courier')),
                  )),

                const SizedBox(height: 20),

                const Text("📂 [STRATUM_2]: LAB_MARKERS", style: TextStyle(color: Colors.greenAccent, fontSize: 12, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                if (sharedLabs.isEmpty)
                  const Text("> SHELF IS EMPTY", style: TextStyle(color: Colors.white38, fontSize: 11, fontFamily: 'Courier'))
                else
                  ...sharedLabs.entries.map((entry) => Padding(
                    padding: const EdgeInsets.symmetric(vertical: 2.0),
                    child: Text("> ${entry.key.toUpperCase()}: ${entry.value}", style: const TextStyle(color: Colors.white70, fontSize: 11, fontFamily: 'Courier')),
                  )),

                const SizedBox(height: 20),

                const Text("📂 [STRATUM_3]: TRAINING_SANDBOX (SECRET)", style: TextStyle(color: Colors.redAccent, fontSize: 12, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                Text("> HYPOTHESIS: ${sandbox['raw_diagnostic_hypothesis'] ?? sandbox['risk_analysis'] ?? 'WAITING...'}",
                    style: const TextStyle(color: Colors.white70, fontSize: 11, fontFamily: 'Courier')),
                const SizedBox(height: 8),
                Text("> DATA GAP: ${sandbox['data_gap_analysis'] ?? sandbox['missing_data'] ?? 'WAITING...'}",
                    style: const TextStyle(color: Colors.white70, fontSize: 11, fontFamily: 'Courier')),
                const SizedBox(height: 8),
                Text("> ERROR PROBABILITY: ${sandbox['error_probability'] ?? '0%'}",
                    style: const TextStyle(color: Colors.white70, fontSize: 11, fontFamily: 'Courier')),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text("TERMINATE", style: TextStyle(color: Colors.purpleAccent)))
        ],
      ),
    );
  }

  void _showCascadeCoreVault(BuildContext context) {
    final vault = Provider.of<VaultService>(context, listen: false);
    final cascadeData = vault.getSystemContext('core_cascade');

    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF0D1117),
        shape: RoundedRectangleBorder(
            side: const BorderSide(color: Colors.blueAccent, width: 1),
            borderRadius: BorderRadius.circular(12)
        ),
        title: const Text("CLINICAL CASCADE :: MAIN MODELS",
            style: TextStyle(color: Colors.blueAccent, fontSize: 14, fontWeight: FontWeight.bold, letterSpacing: 1.2)),
        content: SizedBox(
          width: double.maxFinite,
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text("⚡ GEMINI FLASH (DEPUTY CHIEF DOCTOR) — GENERAL OUTPUT:", style: TextStyle(color: Colors.orangeAccent, fontSize: 11, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                Text(cascadeData['flash_general_shelf'] ?? "Waiting for launch...", style: const TextStyle(color: Colors.white, fontSize: 12)),

                const SizedBox(height: 15),
                const Text("🛠 GEMINI FLASH — TRAINING SHELF (DEFICITS):", style: TextStyle(color: Colors.orangeAccent, fontSize: 10, fontFamily: 'Courier', fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                Text(cascadeData['flash_training_shelf'] ?? "Logs are empty", style: const TextStyle(color: Colors.white70, fontSize: 11, fontFamily: 'Courier')),

                const SizedBox(height: 25),

                const Text("🧠 GEMINI PRO (CHIEF DOCTOR) — GENERAL OUTPUT:", style: TextStyle(color: Colors.purpleAccent, fontSize: 11, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                Text(cascadeData['pro_general_shelf'] ?? "Waiting for cascade...", style: const TextStyle(color: Colors.white, fontSize: 12)),

                const SizedBox(height: 15),
                const Text("🛠 GEMINI PRO — TRAINING SHELF (SPECIFIC DIAGNOSES):", style: TextStyle(color: Colors.purpleAccent, fontSize: 10, fontFamily: 'Courier', fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white24),
                Text(cascadeData['pro_training_shelf'] ?? "Logs are empty", style: const TextStyle(color: Colors.white70, fontSize: 11, fontFamily: 'Courier')),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text("CLOSE", style: TextStyle(color: Colors.blueAccent)))
        ],
      ),
    );
  }

  void _showSystemLog(BuildContext context, String systemKey) {
    final vault = Provider.of<VaultService>(context, listen: false);
    final folder = vault.getSystemContext(systemKey);

    final dynamic m1Facts = folder['data'] ?? folder['facts'] ?? folder['system_facts'] ?? "Sterile facts are missing";
    final dynamic m1Summary = folder['internal_comment'] ?? folder['first_model_short_summary'] ?? folder['summary'] ?? "Internal comment M1 is missing";

    final Map<String, dynamic> labsShelf = folder.containsKey('labs') && folder['labs'] is Map
        ? Map<String, dynamic>.from(folder['labs'])
        : <String, dynamic>{};

    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF0D1117),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        title: Text("SYSTEM_LOG :: ${systemKey.toUpperCase()}", style: const TextStyle(color: Colors.blueAccent, fontSize: 14, fontWeight: FontWeight.bold)),
        content: SizedBox(
          width: double.maxFinite,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text("BIOCHEMICAL MARKERS (OCR LABS):", style: TextStyle(color: Colors.greenAccent, fontSize: 10, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white10),

                if (labsShelf.isNotEmpty)
                  ...labsShelf.entries.expand((entry) {
                    final key = entry.key;
                    final val = entry.value;

                    if (val is Map && !val.containsKey('value')) {
                      return [
                        Padding(
                          padding: const EdgeInsets.only(top: 8.0, bottom: 4.0),
                          child: Text(
                            "[ ${key.toUpperCase()} ]",
                            style: const TextStyle(fontFamily: 'Courier', color: Colors.cyanAccent, fontSize: 11, fontWeight: FontWeight.bold),
                          ),
                        ),
                        ...val.entries.map((subEntry) {
                          final subVal = subEntry.value;
                          String subDisplay = subVal is Map ? "${subVal['value'] ?? 'N/A'} ${subVal['unit'] ?? ''}" : subVal.toString();

                          return Padding(
                            padding: const EdgeInsets.symmetric(vertical: 2.0, horizontal: 8.0),
                            child: Row(
                              children: [
                                Expanded(
                                  child: Text(
                                    "> ${subEntry.key.toUpperCase()}:",
                                    style: const TextStyle(fontFamily: 'Courier', color: Colors.white70, fontSize: 11),
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                                const SizedBox(width: 8),
                                Text(
                                  subDisplay,
                                  style: const TextStyle(fontFamily: 'Courier', color: Colors.greenAccent, fontSize: 11, fontWeight: FontWeight.bold),
                                ),
                              ],
                            ),
                          );
                        })
                      ];
                    }

                    String display = val is Map ? "${val['value'] ?? 'N/A'} ${val['unit'] ?? ''}" : val.toString();
                    return [
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 4.0),
                        child: Row(
                          children: [
                            Expanded(
                              child: Text(
                                "> ${key.toUpperCase()}:",
                                style: const TextStyle(fontFamily: 'Courier', color: Colors.white70, fontSize: 11),
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                            const SizedBox(width: 8),
                            Text(
                              display,
                              style: const TextStyle(fontFamily: 'Courier', color: Colors.greenAccent, fontSize: 11, fontWeight: FontWeight.bold),
                            ),
                          ],
                        ),
                      )
                    ];
                  })
                else
                  const Text("> Laboratory markers are missing", style: TextStyle(fontSize: 11, fontFamily: 'Courier', color: Colors.white38)),

                const SizedBox(height: 20),

                const Text("SYSTEM FACTS (RAW DATA FROM M1):", style: TextStyle(color: Colors.orangeAccent, fontSize: 10, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white10),
                Text(m1Facts.toString(), style: const TextStyle(fontSize: 11, fontFamily: 'Courier', color: Colors.white70)),

                const SizedBox(height: 20),

                const Text("ANAMNESIS MODEL BRIEF SYSTEM SUMMARY:", style: TextStyle(color: Colors.purpleAccent, fontSize: 10, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white10),
                Text(m1Summary.toString(), style: const TextStyle(fontSize: 11, color: Colors.white)),

                const SizedBox(height: 20),

                const Text("SYSTEM MODEL OUTPUT SHELF (CASCADE STAGE):", style: TextStyle(color: Colors.cyanAccent, fontSize: 10, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white10),
                Text(folder['system_general_shelf'] ?? "Analysis was not performed", style: const TextStyle(fontSize: 12, color: Colors.white)),

                const SizedBox(height: 20),

                const Text("TRAINING OUTPUT SHELF (LOGS FOR FINE-TUNING):", style: TextStyle(color: Colors.redAccent, fontSize: 10, fontWeight: FontWeight.bold)),
                const Divider(color: Colors.white10),
                Text(folder['system_training_shelf'] ?? "Logs are missing", style: const TextStyle(fontSize: 11, fontFamily: 'Courier', color: Colors.white70)),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text("CLOSE", style: TextStyle(color: Colors.blueAccent)))
        ],
      ),
    );
  }
}

// 💬 CURATOR VAULT AND CHAT MODAL SCREEN
class CuratorVaultSheet extends StatefulWidget {
  const CuratorVaultSheet({super.key});

  @override
  State<CuratorVaultSheet> createState() => _CuratorVaultSheetState();
}

class _CuratorVaultSheetState extends State<CuratorVaultSheet> {
  /// 🗑️ DIALOGS AND VAULT RESET LOGIC
  Future<void> _showResetVaultDialog(BuildContext context) async {
    // 1. Confirmation window (Yes / No)
    final bool? confirm = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: const Color(0xFF0D1117),
          shape: RoundedRectangleBorder(
            side: const BorderSide(color: Colors.redAccent, width: 1),
            borderRadius: BorderRadius.circular(12),
          ),
          title: const Text(
            'Data Deletion',
            style: TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold),
          ),
          content: const Text(
            'Are you sure you want to completely reset all application data?',
            style: TextStyle(color: Colors.white),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: const Text('No', style: TextStyle(color: Colors.white70)),
            ),
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(true),
              style: TextButton.styleFrom(foregroundColor: Colors.redAccent),
              child: const Text('Yes, delete', style: TextStyle(fontWeight: FontWeight.bold)),
            ),
          ],
        );
      },
    );

    if (confirm != true || !context.mounted) return;

    // 2. First show "Data deleted" window
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: const Color(0xFF0D1117),
          shape: RoundedRectangleBorder(
            side: const BorderSide(color: Colors.blueAccent, width: 1),
            borderRadius: BorderRadius.circular(12),
          ),
          title: const Text(
            'Data Deleted',
            style: TextStyle(color: Colors.blueAccent, fontWeight: FontWeight.bold),
          ),
          content: const Text(
            'All data has been successfully reset. The application is returning to the beginning.',
            style: TextStyle(color: Colors.white),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(),
              child: const Text('Got it', style: TextStyle(color: Colors.blueAccent)),
            ),
          ],
        );
      },
    );

    if (!context.mounted) return;

    // 3. Take services BEFORE clearing
    final vault = Provider.of<VaultService>(context, listen: false);
    final vitals = Provider.of<VitalsMonitorService>(context, listen: false);

    // 4. PERFORM RESET AND IMMEDIATELY CHANGE SCREEN
    await vault.resetVault();
    vitals.reset();

    if (!context.mounted) return;

    // 5. Clear the entire stack and forcefully set RootGateScreen again
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (context) => const RootGateScreen()),
          (Route<dynamic> route) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    final vault = Provider.of<VaultService>(context);

    // 1. Extract aggregated exported data and cascade
    final notesExportData = vault.getSystemContext('notes_export');
    final cascadeData = vault.getSystemContext('core_cascade');

    // 2. Strict filter: throw away null, empty strings, empty Maps and Lists
    final Map<String, dynamic> duplicatedFacts = Map.fromEntries(
      notesExportData.entries.where((entry) {
        final val = entry.value;
        if (val == null) return false;
        if (val is String && val.trim().isEmpty) return false;
        if (val is Map && val.isEmpty) return false;
        if (val is List && val.isEmpty) return false;
        return true;
      }),
    );

    // Honest flag for real data presence
    final bool hasRealFacts = duplicatedFacts.isNotEmpty;

    final String ecgVision = vault.ecgSummary ?? "ECG data is missing";
    final String weeklyPlan = cascadeData['pro_general_shelf'] ?? "Question plan is being formed by Pro-model...";

    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom + 16,
        left: 20,
        right: 20,
        top: 20,
      ),
      child: SizedBox(
        height: MediaQuery.of(context).size.height * 0.65,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // VAULT HEADER (WITH RESET BUTTON ON THE RIGHT)
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Row(
                  children: [
                    Icon(Icons.shield_outlined, color: Colors.blueAccent, size: 22),
                    SizedBox(width: 8),
                    Text("CURATOR VAULT :: AEGIS", style: TextStyle(color: Colors.blueAccent, fontSize: 15, fontWeight: FontWeight.bold, letterSpacing: 1.2)),
                  ],
                ),
                // 🗑️ DELETE / RESET BUTTON
                IconButton(
                  icon: const Icon(Icons.delete_forever_rounded, color: Colors.redAccent, size: 22),
                  tooltip: "Reset all data",
                  onPressed: () => _showResetVaultDialog(context),
                ),
              ],
            ),
            const Divider(color: Colors.white24, height: 20),

            // DUPLICATED FACTS AND PLAN BLOCK
            Expanded(
              child: SingleChildScrollView(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text("📋 M1 DUPLICATED FACTS AND VISION (WITHOUT HYPOTHESES):", style: TextStyle(color: Colors.orangeAccent, fontSize: 11, fontWeight: FontWeight.bold)),
                    const SizedBox(height: 6),
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: const Color(0xFF161B22),
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(
                          color: hasRealFacts ? Colors.white10 : Colors.redAccent.withOpacity(0.3),
                        ),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            hasRealFacts
                                ? "• M1 FACTS: ${duplicatedFacts.toString()}"
                                : "• M1 FACTS: Data is missing. Please complete the initial anamnesis.",
                            style: TextStyle(
                              color: hasRealFacts ? Colors.white70 : Colors.redAccent,
                              fontSize: 11,
                              fontFamily: 'Courier',
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            "• VISION CONCLUSION (ECG): $ecgVision",
                            style: const TextStyle(color: Colors.greenAccent, fontSize: 11, fontFamily: 'Courier'),
                          ),
                        ],
                      ),
                    ),

                    const SizedBox(height: 16),
                    const Text("🎯 WEEKLY OBSERVATION PLAN (FROM PRO):", style: TextStyle(color: Colors.purpleAccent, fontSize: 11, fontWeight: FontWeight.bold)),
                    const SizedBox(height: 6),
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: const Color(0xFF161B22),
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(color: Colors.purpleAccent.withOpacity(0.3)),
                      ),
                      child: Text(weeklyPlan, style: const TextStyle(color: Colors.white, fontSize: 12)),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ⌚ VITALS CONTROLLER WIDGET (ABOVE SILHOUETTE HEAD)
class VitalsHeaderCard extends StatelessWidget {
  const VitalsHeaderCard({super.key});

  /// Public method for direct modal call (for 1-tap interaction)
  static void showQuickEditModal(BuildContext context) {
    final vault = Provider.of<VaultService>(context, listen: false);
    final sysController = TextEditingController(text: vault.systolic?.toString() ?? '');
    final diaController = TextEditingController(text: vault.diastolic?.toString() ?? '');
    final pulseController = TextEditingController(text: vault.pulse?.toString() ?? '');
    final spo2Controller = TextEditingController(text: vault.spo2?.toString() ?? '');

    showModalBottomSheet(
      context: context,
      backgroundColor: const Color(0xFF0D1117),
      isScrollControlled: true, // 👈 Allow modal to take up screen height
      shape: const RoundedRectangleBorder(
        side: BorderSide(color: Colors.cyanAccent, width: 1),
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (context) {
        return Container(
          // Limit maximum height to 85% of screen
          constraints: BoxConstraints(
            maxHeight: MediaQuery.of(context).size.height * 0.85,
          ),
          padding: EdgeInsets.only(
            bottom: MediaQuery.of(context).viewInsets.bottom + 20,
            left: 20,
            right: 20,
            top: 20,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Top indicator bar
              Center(
                child: Container(
                  width: 40,
                  height: 4,
                  decoration: BoxDecoration(
                    color: Colors.white24,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              const SizedBox(height: 15),
              const Center(
                child: Text(
                  "VITALS FORM (SMART WATCH) ⌚",
                  style: TextStyle(
                    color: Colors.cyanAccent,
                    fontWeight: FontWeight.bold,
                    fontSize: 13,
                    letterSpacing: 1.1,
                  ),
                ),
              ),
              const SizedBox(height: 15),

              // 📜 ALL CONTENT WRAPPED IN EXPANDED + SINGLECHILDSCROLLVIEW
              Expanded(
                child: SingleChildScrollView(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      // Vitals input fields
                      Row(
                        children: [
                          Expanded(
                            child: TextField(
                              controller: sysController,
                              keyboardType: TextInputType.number,
                              style: const TextStyle(color: Colors.white),
                              decoration: const InputDecoration(
                                labelText: "Sys",
                                labelStyle: TextStyle(color: Colors.white54),
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: TextField(
                              controller: diaController,
                              keyboardType: TextInputType.number,
                              style: const TextStyle(color: Colors.white),
                              decoration: const InputDecoration(
                                labelText: "Dia",
                                labelStyle: TextStyle(color: Colors.white54),
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: TextField(
                              controller: pulseController,
                              keyboardType: TextInputType.number,
                              style: const TextStyle(color: Colors.white),
                              decoration: const InputDecoration(
                                labelText: "Pulse",
                                labelStyle: TextStyle(color: Colors.white54),
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: TextField(
                              controller: spo2Controller,
                              keyboardType: TextInputType.number,
                              style: const TextStyle(color: Colors.white),
                              decoration: const InputDecoration(
                                labelText: "SpO2",
                                labelStyle: TextStyle(color: Colors.white54),
                              ),
                            ),
                          ),
                        ],
                      ),

                      // Vision ECG digitization block
                      if (vault.ecgSummary != null && vault.ecgSummary!.isNotEmpty) ...[
                        const SizedBox(height: 15),
                        Container(
                          width: double.infinity,
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                            color: const Color(0x66000000),
                            borderRadius: BorderRadius.circular(8),
                            border: Border.all(color: Colors.greenAccent.withOpacity(0.3)),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                                children: [
                                  const Text(
                                    "VISION DIGITIZATION (ECG):",
                                    style: TextStyle(
                                      color: Colors.greenAccent,
                                      fontSize: 10,
                                      fontWeight: FontWeight.bold,
                                    ),
                                  ),
                                  InkWell(
                                    onTap: () {
                                      vault.updateSmartWatchMetrics(ecgSummary: "");
                                      Navigator.pop(context);
                                    },
                                    child: const Icon(Icons.delete_outline, color: Colors.redAccent, size: 14),
                                  )
                                ],
                              ),
                              const SizedBox(height: 6),
                              Text(
                                vault.ecgSummary!,
                                style: const TextStyle(
                                  color: Colors.white70,
                                  fontSize: 11,
                                  fontFamily: 'Courier',
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),

              const SizedBox(height: 15),

              // Save button at the bottom
              SizedBox(
                width: double.maxFinite,
                child: ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.cyanAccent,
                    padding: const EdgeInsets.symmetric(vertical: 12),
                  ),
                  onPressed: () {
                    final int? pulse = int.tryParse(pulseController.text);
                    final int? sys = int.tryParse(sysController.text);
                    final int? dia = int.tryParse(diaController.text);
                    final int? spo2 = int.tryParse(spo2Controller.text);

                    // 🚀 Dispatch manual vitals entry via HealthDataProvider
                    context.read<HealthDataProvider>().dispatchManualInput(
                      pulse: pulse,
                      systolic: sys,
                      diastolic: dia,
                      spo2: spo2,
                    );

                    Navigator.pop(context);
                  },
                  child: const Text(
                    "SAVE",
                    style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold),
                  ),
                ),
              )
            ],
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<VaultService>(
      builder: (context, vault, child) {
        final hasEcg = vault.ecgSummary != null && vault.ecgSummary!.isNotEmpty;

        return GestureDetector(
          onTap: () => showQuickEditModal(context),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22).withOpacity(0.9),
              borderRadius: BorderRadius.circular(20),
              border: Border.all(color: Colors.cyanAccent.withOpacity(0.4), width: 1),
              boxShadow: [
                BoxShadow(
                  color: Colors.cyanAccent.withOpacity(0.12),
                  blurRadius: 10,
                  spreadRadius: 1,
                )
              ],
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.watch_rounded, color: Colors.cyanAccent, size: 16),
                const SizedBox(width: 6),
                const Text(
                  "Smart Watch",
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (hasEcg) ...[
                  const SizedBox(width: 6),
                  Container(
                    width: 6,
                    height: 6,
                    decoration: const BoxDecoration(
                      color: Colors.greenAccent,
                      shape: BoxShape.circle,
                    ),
                  ),
                ],
              ],
            ),
          ),
        );
      },
    );
  }
}

// ⏰ VITALS TIME / DEBUG WIDGET (RIGHT OVAL IN TOP BAR)
class TimeHeaderCard extends StatelessWidget {
  final VoidCallback? onTap;

  const TimeHeaderCard({super.key, this.onTap});

  @override
  Widget build(BuildContext context) {
    final now = DateTime.now();
    final timeString = "${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}";

    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: const Color(0xFF161B22).withOpacity(0.9),
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: Colors.purpleAccent.withOpacity(0.4), width: 1),
          boxShadow: [
            BoxShadow(
              color: Colors.purpleAccent.withOpacity(0.12),
              blurRadius: 10,
              spreadRadius: 1,
            )
          ],
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.access_time_rounded, color: Colors.purpleAccent, size: 16),
            const SizedBox(width: 6),
            Text(
              timeString,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 12,
                fontWeight: FontWeight.w600,
                fontFamily: 'Courier',
              ),
            ),
          ],
        ),
      ),
    );
  }
}