"""Repository contracts for the Hermes AI Engineering System v2 foundation."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

CONTRACTS = (
    "docs/AGENT_BEHAVIOUR_CONTRACT.md",
    "docs/BEHAVIOUR_EVALS.md",
    "docs/LLM_OPS_POLICY.md",
    "docs/AGENT_RELEASE_GATES.md",
    "docs/SKILL_LOOP_GRAPH_LIFECYCLE.md",
)

ADRS = tuple(
    f"docs/adr/ADR-{number}-{slug}.md"
    for number, slug in (
        ("0074", "agent-behaviour-contract"),
        ("0075", "behaviour-evals-release-gates"),
        ("0076", "llm-ops-model-policy"),
        ("0077", "governed-agent-improvement"),
    )
)

KNOWLEDGE = "knowledge/ai/agent-behaviour-llm-ops-v2.md"
EXECUTABLE_CONTRACTS = (
    "ai_engineering/contracts.py",
    "ai_engineering/trace.py",
    "ai_engineering/redaction.py",
    "ai_engineering/scenario.py",
)



def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_v2_contract_and_decision_files_exist() -> None:
    for relative_path in (*CONTRACTS, *ADRS, KNOWLEDGE):
        assert (ROOT / relative_path).is_file(), relative_path


def test_release_contract_preserves_distinct_fail_closed_gates() -> None:
    text = _read("docs/AGENT_RELEASE_GATES.md")
    normalized = " ".join(text.split())
    assert "CODE PASS != PRODUCTION RELEASE PASS" in text
    assert (
        "MERGE_ELIGIBLE = CODE_PASS AND REQUIRED_OFFLINE_BEHAVIOUR_PASS "
        "AND REQUIRED_SECURITY_PASS"
    ) in normalized
    assert (
        "PRODUCTION_RELEASE_ELIGIBLE = MERGE_ELIGIBLE AND "
        "REQUIRED_LIVE_BEHAVIOUR_PASS AND REQUIRED_COST_PASS AND "
        "PRODUCTION_READINESS_PASS"
    ) in normalized
    for rule in ("UNKNOWN != PASS", "NOT_RUN != PASS", "INCONCLUSIVE != PASS"):
        assert rule in text


def test_behaviour_contract_owns_required_dimensions_and_distinction() -> None:
    text = _read("docs/AGENT_BEHAVIOUR_CONTRACT.md")
    for dimension in (
        "Provenance",
        "Authority",
        "Scope",
        "Stop boundary",
        "Truthfulness",
        "Unknown handling",
        "Tool selection",
        "Tool safety",
        "Secret handling",
        "Failure handling",
        "Model selection",
        "Cost discipline",
    ):
        assert dimension in text
    assert "git merge succeeded" in text
    assert "agent was authorized to merge" in text


def test_v2_invariants_have_stable_ids() -> None:
    text = _read("docs/HERMES_INVARIANTS.md")
    for number in range(1, 8):
        assert f"INV-AI-V2-{number:03d}" in text


def test_navigation_indexes_reference_authoritative_sources() -> None:
    source_map = _read("docs/HERMES_SOURCE_MAP.md")
    knowledge_index = _read("knowledge/ai/README.md")
    decision_index = _read("knowledge/decisions/README.md")

    for relative_path in CONTRACTS:
        assert Path(relative_path).name in source_map
    for relative_path in EXECUTABLE_CONTRACTS:
        assert (ROOT / relative_path).is_file(), relative_path
        assert relative_path in source_map
    assert Path(KNOWLEDGE).name in knowledge_index
    for relative_path in ADRS:
        assert Path(relative_path).name in decision_index


def test_contracts_do_not_claim_planned_runtime_exists() -> None:
    combined = "\n".join(
        _read(path)
        for path in (
            *CONTRACTS,
            "docs/HERMES_SOURCE_MAP.md",
            "docs/CURRENT_STATE.md",
            KNOWLEDGE,
        )
    )
    assert "NOT IMPLEMENTED" in combined or "NOT_IMPLEMENTED" in combined
    assert "PLANNED" in combined or "planned" in combined


def test_new_markdown_links_resolve() -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    checked_files = (*CONTRACTS, *ADRS, KNOWLEDGE)

    for relative_path in checked_files:
        document = ROOT / relative_path
        for target in link_pattern.findall(document.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#")):
                continue
            clean_target = target.split("#", maxsplit=1)[0]
            if not clean_target:
                continue
            resolved = (document.parent / clean_target).resolve()
            assert resolved.exists(), f"{relative_path}: broken link {target}"
