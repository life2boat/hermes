"""Strict byte-preserving JSON override transformation."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from ops.secret_remediation_r1.constants import PROTECTED_NAMES


class JsonOverrideError(Exception):
    pass


@dataclass(frozen=True)
class JsonMember:
    key: str
    start: int
    end: int
    value: "JsonNode"


@dataclass(frozen=True)
class JsonNode:
    kind: str
    start: int
    end: int
    value: Any
    members: tuple[JsonMember, ...] = ()


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.length = len(text)
        self.pos = 0

    def parse(self) -> JsonNode:
        self._ws()
        node = self._value()
        self._ws()
        if self.pos != self.length:
            self._fail()
        return node

    def _fail(self) -> None:
        raise JsonOverrideError("Malformed or ambiguous JSON override")

    def _ws(self) -> None:
        while self.pos < self.length and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def _value(self) -> JsonNode:
        if self.pos >= self.length:
            self._fail()
        char = self.text[self.pos]
        if char == "{":
            return self._object()
        if char == "[":
            return self._array()
        if char == '"':
            start = self.pos
            value = self._string()
            return JsonNode("string", start, self.pos, value)
        for literal, value in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(literal, self.pos):
                start = self.pos
                self.pos += len(literal)
                return JsonNode("literal", start, self.pos, value)
        match = re.match(
            r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",
            self.text[self.pos :],
        )
        if match:
            start = self.pos
            self.pos += len(match.group(0))
            try:
                value = json.loads(match.group(0))
            except json.JSONDecodeError:
                self._fail()
            return JsonNode("number", start, self.pos, value)
        self._fail()

    def _string(self) -> str:
        start = self.pos
        self.pos += 1
        while self.pos < self.length:
            char = self.text[self.pos]
            if char == '"':
                self.pos += 1
                try:
                    value = json.loads(self.text[start : self.pos])
                except json.JSONDecodeError:
                    self._fail()
                if not isinstance(value, str):
                    self._fail()
                return value
            if ord(char) < 0x20:
                self._fail()
            if char == "\\":
                self.pos += 1
                if self.pos >= self.length:
                    self._fail()
                escape = self.text[self.pos]
                if escape == "u":
                    digits = self.text[self.pos + 1 : self.pos + 5]
                    if len(digits) != 4 or not all(
                        c in "0123456789abcdefABCDEF" for c in digits
                    ):
                        self._fail()
                    self.pos += 5
                    continue
                if escape not in '"\\/bfnrt':
                    self._fail()
            self.pos += 1
        self._fail()

    def _object(self) -> JsonNode:
        start = self.pos
        self.pos += 1
        self._ws()
        members: list[JsonMember] = []
        values: dict[str, Any] = {}
        if self.pos < self.length and self.text[self.pos] == "}":
            self.pos += 1
            return JsonNode("object", start, self.pos, values, tuple(members))
        while True:
            self._ws()
            member_start = self.pos
            if self.pos >= self.length or self.text[self.pos] != '"':
                self._fail()
            key = self._string()
            if key in values:
                raise JsonOverrideError("Duplicate JSON object key")
            self._ws()
            if self.pos >= self.length or self.text[self.pos] != ":":
                self._fail()
            self.pos += 1
            self._ws()
            value = self._value()
            values[key] = value.value
            members.append(JsonMember(key, member_start, value.end, value))
            self._ws()
            if self.pos >= self.length:
                self._fail()
            if self.text[self.pos] == "}":
                self.pos += 1
                return JsonNode("object", start, self.pos, values, tuple(members))
            if self.text[self.pos] != ",":
                self._fail()
            self.pos += 1
            self._ws()
            if self.pos < self.length and self.text[self.pos] == "}":
                self._fail()

    def _array(self) -> JsonNode:
        start = self.pos
        self.pos += 1
        self._ws()
        values: list[Any] = []
        if self.pos < self.length and self.text[self.pos] == "]":
            self.pos += 1
            return JsonNode("array", start, self.pos, values)
        while True:
            value = self._value()
            values.append(value.value)
            self._ws()
            if self.pos >= self.length:
                self._fail()
            if self.text[self.pos] == "]":
                self.pos += 1
                return JsonNode("array", start, self.pos, values)
            if self.text[self.pos] != ",":
                self._fail()
            self.pos += 1
            self._ws()
            if self.pos < self.length and self.text[self.pos] == "]":
                self._fail()


def _member(node: JsonNode, key: str) -> JsonMember:
    if node.kind != "object":
        raise JsonOverrideError("Unsupported JSON override structure")
    matches = [member for member in node.members if member.key == key]
    if len(matches) != 1:
        raise JsonOverrideError(f"Required JSON member missing: {key}")
    return matches[0]


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonOverrideError("Duplicate JSON object key")
        result[key] = value
    return result


def _independent_load(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                JsonOverrideError("Non-standard JSON constant")
            ),
        )
    except json.JSONDecodeError as exc:
        raise JsonOverrideError("Malformed JSON override") from exc


def _removal_spans(
    members: tuple[JsonMember, ...], remove: set[int]
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(members):
        if index not in remove:
            index += 1
            continue
        first = index
        while index + 1 < len(members) and index + 1 in remove:
            index += 1
        last = index
        if last + 1 < len(members):
            span = (members[first].start, members[last + 1].start)
        elif first > 0:
            span = (members[first - 1].end, members[last].end)
        else:
            span = (members[first].start, members[last].end)
        if span[0] >= span[1]:
            raise JsonOverrideError("Ambiguous JSON removal span")
        spans.append(span)
        index += 1
    for previous, current in zip(spans, spans[1:]):
        if previous[1] > current[0]:
            raise JsonOverrideError("Overlapping JSON removal spans")
    return spans


def _remove_exact_spans(text: str, spans: list[tuple[int, int]]) -> str:
    retained: list[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor or end > len(text):
            raise JsonOverrideError("Invalid JSON removal span")
        retained.append(text[cursor:start])
        cursor = end
    retained.append(text[cursor:])
    return "".join(retained)


def transform_json_override(original_bytes: bytes) -> bytes:
    """Remove protected members from the one supported JSON environment shape."""
    try:
        text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonOverrideError("JSON override is not valid UTF-8") from exc

    root = _Parser(text).parse()
    services = _member(root, "services").value
    bot = _member(services, "hermes-bot").value
    environment_member = _member(bot, "environment")
    environment = environment_member.value
    if environment.kind != "object":
        raise JsonOverrideError("Unsupported JSON environment shape")
    if any(
        member.value.kind not in {"string", "number", "literal"}
        for member in environment.members
    ):
        raise JsonOverrideError("Unsupported JSON environment value")

    remove = {
        index
        for index, member in enumerate(environment.members)
        if member.key in PROTECTED_NAMES
    }
    spans = _removal_spans(environment.members, remove)
    transformed_text = _remove_exact_spans(text, spans)

    original_semantic = _independent_load(text)
    transformed_semantic = _independent_load(transformed_text)
    expected = copy.deepcopy(original_semantic)
    expected_environment = expected["services"]["hermes-bot"]["environment"]
    if not isinstance(expected_environment, dict):
        raise JsonOverrideError("Unsupported JSON environment shape")
    for key in PROTECTED_NAMES:
        expected_environment.pop(key, None)
    if transformed_semantic != expected:
        raise JsonOverrideError("Non-protected JSON content changed")
    transformed_environment = transformed_semantic["services"]["hermes-bot"][
        "environment"
    ]
    if set(transformed_environment) & PROTECTED_NAMES:
        raise JsonOverrideError("Protected JSON environment binding remains")
    return transformed_text.encode("utf-8")
