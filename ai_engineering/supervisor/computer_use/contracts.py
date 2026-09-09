from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Mapping


class ComputerUseActionType(StrEnum):
    OBSERVE = "OBSERVE"
    FOCUS_WINDOW = "FOCUS_WINDOW"
    CLICK = "CLICK"
    TYPE_TEXT = "TYPE_TEXT"
    PRESS_KEY = "PRESS_KEY"
    SCROLL = "SCROLL"
    SELECT = "SELECT"
    OPEN_ALLOWED_URL = "OPEN_ALLOWED_URL"
    UPLOAD_ALLOWED_FILE = "UPLOAD_ALLOWED_FILE"
    WAIT_FOR_STATE = "WAIT_FOR_STATE"
    CAPTURE_SCREENSHOT = "CAPTURE_SCREENSHOT"


class UIActionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"


class ComputerUseTaskError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VisualEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UIActionProposal:
    action_id: str
    action_type: ComputerUseActionType
    target_identity: str
    semantic_effect_class: str
    parameters: dict[str, str]

    def compute_digest(self) -> str:
        d = asdict(self)
        if isinstance(d.get("action_type"), StrEnum):
            d["action_type"] = d["action_type"].value
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UIActionReceipt:
    action_id: str
    session_id: str
    step_id: int
    effect_class: str
    policy_receipt_id: str
    target_identity: str
    precondition_evidence: str
    postcondition_evidence: str
    status: UIActionStatus
    reason: str | None = None

    def compute_digest(self) -> str:
        d = asdict(self)
        if isinstance(d.get("status"), StrEnum):
            d["status"] = d["status"].value
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComputerUseTask:
    schema_version: str = "hermes.computer-use-task.v1"
    run_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    dispatch_id: str = ""
    intent_digest: str = ""

    policy_receipt_id: str = ""
    policy_receipt_digest: str = ""

    target_application: str = ""
    target_environment: str = ""

    allowed_actions: tuple[str, ...] = tuple()
    forbidden_actions: tuple[str, ...] = tuple()

    allowed_effect_classes: tuple[str, ...] = tuple()
    stop_boundary: str = ""

    scenario_id: str = ""
    max_actions: int = 0
    max_duration: int = 0
    max_visual_checkpoints: int = 0

    result_channel: str = ""
    created_at_utc: str = ""

    def compute_digest(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_computer_use_task(task: ComputerUseTask, expected_digest: str | None = None) -> bool:
    if expected_digest is not None and task.compute_digest() != expected_digest:
        raise ComputerUseTaskError("COMPUTER_USE_TASK_TAMPERED")
    return True


@dataclass(frozen=True, slots=True)
class VisualEvidence:
    schema_version: str = "hermes.visual-evidence.v1"
    visual_evidence_id: str = ""
    run_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    dispatch_id: str = ""

    scenario_id: str = ""
    step_id: int = 0

    target_application: str = ""
    target_environment: str = ""

    capture_type: str = ""
    artifact_ref: str = ""
    sha256: str = ""
    width: int = 0
    height: int = 0

    observation_kind: str = ""
    expected_state: str = ""
    observed_state: str = ""
    status: UIActionStatus = UIActionStatus.PENDING

    captured_at_utc: str = ""

    def compute_digest(self) -> str:
        d = asdict(self)
        d.pop("visual_evidence_id", None)
        if isinstance(d.get("status"), StrEnum):
            d["status"] = d["status"].value
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_visual_evidence(evidence: VisualEvidence, expected_digest: str | None = None) -> bool:
    target = expected_digest
    if target is None and evidence.visual_evidence_id:
        target = evidence.visual_evidence_id
    if target is not None and evidence.compute_digest() != target:
        raise VisualEvidenceError("VISUAL_EVIDENCE_TAMPERED")
    return True


@dataclass(frozen=True, slots=True)
class UIScenario:
    schema_version: str = "hermes.ui-scenario.v1"
    scenario_id: str = ""
    name: str = ""
    target_application: str = ""
    target_environment: str = ""

    preconditions: tuple[str, ...] = tuple()
    steps: tuple[str, ...] = tuple()
    checkpoints: tuple[str, ...] = tuple()
    expected_terminal_state: str = ""

    maximum_actions: int = 0
    timeout: int = 0

    required_effect_classes: tuple[str, ...] = tuple()
    required_visual_assertions: tuple[str, ...] = tuple()

    def compute_digest(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
