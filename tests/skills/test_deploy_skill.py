from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_deploy_skill_preserves_fail_closed_contract():
    skill_path = ROOT / "skills" / "deploy" / "SKILL.md"
    metadata_path = ROOT / "skills" / "deploy" / "agents" / "openai.yaml"
    text = skill_path.read_text(encoding="utf-8")
    metadata = metadata_path.read_text(encoding="utf-8")

    description = re.search(r"^description: (.+)$", text, re.MULTILINE)
    assert description is not None
    assert len(description.group(1)) <= 60
    assert description.group(1).endswith(".")
    assert "## When to Use" in text
    assert "## Procedure" in text
    assert "## Failure/Rollback" in text
    assert "SQLite backup API" in text
    assert "exact 40-character" in text
    assert "ROLLED_BACK" in text
    assert "scripts/hermes_production_deploy.sh" in text
    assert 'default_prompt: "Use $deploy ' in metadata
