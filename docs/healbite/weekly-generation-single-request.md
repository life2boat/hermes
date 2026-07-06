# HealBite weekly generation single-request contract

Weekly menu draft generation uses a strict external provider request policy.
One generation invocation may make at most one actual outbound provider request.
The request budget is consumed immediately before the provider transport call.

The weekly path disables:

- transient retry;
- parameter-recovery retry;
- model refresh retry;
- credential recovery retry;
- provider fallback;
- model fallback.

Generic auxiliary LLM callers retain the existing default retry and fallback behavior.
The strict policy is selected explicitly by `AuxiliaryWeeklyMenuGenerator`.

Provider failure leaves no weekly menu rows. Malformed, empty, or invalid provider
output fails closed and does not publish, create shopping rows, or perform a
second provider request. DB persistence failures after a provider response do not
trigger regeneration.

Publishing remains a separate explicit operation. Shopping generation remains a
separate disabled feature path. Live current-week generation still requires an
operator-approved D5C1B execution gate.
