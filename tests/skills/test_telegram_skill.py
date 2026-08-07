from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_telegram_skill_starts_read_only_and_prevents_duplicate_polling():
    skill_path = ROOT / "skills" / "telegram" / "SKILL.md"
    metadata_path = ROOT / "skills" / "telegram" / "agents" / "openai.yaml"
    text = skill_path.read_text(encoding="utf-8")
    metadata = metadata_path.read_text(encoding="utf-8")

    description = re.search(r"^description: (.+)$", text, re.MULTILINE)
    assert description is not None
    assert len(description.group(1)) <= 60
    assert description.group(1).endswith(".")
    assert "./scripts/healbite status" in text
    assert "long polling" in text
    assert "webhook" in text
    assert "Never start a second polling process" in text
    assert "bypass both message guards" in text
    assert "explicit approval" in text
    assert 'default_prompt: "Use $telegram ' in metadata
