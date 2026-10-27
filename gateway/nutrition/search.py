"""
gateway.nutrition.search — Russian food name normalization, tokenization, and multi-stage search engine.

Features:
1. Canonical Unicode & Russian text normalization (ё -> е, case-folding, punctuation strip).
2. Russian inflection and suffix reduction (stemming of noun/adjective declensions).
3. Multi-stage matching:
   - Stage 1: Exact normalized string match.
   - Stage 2: Token set match (order-independent matching, e.g. "грудка куриная" == "куриная грудка").
   - Stage 3: Substring / token prefix match with word boundary rules.
   - Stage 4: Fuzzy matching with SequenceMatcher (threshold >= 0.85).
4. Candidate suggestions generation for unresolved or ambiguous queries.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

_PUNCTUATION_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Common Russian adjectival and nominal declension suffixes ordered by decreasing length
_RUSSIAN_SUFFIXES = (
    # Adjectives 3-letter & 4-letter
    "ными", "ними", "ского", "скому", "ском", "ской",
    "чный", "чная", "чное", "чные", "чным", "чных", "чную",
    "нный", "нная", "нное", "нные", "нным", "нных", "нную",
    # Adjectives & Participles 2-letter
    "ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие",
    "ую", "юю", "ым", "им", "ом", "ем", "ых", "их",
    "ан", "ен", "на", "но", "ны",
    # Noun plurals & oblique cases 2-letter
    "ами", "ями", "ах", "ях", "ов", "ев", "ей", "ам", "ям",
    # Noun 1-letter
    "а", "я", "о", "е", "у", "ю", "ы", "и", "ь",
)


_HARMLESS_CULINARY_WORDS = frozenset({
    "свежий", "свежая", "свежее", "свежие",
    "спелый", "спелая", "спелое", "спелые",
    "домашний", "домашняя", "домашнее", "домашние", "по", "домашнему",
    "отварной", "отварная", "отварное", "отварные",
    "вареный", "вареная", "вареное", "вареные",
    "жареный", "жареная", "жареное", "жареные",
    "запеченный", "запеченная", "запеченное", "запеченные",
    "сырой", "сырая", "сырое", "сырые",
    "сушеный", "сушеная", "сушеное", "сушеные",
    "нарезанный", "нарезанная", "нарезанное", "нарезанные",
    "порезанный", "порезанная", "порезанное", "порезанные",
    "соломкой", "кусочками", "ломтиками", "кубиками", "кружочками", "половинками",
    "красный", "красная", "красное", "красные",
    "зеленый", "зеленая", "зеленое", "зеленые",
    "желтый", "желтая", "желтое", "желтые",
    "белый", "белая", "белое", "белые",
    "черный", "черная", "черное", "черные",
    "цельный", "цельная", "цельное", "цельные",
})


def normalize_russian_food_text(text: str) -> str:
    """
    Normalizes Russian food text:
    - Strips acute accents (U+0301) without altering 'й'.
    - Replaces 'ё' with 'е'.
    - Removes punctuation (except internal hyphens).
    - Lowercases and collapses whitespaces.
    """
    if not text:
        return ""

    cleaned = text.replace("\u0301", "")
    normalized = unicodedata.normalize("NFC", cleaned).lower()
    normalized = normalized.replace("ё", "е")
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = normalized.replace("-", " ")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def reduce_russian_stem(word: str) -> str:
    """
    Lightweight rule-based suffix reduction for Russian food terminology.
    Preserves word root of at least 3 characters.
    """
    w = word.strip().lower().replace("ё", "е")
    if len(w) <= 3:
        return w

    for suffix in _RUSSIAN_SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break

    # Consonant mutation check at stem boundary (e.g. яблоч -> яблок)
    if w.endswith("ч") and len(w) >= 4:
        w = w[:-1] + "к"

    return w


def extract_stemmed_tokens(text: str) -> tuple[str, ...]:
    """Tokenizes and stems normalized Russian text into a sorted tuple of unique stems."""
    normalized = normalize_russian_food_text(text)
    if not normalized:
        return ()
    tokens = normalized.split()
    stems = {reduce_russian_stem(tok) for tok in tokens if len(tok) >= 2}
    return tuple(sorted(stems))


@dataclass(frozen=True, slots=True)
class SearchMatchCandidate:
    target_id: str
    target_name: str
    similarity: float
    match_stage: str  # exact, token_set, substring, fuzzy


def match_query_against_aliases(
    query: str,
    alias_map: dict[str, str],  # normalized_alias -> target_id
    id_to_name: dict[str, str],  # target_id -> display_name
    id_to_stemmed_tokens: dict[str, tuple[str, ...]] | None = None,
    fuzzy_threshold: float = 0.85,
) -> tuple[str | None, float, str, tuple[str, ...]]:
    """
    Matches query against known aliases using multi-stage matching.

    Returns:
    (matched_target_id, confidence, match_stage, candidate_suggestions)
    """
    norm_query = normalize_russian_food_text(query)
    if not norm_query:
        return None, 0.0, "none", ()

    # Stage 1: Exact match
    if norm_query in alias_map:
        target_id = alias_map[norm_query]
        return target_id, 1.0, "exact", ()

    query_stems = extract_stemmed_tokens(norm_query)

    # Stage 2: Token set match
    if id_to_stemmed_tokens and query_stems:
        query_set = set(query_stems)
        best_token_id: str | None = None
        best_jaccard = 0.0

        for target_id, target_stems in id_to_stemmed_tokens.items():
            target_set = set(target_stems)
            intersection = query_set & target_set
            if not intersection:
                continue
            # Jaccard index
            jaccard = len(intersection) / len(query_set | target_set)
            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_token_id = target_id

        # If all query tokens or target tokens match
        if best_token_id and best_jaccard >= 0.70:
            return best_token_id, round(best_jaccard, 2), "token_set", ()

    # Stage 3: Substring match (whole word contained in query or alias)
    for alias, target_id in alias_map.items():
        if len(alias) >= 4:
            if f" {norm_query} " in f" {alias} ":
                confidence = 0.85
                return target_id, confidence, "substring", ()
            if f" {alias} " in f" {norm_query} ":
                extra = [w for w in norm_query.split() if w not in alias.split()]
                if not extra or all(w in _HARMLESS_CULINARY_WORDS for w in extra):
                    confidence = 0.88 if len(norm_query) > len(alias) else 0.85
                    return target_id, confidence, "substring", ()

    # Stage 4: Fuzzy sequence matching
    best_fuzzy_id: str | None = None
    best_ratio = 0.0
    candidates: list[tuple[float, str]] = []

    for alias, target_id in alias_map.items():
        ratio = difflib.SequenceMatcher(None, norm_query, alias).ratio()
        if ratio >= fuzzy_threshold and ratio > best_ratio:
            best_ratio = ratio
            best_fuzzy_id = target_id
        if ratio >= 0.50:
            candidates.append((ratio, id_to_name.get(target_id, alias)))

    # Also check token overlap for candidate suggestions
    if id_to_stemmed_tokens and query_stems:
        query_set = set(query_stems)
        for target_id, target_stems in id_to_stemmed_tokens.items():
            intersection = query_set & set(target_stems)
            if intersection:
                jaccard = len(intersection) / len(query_set | set(target_stems))
                candidates.append((0.55 + 0.40 * jaccard, id_to_name.get(target_id, target_id)))

    if best_fuzzy_id is not None and best_ratio >= fuzzy_threshold:
        return best_fuzzy_id, round(best_ratio, 2), "fuzzy", ()

    # Collect top suggestions for unresolved query
    candidates.sort(key=lambda x: x[0], reverse=True)
    seen_names = set()
    suggestions: list[str] = []
    for _, name in candidates:
        if name not in seen_names:
            seen_names.add(name)
            suggestions.append(name)
            if len(suggestions) >= 5:
                break

    return None, 0.0, "none", tuple(suggestions)


# Alias for compatibility
normalize_russian_food_query = normalize_russian_food_text


def find_candidate_suggestions(
    query: str,
    candidate_names: Iterable[str],
    limit: int = 5,
) -> list[str]:
    """Finds top candidate suggestions for an unresolved query from candidate names."""
    norm_query = normalize_russian_food_text(query)
    query_stems = set(extract_stemmed_tokens(norm_query))

    scored: list[tuple[float, str]] = []
    for cand in candidate_names:
        norm_cand = normalize_russian_food_text(cand)
        ratio = difflib.SequenceMatcher(None, norm_query, norm_cand).ratio()
        if query_stems:
            cand_stems = set(extract_stemmed_tokens(norm_cand))
            overlap = len(query_stems & cand_stems)
            if overlap:
                ratio = max(ratio, 0.50 + 0.15 * overlap)
        if ratio >= 0.40:
            scored.append((ratio, cand))

    scored.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    out = []
    for _, name in scored:
        if name not in seen:
            seen.add(name)
            out.append(name)
            if len(out) >= limit:
                break
    return out
