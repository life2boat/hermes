import subprocess
import os
import time
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
        
        compose_path = os.path.join(self.workspace_dir, self.compose_file)
        if not os.path.exists(compose_path):
            return StagingPreflightReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"{self.compose_file} not found in workspace",
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
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                check=True
            )
            return StagingDeploymentReceipt(
                success=True,
                timestamp=datetime.utcnow(),
                container_id="started"
            )
        except subprocess.CalledProcessError as e:
            return StagingDeploymentReceipt(
                success=False,
                timestamp=datetime.utcnow(),
                error_message=f"Deploy failed: {e.stderr}"
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
        # A mock/CLI canary test as suggested
        try:
            cmd = ["docker", "exec", "hermes-bot-staging", "hermes", "--version"]
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
        cmd = ["docker", "compose", "-p", self.project_name, "-f", self.compose_file, "down"]
        try:
            result = subprocess.run(
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
