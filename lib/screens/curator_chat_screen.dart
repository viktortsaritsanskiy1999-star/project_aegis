import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/api/aegis_api_client.dart';
import '../services/api/medical_ai_api_models.dart';
import '../services/api/vitals_api_models.dart';
import '../services/auth/auth_session_manager.dart';
import '../services/vault_service.dart';
import '../services/time_service.dart';
import '../services/vitals_monitor_service.dart';
import '../widgets/emergency_medical_modal.dart';

class CuratorChatScreen extends StatefulWidget {
  final AegisApiClient? apiClient;
  final AuthSessionManager? authSessionManager;
  final VaultService? vaultService;
  final TimeService? timeService;

  const CuratorChatScreen({
    super.key,
    this.apiClient,
    this.authSessionManager,
    this.vaultService,
    this.timeService,
  });

  @override
  State<CuratorChatScreen> createState() => _CuratorChatScreenState();
}

class _CuratorChatScreenState extends State<CuratorChatScreen> {
  final TextEditingController _textController = TextEditingController();
  final ScrollController _scrollController = ScrollController();

  List<Map<String, String>> _messages = [];
  bool _isInitializing = true;
  bool _isSending = false;
  String? _errorMessage;

  String? _pendingMessageText;
  String? _pendingMessageIdempotencyKey;

  int _chatEpoch = 0;
  String? _trackedPatientId;

  VaultService? _vaultService;
  AuthSessionManager? _authSessionManager;
  VitalsMonitorService? _vitalsService;

  AegisApiClient _resolveApiClient() {
    if (widget.apiClient != null) return widget.apiClient!;
    return context.read<AegisApiClient>();
  }

  AuthSessionManager? _resolveAuthManager() {
    if (widget.authSessionManager != null) return widget.authSessionManager;
    try {
      return context.read<AuthSessionManager>();
    } catch (_) {
      return null;
    }
  }

  VaultService? _resolveVaultService() {
    if (widget.vaultService != null) return widget.vaultService;
    try {
      return context.read<VaultService>();
    } catch (_) {
      return null;
    }
  }

  TimeService? _resolveTimeService() {
    if (widget.timeService != null) return widget.timeService;
    try {
      return context.read<TimeService>();
    } catch (_) {
      return null;
    }
  }

  VitalsMonitorService? _resolveVitalsService() {
    try {
      return context.read<VitalsMonitorService>();
    } catch (_) {
      return null;
    }
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _vaultService = _resolveVaultService();
      _authSessionManager = _resolveAuthManager();
      _vitalsService = _resolveVitalsService();

      _trackedPatientId = _authSessionManager?.confirmedAttributionPatientId;
      _authSessionManager?.addListener(_onAuthChanged);
      _vaultService?.addListener(_onVaultUpdated);

      _loadChatHistoryAndInitialize();
    });
  }

  void _onAuthChanged() {
    if (!mounted) return;
    final currentId = _authSessionManager?.confirmedAttributionPatientId;
    if (currentId != _trackedPatientId) {
      _trackedPatientId = currentId;
      _pendingMessageText = null;
      _pendingMessageIdempotencyKey = null;
      setState(() {
        _messages = [];
        _isSending = false;
        _errorMessage = null;
      });
      _loadChatHistoryAndInitialize();
    }
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

  /// Loads conversational history from server for authenticated patient.
  /// Strictly rejects cross-patient contamination via session epoch fencing.
  Future<void> _loadChatHistoryAndInitialize() async {
    final auth = _resolveAuthManager();
    final vault = _resolveVaultService();
    final timeService = _resolveTimeService();

    if (auth == null || !auth.isAuthenticated) {
      if (mounted) {
        setState(() {
          _isInitializing = false;
          _errorMessage = "Authentication required. Please pair your device.";
        });
      }
      return;
    }

    final patientId = auth.confirmedAttributionPatientId;
    if (patientId == null) {
      if (mounted) {
        setState(() {
          _isInitializing = false;
          _errorMessage = "Active patient attribution not found.";
        });
      }
      return;
    }

    final epoch = ++_chatEpoch;

    // Show cached messages from Vault first if available
    if (vault != null) {
      final cached = vault.getCuratorChatHistory();
      if (cached.isNotEmpty) {
        setState(() {
          _messages = List.from(cached);
          _isInitializing = false;
          _errorMessage = null;
        });
        _scrollToBottom();
      } else {
        setState(() {
          _isInitializing = true;
          _errorMessage = null;
        });
      }
    } else {
      setState(() {
        _isInitializing = true;
        _errorMessage = null;
      });
    }

    final apiClient = _resolveApiClient();

    try {
      final historyResponse = await apiClient.getAthenaHistory();

      if (!mounted || _chatEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
        return;
      }

      if (historyResponse.messages.isNotEmpty) {
        final serverMessages = historyResponse.messages
            .map((m) => {'sender': m.sender, 'text': m.text})
            .toList();

        vault?.syncCuratorChatHistory(serverMessages, expectedPatientId: patientId);
        vault?.setUnreadAthenaMessage(false);

        setState(() {
          _messages = List.from(serverMessages);
          _isInitializing = false;
          _errorMessage = null;
        });
        _scrollToBottom();
      } else {
        // If server history is empty, check if local vault already has messages before generating greeting
        final localHistory = vault?.getCuratorChatHistory() ?? [];
        if (localHistory.isNotEmpty) {
          setState(() {
            _messages = List.from(localHistory);
            _isInitializing = false;
            _errorMessage = null;
          });
          _scrollToBottom();
          return;
        }

        // Both server and local history are empty: generate initial greeting via server turn
        final greetingKey = 'init_greeting_${patientId}_${historyResponse.cycleId}';
        final greetingRequest = AthenaChatRequest.createTurn(
          eventType: 'initial_greeting',
          idempotencyKey: greetingKey,
          sprintDay: vault?.getAthenaSprintDay() ?? 1,
        );

        final greetingResponse = await apiClient.sendAthenaMessage(greetingRequest);

        if (!mounted || _chatEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
          return;
        }

        final initialMsg = {'sender': 'curator', 'text': greetingResponse.reply};
        vault?.saveCuratorChatMessage(initialMsg);
        final nowMs = timeService != null
            ? timeService.now.millisecondsSinceEpoch
            : DateTime.now().millisecondsSinceEpoch;
        vault?.updateAthenaLastPromptTime(nowMs);

        setState(() {
          _messages = vault != null
              ? List.from(vault.getCuratorChatHistory())
              : [initialMsg];
          _isInitializing = false;
          _errorMessage = null;
        });
        _scrollToBottom();

        if (greetingResponse.emergencyTrigger) {
          _showEmergencyModal();
        }
      }
    } on AegisForbiddenException catch (e) {
      if (mounted && _chatEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isInitializing = false;
          _errorMessage = "Access denied: session lacks required chat:read scope (${e.message}).";
        });
      }
    } on AegisTimeoutException catch (_) {
      if (mounted && _chatEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isInitializing = false;
          if (_messages.isEmpty) {
            _errorMessage = "Request timed out while loading conversation history.";
          }
        });
      }
    } on AegisNetworkException catch (e) {
      if (mounted && _chatEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isInitializing = false;
          if (_messages.isEmpty) {
            _errorMessage = "Network connection failed: ${e.message}";
          }
        });
      }
    } on AegisApiException catch (e) {
      if (mounted && _chatEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isInitializing = false;
          if (_messages.isEmpty) {
            _errorMessage = e.message;
          }
        });
      }
    } catch (e) {
      if (mounted && _chatEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isInitializing = false;
          if (_messages.isEmpty) {
            _errorMessage = "Error loading chat: $e";
          }
        });
      }
    }
  }

  /// Sends a new user message via server chat API.
  Future<void> _sendMessage() async {
    final text = _textController.text.trim();
    if (text.isEmpty || _isSending) return;

    final auth = _resolveAuthManager();
    if (auth == null || !auth.isAuthenticated) {
      setState(() {
        _errorMessage = "Authentication required. Please pair your device.";
      });
      return;
    }

    final patientId = auth.confirmedAttributionPatientId;
    if (patientId == null) {
      setState(() {
        _errorMessage = "Active patient attribution not found.";
      });
      return;
    }

    final vault = _resolveVaultService();

    // Prepare stable UUID idempotency key for this message
    if (_pendingMessageText != text || _pendingMessageIdempotencyKey == null) {
      _pendingMessageText = text;
      _pendingMessageIdempotencyKey = AthenaChatRequest.generateUuidV4();
    }

    _textController.clear();

    // Append user message to UI and Vault once
    final userMsg = {'sender': 'user', 'text': text};
    vault?.saveCuratorChatMessage(userMsg);

    setState(() {
      _messages = vault != null ? List.from(vault.getCuratorChatHistory()) : [..._messages, userMsg];
      _isSending = true;
      _errorMessage = null;
    });
    _scrollToBottom();

    await _executeSendTurn(
      patientId: patientId,
      text: text,
      idempotencyKey: _pendingMessageIdempotencyKey!,
    );
  }

  /// Retries sending the pending message using the EXACT SAME idempotency key.
  /// Prevents duplication of user messages on both client UI and server history.
  Future<void> _retryPendingMessage() async {
    if (_isSending || _pendingMessageText == null || _pendingMessageIdempotencyKey == null) {
      return;
    }

    final auth = _resolveAuthManager();
    if (auth == null || !auth.isAuthenticated || auth.confirmedAttributionPatientId == null) {
      setState(() {
        _errorMessage = "Authentication required. Please pair your device.";
      });
      return;
    }

    final patientId = auth.confirmedAttributionPatientId!;

    setState(() {
      _isSending = true;
      _errorMessage = null;
    });

    await _executeSendTurn(
      patientId: patientId,
      text: _pendingMessageText!,
      idempotencyKey: _pendingMessageIdempotencyKey!,
    );
  }

  Future<void> _executeSendTurn({
    required String patientId,
    required String text,
    required String idempotencyKey,
  }) async {
    final auth = _resolveAuthManager();
    final vault = _resolveVaultService();
    final apiClient = _resolveApiClient();
    final timeService = _resolveTimeService();

    final epoch = ++_chatEpoch;

    try {
      final request = AthenaChatRequest.createTurn(
        message: text,
        eventType: 'user_message',
        idempotencyKey: idempotencyKey,
        sprintDay: vault?.getAthenaSprintDay() ?? 1,
      );

      final response = await apiClient.sendAthenaMessage(request);

      // Attribution & epoch fence check
      if (!mounted || _chatEpoch != epoch || auth?.confirmedAttributionPatientId != patientId) {
        return;
      }

      // Success: clear pending retry state
      _pendingMessageIdempotencyKey = null;
      _pendingMessageText = null;

      final curatorMsg = {
        'sender': 'curator',
        'text': response.reply,
      };

      vault?.saveCuratorChatMessage(curatorMsg);
      final nowMs = timeService != null
          ? timeService.now.millisecondsSinceEpoch
          : DateTime.now().millisecondsSinceEpoch;
      vault?.updateAthenaLastPromptTime(nowMs);

      setState(() {
        _messages = vault != null
            ? List.from(vault.getCuratorChatHistory())
            : [..._messages, curatorMsg];
        _isSending = false;
        _errorMessage = null;
      });
      _scrollToBottom();

      if (response.emergencyTrigger) {
        _showEmergencyModal();
      }
    } on AegisForbiddenException catch (e) {
      if (mounted && _chatEpoch == epoch && auth?.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isSending = false;
          _errorMessage = "Access denied: session lacks required chat:write scope (${e.message}).";
        });
      }
    } on AegisTimeoutException catch (_) {
      if (mounted && _chatEpoch == epoch && auth?.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isSending = false;
          _errorMessage = "Message sending timed out. You can safely retry without duplicating.";
        });
      }
    } on AegisNetworkException catch (e) {
      if (mounted && _chatEpoch == epoch && auth?.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isSending = false;
          _errorMessage = "Network connection failed: ${e.message}";
        });
      }
    } on AegisApiException catch (e) {
      if (mounted && _chatEpoch == epoch && auth?.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isSending = false;
          _errorMessage = e.message;
        });
      }
    } catch (e) {
      if (mounted && _chatEpoch == epoch && auth?.confirmedAttributionPatientId == patientId) {
        setState(() {
          _isSending = false;
          _errorMessage = "Error sending message: $e";
        });
      }
    }
  }

  void _showEmergencyModal() {
    Future.delayed(const Duration(milliseconds: 500), () {
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
    _authSessionManager?.removeListener(_onAuthChanged);
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
          child: Container(color: Colors.blueAccent.withValues(alpha: 0.3), height: 1.0),
        ),
      ),
      body: _isInitializing
          ? const Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  CircularProgressIndicator(
                    key: Key('chat_initializing_indicator'),
                    color: Colors.cyanAccent,
                  ),
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
                    key: const Key('chat_messages_list'),
                    controller: _scrollController,
                    padding: const EdgeInsets.all(16),
                    itemCount: _messages.length,
                    itemBuilder: (context, indexVal) {
                      final msg = _messages[indexVal];
                      final isUser = msg['sender'] == 'user';
                      return Align(
                        alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
                        child: Container(
                          key: Key('chat_bubble_${indexVal}_${isUser ? 'user' : 'curator'}'),
                          margin: const EdgeInsets.symmetric(vertical: 6),
                          constraints: BoxConstraints(
                            maxWidth: MediaQuery.of(context).size.width * 0.8,
                          ),
                          padding: const EdgeInsets.all(12),
                          decoration: BoxDecoration(
                            color: isUser
                                ? Colors.blueAccent.withValues(alpha: 0.2)
                                : const Color(0xFF161B22),
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(
                              color: isUser
                                  ? Colors.blueAccent.withValues(alpha: 0.6)
                                  : Colors.cyanAccent.withValues(alpha: 0.3),
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
                        key: Key('chat_typing_indicator'),
                        style: TextStyle(
                          color: Colors.white38,
                          fontSize: 11,
                          fontStyle: FontStyle.italic,
                        ),
                      ),
                    ),
                  ),
                if (_errorMessage != null)
                  Container(
                    key: const Key('chat_error_banner'),
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                    color: Colors.redAccent.withValues(alpha: 0.15),
                    child: Row(
                      children: [
                        const Icon(Icons.error_outline_rounded, color: Colors.redAccent, size: 16),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            _errorMessage!,
                            style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                          ),
                        ),
                        if (_pendingMessageText != null)
                          TextButton(
                            key: const Key('chat_retry_button'),
                            onPressed: _isSending ? null : _retryPendingMessage,
                            child: const Text(
                              'RETRY',
                              style: TextStyle(
                                color: Colors.cyanAccent,
                                fontWeight: FontWeight.bold,
                                fontSize: 12,
                              ),
                            ),
                          )
                        else
                          TextButton(
                            key: const Key('chat_reload_button'),
                            onPressed: _isInitializing ? null : _loadChatHistoryAndInitialize,
                            child: const Text(
                              'RELOAD',
                              style: TextStyle(
                                color: Colors.cyanAccent,
                                fontWeight: FontWeight.bold,
                                fontSize: 12,
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: const Color(0xFF161B22),
                    border: Border(
                      top: BorderSide(color: Colors.white.withValues(alpha: 0.1)),
                    ),
                  ),
                  child: Row(
                    children: [
                      Expanded(
                        child: TextField(
                          key: const Key('chat_input_field'),
                          controller: _textController,
                          enabled: !_isSending,
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
                              borderSide: BorderSide(color: Colors.white.withValues(alpha: 0.1)),
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
                        key: const Key('chat_send_button'),
                        icon: const Icon(Icons.send_rounded, color: Colors.cyanAccent),
                        onPressed: _isSending ? null : _sendMessage,
                      ),
                    ],
                  ),
                ),
              ],
            ),
    );
  }
}
