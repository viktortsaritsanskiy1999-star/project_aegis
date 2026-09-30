import 'package:flutter/foundation.dart';
import 'vault_storage_service.dart';
import 'vitals_alert_context.dart';

class VaultService extends ChangeNotifier {
  // Your data for Atlas
  final Map<String, dynamic> _data = {
    "health_systems": {
      "cvs": {"name": "Cardiovascular", "completeness": 0.0, "color": "0xFFFF0000", "markers": {}},
      "gi_tract": {"name": "GI Tract", "completeness": 0.0, "color": "0xFFFFA500", "markers": {}},
      "respiratory": {"name": "Respiratory", "completeness": 0.0, "color": "0xFF00FFFF", "markers": {}},
      "nervous": {"name": "Nervous", "completeness": 0.0, "color": "0xFF8A2BE2", "markers": {}},
      "musculoskeletal": {"name": "Musculoskeletal", "completeness": 0.0, "color": "0xFFB0C4DE", "markers": {}},
      "kidneys": {"name": "Renal", "completeness": 0.0, "color": "0xFF0000FF", "markers": {}},
      "immune": {"name": "Immune and Blood", "completeness": 0.0, "color": "0xFF32CD32", "markers": {}},
      "endocrine": {"name": "Endocrine", "completeness": 0.0, "color": "0xFFEE82EE", "markers": {}},
    }
  };

  // Flag for the red badge "1" and Athena notifications
  bool _hasUnreadAthenaMessage = false;
  bool get hasUnreadAthenaMessage => _hasUnreadAthenaMessage;

  // Storage for Curator chat history
  final List<Map<String, String>> _curatorChatHistory = [];

  // System folders + Training sandbox + New shelves according to cascade plan + DEDICATED FOLDER FOR NOTES
  final Map<String, Map<String, dynamic>> _systemFolders = {
    'respiratory': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'immune': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'cvs': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'gi_tract': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'kidneys': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'endocrine': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'nervous': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },
    'musculoskeletal': {
      'labs': <String, dynamic>{},
      'system_general_shelf': 'Analysis not performed',
      'system_training_shelf': 'Logs are empty',
    },

    // COMMON FOLDER FOR MAIN MODELS (4 new shelves at the top of the interface)
    'core_cascade': {
      'flash_general_shelf': 'Waiting for Gemini Flash output...',
      'flash_training_shelf': 'Gemini Flash deficiency logs...',
      'pro_general_shelf': 'Waiting for Gemini Pro output...',
      'pro_training_shelf': 'Gemini Pro deficiency logs...',
    },

    // DEDICATED FOLDER FOR NOTES MIRRORING
    'notes_export': <String, dynamic>{},

    'global': {'labs': <String, dynamic>{}},
    'training_sandbox': {
      'raw_predictions': [],
      'missing_data': [],
      'risk_analysis': "PENDING...",
    },
  };

  String? _selectedSystem;

  VaultService() {
    _initVault();
  }

  Future<void> _initVault() async {
    final loadedData = await VaultStorageService.loadVault();
    if (loadedData.isNotEmpty) {
      if (loadedData.containsKey('health_systems')) {
        _data['health_systems'] = loadedData['health_systems'];
      }
      if (loadedData.containsKey('system_folders')) {
        final savedFolders = loadedData['system_folders'] as Map<String, dynamic>;
        savedFolders.forEach((key, value) {
          if (_systemFolders.containsKey(key) && value is Map) {
            _systemFolders[key] = Map<String, dynamic>.from(value);
          }
        });
      }
      if (loadedData.containsKey('curator_chat_history')) {
        final savedChat = loadedData['curator_chat_history'] as List;
        _curatorChatHistory.clear();
        for (var item in savedChat) {
          if (item is Map) {
            _curatorChatHistory.add(Map<String, String>.from(item));
          }
        }
      }
      if (loadedData.containsKey('athena_clinical_logs')) {
        final savedLogs = loadedData['athena_clinical_logs'] as List;
        _athenaClinicalLogs.clear();
        for (var item in savedLogs) {
          if (item is Map) {
            _athenaClinicalLogs.add(Map<String, dynamic>.from(item));
          }
        }
      }

      // 🎯 New: load vital sign thresholds if already established by the Chief Physician
      if (loadedData.containsKey('vitals_thresholds')) {
        _systemFolders['global']?['vitals_thresholds'] = loadedData['vitals_thresholds'];
      }

      notifyListeners();
    }
  }

  void _persist() {
    final Map<String, dynamic> snapshot = {
      'health_systems': _data['health_systems'],
      'system_folders': _systemFolders,
      'curator_chat_history': _curatorChatHistory,
      'vitals_thresholds': _systemFolders['global']?['vitals_thresholds'],
      'athena_clinical_logs': _athenaClinicalLogs, // 🎯 New
    };
    VaultStorageService.saveVault(snapshot);
  }

  String? get selectedSystem => _selectedSystem;
  Map<String, dynamic> get vaultData => _data;

  // Internal helper for automatic mirroring to the notes folder
  void _mirrorToNotesExport(String key, dynamic value) {
    _systemFolders['notes_export']?[key] = {
      'value': value,
      'timestamp': DateTime.now().toIso8601String(),
    };
  }

  // Getter for retrieving context
  Map<String, dynamic> getSystemContext(String systemKey) {
    return _systemFolders[systemKey] ?? {};
  }

  void selectSystem(String? key) {
    _selectedSystem = key;
    notifyListeners();
  }

  // Getters for quick reading of vital markers from smart watch / Atlas
  Map<String, dynamic> get _smartWatchData {
    try {
      final cvs = _data['health_systems']?['cvs'] as Map<String, dynamic>?;
      final markers = cvs?['markers'] as Map<String, dynamic>?;
      return markers?['smart_watch'] as Map<String, dynamic>? ?? {};
    } catch (_) {
      return {};
    }
  }

  int? get systolic => _smartWatchData['pressure']?['systolic'] as int?;
  int? get diastolic => _smartWatchData['pressure']?['diastolic'] as int?;
  int? get pulse => _smartWatchData['pulse']?['value'] as int?;
  int? get spo2 => _smartWatchData['spo2']?['value'] as int?;
  String? get ecgSummary => _smartWatchData['ecg_status']?['value'] as String?;

  // METHOD FOR SAVING ANAMNESIS
  void saveAnamnesis(String cardId, dynamic value, {String userText = ""}) {
    _systemFolders['global']![cardId] = value;
    if (userText.isNotEmpty) {
      _systemFolders['global']!['text_$cardId'] = userText;
    }

    _mirrorToNotesExport('anamnesis_$cardId', value);
    if (userText.isNotEmpty) {
      _mirrorToNotesExport('anamnesis_text_$cardId', userText);
    }

    final String valueString = value.toString().toLowerCase();

    switch (cardId) {
      case 'gender':
        _systemFolders['endocrine']?['user_gender'] = value;
        _systemFolders['kidneys']?['user_gender'] = value;
        _systemFolders['cvs']?['user_gender'] = value;
        break;

      case 'age':
        break;

      case 'height':
      case 'weight':
        _systemFolders['endocrine']?['physical_$cardId'] = value;
        _systemFolders['cvs']?['load_$cardId'] = value;
        _systemFolders['musculoskeletal']?['axial_load_$cardId'] = value;
        break;

      case 'nicotine_status':
      case 'nicotine_years':
      case 'nicotine_freq':
        _systemFolders['respiratory']?['smoking_$cardId'] = value;
        _systemFolders['cvs']?['vessel_$cardId'] = value;
        _systemFolders['immune']?['broncho_local_imm_disruption'] = true;
        break;

      case 'alcohol_status':
      case 'alcohol_type':
        _systemFolders['gi_tract']?['liver_tox_load'] = "Alcohol consumption recorded";
        _systemFolders['nervous']?['gaba_neuro_load'] = "Ethanol-induced GABA modulation";

        if (valueString.contains('пиво') || valueString.contains('beer')) {
          _systemFolders['kidneys']?['uric_acid_risk'] = true;
          _systemFolders['endocrine']?['visceral_adipose_trigger'] = true;
        }
        if (valueString.contains('крепкое') || valueString.contains('вино') || valueString.contains('vodka')) {
          _systemFolders['cvs']?['cardiomyopathy_risk'] = true;
        }
        break;

      case 'spec_factors':
        if (valueString.contains('стимуляция') || valueString.contains('кофеин') || valueString.contains('предтреник')) {
          _systemFolders['nervous']?['synaptic_overdrive'] = "Stimulation (caffeine, pre-workouts)";
          _systemFolders['cvs']?['vasoconstriction_tachy_response'] = "Vasoconstrictor potential";
          _systemFolders['gi_tract']?['mucosal_ischemia_hyperacidity'] = "Risk of hyperacidity / mucosal ischemia";
        }
        if (valueString.contains('седация') || valueString.contains('антидепрессант')) {
          _systemFolders['nervous']?['cns_stabilization_mod'] = "Pharmacological sedation (BP)";
        }
        if (valueString.contains('ингаляции') || valueString.contains('вейп')) {
          _systemFolders['respiratory']?['alveolar_stress'] = "Aerosol / respiratory alveolar stress";
          _systemFolders['nervous']?['heb_psych_effect'] = "Active inhalation components";
        }
        if (valueString.contains('восстановление')) {
          _systemFolders['nervous']?['receptor_downregulation_history'] = "Receptor apparatus recovery period";
        }
        break;

      case 'sleep_quality':
        _systemFolders['endocrine']?['circadian_desynchronosis'] = "Circadian desynchronosis (Sleep quality: $value)";
        _systemFolders['nervous']?['glymphatic_clearance_disruption'] = (value == 'Бессонница' || value == 'Сбит режим');
        break;

      case 'diet_types':
        if (valueString.contains('мясо') || valueString.contains('meat')) {
          _systemFolders['gi_tract']?['protease_load'] = "High protease demand (meat diet)";
          _systemFolders['kidneys']?['nitrogen_waste_load'] = "Increased nitrogen waste load";
        }
        if (valueString.contains('овощи') || valueString.contains('vegetables')) {
          _systemFolders['gi_tract']?['microbiome_substrate'] = "Sufficient dietary fiber";
        }
        if (valueString.contains('фастфуд') || valueString.contains('fastfood')) {
          _systemFolders['endocrine']?['insulin_spike_risk'] = "High glycemic load index (fast food)";
          _systemFolders['cvs']?['endothelial_lipid_damage'] = "Atherogenic dietary profile (trans fats)";
        }
        if (valueString.contains('рыба') || valueString.contains('fish')) {
          _systemFolders['cvs']?['omega3_cardioprotection'] = "Omega-3 protection active";
        }
        if (valueString.contains('молочн') || valueString.contains('milk')) {
          _systemFolders['gi_tract']?['lactase_check'] = "Consumption of lactose-containing products";
          _systemFolders['musculoskeletal']?['minor_calcium_source'] = "Dietary calcium source in the diet";
        }
        if (valueString.contains('vegan') || valueString.contains('веган')) {
          _systemFolders['immune']?['leukopoiesis_deficiency_risk'] = true;
          _systemFolders['cvs']?['hyperhomocysteinemia_endothelial_destruction'] = true;
        }
        break;

      case 'diet_habits':
        if (valueString.contains('сладкое') || valueString.contains('sugar')) {
          _systemFolders['endocrine']?['carb_metabolism_glycation'] = "Excess simple carbohydrates (glycation risk)";
        }
        if (valueString.contains('солен') || valueString.contains('salt')) {
          _systemFolders['cvs']?['volemic_hypertension_load'] = "Sodium-dependent volemic load";
          _systemFolders['kidneys']?['tubular_osmotic_load'] = "Increased tubular osmotic load";
        }
        if (valueString.contains('кофе') || valueString.contains('coffee')) {
          _systemFolders['nervous']?['adenosine_receptor_blockade'] = "Chronic adenosine receptor blockade";
          _systemFolders['cvs']?['arrhythmogenic_potential_bp_fluctuation'] = "Catecholamine blood pressure shift (caffeine)";
          _systemFolders['gi_tract']?['gastric_acid_stimulation'] = "Stimulation of HCl secretion by parietal cells";
        }
        if (valueString.contains('дожор') || valueString.contains('night')) {
          _systemFolders['gi_tract']?['nocturnal_reflux_risk'] = "Risk of nocturnal reflux";
          _systemFolders['endocrine']?['nocturnal_sth_blockade'] = "Risk of suppressing nocturnal GH (STH) secretion";
        }
        if (valueString.contains('мало воды') || valueString.contains('water')) {
          _systemFolders['kidneys']?['hypersthenuria_urolithiasis_risk'] = "Risk of hypersthenuria and urolithiasis";
          _systemFolders['cvs']?['hemoconcentration_viscosity_load'] = "Hemoconcentration syndrome (circulating blood volume deficit)";
        }
        break;

      case 'gi_signals':
        _systemFolders['gi_tract']?['symptoms_motility_sibr'] = "Dyspepsia symptoms / GI motility disorders recorded";
        break;

      case 'work_type':
      case 'work_schedule':
      case 'sport_level':
        _systemFolders['musculoskeletal']?['biomechanics_$cardId'] = "Biomechanics specificity: $value";
        _systemFolders['cvs']?['cardio_adaptation_index'] = "Physiological adaptation level: $value";
        if (valueString.contains('смен') || valueString.contains('ноч') || valueString.contains('без выходных')) {
          _systemFolders['nervous']?['sympathicotonia_stress'] = "Chronic sympathicotonia (stress-induced)";
          _systemFolders['endocrine']?['adrenal_distress_cortisol'] = "Signs of adrenal distress (cortisol shift)";
        }
        break;

      case 'chronic_diseases':
        if (valueString.contains('диабет') || valueString.contains('diabetes')) {
          _systemFolders['endocrine']?['primary_defect_db'] = "History of diabetes mellitus";
          _systemFolders['cvs']?['macroangiopathy_accelerated_atherosclerosis'] = "Accelerated atherosclerosis (diabetic macroangiopathy)";
          _systemFolders['kidneys']?['diabetic_nephropathy_hyperfiltration'] = "Risk of diabetic nephropathy / hyperfiltration";
        }
        if (valueString.contains('давлен') || valueString.contains('гипертенз') || valueString.contains('сердце')) {
          _systemFolders['cvs']?['chronic_cvs_pathology'] = "Arterial hypertension / Chronic cardiovascular pathology";
        }
        if (valueString.contains('астма') || valueString.contains('аллерг')) {
          _systemFolders['respiratory']?['bronchial_hyperreactivity'] = "Bronchial hyperreactivity";
          _systemFolders['immune']?['th2_atopic_shif_ige'] = "Atopic immune formula shift (Th2-profile)";
        }
        if (valueString.contains('сустав') || valueString.contains('спина') || valueString.contains('позвоночник')) {
          _systemFolders['musculoskeletal']?['chronic_degradation_inflammation'] = "Chronic degenerative-inflammatory musculoskeletal lesion";
        }
        if (valueString.contains('невролог') || valueString.contains('эпилепси')) {
          _systemFolders['nervous']?['chronic_cns_pns_pathology'] = "Organic or functional CNS/PNS pathology";
        }
        break;

      case 'onco_status':
        _systemFolders['immune']?['antitumor_surveillance_deficit'] = "Increased oncological alertness";
        break;

      case 'recent_illness':
        _systemFolders['immune']?['postviral_exhaustion_formula_shift'] = "Post-viral asthenia syndrome / leukogram shift";
        _systemFolders['respiratory']?['residual_lung_markers'] = "Residual lung tissue changes";
        if (valueString.contains('отравление') || valueString.contains('кишечная')) {
          _systemFolders['gi_tract']?['mucosal_barrier_destruction_malabsorption'] = "Destruction of the small intestine mucosal barrier";
          _systemFolders['kidneys']?['prerenal_dehydration_risk'] = true;
        }
        break;

      case 'critical_infections':
        if (valueString.contains('гепатит') || valueString.contains('hepatitis')) {
          _systemFolders['gi_tract']?['hepatocyte_cytolysis_fibrosis'] = "Chronic viral hepatitis (risk of liver fibrosis)";
          _systemFolders['immune']?['chronic_viral_persistence_hep'] = "Prolonged viral persistence";
        }
        if (valueString.contains('вич') || valueString.contains('hiv')) {
          _systemFolders['immune']?['cd4_destruction_immunodeficiency'] = "HIV-positive status (risk of T-cell deficiency)";
        }
        if (valueString.contains('туберкулез') || valueString.contains('tbc')) {
          _systemFolders['respiratory']?['tbc_lung_lesions'] = "History of specific pulmonary tuberculosis";
          _systemFolders['immune']?['specific_cellular_response'] = "Activation of specific cellular response";
        }
        if (valueString.contains('сифилис') || valueString.contains('syphilis')) {
          _systemFolders['immune']?['specific_treponema_antibodies'] = "Specific antibodies to Treponema pallidum";
          _systemFolders['nervous']?['neurosyphilis_organic_risk'] = true;
          _systemFolders['cvs']?['mesoaortitis_vascular_risk'] = true;
          _systemFolders['kidneys']?['membranous_nephropathy_immune_complex'] = true;
        }
        break;

      case 'heredity_risks':
        if (valueString.contains('инфаркт') || valueString.contains('инсульт')) {
          _systemFolders['cvs']?['genetic_dyslipidemia_atherosclerosis'] = "Burdened family history of CVD (early myocardial infarctions/strokes)";
        }
        if (valueString.contains('онко') || valueString.contains('рак')) {
          _systemFolders['immune']?['genetic_tumor_surveillance_weakness'] = "Family oncological history";
        }
        if (valueString.contains('диабет')) {
          _systemFolders['endocrine']?['polygenic_diabetes_predisposition'] = "Polygenic predisposition to carbohydrate metabolism disorders";
        }
        if (valueString.contains('альцгеймер') || valueString.contains('деменци')) {
          _systemFolders['nervous']?['tau_amyloidosis_genetic_risk'] = "Genetic risk of early neurodegeneration";
        }
        break;

      case 'medications':
        if (valueString.contains('кок') || valueString.contains('гормон')) {
          _systemFolders['endocrine']?['exogenous_hormonal_background'] = "Exogenous steroid intake / hormone therapy";
          _systemFolders['cvs']?['estrogen_hypercoagulation_thrombosis_risk'] = "Estrogen-induced hypercoagulability (risk of thrombosis)";
        }
        if (valueString.contains('статин')) {
          _systemFolders['cvs']?['lipid_management_pharmacology'] = "Pharmacological lipid profile control (statins)";
          _systemFolders['gi_tract']?['statin_liver_transaminase_control'] = "Transaminase monitoring (risk of drug-induced hepatitis)";
        }
        if (valueString.contains('нпвс') || valueString.contains('обезбол') || valueString.contains('ибупрофен') || valueString.contains('аспирин')) {
          _systemFolders['gi_tract']?['cox1_inhibition_gastropathy_erosion'] = "NSAID-induced gastropathy (COX-1 blockade)";
          _systemFolders['kidneys']?['prostaglandin_block_afferent_arteriole_spasm'] = "Afferent arteriole spasm (risk of GFR decrease)";
          _systemFolders['cvs']?['nsaid_hypertension_thrombotic_shift'] = "Prohypertensive effect of NSAIDs / sodium retention";
        }
        break;
    }

    notifyListeners();
  }

// ==========================================
// CHIEF PHYSICIAN THRESHOLD METHODS
// ==========================================

  /// Saving individual vital sign thresholds from the Chief Physician
  void saveVitalsThresholds(Map<String, dynamic> thresholdsJson) {
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['vitals_thresholds'] = thresholdsJson;
    _mirrorToNotesExport('vitals_thresholds', thresholdsJson);
    _persist();
    notifyListeners();
    debugPrint("✅ [VaultService]: Chief Physician vital sign thresholds saved!");
  }

  /// Retrieving established thresholds (returns null if the Chief Physician hasn't issued a verdict yet)
  VitalsThresholds? getVitalsThresholds() {
    final rawData = _systemFolders['global']?['vitals_thresholds'];
    if (rawData == null || rawData is! Map<String, dynamic>) {
      return null;
    }

    try {
      return VitalsThresholds.fromJson(rawData);
    } catch (e) {
      debugPrint("❌ [VaultService Error]: VitalsThresholds parsing error: $e");
      return null;
    }
  }

// METHOD FOR GEMINI
  void updateSystemFolder(String systemKey, Map<String, dynamic> data) {
    if (_systemFolders.containsKey(systemKey)) {
      _systemFolders[systemKey]!.addAll(data);
      _mirrorToNotesExport('system_update_$systemKey', data);
      _persist();
      notifyListeners();
    }
  }

// Progress calculation
  void _calculateProgress(String key) {
    var markers = _data['health_systems'][key]['markers'] as Map;
    if (markers.isEmpty) return;
    int filled = markers.values.where((v) => v != 0 && v != null).length;
    _data['health_systems'][key]['completeness'] = (filled / markers.length) * 100;
  }

// Method for automatic injection of lab markers
  void injectLabMarker(String systemKey, String markerKey, double value, String unit) {
    final systems = _data['health_systems'] as Map<String, dynamic>;
    if (systems.containsKey(systemKey)) {
      final markers = systems[systemKey]['markers'] as Map<String, dynamic>;
      if (markers.containsKey(markerKey)) {
        markers[markerKey]['value'] = value;
        markers[markerKey]['unit'] = unit;
        markers[markerKey]['date'] = DateTime.now().toIso8601String().substring(0, 10);
      }
    }

    if (_systemFolders.containsKey(systemKey)) {
      final markerData = {
        'value': value,
        'unit': unit,
        'timestamp': DateTime.now().toIso8601String(),
      };
      _systemFolders[systemKey]!['lab_$markerKey'] = markerData;
      _mirrorToNotesExport('lab_${systemKey}_$markerKey', markerData);
    }
    notifyListeners();
  }

// Direct saving method for vital parameters
  void updateSmartWatchMetrics({
    int? systolic,
    int? diastolic,
    int? pulse,
    int? spo2,
    String? ecgSummary,
  }) {
    final healthSystems = Map<String, dynamic>.from(_data['health_systems'] ?? <String, dynamic>{});
    final cvs = Map<String, dynamic>.from(healthSystems['cvs'] ?? <String, dynamic>{});
    final markers = Map<String, dynamic>.from(cvs['markers'] ?? <String, dynamic>{});

    final Map<String, dynamic> smartWatch = markers.containsKey('smart_watch') && markers['smart_watch'] != null
        ? Map<String, dynamic>.from(markers['smart_watch'])
        : <String, dynamic>{};

    final String currentDate = DateTime.now().toIso8601String().split('T')[0];

    if (systolic != null && diastolic != null) {
      smartWatch['pressure'] = <String, dynamic>{
        "systolic": systolic,
        "diastolic": diastolic,
        "date": currentDate,
      };
    }

    if (pulse != null) {
      smartWatch['pulse'] = <String, dynamic>{
        "value": pulse,
        "date": currentDate,
        "unit": "bpm",
      };
    }

    if (spo2 != null) {
      smartWatch['spo2'] = <String, dynamic>{
        "value": spo2,
        "date": currentDate,
        "unit": "%",
      };
    }

    if (ecgSummary != null) {
      smartWatch['ecg_status'] = <String, dynamic>{
        "value": ecgSummary,
        "date": currentDate,
      };
    }

    markers['smart_watch'] = smartWatch;
    cvs['markers'] = markers;
    healthSystems['cvs'] = cvs;
    _data['health_systems'] = healthSystems;

    _mirrorToNotesExport('vitals_smart_watch', smartWatch);
    notifyListeners();
  }

// LABORATORIES
  void injectParsedLaboratories(Map<String, dynamic> parsedJson) {
    _systemFolders['global']!['shared_labs_shelf'] = parsedJson;
    _mirrorToNotesExport('shared_labs_shelf', parsedJson);
    notifyListeners();
  }

  Map<String, dynamic> getSharedLabs() {
    return Map<String, dynamic>.from(_systemFolders['global']?['shared_labs_shelf'] ?? <String, dynamic>{});
  }

// Distribute data across specific system shelves
  void updateSystemCascadeShelves({
    required String systemKey,
    required Map<String, dynamic> selectedLabs,
    required String generalOutput,
    required String trainingOutput,
  }) {
    if (_systemFolders.containsKey(systemKey)) {
      _systemFolders[systemKey]!['labs'] = selectedLabs;
      _systemFolders[systemKey]!['system_general_shelf'] = generalOutput;
      _systemFolders[systemKey]!['system_training_shelf'] = trainingOutput;

      _mirrorToNotesExport('cascade_system_$systemKey', {
        'labs': selectedLabs,
        'general': generalOutput,
        'training': trainingOutput,
      });

      notifyListeners();
    }
  }

// ==========================================
// MODEL CASCADE (FINAL VAULT LOGIC)
// ==========================================

// Recording Gemini Flash results (Deputy Chief Physician)
  void updateFlashShelves({required String general, required String training}) {
    if (_systemFolders.containsKey('core_cascade')) {
      _systemFolders['core_cascade']!['flash_general_shelf'] = general;
      _systemFolders['core_cascade']!['flash_training_shelf'] = training;

      _mirrorToNotesExport('cascade_flash', {
        'general': general,
        'training': training,
      });

      _persist();
      notifyListeners();
    }
  }
  // Recording Gemini Pro results (Chief Physician)
  void updateProShelves({required String general, required String training}) {
    if (_systemFolders.containsKey('core_cascade')) {
      _systemFolders['core_cascade']!['pro_general_shelf'] = general;
      _systemFolders['core_cascade']!['pro_training_shelf'] = training;
      _mirrorToNotesExport('cascade_pro', {'general': general, 'training': training});
      _persist();
      notifyListeners();
    }
  }

  // Assembly of basic context for the master
  String getAggregatedContextForMaster() {
    final StringBuffer buffer = StringBuffer();
    buffer.writeln("=== ANAMNESIS AND INPUT DATA ===");
    final globalData = getSystemContext('global');
    buffer.writeln(globalData.toString());
    buffer.writeln();
    buffer.writeln("=== VITAL MARKERS (SMART WATCH / LIVE) ===");
    buffer.writeln("Blood pressure: ${systolic ?? '--'}/${diastolic ?? '--'} mmHg");
    buffer.writeln("Heart rate (Pulse): ${pulse ?? '--'} bpm");
    buffer.writeln("Oxygen saturation (SpO2): ${spo2 ?? '--'}%");
    buffer.writeln("ECG status: ${ecgSummary ?? 'Not performed'}");
    buffer.writeln();
    buffer.writeln("=== CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===");
    final List<String> systemKeys = ['cvs', 'gi_tract', 'kidneys', 'endocrine', 'immune', 'nervous', 'respiratory', 'musculoskeletal'];
    for (String key in systemKeys) {
      final folder = getSystemContext(key);
      buffer.writeln("--- BLOCK: ${key.toUpperCase()} ---");
      buffer.writeln("Main conclusion: ${folder['system_general_shelf'] ?? 'No data'}");
      buffer.writeln("Training log: ${folder['system_training_shelf'] ?? 'No data'}");
      buffer.writeln();
    }
    return buffer.toString();
  }

  // Assembly of extended context, including Gemini Flash outputs from the vault
  String getAggregatedContextForMasterWithFlash() {
    final StringBuffer buffer = StringBuffer();
    buffer.writeln(getAggregatedContextForMaster());
    final coreCascade = getSystemContext('core_cascade');
    buffer.writeln("=== SYNTHESIS (GEMINI FLASH) ===");
    buffer.writeln("Main conclusion:");
    buffer.writeln(coreCascade['flash_general_shelf'] ?? 'Missing');
    buffer.writeln();
    buffer.writeln("Training output:");
    buffer.writeln(coreCascade['flash_training_shelf'] ?? 'Missing');
    buffer.writeln();
    return buffer.toString();
  }

  // Context for Curator / Pro
  String getCuratorContext() {
    final coreCascade = getSystemContext('core_cascade');
    final StringBuffer buffer = StringBuffer();
    buffer.writeln("=== GUIDELINES AND CLINICAL ROUTE FROM CHIEF PHYSICIAN (PRO) ===");
    buffer.writeln(coreCascade['pro_general_shelf'] ?? 'Route not formed');
    buffer.writeln();
    buffer.writeln("=== DEEP CLINICAL ANALYSIS AND SYNDROMES (PRO TRAINING) ===");
    buffer.writeln(coreCascade['pro_training_shelf'] ?? 'No data available');
    buffer.writeln();
    buffer.writeln("=== PRIMARY SYNTHESIS (FLASH) ===");
    buffer.writeln(coreCascade['flash_general_shelf'] ?? 'No data available');
    buffer.writeln(getAthenaLogsSummaryForPro());
    return buffer.toString();
  }

  String? get lastProGeneralOutput => _systemFolders['core_cascade']?['pro_general_shelf'] as String?;
  String? get lastProTrainingOutput => _systemFolders['core_cascade']?['pro_training_shelf'] as String?;

  // ==========================================
  // CURATOR CHAT METHODS
  // ==========================================

  List<Map<String, String>> getCuratorChatHistory() {
    return List.unmodifiable(_curatorChatHistory);
  }

  void saveCuratorChatMessage(Map<String, String> message) {
    _curatorChatHistory.add(message);
    _persist();
    notifyListeners();
  }

  // ==========================================
  // STORAGE FOR ATHENA'S HIDDEN CLINICAL REPORTS
  // ==========================================
  final List<Map<String, dynamic>> _athenaClinicalLogs = [];

  List<Map<String, dynamic>> getAthenaClinicalLogs() {
    return List.unmodifiable(_athenaClinicalLogs);
  }

  String getAthenaLogsSummaryForPro() {
    if (_athenaClinicalLogs.isEmpty) {
      return "Athena's hidden clinical logs are missing.";
    }
    final buffer = StringBuffer();
    buffer.writeln("=== CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS) ===");
    for (var log in _athenaClinicalLogs) {
      buffer.writeln("⏱ Timestamp: ${log['timestamp']}");
      buffer.writeln("📊 Status: ${log['status']}");
      buffer.writeln("🚩 Symptoms: ${(log['symptoms_mentioned'] as List?)?.join(', ') ?? 'None'}");
      buffer.writeln("⚠️ Red Flags: ${log['red_flags']}");
      buffer.writeln("📝 Summary for Chief Physician: ${log['summary_for_master']}");
      buffer.writeln("--------------------------------------------------");
    }
    return buffer.toString();
  }

  void saveAthenaClinicalReport(Map<String, dynamic> reportJson) {
    reportJson['timestamp'] = DateTime.now().toIso8601String();
    _athenaClinicalLogs.add(reportJson);
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['latest_clinical_report'] = reportJson;
    _mirrorToNotesExport('athena_daily_report', reportJson);
    _persist();
    notifyListeners();
  }

  // 🗑️ Absolute reset of all device data and memory
  Future<void> resetVault() async {
    _athenaClinicalLogs.clear();
    _systemFolders['global']?['athena_sprint_day'] = 1;
    _hasUnreadAthenaMessage = false;
    await VaultStorageService.clearVault();
    _selectedSystem = null;
    _curatorChatHistory.clear();
    _athenaClinicalLogs.clear();
    _data.clear();
    _data.addAll({
      "health_systems": {
        "cvs": {"name": "Cardiovascular", "completeness": 0.0, "color": "0xFFFF0000", "markers": {}},
        "gi_tract": {"name": "GI Tract", "completeness": 0.0, "color": "0xFFFFA500", "markers": {}},
        "respiratory": {"name": "Respiratory", "completeness": 0.0, "color": "0xFF00FFFF", "markers": {}},
        "nervous": {"name": "Nervous", "completeness": 0.0, "color": "0xFF8A2BE2", "markers": {}},
        "musculoskeletal": {"name": "Musculoskeletal", "completeness": 0.0, "color": "0xFFB0C4DE", "markers": {}},
        "kidneys": {"name": "Renal", "completeness": 0.0, "color": "0xFF0000FF", "markers": {}},
        "immune": {"name": "Immune and Blood", "completeness": 0.0, "color": "0xFF32CD32", "markers": {}},
        "endocrine": {"name": "Endocrine", "completeness": 0.0, "color": "0xFFEE82EE", "markers": {}},
      }
    });
    _systemFolders.clear();
    _systemFolders.addAll({
      'respiratory': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'immune': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'cvs': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'gi_tract': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'kidneys': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'endocrine': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'nervous': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'musculoskeletal': {'labs': <String, dynamic>{}, 'system_general_shelf': 'Analysis not performed', 'system_training_shelf': 'Logs are empty'},
      'core_cascade': {
        'flash_general_shelf': 'Waiting for Gemini Flash output...',
        'flash_training_shelf': 'Gemini Flash deficiency logs...',
        'pro_general_shelf': 'Waiting for Gemini Pro output...',
        'pro_training_shelf': 'Gemini Pro deficiency logs...',
      },
      'notes_export': <String, dynamic>{},
      'global': {'labs': <String, dynamic>{}},
      'training_sandbox': {'raw_predictions': [], 'missing_data': [], 'risk_analysis': "PENDING..."},
    });
    notifyListeners();
  }

  // ==========================================
  // METHODS FOR ATHENA'S DAILY TRIGGER
  // ==========================================

  int? getAthenaLastPromptTime() {
    return _systemFolders['global']?['athena_last_prompt_time'] as int?;
  }

  void updateAthenaLastPromptTime(int timestamp) {
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['athena_last_prompt_time'] = timestamp;
    _persist();
    notifyListeners();
  }

  void setUnreadAthenaMessage(bool value) {
    _hasUnreadAthenaMessage = value;
    notifyListeners();
  }

  // ==========================================
  // SPRINT COUNTER METHODS (1..7 DAYS)
  // ==========================================

  int getAthenaSprintDay() {
    return (_systemFolders['global']?['athena_sprint_day'] as int?) ?? 1;
  }

  int incrementAthenaSprintDay() {
    int currentDay = getAthenaSprintDay();
    if (currentDay >= 7) {
      currentDay = 1;
    } else {
      currentDay++;
    }
    if (_systemFolders['global'] == null) {
      _systemFolders['global'] = {};
    }
    _systemFolders['global']!['athena_sprint_day'] = currentDay;
    _persist();
    notifyListeners();
    return currentDay;
  }
}