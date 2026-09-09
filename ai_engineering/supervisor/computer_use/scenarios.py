from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from ai_engineering.contracts import EffectClass, GateResult, Status
from ai_engineering.supervisor.computer_use.contracts import (
    UIActionStatus,
    UIScenario,
    VisualEvidence,
)
from ai_engineering.supervisor.computer_use.driver import ComputerUseDriver
from ai_engineering.supervisor.policy.contracts import compute_deterministic_digest
from ai_engineering.supervisor.validator import VERIFIED_RESULT_SCHEMA_VERSION, VerifiedResult


class HermesGUITestRunner:
    def __init__(self, driver: ComputerUseDriver) -> None:
        self.driver = driver

    def run_scenario(
        self,
        scenario: UIScenario,
        task_id: str = "gui-test-task",
        attempt_id: str = "attempt-01",
        intent_digest: str = "0" * 64,
        base_sha: str = "0" * 40,
        head_sha: str = "0" * 40,
    ) -> VerifiedResult:
        utc_now = datetime.now(timezone.utc).isoformat()

        # 1. External send guard
        has_external_send = (
            "EXTERNAL_SEND" in scenario.required_effect_classes
            or EffectClass.EXTERNAL_SEND in scenario.required_effect_classes
            or EffectClass.EXTERNAL_SEND.value in scenario.required_effect_classes
        )
        if has_external_send:
            gr = (
                GateResult(
                    gate_name="gui_e2e",
                    required=True,
                    status=Status.FAIL,
                    evidence_refs=(),
                ),
            )
            res_id = compute_deterministic_digest({
                "scenario_id": scenario.scenario_id,
                "reason": "EXTERNAL_SEND_DENIED",
                "status": Status.FAIL.value,
            })
            return VerifiedResult(
                schema_version=VERIFIED_RESULT_SCHEMA_VERSION,
                result_id=res_id,
                task_id=task_id,
                attempt_id=attempt_id,
                intent_digest=intent_digest,
                base_sha=base_sha,
                head_sha=head_sha,
                normalized_evidence_digest=res_id,
                required_gates=("gui_e2e",),
                gate_results=gr,
                blockers=("EXTERNAL_SEND_DENIED",),
                status=Status.FAIL,
                reason_codes=("EXTERNAL_SEND_DENIED",),
                verified_at_utc=utc_now,
            )

        # 2. Production target guard
        if scenario.target_environment.upper() == "PRODUCTION":
            gr = (
                GateResult(
                    gate_name="gui_e2e",
                    required=True,
                    status=Status.FAIL,
                    evidence_refs=(),
                ),
            )
            res_id = compute_deterministic_digest({
                "scenario_id": scenario.scenario_id,
                "reason": "PRODUCTION_TARGET_DENIED",
                "status": Status.FAIL.value,
            })
            return VerifiedResult(
                schema_version=VERIFIED_RESULT_SCHEMA_VERSION,
                result_id=res_id,
                task_id=task_id,
                attempt_id=attempt_id,
                intent_digest=intent_digest,
                base_sha=base_sha,
                head_sha=head_sha,
                normalized_evidence_digest=res_id,
                required_gates=("gui_e2e",),
                gate_results=gr,
                blockers=("PRODUCTION_TARGET_DENIED",),
                status=Status.FAIL,
                reason_codes=("PRODUCTION_TARGET_DENIED",),
                verified_at_utc=utc_now,
            )

        # 3. Observe UI target
        obs = self.driver.observe(scenario.target_application)

        # 4. Process observation state
        if obs.observed_state == "STALE":
            status = Status.BLOCKED
            reason = "STALE_VISUAL_EVIDENCE"
            blockers = ("STALE_VISUAL_EVIDENCE",)
            reason_codes = ("STALE_VISUAL_EVIDENCE",)
        elif obs.status == UIActionStatus.BLOCKED:
            status = Status.BLOCKED
            reason = "TARGET_UNAVAILABLE"
            blockers = ("TARGET_UNAVAILABLE",)
            reason_codes = ("TARGET_UNAVAILABLE",)
        elif obs.status == UIActionStatus.PASS:
            status = Status.PASS
            reason = None
            blockers = ()
            reason_codes = ()
        else:
            status = Status.FAIL
            reason = "VISUAL_ASSERTION_FAILED"
            blockers = ()
            reason_codes = ("VISUAL_ASSERTION_FAILED",)

        gr_status = Status.PASS if status == Status.PASS else (Status.BLOCKED if status == Status.BLOCKED else Status.FAIL)
        evidence_ref = obs.visual_evidence_id or "obs-ref"
        gate_results = (
            GateResult(
                gate_name="gui_e2e",
                required=True,
                status=gr_status,
                evidence_refs=(evidence_ref,),
            ),
        )
        res_id = compute_deterministic_digest({
            "scenario_id": scenario.scenario_id,
            "status": status.value,
            "evidence_ref": evidence_ref,
            "reason": reason or "",
        })
        return VerifiedResult(
            schema_version=VERIFIED_RESULT_SCHEMA_VERSION,
            result_id=res_id,
            task_id=task_id,
            attempt_id=attempt_id,
            intent_digest=intent_digest,
            base_sha=base_sha,
            head_sha=head_sha,
            normalized_evidence_digest=res_id,
            required_gates=("gui_e2e",),
            gate_results=gate_results,
            blockers=blockers,
            status=status,
            reason_codes=reason_codes,
            verified_at_utc=utc_now,
        )
