from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_memory_skill_keeps_sqlite_authoritative():
    skill_path = ROOT / "skills" / "memory" / "SKILL.md"
    metadata_path = ROOT / "skills" / "memory" / "agents" / "openai.yaml"
    text = skill_path.read_text(encoding="utf-8")
    metadata = metadata_path.read_text(encoding="utf-8")

    description = re.search(r"^description: (.+)$", text, re.MULTILINE)
    assert description is not None
    assert len(description.group(1)) <= 60
    assert description.group(1).endswith(".")
    assert "SQLite remains the source of truth" in text
    assert "Qdrant is a rebuildable semantic index" in text
    assert "--dry-run" in text
    assert "upsert-only" in text
    assert "does not prove identity equality" in text
    assert 'default_prompt: "Use $memory ' in metadata
