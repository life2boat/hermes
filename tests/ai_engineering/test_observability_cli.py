"""PR-12 observability: read-only CLI behavior."""

from __future__ import annotations

import json

import pytest

from ai_engineering.observability.__main__ import main
from ai_engineering.observability.rendering import canonical_json
from tests.ai_engineering.observability_fixture_helpers import collect_full


@pytest.fixture()
def snapshot_json() -> str:
    return canonical_json(collect_full())


def test_cli_human_summary(snapshot_json, capsys):
    import io
    import sys

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(snapshot_json)
    try:
        code = main([])
    finally:
        sys.stdin = old_stdin
    out = capsys.readouterr().out
    assert code == 0
    assert "HERMES OPERATOR SNAPSHOT" in out


def test_cli_json_flag(snapshot_json, capsys):
    import io
    import sys

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(snapshot_json)
    try:
        code = main(["--json"])
    finally:
        sys.stdin = old_stdin
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert parsed["schema_version"] == 1


def test_cli_unsupported_schema_fails_closed(capsys):
    import io
    import sys

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"schema_version": 999}))
    try:
        code = main(["--json"])
    finally:
        sys.stdin = old_stdin
    err = capsys.readouterr().err
    assert code == 2
    assert "OBSERVABILITY_SCHEMA_UNSUPPORTED" in err


def test_cli_empty_input_fails_closed(capsys):
    import io
    import sys

    old_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        code = main(["--json"])
    finally:
        sys.stdin = old_stdin
    assert code == 2


def test_cli_file_input(snapshot_json, tmp_path, capsys):
    target = tmp_path / "snapshot.json"
    target.write_text(snapshot_json, encoding="utf-8")
    code = main(["--input", str(target), "--json"])
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["projection_status"] in {"COMPLETE", "PARTIAL", "STALE", "CONFLICTED", "UNVERIFIABLE"}


def test_cli_missing_file_fails_closed(capsys):
    code = main(["--input", "Z:/definitely/not/here.json", "--json"])
    err = capsys.readouterr().err
    assert code == 2
    assert "OBSERVABILITY_INPUT_UNREADABLE" in err


def test_cli_output_is_redacted(snapshot_json, capsys):
    import io
    import sys

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(snapshot_json)
    try:
        main(["--json"])
    finally:
        sys.stdin = old_stdin
    out = capsys.readouterr().out
    assert "HERMES_OBSERVABILITY_SECRET_SENTINEL_DO_NOT_EXPOSE" not in out
