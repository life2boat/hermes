import subprocess
import os
import time
import json
import yaml
from datetime import datetime
from ai_engineering.supervisor.staging.receipts import (
    StagingPreflightReceipt,
    StagingDeploymentReceipt,
    StagingHealthReceipt,
    ExactImageAttestation,
    StagingCanaryReceipt,
    StagingRollbackReceipt
)

class StagingDeployer:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.project_name = "hermes-staging"
        self.compose_file = "docker-compose.staging.yml"

    def preflight(self, staging_target_id: str, production_target_id: str) -> StagingPreflightReceipt:
        if staging_target_id == production_target_id:
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message="STAGING_TARGET_ID must not equal PRODUCTION_TARGET_ID",
                target_id=staging_target_id
            )

        # Dirty worktree test
        git_cmd = ["git", "status", "--porcelain=v1"]
        try:
            res = subprocess.run(git_cmd, cwd=self.workspace_dir, capture_output=True, text=True, check=True)
            if res.stdout.strip():
                return StagingPreflightReceipt(
                    success=False,
                    timestamp=datetime.utcnow(),
                    error_message="Dirty worktree: uncommitted changes present",
                    target_id=staging_target_id
                )
        except Exception as e:
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Git status failed: {e}",
                target_id=staging_target_id
            )

        staging_image = os.environ.get("STAGING_IMAGE", "")
        if "@sha256:" not in staging_image:
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message="STAGING_IMAGE must be present and contain @sha256:",
                target_id=staging_target_id
            )

        compose_path = os.path.join(self.workspace_dir, self.compose_file)
        prod_compose_path = os.path.join(self.workspace_dir, "docker-compose.yml")
        if not os.path.exists(compose_path) or not os.path.exists(prod_compose_path):
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"{self.compose_file} or docker-compose.yml not found in workspace",
                target_id=staging_target_id
            )

        try:
            with open(compose_path, 'r', encoding='utf-8') as f:
                staging_compose = yaml.safe_load(f)
            with open(prod_compose_path, 'r', encoding='utf-8') as f:
                prod_compose = yaml.safe_load(f)
        except Exception as e:
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Failed to parse compose files: {e}",
                target_id=staging_target_id
            )

        prod_containers = {c.get("container_name") for c in prod_compose.get("services", {}).values() if c.get("container_name")}
        staging_containers = {c.get("container_name") for c in staging_compose.get("services", {}).values() if c.get("container_name")}

        if prod_containers.intersection(staging_containers):
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message="Shared container names found between staging and production",
                target_id=staging_target_id
            )

        prod_volumes = set()
        for svc in prod_compose.get("services", {}).values():
            for vol in svc.get("volumes", []):
                if isinstance(vol, str) and ":" in vol:
                    prod_volumes.add(vol.split(":")[0])
            for env in svc.get("env_file", []):
                prod_volumes.add(env)

        staging_volumes = set()
        for svc in staging_compose.get("services", {}).values():
            for vol in svc.get("volumes", []):
                if isinstance(vol, str) and ":" in vol:
                    staging_volumes.add(vol.split(":")[0])
            for env in svc.get("env_file", []):
                staging_volumes.add(env)

        if prod_volumes.intersection(staging_volumes):
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message="Shared host paths found between staging and production",
                target_id=staging_target_id
            )

        return StagingPreflightReceipt(
            success=True,
            timestamp=datetime.utcnow(),
            target_id=staging_target_id
        )

    def deploy(self, attestation: ExactImageAttestation) -> StagingDeploymentReceipt:
        if not attestation.registry_digest.startswith("sha256:") or len(attestation.registry_digest) != 71:
            return StagingDeploymentReceipt(success=False, timestamp=datetime.utcnow(), error_message="Malformed expected_digest")
        cmd = ["docker", "compose", "-p", self.project_name, "-f", self.compose_file, "up", "-d"]
        try:
            subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                check=True
            )

            inspect_cmd = ["docker", "inspect", "hermes-bot-staging"]
            inspect_res = subprocess.run(inspect_cmd, capture_output=True, text=True, check=True)
            container_info = json.loads(inspect_res.stdout)[0]

            running_config_image_id = container_info.get("Image", "")
            container_id = container_info.get("Id", "started")

            if not running_config_image_id:
                return StagingDeploymentReceipt(success=False, timestamp=datetime.utcnow(), error_message="Container has no Image ID")

            image_inspect_cmd = ["docker", "image", "inspect", running_config_image_id]
            image_inspect_res = subprocess.run(image_inspect_cmd, capture_output=True, text=True, check=True)
            image_info = json.loads(image_inspect_res.stdout)[0]

            docker_image_inspect_id = image_info.get("Id")
            repo_digests = image_info.get("RepoDigests", [])
            labels = image_info.get("Config", {}).get("Labels", {})
            oci_revision = labels.get("org.opencontainers.image.revision")

            if docker_image_inspect_id == attestation.registry_digest:
                try:
                    import subprocess
                    import re
                    ctr_res = subprocess.run(["ctr", "-n", "moby", "images", "inspect", running_config_image_id], capture_output=True, text=True, check=True)
                    match = re.search(r'application/vnd\.oci\.image\.config\.v1\+json @(sha256:[a-f0-9]+)', ctr_res.stdout)
                    if match:
                        docker_image_inspect_id = match.group(1)
                except Exception:
                    pass

            if docker_image_inspect_id != attestation.config_digest:
                return StagingDeploymentReceipt(success=False, timestamp=datetime.utcnow(), error_message=f"Config Image ID mismatch: expected {attestation.config_digest}, got {docker_image_inspect_id}")

            expected_registry_reference = f"ghcr.io/life2boat/hermes@{attestation.registry_digest}"
            if expected_registry_reference not in repo_digests:
                return StagingDeploymentReceipt(success=False, timestamp=datetime.utcnow(), error_message=f"Registry Manifest Digest mismatch: expected {expected_registry_reference} in {repo_digests}")

            if attestation.oci_revision != oci_revision:
                return StagingDeploymentReceipt(success=False, timestamp=datetime.utcnow(), error_message=f"OCI revision mismatch: expected {attestation.oci_revision}, got {oci_revision}")

            return StagingDeploymentReceipt(
                success=True,
                timestamp=datetime.utcnow(),
                container_id=container_id,
                repo_digest=repo_digests[0] if repo_digests else "",
                oci_revision=oci_revision
            )
        except subprocess.CalledProcessError as e:
            return StagingDeploymentReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Deploy failed: {e.stderr}"
            )
        except Exception as e:
            return StagingDeploymentReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Deploy error: {str(e)}"
            )

    def health(self) -> StagingHealthReceipt:
        cmd = ["docker", "compose", "-p", self.project_name, "-f", self.compose_file, "ps", "--services", "--filter", "status=running"]
        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                check=True
            )
            running_services = result.stdout.strip().split("\n")
            is_healthy = "hermes-bot-staging" in running_services and "qdrant-staging" in running_services

            if is_healthy:
                # Real health check via docker exec
                check_cmd = ["docker", "exec", "hermes-bot-staging", "hermes", "status"]
                try:
                    subprocess.run(check_cmd, capture_output=True, text=True, check=True)
                except subprocess.CalledProcessError as e:
                    return StagingHealthReceipt(
                        success=False, timestamp=datetime.utcnow(), is_healthy=False,
                        error_message=f"hermes status failed: {e.stderr or e.stdout}"
                    )

                db_cmd = ["docker", "exec", "hermes-bot-staging", "python", "-c",
                          "import sqlite3; db = sqlite3.connect('/opt/data/sessions.db'); assert db.execute('PRAGMA integrity_check;').fetchone()[0] == 'ok'"]
                try:
                    subprocess.run(db_cmd, capture_output=True, text=True, check=True)
                except subprocess.CalledProcessError as e:
                    return StagingHealthReceipt(
                        success=False, timestamp=datetime.utcnow(), is_healthy=False,
                        error_message=f"SQLite integrity check failed: {e.stderr or e.stdout}"
                    )

            return StagingHealthReceipt(
                success=is_healthy,
                timestamp=datetime.utcnow(),
                is_healthy=is_healthy,
                error_message=None if is_healthy else "Not all services are running"
            )
        except subprocess.CalledProcessError as e:
            return StagingHealthReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                is_healthy=False,
                error_message=f"Health check failed: {e.stderr}"
            )

    def canary(self) -> StagingCanaryReceipt:
        try:
            # Execute the minimal internal script bypassing external network but hitting handlers
            cmd = ["docker", "exec", "hermes-bot-staging", "python", "/opt/hermes/scripts/synthetic_canary_adapter.py"]
            # Container-only execution: no local fallback allowed.
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            data = json.loads(result.stdout.strip())
            return StagingCanaryReceipt(
                success=data.get("success", False),
                timestamp=datetime.fromisoformat(data.get("timestamp")) if data.get("timestamp") else datetime.utcnow(),
                canary_result=data.get("canary_result", ""),
                error_message=data.get("error_message")
            )
        except subprocess.CalledProcessError as e:
            return StagingCanaryReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Canary failed: {e.stderr or e.stdout}"
            )
        except Exception as e:
            return StagingCanaryReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Canary failed: {e}"
            )

    def rollback(self, delete_volumes: bool = False) -> StagingRollbackReceipt:
        cmd = ["docker", "compose", "-p", self.project_name, "-f", self.compose_file, "down"]
        if delete_volumes:
            cmd.append("-v")
        try:
            subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                check=True
            )

            # Rollback verification
            inspect_cmd = ["docker", "compose", "-p", self.project_name, "-f", self.compose_file, "ps", "--services"]
            res = subprocess.run(inspect_cmd, cwd=self.workspace_dir, capture_output=True, text=True, check=True)
            if "hermes-bot-staging" in res.stdout:
                return StagingRollbackReceipt(
                    success=False,
                    timestamp=datetime.utcnow(),
                    error_message="Rollback verification failed: container still exists"
                )

            return StagingRollbackReceipt(
                success=True,
                timestamp=datetime.utcnow()
            )
        except subprocess.CalledProcessError as e:
            return StagingRollbackReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Rollback failed: {e.stderr}"
            )
