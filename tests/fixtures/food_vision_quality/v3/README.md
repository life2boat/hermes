# Food-Vision quality fixtures v3

Status: `CANDIDATE`; human visual review has not been performed.

V3 reuses the exact three repository-owned synthetic PNG byte identities from
v2. It does not copy or mutate them. The manifest points to the immutable v2
images and binds every path by SHA-256.

The product contract keeps Fixture A as the low-complexity apple, banana, and
bread control and Fixture B as the carrot, cucumber, cheese, and empty-cup
distractor control. Fixture C retains exact recognition for the red ketchup and
generic yellow sauce while treating the unlabelled white condiment only as the
runtime-supported generic `sauce` class. A narrower white-condiment subtype is
not pixel-supported and requires clarification.

Fixture D is not required: A and B already test exact recognition, while C
contains both visually resolvable condiments and the required ambiguity case.

Human review must bind the exact manifest and review-package digests before a
provider benchmark may treat this candidate reference truth as reviewed.
