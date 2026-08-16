from __future__ import annotations
import os
import tempfile
import subprocess
import shutil

class PreflightError(Exception):
    pass

def run_compose_preflight() -> None:
    """
    Synthetically verify that the target environment's docker-compose binary
    parses .env files in the expected way (e.g., stripping quotes properly)
    before running the remediation. Fails if the behavior is incompatible.
    """
    # Create a temporary directory for the preflight
    preflight_dir = tempfile.mkdtemp(prefix="hermes_preflight_")
    try:
        env_path = os.path.join(preflight_dir, ".env")
        compose_path = os.path.join(preflight_dir, "docker-compose.yml")

        # Test case: variables with and without quotes
        with open(env_path, "wb") as f:
            f.write(b"RAW_VAR=val1\nQUOTED_VAR=\"val2\"\n")

        with open(compose_path, "w", encoding="utf-8") as f:
            f.write(
                "services:\n"
                "  test:\n"
                "    image: alpine\n"
                "    command: echo hello\n"
                "    environment:\n"
                "      - RAW_VAR=${RAW_VAR}\n"
                "      - QUOTED_VAR=${QUOTED_VAR}\n"
            )

        # Run docker-compose config to dump the resolved environment
        try:
            result = subprocess.run(
                ["docker", "compose", "config"],
                cwd=preflight_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError as exc:
            raise PreflightError(f"Compose preflight config failed: {exc.stderr}")
        except FileNotFoundError:
            raise PreflightError("docker compose command not found")

        output = result.stdout

        # Verify parsing behavior
        # Expectation: QUOTED_VAR should not include the literal quotes if Compose behaves correctly
        # Wait, docker-compose v2 strips quotes from .env files for variable substitution.
        
        # We look for the resolved environment block.
        if "RAW_VAR: val1" not in output:
            raise PreflightError("Compose preflight failed: RAW_VAR not resolved correctly")
        if "QUOTED_VAR: val2" not in output:
            if "QUOTED_VAR: '\"val2\"'" in output or 'QUOTED_VAR: "\\"val2\\""' in output:
                 raise PreflightError("Compose preflight failed: Quotes were not stripped from QUOTED_VAR")
            raise PreflightError("Compose preflight failed: QUOTED_VAR not resolved correctly")

    finally:
        shutil.rmtree(preflight_dir, ignore_errors=True)
