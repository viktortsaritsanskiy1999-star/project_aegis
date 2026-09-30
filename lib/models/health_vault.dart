// Full data structure based on your JSON template (with smart watch shelf and immune key)
final Map<String, dynamic> _data = {
  "user_profile": {
    "age": 0,
    "gender": "",
    "weight": 0,
    "height": 0,
    "chronic_diseases": [],
    "habits": {
      "smoking": false,
      "alcohol": "rarely"
    },
    "completeness": 0
  },
  "health_systems": {
    "cvs": {
      "name": "Cardiovascular",
      "completeness": 0,
      "markers": {
        "ldl": {"value": 0.0, "date": "", "unit": "mmol/l"},
        "hdl": {"value": 0.0, "date": "", "unit": "mmol/l"},
        "cholesterol_total": {"value": 0.0, "date": "", "unit": "mmol/l"},
        "crp": {"value": 0.0, "date": "", "unit": "mg/l"},

        // DEDICATED SHELF FOR SMART WATCH DATA
        "smart_watch": {
          "pressure": {"systolic": 0, "diastolic": 0, "date": ""},
          "pulse": {"value": 0, "date": "", "unit": "bpm"},
          "spo2": {"value": 0, "date": "", "unit": "%"},
          "ecg_status": {"value": "Not uploaded", "date": "", "local_path": ""}
        }
      }
    },
    "gi_tract": {
      "name": "GI Tract & Metabolism",
      "completeness": 0,
      "markers": {
        "alt": {"value": 0.0, "date": "", "unit": "U/L"},
        "ast": {"value": 0.0, "date": "", "unit": "U/L"},
        "bilirubin_total": {"value": 0.0, "date": "", "unit": "μmol/L"},
        "ggt": {"value": 0.0, "date": "", "unit": "U/L"}
      },
      "complaints": []
    },
    "kidneys": {
      "name": "Urinary System",
      "completeness": 0,
      "markers": {
        "creatinine": {"value": 0.0, "date": "", "unit": "μmol/L"},
        "urea": {"value": 0.0, "date": "", "unit": "mmol/L"},
        "gfr": {"value": 0.0, "date": "", "unit": "ml/min/1.73m2"}
      },
      "complaints": []
    },
    "endocrine": {
      "name": "Endocrinology",
      "completeness": 0,
      "markers": {
        "glucose": {"value": 0.0, "date": "", "unit": "mmol/l"},
        "hba1c": {"value": 0.0, "date": "", "unit": "%"},
        "tsh": {"value": 0.0, "date": "", "unit": "mIU/L"},
        "insulin": {"value": 0.0, "date": "", "unit": "μU/mL"}
      },
      "complaints": []
    },
    "immune": { // FIXED: Returned clean immune instead of immune_blood, as agreed
      "name": "Immune & Blood",
      "completeness": 0,
      "markers": {
        "hemoglobin": {"value": 0.0, "date": "", "unit": "g/L"},
        "ferritin": {"value": 0.0, "date": "", "unit": "ng/mL"},
        "leukocytes": {"value": 0.0, "date": "", "unit": "10^9/L"},
        "vit_d": {"value": 0.0, "date": "", "unit": "ng/mL"}
      },
      "complaints": []
    },
    "nervous": {
      "name": "Nervous System",
      "completeness": 0,
      "markers": {
        "sleep_hours": {"value": 0.0, "date": ""},
        "stress_level": {"value": 0.0, "date": "", "scale": "1-10"},
        "magnesium": {"value": 0.0, "date": "", "unit": "mmol/l"}
      },
      "complaints": []
    },
    "musculoskeletal": {
      "name": "Musculoskeletal",
      "completeness": 0,
      "markers": {
        "calcium_total": {"value": 0.0, "date": "", "unit": "mmol/l"},
        "uric_acid": {"value": 0.0, "date": "", "unit": "μmol/L"}
      },
      "complaints": []
    }
  },
  "emergency_logs": []
};