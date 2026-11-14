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
  bool _isSubmitting = false;
  int? _lastSeenEpoch;
  String? _lastSeenPatientId;

  // --- STAGE 1 ENHANCED STATES ---
  // Card 1: Bio Profile
  bool _ageConfirmedUnusual = false;
  String? _pregnancyStatus;
  String? _pregnancyTrimester;
  final TextEditingController _pregnancyWeeksController = TextEditingController();
  String? _breastfeedingStatus;

  // Card 2: Dimensions & Dynamics
  bool _heightConfirmedUnusual = false;
  bool _weightConfirmedUnusual = false;
  String? _weightChangeStatus;
  final TextEditingController _weightChangeAmountController = TextEditingController();
  String? _weightChangeIntentional;

  // Card 3: Nicotine
  String? _nicotineStatus;
  final Set<String> _nicotineProducts = {};
  final Map<String, String> _nicotineProductTenure = {};
  String? _nicotineCurrentFrequency;
  final Map<String, dynamic> _nicotineQuantities = {};
  String? _nicotineQuitWhen;
  int _nicotineSubStep = 0;

  // Card 4: Alcohol
  String? _alcoholStatus;
  final Set<String> _alcoholTypes = {};
  final Map<String, String> _alcoholVolumes = {};
  final TextEditingController _alcoholCommentController = TextEditingController();
  int _alcoholSubStep = 0;

  // Card 5: Additional Health Factors (Substances)
  final Set<String> _substanceTypes = {};
  String? _substanceLastUse;
  final TextEditingController _substanceNotesController = TextEditingController();

  // --- STAGE 2 ENHANCED STATES ---
  // Card 6: Sleep & Recovery
  String? _sleepDuration;
  String? _sleepQuality;
  final Set<String> _selectedSleepSymptoms = {};
  String? _sleepSchedule;
  final TextEditingController _sleepNotesController = TextEditingController();

  // Card 7: Diet & Eating Habits (merged)
  String? _dietType;
  final TextEditingController _dietCustomDetailsController = TextEditingController();
  bool _hasExcludedFoods = false;
  bool _excludedFoodsCollapsed = false;
  final TextEditingController _excludedFoodsController = TextEditingController();
  String? _dietMealsPerDay;
  String? _dietSnacksPerDay;
  final Set<String> _dietStaples = {};
  final Set<String> _dietOccasionalFoods = {};
  final Map<String, String> _dietFoodFrequencies = {};
  String? _activeOccasionalFood;
  final TextEditingController _dietDayDescriptionController = TextEditingController();

  // Card 8: Drinks & Hydration
  String? _drinksTotalVolume;
  final Set<String> _drinksTypes = {};
  bool _drinksCoffeeCollapsed = false;
  final Set<String> _drinksCoffeeTypes = {};
  String? _drinksCoffeeFrequency;
  String? _drinksCaffeineFrequency;
  final TextEditingController _drinksDayDescriptionController = TextEditingController();

  // --- STAGE 3 ENHANCED STATES ---
  // Card 9: Digestion & Gastrointestinal Health
  final Set<String> _giSymptoms = {};
  final Map<String, String> _giSymptomFrequencies = {};
  final Map<String, String> _giSymptomDurations = {};
  String? _activeGiSymptom;
  final TextEditingController _giDetailsController = TextEditingController();

  // Card 10: Work & Daily Routine
  final TextEditingController _workOccupationController = TextEditingController();
  String? _workActivity;
  String? _workSchedule;
  String? _workStress;
  final TextEditingController _workNotesController = TextEditingController();

  // Card 11: Physical Activity
  String? _sportLevel;
  String? _activityDaysPerWeek;
  String? _activityDurationMinutes;
  final Set<String> _activityTypes = {};
  final TextEditingController _activityOtherController = TextEditingController();
  final TextEditingController _activityNotesController = TextEditingController();

  // --- STAGE 4 ENHANCED STATES ---
  // Card 12: Chronic Conditions & Medical History
  final Set<String> _chronicCategories = {};
  final Map<String, Map<String, String>> _chronicConditionDetails = {};
  final Map<String, TextEditingController> _chronicDiagnosisControllers = {};
  final Map<String, TextEditingController> _chronicWhenControllers = {};
  final Map<String, TextEditingController> _chronicSymptomsControllers = {};
  String? _activeChronicCategory;
  final TextEditingController _chronicNotesController = TextEditingController();

  // Card 13: Recent Illnesses & Health Events
  final Set<String> _recentEvents = {};
  final Map<String, Map<String, String>> _recentEventDetails = {};
  final Map<String, TextEditingController> _recentDetailsControllers = {};
  String? _activeRecentEvent;
  final TextEditingController _recentNotesController = TextEditingController();

  // Card 14: Infectious Disease History
  String? _infectiousHistoryStatus;
  final Set<String> _infectiousDiagnosed = {};
  final TextEditingController _infectiousDiagnosedNotesController = TextEditingController();
  final Set<String> _infectiousTestedNegative = {};
  final TextEditingController _infectiousLastTestedController = TextEditingController();

  // --- STAGE 5 ENHANCED STATES ---
  // Card 15: Family History & Heredity
  String? _familyHistoryStatus;
  final Set<String> _familyRelatives = {};
  final Map<String, Set<String>> _familyRelativeConditions = {};
  final Map<String, TextEditingController> _familyDetailsControllers = {};
  final TextEditingController _familyOtherRelController = TextEditingController();
  String? _activeFamilyRelative;

  // Card 16: Medications, Vitamins & Supplements
  String? _medicationsStatus;
  final Set<String> _medicationCategories = {};
  final Map<String, TextEditingController> _medicationNameControllers = {};
  final Map<String, TextEditingController> _medicationCourseControllers = {};
  final Map<String, Map<String, String>> _medicationDraftArchive = {};
  String? _activeMedicationCategory;

  TextEditingController _getMapController(Map<String, TextEditingController> map, String key) {
    return map.putIfAbsent(key, () => TextEditingController());
  }

  String _clampSummary(String text, {int maxLen = 1000}) {
    if (text.length <= maxLen) return text;
    return '${text.substring(0, maxLen - 3)}...';
  }

  void _clearChronicCategoryControllers(String cat) {
    _chronicDiagnosisControllers[cat]?.clear();
    _chronicWhenControllers[cat]?.clear();
    _chronicSymptomsControllers[cat]?.clear();
  }

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
    if (_customInputController.text.isEmpty && global['age'] != null) {
      _customInputController.text = global['age'].toString();
    }
    if (global['age_confirmed_unusual'] == true) {
      _ageConfirmedUnusual = true;
    }
    if (_pregnancyStatus == null && global['pregnancy_status'] != null) {
      _pregnancyStatus = global['pregnancy_status'].toString();
    }
    if (_pregnancyTrimester == null && global['pregnancy_trimester'] != null) {
      _pregnancyTrimester = global['pregnancy_trimester'].toString();
    }
    if (_pregnancyWeeksController.text.isEmpty && global['pregnancy_weeks'] != null) {
      _pregnancyWeeksController.text = global['pregnancy_weeks'].toString();
    }
    if (_breastfeedingStatus == null && global['breastfeeding_status'] != null) {
      _breastfeedingStatus = global['breastfeeding_status'].toString();
    }
    if (hController.text.isEmpty && global['height'] != null) {
      hController.text = global['height'].toString();
    }
    if (wController.text.isEmpty && global['weight'] != null) {
      wController.text = global['weight'].toString();
    }
    if (global['height_confirmed_unusual'] == true) {
      _heightConfirmedUnusual = true;
    }
    if (global['weight_confirmed_unusual'] == true) {
      _weightConfirmedUnusual = true;
    }
    if (_weightChangeStatus == null && global['weight_change_status'] != null) {
      _weightChangeStatus = global['weight_change_status'].toString();
    }
    if (_weightChangeAmountController.text.isEmpty && global['weight_change_amount'] != null) {
      _weightChangeAmountController.text = global['weight_change_amount'].toString();
    }
    if (_weightChangeIntentional == null && global['weight_change_intentional'] != null) {
      _weightChangeIntentional = global['weight_change_intentional'].toString();
    }
    if (_nicotineStatus == null && global['nicotine_status'] != null) {
      _nicotineStatus = global['nicotine_status'].toString();
    }
    if (_nicotineProducts.isEmpty && global['nicotine_products'] is List) {
      _nicotineProducts.addAll((global['nicotine_products'] as List).map((e) => e.toString()));
    }
    if (_nicotineProductTenure.isEmpty && global['nicotine_product_tenure'] is Map) {
      (global['nicotine_product_tenure'] as Map).forEach((k, v) {
        _nicotineProductTenure[k.toString()] = v.toString();
      });
    }
    if (_nicotineCurrentFrequency == null && global['nicotine_current_frequency'] != null) {
      _nicotineCurrentFrequency = global['nicotine_current_frequency'].toString();
    }
    if (_nicotineQuantities.isEmpty && global['nicotine_quantities'] is Map) {
      _nicotineQuantities.addAll(Map<String, dynamic>.from(global['nicotine_quantities'] as Map));
    }
    if (_nicotineQuitWhen == null && global['nicotine_quit_when'] != null) {
      _nicotineQuitWhen = global['nicotine_quit_when'].toString();
    }
    if (_alcoholStatus == null && global['alcohol_status'] != null) {
      _alcoholStatus = global['alcohol_status'].toString();
    }
    if (_alcoholTypes.isEmpty && global['alcohol_types'] is List) {
      _alcoholTypes.addAll((global['alcohol_types'] as List).map((e) => e.toString()));
    }
    if (_alcoholVolumes.isEmpty && global['alcohol_volumes'] is Map) {
      (global['alcohol_volumes'] as Map).forEach((k, v) {
        _alcoholVolumes[k.toString()] = v.toString();
      });
    }
    if (_alcoholCommentController.text.isEmpty && (global['alcohol_comment'] != null || global['text_alcohol'] != null)) {
      _alcoholCommentController.text = (global['alcohol_comment'] ?? global['text_alcohol']).toString();
    }
    if (_substanceTypes.isEmpty && global['substance_types'] is List) {
      _substanceTypes.addAll((global['substance_types'] as List).map((e) => e.toString()));
    }
    if (_substanceLastUse == null && global['substance_last_use'] != null) {
      _substanceLastUse = global['substance_last_use'].toString();
    }
    if (_substanceNotesController.text.isEmpty && (global['substance_notes'] != null || global['text_spec_factors'] != null)) {
      _substanceNotesController.text = (global['substance_notes'] ?? global['text_spec_factors']).toString();
    }

    // Stage 2: Sleep
    if (_sleepDuration == null && global['sleep_duration'] != null) {
      _sleepDuration = global['sleep_duration'].toString();
    }
    if (_sleepQuality == null && global['sleep_quality'] != null) {
      _sleepQuality = global['sleep_quality'].toString();
    }
    if (_selectedSleepSymptoms.isEmpty && global['sleep_symptoms'] is List) {
      _selectedSleepSymptoms.addAll((global['sleep_symptoms'] as List).map((e) => e.toString()));
    }
    if (_sleepSchedule == null && global['sleep_schedule'] != null) {
      _sleepSchedule = global['sleep_schedule'].toString();
    }
    if (_sleepNotesController.text.isEmpty && (global['sleep_schedule_notes'] != null || global['user_text_sleep'] != null || global['text_sleep'] != null)) {
      _sleepNotesController.text = (global['sleep_schedule_notes'] ?? global['user_text_sleep'] ?? global['text_sleep']).toString();
    }

    // Stage 2: Diet
    if (_dietType == null && global['diet_types'] != null) {
      _dietType = global['diet_types'].toString();
    }
    if (_dietCustomDetailsController.text.isEmpty && global['diet_custom_details'] != null) {
      _dietCustomDetailsController.text = global['diet_custom_details'].toString();
    }
    if (global['diet_excluded_foods_text'] != null && global['diet_excluded_foods_text'].toString().trim().isNotEmpty) {
      _hasExcludedFoods = true;
      _excludedFoodsCollapsed = true;
      if (_excludedFoodsController.text.isEmpty) {
        _excludedFoodsController.text = global['diet_excluded_foods_text'].toString();
      }
    }
    if (_dietMealsPerDay == null && global['diet_meals_per_day'] != null) {
      _dietMealsPerDay = global['diet_meals_per_day'].toString();
    }
    if (_dietSnacksPerDay == null && global['diet_snacks_per_day'] != null) {
      _dietSnacksPerDay = global['diet_snacks_per_day'].toString();
    }
    if (_dietStaples.isEmpty && global['diet_staples'] is List) {
      _dietStaples.addAll((global['diet_staples'] as List).map((e) => e.toString()));
    }
    if (_dietOccasionalFoods.isEmpty && global['diet_occasional_foods'] is List) {
      _dietOccasionalFoods.addAll((global['diet_occasional_foods'] as List).map((e) => e.toString()));
    }
    if (_dietFoodFrequencies.isEmpty && global['diet_food_frequencies'] is Map) {
      (global['diet_food_frequencies'] as Map).forEach((k, v) {
        _dietFoodFrequencies[k.toString()] = v.toString();
      });
    }
    if (_dietDayDescriptionController.text.isEmpty && (global['diet_day_description'] != null || global['text_diet_day'] != null)) {
      _dietDayDescriptionController.text = (global['diet_day_description'] ?? global['text_diet_day']).toString();
    }

    // Stage 2: Drinks
    if (_drinksTotalVolume == null && global['drinks_total_volume'] != null) {
      _drinksTotalVolume = global['drinks_total_volume'].toString();
    }
    if (_drinksTypes.isEmpty && global['drinks_types'] is List) {
      _drinksTypes.addAll((global['drinks_types'] as List).map((e) => e.toString()));
    }
    if (_drinksCoffeeTypes.isEmpty && global['drinks_coffee_types'] is List) {
      _drinksCoffeeTypes.addAll((global['drinks_coffee_types'] as List).map((e) => e.toString()));
      _drinksCoffeeCollapsed = true;
    }
    if (_drinksCoffeeFrequency == null && global['drinks_coffee_frequency'] != null) {
      _drinksCoffeeFrequency = global['drinks_coffee_frequency'].toString();
    }
    if (_drinksCaffeineFrequency == null && global['drinks_caffeine_frequency'] != null) {
      _drinksCaffeineFrequency = global['drinks_caffeine_frequency'].toString();
    }
    if (_drinksDayDescriptionController.text.isEmpty && (global['drinks_day_description'] != null || global['text_drinks'] != null || global['user_text_drinks'] != null)) {
      _drinksDayDescriptionController.text = (global['drinks_day_description'] ?? global['text_drinks'] ?? global['user_text_drinks']).toString();
    }

    // Stage 3: GI
    if (_giSymptoms.isEmpty && global['gi_symptoms'] is List) {
      _giSymptoms.addAll((global['gi_symptoms'] as List).map((e) => e.toString()));
    } else if (_giSymptoms.isEmpty && global['gi_signals'] != null) {
      final sig = global['gi_signals'].toString();
      if (sig.toLowerCase() == 'none' || sig.toLowerCase() == 'gi_ok') {
        _giSymptoms.add('None of the above');
      }
    }
    if (_giSymptomFrequencies.isEmpty && global['gi_symptom_details'] is Map) {
      (global['gi_symptom_details'] as Map).forEach((k, v) {
        if (v is Map) {
          final freq = v['frequency']?.toString();
          final dur = v['duration']?.toString();
          if (freq != null) _giSymptomFrequencies[k.toString()] = freq;
          if (dur != null) _giSymptomDurations[k.toString()] = dur;
        }
      });
    }
    if (_giDetailsController.text.isEmpty && (global['user_text_gi_signals'] != null || global['text_gi_signals'] != null)) {
      _giDetailsController.text = (global['user_text_gi_signals'] ?? global['text_gi_signals']).toString();
    }

    // Stage 3: Work
    if (_workOccupationController.text.isEmpty && global['work_occupation'] != null) {
      _workOccupationController.text = global['work_occupation'].toString();
    }
    if (_workActivity == null && global['work_type'] != null) {
      _workActivity = global['work_type'].toString();
    }
    if (_workSchedule == null && global['work_schedule'] != null) {
      _workSchedule = global['work_schedule'].toString();
    }
    if (_workStress == null && global['work_stress'] != null) {
      _workStress = global['work_stress'].toString();
    }
    if (_workNotesController.text.isEmpty && (global['user_text_work'] != null || global['text_work'] != null)) {
      _workNotesController.text = (global['user_text_work'] ?? global['text_work']).toString();
    }

    // Stage 3: Activity
    if (_sportLevel == null && global['sport_level'] != null) {
      _sportLevel = global['sport_level'].toString();
    }
    if (_activityDaysPerWeek == null && global['activity_days_per_week'] != null) {
      _activityDaysPerWeek = global['activity_days_per_week'].toString();
    }
    if (_activityDurationMinutes == null && global['activity_duration_minutes'] != null) {
      _activityDurationMinutes = global['activity_duration_minutes'].toString();
    }
    if (_activityTypes.isEmpty && global['activity_types'] is List) {
      _activityTypes.addAll((global['activity_types'] as List).map((e) => e.toString()));
    }
    if (_activityOtherController.text.isEmpty && global['activity_other_details'] != null) {
      _activityOtherController.text = global['activity_other_details'].toString();
    }
    if (_activityNotesController.text.isEmpty && (global['user_text_sport'] != null || global['text_sport'] != null)) {
      _activityNotesController.text = (global['user_text_sport'] ?? global['text_sport']).toString();
    }

    // Stage 4: Chronic Conditions
    if (_chronicCategories.isEmpty && global['chronic_categories'] is List) {
      _chronicCategories.addAll((global['chronic_categories'] as List).map((e) => e.toString()));
    }
    if (_chronicConditionDetails.isEmpty && global['chronic_condition_details'] is Map) {
      (global['chronic_condition_details'] as Map).forEach((k, v) {
        if (v is Map) {
          final cat = k.toString();
          final detailMap = <String, String>{};
          v.forEach((ik, iv) => detailMap[ik.toString()] = iv.toString());
          if (cat == 'Cancer or oncological history' &&
              (!detailMap.containsKey('onco_status') || detailMap['onco_status']!.isEmpty) &&
              global['onco_status'] != null) {
            detailMap['onco_status'] = global['onco_status'].toString();
          }
          _chronicConditionDetails[cat] = detailMap;
          if (detailMap['diagnosis'] != null) {
            _getMapController(_chronicDiagnosisControllers, cat).text = detailMap['diagnosis']!;
          }
          if (detailMap['diagnosed_when'] != null) {
            _getMapController(_chronicWhenControllers, cat).text = detailMap['diagnosed_when']!;
          }
          if (detailMap['current_symptoms_text'] != null) {
            _getMapController(_chronicSymptomsControllers, cat).text = detailMap['current_symptoms_text']!;
          }
        }
      });
    }
    if (_chronicNotesController.text.isEmpty && (global['user_text_chronic_diseases'] != null || global['text_chronic_diseases'] != null)) {
      _chronicNotesController.text = (global['user_text_chronic_diseases'] ?? global['text_chronic_diseases']).toString();
    }

    // Stage 4: Recent Events
    if (_recentEvents.isEmpty && global['recent_events'] is List) {
      _recentEvents.addAll((global['recent_events'] as List).map((e) => e.toString()));
    }
    if (_recentEventDetails.isEmpty && global['recent_event_details'] is Map) {
      (global['recent_event_details'] as Map).forEach((k, v) {
        if (v is Map) {
          final ev = k.toString();
          final detailMap = <String, String>{};
          v.forEach((ik, iv) => detailMap[ik.toString()] = iv.toString());
          _recentEventDetails[ev] = detailMap;
          if (detailMap['details'] != null) {
            _getMapController(_recentDetailsControllers, ev).text = detailMap['details']!;
          }
        }
      });
    }
    if (_recentNotesController.text.isEmpty && (global['user_text_recent_illness'] != null || global['text_recent_illness'] != null)) {
      _recentNotesController.text = (global['user_text_recent_illness'] ?? global['text_recent_illness']).toString();
    }

    // Stage 4: Infectious Disease History
    if (_infectiousHistoryStatus == null && global['infectious_history_status'] != null) {
      _infectiousHistoryStatus = global['infectious_history_status'].toString();
    }
    if (_infectiousDiagnosed.isEmpty && global['infectious_diagnosed'] is List) {
      _infectiousDiagnosed.addAll((global['infectious_diagnosed'] as List).map((e) => e.toString()));
    }
    if (_infectiousDiagnosedNotesController.text.isEmpty &&
        (global['user_text_critical_infections'] != null || global['text_critical_infections'] != null)) {
      _infectiousDiagnosedNotesController.text =
          (global['user_text_critical_infections'] ?? global['text_critical_infections']).toString();
    }
    if (_infectiousTestedNegative.isEmpty && global['infectious_tested_negative'] is List) {
      _infectiousTestedNegative.addAll((global['infectious_tested_negative'] as List).map((e) => e.toString()));
    }
    if (_infectiousLastTestedController.text.isEmpty && global['infectious_last_tested'] != null) {
      _infectiousLastTestedController.text = global['infectious_last_tested'].toString();
    }

    // Stage 5: Family History & Heredity
    if (_familyHistoryStatus == null && global['family_history_status'] != null) {
      _familyHistoryStatus = global['family_history_status'].toString();
    }
    if (_familyRelatives.isEmpty && global['family_history_relatives'] is List) {
      _familyRelatives.addAll((global['family_history_relatives'] as List).map((e) => e.toString()));
    }
    if (_familyRelativeConditions.isEmpty && global['family_history_details'] is Map) {
      (global['family_history_details'] as Map).forEach((k, v) {
        if (v is Map) {
          final rel = k.toString();
          if (v['conditions'] is List) {
            _familyRelativeConditions[rel] =
                (v['conditions'] as List).map((e) => e.toString()).toSet();
          }
          if (v['details'] != null) {
            _getMapController(_familyDetailsControllers, rel).text = v['details'].toString();
          }
          if (rel == 'Other blood relatives' && v['relationship_detail'] != null) {
            _familyOtherRelController.text = v['relationship_detail'].toString();
          }
        }
      });
    }

    // Stage 5: Medications, Vitamins & Supplements
    if (_medicationsStatus == null && global['medications_status'] != null) {
      _medicationsStatus = global['medications_status'].toString();
    }
    if (_medicationCategories.isEmpty && global['medication_categories'] is List) {
      _medicationCategories.addAll((global['medication_categories'] as List).map((e) => e.toString()));
    }
    if (global['medication_details'] is Map) {
      (global['medication_details'] as Map).forEach((k, v) {
        if (v is Map) {
          final cat = k.toString();
          final nameCtrl = _getMapController(_medicationNameControllers, cat);
          final courseCtrl = _getMapController(_medicationCourseControllers, cat);
          if (nameCtrl.text.isEmpty && v['names'] != null) {
            nameCtrl.text = v['names'].toString();
          }
          if (courseCtrl.text.isEmpty && v['course'] != null) {
            courseCtrl.text = v['course'].toString();
          }
        }
      });
    }
  }

  void _resetLocalState() {
    _selectedGender = null;
    _multiSelect.clear();
    _customInputController.clear();
    hController.clear();
    wController.clear();
    _ageConfirmedUnusual = false;
    _pregnancyStatus = null;
    _pregnancyTrimester = null;
    _pregnancyWeeksController.clear();
    _breastfeedingStatus = null;
    _heightConfirmedUnusual = false;
    _weightConfirmedUnusual = false;
    _weightChangeStatus = null;
    _weightChangeAmountController.clear();
    _weightChangeIntentional = null;
    _nicotineStatus = null;
    _nicotineProducts.clear();
    _nicotineProductTenure.clear();
    _nicotineCurrentFrequency = null;
    _nicotineQuantities.clear();
    _nicotineQuitWhen = null;
    _nicotineSubStep = 0;
    _alcoholStatus = null;
    _alcoholTypes.clear();
    _alcoholVolumes.clear();
    _alcoholCommentController.clear();
    _alcoholSubStep = 0;
    _substanceTypes.clear();
    _substanceLastUse = null;
    _substanceNotesController.clear();
    _sleepDuration = null;
    _sleepQuality = null;
    _selectedSleepSymptoms.clear();
    _sleepSchedule = null;
    _sleepNotesController.clear();
    _dietType = null;
    _dietCustomDetailsController.clear();
    _hasExcludedFoods = false;
    _excludedFoodsCollapsed = false;
    _excludedFoodsController.clear();
    _dietMealsPerDay = null;
    _dietSnacksPerDay = null;
    _dietStaples.clear();
    _dietOccasionalFoods.clear();
    _dietFoodFrequencies.clear();
    _activeOccasionalFood = null;
    _dietDayDescriptionController.clear();
    _drinksTotalVolume = null;
    _drinksTypes.clear();
    _drinksCoffeeCollapsed = false;
    _drinksCoffeeTypes.clear();
    _drinksCoffeeFrequency = null;
    _drinksCaffeineFrequency = null;
    _drinksDayDescriptionController.clear();
    _giSymptoms.clear();
    _giSymptomFrequencies.clear();
    _giSymptomDurations.clear();
    _activeGiSymptom = null;
    _giDetailsController.clear();
    _workOccupationController.clear();
    _workActivity = null;
    _workSchedule = null;
    _workStress = null;
    _workNotesController.clear();
    _sportLevel = null;
    _activityDaysPerWeek = null;
    _activityDurationMinutes = null;
    _activityTypes.clear();
    _activityOtherController.clear();
    _activityNotesController.clear();
    _chronicCategories.clear();
    _chronicConditionDetails.clear();
    for (final c in _chronicDiagnosisControllers.values) { c.clear(); }
    for (final c in _chronicWhenControllers.values) { c.clear(); }
    for (final c in _chronicSymptomsControllers.values) { c.clear(); }
    _activeChronicCategory = null;
    _chronicNotesController.clear();
    _recentEvents.clear();
    _recentEventDetails.clear();
    for (final c in _recentDetailsControllers.values) { c.clear(); }
    _activeRecentEvent = null;
    _recentNotesController.clear();
    _infectiousHistoryStatus = null;
    _infectiousDiagnosed.clear();
    _infectiousDiagnosedNotesController.clear();
    _infectiousTestedNegative.clear();
    _infectiousLastTestedController.clear();
    _familyHistoryStatus = null;
    _familyRelatives.clear();
    _familyRelativeConditions.clear();
    for (final c in _familyDetailsControllers.values) { c.clear(); }
    _familyOtherRelController.clear();
    _activeFamilyRelative = null;
    _medicationsStatus = null;
    _medicationCategories.clear();
    for (final c in _medicationNameControllers.values) { c.clear(); }
    for (final c in _medicationCourseControllers.values) { c.clear(); }
    _medicationDraftArchive.clear();
    _activeMedicationCategory = null;
    _isSmokingBranch = false;
    _isSmokingFrequencyBranch = false;
    _isAlcoholBranch = false;
    _currentError = null;
    _currentPage = 0;
    if (_controller.hasClients) {
      _controller.jumpToPage(0);
    }
  }

  void _previousPage() {
    if (_currentPage > 0) {
      setState(() {
        _currentError = null;
        _nicotineSubStep = 0;
        _alcoholSubStep = 0;
        _activeOccasionalFood = null;
        _activeGiSymptom = null;
        _activeChronicCategory = null;
        _activeRecentEvent = null;
        _activeFamilyRelative = null;
        _activeMedicationCategory = null;
      });
      _controller.previousPage(
        duration: const Duration(milliseconds: 400),
        curve: Curves.easeInOut,
      );
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
    _pregnancyWeeksController.dispose();
    _weightChangeAmountController.dispose();
    _alcoholCommentController.dispose();
    _substanceNotesController.dispose();
    _sleepNotesController.dispose();
    _dietCustomDetailsController.dispose();
    _excludedFoodsController.dispose();
    _dietDayDescriptionController.dispose();
    _drinksDayDescriptionController.dispose();
    _giDetailsController.dispose();
    _workOccupationController.dispose();
    _workNotesController.dispose();
    _activityOtherController.dispose();
    _activityNotesController.dispose();
    _chronicNotesController.dispose();
    for (final c in _chronicDiagnosisControllers.values) { c.dispose(); }
    for (final c in _chronicWhenControllers.values) { c.dispose(); }
    for (final c in _chronicSymptomsControllers.values) { c.dispose(); }
    _recentNotesController.dispose();
    for (final c in _recentDetailsControllers.values) { c.dispose(); }
    _infectiousDiagnosedNotesController.dispose();
    _infectiousLastTestedController.dispose();
    for (final c in _familyDetailsControllers.values) { c.dispose(); }
    _familyOtherRelController.dispose();
    for (final c in _medicationNameControllers.values) { c.dispose(); }
    for (final c in _medicationCourseControllers.values) { c.dispose(); }
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
            if (_currentPage > 0)
              Align(
                alignment: Alignment.centerLeft,
                child: IconButton(
                  icon: const Icon(Icons.arrow_back, color: Colors.white70),
                  onPressed: _previousPage,
                  tooltip: 'Назад',
                ),
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
                  _stepDrinks(), // 8
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

  Widget _stepBioProfile() {
    final vault = context.read<VaultService>();
    final isUnusualAge = () {
      final val = num.tryParse(_customInputController.text.replaceAll(',', '.').trim());
      return val != null && val >= 100 && val <= 120;
    }();

    return _wrapper(children: [
      _buildTitle("Biological Profile"),
      _buildSubtitle("«Where shall we start? Gender and age determine your baseline health metrics.»"),

      Row(children: [
        Expanded(child: _buildSelectableOption("🧔 Male", _selectedGender == 'm', () {
          setState(() {
            _selectedGender = 'm';
            _pregnancyStatus = null;
            _pregnancyTrimester = null;
            _pregnancyWeeksController.clear();
            _breastfeedingStatus = null;
          });
          vault.removeAnamnesisKeys(['pregnancy_status', 'pregnancy_trimester', 'pregnancy_weeks', 'breastfeeding_status']);
        })),
        const SizedBox(width: 10),
        Expanded(child: _buildSelectableOption("👩 Female", _selectedGender == 'f', () {
          setState(() => _selectedGender = 'f');
        })),
      ]),

      TextField(
        controller: _customInputController,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Age (18-99)",
          labelStyle: TextStyle(color: Colors.white38),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      if (isUnusualAge && !_ageConfirmedUnusual) ...[
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.amber.withOpacity(0.15),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: Colors.amberAccent),
          ),
          child: Column(
            children: [
              Text(
                "Вы указали возраст: ${_customInputController.text.trim()}. Пожалуйста, проверьте правильность ввода.",
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.amberAccent, fontSize: 13),
              ),
              const SizedBox(height: 8),
              OutlinedButton(
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: Colors.amberAccent),
                ),
                onPressed: () => setState(() {
                  _ageConfirmedUnusual = true;
                  _currentError = null;
                }),
                child: const Text("Подтвердить правильность ввода", style: TextStyle(color: Colors.amberAccent)),
              ),
            ],
          ),
        ),
      ],

      if (_selectedGender == 'f') ...[
        const SizedBox(height: 24),
        const Text("Беременны ли вы сейчас?", style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 16)),
        const SizedBox(height: 8),
        _buildSelectableOption("Да", _pregnancyStatus == 'Да', () {
          setState(() => _pregnancyStatus = 'Да');
        }),
        _buildSelectableOption("Возможно, но не подтверждено", _pregnancyStatus == 'Возможно, но не подтверждено', () {
          setState(() => _pregnancyStatus = 'Возможно, но не подтверждено');
        }),
        _buildSelectableOption("Нет", _pregnancyStatus == 'Нет', () {
          setState(() {
            _pregnancyStatus = 'Нет';
            _pregnancyTrimester = null;
            _pregnancyWeeksController.clear();
          });
          vault.removeAnamnesisKeys(['pregnancy_trimester', 'pregnancy_weeks']);
        }),
        _buildSelectableOption("Предпочитаю не отвечать", _pregnancyStatus == 'Предпочитаю не отвечать', () {
          setState(() {
            _pregnancyStatus = 'Предпочитаю не отвечать';
            _pregnancyTrimester = null;
            _pregnancyWeeksController.clear();
          });
          vault.removeAnamnesisKeys(['pregnancy_trimester', 'pregnancy_weeks']);
        }),

        if (_pregnancyStatus == 'Да' || _pregnancyStatus == 'Возможно, но не подтверждено') ...[
          const SizedBox(height: 12),
          const Text("Триместр беременности:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.w600)),
          const SizedBox(height: 6),
          _buildSelectableOption("1-й триместр (до 13 нед. 6 дн.)", _pregnancyTrimester == '1st trimester', () {
            setState(() => _pregnancyTrimester = '1st trimester');
          }),
          _buildSelectableOption("2-й триместр (14–27 нед. 6 дн.)", _pregnancyTrimester == '2nd trimester', () {
            setState(() => _pregnancyTrimester = '2nd trimester');
          }),
          _buildSelectableOption("3-й триместр (28 нед. и более)", _pregnancyTrimester == '3rd trimester', () {
            setState(() => _pregnancyTrimester = '3rd trimester');
          }),
          _buildSelectableOption("Не знаю / не уверена", _pregnancyTrimester == 'Unknown', () {
            setState(() => _pregnancyTrimester = 'Unknown');
          }),
          TextField(
            controller: _pregnancyWeeksController,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            style: const TextStyle(color: Colors.white),
            decoration: const InputDecoration(
              labelText: "Срок беременности (в неделях, 1–45, необязательно)",
              labelStyle: TextStyle(color: Colors.white38),
              enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
            ),
          ),
        ],

        const SizedBox(height: 20),
        const Text("Кормите ли вы грудью?", style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 16)),
        const SizedBox(height: 8),
        _buildSelectableOption("Да", _breastfeedingStatus == 'Yes', () {
          setState(() => _breastfeedingStatus = 'Yes');
        }),
        _buildSelectableOption("Нет", _breastfeedingStatus == 'No', () {
          setState(() => _breastfeedingStatus = 'No');
        }),
        _buildSelectableOption("Предпочитаю не отвечать", _breastfeedingStatus == 'Prefer not to say', () {
          setState(() => _breastfeedingStatus = 'Prefer not to say');
        }),
      ],

      const SizedBox(height: 20),

      _buildSelectableOption("Next", false, () {
        FocusScope.of(context).unfocus();
        final String ageInput = _customInputController.text.trim();
        final String ageNorm = ageInput.replaceAll(',', '.');
        final num? ageNum = num.tryParse(ageNorm);

        if (_selectedGender == null) {
          setState(() => _currentError = "Please select your gender");
          return;
        }

        if (ageInput.isEmpty) {
          setState(() => _currentError = "Please enter your age");
          return;
        }

        if (ageNum == null || ageNum != ageNum.toInt() || ageNum < 18 || ageNum > 120) {
          setState(() => _currentError = "Age must be an integer between 18 and 120 years");
          return;
        }

        final int age = ageNum.toInt();
        if (age >= 100 && age <= 120 && !_ageConfirmedUnusual) {
          setState(() => _currentError = "Вы указали возраст: $age. Пожалуйста, проверьте правильность ввода.");
          return;
        }

        if (_selectedGender == 'f') {
          if (_pregnancyStatus == null) {
            setState(() => _currentError = "Пожалуйста, укажите статус беременности");
            return;
          }
          if (_pregnancyStatus == 'Да' || _pregnancyStatus == 'Возможно, но не подтверждено') {
            if (_pregnancyTrimester == null) {
              setState(() => _currentError = "Пожалуйста, укажите триместр беременности");
              return;
            }
            if (_pregnancyWeeksController.text.trim().isNotEmpty) {
              final w = num.tryParse(_pregnancyWeeksController.text.replaceAll(',', '.').trim());
              if (w == null || w < 1 || w > 45) {
                setState(() => _currentError = "Срок беременности должен быть от 1 до 45 недель");
                return;
              }
              if (_pregnancyTrimester == '1st trimester' && w > 13) {
                setState(() => _currentError = "Указанный срок ($w нед.) не соответствует 1-му триместру (до 13 нед. 6 дн.)");
                return;
              }
              if (_pregnancyTrimester == '2nd trimester' && (w < 14 || w > 27)) {
                setState(() => _currentError = "Указанный срок ($w нед.) не соответствует 2-му триместру (14–27 нед. 6 дн.)");
                return;
              }
              if (_pregnancyTrimester == '3rd trimester' && w < 28) {
                setState(() => _currentError = "Указанный срок ($w нед.) не соответствует 3-му триместру (28 нед. и более)");
                return;
              }
            }
          }
          if (_breastfeedingStatus == null) {
            setState(() => _currentError = "Пожалуйста, укажите статус грудного вскармливания");
            return;
          }
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('gender', _selectedGender == 'm' ? 'Male' : 'Female');
        vault.saveAnamnesis('age', age.toString());
        if (_ageConfirmedUnusual) {
          vault.saveAnamnesis('age_confirmed_unusual', true);
        }
        if (_selectedGender == 'f') {
          vault.saveAnamnesis('pregnancy_status', _pregnancyStatus);
          if (_pregnancyTrimester != null) {
            vault.saveAnamnesis('pregnancy_trimester', _pregnancyTrimester);
          }
          if (_pregnancyWeeksController.text.trim().isNotEmpty) {
            vault.saveAnamnesis('pregnancy_weeks', num.parse(_pregnancyWeeksController.text.replaceAll(',', '.').trim()));
          }
          vault.saveAnamnesis('breastfeeding_status', _breastfeedingStatus);
        } else {
          vault.removeAnamnesisKeys(['pregnancy_status', 'pregnancy_trimester', 'pregnancy_weeks', 'breastfeeding_status']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepDimensions() {
    final vault = context.read<VaultService>();

    final isUnusualHeight = () {
      final h = num.tryParse(hController.text.replaceAll(',', '.').trim());
      return h != null && ((h >= 50 && h < 100) || (h > 220 && h <= 260));
    }();

    final isUnusualWeight = () {
      final w = num.tryParse(wController.text.replaceAll(',', '.').trim());
      return w != null && ((w >= 20 && w < 30) || (w > 250 && w <= 500));
    }();

    return _wrapper(children: [
      _buildTitle("Your Dimensions"),
      _buildSubtitle("«Your height and weight are needed to calculate metabolic load.»"),

      TextField(
        controller: hController,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Height (cm)",
          labelStyle: TextStyle(color: Colors.white38),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      if (isUnusualHeight && !_heightConfirmedUnusual) ...[
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: Colors.amber.withOpacity(0.15),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: Colors.amberAccent),
          ),
          child: Column(
            children: [
              Text(
                "Вы указали рост: ${hController.text.trim()} см. Пожалуйста, проверьте правильность ввода.",
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.amberAccent, fontSize: 13),
              ),
              const SizedBox(height: 6),
              OutlinedButton(
                style: OutlinedButton.styleFrom(side: const BorderSide(color: Colors.amberAccent)),
                onPressed: () => setState(() {
                  _heightConfirmedUnusual = true;
                  _currentError = null;
                }),
                child: const Text("Подтвердить рост", style: TextStyle(color: Colors.amberAccent)),
              ),
            ],
          ),
        ),
      ],

      TextField(
        controller: wController,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Weight (kg)",
          labelStyle: TextStyle(color: Colors.white38),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      if (isUnusualWeight && !_weightConfirmedUnusual) ...[
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: Colors.amber.withOpacity(0.15),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: Colors.amberAccent),
          ),
          child: Column(
            children: [
              Text(
                "Вы указали вес: ${wController.text.trim()} кг. Пожалуйста, проверьте правильность ввода.",
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.amberAccent, fontSize: 13),
              ),
              const SizedBox(height: 6),
              OutlinedButton(
                style: OutlinedButton.styleFrom(side: const BorderSide(color: Colors.amberAccent)),
                onPressed: () => setState(() {
                  _weightConfirmedUnusual = true;
                  _currentError = null;
                }),
                child: const Text("Подтвердить вес", style: TextStyle(color: Colors.amberAccent)),
              ),
            ],
          ),
        ),
      ],

      const SizedBox(height: 20),
      const Text("Изменился ли ваш вес за последние 3–6 месяцев?", style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 16)),
      const SizedBox(height: 8),

      _buildSelectableOption("Вес примерно одинаковый", _weightChangeStatus == 'Stable', () {
        setState(() {
          _weightChangeStatus = 'Stable';
          _weightChangeAmountController.clear();
          _weightChangeIntentional = null;
        });
        vault.removeAnamnesisKeys(['weight_change_amount', 'weight_change_intentional']);
      }),
      _buildSelectableOption("Набрал(а) вес", _weightChangeStatus == 'Gained', () {
        setState(() => _weightChangeStatus = 'Gained');
      }),
      _buildSelectableOption("Потерял(а) вес", _weightChangeStatus == 'Lost', () {
        setState(() => _weightChangeStatus = 'Lost');
      }),
      _buildSelectableOption("Не знаю / не отслеживаю", _weightChangeStatus == 'Unknown', () {
        setState(() {
          _weightChangeStatus = 'Unknown';
          _weightChangeAmountController.clear();
          _weightChangeIntentional = null;
        });
        vault.removeAnamnesisKeys(['weight_change_amount', 'weight_change_intentional']);
      }),

      if (_weightChangeStatus == 'Gained' || _weightChangeStatus == 'Lost') ...[
        TextField(
          controller: _weightChangeAmountController,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          style: const TextStyle(color: Colors.white),
          decoration: const InputDecoration(
            labelText: "На сколько килограммов? (например, 5)",
            labelStyle: TextStyle(color: Colors.white38),
            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
          ),
        ),
        const SizedBox(height: 12),
        const Text("Это изменение было намеренным?", style: TextStyle(color: Colors.white70, fontSize: 14)),
        const SizedBox(height: 6),
        Row(
          children: [
            Expanded(child: _buildSelectableOption("Да", _weightChangeIntentional == 'Yes', () => setState(() => _weightChangeIntentional = 'Yes'))),
            const SizedBox(width: 6),
            Expanded(child: _buildSelectableOption("Нет", _weightChangeIntentional == 'No', () => setState(() => _weightChangeIntentional = 'No'))),
            const SizedBox(width: 6),
            Expanded(child: _buildSelectableOption("Не уверен(а)", _weightChangeIntentional == 'Not sure', () => setState(() => _weightChangeIntentional = 'Not sure'))),
          ],
        ),
      ],

      const SizedBox(height: 20),

      _buildSelectableOption("Next", false, () {
        FocusScope.of(context).unfocus();

        final String hInput = hController.text.trim();
        final String wInput = wController.text.trim();
        final num? height = num.tryParse(hInput.replaceAll(',', '.'));
        final num? weight = num.tryParse(wInput.replaceAll(',', '.'));

        if (hInput.isEmpty || height == null || height < 50 || height > 260) {
          setState(() => _currentError = "Please enter a valid height (50-260 cm)");
          return;
        }

        if (wInput.isEmpty || weight == null || weight < 20 || weight > 500) {
          setState(() => _currentError = "Please enter a valid weight (20-500 kg)");
          return;
        }

        if (((height >= 50 && height < 100) || (height > 220 && height <= 260)) && !_heightConfirmedUnusual) {
          setState(() => _currentError = "Вы указали рост: $height см. Пожалуйста, проверьте правильность ввода.");
          return;
        }

        if (((weight >= 20 && weight < 30) || (weight > 250 && weight <= 500)) && !_weightConfirmedUnusual) {
          setState(() => _currentError = "Вы указали вес: $weight кг. Пожалуйста, проверьте правильность ввода.");
          return;
        }

        if (_weightChangeStatus == 'Gained' || _weightChangeStatus == 'Lost') {
          final amtStr = _weightChangeAmountController.text.trim();
          final amt = num.tryParse(amtStr.replaceAll(',', '.'));
          if (amtStr.isEmpty || amt == null || amt <= 0) {
            setState(() => _currentError = "Укажите положительное количество килограммов изменения веса");
            return;
          }
          if (_weightChangeStatus == 'Lost' && amt >= weight) {
            setState(() => _currentError = "Потеря веса ($amt кг) не может превышать текущий вес ($weight кг)");
            return;
          }
          if (_weightChangeIntentional == null) {
            setState(() => _currentError = "Пожалуйста, укажите, было ли изменение веса намеренным");
            return;
          }
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('height', height.toString());
        vault.saveAnamnesis('weight', weight.toString());
        if (_heightConfirmedUnusual) vault.saveAnamnesis('height_confirmed_unusual', true);
        if (_weightConfirmedUnusual) vault.saveAnamnesis('weight_confirmed_unusual', true);

        if (_weightChangeStatus != null) {
          vault.saveAnamnesis('weight_change_status', _weightChangeStatus);
          if (_weightChangeStatus == 'Gained' || _weightChangeStatus == 'Lost') {
            final amt = num.parse(_weightChangeAmountController.text.replaceAll(',', '.').trim());
            vault.saveAnamnesis('weight_change_amount', amt);
            vault.saveAnamnesis('weight_change_intentional', _weightChangeIntentional);
          } else {
            vault.removeAnamnesisKeys(['weight_change_amount', 'weight_change_intentional']);
          }
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepNicotine() {
    final vault = context.read<VaultService>();

    const productOptions = [
      'Обычные сигареты',
      'Вейп / электронные сигареты',
      'Нагреваемый табак (IQOS и аналоги)',
      'Кальян',
      'Другие ингаляционные продукты',
    ];

    const tenureOptions = [
      'Менее 1 года',
      '1–3 года',
      '3–5 лет',
      '5–10 лет',
      'Более 10 лет',
    ];

    const freqOptions = [
      'Реже 1 дня в месяц',
      '1–3 дня в месяц',
      '1–3 дня в неделю',
      '4–6 дней в неделю',
      'Ежедневно',
    ];

    const quitOptions = [
      'Менее 1 месяца назад',
      '1–6 месяцев назад',
      '6–12 месяцев назад',
      '1–5 лет назад',
      'Более 5 лет назад',
    ];

    return _wrapper(children: [
      _buildTitle("Nicotine 🚬"),
      _buildSubtitle("«Nicotine interaction directly affects blood viscosity.»"),

      if (_nicotineSubStep == 0) ...[
        _buildSelectableOption("1️⃣ Никогда не употреблял(а)", _nicotineStatus == 'Never', () {
          setState(() {
            _nicotineStatus = 'Never';
            _nicotineProducts.clear();
            _nicotineProductTenure.clear();
            _nicotineCurrentFrequency = null;
            _nicotineQuantities.clear();
            _nicotineQuitWhen = null;
          });
          vault.saveAnamnesis('nicotine_status', 'Non-user');
          vault.removeAnamnesisKeys(['nicotine_products', 'nicotine_product_tenure', 'nicotine_current_frequency', 'nicotine_quantities', 'nicotine_quit_when', 'nicotine_years', 'nicotine_freq']);
          _nextPage();
        }),
        _buildSelectableOption("2️⃣ Иногда, от случая к случаю", _nicotineStatus == 'Occasional', () {
          setState(() {
            _nicotineStatus = 'Occasional';
            _nicotineSubStep = 1;
          });
        }),
        _buildSelectableOption("3️⃣ Регулярно", _nicotineStatus == 'Regular', () {
          setState(() {
            _nicotineStatus = 'Regular';
            _nicotineSubStep = 1;
          });
        }),
        _buildSelectableOption("4️⃣ Употреблял(а) раньше, сейчас нет", _nicotineStatus == 'Former', () {
          setState(() {
            _nicotineStatus = 'Former';
            _nicotineSubStep = 1;
          });
        }),
        _buildSelectableOption("5️⃣ Предпочитаю не отвечать", _nicotineStatus == 'Prefer not to say', () {
          setState(() {
            _nicotineStatus = 'Prefer not to say';
            _nicotineProducts.clear();
            _nicotineProductTenure.clear();
            _nicotineCurrentFrequency = null;
            _nicotineQuantities.clear();
            _nicotineQuitWhen = null;
          });
          vault.saveAnamnesis('nicotine_status', 'Prefer not to say');
          vault.removeAnamnesisKeys(['nicotine_products', 'nicotine_product_tenure', 'nicotine_current_frequency', 'nicotine_quantities', 'nicotine_quit_when', 'nicotine_years', 'nicotine_freq']);
          _nextPage();
        }),
      ] else ...[
        const Text("Выберите продукты:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 16)),
        const SizedBox(height: 8),
        ...productOptions.map((prod) {
          final isSel = _nicotineProducts.contains(prod);
          return _buildSelectableOption(prod, isSel, () {
            setState(() {
              if (isSel) {
                _nicotineProducts.remove(prod);
                _nicotineProductTenure.remove(prod);
                _nicotineQuantities.remove(prod);
              } else {
                _nicotineProducts.add(prod);
              }
            });
          });
        }),

        if (_nicotineProducts.isNotEmpty) ...[
          const SizedBox(height: 16),
          const Text("Стаж употребления для выбранных продуктов:", style: TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          ..._nicotineProducts.map((prod) {
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text("• $prod:", style: const TextStyle(color: Colors.white70, fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: tenureOptions.map((t) {
                    final isTenureSel = _nicotineProductTenure[prod] == t;
                    return ChoiceChip(
                      label: Text(t, style: TextStyle(color: isTenureSel ? Colors.black : Colors.white, fontSize: 12)),
                      selected: isTenureSel,
                      selectedColor: Colors.greenAccent,
                      backgroundColor: Colors.white12,
                      onSelected: (val) => setState(() => _nicotineProductTenure[prod] = t),
                    );
                  }).toList(),
                ),
                const SizedBox(height: 10),
              ],
            );
          }),
        ],

        if (_nicotineStatus == 'Occasional' || _nicotineStatus == 'Regular') ...[
          const SizedBox(height: 12),
          const Text("Частота употребления:", style: TextStyle(color: Colors.blueAccent, fontWeight: FontWeight.bold)),
          const SizedBox(height: 6),
          ...freqOptions.map((f) => _buildSelectableOption(f, _nicotineCurrentFrequency == f, () {
            setState(() => _nicotineCurrentFrequency = f);
          })),

          if (_nicotineProducts.contains('Обычные сигареты')) ...[
            const SizedBox(height: 6),
            const Text("Сигарет в день:", style: TextStyle(color: Colors.white70)),
            Wrap(
              spacing: 6,
              children: ['1–5', '6–10', '11–20', '20+'].map((q) {
                final isQ = _nicotineQuantities['cigarettes_per_day'] == q;
                return ChoiceChip(
                  label: Text(q, style: TextStyle(color: isQ ? Colors.black : Colors.white, fontSize: 12)),
                  selected: isQ,
                  selectedColor: Colors.greenAccent,
                  backgroundColor: Colors.white12,
                  onSelected: (val) => setState(() => _nicotineQuantities['cigarettes_per_day'] = q),
                );
              }).toList(),
            ),
          ],
          if (_nicotineProducts.contains('Нагреваемый табак (IQOS и аналоги)')) ...[
            const SizedBox(height: 6),
            const Text("Стиков в день:", style: TextStyle(color: Colors.white70)),
            Wrap(
              spacing: 6,
              children: ['1–5', '6–10', '11–20', '20+'].map((q) {
                final isQ = _nicotineQuantities['sticks_per_day'] == q;
                return ChoiceChip(
                  label: Text(q, style: TextStyle(color: isQ ? Colors.black : Colors.white, fontSize: 12)),
                  selected: isQ,
                  selectedColor: Colors.greenAccent,
                  backgroundColor: Colors.white12,
                  onSelected: (val) => setState(() => _nicotineQuantities['sticks_per_day'] = q),
                );
              }).toList(),
            ),
          ],
          if (_nicotineProducts.contains('Вейп / электронные сигареты')) ...[
            const SizedBox(height: 6),
            const Text("Тип никотина в вейпе:", style: TextStyle(color: Colors.white70)),
            Wrap(
              spacing: 6,
              children: ['Солевой никотин', 'Щелочной никотин', 'Без никотина', 'Не знаю'].map((q) {
                final isQ = _nicotineQuantities['vape_nicotine_type'] == q;
                return ChoiceChip(
                  label: Text(q, style: TextStyle(color: isQ ? Colors.black : Colors.white, fontSize: 12)),
                  selected: isQ,
                  selectedColor: Colors.greenAccent,
                  backgroundColor: Colors.white12,
                  onSelected: (val) => setState(() => _nicotineQuantities['vape_nicotine_type'] = q),
                );
              }).toList(),
            ),
          ],
        ],

        if (_nicotineStatus == 'Former') ...[
          const SizedBox(height: 12),
          const Text("Когда вы бросили?", style: TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold)),
          const SizedBox(height: 6),
          ...quitOptions.map((q) => _buildSelectableOption(q, _nicotineQuitWhen == q, () {
            setState(() => _nicotineQuitWhen = q);
          })),
        ],

        const SizedBox(height: 20),

        _buildSelectableOption("Далее ➡️", false, () {
          if (_nicotineProducts.isEmpty) {
            setState(() => _currentError = "Пожалуйста, выберите хотя бы один продукт");
            return;
          }
          for (final prod in _nicotineProducts) {
            if (_nicotineProductTenure[prod] == null) {
              setState(() => _currentError = "Укажите стаж для «$prod»");
              return;
            }
          }
          if ((_nicotineStatus == 'Occasional' || _nicotineStatus == 'Regular') && _nicotineCurrentFrequency == null) {
            setState(() => _currentError = "Пожалуйста, выберите частоту употребления");
            return;
          }
          if (_nicotineStatus == 'Former' && _nicotineQuitWhen == null) {
            setState(() => _currentError = "Пожалуйста, укажите, когда вы бросили");
            return;
          }

          setState(() => _currentError = null);

          final legacyStatus = _nicotineStatus == 'Former' ? 'Quit' : 'Active';
          vault.saveAnamnesis('nicotine_status', legacyStatus);
          vault.saveAnamnesis('nicotine_products', _nicotineProducts.toList());
          vault.saveAnamnesis('nicotine_product_tenure', _nicotineProductTenure);
          if (_nicotineCurrentFrequency != null) {
            vault.saveAnamnesis('nicotine_current_frequency', _nicotineCurrentFrequency);
            vault.saveAnamnesis('nicotine_freq', _nicotineCurrentFrequency);
          }
          if (_nicotineQuantities.isNotEmpty) {
            vault.saveAnamnesis('nicotine_quantities', _nicotineQuantities);
          }
          if (_nicotineQuitWhen != null) {
            vault.saveAnamnesis('nicotine_quit_when', _nicotineQuitWhen);
          }
          if (_nicotineProductTenure.isNotEmpty) {
            vault.saveAnamnesis('nicotine_years', _nicotineProductTenure.values.first);
          }

          _nextPage();
        }),
      ],
    ]);
  }

  Widget _stepAlcohol() {
    final vault = context.read<VaultService>();

    const alcoholTypesList = [
      'Пиво',
      'Вино',
      'Игристое вино / шампанское',
      'Коктейли, сидр',
      'Алкогольные энергетики',
      'Крепкие напитки (40% и выше)',
      'Крепкие напитки до 40%',
      'Другое',
    ];

    final volumePresets = {
      'Пиво': ['0.33 л', '0.5 л', '1 л', '1.5 л+'],
      'Вино': ['150 мл (бокал)', '300 мл', '750 мл (бутылка)'],
      'Игристое вино / шампанское': ['150 мл (бокал)', '300 мл', '750 мл (бутылка)'],
      'Коктейли, сидр': ['200 мл', '400 мл', '600 мл+'],
      'Алкогольные энергетики': ['250 мл', '500 мл', '1 л+'],
      'Крепкие напитки (40% и выше)': ['50 мл (шот)', '100 мл', '200 мл', '500 мл+'],
      'Крепкие напитки до 40%': ['50 мл (шот)', '100 мл', '200 мл', '500 мл+'],
      'Другое': ['100 мл', '250 мл', '500 мл+'],
    };

    return _wrapper(children: [
      _buildTitle("Alcohol 🍷"),
      _buildSubtitle("«Frequency helps distinguish temporary noise from systemic issues.»"),

      if (_alcoholSubStep == 0) ...[
        _buildSelectableOption("1️⃣ Не употребляю", _alcoholStatus == 'Non-drinker', () {
          setState(() {
            _alcoholStatus = 'Non-drinker';
            _alcoholTypes.clear();
            _alcoholVolumes.clear();
            _alcoholCommentController.clear();
          });
          vault.saveAnamnesis('alcohol_status', 'Non-drinker');
          vault.removeAnamnesisKeys(['alcohol_types', 'alcohol_volumes', 'alcohol_comment', 'alcohol_type', 'user_text_alcohol']);
          _nextPage();
        }),
        _buildSelectableOption("2️⃣ Реже одного раза в месяц", _alcoholStatus == 'Rarely', () {
          setState(() {
            _alcoholStatus = 'Rarely';
            _alcoholSubStep = 1;
          });
        }),
        _buildSelectableOption("3️⃣ 1–3 раза в месяц", _alcoholStatus == '1-3/month', () {
          setState(() {
            _alcoholStatus = '1-3/month';
            _alcoholSubStep = 1;
          });
        }),
        _buildSelectableOption("4️⃣ 1–2 раза в неделю", _alcoholStatus == '1-2/week', () {
          setState(() {
            _alcoholStatus = '1-2/week';
            _alcoholSubStep = 1;
          });
        }),
        _buildSelectableOption("5️⃣ 3 раза в неделю или чаще", _alcoholStatus == '>=3/week', () {
          setState(() {
            _alcoholStatus = '>=3/week';
            _alcoholSubStep = 1;
          });
        }),
        _buildSelectableOption("6️⃣ Предпочитаю не отвечать", _alcoholStatus == 'Prefer not to say', () {
          setState(() {
            _alcoholStatus = 'Prefer not to say';
            _alcoholTypes.clear();
            _alcoholVolumes.clear();
            _alcoholCommentController.clear();
          });
          vault.saveAnamnesis('alcohol_status', 'Prefer not to say');
          vault.removeAnamnesisKeys(['alcohol_types', 'alcohol_volumes', 'alcohol_comment', 'alcohol_type', 'user_text_alcohol']);
          _nextPage();
        }),
      ] else ...[
        const Text("Что вы обычно употребляете?", style: TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 16)),
        const SizedBox(height: 8),

        ...alcoholTypesList.map((type) {
          final isSel = _alcoholTypes.contains(type);
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _buildSelectableOption(type, isSel, () {
                setState(() {
                  if (isSel) {
                    _alcoholTypes.remove(type);
                    _alcoholVolumes.remove(type);
                  } else {
                    _alcoholTypes.add(type);
                  }
                });
              }),
              if (isSel) ...[
                Padding(
                  padding: const EdgeInsets.only(left: 8, bottom: 8),
                  child: Wrap(
                    spacing: 6,
                    runSpacing: 4,
                    children: (volumePresets[type] ?? ['100 мл', '250 мл', '500 мл']).map((vol) {
                      final isVol = _alcoholVolumes[type] == vol;
                      return ChoiceChip(
                        label: Text(vol, style: TextStyle(color: isVol ? Colors.black : Colors.white, fontSize: 12)),
                        selected: isVol,
                        selectedColor: Colors.orangeAccent,
                        backgroundColor: Colors.white12,
                        onSelected: (val) => setState(() => _alcoholVolumes[type] = vol),
                      );
                    }).toList(),
                  ),
                ),
              ],
            ],
          );
        }),

        const SizedBox(height: 12),
        TextField(
          controller: _alcoholCommentController,
          style: const TextStyle(color: Colors.white),
          decoration: const InputDecoration(
            labelText: "Пояснение или комментарий (по желанию)",
            labelStyle: TextStyle(color: Colors.white38),
            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
          ),
        ),

        const SizedBox(height: 20),

        _buildSelectableOption("Далее ➡️", false, () {
          if (_alcoholTypes.isEmpty) {
            setState(() => _currentError = "Пожалуйста, выберите хотя бы один тип напитка");
            return;
          }

          setState(() => _currentError = null);

          final legacyStatus = (_alcoholStatus == 'Rarely') ? 'Rarely' : 'Frequently';
          vault.saveAnamnesis('alcohol_status', legacyStatus);
          vault.saveAnamnesis('alcohol_types', _alcoholTypes.toList());
          vault.saveAnamnesis('alcohol_volumes', _alcoholVolumes);
          vault.saveAnamnesis('alcohol_type', _alcoholTypes.first);

          final comment = _alcoholCommentController.text.trim();
          if (comment.isNotEmpty) {
            vault.saveAnamnesis('alcohol_comment', comment, userText: comment);
            vault.saveAnamnesis('user_text_alcohol', comment);
          } else {
            vault.removeAnamnesisKeys(['alcohol_comment', 'user_text_alcohol']);
          }

          _nextPage();
        }),
      ],
    ]);
  }

  Widget _stepSpecFactors() {
    final vault = context.read<VaultService>();

    const substanceOptions = [
      'Каннабис / марихуана',
      'Стимулирующие вещества',
      'Седативные вещества без назначения врача',
      'Опиоиды без назначения врача',
      'Другие психоактивные вещества',
      'Употреблял(а) ранее, сейчас нет',
    ];

    const lastUseOptions = [
      'Менее 7 дней назад',
      'От 7 дней до 1 месяца назад',
      'Более 1 месяца назад',
      'Не хочу уточнять',
    ];

    final hasSubstanceSelected = _substanceTypes.any((s) => substanceOptions.contains(s));

    return _wrapper(children: [
      _buildTitle("Дополнительные факторы здоровья 🛡️"),
      _buildSubtitle("«Некоторые вещества могут влиять на сон, пульс, давление и результаты анализов. Вы можете пропустить этот вопрос»"),

      ...substanceOptions.map((sub) {
        final isSel = _substanceTypes.contains(sub);
        return _buildSelectableOption(sub, isSel, () {
          setState(() {
            _substanceTypes.remove('Не употребляю');
            _substanceTypes.remove('Предпочитаю не отвечать');
            if (isSel) {
              _substanceTypes.remove(sub);
            } else {
              _substanceTypes.add(sub);
            }
          });
        });
      }),

      _buildSelectableOption("🚫 Не употребляю", _substanceTypes.contains('Не употребляю'), () {
        setState(() {
          _substanceTypes.clear();
          _substanceTypes.add('Не употребляю');
          _substanceLastUse = null;
          _substanceNotesController.clear();
        });
        vault.removeAnamnesisKeys(['substance_last_use', 'substance_notes']);
      }),

      _buildSelectableOption("🤐 Предпочитаю не отвечать", _substanceTypes.contains('Предпочитаю не отвечать'), () {
        setState(() {
          _substanceTypes.clear();
          _substanceTypes.add('Предпочитаю не отвечать');
          _substanceLastUse = null;
          _substanceNotesController.clear();
        });
        vault.removeAnamnesisKeys(['substance_last_use', 'substance_notes']);
      }),

      if (hasSubstanceSelected) ...[
        const SizedBox(height: 16),
        const Text("Когда это было в последний раз?", style: TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold)),
        const SizedBox(height: 8),
        ...lastUseOptions.map((opt) => _buildSelectableOption(opt, _substanceLastUse == opt, () {
          setState(() => _substanceLastUse = opt);
        })),

        const SizedBox(height: 10),
        TextField(
          controller: _substanceNotesController,
          style: const TextStyle(color: Colors.white),
          decoration: const InputDecoration(
            labelText: "Заметка или комментарий (по желанию)",
            labelStyle: TextStyle(color: Colors.white38),
            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
          ),
        ),
      ],

      const SizedBox(height: 20),

      _buildSelectableOption("Continue ➡️", false, () {
        if (_substanceTypes.isEmpty) {
          setState(() => _currentError = "Пожалуйста, выберите вариант или нажмите «Не употребляю» / «Предпочитаю не отвечать»");
          return;
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('substance_types', _substanceTypes.toList());
        vault.saveAnamnesis('spec_factors', _substanceTypes.join(', '));

        if (hasSubstanceSelected) {
          if (_substanceLastUse != null) {
            vault.saveAnamnesis('substance_last_use', _substanceLastUse);
          }
          final note = _substanceNotesController.text.trim();
          if (note.isNotEmpty) {
            vault.saveAnamnesis('substance_notes', note, userText: note);
          } else {
            vault.removeAnamnesisKeys(['substance_notes']);
          }
        } else {
          vault.removeAnamnesisKeys(['substance_last_use', 'substance_notes']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepSleep() {
    final vault = context.read<VaultService>();

    const durationOptions = [
      '< 5 hours',
      '5–6 hours',
      '7–8 hours',
      '8–9 hours',
      '> 9 hours',
    ];

    const qualityOptions = [
      'Great',
      'Schedule off',
      'Insomnia',
    ];

    const symptomOptions = [
      'Difficulty falling asleep',
      'Frequent nighttime awakenings',
      'Early morning awakening',
      'Daytime sleepiness / fatigue',
      'Snoring or breathing pauses',
      'None of the above',
    ];

    const scheduleOptions = [
      'Regular schedule',
      'Shift work / Night shifts',
      'Irregular / Jet lag',
    ];

    return _wrapper(children: [
      _buildTitle("Sleep 🌙"),
      _buildSubtitle("«Restorative sleep is essential for hormonal balance, cellular repair, and cardiovascular health.»"),

      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Average sleep duration per night:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: durationOptions.map((opt) {
          final isSel = _sleepDuration == opt;
          return ChoiceChip(
            label: Text(opt, style: TextStyle(color: isSel ? Colors.black : Colors.white, fontSize: 13)),
            selected: isSel,
            selectedColor: Colors.greenAccent,
            backgroundColor: Colors.white12,
            onSelected: (val) => setState(() => _sleepDuration = opt),
          );
        }).toList(),
      ),

      const SizedBox(height: 20),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Overall sleep quality:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...qualityOptions.map((q) => _buildSelectableOption(q, _sleepQuality == q, () {
        setState(() => _sleepQuality = q);
      })),

      const SizedBox(height: 16),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Sleep symptoms / disturbances:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...symptomOptions.map((sym) {
        final isSel = _selectedSleepSymptoms.contains(sym);
        return _buildSelectableOption(sym, isSel, () {
          setState(() {
            if (sym == 'None of the above') {
              _selectedSleepSymptoms.clear();
              _selectedSleepSymptoms.add('None of the above');
            } else {
              _selectedSleepSymptoms.remove('None of the above');
              if (isSel) {
                _selectedSleepSymptoms.remove(sym);
              } else {
                _selectedSleepSymptoms.add(sym);
              }
            }
          });
        });
      }),

      const SizedBox(height: 16),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Sleep schedule regularity:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...scheduleOptions.map((sch) => _buildSelectableOption(sch, _sleepSchedule == sch, () {
        setState(() => _sleepSchedule = sch);
      })),

      const SizedBox(height: 12),
      TextField(
        controller: _sleepNotesController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Sleep notes or comments (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_sleepDuration == null) {
          setState(() => _currentError = "Please select your average sleep duration");
          return;
        }
        if (_sleepQuality == null) {
          setState(() => _currentError = "Please select your sleep quality");
          return;
        }
        if (_selectedSleepSymptoms.isEmpty) {
          setState(() => _currentError = "Please select sleep symptoms or «None of the above»");
          return;
        }
        if (_sleepSchedule == null) {
          setState(() => _currentError = "Please select your sleep schedule");
          return;
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('sleep_duration', _sleepDuration);
        vault.saveAnamnesis('sleep_quality', _sleepQuality);
        vault.saveAnamnesis('sleep_symptoms', _selectedSleepSymptoms.toList());
        vault.saveAnamnesis('sleep_schedule', _sleepSchedule);

        final note = _sleepNotesController.text.trim();
        if (note.isNotEmpty) {
          vault.saveAnamnesis('sleep_schedule_notes', note, userText: note);
          vault.saveAnamnesis('user_text_sleep', note);
        } else {
          vault.removeAnamnesisKeys(['sleep_schedule_notes', 'user_text_sleep']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepDiet() {
    final vault = context.read<VaultService>();

    const dietTypeOptions = [
      'Mixed / Balanced diet',
      'Vegetarian',
      'Vegan',
      'Pescatarian',
      'Keto / Low-carb',
      'Mediterranean',
      'Special diet / Other',
    ];

    const mealOptions = ['1 meal', '2 meals', '3 meals', '4+ meals', 'Irregular'];
    const snackOptions = ['No snacks', '1–2 snacks', '3+ snacks'];

    const stapleOptions = [
      'Meat and poultry',
      'Fish and seafood',
      'Vegetables and greens',
      'Dairy products',
      'Eggs',
      'Grains and cereals',
      'Legumes and nuts',
      'Fruits and berries',
    ];

    const occasionalOptions = [
      'Sweets and desserts',
      'Fast food and fried foods',
      'Processed meats (sausages, bacon)',
      'Salty snacks (chips, crackers)',
      'Pastries and baked goods',
      'None of the above',
    ];

    const freqOptions = [
      'Rarely (< once a month)',
      '1–2 times a month',
      '1–2 times a week',
      '3–5 times a week',
      'Daily',
    ];

    return _wrapper(children: [
      _buildTitle("Your Diet 🍽️"),
      _buildSubtitle("«Your nutritional pattern directly influences metabolic rate, lipid panels, and microbiome resilience.»"),

      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Primary diet pattern:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...dietTypeOptions.map((type) => _buildSelectableOption(type, _dietType == type, () {
        setState(() {
          _dietType = type;
          if (type == 'Vegan') {
            _dietStaples.removeWhere((s) => ['Meat and poultry', 'Fish and seafood', 'Dairy products', 'Eggs'].contains(s));
          } else if (type == 'Vegetarian') {
            _dietStaples.removeWhere((s) => ['Meat and poultry', 'Fish and seafood'].contains(s));
          } else if (type == 'Pescatarian') {
            _dietStaples.removeWhere((s) => s == 'Meat and poultry');
          }
        });
      })),

      if (_dietType == 'Special diet / Other') ...[
        const SizedBox(height: 8),
        TextField(
          controller: _dietCustomDetailsController,
          style: const TextStyle(color: Colors.white),
          decoration: const InputDecoration(
            labelText: "Please specify your diet details (required)",
            labelStyle: TextStyle(color: Colors.orangeAccent),
            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.orangeAccent)),
          ),
        ),
      ],

      const SizedBox(height: 20),
      Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.white.withOpacity(0.04),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: _hasExcludedFoods ? Colors.orangeAccent.withOpacity(0.5) : Colors.white12),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            CheckboxListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text("I avoid specific foods or ingredients (allergies, intolerances)", style: TextStyle(color: Colors.white, fontSize: 14)),
              value: _hasExcludedFoods,
              activeColor: Colors.orangeAccent,
              onChanged: (val) {
                setState(() {
                  _hasExcludedFoods = val ?? false;
                  if (!_hasExcludedFoods) {
                    _excludedFoodsCollapsed = false;
                    _excludedFoodsController.clear();
                  } else {
                    _excludedFoodsCollapsed = false;
                  }
                });
              },
            ),
            if (_hasExcludedFoods) ...[
              if (!_excludedFoodsCollapsed) ...[
                TextField(
                  controller: _excludedFoodsController,
                  style: const TextStyle(color: Colors.white),
                  decoration: const InputDecoration(
                    hintText: "E.g., gluten, lactose, peanuts, shellfish",
                    hintStyle: TextStyle(color: Colors.white38),
                    enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                  ),
                ),
                const SizedBox(height: 10),
                Align(
                  alignment: Alignment.centerRight,
                  child: ElevatedButton(
                    style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                    onPressed: () {
                      setState(() => _excludedFoodsCollapsed = true);
                    },
                    child: const Text("Save and collapse", style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
                  ),
                ),
              ] else ...[
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        "Excluded: ${_excludedFoodsController.text.trim().isEmpty ? 'None specified' : _excludedFoodsController.text.trim()}",
                        style: const TextStyle(color: Colors.orangeAccent, fontSize: 13),
                      ),
                    ),
                    TextButton(
                      onPressed: () => setState(() => _excludedFoodsCollapsed = false),
                      child: const Text("Edit / Expand", style: TextStyle(color: Colors.greenAccent)),
                    ),
                  ],
                ),
              ],
            ],
          ],
        ),
      ),

      const SizedBox(height: 20),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Main meals per day:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: mealOptions.map((m) {
          final isSel = _dietMealsPerDay == m;
          return ChoiceChip(
            label: Text(m, style: TextStyle(color: isSel ? Colors.black : Colors.white, fontSize: 13)),
            selected: isSel,
            selectedColor: Colors.greenAccent,
            backgroundColor: Colors.white12,
            onSelected: (val) => setState(() => _dietMealsPerDay = m),
          );
        }).toList(),
      ),

      const SizedBox(height: 16),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Snacks per day:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: snackOptions.map((s) {
          final isSel = _dietSnacksPerDay == s;
          return ChoiceChip(
            label: Text(s, style: TextStyle(color: isSel ? Colors.black : Colors.white, fontSize: 13)),
            selected: isSel,
            selectedColor: Colors.greenAccent,
            backgroundColor: Colors.white12,
            onSelected: (val) => setState(() => _dietSnacksPerDay = s),
          );
        }).toList(),
      ),

      const SizedBox(height: 20),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Core staples in your regular diet:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...stapleOptions.map((staple) {
        final isSel = _dietStaples.contains(staple);
        return _buildSelectableOption(staple, isSel, () {
          if (!isSel) {
            if (_dietType == 'Vegan' && ['Meat and poultry', 'Fish and seafood', 'Dairy products', 'Eggs'].contains(staple)) {
              setState(() => _currentError = "Vegan diet is incompatible with animal products ($staple)");
              return;
            }
            if (_dietType == 'Vegetarian' && ['Meat and poultry', 'Fish and seafood'].contains(staple)) {
              setState(() => _currentError = "Vegetarian diet is incompatible with $staple");
              return;
            }
            if (_dietType == 'Pescatarian' && staple == 'Meat and poultry') {
              setState(() => _currentError = "Pescatarian diet is incompatible with meat and poultry");
              return;
            }
          }
          setState(() {
            _currentError = null;
            if (isSel) {
              _dietStaples.remove(staple);
            } else {
              _dietStaples.add(staple);
            }
          });
        });
      }),

      const SizedBox(height: 20),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Occasional or indulgence foods:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...occasionalOptions.map((prod) {
        if (prod == 'None of the above') {
          final isNoneSel = _dietOccasionalFoods.contains('None of the above');
          return _buildSelectableOption(prod, isNoneSel, () {
            setState(() {
              _dietOccasionalFoods.clear();
              _dietFoodFrequencies.clear();
              _activeOccasionalFood = null;
              if (!isNoneSel) {
                _dietOccasionalFoods.add('None of the above');
              }
            });
          });
        }

        final isSel = _dietOccasionalFoods.contains(prod);
        final isActive = _activeOccasionalFood == prod;
        final selectedFreq = _dietFoodFrequencies[prod];

        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _buildSelectableOption(
              selectedFreq != null ? "$prod [$selectedFreq]" : prod,
              isSel,
              () {
                setState(() {
                  _dietOccasionalFoods.remove('None of the above');
                  if (isSel) {
                    _dietOccasionalFoods.remove(prod);
                    _dietFoodFrequencies.remove(prod);
                    if (_activeOccasionalFood == prod) _activeOccasionalFood = null;
                  } else {
                    _dietOccasionalFoods.add(prod);
                    _activeOccasionalFood = prod;
                  }
                });
              },
            ),
            if (isSel && isActive) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text("How often do you eat this?", style: TextStyle(color: Colors.orangeAccent, fontSize: 13, fontWeight: FontWeight.bold)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: freqOptions.map((freq) {
                        final isFreqSel = _dietFoodFrequencies[prod] == freq;
                        return ChoiceChip(
                          label: Text(freq, style: TextStyle(color: isFreqSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isFreqSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (val) {
                            setState(() {
                              _dietFoodFrequencies[prod] = freq;
                              _activeOccasionalFood = null;
                            });
                          },
                        );
                      }).toList(),
                    ),
                  ],
                ),
              ),
            ] else if (isSel && !isActive && selectedFreq != null) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: Row(
                  children: [
                    Text("Frequency: $selectedFreq", style: const TextStyle(color: Colors.orangeAccent, fontSize: 12)),
                    const SizedBox(width: 8),
                    InkWell(
                      onTap: () => setState(() => _activeOccasionalFood = prod),
                      child: const Text("[Change ✏️]", style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                    ),
                  ],
                ),
              ),
            ],
          ],
        );
      }),

      const SizedBox(height: 12),
      TextField(
        controller: _dietDayDescriptionController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Describe a typical day of eating (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_dietType == null) {
          setState(() => _currentError = "Please select your primary diet pattern");
          return;
        }
        if (_dietType == 'Special diet / Other' && _dietCustomDetailsController.text.trim().isEmpty) {
          setState(() => _currentError = "Please specify your diet details");
          return;
        }
        if (_dietMealsPerDay == null) {
          setState(() => _currentError = "Please select your daily meal frequency");
          return;
        }
        if (_dietSnacksPerDay == null) {
          setState(() => _currentError = "Please select your snack frequency");
          return;
        }
        if (_dietStaples.isEmpty) {
          setState(() => _currentError = "Please select at least one core staple");
          return;
        }

        // Contradiction checks
        if (_dietType == 'Vegan') {
          final invalid = _dietStaples.where((s) => ['Meat and poultry', 'Fish and seafood', 'Dairy products', 'Eggs'].contains(s)).toList();
          if (invalid.isNotEmpty) {
            setState(() => _currentError = "Vegan diet cannot include animal products: ${invalid.join(', ')}");
            return;
          }
        } else if (_dietType == 'Vegetarian') {
          final invalid = _dietStaples.where((s) => ['Meat and poultry', 'Fish and seafood'].contains(s)).toList();
          if (invalid.isNotEmpty) {
            setState(() => _currentError = "Vegetarian diet cannot include: ${invalid.join(', ')}");
            return;
          }
        } else if (_dietType == 'Pescatarian') {
          if (_dietStaples.contains('Meat and poultry')) {
            setState(() => _currentError = "Pescatarian diet cannot include meat and poultry");
            return;
          }
        }

        if (_dietOccasionalFoods.isEmpty) {
          setState(() => _currentError = "Please select occasional foods or «None of the above»");
          return;
        }

        if (!_dietOccasionalFoods.contains('None of the above')) {
          for (final prod in _dietOccasionalFoods) {
            if (_dietFoodFrequencies[prod] == null || _dietFoodFrequencies[prod]!.isEmpty) {
              setState(() => _currentError = "Please select consumption frequency for «$prod»");
              return;
            }
          }
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('diet_types', _dietType);
        if (_dietType == 'Special diet / Other') {
          vault.saveAnamnesis('diet_custom_details', _dietCustomDetailsController.text.trim());
        } else {
          vault.removeAnamnesisKeys(['diet_custom_details']);
        }

        vault.saveAnamnesis('diet_meals_per_day', _dietMealsPerDay);
        vault.saveAnamnesis('diet_snacks_per_day', _dietSnacksPerDay);
        vault.saveAnamnesis('diet_staples', _dietStaples.toList());
        vault.saveAnamnesis('diet_occasional_foods', _dietOccasionalFoods.toList());
        vault.saveAnamnesis('diet_food_frequencies', _dietFoodFrequencies);

        final excludedText = _excludedFoodsController.text.trim();
        if (_hasExcludedFoods && excludedText.isNotEmpty) {
          vault.saveAnamnesis('diet_excluded_foods_text', excludedText);
        } else {
          vault.removeAnamnesisKeys(['diet_excluded_foods_text']);
        }

        final dayDesc = _dietDayDescriptionController.text.trim();
        if (dayDesc.isNotEmpty) {
          vault.saveAnamnesis('diet_day_description', dayDesc, userText: dayDesc);
        } else {
          vault.removeAnamnesisKeys(['diet_day_description']);
        }

        vault.saveAnamnesis('diet_habits', '$_dietMealsPerDay meals, $_dietSnacksPerDay snacks/day; ${_dietStaples.join(", ")}');

        _nextPage();
      }),
    ]);
  }

  Widget _stepDrinks() {
    final vault = context.read<VaultService>();

    const volumeOptions = [
      '< 1 liter',
      '1–1.5 liters',
      '1.5–2 liters',
      '2–2.5 liters',
      '> 2.5 liters',
    ];

    const drinkTypeOptions = [
      'Still water',
      'Sparkling water',
      'Coffee and coffee beverages',
      'Decaf coffee',
      'Black / Green tea',
      'Herbal tea / Infusions',
      'Sugary drinks / Sodas',
      'Energy drinks',
      'Fresh juices',
    ];

    const coffeeTypeOptions = [
      'Espresso / Black coffee',
      'Coffee with milk (Cappuccino, Latte)',
      'Sweet / Flavored coffee',
      'Instant coffee',
    ];

    const coffeeFreqOptions = [
      '1 cup a day',
      '2–3 cups a day',
      '4+ cups a day',
      'Occasional (few times a week)',
    ];

    const caffeineFreqOptions = [
      '1–2 servings a day',
      '3–4 servings a day',
      '5+ servings a day',
      'Occasional (few times a week)',
    ];

    final hasCoffee = _drinksTypes.contains('Coffee and coffee beverages');
    final hasCaffeinatedTeaOrEnergy = _drinksTypes.contains('Black / Green tea') || _drinksTypes.contains('Energy drinks');

    return _wrapper(children: [
      _buildTitle("Drinks & Hydration 💧"),
      _buildSubtitle("«Fluid balance and caffeine consumption impact renal filtration, blood pressure, and autonomic tone.»"),

      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Total daily fluid intake (excluding alcohol):", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: volumeOptions.map((v) {
          final isSel = _drinksTotalVolume == v;
          return ChoiceChip(
            label: Text(v, style: TextStyle(color: isSel ? Colors.black : Colors.white, fontSize: 13)),
            selected: isSel,
            selectedColor: Colors.greenAccent,
            backgroundColor: Colors.white12,
            onSelected: (val) => setState(() => _drinksTotalVolume = v),
          );
        }).toList(),
      ),

      const SizedBox(height: 20),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("What beverages do you regularly drink?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...drinkTypeOptions.map((drink) {
        final isSel = _drinksTypes.contains(drink);
        return _buildSelectableOption(drink, isSel, () {
          setState(() {
            if (isSel) {
              _drinksTypes.remove(drink);
              if (drink == 'Coffee and coffee beverages') {
                _drinksCoffeeTypes.clear();
                _drinksCoffeeFrequency = null;
                _drinksCoffeeCollapsed = false;
              }
            } else {
              _drinksTypes.add(drink);
              if (drink == 'Coffee and coffee beverages') {
                _drinksCoffeeCollapsed = false;
              }
            }
          });
        });
      }),

      if (hasCoffee) ...[
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.brown.withOpacity(0.15),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: Colors.orangeAccent.withOpacity(0.5)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (!_drinksCoffeeCollapsed) ...[
                const Text("Coffee details ☕", style: TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 15)),
                const SizedBox(height: 8),
                const Text("Coffee types (select all that apply):", style: TextStyle(color: Colors.white70, fontSize: 13)),
                const SizedBox(height: 6),
                Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: coffeeTypeOptions.map((cType) {
                    final isCTypeSel = _drinksCoffeeTypes.contains(cType);
                    return FilterChip(
                      label: Text(cType, style: TextStyle(color: isCTypeSel ? Colors.black : Colors.white, fontSize: 12)),
                      selected: isCTypeSel,
                      selectedColor: Colors.orangeAccent,
                      backgroundColor: Colors.white12,
                      onSelected: (val) {
                        setState(() {
                          if (isCTypeSel) {
                            _drinksCoffeeTypes.remove(cType);
                          } else {
                            _drinksCoffeeTypes.add(cType);
                          }
                        });
                      },
                    );
                  }).toList(),
                ),

                const SizedBox(height: 12),
                const Text("Coffee consumption frequency:", style: TextStyle(color: Colors.white70, fontSize: 13)),
                const SizedBox(height: 6),
                Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: coffeeFreqOptions.map((freq) {
                    final isFreqSel = _drinksCoffeeFrequency == freq;
                    return ChoiceChip(
                      label: Text(freq, style: TextStyle(color: isFreqSel ? Colors.black : Colors.white, fontSize: 12)),
                      selected: isFreqSel,
                      selectedColor: Colors.orangeAccent,
                      backgroundColor: Colors.white12,
                      onSelected: (val) => setState(() => _drinksCoffeeFrequency = freq),
                    );
                  }).toList(),
                ),

                const SizedBox(height: 12),
                Align(
                  alignment: Alignment.centerRight,
                  child: ElevatedButton(
                    style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                    onPressed: () {
                      if (_drinksCoffeeTypes.isEmpty) {
                        setState(() => _currentError = "Please select at least one coffee type");
                        return;
                      }
                      if (_drinksCoffeeFrequency == null) {
                        setState(() => _currentError = "Please select your coffee frequency");
                        return;
                      }
                      setState(() {
                        _currentError = null;
                        _drinksCoffeeCollapsed = true;
                      });
                    },
                    child: const Text("Save and collapse coffee details", style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
                  ),
                ),
              ] else ...[
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        "Coffee: ${_drinksCoffeeTypes.join(', ')} (${_drinksCoffeeFrequency ?? 'No frequency'})",
                        style: const TextStyle(color: Colors.orangeAccent, fontSize: 13),
                      ),
                    ),
                    TextButton(
                      onPressed: () => setState(() => _drinksCoffeeCollapsed = false),
                      child: const Text("Edit coffee details", style: TextStyle(color: Colors.greenAccent)),
                    ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ],

      if (hasCaffeinatedTeaOrEnergy) ...[
        const SizedBox(height: 16),
        const Align(
          alignment: Alignment.centerLeft,
          child: Text("Tea / Energy drinks frequency (optional):", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 14)),
        ),
        const SizedBox(height: 6),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: caffeineFreqOptions.map((freq) {
            final isFreqSel = _drinksCaffeineFrequency == freq;
            return ChoiceChip(
              label: Text(freq, style: TextStyle(color: isFreqSel ? Colors.black : Colors.white, fontSize: 12)),
              selected: isFreqSel,
              selectedColor: Colors.greenAccent,
              backgroundColor: Colors.white12,
              onSelected: (val) => setState(() => _drinksCaffeineFrequency = val ? freq : null),
            );
          }).toList(),
        ),
      ],

      const SizedBox(height: 12),
      TextField(
        controller: _drinksDayDescriptionController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Hydration notes or routine (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_drinksTotalVolume == null) {
          setState(() => _currentError = "Please select your total daily fluid intake");
          return;
        }
        if (_drinksTypes.isEmpty) {
          setState(() => _currentError = "Please select at least one beverage type");
          return;
        }

        if (hasCoffee) {
          if (_drinksCoffeeTypes.isEmpty) {
            setState(() => _currentError = "Please select at least one coffee type");
            return;
          }
          if (_drinksCoffeeFrequency == null) {
            setState(() => _currentError = "Please select your coffee frequency");
            return;
          }
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('drinks_total_volume', _drinksTotalVolume);
        vault.saveAnamnesis('drinks_types', _drinksTypes.toList());

        if (hasCoffee) {
          vault.saveAnamnesis('drinks_coffee_types', _drinksCoffeeTypes.toList());
          vault.saveAnamnesis('drinks_coffee_frequency', _drinksCoffeeFrequency);
        } else {
          vault.removeAnamnesisKeys(['drinks_coffee_types', 'drinks_coffee_frequency']);
        }

        if (_drinksCaffeineFrequency != null) {
          vault.saveAnamnesis('drinks_caffeine_frequency', _drinksCaffeineFrequency);
        } else {
          vault.removeAnamnesisKeys(['drinks_caffeine_frequency']);
        }

        final desc = _drinksDayDescriptionController.text.trim();
        if (desc.isNotEmpty) {
          vault.saveAnamnesis('drinks_day_description', desc, userText: desc);
          vault.saveAnamnesis('user_text_drinks', desc);
        } else {
          vault.removeAnamnesisKeys(['drinks_day_description', 'user_text_drinks']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepGISignals() {
    final vault = context.read<VaultService>();
    final brain = Provider.of<BrainService?>(context, listen: false);

    const symptomOptions = [
      'Bloating / Heaviness after meals',
      'Heartburn / Acid taste',
      'Abdominal pain',
      'Nausea / Vomiting',
      'Loose stools / Diarrhea',
      'Constipation',
      'Reduced appetite',
      'None of the above',
    ];

    const freqOptions = [
      'Less than once a month',
      '1–3 times a month',
      '1–3 times a week',
      '4–6 times a week',
      'Daily',
    ];

    const durationOptions = [
      'For the past few days',
      'For several weeks',
      'For several months',
      'For more than a year',
    ];

    return _wrapper(children: [
      _buildTitle("Digestion & Gastrointestinal Health 🎈"),
      _buildSubtitle("«Select any digestive symptoms you experience occasionally or currently.»"),

      const Align(
        alignment: Alignment.centerLeft,
        child: Text("Digestive symptoms:", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),

      ...symptomOptions.map((sym) {
        if (sym == 'None of the above') {
          final isNoneSel = _giSymptoms.contains('None of the above');
          return _buildSelectableOption(sym, isNoneSel, () {
            setState(() {
              _giSymptoms.clear();
              _giSymptomFrequencies.clear();
              _giSymptomDurations.clear();
              _activeGiSymptom = null;
              if (!isNoneSel) {
                _giSymptoms.add('None of the above');
              }
            });
          });
        }

        final isSel = _giSymptoms.contains(sym);
        final isActive = _activeGiSymptom == sym;
        final chosenFreq = _giSymptomFrequencies[sym];
        final chosenDur = _giSymptomDurations[sym];
        final isComplete = chosenFreq != null && chosenDur != null;

        String label = sym;
        if (isSel && isComplete && !isActive) {
          label = "$sym [$chosenFreq, $chosenDur]";
        }

        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _buildSelectableOption(label, isSel, () {
              setState(() {
                _giSymptoms.remove('None of the above');
                if (isSel) {
                  _giSymptoms.remove(sym);
                  _giSymptomFrequencies.remove(sym);
                  _giSymptomDurations.remove(sym);
                  if (_activeGiSymptom == sym) _activeGiSymptom = null;
                } else {
                  _giSymptoms.add(sym);
                  _giSymptomFrequencies.remove(sym);
                  _giSymptomDurations.remove(sym);
                  _activeGiSymptom = sym;
                }
              });
            }),

            if (isSel && isActive) ...[
              Container(
                margin: const EdgeInsets.only(left: 8, right: 8, bottom: 16),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.white.withOpacity(0.04),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: Colors.orangeAccent.withOpacity(0.5)),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text("Details for: $sym", style: const TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 14)),
                    const SizedBox(height: 10),
                    const Text("How often does this happen?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: freqOptions.map((f) {
                        final isFSel = _giSymptomFrequencies[sym] == f;
                        return ChoiceChip(
                          label: Text(f, style: TextStyle(color: isFSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isFSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (val) {
                            setState(() => _giSymptomFrequencies[sym] = f);
                          },
                        );
                      }).toList(),
                    ),

                    const SizedBox(height: 12),
                    const Text("How long have you been experiencing this?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: durationOptions.map((d) {
                        final isDSel = _giSymptomDurations[sym] == d;
                        return ChoiceChip(
                          label: Text(d, style: TextStyle(color: isDSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isDSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (val) {
                            setState(() => _giSymptomDurations[sym] = d);
                          },
                        );
                      }).toList(),
                    ),

                    const SizedBox(height: 12),
                    Align(
                      alignment: Alignment.centerRight,
                      child: ElevatedButton(
                        style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                        onPressed: () {
                          if (_giSymptomFrequencies[sym] == null) {
                            setState(() => _currentError = "Please select frequency for «$sym»");
                            return;
                          }
                          if (_giSymptomDurations[sym] == null) {
                            setState(() => _currentError = "Please select duration for «$sym»");
                            return;
                          }
                          setState(() {
                            _currentError = null;
                            _activeGiSymptom = null;
                          });
                        },
                        child: const Text("Save and collapse", style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
                      ),
                    ),
                  ],
                ),
              ),
            ] else if (isSel && !isActive && isComplete) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        "Details: $chosenFreq, $chosenDur",
                        style: const TextStyle(color: Colors.orangeAccent, fontSize: 12),
                      ),
                    ),
                    InkWell(
                      onTap: () => setState(() => _activeGiSymptom = sym),
                      child: const Text("[Edit ✏️]", style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                    ),
                  ],
                ),
              ),
            ] else if (isSel && !isActive && !isComplete) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: InkWell(
                  onTap: () => setState(() => _activeGiSymptom = sym),
                  child: const Text("⚠️ Incomplete details: tap to complete", style: TextStyle(color: Colors.amberAccent, fontSize: 12)),
                ),
              ),
            ],
          ],
        );
      }),

      const SizedBox(height: 16),
      TextField(
        controller: _giDetailsController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Additional details (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          hintText: "For example: stomach discomfort after certain foods, heartburn at night, or bloating after dairy products.",
          hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () async {
        if (_giSymptoms.isEmpty) {
          setState(() => _currentError = "Please select your digestive symptoms or «None of the above»");
          return;
        }

        if (!_giSymptoms.contains('None of the above')) {
          for (final sym in _giSymptoms) {
            if (_giSymptomFrequencies[sym] == null || _giSymptomDurations[sym] == null) {
              setState(() {
                _activeGiSymptom = sym;
                _currentError = "Please complete frequency and duration for «$sym»";
              });
              return;
            }
          }
        }

        setState(() => _currentError = null);

        final detailsText = _giDetailsController.text.trim();
        if (detailsText.isNotEmpty && brain != null) {
          await brain.identifySystems(detailsText);
        }

        final bool isNone = _giSymptoms.contains('None of the above');
        final String giSignals = isNone ? 'None' : _giSymptoms.join(', ');

        vault.saveAnamnesis(
          'gi_signals',
          giSignals,
          userText: detailsText,
        );
        vault.saveAnamnesis('gi_symptoms', _giSymptoms.toList());

        if (!isNone) {
          final Map<String, Map<String, String>> detailsMap = {};
          for (final sym in _giSymptoms) {
            detailsMap[sym] = {
              'frequency': _giSymptomFrequencies[sym]!,
              'duration': _giSymptomDurations[sym]!,
            };
          }
          vault.saveAnamnesis('gi_symptom_details', detailsMap);
        } else {
          vault.removeAnamnesisKeys(['gi_symptom_details']);
        }

        if (detailsText.isNotEmpty) {
          vault.saveAnamnesis('user_text_gi_signals', detailsText, userText: detailsText);
          vault.saveAnamnesis('text_gi_signals', detailsText);
        } else {
          vault.removeAnamnesisKeys(['user_text_gi_signals', 'text_gi_signals']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepWorkMode() {
    final vault = context.read<VaultService>();

    const activityOptions = [
      'Mostly sitting',
      'Standing or walking',
      'Heavy physical work',
      'A mix of activities',
    ];

    const scheduleOptions = [
      'Regular daytime schedule',
      'Shift work',
      'Mostly night shifts',
      'Irregular schedule',
      'Not currently working or studying',
    ];

    const stressOptions = [
      'Low',
      'Moderate',
      'High',
    ];

    return _wrapper(children: [
      _buildTitle("Work & Daily Routine 💼"),
      _buildSubtitle("«Your daily environment and occupational stress significantly affect muscular load and circadian rhythm.»"),

      TextField(
        controller: _workOccupationController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "What is your occupation or profession? (optional)",
          labelStyle: TextStyle(color: Colors.white70),
          hintText: "e.g. Software developer, teacher, driver, student, retired",
          hintStyle: TextStyle(color: Colors.white24, fontSize: 13),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 20),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("How do you spend most of your day?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...activityOptions.map((opt) => _buildSelectableOption(opt, _workActivity == opt, () {
        setState(() => _workActivity = opt);
      })),

      const SizedBox(height: 16),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("What is your usual schedule?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...scheduleOptions.map((opt) => _buildSelectableOption(opt, _workSchedule == opt, () {
        setState(() => _workSchedule = opt);
      })),

      const SizedBox(height: 16),
      const Align(
        alignment: Alignment.centerLeft,
        child: Text("How would you rate your daily stress?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...stressOptions.map((opt) => _buildSelectableOption(opt, _workStress == opt, () {
        setState(() => _workStress = opt);
      })),

      const SizedBox(height: 16),
      TextField(
        controller: _workNotesController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Additional details (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          hintText: "e.g. I work remotely and sit for about 9 hours a day. Some weeks involve night shifts.",
          hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_workActivity == null) {
          setState(() => _currentError = "Please select how you spend most of your day");
          return;
        }
        if (_workSchedule == null) {
          setState(() => _currentError = "Please select your usual schedule");
          return;
        }
        if (_workStress == null) {
          setState(() => _currentError = "Please rate your daily stress level");
          return;
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('work_type', _workActivity);
        vault.saveAnamnesis('work_schedule', _workSchedule);
        vault.saveAnamnesis('work_stress', _workStress);

        final occ = _workOccupationController.text.trim();
        if (occ.isNotEmpty) {
          vault.saveAnamnesis('work_occupation', occ);
        } else {
          vault.removeAnamnesisKeys(['work_occupation']);
        }

        final notes = _workNotesController.text.trim();
        if (notes.isNotEmpty) {
          vault.saveAnamnesis('user_text_work', notes, userText: notes);
          vault.saveAnamnesis('text_work', notes);
        } else {
          vault.removeAnamnesisKeys(['user_text_work', 'text_work']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepSport() {
    final vault = context.read<VaultService>();

    const sportLevelOptions = [
      'Almost no physical activity',
      'Light activity — walking, daily tasks',
      'Moderate — regular movement or exercise',
      'High — intense exercise or training',
    ];

    const daysOptions = [
      '1 day',
      '2 days',
      '3–4 days',
      '5–7 days',
    ];

    const durationOptions = [
      'Less than 30 minutes',
      '30–60 minutes',
      'More than 60 minutes',
    ];

    const activityOptions = [
      'Walking',
      'Running / Cardio',
      'Strength training',
      'Swimming',
      'Cycling',
      'Dancing',
      'Team sports',
      'Yoga / Stretching',
      'Other',
    ];

    final isAlmostNoActivity = _sportLevel == 'Almost no physical activity';
    final hasSportActive = _sportLevel != null && !isAlmostNoActivity;

    return _wrapper(children: [
      _buildTitle("Physical Activity 🏃"),
      _buildSubtitle("«Regular movement supports cardiovascular reserve, insulin sensitivity, and musculoskeletal health.»"),

      const Align(
        alignment: Alignment.centerLeft,
        child: Text("How would you describe your physical activity outside work?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
      ),
      const SizedBox(height: 8),
      ...sportLevelOptions.map((opt) => _buildSelectableOption(opt, _sportLevel == opt, () {
        setState(() {
          _sportLevel = opt;
          if (opt == 'Almost no physical activity') {
            _activityDaysPerWeek = null;
            _activityDurationMinutes = null;
            _activityTypes.clear();
            _activityOtherController.clear();
          }
        });
      })),

      if (hasSportActive) ...[
        const SizedBox(height: 20),
        const Align(
          alignment: Alignment.centerLeft,
          child: Text("How many days per week are you usually active?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: daysOptions.map((d) {
            final isSel = _activityDaysPerWeek == d;
            return ChoiceChip(
              label: Text(d, style: TextStyle(color: isSel ? Colors.black : Colors.white, fontSize: 13)),
              selected: isSel,
              selectedColor: Colors.greenAccent,
              backgroundColor: Colors.white12,
              onSelected: (val) => setState(() => _activityDaysPerWeek = d),
            );
          }).toList(),
        ),

        const SizedBox(height: 20),
        const Align(
          alignment: Alignment.centerLeft,
          child: Text("How long does your activity usually last on those days?", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: durationOptions.map((d) {
            final isSel = _activityDurationMinutes == d;
            return ChoiceChip(
              label: Text(d, style: TextStyle(color: isSel ? Colors.black : Colors.white, fontSize: 13)),
              selected: isSel,
              selectedColor: Colors.greenAccent,
              backgroundColor: Colors.white12,
              onSelected: (val) => setState(() => _activityDurationMinutes = d),
            );
          }).toList(),
        ),

        const SizedBox(height: 20),
        const Align(
          alignment: Alignment.centerLeft,
          child: Text("What activities do you do? (select all that apply)", style: TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 15)),
        ),
        const SizedBox(height: 8),
        ...activityOptions.map((act) {
          final isSel = _activityTypes.contains(act);
          return _buildSelectableOption(act, isSel, () {
            setState(() {
              if (isSel) {
                _activityTypes.remove(act);
                if (act == 'Other') {
                  _activityOtherController.clear();
                }
              } else {
                _activityTypes.add(act);
              }
            });
          });
        }),

        if (_activityTypes.contains('Other')) ...[
          const SizedBox(height: 10),
          TextField(
            controller: _activityOtherController,
            style: const TextStyle(color: Colors.white),
            decoration: const InputDecoration(
              labelText: "Please specify your activity",
              labelStyle: TextStyle(color: Colors.orangeAccent),
              hintText: "e.g. Martial arts, climbing, Pilates",
              hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
              enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.orangeAccent)),
            ),
          ),
        ],
      ],

      const SizedBox(height: 20),
      TextField(
        controller: _activityNotesController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Tell us a little more about your activity (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          hintText: "e.g. I used to go to the gym, but stopped two months ago. Now I mostly stay at home and walk occasionally.",
          hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_sportLevel == null) {
          setState(() => _currentError = "Please select your physical activity level");
          return;
        }

        if (hasSportActive) {
          if (_activityDaysPerWeek == null) {
            setState(() => _currentError = "Please select how many days per week you are active");
            return;
          }
          if (_activityDurationMinutes == null) {
            setState(() => _currentError = "Please select how long your activity usually lasts");
            return;
          }
          if (_activityTypes.isEmpty) {
            setState(() => _currentError = "Please select at least one activity type");
            return;
          }
          if (_activityTypes.contains('Other') && _activityOtherController.text.trim().isEmpty) {
            setState(() => _currentError = "Please specify your activity for «Other»");
            return;
          }
        }

        setState(() => _currentError = null);

        vault.saveAnamnesis('sport_level', _sportLevel);

        if (hasSportActive) {
          vault.saveAnamnesis('activity_days_per_week', _activityDaysPerWeek);
          vault.saveAnamnesis('activity_duration_minutes', _activityDurationMinutes);
          vault.saveAnamnesis('activity_types', _activityTypes.toList());
          if (_activityTypes.contains('Other')) {
            vault.saveAnamnesis('activity_other_details', _activityOtherController.text.trim());
          } else {
            vault.removeAnamnesisKeys(['activity_other_details']);
          }
        } else {
          vault.removeAnamnesisKeys(['activity_days_per_week', 'activity_duration_minutes', 'activity_types', 'activity_other_details']);
        }

        final notes = _activityNotesController.text.trim();
        if (notes.isNotEmpty) {
          vault.saveAnamnesis('user_text_sport', notes, userText: notes);
          vault.saveAnamnesis('text_sport', notes);
        } else {
          vault.removeAnamnesisKeys(['user_text_sport', 'text_sport']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepChronic() {
    final vault = context.read<VaultService>();

    const conditionCategories = [
      'Heart or blood pressure conditions',
      'Diabetes or blood sugar disorders',
      'Thyroid or hormonal conditions',
      'Asthma or chronic respiratory conditions',
      'Allergies or chronic skin conditions',
      'Chronic digestive conditions',
      'Kidney or urinary conditions',
      'Joint, spine or musculoskeletal conditions',
      'Neurological conditions',
      'Cancer or oncological history',
      'Mental health conditions',
      'Other diagnosed conditions',
      'No diagnosed chronic conditions',
      "Not sure / Haven't been evaluated",
    ];

    const diagnosisHints = <String, String>{
      'Heart or blood pressure conditions': 'e.g. Arterial hypertension, congenital heart defect, arrhythmia',
      'Diabetes or blood sugar disorders': 'e.g. Type 1 diabetes, type 2 diabetes, prediabetes',
      'Thyroid or hormonal conditions': "e.g. Hypothyroidism, Hashimoto's thyroiditis, hyperthyroidism",
      'Asthma or chronic respiratory conditions': 'e.g. Bronchial asthma, COPD, chronic bronchitis',
      'Allergies or chronic skin conditions': 'e.g. Atopic dermatitis, allergic rhinitis, chronic eczema',
      'Chronic digestive conditions': 'e.g. Gastritis, GERD, inflammatory bowel disease',
      'Kidney or urinary conditions': 'e.g. Chronic kidney disease, recurrent kidney stones',
      'Joint, spine or musculoskeletal conditions': 'e.g. Lumbar disc herniation, scoliosis, rheumatoid arthritis',
      'Neurological conditions': 'e.g. Migraine, epilepsy, peripheral neuropathy',
      'Cancer or oncological history': 'e.g. Breast cancer, colorectal cancer, lymphoma',
      'Mental health conditions': 'e.g. Depression, anxiety disorder, bipolar disorder',
      'Other diagnosed conditions': 'e.g. Specify your diagnosed condition',
    };

    const symptomHints = <String, String>{
      'Heart or blood pressure conditions': 'e.g. Occasional palpitations or shortness of breath',
      'Diabetes or blood sugar disorders': 'e.g. Blood sugar fluctuations or increased thirst',
      'Thyroid or hormonal conditions': 'e.g. Fatigue, temperature sensitivity, or weight changes',
      'Asthma or chronic respiratory conditions': 'e.g. Occasional wheezing or shortness of breath',
      'Allergies or chronic skin conditions': 'e.g. Seasonal sneezing, itching, or skin flare-ups',
      'Chronic digestive conditions': 'e.g. Heartburn after meals',
      'Kidney or urinary conditions': 'e.g. Flank discomfort or changes in urination',
      'Joint, spine or musculoskeletal conditions': 'e.g. Lower back pain after sitting for a long time',
      'Neurological conditions': 'e.g. Recurring headaches or tingling sensations',
      'Cancer or oncological history': 'e.g. Ongoing fatigue or post-treatment sensitivity',
      'Mental health conditions': 'e.g. Periods of low mood, tension, or poor sleep',
      'Other diagnosed conditions': 'e.g. Describe any current symptoms you notice',
    };

    const statusOptions = [
      'Ongoing / Still present',
      'Controlled or stable',
      'In remission (if applicable)',
      'Previously diagnosed, no current symptoms',
      'Not sure',
    ];

    const oncoStatusOptions = [
      'Remission',
      'Newly diagnosed',
      'Active treatment',
      'Relapse',
    ];

    const bothersOptions = [
      "No, it doesn't currently bother me",
      'Yes, I occasionally experience symptoms',
      'Yes, I frequently experience symptoms',
      'Not sure',
    ];

    bool isNegativeCategory(String c) =>
        c == 'No diagnosed chronic conditions' || c == "Not sure / Haven't been evaluated";

    bool isCategoryComplete(String cat) {
      final d = _chronicConditionDetails[cat];
      if (d == null) return false;
      final diag = (d['diagnosis'] ?? '').trim();
      final status = (d['current_status'] ?? '').trim();
      final bothers = (d['currently_bothers'] ?? '').trim();
      if (diag.isEmpty || status.isEmpty || bothers.isEmpty) return false;
      if (cat == 'Cancer or oncological history') {
        final onco = (d['onco_status'] ?? '').trim();
        if (onco.isEmpty) return false;
      }
      return true;
    }

    return _wrapper(children: [
      _buildTitle("Chronic Conditions & Medical History"),
      _buildSubtitle("Have you ever been diagnosed with any of the following conditions by a healthcare professional?"),

      ...conditionCategories.map((cat) {
        if (isNegativeCategory(cat)) {
          final isSel = _chronicCategories.contains(cat);
          return _buildSelectableOption(cat, isSel, () {
            setState(() {
              for (final existing in _chronicCategories.toList()) {
                _clearChronicCategoryControllers(existing);
              }
              _chronicCategories.clear();
              _chronicConditionDetails.clear();
              _activeChronicCategory = null;
              _currentError = null;
              if (!isSel) {
                _chronicCategories.add(cat);
              }
            });
          });
        }

        final isSel = _chronicCategories.contains(cat);
        final isActive = _activeChronicCategory == cat;
        final isComplete = isCategoryComplete(cat);
        final detail = _chronicConditionDetails[cat] ?? {};
        final diagName = (detail['diagnosis'] ?? '').trim();
        final curStatus = (detail['current_status'] ?? '').trim();

        String optionLabel = cat;
        if (isSel && isComplete && !isActive) {
          optionLabel = "$cat [$diagName • $curStatus]";
        }

        final diagCtrl = _getMapController(_chronicDiagnosisControllers, cat);
        final whenCtrl = _getMapController(_chronicWhenControllers, cat);
        final symCtrl = _getMapController(_chronicSymptomsControllers, cat);
        final chosenBothers = detail['currently_bothers'];
        final showSymptomField = chosenBothers == 'Yes, I occasionally experience symptoms' ||
            chosenBothers == 'Yes, I frequently experience symptoms';

        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _buildSelectableOption(optionLabel, isSel, () {
              setState(() {
                _chronicCategories.remove('No diagnosed chronic conditions');
                _chronicCategories.remove("Not sure / Haven't been evaluated");
                _currentError = null;
                if (isSel) {
                  _chronicCategories.remove(cat);
                  _chronicConditionDetails.remove(cat);
                  _clearChronicCategoryControllers(cat);
                  if (_activeChronicCategory == cat) {
                    _activeChronicCategory = null;
                  }
                } else {
                  _chronicCategories.add(cat);
                  _chronicConditionDetails[cat] = {};
                  _clearChronicCategoryControllers(cat);
                  _activeChronicCategory = cat;
                }
              });
            }),

            if (isSel && isActive) ...[
              Container(
                margin: const EdgeInsets.only(left: 8, right: 8, bottom: 16),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.04),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: Colors.orangeAccent.withValues(alpha: 0.5)),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text("Details for: $cat", style: const TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 14)),
                    const SizedBox(height: 10),

                    const Text("What condition were you diagnosed with?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    TextField(
                      controller: diagCtrl,
                      style: const TextStyle(color: Colors.white),
                      onChanged: (val) {
                        _chronicConditionDetails.putIfAbsent(cat, () => {})['diagnosis'] = val.trim();
                      },
                      decoration: InputDecoration(
                        hintText: diagnosisHints[cat] ?? 'e.g. Specify your diagnosed condition',
                        hintStyle: const TextStyle(color: Colors.white24, fontSize: 12),
                        enabledBorder: const UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                      ),
                    ),

                    const SizedBox(height: 12),
                    const Text("When was it diagnosed? (optional)", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    TextField(
                      controller: whenCtrl,
                      style: const TextStyle(color: Colors.white),
                      onChanged: (val) {
                        final trimmed = val.trim();
                        final map = _chronicConditionDetails.putIfAbsent(cat, () => {});
                        if (trimmed.isNotEmpty) {
                          map['diagnosed_when'] = trimmed;
                        } else {
                          map.remove('diagnosed_when');
                        }
                      },
                      decoration: const InputDecoration(
                        hintText: "e.g. In 2021 or approximately 5 years ago",
                        hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
                        enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                      ),
                    ),

                    if (cat == 'Cancer or oncological history') ...[
                      const SizedBox(height: 12),
                      const Text("Oncological stage / clinical status:", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                      const SizedBox(height: 6),
                      Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: oncoStatusOptions.map((st) {
                          final isOSel = detail['onco_status'] == st;
                          return ChoiceChip(
                            label: Text(st, style: TextStyle(color: isOSel ? Colors.black : Colors.white, fontSize: 12)),
                            selected: isOSel,
                            selectedColor: Colors.orangeAccent,
                            backgroundColor: Colors.white12,
                            onSelected: (_) {
                              setState(() {
                                _chronicConditionDetails.putIfAbsent(cat, () => {})['onco_status'] = st;
                              });
                            },
                          );
                        }).toList(),
                      ),
                    ],

                    const SizedBox(height: 12),
                    const Text("What is its current status?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: statusOptions.map((st) {
                        final isSSel = detail['current_status'] == st;
                        return ChoiceChip(
                          label: Text(st, style: TextStyle(color: isSSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isSSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (_) {
                            setState(() {
                              _chronicConditionDetails.putIfAbsent(cat, () => {})['current_status'] = st;
                            });
                          },
                        );
                      }).toList(),
                    ),

                    const SizedBox(height: 12),
                    const Text("Does this condition currently bother you?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: bothersOptions.map((bOpt) {
                        final isBSel = detail['currently_bothers'] == bOpt;
                        return ChoiceChip(
                          label: Text(bOpt, style: TextStyle(color: isBSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isBSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (_) {
                            setState(() {
                              final map = _chronicConditionDetails.putIfAbsent(cat, () => {});
                              map['currently_bothers'] = bOpt;
                              if (bOpt == "No, it doesn't currently bother me" || bOpt == 'Not sure') {
                                symCtrl.clear();
                                map.remove('current_symptoms_text');
                              }
                            });
                          },
                        );
                      }).toList(),
                    ),

                    if (showSymptomField) ...[
                      const SizedBox(height: 12),
                      const Text("What symptoms are you experiencing? (optional)", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                      TextField(
                        controller: symCtrl,
                        style: const TextStyle(color: Colors.white),
                        onChanged: (val) {
                          final trimmed = val.trim();
                          final map = _chronicConditionDetails.putIfAbsent(cat, () => {});
                          if (trimmed.isNotEmpty) {
                            map['current_symptoms_text'] = trimmed;
                          } else {
                            map.remove('current_symptoms_text');
                          }
                        },
                        decoration: InputDecoration(
                          hintText: symptomHints[cat] ?? 'e.g. Describe any current symptoms you notice',
                          hintStyle: const TextStyle(color: Colors.white24, fontSize: 12),
                          enabledBorder: const UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                        ),
                      ),
                    ],

                    const SizedBox(height: 12),
                    Align(
                      alignment: Alignment.centerRight,
                      child: ElevatedButton(
                        style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                        onPressed: () {
                          final map = _chronicConditionDetails.putIfAbsent(cat, () => {});
                          final diagText = diagCtrl.text.trim();
                          if (diagText.isEmpty) {
                            setState(() => _currentError = "Please enter the diagnosed condition for «$cat»");
                            return;
                          }
                          map['diagnosis'] = diagText;

                          final whenText = whenCtrl.text.trim();
                          if (whenText.isNotEmpty) {
                            map['diagnosed_when'] = whenText;
                          } else {
                            map.remove('diagnosed_when');
                          }

                          if (cat == 'Cancer or oncological history' && (map['onco_status'] ?? '').isEmpty) {
                            setState(() => _currentError = "Please select the oncological status for «$cat»");
                            return;
                          }

                          if ((map['current_status'] ?? '').isEmpty) {
                            setState(() => _currentError = "Please select the current status for «$cat»");
                            return;
                          }

                          if ((map['currently_bothers'] ?? '').isEmpty) {
                            setState(() => _currentError = "Please indicate whether «$cat» currently bothers you");
                            return;
                          }

                          final bothers = map['currently_bothers'];
                          if (bothers == 'Yes, I occasionally experience symptoms' ||
                              bothers == 'Yes, I frequently experience symptoms') {
                            final symText = symCtrl.text.trim();
                            if (symText.isNotEmpty) {
                              map['current_symptoms_text'] = symText;
                            } else {
                              map.remove('current_symptoms_text');
                            }
                          } else {
                            symCtrl.clear();
                            map.remove('current_symptoms_text');
                          }

                          setState(() {
                            _currentError = null;
                            _activeChronicCategory = null;
                          });
                        },
                        child: const Text("Save and collapse", style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
                      ),
                    ),
                  ],
                ),
              ),
            ] else if (isSel && !isActive && isComplete) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: InkWell(
                  onTap: () => setState(() => _activeChronicCategory = cat),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          "$diagName • $curStatus",
                          style: const TextStyle(color: Colors.orangeAccent, fontSize: 12),
                        ),
                      ),
                      const Text("[Edit ✏️]", style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                    ],
                  ),
                ),
              ),
            ] else if (isSel && !isActive && !isComplete) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: InkWell(
                  onTap: () => setState(() => _activeChronicCategory = cat),
                  child: const Text("⚠️ Incomplete details: tap to complete", style: TextStyle(color: Colors.amberAccent, fontSize: 12)),
                ),
              ),
            ],
          ],
        );
      }),

      const SizedBox(height: 16),
      TextField(
        controller: _chronicNotesController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Additional medical history (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          hintText: "e.g. I have a diagnosed lower back disc problem. Symptoms occasionally return after prolonged sitting.",
          hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_chronicCategories.isEmpty) {
          setState(() => _currentError = "Please select at least one option or «No diagnosed chronic conditions»");
          return;
        }

        final hasNegative = _chronicCategories.any(isNegativeCategory);
        if (hasNegative) {
          setState(() => _currentError = null);
          vault.saveAnamnesis('chronic_categories', _chronicCategories.toList());
          vault.removeAnamnesisKeys(['chronic_condition_details', 'onco_status']);
          vault.saveAnamnesis('chronic_diseases', _chronicCategories.first);
        } else {
          for (final cat in _chronicCategories) {
            final map = _chronicConditionDetails[cat];
            final diagText = _chronicDiagnosisControllers[cat]?.text.trim() ?? (map?['diagnosis'] ?? '').trim();
            if (diagText.isEmpty) {
              setState(() {
                _activeChronicCategory = cat;
                _currentError = "Please enter the diagnosed condition for «$cat»";
              });
              return;
            }
            if (map != null) {
              map['diagnosis'] = diagText;
              final whenText = _chronicWhenControllers[cat]?.text.trim() ?? '';
              if (whenText.isNotEmpty) {
                map['diagnosed_when'] = whenText;
              } else {
                map.remove('diagnosed_when');
              }
            }
            if (!isCategoryComplete(cat)) {
              setState(() {
                _activeChronicCategory = cat;
                _currentError = "Please complete all required details for «$cat»";
              });
              return;
            }
          }

          // Clean copy ensuring no extraneous keys
          final cleanDetails = <String, Map<String, String>>{};
          final summaryParts = <String>[];
          String? resolvedOncoStatus;

          for (final cat in _chronicCategories) {
            final src = _chronicConditionDetails[cat]!;
            final cleaned = Map<String, String>.from(src);
            final bothers = cleaned['currently_bothers'];
            if (bothers == "No, it doesn't currently bother me" || bothers == 'Not sure') {
              cleaned.remove('current_symptoms_text');
            }
            cleanDetails[cat] = cleaned;
            summaryParts.add("$cat (${cleaned['diagnosis']})");
            if (cat == 'Cancer or oncological history') {
              resolvedOncoStatus = cleaned['onco_status'];
            }
          }

          setState(() => _currentError = null);
          vault.saveAnamnesis('chronic_categories', _chronicCategories.toList());
          vault.saveAnamnesis('chronic_condition_details', cleanDetails);
          vault.saveAnamnesis('chronic_diseases', summaryParts.join(', '));
          if (resolvedOncoStatus != null) {
            vault.saveAnamnesis('onco_status', resolvedOncoStatus);
          } else {
            vault.removeAnamnesisKeys(['onco_status']);
          }
        }

        final notes = _chronicNotesController.text.trim();
        if (notes.isNotEmpty) {
          vault.saveAnamnesis('user_text_chronic_diseases', notes, userText: notes);
          vault.saveAnamnesis('text_chronic_diseases', notes);
        } else {
          vault.removeAnamnesisKeys(['user_text_chronic_diseases', 'text_chronic_diseases']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepRecentEvents() {
    final vault = context.read<VaultService>();

    const recentEventOptions = [
      'Cold, flu, COVID-19 or another respiratory viral infection',
      'Food poisoning or gastrointestinal infection',
      'Another infection',
      'Injury or significant physical trauma',
      'Surgery or medical procedure',
      'Hospitalization or emergency visit',
      'Other significant health event',
      'None of the above',
    ];

    const timeframeOptions = <MapEntry<String, String>>[
      MapEntry('Within the last 7 days', '0_7_days'),
      MapEntry('Within the last month', '8_30_days'),
      MapEntry('1–3 months ago', '31_90_days'),
    ];

    const recoveryOptions = [
      'Fully recovered',
      'Improving',
      'Symptoms are still present',
    ];

    bool isEventComplete(String ev) {
      final d = _recentEventDetails[ev];
      if (d == null) return false;
      final tf = (d['timeframe_code'] ?? '').trim();
      final rec = (d['recovery_status'] ?? '').trim();
      return tf.isNotEmpty && rec.isNotEmpty;
    }

    return _wrapper(children: [
      _buildTitle("Recent Illnesses & Health Events"),
      _buildSubtitle("Have you experienced any of the following during the last 3 months?"),

      ...recentEventOptions.map((ev) {
        if (ev == 'None of the above') {
          final isNoneSel = _recentEvents.contains('None of the above');
          return _buildSelectableOption(ev, isNoneSel, () {
            setState(() {
              for (final existing in _recentEvents.toList()) {
                _recentDetailsControllers[existing]?.clear();
              }
              _recentEvents.clear();
              _recentEventDetails.clear();
              _activeRecentEvent = null;
              _currentError = null;
              if (!isNoneSel) {
                _recentEvents.add('None of the above');
              }
            });
          });
        }

        final isSel = _recentEvents.contains(ev);
        final isActive = _activeRecentEvent == ev;
        final isComplete = isEventComplete(ev);
        final detail = _recentEventDetails[ev] ?? {};
        final chosenTfLabel = detail['timeframe'] ?? '';
        final chosenRecovery = detail['recovery_status'] ?? '';

        String optionLabel = ev;
        if (isSel && isComplete && !isActive) {
          optionLabel = "$ev [$chosenTfLabel, $chosenRecovery]";
        }

        final detailsCtrl = _getMapController(_recentDetailsControllers, ev);

        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _buildSelectableOption(optionLabel, isSel, () {
              setState(() {
                _recentEvents.remove('None of the above');
                _currentError = null;
                if (isSel) {
                  _recentEvents.remove(ev);
                  _recentEventDetails.remove(ev);
                  _recentDetailsControllers[ev]?.clear();
                  if (_activeRecentEvent == ev) {
                    _activeRecentEvent = null;
                  }
                } else {
                  _recentEvents.add(ev);
                  _recentEventDetails[ev] = {};
                  _recentDetailsControllers[ev]?.clear();
                  _activeRecentEvent = ev;
                }
              });
            }),

            if (isSel && isActive) ...[
              Container(
                margin: const EdgeInsets.only(left: 8, right: 8, bottom: 16),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.04),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: Colors.orangeAccent.withValues(alpha: 0.5)),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text("Details for: $ev", style: const TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 14)),
                    const SizedBox(height: 10),

                    const Text("When did this happen?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: timeframeOptions.map((entry) {
                        final isTfSel = detail['timeframe_code'] == entry.value;
                        return ChoiceChip(
                          label: Text(entry.key, style: TextStyle(color: isTfSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isTfSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (_) {
                            setState(() {
                              final map = _recentEventDetails.putIfAbsent(ev, () => {});
                              map['timeframe'] = entry.key;
                              map['timeframe_code'] = entry.value;
                            });
                          },
                        );
                      }).toList(),
                    ),

                    const SizedBox(height: 12),
                    const Text("How are you feeling now?", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: recoveryOptions.map((rec) {
                        final isRSel = detail['recovery_status'] == rec;
                        return ChoiceChip(
                          label: Text(rec, style: TextStyle(color: isRSel ? Colors.black : Colors.white, fontSize: 12)),
                          selected: isRSel,
                          selectedColor: Colors.orangeAccent,
                          backgroundColor: Colors.white12,
                          onSelected: (_) {
                            setState(() {
                              _recentEventDetails.putIfAbsent(ev, () => {})['recovery_status'] = rec;
                            });
                          },
                        );
                      }).toList(),
                    ),

                    const SizedBox(height: 12),
                    const Text("Additional details (optional)", style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                    TextField(
                      controller: detailsCtrl,
                      style: const TextStyle(color: Colors.white),
                      onChanged: (val) {
                        final trimmed = val.trim();
                        final map = _recentEventDetails.putIfAbsent(ev, () => {});
                        if (trimmed.isNotEmpty) {
                          map['details'] = trimmed;
                        } else {
                          map.remove('details');
                        }
                      },
                      decoration: const InputDecoration(
                        hintText: "e.g. Fever for 3 days, treated at home",
                        hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
                        enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                      ),
                    ),

                    const SizedBox(height: 12),
                    Align(
                      alignment: Alignment.centerRight,
                      child: ElevatedButton(
                        style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                        onPressed: () {
                          final map = _recentEventDetails.putIfAbsent(ev, () => {});
                          if ((map['timeframe_code'] ?? '').isEmpty) {
                            setState(() => _currentError = "Please select when «$ev» happened");
                            return;
                          }
                          if ((map['recovery_status'] ?? '').isEmpty) {
                            setState(() => _currentError = "Please select your recovery status for «$ev»");
                            return;
                          }
                          final detText = detailsCtrl.text.trim();
                          if (detText.isNotEmpty) {
                            map['details'] = detText;
                          } else {
                            map.remove('details');
                          }
                          setState(() {
                            _currentError = null;
                            _activeRecentEvent = null;
                          });
                        },
                        child: const Text("Save and collapse", style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold)),
                      ),
                    ),
                  ],
                ),
              ),
            ] else if (isSel && !isActive && isComplete) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: InkWell(
                  onTap: () => setState(() => _activeRecentEvent = ev),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          "$chosenTfLabel • $chosenRecovery",
                          style: const TextStyle(color: Colors.orangeAccent, fontSize: 12),
                        ),
                      ),
                      const Text("[Edit ✏️]", style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                    ],
                  ),
                ),
              ),
            ] else if (isSel && !isActive && !isComplete) ...[
              Padding(
                padding: const EdgeInsets.only(left: 12, bottom: 8),
                child: InkWell(
                  onTap: () => setState(() => _activeRecentEvent = ev),
                  child: const Text("⚠️ Incomplete details: tap to complete", style: TextStyle(color: Colors.amberAccent, fontSize: 12)),
                ),
              ),
            ],
          ],
        );
      }),

      const SizedBox(height: 16),
      TextField(
        controller: _recentNotesController,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          labelText: "Anything else you would like to mention? (optional)",
          labelStyle: TextStyle(color: Colors.white38),
          hintText: "e.g. I had a bad flu about 6 weeks ago and still feel more tired than usual.",
          hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
        ),
      ),

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_recentEvents.isEmpty) {
          setState(() => _currentError = "Please select your recent events or «None of the above»");
          return;
        }

        if (_recentEvents.contains('None of the above')) {
          setState(() => _currentError = null);
          vault.saveAnamnesis('recent_events', ['None of the above']);
          vault.removeAnamnesisKeys(['recent_event_details']);
          vault.saveAnamnesis('recent_illness', 'Healthy (None of the above)');
        } else {
          for (final ev in _recentEvents) {
            if (!isEventComplete(ev)) {
              setState(() {
                _activeRecentEvent = ev;
                _currentError = "Please complete timeframe and recovery status for «$ev»";
              });
              return;
            }
          }

          final cleanDetails = <String, Map<String, String>>{};
          final summaryParts = <String>[];
          for (final ev in _recentEvents) {
            final src = _recentEventDetails[ev]!;
            final cleaned = Map<String, String>.from(src);
            final detText = _recentDetailsControllers[ev]?.text.trim() ?? (cleaned['details'] ?? '').trim();
            if (detText.isNotEmpty) {
              cleaned['details'] = detText;
            } else {
              cleaned.remove('details');
            }
            cleanDetails[ev] = cleaned;
            summaryParts.add("$ev (${cleaned['timeframe']}, ${cleaned['recovery_status']})");
          }

          setState(() => _currentError = null);
          vault.saveAnamnesis('recent_events', _recentEvents.toList());
          vault.saveAnamnesis('recent_event_details', cleanDetails);
          vault.saveAnamnesis('recent_illness', summaryParts.join(', '));
        }

        final notes = _recentNotesController.text.trim();
        if (notes.isNotEmpty) {
          vault.saveAnamnesis('user_text_recent_illness', notes, userText: notes);
          vault.saveAnamnesis('text_recent_illness', notes);
        } else {
          vault.removeAnamnesisKeys(['user_text_recent_illness', 'text_recent_illness']);
        }

        _nextPage();
      }),
    ]);
  }

  Widget _stepSafeSpace() {
    final vault = context.read<VaultService>();

    const statusOptions = [
      'I have been diagnosed with one or more of these infections',
      'I have not been diagnosed with any of these infections',
      'I have had negative test results',
      "I don't know / I haven't been tested",
      'Prefer not to answer',
    ];

    const infectionList = [
      'Hepatitis B',
      'Hepatitis C',
      'HIV',
      'Tuberculosis',
      'Syphilis',
    ];

    const diagnosedKey = 'I have been diagnosed with one or more of these infections';
    const negativeKey = 'I have had negative test results';

    return _wrapper(children: [
      _buildTitle("Infectious Disease History"),
      Container(
        margin: const EdgeInsets.only(bottom: 16),
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.04),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.blueGrey.withValues(alpha: 0.35)),
        ),
        child: const Text(
          "Your health information is personal and treated with care. These questions help us understand your medical history. You can choose not to answer.",
          style: TextStyle(color: Colors.white70, fontSize: 13.5, height: 1.4),
        ),
      ),

      const Align(
        alignment: Alignment.centerLeft,
        child: Text(
          "Which statement best describes your history?",
          style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 15),
        ),
      ),
      const SizedBox(height: 10),

      ...statusOptions.map((st) {
        final isSel = _infectiousHistoryStatus == st;
        return _buildSelectableOption(st, isSel, () {
          setState(() {
            _infectiousHistoryStatus = st;
            _currentError = null;
            if (st != diagnosedKey) {
              _infectiousDiagnosed.clear();
              _infectiousDiagnosedNotesController.clear();
            }
            if (st != negativeKey) {
              _infectiousTestedNegative.clear();
              _infectiousLastTestedController.clear();
            }
          });
        });
      }),

      if (_infectiousHistoryStatus == diagnosedKey) ...[
        const SizedBox(height: 16),
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.04),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: Colors.blueGrey.withValues(alpha: 0.4)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                "Which infections were diagnosed?",
                style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: 14),
              ),
              const SizedBox(height: 8),
              ...infectionList.map((inf) {
                final isInfSel = _infectiousDiagnosed.contains(inf);
                return _buildSelectableOption(inf, isInfSel, () {
                  setState(() {
                    _currentError = null;
                    if (isInfSel) {
                      _infectiousDiagnosed.remove(inf);
                    } else {
                      _infectiousDiagnosed.add(inf);
                    }
                  });
                });
              }),
              const SizedBox(height: 10),
              TextField(
                controller: _infectiousDiagnosedNotesController,
                style: const TextStyle(color: Colors.white),
                decoration: const InputDecoration(
                  labelText: "Current status or additional details (optional)",
                  labelStyle: TextStyle(color: Colors.white54, fontSize: 13),
                  hintText: "e.g. Hepatitis B was diagnosed several years ago, and I currently receive medical follow-up.",
                  hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
                  enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                ),
              ),
            ],
          ),
        ),
      ],

      if (_infectiousHistoryStatus == negativeKey) ...[
        const SizedBox(height: 16),
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.04),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: Colors.blueGrey.withValues(alpha: 0.4)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                "Which infections were you tested for? (optional)",
                style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: 14),
              ),
              const SizedBox(height: 8),
              ...infectionList.map((inf) {
                final isInfSel = _infectiousTestedNegative.contains(inf);
                return _buildSelectableOption(inf, isInfSel, () {
                  setState(() {
                    _currentError = null;
                    if (isInfSel) {
                      _infectiousTestedNegative.remove(inf);
                    } else {
                      _infectiousTestedNegative.add(inf);
                    }
                  });
                });
              }),
              const SizedBox(height: 10),
              TextField(
                controller: _infectiousLastTestedController,
                style: const TextStyle(color: Colors.white),
                decoration: const InputDecoration(
                  labelText: "When were you last tested? (optional)",
                  labelStyle: TextStyle(color: Colors.white54, fontSize: 13),
                  hintText: "e.g. About 6 months ago",
                  hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
                  enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                ),
              ),
            ],
          ),
        ),
      ],

      const SizedBox(height: 24),
      _buildSelectableOption("Next ➡️", false, () {
        if (_infectiousHistoryStatus == null) {
          setState(() => _currentError = "Please select one of the options above");
          return;
        }

        if (_infectiousHistoryStatus == diagnosedKey && _infectiousDiagnosed.isEmpty) {
          setState(() => _currentError = "Please select at least one diagnosed infection");
          return;
        }

        setState(() => _currentError = null);
        vault.saveAnamnesis('infectious_history_status', _infectiousHistoryStatus);

        if (_infectiousHistoryStatus == diagnosedKey) {
          vault.saveAnamnesis('infectious_diagnosed', _infectiousDiagnosed.toList());
          vault.removeAnamnesisKeys(['infectious_tested_negative', 'infectious_last_tested']);

          final legacySummary = _infectiousDiagnosed
              .map((inf) => inf == 'Tuberculosis' ? 'Tuberculosis (TBC)' : inf)
              .join(', ');
          vault.saveAnamnesis('critical_infections', legacySummary);

          final notes = _infectiousDiagnosedNotesController.text.trim();
          if (notes.isNotEmpty) {
            vault.saveAnamnesis('user_text_critical_infections', notes, userText: notes);
            vault.saveAnamnesis('text_critical_infections', notes);
          } else {
            vault.removeAnamnesisKeys(['user_text_critical_infections', 'text_critical_infections']);
          }
        } else if (_infectiousHistoryStatus == negativeKey) {
          vault.removeAnamnesisKeys([
            'infectious_diagnosed',
            'user_text_critical_infections',
            'text_critical_infections',
          ]);

          if (_infectiousTestedNegative.isNotEmpty) {
            vault.saveAnamnesis('infectious_tested_negative', _infectiousTestedNegative.toList());
          } else {
            vault.removeAnamnesisKeys(['infectious_tested_negative']);
          }

          final lastTested = _infectiousLastTestedController.text.trim();
          if (lastTested.isNotEmpty) {
            vault.saveAnamnesis('infectious_last_tested', lastTested);
          } else {
            vault.removeAnamnesisKeys(['infectious_last_tested']);
          }

          vault.saveAnamnesis('critical_infections', 'Negative test results reported');
        } else {
          vault.removeAnamnesisKeys([
            'infectious_diagnosed',
            'infectious_tested_negative',
            'infectious_last_tested',
            'user_text_critical_infections',
            'text_critical_infections',
          ]);

          if (_infectiousHistoryStatus == 'Prefer not to answer') {
            vault.saveAnamnesis('critical_infections', 'User declined to answer');
          } else if (_infectiousHistoryStatus == "I don't know / I haven't been tested") {
            vault.saveAnamnesis('critical_infections', "Unknown / Not tested");
          } else {
            vault.saveAnamnesis('critical_infections', 'No diagnosed chronic infections');
          }
        }

        _nextPage();
      }),
    ]);
  }

  Widget _buildMatryoshkaCardHeader({
    required Key? checkboxKey,
    required String label,
    required bool isSelected,
    required VoidCallback onToggleCheckbox,
    required VoidCallback onTapTitle,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        color: isSelected ? Colors.greenAccent.withValues(alpha: 0.1) : Colors.transparent,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(
          color: isSelected ? Colors.greenAccent : Colors.white24,
          width: isSelected ? 2 : 1,
        ),
      ),
      child: Row(
        children: [
          Checkbox(
            key: checkboxKey,
            value: isSelected,
            activeColor: Colors.greenAccent,
            checkColor: Colors.black,
            side: const BorderSide(color: Colors.white54),
            onChanged: (_) => onToggleCheckbox(),
          ),
          Expanded(
            child: InkWell(
              onTap: onTapTitle,
              borderRadius: BorderRadius.circular(12),
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 6),
                child: Text(
                  label,
                  style: TextStyle(
                    color: isSelected ? Colors.greenAccent : Colors.white,
                    fontSize: 15.5,
                    fontWeight: isSelected ? FontWeight.w600 : FontWeight.normal,
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _stepHeredity() {
    final vault = context.read<VaultService>();

    const statusOptions = [
      'Yes',
      'No, as far as I know',
      "I don't know my family history",
      'Prefer not to answer',
    ];

    const relativeOptions = [
      'Mother',
      'Father',
      'Brother',
      'Sister',
      'Maternal grandmother',
      'Maternal grandfather',
      'Paternal grandmother',
      'Paternal grandfather',
      'Children',
      'Other blood relatives',
    ];

    const conditionOptions = [
      'Heart and vascular conditions',
      'Stroke',
      'Diabetes mellitus',
      'Oncological / cancer conditions',
      'Neurological conditions',
      'Mental health conditions',
      'Genetic / hereditary conditions',
      'Kidney conditions',
      'Blood clotting disorders',
      'Other conditions',
    ];

    bool isRelativeComplete(String rel) {
      final conds = _familyRelativeConditions[rel];
      if (conds == null || conds.isEmpty) return false;
      if (rel == 'Other blood relatives' && _familyOtherRelController.text.trim().isEmpty) {
        return false;
      }
      return true;
    }

    String buildRelativeSummary(String rel) {
      final conds = conditionOptions
          .where((c) => _familyRelativeConditions[rel]?.contains(c) ?? false)
          .toList();
      final condText = conds.join(', ');
      final extra = _getMapController(_familyDetailsControllers, rel).text.trim();
      final relSpec = rel == 'Other blood relatives' ? _familyOtherRelController.text.trim() : '';
      final prefix = relSpec.isNotEmpty ? '$relSpec: ' : '';
      if (extra.isNotEmpty) {
        return '$prefix$condText • $extra';
      }
      return '$prefix$condText';
    }

    return _wrapper(children: [
      _buildTitle("Family Archive 🌳"),
      _buildSubtitle("Do any of your blood relatives have medical conditions you know about?"),

      ...statusOptions.map((st) {
        final isSel = _familyHistoryStatus == st;
        return _buildSelectableOption(st, isSel, () {
          setState(() {
            _familyHistoryStatus = st;
            _currentError = null;
            if (st != 'Yes') {
              _activeFamilyRelative = null;
            }
          });
        });
      }),

      if (_familyHistoryStatus == 'Yes') ...[
        const SizedBox(height: 16),
        const Align(
          alignment: Alignment.centerLeft,
          child: Text(
            "Select affected blood relatives and add known conditions:",
            style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 15),
          ),
        ),
        const SizedBox(height: 10),

        ...relativeOptions.map((rel) {
          final isSel = _familyRelatives.contains(rel);
          final isActive = _activeFamilyRelative == rel;
          final isComplete = isRelativeComplete(rel);
          final detailsCtrl = _getMapController(_familyDetailsControllers, rel);
          final chosenConds = _familyRelativeConditions[rel] ?? <String>{};

          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _buildMatryoshkaCardHeader(
                checkboxKey: ValueKey('fh_checkbox_$rel'),
                label: rel,
                isSelected: isSel,
                onToggleCheckbox: () {
                  setState(() {
                    _currentError = null;
                    if (isSel) {
                      _familyRelatives.remove(rel);
                      _familyRelativeConditions.remove(rel);
                      detailsCtrl.clear();
                      if (rel == 'Other blood relatives') {
                        _familyOtherRelController.clear();
                      }
                      if (_activeFamilyRelative == rel) {
                        _activeFamilyRelative = null;
                      }
                    } else {
                      _familyRelatives.add(rel);
                      _familyRelativeConditions.putIfAbsent(rel, () => <String>{});
                      _activeFamilyRelative = rel;
                    }
                  });
                },
                onTapTitle: () {
                  setState(() {
                    _currentError = null;
                    if (!isSel) {
                      _familyRelatives.add(rel);
                      _familyRelativeConditions.putIfAbsent(rel, () => <String>{});
                      _activeFamilyRelative = rel;
                    } else {
                      _activeFamilyRelative = isActive ? null : rel;
                    }
                  });
                },
              ),

              if (isSel && isActive) ...[
                Container(
                  margin: const EdgeInsets.only(left: 8, right: 8, bottom: 16),
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.04),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: Colors.orangeAccent.withValues(alpha: 0.5)),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        "Family history for: $rel",
                        style: const TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 14),
                      ),
                      if (rel == 'Other blood relatives') ...[
                        const SizedBox(height: 10),
                        TextField(
                          controller: _familyOtherRelController,
                          style: const TextStyle(color: Colors.white),
                          decoration: const InputDecoration(
                            labelText: "Specify relationship",
                            labelStyle: TextStyle(color: Colors.white70, fontSize: 13),
                            hintText: "e.g. Aunt, uncle, cousin",
                            hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
                            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                          ),
                        ),
                      ],
                      const SizedBox(height: 12),
                      const Text(
                        "Known medical conditions (select all that apply):",
                        style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600),
                      ),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: conditionOptions.map((cond) {
                          final isCSel = chosenConds.contains(cond);
                          return FilterChip(
                            label: Text(
                              cond,
                              style: TextStyle(color: isCSel ? Colors.black : Colors.white, fontSize: 12),
                            ),
                            selected: isCSel,
                            selectedColor: Colors.orangeAccent,
                            backgroundColor: Colors.white12,
                            checkmarkColor: Colors.black,
                            onSelected: (_) {
                              setState(() {
                                _currentError = null;
                                final setForRel = _familyRelativeConditions.putIfAbsent(rel, () => <String>{});
                                if (isCSel) {
                                  setForRel.remove(cond);
                                } else {
                                  setForRel.add(cond);
                                }
                              });
                            },
                          );
                        }).toList(),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: detailsCtrl,
                        style: const TextStyle(color: Colors.white),
                        maxLines: null,
                        decoration: const InputDecoration(
                          labelText: "Additional details (optional)",
                          labelStyle: TextStyle(color: Colors.white70, fontSize: 13),
                          hintText: "e.g. Type 2 diabetes, hypertension, colon cancer, other details",
                          hintStyle: TextStyle(color: Colors.white24, fontSize: 12),
                          enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                        ),
                      ),
                      const SizedBox(height: 12),
                      Align(
                        alignment: Alignment.centerRight,
                        child: ElevatedButton(
                          style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                          onPressed: () {
                            if (rel == 'Other blood relatives' &&
                                _familyOtherRelController.text.trim().isEmpty) {
                              setState(() {
                                _currentError = "Please specify the relationship for «Other blood relatives»";
                              });
                              return;
                            }
                            final conds = _familyRelativeConditions[rel];
                            if (conds == null || conds.isEmpty) {
                              setState(() {
                                _currentError = "Please select at least one condition for «$rel»";
                              });
                              return;
                            }
                            setState(() {
                              _currentError = null;
                              _activeFamilyRelative = null;
                            });
                          },
                          child: const Text(
                            "Save and collapse",
                            style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ] else if (isSel && !isActive && isComplete) ...[
                Padding(
                  padding: const EdgeInsets.only(left: 12, bottom: 10),
                  child: InkWell(
                    onTap: () => setState(() => _activeFamilyRelative = rel),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            buildRelativeSummary(rel),
                            style: const TextStyle(color: Colors.orangeAccent, fontSize: 12.5),
                          ),
                        ),
                        const Text("[Edit ✏️]", style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                      ],
                    ),
                  ),
                ),
              ] else if (isSel && !isActive && !isComplete) ...[
                Padding(
                  padding: const EdgeInsets.only(left: 12, bottom: 10),
                  child: InkWell(
                    onTap: () => setState(() => _activeFamilyRelative = rel),
                    child: const Text(
                      "⚠️ Incomplete details: tap to complete",
                      style: TextStyle(color: Colors.amberAccent, fontSize: 12),
                    ),
                  ),
                ),
              ],
            ],
          );
        }),
      ],

      const SizedBox(height: 20),

      _buildSelectableOption("Next ➡️", false, () {
        if (_familyHistoryStatus == null) {
          setState(() => _currentError = "Please select one of the options above");
          return;
        }

        if (_familyHistoryStatus == 'Yes') {
          if (_familyRelatives.isEmpty) {
            setState(() => _currentError = "Please select at least one blood relative or change your answer above");
            return;
          }

          final orderedRelatives = relativeOptions.where((r) => _familyRelatives.contains(r)).toList();
          final detailsPayload = <String, Map<String, dynamic>>{};
          final summaryParts = <String>[];

          for (final rel in orderedRelatives) {
            if (rel == 'Other blood relatives' &&
                _familyOtherRelController.text.trim().isEmpty) {
              setState(() {
                _activeFamilyRelative = rel;
                _currentError = "Please specify the relationship for «Other blood relatives»";
              });
              return;
            }
            final chosen = _familyRelativeConditions[rel] ?? <String>{};
            final orderedConds = conditionOptions.where((c) => chosen.contains(c)).toList();
            if (orderedConds.isEmpty) {
              setState(() {
                _activeFamilyRelative = rel;
                _currentError = "Please select at least one condition for «$rel»";
              });
              return;
            }

            final relMap = <String, dynamic>{
              'conditions': orderedConds,
            };
            if (rel == 'Other blood relatives') {
              relMap['relationship_detail'] = _familyOtherRelController.text.trim();
            }
            final notesText = _getMapController(_familyDetailsControllers, rel).text.trim();
            if (notesText.isNotEmpty) {
              relMap['details'] = notesText;
            }
            detailsPayload[rel] = relMap;

            final relLabel = (rel == 'Other blood relatives' && relMap['relationship_detail'] != null)
                ? "$rel (${relMap['relationship_detail']})"
                : rel;
            summaryParts.add("$relLabel: ${orderedConds.join(', ')}");
          }

          setState(() => _currentError = null);
          vault.saveAnamnesis('family_history_status', 'Yes');
          vault.saveAnamnesis('family_history_relatives', orderedRelatives);
          vault.saveAnamnesis('family_history_details', detailsPayload);
          vault.saveAnamnesis('heredity_risks', _clampSummary(summaryParts.join('; ')));
          vault.removeAnamnesisKeys(['user_text_heredity_risks', 'text_heredity_risks']);
        } else {
          setState(() => _currentError = null);
          vault.saveAnamnesis('family_history_status', _familyHistoryStatus);
          vault.saveAnamnesis('heredity_risks', _familyHistoryStatus!);
          vault.removeAnamnesisKeys([
            'family_history_relatives',
            'family_history_details',
            'user_text_heredity_risks',
            'text_heredity_risks',
          ]);
        }

        _nextPage();
      }),
    ]);
  }

  void _archiveMedicationDraft(String cat) {
    final nameText = _getMapController(_medicationNameControllers, cat).text;
    final courseText = _getMapController(_medicationCourseControllers, cat).text;
    if (nameText.trim().isNotEmpty || courseText.trim().isNotEmpty) {
      _medicationDraftArchive[cat] = {
        'names': nameText,
        'course': courseText,
      };
    } else {
      _medicationDraftArchive.remove(cat);
    }
  }

  void _uncheckMedicationCategory(String cat) {
    _archiveMedicationDraft(cat);
    _medicationCategories.remove(cat);
    if (_activeMedicationCategory == cat) {
      _activeMedicationCategory = null;
    }
  }

  void _checkAndOpenMedicationCategory(String cat) {
    final nameCtrl = _getMapController(_medicationNameControllers, cat);
    final courseCtrl = _getMapController(_medicationCourseControllers, cat);
    final archived = _medicationDraftArchive[cat];
    if (archived != null) {
      if (nameCtrl.text.isEmpty && (archived['names'] ?? '').isNotEmpty) {
        nameCtrl.text = archived['names']!;
      }
      if (courseCtrl.text.isEmpty && (archived['course'] ?? '').isNotEmpty) {
        courseCtrl.text = archived['course']!;
      }
    }
    _medicationCategories.add(cat);
    _activeMedicationCategory = cat;
  }

  void _permanentlyClearMedicationCategory(String cat) {
    _medicationDraftArchive.remove(cat);
    _medicationNameControllers[cat]?.clear();
    _medicationCourseControllers[cat]?.clear();
  }

  Widget _stepMeds() {
    const statusOptions = [
      'Yes',
      'No',
      'Not sure',
      'Prefer not to answer',
    ];

    const categoryOptions = [
      'Prescription medications',
      'Over-the-counter medications',
      'Vitamins',
      'Supplements and herbal products',
    ];

    const nameHints = <String, String>{
      'Prescription medications': 'e.g. Amoxicillin, tablets; physician-prescribed injections',
      'Over-the-counter medications': 'e.g. Ibuprofen, tablets; nasal spray',
      'Vitamins': 'e.g. Vitamin B6, intramuscular injections; Vitamin D, tablets',
      'Supplements and herbal products': 'e.g. Ashwagandha, herbal extract; other dietary supplements',
    };

    const courseHints = <String, String>{
      'Prescription medications': 'e.g. I have been taking a prescribed antibiotic for a week, started November 10',
      'Over-the-counter medications': 'e.g. I only take a painkiller for headaches',
      'Vitamins': 'e.g. I take a 1-month course followed by a 2-week break',
      'Supplements and herbal products': 'e.g. I take a 1-month course followed by a 2-week break',
    };

    bool hasArchivedDraft(String cat) {
      final archived = _medicationDraftArchive[cat];
      if (archived != null &&
          ((archived['names'] ?? '').trim().isNotEmpty ||
              (archived['course'] ?? '').trim().isNotEmpty)) {
        return true;
      }
      final nText = _medicationNameControllers[cat]?.text.trim() ?? '';
      final cText = _medicationCourseControllers[cat]?.text.trim() ?? '';
      return nText.isNotEmpty || cText.isNotEmpty;
    }

    String buildCategorySummary(String cat) {
      final names = _getMapController(_medicationNameControllers, cat).text.trim();
      final course = _getMapController(_medicationCourseControllers, cat).text.trim();
      if (course.isNotEmpty) {
        return '$names • $course';
      }
      return names;
    }

    return _wrapper(children: [
      _buildTitle("Medications and Supplements 💊"),
      _buildSubtitle("Do you currently take any medications, vitamins, or supplements?"),

      ...statusOptions.map((st) {
        final isSel = _medicationsStatus == st;
        return _buildSelectableOption(st, isSel, () {
          setState(() {
            if (_medicationsStatus == 'Yes' && st != 'Yes') {
              for (final cat in _medicationCategories.toList()) {
                _archiveMedicationDraft(cat);
              }
              _medicationCategories.clear();
              _activeMedicationCategory = null;
            }
            _medicationsStatus = st;
            _currentError = null;
          });
        });
      }),

      if (_medicationsStatus == 'Yes') ...[
        const SizedBox(height: 16),
        const Align(
          alignment: Alignment.centerLeft,
          child: Text(
            "Select all categories you currently take and fill in details:",
            style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 15),
          ),
        ),
        const SizedBox(height: 10),

        ...categoryOptions.map((cat) {
          final isSel = _medicationCategories.contains(cat);
          final isActive = _activeMedicationCategory == cat;
          final nameCtrl = _getMapController(_medicationNameControllers, cat);
          final courseCtrl = _getMapController(_medicationCourseControllers, cat);
          final hasNames = nameCtrl.text.trim().isNotEmpty;

          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _buildMatryoshkaCardHeader(
                checkboxKey: ValueKey('med_checkbox_$cat'),
                label: cat,
                isSelected: isSel,
                onToggleCheckbox: () {
                  setState(() {
                    _currentError = null;
                    if (isSel) {
                      _uncheckMedicationCategory(cat);
                    } else {
                      _checkAndOpenMedicationCategory(cat);
                    }
                  });
                },
                onTapTitle: () {
                  setState(() {
                    _currentError = null;
                    if (!isSel) {
                      _checkAndOpenMedicationCategory(cat);
                    } else {
                      _activeMedicationCategory = isActive ? null : cat;
                    }
                  });
                },
              ),

              if (!isSel && hasArchivedDraft(cat)) ...[
                Padding(
                  padding: const EdgeInsets.only(left: 12, bottom: 10),
                  child: Row(
                    children: [
                      const Expanded(
                        child: Text(
                          "Draft preserved — re-select category to restore",
                          style: TextStyle(color: Colors.amberAccent, fontSize: 12),
                        ),
                      ),
                      TextButton(
                        onPressed: () {
                          setState(() {
                            _currentError = null;
                            _permanentlyClearMedicationCategory(cat);
                          });
                        },
                        child: const Text("Discard draft", style: TextStyle(color: Colors.redAccent, fontSize: 12)),
                      ),
                    ],
                  ),
                ),
              ],

              if (isSel && isActive) ...[
                Container(
                  margin: const EdgeInsets.only(left: 8, right: 8, bottom: 16),
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.04),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: Colors.orangeAccent.withValues(alpha: 0.5)),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        "Details for: $cat",
                        style: const TextStyle(color: Colors.orangeAccent, fontWeight: FontWeight.bold, fontSize: 14),
                      ),
                      const SizedBox(height: 10),
                      TextField(
                        controller: nameCtrl,
                        style: const TextStyle(color: Colors.white),
                        maxLines: null,
                        keyboardType: TextInputType.multiline,
                        onChanged: (_) => _archiveMedicationDraft(cat),
                        decoration: InputDecoration(
                          labelText: "Medication or supplement names & form / route",
                          labelStyle: const TextStyle(color: Colors.white70, fontSize: 13),
                          hintText: nameHints[cat],
                          hintStyle: const TextStyle(color: Colors.white24, fontSize: 12),
                          helperText: "You can list multiple items separated by commas or new lines",
                          helperStyle: const TextStyle(color: Colors.white38, fontSize: 11),
                          enabledBorder: const UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: courseCtrl,
                        style: const TextStyle(color: Colors.white),
                        maxLines: null,
                        keyboardType: TextInputType.multiline,
                        onChanged: (_) => _archiveMedicationDraft(cat),
                        decoration: InputDecoration(
                          labelText: "How long and how have you been taking them? (optional)",
                          labelStyle: const TextStyle(color: Colors.white70, fontSize: 13),
                          hintText: courseHints[cat],
                          hintStyle: const TextStyle(color: Colors.white24, fontSize: 12),
                          enabledBorder: const UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                        ),
                      ),
                      const SizedBox(height: 12),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          TextButton(
                            onPressed: () {
                              setState(() {
                                _currentError = null;
                                _permanentlyClearMedicationCategory(cat);
                              });
                            },
                            child: const Text("Clear text", style: TextStyle(color: Colors.redAccent, fontSize: 12.5)),
                          ),
                          ElevatedButton(
                            style: ElevatedButton.styleFrom(backgroundColor: Colors.orangeAccent),
                            onPressed: () {
                              if (nameCtrl.text.trim().isEmpty) {
                                setState(() {
                                  _currentError = "Please enter at least one medication or supplement name for «$cat»";
                                });
                                return;
                              }
                              _archiveMedicationDraft(cat);
                              setState(() {
                                _currentError = null;
                                _activeMedicationCategory = null;
                              });
                            },
                            child: const Text(
                              "Save and collapse",
                              style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ] else if (isSel && !isActive && hasNames) ...[
                Padding(
                  padding: const EdgeInsets.only(left: 12, bottom: 10),
                  child: InkWell(
                    onTap: () => setState(() => _activeMedicationCategory = cat),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            buildCategorySummary(cat),
                            style: const TextStyle(color: Colors.orangeAccent, fontSize: 12.5),
                          ),
                        ),
                        const Text("[Edit ✏️]", style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                      ],
                    ),
                  ),
                ),
              ] else if (isSel && !isActive && !hasNames) ...[
                Padding(
                  padding: const EdgeInsets.only(left: 12, bottom: 10),
                  child: InkWell(
                    onTap: () => setState(() => _activeMedicationCategory = cat),
                    child: const Text(
                      "⚠️ Incomplete details: tap to enter medication name",
                      style: TextStyle(color: Colors.amberAccent, fontSize: 12),
                    ),
                  ),
                ),
              ],
            ],
          );
        }),
      ],

      const SizedBox(height: 20),

      _buildSelectableOption("Finish 🚀", false, () async {
        if (_isSubmitting) return;

        final vault = context.read<VaultService>();

        if (_medicationsStatus == null) {
          setState(() => _currentError = "Please select one of the options above");
          return;
        }

        if (_medicationsStatus == 'Yes') {
          if (_medicationCategories.isEmpty) {
            setState(() => _currentError = "Please select at least one category or change your answer above");
            return;
          }

          final orderedCats = categoryOptions.where((c) => _medicationCategories.contains(c)).toList();
          final detailsPayload = <String, Map<String, String>>{};
          final summaryParts = <String>[];

          for (final cat in orderedCats) {
            final names = _getMapController(_medicationNameControllers, cat).text.trim();
            if (names.isEmpty) {
              setState(() {
                _activeMedicationCategory = cat;
                _currentError = "Please enter at least one medication or supplement name for «$cat»";
              });
              return;
            }
            final course = _getMapController(_medicationCourseControllers, cat).text.trim();
            final catMap = <String, String>{'names': names};
            if (course.isNotEmpty) {
              catMap['course'] = course;
            }
            detailsPayload[cat] = catMap;
            summaryParts.add("$cat ($names)");
          }

          vault.saveAnamnesis('medications_status', 'Yes');
          vault.saveAnamnesis('medication_categories', orderedCats);
          vault.saveAnamnesis('medication_details', detailsPayload);
          vault.saveAnamnesis('medications', _clampSummary(summaryParts.join('; ')));
          vault.removeAnamnesisKeys(['user_text_medications', 'text_medications']);
        } else {
          vault.saveAnamnesis('medications_status', _medicationsStatus);
          vault.saveAnamnesis('medications', _medicationsStatus!);
          vault.removeAnamnesisKeys([
            'medication_categories',
            'medication_details',
            'user_text_medications',
            'text_medications',
          ]);
        }

        setState(() {
          _currentError = null;
          _isSubmitting = true;
        });

        try {
          final auth = Provider.of<AuthSessionManager?>(context, listen: false);
          final apiClient = Provider.of<AegisApiClient?>(context, listen: false);

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
  }

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
      _activeChronicCategory = null;
      _activeRecentEvent = null;
      _activeFamilyRelative = null;
      _activeMedicationCategory = null;
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