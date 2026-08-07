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
    normalized = " ".join(text.casefold().split())
    required_semantics = {
        "canonical provenance": (
            "branch names and worktree contents are mutable",
            "resolved `main` sha",
            "passing `check-repository` result",
        ),
        "trusted immutable build": (
            "ignored or untracked files can contaminate a raw build context",
            "mutable tags can move after review",
            "single oci revision equals the source sha",
        ),
        "quiescent fresh backup": (
            "active writers to be zero",
            "older backup does not represent the state immediately before",
            "successful isolated restore",
        ),
        "database compatibility": (
            "`pragma integrity_check`",
            "`pragma foreign_key_check`",
            "schema-breaking migration",
            "compatibility tests for the candidate and rollback images",
        ),
        "exact rollback boundary": (
            "exact inspected immutable image without tag re-resolution",
            "previous image is not a valid automatic recovery target",
        ),
        "technical versus governance": (
            "governance-only warnings",
            "technical fail-closed gates",
            "any `fail`, `unknown`, or absent result blocks mutation",
        ),
    }
    for decision, phrases in required_semantics.items():
        assert all(phrase in normalized for phrase in phrases), decision
    assert 'default_prompt: "Use $deploy ' in metadata
