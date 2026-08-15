# Dogfood #1 Offline Replay (v2 Artifacts)

This document represents the offline requalification of the Dogfood #1 trace using the hardened v2 Intent Control Plane semantics. No new evidence was collected. The exact bytes produced by the completed R0.2a tasks are used.

## 1. ExecutionAuthorityReceipt
- **Authority Kind:** `OPERATOR_TOKEN`
- **Token:** `SUSTAINED_WSL_R0_2A_ALLOWED`
- **Authorized Effect:** `READ_ONLY_WSL_SESSION`
- **Status:** `AUTHORITY_RECORDED`

## 2. EvidenceCollectionReceipt
- **Collection Mode:** `READ_ONLY_WSL_SESSION`
- **Producer:** Dogfood #1 offline replay
- **Status:** PASS

## 3. EvidenceSufficiencyReview
- **Reviewer Class:** `INDEPENDENT_AGENT`
- **Overall Status:** `INSUFFICIENT_EVIDENCE` (per adjudication, e.g. CONFIG_PRECEDENCE overclaimed)
- **Reviewer Identity:** `dogfood-reviewer-offline-01`

## 4. Convergence v2 Report
- **Is High Risk:** `True` (as this was a R0.2a production task)
- **Status:** `NOT_CONVERGED`
- **Blocking Reasons:**
  - `CRITERION_FAILED` (due to AC4/AC9 semantic gaps)
  - `CRITERION_EVIDENCED_UNREVIEWED` (for any criterion that passed evidence but was not semantically reviewed)

## Conclusion
The offline replay confirms that under Convergence v2, Dogfood #1 accurately fails closed. The Analyzer v1 checks were not enough, but with the EvidenceSufficiencyReview and Convergence v2, the semantic gaps are now correctly blocking production deployment.
