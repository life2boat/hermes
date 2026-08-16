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
    parses env_file values in the exact literal way required for secret transfer.
    """
    preflight_dir = tempfile.mkdtemp(prefix="hermes_preflight_")
    try:
        env_file_test_path = os.path.join(preflight_dir, "my_env_file.env")
        compose_path = os.path.join(preflight_dir, "docker-compose.yml")

        # Our serialization writes EXACT raw bytes, e.g., KEY=VALUE\n
        with open(env_file_test_path, "wb") as f:
            f.write(
                b"EMBEDDED_EQUALS=a=b=c\n"
                b"DOLLAR=value$with$dollars\n"
                b"BACKSLASH=value\\with\\slashes\n"
                b'QUOTED_OR_SPACE_CASE="quoted string"\n'
            )

        with open(compose_path, "w", encoding="utf-8") as f:
            f.write(
                "services:\n"
                "  test:\n"
                "    image: healbite-hermes:pr99-main-273b0a6cccaf\n"
                "    command: echo hello\n"
                "    env_file:\n"
                f"      - {os.path.basename(env_file_test_path)}\n"
            )

        try:
            subprocess.run(
                ["docker", "compose", "create", "test"],
                cwd=preflight_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            # Fallback to alpine if the legacy image isn't local on the tester's machine.
            # But the constraint says to use a deterministic mechanism supported by CI.
            raise PreflightError(f"Compose preflight create failed: {exc.stderr}")
        except FileNotFoundError:
            raise PreflightError("docker compose command not found")

        result = subprocess.run(
            ["docker", "compose", "ps", "-q", "test"],
            cwd=preflight_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        container_id = result.stdout.strip()
        if not container_id:
            raise PreflightError("Could not get container ID from compose ps")

        inspect_res = subprocess.run(
            ["docker", "inspect", container_id],
            cwd=preflight_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        import json

        inspect_data = json.loads(inspect_res.stdout)
        env_list = inspect_data[0].get("Config", {}).get("Env", [])

        env_dict = {}
        for e in env_list:
            if "=" in e:
                k, v = e.split("=", 1)
                env_dict[k] = v

        if env_dict.get("EMBEDDED_EQUALS") != "a=b=c":
            raise PreflightError(
                "Compose preflight failed: EMBEDDED_EQUALS semantics mismatch"
            )
        if env_dict.get("DOLLAR") != "value$with$dollars":
            raise PreflightError("Compose preflight failed: DOLLAR semantics mismatch")
        if env_dict.get("BACKSLASH") != r"value\with\slashes":
            raise PreflightError(
                "Compose preflight failed: BACKSLASH semantics mismatch"
            )

        # In our serialization, quotes are literal. If docker-compose strips them, it breaks!
        # (Actually, standard docker run --env-file treats quotes literally. Compose v2 might strip them
        # if format is missing, but if it strips them, we fail closed.)
        if env_dict.get("QUOTED_OR_SPACE_CASE") != '"quoted string"':
            raise PreflightError(
                "Compose preflight failed: QUOTED_OR_SPACE_CASE semantics mismatch (quotes stripped or space broken)"
            )

        # Cleanup the container
        subprocess.run(
            ["docker", "compose", "rm", "-f", "test"],
            cwd=preflight_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    finally:
        shutil.rmtree(preflight_dir, ignore_errors=True)
