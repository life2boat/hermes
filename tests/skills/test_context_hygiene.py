from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_root_agent_instructions_route_context_without_global_loads():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert len(text.splitlines()) < 220
    assert "## Context router" in text
    assert "only the context needed" in text
    assert "not required for a typo" in text
    assert "INTENT_CONTROL_PLANE=REQUIRED" in text
    assert "Continue through consecutive phases" in text
    assert "declared completion boundary" in text
    assert "Before starting any task, read" not in text
    assert "Before starting any implementation, you **MUST** run" not in text


def test_progressive_skill_references_preserve_domain_boundaries():
    required = {
        "skills/deploy/references/provenance.md",
        "skills/deploy/references/image-attestation.md",
        "skills/deploy/references/authority.md",
        "skills/deploy/references/database-safety.md",
        "skills/deploy/references/migration.md",
        "skills/deploy/references/backup-restore.md",
        "skills/deploy/references/rollback.md",
        "skills/deploy/references/runtime-verification.md",
        "skills/memory/references/authority.md",
        "skills/memory/references/epoch-safety.md",
        "skills/memory/references/convergence.md",
        "skills/memory/references/qdrant-rebuild.md",
        "skills/memory/references/restore.md",
        "skills/telegram/references/safety-invariants.md",
        "skills/telegram/references/runtime-diagnostics.md",
        "docs/agent-reference/CONTEXT_HYGIENE_PRESERVATION_MAP.md",
    }
    assert all((ROOT / path).is_file() for path in required)

    deploy_root = (ROOT / "skills/deploy/SKILL.md").read_text(encoding="utf-8")
    memory_root = (ROOT / "skills/memory/SKILL.md").read_text(encoding="utf-8")
    telegram_root = (ROOT / "skills/telegram/SKILL.md").read_text(encoding="utf-8")
    assert "## Select references" in deploy_root
    assert "## Select references" in memory_root
    assert "## Select references" in telegram_root
