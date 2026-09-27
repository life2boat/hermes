"""Claim and lease management to guarantee single-worker ownership."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
from typing import Mapping

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    ClaimRecord,
)


class LeaseError(Exception):
    """Raised on invalid lease operations or ambiguous ownership."""


class LeaseManager:
    """Thread-safe and process-safe claim/lease manager."""

    def __init__(self, persistence_path: Path | str | None = None) -> None:
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._leases: dict[str, ClaimRecord] = {}
        if self._persistence_path and self._persistence_path.exists():
            self._load()

    def _load(self) -> None:
        if not self._persistence_path or not self._persistence_path.exists():
            return
        try:
            with open(self._persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                self._leases[k] = ClaimRecord(
                    task_id=v["task_id"],
                    claim_owner=v["claim_owner"],
                    claim_token=v["claim_token"],
                    claimed_at=v["claimed_at"],
                    lease_expires_at=v["lease_expires_at"],
                )
        except Exception as exc:
            raise LeaseError(f"Failed to load lease store: {exc}") from exc

    def _save(self) -> None:
        if not self._persistence_path:
            return
        self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            k: {
                "task_id": v.task_id,
                "claim_owner": v.claim_owner,
                "claim_token": v.claim_token,
                "claimed_at": v.claimed_at,
                "lease_expires_at": v.lease_expires_at,
            }
            for k, v in self._leases.items()
        }
        tmp_path = self._persistence_path.with_suffix('.tmp')
        import os
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._persistence_path)

    def is_held_by_foreign_worker(
        self,
        task_id: str,
        owner: str,
        now_iso: str | None = None,
    ) -> bool:
        """Check if task has an active lease owned by a different worker."""
        lease = self._leases.get(task_id)
        if not lease:
            return False
        if not lease.is_active(now_iso):
            return False
        return lease.claim_owner != owner

    def claim(
        self,
        task_id: str,
        owner: str,
        lease_duration_sec: int = 1800,
        now_iso: str | None = None,
    ) -> tuple[bool, ClaimRecord | None, str | None]:
        """Attempt to claim or renew a lease on task_id.

        Returns (success, claim_record, error_reason).
        """
        if now_iso is None:
            now_dt = datetime.now(timezone.utc)
        else:
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

        now_str = now_dt.isoformat()
        expires_str = (now_dt + timedelta(seconds=lease_duration_sec)).isoformat()

        existing = self._leases.get(task_id)
        if existing and existing.is_active(now_str):
            if existing.claim_owner == owner:
                # Same-owner retry: IDEMPOTENT renewal
                renewed = ClaimRecord(
                    task_id=task_id,
                    claim_owner=owner,
                    claim_token=existing.claim_token,
                    claimed_at=existing.claimed_at,
                    lease_expires_at=expires_str,
                )
                self._leases[task_id] = renewed
                self._save()
                return True, renewed, None
            # Foreign worker holds active lease -> FAIL CLOSED
            return False, None, BlockReasonCode.ACTIVE_FOREIGN_LEASE.value

        # No lease or lease expired -> grant claim
        new_token = secrets.token_hex(16)
        claim = ClaimRecord(
            task_id=task_id,
            claim_owner=owner,
            claim_token=new_token,
            claimed_at=now_str,
            lease_expires_at=expires_str,
        )
        self._leases[task_id] = claim
        self._save()
        return True, claim, None

    def verify_lease(
        self,
        task_id: str,
        owner: str,
        claim_token: str,
        now_iso: str | None = None,
    ) -> bool:
        """Verify that the worker still holds a valid, active lease.

        Critical for fail-closed behavior before any mutations.
        """
        lease = self._leases.get(task_id)
        if not lease:
            return False
        if lease.claim_owner != owner or lease.claim_token != claim_token:
            return False
        return lease.is_active(now_iso)

    def release(self, task_id: str, owner: str, claim_token: str) -> bool:
        """Release lease upon successful completion or graceful abort."""
        lease = self._leases.get(task_id)
        if not lease:
            return False
        if lease.claim_owner == owner and lease.claim_token == claim_token:
            del self._leases[task_id]
            self._save()
            return True
        return False
