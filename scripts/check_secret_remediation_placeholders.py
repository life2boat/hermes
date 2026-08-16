#!/usr/bin/env python3
"""Static anti-placeholder gate for secret remediation R1 modules."""
from __future__ import annotations
import ast
import re
import sys
from pathlib import Path

MUTATION_CRITICAL_MODULES = [
    "ops/secret_remediation_r1/safe_fs.py",
    "ops/secret_remediation_r1/parent_dir.py",
    "ops/secret_remediation_r1/process_identity.py",
    "ops/secret_remediation_r1/secret_transfer.py",
    "ops/secret_remediation_r1/env_split.py",
    "ops/secret_remediation_r1/compose_transform.py",
    "ops/secret_remediation_r1/override_transform.py",
    "ops/secret_remediation_r1/compose_command.py",
    "ops/secret_remediation_r1/candidate_image_guard.py",
    "ops/secret_remediation_r1/poller_checker.py",
    "ops/secret_remediation_r1/source_invariant.py",
    "ops/secret_remediation_r1/runtime_invariant.py",
    "ops/secret_remediation_r1/rollback.py",
    "ops/secret_remediation_r1/health.py",
    "ops/secret_remediation_r1/executor.py",
]

FORBIDDEN_PATTERNS = [
    (r"#\s*TODO", "TODO comment"),
    (r"#\s*FIXME", "FIXME comment"),
    (r"#\s*PLACEHOLDER", "PLACEHOLDER comment"),
    (r'print\s*\([^)]*=PASS', "Unconditional PASS print"),
    (r'return\s+True\s*$', "Bare return True (checked separately)"),
]


def check_unconditional_pass(source: str, path: str) -> list[str]:
    """Check for functions that only print PASS or return True unconditionally."""
    findings: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{path}: SyntaxError: {exc}"]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        # Skip single-statement docstring functions
        if len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            continue
        # Skip pure passthrough / abstract
        non_trivial = [n for n in body if not (
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        )]
        if len(non_trivial) == 1:
            stmt = non_trivial[0]
            # Bare return True
            if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and stmt.value.value is True:
                findings.append(
                    f"{path}:{node.lineno}: function {node.name!r} unconditionally returns True"
                )
    return findings


def check_empty_tests(root) -> list[str]:
    import ast
    findings = []
    for fpath in root.glob('tests/secret_remediation_r1/**/*.py'):
        try:
            source = fpath.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
                    body = node.body
                    # filter out docstrings
                    statements = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
                    if not statements:
                        findings.append(f"{fpath.relative_to(root)}:{node.lineno}: Empty test (only docstring)")
                        continue
                    if len(statements) == 1:
                        stmt = statements[0]
                        if isinstance(stmt, ast.Pass):
                            findings.append(f"{fpath.relative_to(root)}:{node.lineno}: Empty test (pass)")
                        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                            findings.append(f"{fpath.relative_to(root)}:{node.lineno}: Empty test (ellipsis)")
                        elif isinstance(stmt, ast.Assert) and isinstance(stmt.test, ast.Constant) and stmt.test.value is True:
                            findings.append(f"{fpath.relative_to(root)}:{node.lineno}: Empty test (assert True)")
                        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == 'skip' and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == 'pytest':
                            findings.append(f"{fpath.relative_to(root)}:{node.lineno}: Empty test (only pytest.skip)")
        except Exception as e:
            findings.append(f"{fpath}: Error parsing AST: {e}")
    return findings

def main() -> int:
    root = Path(__file__).parent.parent
    findings: list[str] = []

    for rel_path in MUTATION_CRITICAL_MODULES:
        path = root / rel_path
        if not path.exists():
            findings.append(f"MISSING: {rel_path}")
            continue

        source = path.read_text(encoding="utf-8")

        for pattern, label in FORBIDDEN_PATTERNS:
            if pattern == r'return\s+True\s*$':
                continue  # Handled by AST check
            for i, line in enumerate(source.splitlines(), 1):
                if re.search(pattern, line, re.IGNORECASE):
                    findings.append(f"{rel_path}:{i}: {label}: {line.strip()!r}")

        findings.extend(check_unconditional_pass(source, rel_path))

    findings.extend(check_empty_tests(root))

    pass_only = sum(1 for f in findings if "(pass)" in f)
    ellipsis_only = sum(1 for f in findings if "(ellipsis)" in f)
    assert_true_only = sum(1 for f in findings if "(assert True)" in f)

    if findings:
        print(f"PLACEHOLDER_FINDINGS={len(findings)}")
        for f in findings:
            print(f"  FINDING: {f}")
        print(f"PASS_ONLY_TEST_COUNT={pass_only}")
        print(f"ELLIPSIS_ONLY_TEST_COUNT={ellipsis_only}")
        print(f"ASSERT_TRUE_ONLY_TEST_COUNT={assert_true_only}")
        print("PLACEHOLDER_GATE=FAIL")
        return 1

    print(f"PLACEHOLDER_FINDINGS=0")
    print(f"PASS_ONLY_TEST_COUNT={pass_only}")
    print(f"ELLIPSIS_ONLY_TEST_COUNT={ellipsis_only}")
    print(f"ASSERT_TRUE_ONLY_TEST_COUNT={assert_true_only}")
    print("PLACEHOLDER_GATE=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
