import subprocess
from typing import Any

from ai_engineering.production_runtime_attestation import (
    CollectorResult,
    CollectorStatus,
    create_collector_result,
)


class DockerRuntimeCollector:
    """Collects structural Docker state safely without exposing environments."""

    collector_id = "docker_runtime"

    def __init__(self, container_name: str, expected_db_mount: str | None = None) -> None:
        self.container_name = container_name
        self.expected_db_mount = expected_db_mount

    def collect(self) -> CollectorResult:
        try:
            # 1. Check if container exists and get basic safe properties.
            # We strictly avoid dumping the full inspect JSON to prevent secret leakage.
            format_str = (
                "{{.State.Running}}|{{.RestartCount}}|{{.Config.Image}}|"
                '{{index .Config.Labels "com.docker.compose.project"}}|'
                '{{index .Config.Labels "com.docker.compose.service"}}'
            )
            proc = subprocess.run(
                ["docker", "inspect", self.container_name, "--format", format_str],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if proc.returncode != 0:
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            output = proc.stdout.strip()
            if not output:
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            parts = output.split("|")
            if len(parts) != 5:
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            running = parts[0].lower() == "true"
            try:
                restart_count = int(parts[1])
            except ValueError:
                restart_count = -1
            
            image = parts[2]
            project = parts[3] if parts[3] != "<no value>" else ""
            service = parts[4] if parts[4] != "<no value>" else ""

            # 2. Check health safely
            health_proc = subprocess.run(
                ["docker", "inspect", self.container_name, "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            health_status = health_proc.stdout.strip() if health_proc.returncode == 0 else "unknown"
            if not health_status or health_status == "<no value>":
                health_status = "none"

            observations: dict[str, Any] = {
                "running": running,
                "restart_count": restart_count,
                "image": image,
                "compose_project": project,
                "compose_service": service,
                "health_status": health_status,
            }

            # 3. Check db mount match if requested
            if self.expected_db_mount:
                mounts_proc = subprocess.run(
                    ["docker", "inspect", self.container_name, "--format", "{{range .Mounts}}{{.Source}}::{{.Destination}}||{{end}}"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                db_mount_matches = False
                if mounts_proc.returncode == 0:
                    mounts_output = mounts_proc.stdout.strip()
                    for mount in mounts_output.split("||"):
                        if not mount:
                            continue
                        src_dst = mount.split("::")
                        if len(src_dst) == 2:
                            src, dst = src_dst
                            # Expected mount logic
                            if dst.strip() == self.expected_db_mount or src.strip() == self.expected_db_mount:
                                db_mount_matches = True
                
                observations["db_mount_matches_expected"] = db_mount_matches

            return create_collector_result(
                self.collector_id, CollectorStatus.AVAILABLE, observations
            )

        except subprocess.TimeoutExpired:
            return create_collector_result(
                self.collector_id, CollectorStatus.UNAVAILABLE, {}
            )
        except Exception:
            return create_collector_result(
                self.collector_id, CollectorStatus.UNAVAILABLE, {}
            )
