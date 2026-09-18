import json
import os
import pytest
import subprocess

def test_missing_trust_anchor():
    proc = subprocess.run(
        ["python3", "scripts/hermes_production_evidence_sign.py", "a" * 40],
        input="",
        text=True,
        capture_output=True,
        env={}
    )
    assert proc.returncode != 0
    assert "Missing provenance key" in proc.stdout

def test_invalid_sha():
    proc = subprocess.run(
        ["python3", "scripts/hermes_production_evidence_sign.py", "invalid"],
        input="dummykey",
        text=True,
        capture_output=True,
        env={}
    )
    assert proc.returncode != 0
    assert "Invalid or missing target SHA" in proc.stdout
