from __future__ import annotations

import re
import subprocess


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_LINE = re.compile(r"^version:\s+(?P<rest>.+)$", re.MULTILINE)
_SHA_BRACKET = re.compile(r"\[(?P<sha>[^\]]+)\]\s*$")


def _run_dump(image: str) -> str:
    r = subprocess.run(
        ["docker", "run", "--rm", image, "dump"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, (
        f"hermes dump exited {r.returncode}: "
        f"stderr={r.stderr[-1000:]!r}\nstdout={r.stdout[-1000:]!r}"
    )
    return r.stdout


def _read_baked_sha_from_image(image: str) -> str:
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "cat",
            image,
            "/opt/hermes/.hermes_build_sha",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, (
        "expected /opt/hermes/.hermes_build_sha to exist in the built image: "
        f"stderr={r.stderr[-1000:]!r}"
    )
    baked = r.stdout.strip()
    assert _FULL_SHA_RE.fullmatch(baked), f"invalid baked SHA: {baked!r}"
    return baked


def _read_baked_sha_mode(image: str) -> int:
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "stat",
            image,
            "-c",
            "%a",
            "/opt/hermes/.hermes_build_sha",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, f"unable to stat baked SHA file: {r.stderr[-1000:]!r}"
    return int(r.stdout.strip(), 8)


def _read_revision_label(image: str) -> str:
    r = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            image,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, f"unable to inspect revision label: {r.stderr[-1000:]!r}"
    label = r.stdout.strip()
    assert _FULL_SHA_RE.fullmatch(label), f"invalid OCI revision label: {label!r}"
    return label


def test_dump_reports_baked_sha_and_label_contract(built_image: str) -> None:
    baked = _read_baked_sha_from_image(built_image)
    mode = _read_baked_sha_mode(built_image)
    label = _read_revision_label(built_image)
    stdout = _run_dump(built_image)

    assert label == baked, (
        "OCI revision label and baked build SHA diverged: "
        f"label={label!r} baked={baked!r}"
    )
    assert mode & 0o222 == 0, f"baked SHA file must be read-only, got mode {mode:o}"

    match = _VERSION_LINE.search(stdout)
    assert match, f"no `version:` line in dump output:\n{stdout[:2000]}"
    sha_match = _SHA_BRACKET.search(match.group("rest"))
    assert sha_match, (
        f"`version:` line missing [<sha>] bracket: {match.group('rest')!r}"
    )
    reported = sha_match.group("sha")
    assert reported == baked[:8], (
        f"dump reported {reported!r} but baked file contained {baked!r} "
        f"(expected first 8 chars: {baked[:8]!r})"
    )
