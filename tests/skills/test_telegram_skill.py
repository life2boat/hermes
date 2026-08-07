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
    normalized = " ".join(text.casefold().split())
    required_semantics = {
        "runtime and user ownership": (
            "bind the bot token to the intended gateway/profile",
            "private-user, group-user, group-chat, and feature scope",
            "synthetic cross-user/cross-chat routing and fsm tests",
        ),
        "token secrecy": (
            "telegram bot token as a bearer credential",
            "preventing the value from entering output",
            "neither the token nor credential-bearing urls",
        ),
        "single long-poll consumer": (
            "exactly one active `getupdates` long-polling consumer",
            "two runtimes using the same bot token must not be active",
            "telegram terminates competing `getupdates` sessions with a conflict",
        ),
        "read-only no-send health": (
            "default health checks to no-send operations",
            "`getme` as evidence of credential/network/api readiness, not as proof",
            "zero send/edit api calls and zero outbound messages",
        ),
    }
    for decision, phrases in required_semantics.items():
        assert all(phrase in normalized for phrase in phrases), decision
    assert 'default_prompt: "Use $telegram ' in metadata
