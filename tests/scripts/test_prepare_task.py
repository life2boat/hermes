from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.prepare_task import CORE_DOCUMENTS, build_task_context, main


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Prepare Task Tests")
    for relative in CORE_DOCUMENTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    adr = root / "docs" / "adr" / "001-test.md"
    adr.parent.mkdir(parents=True, exist_ok=True)
    adr.write_text("# ADR test\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        ".pytest_cache/\n.task_context/\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test fixture")
    return root


def test_core_documents_include_v2_normative_contracts() -> None:
    assert {
        "docs/AGENT_BEHAVIOUR_CONTRACT.md",
        "docs/BEHAVIOUR_EVALS.md",
        "docs/LLM_OPS_POLICY.md",
        "docs/AGENT_RELEASE_GATES.md",
        "docs/SKILL_LOOP_GRAPH_LIFECYCLE.md",
    }.issubset(CORE_DOCUMENTS)


def test_context_binds_git_changes_docs_and_safe_pytest_classification(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "docs" / "CURRENT_STATE.md").write_text(
        "# changed state\n",
        encoding="utf-8",
    )
    (root / "notes.txt").write_text("untracked\n", encoding="utf-8")
    untracked_adr = root / "docs" / "adr" / "999-untracked.md"
    untracked_adr.write_text("# untracked ADR\n", encoding="utf-8")
    cache = root / ".pytest_cache" / "v" / "cache" / "lastfailed"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps({"tests/test_example.py::test_failure": True}),
        encoding="utf-8",
    )

    context = build_task_context(root)

    assert context["repository"]["head_sha"] == _git(root, "rev-parse", "HEAD")
    assert context["repository"]["branch"] == "main"
    assert context["repository"]["worktree_clean"] is False
    changed = {(item["status"], item["path"]) for item in context["changed_files"]}
    assert (" M", "docs/CURRENT_STATE.md") in changed
    assert ("??", "notes.txt") in changed
    assert ("??", "docs/adr/999-untracked.md") in changed
    packaged = {item["path"] for item in context["documents"]}
    assert "docs/adr/001-test.md" in packaged
    assert "docs/adr/999-untracked.md" not in packaged
    assert context["test_evidence"]["classification"] == "INCONCLUSIVE"
    assert context["test_evidence"]["cache_state"] == "RECORDED_FAILURES"
    assert context["test_evidence"]["failed_test_count"] == 1
    assert context["test_evidence"]["pass_claim_allowed"] is False
    assert "test_failure" not in json.dumps(context)


def test_cli_writes_context_only_under_ignored_task_context(
    tmp_path: Path,
    capsys,
) -> None:
    root = _repository(tmp_path)

    result = main(
        [
            "--repository-root",
            str(root),
            "--output",
            ".task_context/task-context.json",
        ]
    )

    assert result == 0
    output = root / ".task_context" / "task-context.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["repository"]["head_sha"] == _git(root, "rev-parse", "HEAD")
    assert capsys.readouterr().out == (
        "TASK_CONTEXT_WRITTEN=.task_context/task-context.json\n"
    )
    assert _git(root, "status", "--porcelain=v1") == ""


def test_cli_rejects_output_outside_task_context(
    tmp_path: Path,
    capsys,
) -> None:
    root = _repository(tmp_path)
    escaped = tmp_path / "escaped.json"

    result = main(
        [
            "--repository-root",
            str(root),
            "--output",
            str(escaped),
        ]
    )

    assert result == 2
    assert not escaped.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "prepare_task: OUTPUT_OUTSIDE_TASK_CONTEXT\n"
