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
            with open(compose_path, 'r') as f:
                staging_compose = yaml.safe_load(f)
            with open(prod_compose_path, 'r') as f:
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

    def deploy(self) -> StagingDeploymentReceipt:
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
            
            repo_digests = container_info.get("Image", "")
            if "RepoDigests" in container_info and container_info["RepoDigests"]:
                repo_digests = container_info["RepoDigests"][0]
            
            labels = container_info.get("Config", {}).get("Labels", {})
            oci_revision = labels.get("org.opencontainers.image.revision")
            container_id = container_info.get("Id", "started")
            
            return StagingDeploymentReceipt(
                success=True,
                timestamp=datetime.utcnow(),
                container_id=container_id,
                repo_digest=repo_digests,
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
                check_cmd = ["docker", "exec", "hermes-bot-staging", "hermes", "--version"]
                try:
                    subprocess.run(check_cmd, capture_output=True, text=True, check=True)
                except subprocess.CalledProcessError as e:
                    is_healthy = False
                    return StagingHealthReceipt(
                        success=False,
                        timestamp=datetime.utcnow(),
                        is_healthy=False,
                        error_message=f"Real health check failed: {e.stderr}"
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
            cmd = ["docker", "exec", "hermes-bot-staging", "hermes", "gateway", "trigger-synthetic", "--intent", "menu"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return StagingCanaryReceipt(
                success=True,
                timestamp=datetime.utcnow(),
                canary_result=result.stdout.strip()
            )
        except subprocess.CalledProcessError as e:
            return StagingCanaryReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Canary failed: {e.stderr}"
            )

    def rollback(self) -> StagingRollbackReceipt:
        cmd = ["docker", "compose", "-p", self.project_name, "-f", self.compose_file, "down", "-v"]
        try:
            subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                check=True
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
