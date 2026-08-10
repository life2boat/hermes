# Qwen Vision activation contract

## Scope and default state

This is a repository contract for a future, separately authorized activation.
It does not select a model, add a secret, change a running environment, or
deploy an image. The production Compose contract keeps
`HEALBITE_INVENTORY_PHOTO_ENABLED=false` and
`HEALBITE_INVENTORY_PHOTO_UI_ENABLED=false`; both corresponding allowlists are
empty. A deployment must preserve those values unless a later activation
authority explicitly changes and validates them.

## Provider and secret boundary

DashScope Vision uses the canonical provider name `alibaba` and obtains its
credential only from `DASHSCOPE_API_KEY` in the approved protected secret
source. `QWEN_API_KEY` is a distinct credential contract and is never an alias
or fallback for DashScope. Inline API keys, key-environment overrides, and
endpoint overrides in `auxiliary.vision` are rejected by configuration
validation.

A future activation must name an operator-approved, explicit model in
`auxiliary.vision.model`. This repository deliberately provides no default
model. The `qwen`, `qwen-oauth`, and `qwen-portal` names are rejected for the
DashScope Vision configuration boundary; they cannot select a credential or
endpoint implicitly. An unavailable credential blocks only an explicitly
configured DashScope request; disabled or unrelated provider paths do not
require `DASHSCOPE_API_KEY`.

## Request and fallback boundary

Vision requests are fail-closed. For `task="vision"`, a caller cannot turn on
cross-provider fallback through a custom call policy. A provider initialization
failure or request failure returns the existing safe unavailable/error path;
it must not forward image data to another provider. Standard text-task fallback
behavior is unchanged.

## Future activation gate

Before enabling the feature in any environment, a separate approved runbook
must bind an immutable image, explicit model, canary identity, protected secret
source, timeout and health checks. It must validate provider availability,
one-request/no-fallback behavior, image ownership isolation, and rollback to
the default-off feature state. This document grants no production authority.