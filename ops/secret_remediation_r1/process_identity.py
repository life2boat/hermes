"""Container and process identity binding for HealBite secret remediation."""
from __future__ import annotations
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ops.secret_remediation_r1.constants import (
    CONTAINER_NAME, COMPOSE_PROJECT, COMPOSE_SERVICE,
    LEGACY_IMAGE_REF, LEGACY_IMAGE_ID,
)


class ProcessIdentityError(Exception):
    pass


@dataclass
class ContainerIdentity:
    container_id: str
    init_pid: int
    image_id: str
    running: bool


class DockerBackend(Protocol):
    def inspect(self, container_name: str) -> list[dict]: ...
    def container_pids(self, container_id: str) -> list[int]: ...


class RealDockerBackend:
    def inspect(self, container_name: str) -> list[dict]:
        r = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise ProcessIdentityError(f"docker inspect failed: {r.stderr}")
        return json.loads(r.stdout)

    def container_pids(self, container_id: str) -> list[int]:
        r = subprocess.run(
            ["docker", "top", container_id, "-eo", "pid"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise ProcessIdentityError(f"docker top failed: {r.stderr}")
        lines = r.stdout.strip().splitlines()
        result = []
        for line in lines[1:]:  # Skip header
            line = line.strip()
            if line.isdigit():
                result.append(int(line))
        return result


def _get_container_init_pid(container_data: dict) -> int:
    pid = container_data.get("State", {}).get("Pid", 0)
    if not pid:
        raise ProcessIdentityError("Container has no init PID")
    return pid


def _verify_compose_labels(labels: dict) -> None:
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    if project != COMPOSE_PROJECT:
        raise ProcessIdentityError(
            f"Wrong compose project: {project!r} != {COMPOSE_PROJECT!r}"
        )
    if service != COMPOSE_SERVICE:
        raise ProcessIdentityError(
            f"Wrong compose service: {service!r} != {COMPOSE_SERVICE!r}"
        )


def _verify_image(container_data: dict) -> None:
    image_id = container_data.get("Image", "")
    if image_id != LEGACY_IMAGE_ID:
        raise ProcessIdentityError(
            f"Wrong image ID: {image_id!r} != {LEGACY_IMAGE_ID!r}"
        )


def _find_poller_processes() -> list[int]:
    """Find hermes gateway run --replace processes, excluding supervisors."""
    try:
        r = subprocess.run(
            ["pgrep", "-a", "-f", "hermes"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise ProcessIdentityError("pgrep not found")

    pids = []
    exclude_patterns = ["s6", "s6-supervise", "grep", "pgrep"]
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, cmd = parts
        if not pid_str.isdigit():
            continue
        cmd_lower = cmd.lower()
        if "hermes gateway run --replace" not in cmd_lower:
            continue
        if any(excl in cmd_lower for excl in exclude_patterns):
            continue
        pids.append(int(pid_str))
    return pids


def _get_pid_namespace(pid: int) -> int:
    try:
        ns_path = f"/proc/{pid}/ns/pid"
        ns_stat = os.stat(ns_path)
        return ns_stat.st_ino
    except OSError as exc:
        raise ProcessIdentityError(f"Cannot read PID namespace for {pid}: {exc}")


def _read_pid_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            return f.read()
    except OSError as exc:
        raise ProcessIdentityError(f"Cannot read cgroup for {pid}: {exc}")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def resolve_poller_pid(
    docker: DockerBackend | None = None,
) -> tuple[int, ContainerIdentity]:
    """
    Identify the single hermes-bot poller PID and verify its container binding.
    Returns (host_pid, container_identity).
    Raises ProcessIdentityError on any failure.
    """
    if docker is None:
        docker = RealDockerBackend()

    # 1. Inspect container
    try:
        containers = docker.inspect(CONTAINER_NAME)
    except Exception as exc:
        raise ProcessIdentityError(f"Container inspection failed: {exc}")

    if not containers:
        raise ProcessIdentityError(f"Container {CONTAINER_NAME!r} not found")

    data = containers[0]

    if not data.get("State", {}).get("Running"):
        raise ProcessIdentityError("Container is not running")

    _verify_compose_labels(data.get("Config", {}).get("Labels", {}))
    _verify_image(data)

    container_id = data["Id"]
    init_pid = _get_container_init_pid(data)
    identity = ContainerIdentity(
        container_id=container_id,
        init_pid=init_pid,
        image_id=data["Image"],
        running=True,
    )

    init_ns = _get_pid_namespace(init_pid)

    # 2. Find exactly one poller process
    poller_pids = _find_poller_processes()
    if len(poller_pids) == 0:
        raise ProcessIdentityError("No hermes gateway poller found")
    if len(poller_pids) > 1:
        raise ProcessIdentityError(f"Multiple pollers found: {poller_pids}")

    pid = poller_pids[0]

    # 3. Bind via PID namespace
    pid_ns = _get_pid_namespace(pid)
    if pid_ns != init_ns:
        raise ProcessIdentityError(
            f"PID {pid} namespace {pid_ns} != container init namespace {init_ns}"
        )

    # 4. Bind via cgroup
    cgroup = _read_pid_cgroup(pid)
    if container_id not in cgroup and container_id[:12] not in cgroup:
        raise ProcessIdentityError(f"PID {pid} cgroup does not match container {container_id[:12]}")

    return pid, identity


def read_poller_environ(
    pid: int,
    identity: ContainerIdentity,
    docker: DockerBackend | None = None,
) -> bytes:
    """
    Read /proc/<pid>/environ, revalidating identity before and after.
    Returns raw NUL-delimited environ bytes.
    """
    if docker is None:
        docker = RealDockerBackend()

    def _revalidate() -> None:
        if not _pid_exists(pid):
            raise ProcessIdentityError(f"PID {pid} no longer exists")
        try:
            containers = docker.inspect(CONTAINER_NAME)
        except Exception as exc:
            raise ProcessIdentityError(f"Re-inspection failed: {exc}")
        data = containers[0] if containers else {}
        if data.get("Id") != identity.container_id:
            raise ProcessIdentityError("Container ID changed during operation")
        if not data.get("State", {}).get("Running"):
            raise ProcessIdentityError("Container stopped during operation")

    # Validate before
    _revalidate()

    env_path = f"/proc/{pid}/environ"
    try:
        env_st = os.lstat(env_path)
    except OSError as exc:
        raise ProcessIdentityError(f"environ lstat failed: {exc}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(env_path, flags)
    except OSError as exc:
        raise ProcessIdentityError(f"environ open failed: {exc}")

    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        env_bytes = b"".join(chunks)
    finally:
        os.close(fd)

    # Validate after
    _revalidate()

    return env_bytes
