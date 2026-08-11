# Food-Vision quality fixtures v2

## Purpose and provenance

These three PNG fixtures are purpose-created synthetic, photorealistic food
scenes produced for the repository's offline quality harness. They are not
user photos and were not downloaded from third-party sources. Each committed
PNG is bound by the adjacent manifest SHA-256 and by the exact-path/hash binary
fixture policy; no image metadata is read by the harness or scorer.

The set improves visual representativeness over the historical geometric v1
fixtures, but it is still a small deterministic benchmark. A passing result is
not evidence of real-world accuracy or production rollout eligibility.

## Scene contract

- `fixture_a.png`: apple, banana, and bread roll in a natural table setting.
- `fixture_b.png`: carrot, cucumber, and cheese with an empty cup as a
  non-food distractor.
- `fixture_c.png`: ketchup, mustard (`yellow_sauce`), and sour cream in
  unlabelled serving dishes, distinguished only by visual texture and context.

The scenes contain no labels, text, logos, QR codes, or emoji. All fixtures
expect conservative clarification because they show multiple components or
condiments. The scorer preserves the established v1 thresholds and aliases;
v2 introduces no broad aliasing or scanner exception.

## Generation record

The final PNGs were generated with the built-in image-generation tool from the
scene contract above, visually reviewed, and copied unchanged into this
versioned fixture set. Only the final image bytes and this safe provenance
record are committed. No provider response, credential, user data, or hidden
metadata participates in scoring.
