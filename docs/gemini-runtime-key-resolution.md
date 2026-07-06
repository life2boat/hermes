# Gemini runtime key resolution

- Gemini native credentials resolve immediately before each outbound request.
- The runtime transport is `x-goog-api-key` header only.
- Secret values must not appear in logs, exception text, request summaries or telemetry.
- Restart or container recreate is still required for environment changes to reach a process that cannot otherwise observe updated credentials, but the Gemini client itself no longer captures a stale key at construction time.
- HTTP 403 can still represent revoked credentials, missing API enablement, billing/quota limits or model-access restrictions; this patch only fixes stale or mis-propagated runtime key state.
- Post-deploy validation requires one controlled live Gemini vision request.
- Synthetic validation must not use a Telegram user photo or real credential material.
