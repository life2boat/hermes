# Offline Vision quality fixtures are not provider eligibility evidence

## Cause

The original food-Vision quality contract evaluated prepared JSON through the
local Hermes validator. It proved parser and scoring behavior, but it did not
bind an image, an explicit provider/model, bounded requests, or a real provider
response.

## Resolution

`food_vision_quality_v1` adds versioned synthetic fixtures, hash validation,
the opt-in `scripts/run_food_vision_quality.py` provider harness, and a
sanitized receipt. The existing offline contract remains the source for scoring
and thresholds; normal CI still performs zero external requests.

## Lesson Learned

Provider eligibility requires all three: a versioned fixture, bounded real
execution, and a sanitized receipt. Offline payload tests alone cannot make a
Vision model rollout eligible.
