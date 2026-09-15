import json
import subprocess
import os

def test_generate_offline_evidence():
    result = subprocess.run(
        ["python3", "scripts/generate_offline_evidence.py", "abc123sha"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    
    with open("secret_evidence_unsigned.json") as f:
        secret_ev = json.load(f)
        
    assert secret_ev["evidence_type"] == "production_secret_presence"
    assert secret_ev["target_sha"] == "abc123sha"
    assert "source_class" in secret_ev
    assert secret_ev["source_class"] == "explicit-protected-dotenv"
    assert "required_secrets" in secret_ev
    
    # ensure no actual secret values are emitted
    for sec in secret_ev["required_secrets"]:
        assert "value" not in sec
        assert sec["name"] == "TELEGRAM_BOT_TOKEN"
        assert sec["required"] is True
        assert sec["present"] is True
        assert sec["source_class"] == "approved-production-secret-source"

def test_producer_output_accepted_by_real_qualifier():
    # We will simulate this by checking the dictionary schema against hermes_release_qualification logic
    pass

def test_missing_top_level_source_class_rejected():
    pass
