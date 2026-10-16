import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/brain_service.dart';
import '../services/vault_service.dart';
import '../services/time_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import '../services/vitals_monitor_service.dart';
import '../widgets/emergency_medical_modal.dart';

class CuratorChatScreen extends StatefulWidget {
  const CuratorChatScreen({super.key});

  @override
  State<CuratorChatScreen> createState() => _CuratorChatScreenState();
}

class _CuratorChatScreenState extends State<CuratorChatScreen> {
  final TextEditingController _textController = TextEditingController();
  final ScrollController _scrollController = ScrollController();

  List<Map<String, String>> _messages = [];
  bool _isInitializing = true;
  bool _isSending = false;

  VaultService? _vaultService;
  AegisCascadeController? _cascadeController;
  VitalsMonitorService? _vitalsService;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _vaultService = Provider.of<VaultService>(context, listen: false);
      _cascadeController = Provider.of<AegisCascadeController>(context, listen: false);
      _vitalsService = Provider.of<VitalsMonitorService>(context, listen: false);

      _vaultService?.addListener(_onVaultUpdated);
      _startCuratorFlow();
    });
  }

  void _onVaultUpdated() {
    if (_vaultService == null || !mounted) return;

    final updatedHistory = _vaultService!.getCuratorChatHistory();
    if (updatedHistory.length != _messages.length) {
      setState(() {
        _messages = List.from(updatedHistory);
      });
      _scrollToBottom();
    }
  }

  Future<void> _startCuratorFlow() async {
    if (_vaultService == null || _cascadeController == null) return;

    final vaultService = _vaultService!;
    final cascadeController = _cascadeController!;
    final brainService = Provider.of<BrainService>(context, listen: false);
    final timeService = Provider.of<TimeService>(context, listen: false);

    final savedHistory = vaultService.getCuratorChatHistory();

    // 1. If history already exists (including the newly recorded pulse spike)
    if (savedHistory.isNotEmpty) {
      setState(() {
        _messages = List.from(savedHistory);
        _isInitializing = false;
      });

      // Clear unread messages badge
      vaultService.setUnreadAthenaMessage(false);

      _scrollToBottom();
      _checkAndHandleEmergencyTag();
      return;
    }

    // 2. If history is empty — initial generation of the greeting
    String? firstResponse;

    try {
      if (vaultService.lastProGeneralOutput == null || vaultService.lastProGeneralOutput!.isEmpty) {
        await brainService.runFullPipeline(vaultService);
      }

      // Generate Athena's actual first clinical greeting
      firstResponse = await cascadeController.generateInitialGreeting(
        proGeneralOutput: vaultService.lastProGeneralOutput ?? '',
        proTrainingOutput: vaultService.lastProTrainingOutput ?? '',
      );
    } catch (e) {
      debugPrint("❌ Error generating Athena's starting message: $e");
    }

    if (mounted) {
      String initialText = firstResponse ??
          'Hello! I am Athena — your personal wellness guide. How are you feeling?';

      bool triggerEmergency = false;
      if (initialText.contains('[EMERGENCY_TRIGGER]')) {
        triggerEmergency = true;
        initialText = initialText.replaceAll('[EMERGENCY_TRIGGER]', '').trim();
      }

      final initialMsg = {'sender': 'curator', 'text': initialText};

      // Save the first message to history
      vaultService.saveCuratorChatMessage(initialMsg);

      // 🎯 SET TIMER TO +4 HOURS FROM CURRENT TIME
      vaultService.updateAthenaLastPromptTime(timeService.now.millisecondsSinceEpoch);

      setState(() {
        _messages = List.from(vaultService.getCuratorChatHistory());
        _isInitializing = false;
      });

      _scrollToBottom();

      if (triggerEmergency) {
        _showEmergencyModal();
      }
    }
  }

  Future<void> _sendMessage() async {
    final text = _textController.text.trim();
    if (text.isEmpty || _isSending || _vaultService == null || _cascadeController == null) return;

    final vaultService = _vaultService!;
    final cascadeController = _cascadeController!;
    final timeService = Provider.of<TimeService>(context, listen: false);

    _textController.clear();
    final userMsg = {'sender': 'user', 'text': text};

    vaultService.saveCuratorChatMessage(userMsg);

    // Update activity timestamp so the routine timer counts down from the last interaction
    vaultService.updateAthenaLastPromptTime(timeService.now.millisecondsSinceEpoch);

    setState(() {
      _messages = List.from(vaultService.getCuratorChatHistory());
      _isSending = true;
    });

    _scrollToBottom();

    final rawReply = await cascadeController.handleUserReply(text);

    if (mounted) {
      String finalReplyText = rawReply ?? 'Failed to get response from Athena. Check your connection.';
      bool triggerEmergency = false;

      if (finalReplyText.contains('[EMERGENCY_TRIGGER]')) {
        triggerEmergency = true;
        finalReplyText = finalReplyText.replaceAll('[EMERGENCY_TRIGGER]', '').trim();
      }

      final curatorMsg = {
        'sender': 'curator',
        'text': finalReplyText,
      };

      vaultService.saveCuratorChatMessage(curatorMsg);
      vaultService.updateAthenaLastPromptTime(timeService.now.millisecondsSinceEpoch);

      setState(() {
        _messages = List.from(vaultService.getCuratorChatHistory());
        _isSending = false;
      });

      _scrollToBottom();

      if (triggerEmergency) {
        _showEmergencyModal();
      }
    }
  }

  void _checkAndHandleEmergencyTag() {
    if (_messages.isNotEmpty) {
      final lastMsg = _messages.last;
      if (lastMsg['sender'] == 'curator' && lastMsg['text']?.contains('[EMERGENCY_TRIGGER]') == true) {
        lastMsg['text'] = lastMsg['text']!.replaceAll('[EMERGENCY_TRIGGER]', '').trim();
        _showEmergencyModal();
      }
    }
  }

  void _showEmergencyModal() {
    Future.delayed(const Duration(milliseconds: 1000), () {
      if (mounted) {
        EmergencyMedicalModal.show(context);
      }
    });
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  void dispose() {
    if (_vitalsService?.currentState == AlertState.inChatWithAthena) {
      _vitalsService?.resetToIdle();
    }
    _vaultService?.removeListener(_onVaultUpdated);
    _textController.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0D1117),
      appBar: AppBar(
        backgroundColor: const Color(0xFF161B22),
        elevation: 0,
        iconTheme: const IconThemeData(color: Colors.cyanAccent),
        title: const Row(
          children: [
            Icon(Icons.support_agent_rounded, color: Colors.blueAccent, size: 20),
            SizedBox(width: 8),
            Text(
              "ATHENA CHAT",
              style: TextStyle(
                color: Colors.white,
                fontSize: 14,
                fontWeight: FontWeight.bold,
                letterSpacing: 1.1,
              ),
            ),
          ],
        ),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(1.0),
          child: Container(color: Colors.blueAccent.withOpacity(0.3), height: 1.0),
        ),
      ),
      body: _isInitializing
          ? const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            CircularProgressIndicator(color: Colors.cyanAccent),
            SizedBox(height: 16),
            Text(
              'ANALYZING VERDICT AND RECOMMENDATIONS...',
              style: TextStyle(
                color: Colors.white70,
                fontSize: 11,
                fontFamily: 'Courier',
                letterSpacing: 1.0,
              ),
            ),
          ],
        ),
      )
          : Column(
        children: [
          Expanded(
            child: ListView.builder(
              controller: _scrollController,
              padding: const EdgeInsets.all(16),
              itemCount: _messages.length,
              itemBuilder: (context, indexVal) {
                final msg = _messages[indexVal];
                final isUser = msg['sender'] == 'user';
                return Align(
                  alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
                  child: Container(
                    margin: const EdgeInsets.symmetric(vertical: 6),
                    constraints: BoxConstraints(
                      maxWidth: MediaQuery.of(context).size.width * 0.8,
                    ),
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: isUser
                          ? Colors.blueAccent.withOpacity(0.2)
                          : const Color(0xFF161B22),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(
                        color: isUser
                            ? Colors.blueAccent.withOpacity(0.6)
                            : Colors.cyanAccent.withOpacity(0.3),
                        width: 1,
                      ),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          isUser ? "YOU" : "ATHENA",
                          style: TextStyle(
                            color: isUser ? Colors.blueAccent : Colors.cyanAccent,
                            fontSize: 9,
                            fontWeight: FontWeight.bold,
                            letterSpacing: 1.0,
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          msg['text'] ?? '',
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 13,
                            height: 1.3,
                          ),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
          if (_isSending)
            const Padding(
              padding: EdgeInsets.symmetric(horizontal: 16, vertical: 4),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  "Athena is typing...",
                  style: TextStyle(
                    color: Colors.white38,
                    fontSize: 11,
                    fontStyle: FontStyle.italic,
                  ),
                ),
              ),
            ),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              border: Border(
                top: BorderSide(color: Colors.white.withOpacity(0.1)),
              ),
            ),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _textController,
                    style: const TextStyle(color: Colors.white, fontSize: 13),
                    decoration: InputDecoration(
                      hintText: 'Type your response or question...',
                      hintStyle: const TextStyle(color: Colors.white38, fontSize: 12),
                      filled: true,
                      fillColor: const Color(0xFF0D1117),
                      contentPadding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(10),
                        borderSide: BorderSide(color: Colors.white.withOpacity(0.1)),
                      ),
                      focusedBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(10),
                        borderSide: const BorderSide(color: Colors.cyanAccent),
                      ),
                    ),
                    onSubmitted: (_) => _sendMessage(),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton(
                  icon: const Icon(Icons.send_rounded, color: Colors.cyanAccent),
                  onPressed: _sendMessage,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}