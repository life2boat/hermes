from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.collect_production_attestation import (
    load_production_manifest,
    save_evidence,
)


def test_load_production_manifest() -> None:
    manifest = load_production_manifest()
    assert isinstance(manifest, dict)
    compose = manifest.get("compose", {})
    assert compose.get("project_name") == "hermes-agent"
    assert compose.get("target_service") == "hermes-bot"


def test_save_evidence_creates_file_in_specified_dir(tmp_path: Path) -> None:
    data = b'{"test": "evidence"}'
    saved = save_evidence(data, "test_prefix", "38aff7162d7075f6b2797085ec3cee46da8ac211", evidence_dir=tmp_path)
    assert saved.exists()
    assert saved.is_file()
    assert saved.read_bytes() == data
    assert "test_prefix_38aff7162d7075f6b2797085ec3cee46da8ac211" in saved.name
