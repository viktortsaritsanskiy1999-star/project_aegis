import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'services/api/aegis_api_client.dart';
import 'services/auth/auth_session_manager.dart';
import 'services/vault_service.dart';
import 'services/brain_service.dart';
import 'widgets/aegis_device_pairing_modal.dart';
import 'package:aegis_health/screens/smart_watch_import_screen.dart';

class AnamnesisScreen extends StatefulWidget {
  @override
  _AnamnesisScreenState createState() => _AnamnesisScreenState();
}

class _AnamnesisScreenState extends State<AnamnesisScreen> {
  final PageController _controller = PageController();
  int _currentPage = 0;
  final int _totalPages = 17;

  // --- SELECTION STATES ---
  String? _currentError; // Universal error indicator for the current screen
  String? _selectedGender;
  bool _isSmokingBranch = false;
  bool _isSmokingFrequencyBranch = false;
  bool _isAlcoholBranch = false;
  bool _isWorkFrequencyBranch = false;
  bool _isOncoBranch = false;
  bool _isSubmitting = false;
  int? _lastSeenEpoch;
  String? _lastSeenPatientId;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final vault = Provider.of<VaultService>(context, listen: true);
    if (_lastSeenPatientId != null &&
        (_lastSeenPatientId != vault.activePatientId || _lastSeenEpoch != vault.sessionEpoch)) {
      _resetLocalState();
    } else if (_lastSeenPatientId == null && vault.activePatientId != null) {
      _restoreLocalStateFromVault(vault);
    }
    _lastSeenPatientId = vault.activePatientId;
    _lastSeenEpoch = vault.sessionEpoch;
  }

  void _restoreLocalStateFromVault(VaultService vault) {
    final global = vault.getSystemContext('global');
    if (global.isEmpty) return;
    if (_selectedGender == null && global['gender'] != null) {
      final g = global['gender'].toString().toLowerCase();
      if (g == 'male') _selectedGender = 'm';
      if (g == 'female') _selectedGender = 'f';
    }
    if (hController.text.isEmpty && global['height'] != null) {
      hController.text = global['height'].toString();
    }
    if (wController.text.isEmpty && global['weight'] != null) {
      wController.text = global['weight'].toString();
    }
  }

  void _resetLocalState() {
    _selectedGender = null;
    _multiSelect.clear();
    _customInputController.clear();
    hController.clear();
    wController.clear();
    _isSmokingBranch = false;
    _isSmokingFrequencyBranch = false;
    _isAlcoholBranch = false;
    _isWorkFrequencyBranch = false;
    _isOncoBranch = false;
    _currentError = null;
    _currentPage = 0;
    if (_controller.hasClients) {
      _controller.jumpToPage(0);
    }
  }

  final Set<String> _multiSelect = {};

  // --- CONTROLLERS ---
  final TextEditingController _customInputController = TextEditingController();
  final TextEditingController hController = TextEditingController(); // Height
  final TextEditingController wController = TextEditingController(); // Weight

  @override
  void dispose() {
    _controller.dispose();
    _customInputController.dispose();
    hController.dispose();
    wController.dispose();
    super.dispose();
  }
  Widget build(BuildContext context) {
    double progress = (_currentPage) / (_totalPages - 1);

    return Scaffold(
      backgroundColor: const Color(0xFF1A1A1A),
      body: SafeArea(
        child: Column(
          children: [
            LinearProgressIndicator(
              value: progress,
              backgroundColor: Colors.white10,
              valueColor: const AlwaysStoppedAnimation<Color>(Colors.greenAccent),
              minHeight: 4,
            ),
            Expanded(
              child: PageView(
                controller: _controller,
                physics: const NeverScrollableScrollPhysics(),
                onPageChanged: (page) => setState(() => _currentPage = page),
                children: [
                  _stepAgeGate(), // 0
                  _stepBioProfile(), // 1
                  _stepDimensions(), // 2
                  _stepNicotine(), // 3
                  _stepAlcohol(), // 4
                  _stepSpecFactors(), // 5
                  _stepSleep(), // 6
                  _stepDiet(), // 7
                  _stepDietHabits(), // 8
                  _stepGISignals(), // 9
                  _stepWorkMode(), // 10
                  _stepSport(), // 10.1
                  _stepChronic(), // 11
                  _stepRecentEvents(), // 12
                  _stepSafeSpace(), // 13
                  _stepHeredity(), // 14
                  _stepMeds(), // 15
                  _stepFinal(), // Finish
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  // --- UI ELEMENTS ---

  Widget _wrapper({required List<Widget> children}) {
    return SingleChildScrollView(
      // Adding bottom padding so the keyboard doesn't cover the button
      padding: EdgeInsets.only(
          left: 30,
          right: 30,
          top: 40,
          bottom: MediaQuery.of(context).viewInsets.bottom + 20
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          ...children,
          if (_currentError != null) ...[
            const SizedBox(height: 20),
            Text(
              _currentError!,
              textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold, fontSize: 14),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildTitle(String text) {
    return Text(text, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold));
  }

  Widget _buildSubtitle(String text) {
    return Padding(
      padding: const EdgeInsets.only(top: 15, bottom: 30),
      child: Text(text, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white60, fontSize: 15, height: 1.5)),
    );
  }

  // Button with choice fixation (for gender and others)
  Widget _buildSelectableOption(String label, bool isSelected, VoidCallback onTap) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: SizedBox(
        width: double.infinity,
        height: 55,
        child: OutlinedButton(
          style: OutlinedButton.styleFrom(
            backgroundColor: isSelected ? Colors.greenAccent.withOpacity(0.1) : Colors.transparent,
            side: BorderSide(color: isSelected ? Colors.greenAccent : Colors.white24, width: isSelected ? 2 : 1),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          ),
          onPressed: onTap,
          child: Text(label, style: TextStyle(color: isSelected ? Colors.greenAccent : Colors.white, fontSize: 16)),
        ),
      ),
    );
  }

  Widget _buildCheckOption(String label, String key) {
    bool isSelected = _multiSelect.contains(key);

    return _buildSelectableOption(label, isSelected, () {
      setState(() {
        // List of all placeholders (Added safe_clear)
        final noneKeys = [
          'spec_none', 'chr_none', 'hab_balance', 'gi_ok',
          'ill_ok', 'safe_none', 'safe_clear', 'her_none', 'med_none'
        ];

        // 1. "CLEAR ALL" LOGIC (Placeholders)
        if (noneKeys.contains(key)) {
          if (isSelected) {
            // If it was already selected — just unselect it
            _multiSelect.remove(key);
          } else {
            // If not selected — clear everything and select it
            _multiSelect.clear();
            _multiSelect.add(key);
            _customInputController.clear();
          }
        }
        // 2. OTHER LOGIC (Vegan and others...)
        else {
          // Remove all possible placeholders when choosing a specific item
          _multiSelect.removeWhere((item) => noneKeys.contains(item));

          if (key == 'diet_vegan') {
            _multiSelect.remove('diet_meat');
            _multiSelect.remove('diet_fish');
            _multiSelect.remove('diet_milk');
            _multiSelect.add(key);
          } else {
            if (['diet_meat', 'diet_fish', 'diet_milk'].contains(key)) {
              _multiSelect.remove('diet_vegan');
            }

            if (isSelected) {
              _multiSelect.remove(key);
            } else {
              _multiSelect.add(key);
            }
          }
        }
      });
    });
  }

  // --- CARDS ---

  Widget _stepAgeGate() => _wrapper(children: [
    _buildTitle("Let's set the ground rules 🤝"),
    _buildSubtitle("«We all know how this works on other websites: you click \"I am 18\" and move on. But here, the story is different...\n\nBy clicking the button, you confirm that you are an adult (18+) and take full responsibility for the accuracy of your data.»"),
    _buildSelectableOption("I'm 18+, let's go! 🚀", false, () {
      context.read<VaultService>().saveAnamnesis('age_confirmation', 'Confirmed');
      _nextPage();
    }),
  ]);

  Widget _stepBioProfile() => _wrapper(children: [
    _buildTitle("Biological Profile"),
    _buildSubtitle("«Where shall we start? Gender and age determine your baseline health metrics.»"),

    Row(children: [
      Expanded(child: _buildSelectableOption("🧔 Male", _selectedGender == 'm', () => setState(() => _selectedGender = 'm'))),
      const SizedBox(width: 10),
      Expanded(child: _buildSelectableOption("👩 Female", _selectedGender == 'f', () => setState(() => _selectedGender = 'f'))),
    ]),

    TextField(
      controller: _customInputController,
      keyboardType: TextInputType.number,
      style: const TextStyle(color: Colors.white),
      decoration: const InputDecoration(
        labelText: "Age (18-99)",
        labelStyle: TextStyle(color: Colors.white38),
        enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
      ),
    ),

    const SizedBox(height: 20),

    _buildSelectableOption("Next", false, () {
      // 1. Hide the keyboard immediately on click to see the result
      FocusScope.of(context).unfocus();

      final String ageInput = _customInputController.text.trim();
      final int? age = int.tryParse(ageInput);

      // 2. Use the general _currentError instead of a local variable
      if (_selectedGender == null) {
        setState(() => _currentError = "Please select your gender");
        return;
      }

      if (ageInput.isEmpty) {
        setState(() => _currentError = "Please enter your age");
        return;
      }

      if (age == null || age < 18 || age > 99) {
        setState(() => _currentError = "Age must be between 18 and 99 years");
        return;
      }

      // If no errors — clear error and save
      setState(() => _currentError = null);

      context.read<VaultService>().saveAnamnesis('gender', _selectedGender == 'm' ? 'Male' : 'Female');
      context.read<VaultService>().saveAnamnesis('age', ageInput);

      _nextPage();
    }),
  ]);

  Widget _stepDimensions() {
    return _wrapper(children: [
      _buildTitle("Your Dimensions"),
      _buildSubtitle("«Your height and weight are needed to calculate metabolic load.»"),

      TextField(
          controller: hController,
          keyboardType: TextInputType.number,
          style: const TextStyle(color: Colors.white),
          decoration: const InputDecoration(
            labelText: "Height (cm)",
            labelStyle: TextStyle(color: Colors.white38),
            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
          )
      ),

      TextField(
          controller: wController,
          keyboardType: TextInputType.number,
          style: const TextStyle(color: Colors.white),
          decoration: const InputDecoration(
            labelText: "Weight (kg)",
            labelStyle: TextStyle(color: Colors.white38),
            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
          )
      ),

      const SizedBox(height: 30),

      _buildSelectableOption("Next", false, () {
        // 1. Hide the keyboard to see the error if any
        FocusScope.of(context).unfocus();

        final String hInput = hController.text.trim();
        final String wInput = wController.text.trim();

        final int? height = int.tryParse(hInput);
        final int? weight = int.tryParse(wInput);

        // 2. Validation via the general error variable
        if (hInput.isEmpty || height == null || height < 100 || height > 250) {
          setState(() => _currentError = "Please enter a valid height (100-250 cm)");
          return;
        }

        if (wInput.isEmpty || weight == null || weight < 30 || weight > 300) {
          setState(() => _currentError = "Please enter a valid weight (30-300 kg)");
          return;
        }

        // 3. If we reached here, everything is clear
        setState(() => _currentError = null);

        context.read<VaultService>().saveAnamnesis('height', hInput);
        context.read<VaultService>().saveAnamnesis('weight', wInput);

        // Clear controllers and proceed (error will be reset in _nextPage)
        hController.clear();
        wController.clear();
        _nextPage();
      }),
    ]);
  }

  Widget _stepNicotine() {
    return _wrapper(children: [
      _buildTitle("Nicotine 🚬"),
      _buildSubtitle("«Nicotine interaction directly affects blood viscosity.»"),

      if (!_isSmokingBranch) ...[
        _buildSelectableOption("1️⃣ Non-user", false, () {
          context.read<VaultService>().saveAnamnesis('nicotine_status', 'Non-user');
          _nextPage();
        }),
        _buildSelectableOption("2️⃣ User (Cigarettes, vape...)", false, () {
          setState(() => _isSmokingBranch = true);
        }),
        _buildSelectableOption("3️⃣ Quit (> 6 months ago)", false, () {
          context.read<VaultService>().saveAnamnesis('nicotine_status', 'Quit');
          _nextPage();
        }),
      ]
      else if (!_isSmokingFrequencyBranch) ...[
        const Text("Duration:", style: TextStyle(color: Colors.blueAccent)),
        _buildSelectableOption("Under a year", false, () {
          context.read<VaultService>().saveAnamnesis('nicotine_years', 'Under a year');
          setState(() => _isSmokingFrequencyBranch = true);
        }),
        _buildSelectableOption("1-5 years", false, () {
          context.read<VaultService>().saveAnamnesis('nicotine_years', '1-5 years');
          setState(() => _isSmokingFrequencyBranch = true);
        }),
        _buildSelectableOption("5-10 years", false, () {
          context.read<VaultService>().saveAnamnesis('nicotine_years', '5-10 years');
          setState(() => _isSmokingFrequencyBranch = true);
        }),
        _buildSelectableOption("10+ years", false, () {
          context.read<VaultService>().saveAnamnesis('nicotine_years', '10+ years');
          setState(() => _isSmokingFrequencyBranch = true);
        }),
      ]
      else ...[
          const Text("Frequency:", style: TextStyle(color: Colors.blueAccent)),
          _buildSelectableOption("Rarely", false, () {
            context.read<VaultService>().saveAnamnesis('nicotine_status', 'Active');
            context.read<VaultService>().saveAnamnesis('nicotine_freq', 'Rarely');
            _nextPage();
          }),
          _buildSelectableOption("Several times a day", false, () {
            context.read<VaultService>().saveAnamnesis('nicotine_status', 'Active');
            context.read<VaultService>().saveAnamnesis('nicotine_freq', 'Daily');
            _nextPage();
          }),
          _buildSelectableOption("A pack or more", false, () {
            context.read<VaultService>().saveAnamnesis('nicotine_status', 'Active');
            context.read<VaultService>().saveAnamnesis('nicotine_freq', 'Pack+');
            _nextPage();
          }),
        ]
    ]);
  }

  Widget _stepAlcohol() => _wrapper(children: [
    _buildTitle("Alcohol 🍷"),
    _buildSubtitle("«Frequency helps distinguish temporary noise from systemic issues.»"),

    if (!_isAlcoholBranch) ...[
      _buildSelectableOption("1️⃣ Zero (do not drink at all)", false, () {
        context.read<VaultService>().saveAnamnesis('alcohol_status', 'Non-user');
        _nextPage();
      }),
      _buildSelectableOption("2️⃣ Rarely (up to once a month)", false, () {
        context.read<VaultService>().saveAnamnesis('alcohol_status', 'Rarely');
        _nextPage();
      }),
      _buildSelectableOption("3️⃣ Moderately / Frequently", false, () {
        // Go deeper to understand the type of beverages
        setState(() => _isAlcoholBranch = true);
      }),

    ] else ...[
      const Text("What predominates?", style: TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold)),
      const SizedBox(height: 10),
      _buildSelectableOption("Hard liquor", false, () {
        context.read<VaultService>().saveAnamnesis('alcohol_status', 'Frequently');
        context.read<VaultService>().saveAnamnesis('alcohol_type', 'Hard liquor');
        _nextPage();
      }),
      _buildSelectableOption("Wine", false, () {
        context.read<VaultService>().saveAnamnesis('alcohol_status', 'Frequently');
        context.read<VaultService>().saveAnamnesis('alcohol_type', 'Wine');
        _nextPage();
      }),
      _buildSelectableOption("Beer", false, () {
        context.read<VaultService>().saveAnamnesis('alcohol_status', 'Frequently');
        context.read<VaultService>().saveAnamnesis('alcohol_type', 'Beer');
        _nextPage();
      }),
    ]
  ]);

  Widget _stepSpecFactors() => _wrapper(children: [
    _buildTitle("Specific Factors 🛡️"),
    _buildSubtitle("«This will rule out false conclusions in your lab results. We only ask for interpretation accuracy.»"),

    _buildCheckOption("⚡ Stimulation (coffee, pre-workouts)", "spec_stim"),
    _buildCheckOption("💊 Sedation (antidepressants)", "spec_sed"),
    _buildCheckOption("💨 Inhalations (other than tobacco)", "spec_inhale"),
    _buildCheckOption("🔄 Recovery (past experience, currently clean)", "spec_recovery"),
    _buildCheckOption("🚫 None of these", "spec_none"),

    const SizedBox(height: 20),

    _buildSelectableOption("Continue ➡️", false, () {
      // 1. Validation via isEmpty now works perfectly for Set
      if (_multiSelect.isEmpty) {
        setState(() => _currentError = "Please select an option or click «Prefer not to answer»");
        return;
      }

      // 2. Save. join(', ') will concatenate all Set elements into a string
      context.read<VaultService>().saveAnamnesis('spec_factors', _multiSelect.join(', '));

      _nextPage();
    }),

    const SizedBox(height: 10),

    _buildSelectableOption("🤐 Prefer not to answer", false, () {
      context.read<VaultService>().saveAnamnesis('spec_factors', 'User declined to answer');
      _nextPage();
    }),
  ]);

  Widget _stepSleep() => _wrapper(children: [
    _buildTitle("Sleep 🌙"),
    _buildSubtitle("«Sleep is the repair time for all systems.»"),

    _buildSelectableOption("1️⃣ Sleep great", false, () {
      context.read<VaultService>().saveAnamnesis('sleep_quality', 'Great');
      _nextPage();
    }),

    _buildSelectableOption("2️⃣ Schedule is off", false, () {
      context.read<VaultService>().saveAnamnesis('sleep_quality', 'Schedule off');
      _nextPage();
    }),

    _buildSelectableOption("3️⃣ Insomnia", false, () {
      context.read<VaultService>().saveAnamnesis('sleep_quality', 'Insomnia');
      _nextPage();
    }),
  ]);

  Widget _stepDiet() => _wrapper(children: [
    _buildTitle("Your Diet 🍽️"),
    _buildSubtitle("«What is most often present on your plate?»"),

    _buildCheckOption("🥩 Meat and poultry", "diet_meat"),
    _buildCheckOption("🐟 Fish and seafood", "diet_fish"),
    _buildCheckOption("🥦 Vegetables and greens", "diet_veg"),
    _buildCheckOption("🥛 Dairy products", "diet_milk"),
    _buildCheckOption("🍟 Fast food / Pastries", "diet_junk"),
    _buildCheckOption("🥬 Plant-based only (Vegan)", "diet_vegan"),

    const SizedBox(height: 20),

    _buildSelectableOption("Next ➡️", false, () {
      // 1. Validation: did the user choose anything at all
      if (_multiSelect.isEmpty) {
        setState(() => _currentError = "Please select one or more options");
        return;
      }

      // 2. Save diet to Vault
      context.read<VaultService>().saveAnamnesis('diet_types', _multiSelect.join(', '));

      _nextPage();
    }),
  ]);

  Widget _stepDietHabits() => _wrapper(children: [
    _buildTitle("Dietary Habits 🍰"),
    _buildSubtitle("«Details that may affect glucose levels and kidney function.»"),

    _buildCheckOption("🍭 Sweet tooth (love desserts)", "hab_sugar"),
    _buildCheckOption("🧂 Salty (add salt to food)", "hab_salt"),
    _buildCheckOption("☕ Coffee lover (3+ cups a day)", "hab_coffee"),
    _buildCheckOption("🌙 Late-night binge", "hab_night"),
    _buildCheckOption("💧 Low water intake (< 1 liter)", "hab_water"),

    // New item for "health enthusiasts"
    _buildCheckOption("⚖️ Everything in balance / No such habits", "hab_balance"),

    const SizedBox(height: 20),

    _buildSelectableOption("Next ➡️", false, () {
      // Validation: if nothing at all is selected
      if (_multiSelect.isEmpty) {
        setState(() => _currentError = "Please select 1 or more options or click «Everything in balance»");
        return;
      }

      // Save habits to Vault
      context.read<VaultService>().saveAnamnesis('diet_habits', _multiSelect.join(', '));
      _nextPage();
    }),
  ]);

  Widget _stepGISignals() => _wrapper(children: [
    _buildTitle("GI Signals 🎈"),
    _buildSubtitle("«Do you experience discomfort after eating?»"),

    _buildCheckOption("🎈 Bloating or heaviness", "gi_bloat"),
    _buildCheckOption("🔥 Heartburn", "gi_burn"),
    _buildCheckOption("⚡ Sharp pain", "gi_pain"),
    _buildCheckOption("✅ No discomfort", "gi_ok"),

    const SizedBox(height: 10),

    TextField(
      controller: _customInputController,
      style: const TextStyle(color: Colors.white),
      // Add a listener to reset "gi_ok" if the user starts typing
      onChanged: (text) {
        if (text.isNotEmpty && _multiSelect.contains('gi_ok')) {
          setState(() => _multiSelect.remove('gi_ok'));
        }
      },
      decoration: const InputDecoration(
          hintText: "📝 Something else...",
          prefixIcon: Icon(Icons.edit, color: Colors.white24)
      ),
    ),

    const SizedBox(height: 20),

    _buildSelectableOption("Next ➡️", false, () async {
      final String textInput = _customInputController.text.trim();
      final bool hasText = textInput.isNotEmpty;
      final bool hasSelection = _multiSelect.isNotEmpty;

      // 1. Validation
      if (!hasSelection && !hasText) {
        setState(() => _currentError = "Select an option or describe the issue");
        return;
      }

      // 2. Get services
      final brain = Provider.of<BrainService?>(context, listen: false);
      final vault = context.read<VaultService>();

      // 3. First AI determines systems (if there is text)
      if (hasText && brain != null) {
        await brain.identifySystems(textInput);
      }

      // 4. SAVING: Now without await and without systemsFromAi
      vault.saveAnamnesis(
        'gi_signals',
        _multiSelect.join(', '),
        userText: textInput,
      );

      // Only after the Vault has recorded everything, move on
      _nextPage();
    }),
  ]);

  Widget _stepWorkMode() => _wrapper(children: [
    _buildTitle("Your Workday"),
    _buildSubtitle("«What does your main routine look like?»"),
    if (!_isWorkFrequencyBranch) ...[
      _buildSelectableOption("🪑 Sedentary", false, () {
        context.read<VaultService>().saveAnamnesis('work_type', 'Sedentary');
        setState(() => _isWorkFrequencyBranch = true);
      }),
      _buildSelectableOption("🧍 On my feet all day", false, () {
        context.read<VaultService>().saveAnamnesis('work_type', 'On my feet');
        setState(() => _isWorkFrequencyBranch = true);
      }),
      _buildSelectableOption("🏗️ Heavy physical labor", false, () {
        context.read<VaultService>().saveAnamnesis('work_type', 'Heavy labor');
        setState(() => _isWorkFrequencyBranch = true);
      }),
      _buildSelectableOption("🎓 Studying / Student", false, () {
        context.read<VaultService>().saveAnamnesis('work_type', 'Studying');
        setState(() => _isWorkFrequencyBranch = true);
      }),
      _buildSelectableOption("🏠 Working from home", false, () {
        context.read<VaultService>().saveAnamnesis('work_type', 'Home');
        setState(() => _isWorkFrequencyBranch = true);
      }),
    ] else ...[
      const Text("How often?", style: TextStyle(color: Colors.blueAccent, fontWeight: FontWeight.bold)),
      const SizedBox(height: 10),
      _buildSelectableOption("🗓️ Standard (5/2)", false, () {
        context.read<VaultService>().saveAnamnesis('work_schedule', '5/2');
        _nextPage();
      }),
      _buildSelectableOption("🔄 Shift schedule", false, () {
        context.read<VaultService>().saveAnamnesis('work_schedule', 'Shift');
        _nextPage();
      }),
      _buildSelectableOption("⚡ No days off", false, () {
        context.read<VaultService>().saveAnamnesis('work_schedule', 'No days off');
        _nextPage();
      }),
    ]
  ]);

  Widget _stepSport() => _wrapper(children: [
    _buildTitle("Sport (Optional)"),
    _buildSubtitle("«And what about sports outside of work?»"),

    _buildSelectableOption("🛋️ Couch expert", false, () {
      context.read<VaultService>().saveAnamnesis('sport_level', 'None');
      _nextPage();
    }),
    _buildSelectableOption("🏃 Light activity", false, () {
      context.read<VaultService>().saveAnamnesis('sport_level', 'Light');
      _nextPage();
    }),
    _buildSelectableOption("🏋️ Hardcore (3+ times a week)", false, () {
      context.read<VaultService>().saveAnamnesis('sport_level', 'High');
      _nextPage();
    }),
  ]);

  Widget _stepChronic() => _wrapper(children: [
    _buildTitle("Your Medical Baseline"),
    _buildSubtitle("«Confirmed diagnoses calibrate the norm.»"),

    if (!_isOncoBranch) ...[
      _buildCheckOption("🩸 Blood pressure / Heart", "chr_heart"),
      _buildCheckOption("🍬 Diabetes", "chr_diabetes"),
      _buildCheckOption("🎗️ Oncology", "chr_onco_trigger"),
      _buildCheckOption("🫁 Asthma / Allergy", "chr_asthma"),
      _buildCheckOption("🧪 GI (Gastritis, ulcer)", "chr_gi"),
      _buildCheckOption("🦴 Joints / Spine", "chr_bones"),
      _buildCheckOption("🧠 Neurology", "chr_neuro"),
      _buildCheckOption("✅ No chronic conditions", "chr_none"),

      const SizedBox(height: 10),

      TextField(
        controller: _customInputController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
            hintText: "🌀 Other...",
            prefixIcon: Icon(Icons.add, color: Colors.white24)
        ),
      ),

      _buildSelectableOption("Confirm ➡️", false, () {
        final bool hasText = _customInputController.text.trim().isNotEmpty;

        // 1. VALIDATION: Is anything selected at all?
        if (_multiSelect.isEmpty && !hasText) {
          setState(() => _currentError = "Please select an option or enter your own");
          return;
        }

        // 2. FOOLPROOF: Cannot select "No chronic" together with something else
        if (_multiSelect.contains('chr_none') && (_multiSelect.length > 1 || hasText)) {
          setState(() => _currentError = "«No chronic conditions» selected, but diagnoses are also chosen. Please clarify.");
          return;
        }

        // 3. PROCEED TO ONCO BRANCH (if trigger is pressed)
        if (_multiSelect.contains('chr_onco_trigger')) {
          setState(() => _isOncoBranch = true);
        } else {
          // 4. SAVE AND REGULAR NEXT
          String result = _multiSelect.join(', ');
          if (hasText) {
            result += (result.isEmpty ? "" : ", ") + _customInputController.text.trim();
          }
          context.read<VaultService>().saveAnamnesis('chronic_diseases', result);
          _nextPage();
        }
      }),
    ] else ...[
      _buildTitle("Oncology Status 🎗️"),
      _buildSubtitle("Current stage affects marker references"),

      _buildSelectableOption("🟢 In remission", false, () {
        context.read<VaultService>().saveAnamnesis('onco_status', 'Remission');
        _saveChronicAndNext(); // Helper function (below)
      }),
      _buildSelectableOption("🟡 Newly diagnosed", false, () {
        context.read<VaultService>().saveAnamnesis('onco_status', 'Newly diagnosed');
        _saveChronicAndNext();
      }),
      _buildSelectableOption("🔴 Active treatment", false, () {
        context.read<VaultService>().saveAnamnesis('onco_status', 'Active treatment');
        _saveChronicAndNext();
      }),
      _buildSelectableOption("🟠 Relapse", false, () {
        context.read<VaultService>().saveAnamnesis('onco_status', 'Relapse');
        _saveChronicAndNext();
      }),
    ]
  ]);

// Helper method to avoid duplicating disease list saving code
  void _saveChronicAndNext() {
    String result = _multiSelect.join(', ');
    if (_customInputController.text.isNotEmpty) {
      result += (result.isEmpty ? "" : ", ") + _customInputController.text.trim();
    }
    context.read<VaultService>().saveAnamnesis('chronic_diseases', result);
    _nextPage();
  }

  Widget _stepRecentEvents() => _wrapper(children: [
    _buildTitle("Viral Pause 🤒"),
    _buildSubtitle("«Have you been sick with anything in the last 4 weeks?»"),

    // Using our universal _buildCheckOption
    _buildCheckOption("🟢 Completely healthy", "ill_ok"),
    _buildCheckOption("🟡 ARVI / Cold", "ill_orvi"),
    _buildCheckOption("🟠 COVID-19", "ill_covid"),
    _buildCheckOption("🔴 Poisoning / Infection", "ill_poison"),

    const SizedBox(height: 10),

    TextField(
      controller: _customInputController,
      style: const TextStyle(color: Colors.white),
      onChanged: (text) {
        // If typing starts, remove the "Healthy" checkmark
        if (text.isNotEmpty && _multiSelect.contains('ill_ok')) {
          setState(() => _multiSelect.remove('ill_ok'));
        }
      },
      decoration: const InputDecoration(
          hintText: "📝 Enter your own...",
          prefixIcon: Icon(Icons.history, color: Colors.white24)
      ),
    ),

    const SizedBox(height: 20),

    _buildSelectableOption("Next ➡️", false, () {
      final bool hasText = _customInputController.text.trim().isNotEmpty;
      final bool hasSelection = _multiSelect.isNotEmpty;

      // 1. VALIDATION
      if (!hasSelection && !hasText) {
        setState(() => _currentError = "Please select an option or describe the condition in the field");
        return;
      }

      // 2. FOOLPROOF (Healthy + diseases)
      if (_multiSelect.contains('ill_ok') && (hasSelection && _multiSelect.length > 1 || hasText)) {
        setState(() => _currentError = "«Healthy» is selected, but diseases are also indicated. Please clarify your choice.");
        return;
      }

      // 3. DATA COLLECTION
      String result = _multiSelect.join(', ');
      if (hasText) {
        result += (result.isEmpty ? "" : ", ") + _customInputController.text.trim();
      }

      context.read<VaultService>().saveAnamnesis('recent_illness', result);
      _nextPage();
    }),
  ]);

  Widget _stepSafeSpace() => _wrapper(children: [
    _buildTitle("Specific Screening 🛡️"),
    _buildSubtitle("«Critically important for assessing liver and immune health. This data is strictly confidential.»"),

    _buildCheckOption("🧬 Hepatitis (B or C)", "safe_hep"),
    _buildCheckOption("👤 HIV status", "safe_hiv"),
    _buildCheckOption("🫁 Tuberculosis", "safe_tub"),
    _buildCheckOption("🩸 Syphilis", "safe_siph"),
    _buildCheckOption("✅ Tests are clean / Negative", "safe_clear"),
    _buildCheckOption("🚫 Prefer not to answer", "safe_none"),

    const SizedBox(height: 20),

    _buildSelectableOption("Next ➡️", false, () {
      // 1. Check for emptiness (works for Set/List)
      if (_multiSelect.isEmpty) {
        setState(() => _currentError = "Please select options or a placeholder");
        return;
      }

      // 2. Logic for mapping strings to send to the database
      String result;
      if (_multiSelect.contains('safe_clear')) {
        result = 'No chronic infections (Negative)';
      } else if (_multiSelect.contains('safe_none')) {
        result = 'User declined to answer';
      } else {
        result = _multiSelect.join(', ');
      }

      // 3. Save to VaultService and proceed
      context.read<VaultService>().saveAnamnesis('critical_infections', result);
      _nextPage();
    }),
  ]);

  Widget _stepHeredity() => _wrapper(children: [
    _buildTitle("Family Archive 🌳"),
    _buildSubtitle("«Did close relatives experience serious conditions before 50–60 years of age?»"),

    _buildCheckOption("💔 Heart attacks / Strokes", "her_heart"),
    _buildCheckOption("📉 Oncology", "her_onco"),
    _buildCheckOption("🍭 Diabetes", "her_diab"),
    _buildCheckOption("🧠 Alzheimer's / Dementia", "her_brain"),
    _buildCheckOption("✅ Nothing serious", "her_none"),

    const SizedBox(height: 10),

    TextField(
      controller: _customInputController,
      style: const TextStyle(color: Colors.white),
      onChanged: (text) {
        // If the user starts typing, we must uncheck "Nothing serious"
        if (text.isNotEmpty && _multiSelect.contains('her_none')) {
          setState(() => _multiSelect.remove('her_none'));
        }
      },
      decoration: const InputDecoration(
          hintText: "📝 Add your own (e.g., thyroid)...",
          prefixIcon: Icon(Icons.people, color: Colors.white24)
      ),
    ),

    const SizedBox(height: 20),

    _buildSelectableOption("Next ➡️", false, () {
      final bool hasText = _customInputController.text.trim().isNotEmpty;
      final bool hasSelection = _multiSelect.isNotEmpty;

      // 1. VALIDATION: Do not allow proceeding with completely empty data
      if (!hasSelection && !hasText) {
        setState(() => _currentError = "Please select options or click «Nothing serious»");
        return;
      }

      // 2. CONFLICT PLACEHOLDER: If "Nothing" is selected along with something else
      if (_multiSelect.contains('her_none') && (hasText || _multiSelect.length > 1)) {
        setState(() => _currentError = "«Nothing serious» is selected, but other data has been added. Please clarify.");
        return;
      }

      // 3. SAVE TO VAULT: Bind the result
      String result = _multiSelect.join(', ');
      if (hasText) {
        result += (result.isEmpty ? "" : ", ") + _customInputController.text.trim();
      }

      context.read<VaultService>().saveAnamnesis('heredity_risks', result);
      _nextPage();
    }),
  ]);

  Widget _stepMeds() => _wrapper(children: [
    _buildTitle("Medications and Supplements 💊"),
    _buildSubtitle("«Many medications directly alter lab results. This is critical for accuracy.»"),

    _buildCheckOption("💊 Hormonal (incl. COC)", "med_horm"),
    _buildCheckOption("💊 Statins (for cholesterol)", "med_stat"),
    _buildCheckOption("💊 Vitamins / Supplements", "med_vit"),
    _buildCheckOption("💊 Painkillers (NSAIDs)", "med_pain"),
    _buildCheckOption("🚫 Taking nothing", "med_none"),

    const SizedBox(height: 10),

    TextField(
      controller: _customInputController,
      style: const TextStyle(color: Colors.white),
      onChanged: (text) {
        // Protection: if text input starts, reset "Taking nothing"
        if (text.isNotEmpty && _multiSelect.contains('med_none')) {
          setState(() => _multiSelect.remove('med_none'));
        }
      },
      decoration: const InputDecoration(
          hintText: "🌀 Other (e.g. Metformin)...",
          prefixIcon: Icon(Icons.medication, color: Colors.white24)
      ),
    ),

    const SizedBox(height: 20),

    _buildSelectableOption("Finish 🚀", false, () async {
      if (_isSubmitting) return;

      setState(() {
        _isSubmitting = true;
      });

      try {
        final vault = context.read<VaultService>();
        final auth = Provider.of<AuthSessionManager?>(context, listen: false);
        final apiClient = Provider.of<AegisApiClient?>(context, listen: false);
        final String textInput = _customInputController.text.trim();

        // 1. Form the final medication string
        String result = _multiSelect.join(', ');
        if (textInput.isNotEmpty) {
          result += (result.isEmpty ? "" : ", ") + textInput;
        }

        // 2. Local save to Vault (instant)
        vault.saveAnamnesis(
          'medications',
          result,
          userText: textInput,
        );

        // 3. Ensure local persistence completes
        await vault.flushPendingWrites();
        if (!mounted) return;

        // 4. Check device authentication
        if (auth == null || !auth.isAuthenticated) {
          setState(() => _currentError = "Device pairing required to synchronize anamnesis with Aegis Server.");
          await AegisDevicePairingModal.show(context);
          if (!mounted) return;

          if (auth == null || !auth.isAuthenticated) {
            setState(() => _currentError = "Draft saved securely. Pair device to synchronize with Aegis Server.");
            return;
          }

          // Newly paired: associate guest draft to active patient
          await vault.associateGuestDraftToActivePatient();
          if (!mounted) return;

          setState(() => _currentError = "Device paired! Tap 'Finish 🚀' to synchronize anamnesis with Aegis Server.");
          return;
        }

        // 5. Server synchronization via AegisApiClient
        if (apiClient == null) {
          setState(() => _currentError = "API client unavailable. Draft saved securely.");
          return;
        }

        setState(() {
          _currentError = "Synchronizing with Aegis Server (M1)...";
        });

        final syncResult = await vault.syncAnamnesisToServer(
          apiClient: apiClient,
          authSessionManager: auth,
        );

        if (!mounted) return;

        if (syncResult.isSuccess) {
          setState(() {
            _currentError = null;
          });
          _nextPage();
        } else {
          setState(() {
            if (syncResult.status == AnamnesisSyncStatus.offline) {
              _currentError = "Connection timeout or network failure. Draft saved securely. Tap 'Finish 🚀' to retry.";
            } else if (syncResult.status == AnamnesisSyncStatus.insufficientScopes) {
              _currentError = "Access denied: session lacks 'cascade:intake' scope. Please re-pair device.";
            } else if (syncResult.status == AnamnesisSyncStatus.unauthorized) {
              _currentError = "Session expired or device not paired. Tap 'Finish 🚀' to re-authenticate.";
            } else {
              _currentError = syncResult.errorMessage ?? "Synchronization failed. Draft saved securely.";
            }
          });
        }
      } catch (e) {
        if (!mounted) return;
        setState(() {
          _currentError = "Error: $e. Draft saved securely. Tap 'Finish 🚀' to retry.";
        });
      } finally {
        if (mounted) {
          setState(() {
            _isSubmitting = false;
          });
        }
      }
    }),
  ]);

  Widget _stepFinal() => _wrapper(children: [
    _buildTitle("Your Profile is Configured! 🚀"),
    _buildSubtitle("«We've created your digital twin. The neural network now has enough context.»"),

    _buildSelectableOption("Continue ➡️", false, () {
      Navigator.pushReplacement(
          context,
          MaterialPageRoute(builder: (context) => const SmartWatchImportScreen())
      );
    }),
  ]);

  void _nextPage() {
    setState(() {
      _currentError = null;

      _multiSelect.clear();

      _isSmokingBranch = false;
      _isSmokingFrequencyBranch = false;
      _isAlcoholBranch = false;
      _isWorkFrequencyBranch = false;
      _isOncoBranch = false;
    });

    _customInputController.clear();
    hController.clear();
    wController.clear();

    _controller.nextPage(
        duration: const Duration(milliseconds: 400),
        curve: Curves.easeInOut
    );
  }
}