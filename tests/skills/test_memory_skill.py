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
    normalized = " ".join(text.casefold().split())
    required_semantics = {
        "user and household isolation": (
            "scope every memory os fact read and write by normalized `user_id`",
            "authoritative household authorization context",
            "qdrant `user_id` filters/payloads",
        ),
        "durable versus derived state": (
            "sqlite as the durable source of truth",
            "qdrant and sqlite fts as derived search indexes",
            "sqlite schema/content fingerprints remain unchanged",
        ),
        "dual-write reconciliation": (
            "commit the sqlite fact first",
            "two writes as non-atomic",
            "deletes also do not remove stale qdrant points",
        ),
        "qdrant mutation boundary": (
            "default to read-only metadata and `--dry-run`",
            "replacement, cutover, deletion, or collection cleanup",
            "proof that no delete or collection switch occurred",
        ),
        "integrity around mutation": (
            "pre/post `pragma integrity_check`",
            "`pragma foreign_key_check`",
            "successful isolated restore test",
        ),
    }
    for decision, phrases in required_semantics.items():
        assert all(phrase in normalized for phrase in phrases), decision
    assert 'default_prompt: "Use $memory ' in metadata
