# ADR-006: Provider-Scoped Qwen Vision and Ephemeral Media

## Problem

Hermes can reach Qwen through more than one credential and transport surface,
while Vision requests carry user-provided image data across a higher-risk trust
boundary. Treating every Qwen route as interchangeable, silently forwarding an
image to another provider after a failure, or retaining image-bearing messages
in session and memory artifacts would create ambiguous authorization, cost,
and privacy behavior.

## Context

The existing provider registry distinguishes the Qwen Portal OAuth route
(`qwen-oauth`) from the Alibaba Cloud DashScope API-key route (`alibaba`).
DashScope exposes explicit Qwen vision models through its OpenAI-compatible
endpoint; Portal multimodal compatibility and a rollout-eligible default model
are not proven by the repository's bounded benchmark evidence.

The uploaded still-image `vision_analyze_tool` route already uses a strict
single-request, no-provider-fallback policy, local validation before HealBite
persistence, and temporary-file cleanup for remote image downloads. Browser
screenshot and video tools retain their existing bounded capture/resize and
multi-frame semantics; they are not silently changed by this decision. The
generic auxiliary API still supports explicitly selected fallback policies for
non-image callers. This sprint makes the Qwen still-image boundary and the
non-persistence boundary explicit without activating production or selecting
a new default model.

## Decision

- Route Qwen Vision through the existing provider registry and auxiliary LLM
  interface. Use canonical `alibaba` (including its unambiguous
  `qwen-dashscope` alias) with an explicitly configured model for DashScope;
  keep `qwen-oauth` a distinct Portal identity. Do not use bare `qwen` where it
  could conceal that choice.
- Keep the integration opt-in. The repository does not declare an automatic
  Qwen vision model, production activation, or rollout eligibility.
- Keep the uploaded still-image Qwen route in `vision_analyze_tool` on
  `VISION_SINGLE_REQUEST_LLM_CALL_POLICY`: one external request and no
  provider fallback. Authentication, timeout, capacity, transport, and
  response-shape failures return a sanitized user-safe failure. Browser
  screenshot and video paths retain their separately tested resize or
  multi-frame behavior. A future cross-provider image fallback for the
  still-image route requires a separate consent, privacy, and request-budget
  decision.
- Apply provider-specific request behavior through provider profiles and
  transport hooks, not HealBite business logic or Telegram handlers.
- Treat image bytes, data URLs, and local cache paths as turn-scoped data.
  Session snapshots, background review, and Memory OS inputs must receive a
  recursively sanitized text-only representation. Temporary inbound media is
  ownership-bound and removed after success, failure, cancellation, or
  abandoned-batch cleanup.
- Continue to treat Vision output as untrusted. Existing deterministic schema,
  ownership, nutrition, and confirmation checks remain authoritative before
  any SQLite mutation; Qdrant does not become image storage.

## Alternatives Considered

- **Map bare `qwen` to one transport globally.** Rejected because model-catalog
  and provider-profile history attach that alias to different Qwen surfaces;
  silent remapping can select the wrong credential or endpoint.
- **Automatically fall back from Qwen to Gemini or another provider.** Rejected
  because a second image disclosure changes privacy, cost, and request-budget
  semantics and can occur without informed user consent.
- **Enable a default Qwen vision model now.** Rejected because current evidence
  establishes bounded schema compatibility, not rollout eligibility or Portal
  multimodal compatibility.
- **Keep images in profile-wide cache until age-based pruning.** Rejected for
  processed turns because it exceeds the minimum lifetime needed for the
  provider request and weakens session ownership guarantees.

## Consequences (+ and -)

**Positive**

- Provider identity, credential source, and endpoint selection remain explicit
  and testable.
- A Qwen failure on the uploaded still-image route cannot cause an unannounced
  second disclosure of image data.
- Durable session, review, and Memory OS paths cannot retain raw image payloads
  or local image paths.
- Existing GPT/text routing, local HealBite validation, SQLite authority, and
  Qdrant boundaries remain unchanged.

**Negative**

- Users must configure an explicit DashScope provider and vision model; there
  is no automatic Qwen default.
- A retryable provider failure still ends the Vision attempt instead of using a
  potentially available secondary provider.
- Turn-scoped cleanup and recursive sanitization add lifecycle code and
  regression-test obligations around every durable or asynchronous boundary.
