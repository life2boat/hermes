"""Bounded explicit profile statements; no provider calls or inferred defaults."""

from __future__ import annotations

import re

FIELD_COLUMNS = {
    "household_size": "household_size",
    "allergies": "allergies",
    "disliked_foods": "stop_products",
    "preferred_cuisines": "preferences",
    "cooking_frequency": "cooking_frequency",
    "budget": "budget",
}
PREFERENCE_FIELDS = frozenset({"allergies", "disliked_foods", "preferred_cuisines"})


class ProfileUpdateValidationError(ValueError):
    """Sanitized failure; no partial profile writes are permitted."""


def validate_profile_delta(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.keys() - FIELD_COLUMNS.keys():
        raise ProfileUpdateValidationError("invalid profile fields")
    result: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ProfileUpdateValidationError("invalid profile field")
        if key == "household_size":
            if type(value) is not int or not 1 <= value <= 100:
                raise ProfileUpdateValidationError("invalid household size")
        elif key in PREFERENCE_FIELDS:
            if not isinstance(value, list) or len(value) > 32:
                raise ProfileUpdateValidationError("invalid preference list")
            value = [_validated_text(item) for item in value]
        else:
            value = _validated_text(value)
        result[key] = value
    return result


def _validated_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ProfileUpdateValidationError("invalid profile text")
    if any(ord(char) < 32 for char in value):
        raise ProfileUpdateValidationError("invalid profile text")
    return " ".join(value.split())


_NUMBERS = {
    "один": 1,
    "одна": 1,
    "двое": 2,
    "два": 2,
    "трое": 3,
    "три": 3,
    "четверо": 4,
    "четыре": 4,
    "пятеро": 5,
    "пять": 5,
}
_PREFIX = r"(?:теперь\s+)?(?:нас|у меня аллергия|не люблю|не любим|не едим|люблю|любим|готовлю|бюджет на питание|очисти)\b"
_SPLIT = re.compile(r"(?:\s*[,;]\s*|\s+и\s+)(?=" + _PREFIX + r")", re.I)


def extract_profile_delta(text: str) -> dict[str, object]:
    """Only accept complete, anchored declarative clauses in this MVP grammar.

    Lists append; an explicit ``очисти …`` yields an empty list (clear).
    Missing fields are omitted. Null is never accepted as inferred clearing.
    Unsupported clauses make the entire extraction a no-op.
    """
    if not isinstance(text, str) or len(text) > 1500:
        return {}
    text = text.strip().rstrip(".! ")
    if not re.match(_PREFIX, text, re.I) or "?" in text or "\n" in text:
        return {}
    delta: dict[str, object] = {}
    for clause in _SPLIT.split(text):
        clause = re.sub(r"^теперь\s+", "", clause, flags=re.I)
        match = re.fullmatch(r"нас(?: дома)?\s+(\w+)(?:\s+человек[а]?)?", clause, re.I)
        if match:
            raw = match[1].lower()
            value = int(raw) if raw.isdecimal() else _NUMBERS.get(raw)
            delta["household_size"] = value
            continue
        clear = re.fullmatch(
            r"очисти (аллергии|нелюбимые продукты|предпочтения)", clause, re.I
        )
        if clear:
            delta[
                {
                    "аллергии": "allergies",
                    "нелюбимые продукты": "disliked_foods",
                    "предпочтения": "preferred_cuisines",
                }[clear[1].lower()]
            ] = []
            continue
        matched = False
        for pattern, field in (
            (r"у меня аллергия на (.+)", "allergies"),
            (r"(?:не люблю|не любим|не едим) (.+)", "disliked_foods"),
            (r"(?:люблю|любим) (.+?)(?: кухню| кухни)", "preferred_cuisines"),
            (r"готовлю (.+)", "cooking_frequency"),
            (r"бюджет на питание (.+)", "budget"),
        ):
            match = re.fullmatch(pattern, clause, re.I)
            if match:
                value = match[1]
                delta[field] = (
                    re.split(r"\s+и\s+|,\s*", value)
                    if field in PREFERENCE_FIELDS
                    else value
                )
                matched = True
                break
        if not matched:
            return {}
    return validate_profile_delta(delta)


def merge_preference_text(existing: str | None, additions: list[str]) -> str:
    """Keep legacy text verbatim as a prefix; explicit [] clears it."""
    if not additions:
        return ""
    parts = [existing] if existing else []
    known: set[str] = {part.strip().casefold() for part in (existing or "").split(",")}
    for item in additions:
        if item.casefold() not in known:
            parts.append(item)
            known.add(item.casefold())
    value = ", ".join(parts)
    if len(value) > 2000:
        raise ProfileUpdateValidationError("preference limit exceeded")
    return value
