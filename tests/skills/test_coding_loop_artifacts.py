from pathlib import Path


def test_coding_loop_artifacts_exist():
    root = Path(__file__).resolve().parents[2]
    skill_path = root / "skills" / "dev" / "coding-loop" / "SKILL.md"
    script_path = root / "scripts" / "agent_check.sh"
    runbook_path = root / "RUNBOOK_CODING_LOOP.md"

    assert skill_path.exists()
    assert script_path.exists()
    assert runbook_path.exists()

    skill_text = skill_path.read_text(encoding="utf-8")
    assert "name: coding-loop" in skill_text
