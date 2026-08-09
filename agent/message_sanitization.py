"""Message and tool-payload sanitization helpers.

Pure functions extracted from ``run_agent.py`` so the AIAgent module can
stay focused on the conversation loop.  These walk OpenAI-format message
lists and structured payloads, repairing or stripping problematic
characters that would otherwise crash ``json.dumps`` inside the OpenAI
SDK or be rejected by upstream APIs.

All helpers are stateless and side-effect-free except for in-place
mutation of their input (where documented).  Backward-compatible
re-exports from ``run_agent`` remain in place so existing imports
``from run_agent import _sanitize_surrogates`` keep working.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Lone surrogate code points are invalid in UTF-8 and crash json.dumps
# inside the OpenAI SDK.  Used by every surrogate-sanitization helper
# below as well as by run_agent and the CLI for paste-from-clipboard
# scrubbing.
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


# Durable consumers (session snapshots, background review, and Memory OS
# providers) must never receive the image payload that the foreground model
# needs for the current request. Keep the placeholder stable so downstream
# code and tests can recognize the boundary without learning anything about
# the source URL/path or the image bytes.
DURABLE_IMAGE_PLACEHOLDER = '[Image omitted from durable context]'
_DURABLE_IMAGE_REFERENCE_PLACEHOLDER = '[Image reference omitted from durable context]'
_DURABLE_BINARY_PLACEHOLDER = '[Binary payload omitted from durable context]'

_DURABLE_IMAGE_PART_TYPES = frozenset({'image', 'image_url', 'input_image'})
_DURABLE_IMAGE_REFERENCE_KEYS = frozenset({
    'browser_screenshot',
    'browser_screenshots',
    'image_base64',
    'image_bytes',
    'image_data',
    'image_path',
    'image_paths',
    'image_uri',
    'image_uris',
    'image_url',
    'image_urls',
    'input_image',
    'screenshot_base64',
    'screenshot_bytes',
    'screenshot_data',
    'screenshot_path',
    'screenshot_paths',
    'screenshot_uri',
    'screenshot_uris',
    'screenshot_url',
    'screenshot_urls',
    'screenshot',
    'screenshots',
})
_DATA_IMAGE_URL_RE = re.compile(
    r'data:image/[a-z0-9.+-]+(?:;[a-z0-9.+_=-]+)*,[a-z0-9+/=_-]+',
    re.IGNORECASE,
)
_REMOTE_IMAGE_URL_RE = re.compile(
    r'https?://[^\s<>\x22\x27\]\[]+\.(?:avif|gif|heic|jpeg|jpg|png|webp)'
    r'(?:\?[^\s<>\x22\x27\]\[]*)?',
    re.IGNORECASE,
)
_NAMED_SCREENSHOT_URL_RE = re.compile(
    r'(?i)(?P<label>\b(?:screenshot(?:s)?[ _]?(?:url|uri)|'
    r'opaque[ _]+screenshot))\s*(?P<separator>[:=])\s*'
    r'(?P<url>(?:https?://|data:image/)[^\s<>\x22\x27\]\[]+)',
)
_DURABLE_IMAGE_CACHE_PATH_RE = re.compile(
    r'(?:(?:[a-z]:[\\/])|/|~/)(?:[^\s<>\x22\x27\]\[]+[\\/])*'
    r'(?:cache[\\/](?:images|screenshots)|image_cache|browser_screenshots)'
    r'[\\/][^\s<>\x22\x27\]\[]+',
    re.IGNORECASE,
)
_DURABLE_BROWSER_SCREENSHOT_PATH_RE = re.compile(
    r'(?:(?:[a-z]:[\\/])|/|~/)(?:[^\s<>\x22\x27\]\[]+[\\/])*'
    r'browser_screenshot(?:_[^\s<>\x22\x27\]\[/\\]*)?'
    r'\.(?:avif|gif|heic|jpeg|jpg|png|webp)',
    re.IGNORECASE,
)
_DURABLE_RELATIVE_SCREENSHOT_CACHE_PATH_RE = re.compile(
    r'(?<![A-Za-z0-9_.-])(?:\.[\\/])?'
    r'(?:cache[\\/](?:images|screenshots)|image_cache|browser_screenshots)'
    r'[\\/][^\s<>\x22\x27\]\[]+',
    re.IGNORECASE,
)
_DURABLE_RELATIVE_BROWSER_SCREENSHOT_PATH_RE = re.compile(
    r'(?<![A-Za-z0-9_.-])(?:\.[\\/])?browser_screenshot'
    r'(?:_[^\s<>\x22\x27\]\[/\\]*)?'
    r'\.(?:avif|gif|heic|jpeg|jpg|png|webp)',
    re.IGNORECASE,
)
_GENERATED_IMAGE_ATTACHMENT_HINT_RE = re.compile(
    r'\[Image attached(?: at)?:[^\]\r\n]*\]',
    re.IGNORECASE,
)
_GENERATED_VISION_REEXAMINE_HINT_RE = re.compile(
    r'\[If you need a closer look,\s*use vision_analyze with\s+'
    r'image_url:\s*[^\]\r\n]*\]',
    re.IGNORECASE,
)
_JSON_IMAGE_FIELD_RE = re.compile(
    r'(?:\x22)?\b(?:(?:image|screenshot)_'
    r'(?:base64|bytes|data|path|paths|uri|uris|url|urls)|input_image|'
    r'browser_screenshots?|screenshots?)'
    r'\b(?:\x22)?\s*:',
    re.IGNORECASE,
)


def _sanitize_durable_multimodal_text(text: str) -> str:
    '''Remove generated image references from a durable text value.

    Ordinary prose and ordinary filesystem paths are intentionally left alone.
    Raw data/image URLs, recognized remote image URLs, Hermes cache paths, and
    generated media-reference annotations are rewritten. JSON-encoded tool
    arguments are parsed only when they contain a known image field, allowing
    nested vision payloads to cross the same boundary safely.
    '''
    sanitized = _DATA_IMAGE_URL_RE.sub(_DURABLE_IMAGE_REFERENCE_PLACEHOLDER, text)
    sanitized = _REMOTE_IMAGE_URL_RE.sub(
        _DURABLE_IMAGE_REFERENCE_PLACEHOLDER,
        sanitized,
    )
    sanitized = _NAMED_SCREENSHOT_URL_RE.sub(
        lambda match: (
            f"{match.group('label')}{match.group('separator')} "
            f"{_DURABLE_IMAGE_REFERENCE_PLACEHOLDER}"
        ),
        sanitized,
    )
    sanitized = _DURABLE_IMAGE_CACHE_PATH_RE.sub(
        _DURABLE_IMAGE_REFERENCE_PLACEHOLDER,
        sanitized,
    )
    sanitized = _DURABLE_BROWSER_SCREENSHOT_PATH_RE.sub(
        _DURABLE_IMAGE_REFERENCE_PLACEHOLDER,
        sanitized,
    )
    sanitized = _DURABLE_RELATIVE_SCREENSHOT_CACHE_PATH_RE.sub(
        _DURABLE_IMAGE_REFERENCE_PLACEHOLDER,
        sanitized,
    )
    sanitized = _DURABLE_RELATIVE_BROWSER_SCREENSHOT_PATH_RE.sub(
        _DURABLE_IMAGE_REFERENCE_PLACEHOLDER,
        sanitized,
    )
    sanitized = _GENERATED_IMAGE_ATTACHMENT_HINT_RE.sub(
        DURABLE_IMAGE_PLACEHOLDER,
        sanitized,
    )
    sanitized = _GENERATED_VISION_REEXAMINE_HINT_RE.sub(
        _DURABLE_IMAGE_REFERENCE_PLACEHOLDER,
        sanitized,
    )

    candidate = sanitized.lstrip()
    if candidate.startswith(('{', '[')) and _JSON_IMAGE_FIELD_RE.search(sanitized):
        try:
            parsed = json.loads(sanitized)
        except (json.JSONDecodeError, TypeError, ValueError):
            return sanitized
        cleaned = sanitize_durable_multimodal_payload(parsed)
        if cleaned != parsed:
            return json.dumps(cleaned, ensure_ascii=False, separators=(',', ':'))
    return sanitized


def sanitize_durable_multimodal_payload(payload: Any, *, _parent_key: str = '') -> Any:
    '''Return a recursively sanitized copy for durable/secondary consumers.

    This helper is deliberately not used on the foreground provider request:
    the current model still receives the original image. At persistence and
    secondary-consumer boundaries it replaces OpenAI, Responses, and Anthropic
    image blocks, key-addressed image and screenshot references, raw image
    data URLs, binary values, and Hermes-generated cache-path hints with
    stable placeholders.

    The input is never mutated. Non-image text and structure are preserved.
    '''
    normalized_parent = str(_parent_key or '').lower()
    if normalized_parent in _DURABLE_IMAGE_REFERENCE_KEYS:
        if isinstance(payload, (list, tuple)):
            placeholders = [_DURABLE_IMAGE_REFERENCE_PLACEHOLDER for _ in payload]
            return tuple(placeholders) if isinstance(payload, tuple) else placeholders
        return _DURABLE_IMAGE_REFERENCE_PLACEHOLDER

    if isinstance(payload, dict):
        part_type = str(payload.get('type') or '').lower()
        if part_type in _DURABLE_IMAGE_PART_TYPES:
            return {
                'type': 'text',
                'text': DURABLE_IMAGE_PLACEHOLDER,
            }
        return {
            key: sanitize_durable_multimodal_payload(value, _parent_key=str(key))
            for key, value in payload.items()
        }

    if isinstance(payload, list):
        return [sanitize_durable_multimodal_payload(value) for value in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize_durable_multimodal_payload(value) for value in payload)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return _DURABLE_BINARY_PLACEHOLDER
    if isinstance(payload, str):
        return _sanitize_durable_multimodal_text(payload)
    return payload


def _sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD (replacement character).

    Surrogates are invalid in UTF-8 and will crash ``json.dumps()`` inside the
    OpenAI SDK.  This is a fast no-op when the text contains no surrogates.
    """
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub('\ufffd', text)
    return text


def _sanitize_structure_surrogates(payload: Any) -> bool:
    """Replace surrogate code points in nested dict/list payloads in-place.

    Mirror of ``_sanitize_structure_non_ascii`` but for surrogate recovery.
    Used to scrub nested structured fields (e.g. ``reasoning_details`` — an
    array of dicts with ``summary``/``text`` strings) that flat per-field
    checks don't reach.  Returns True if any surrogates were replaced.
    """
    found = False

    def _walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[key] = _SURROGATE_RE.sub('\ufffd', value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[idx] = _SURROGATE_RE.sub('\ufffd', value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


def _sanitize_messages_surrogates(messages: list) -> bool:
    """Sanitize surrogate characters from all string content in a messages list.

    Walks message dicts in-place. Returns True if any surrogates were found
    and replaced, False otherwise. Covers content/text, name, tool call
    metadata/arguments, AND any additional string or nested structured fields
    (``reasoning``, ``reasoning_content``, ``reasoning_details``, etc.) so
    retries don't fail on a non-content field.  Byte-level reasoning models
    (xiaomi/mimo, kimi, glm) can emit lone surrogates in reasoning output
    that flow through to ``api_messages["reasoning_content"]`` on the next
    turn and crash json.dumps inside the OpenAI SDK.
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub('\ufffd', content)
            found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and _SURROGATE_RE.search(text):
                        part["text"] = _SURROGATE_RE.sub('\ufffd', text)
                        found = True
        name = msg.get("name")
        if isinstance(name, str) and _SURROGATE_RE.search(name):
            msg["name"] = _SURROGATE_RE.sub('\ufffd', name)
            found = True
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and _SURROGATE_RE.search(tc_id):
                    tc["id"] = _SURROGATE_RE.sub('\ufffd', tc_id)
                    found = True
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn_name = fn.get("name")
                    if isinstance(fn_name, str) and _SURROGATE_RE.search(fn_name):
                        fn["name"] = _SURROGATE_RE.sub('\ufffd', fn_name)
                        found = True
                    fn_args = fn.get("arguments")
                    if isinstance(fn_args, str) and _SURROGATE_RE.search(fn_args):
                        fn["arguments"] = _SURROGATE_RE.sub('\ufffd', fn_args)
                        found = True
        # Walk any additional string / nested fields (reasoning,
        # reasoning_content, reasoning_details, etc.) — surrogates from
        # byte-level reasoning models (xiaomi/mimo, kimi, glm) can lurk
        # in these fields and aren't covered by the per-field checks above.
        # Matches _sanitize_messages_non_ascii's coverage (PR #10537).
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                if _SURROGATE_RE.search(value):
                    msg[key] = _SURROGATE_RE.sub('\ufffd', value)
                    found = True
            elif isinstance(value, (dict, list)):
                if _sanitize_structure_surrogates(value):
                    found = True
    return found


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control chars inside JSON string values.

    Walks the raw JSON character-by-character, tracking whether we are
    inside a double-quoted string. Inside strings, replaces literal
    control characters (0x00-0x1F) that aren't already part of an escape
    sequence with their ``\\uXXXX`` equivalents. Pass-through for everything
    else.

    Ported from #12093 — complements the other repair passes in
    ``_repair_tool_call_arguments`` when ``json.loads(strict=False)`` is
    not enough (e.g. llama.cpp backends that emit literal apostrophes or
    tabs alongside other malformations).
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                # Already-escaped char — pass through as-is
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


def _repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Attempt to repair malformed tool_call argument JSON.

    Models like GLM-5.1 via Ollama can produce truncated JSON, trailing
    commas, Python ``None``, etc.  The API proxy rejects these with HTTP 400
    "invalid tool call arguments".  This function applies common repairs;
    if all fail it returns ``"{}"`` so the request succeeds (better than
    crashing the session).  All repairs are logged at WARNING level.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    # Fast-path: empty / whitespace-only -> empty object
    if not raw_stripped:
        logger.warning("Sanitized empty tool_call arguments for %s", tool_name)
        return "{}"

    # Python-literal None -> normalise to {}
    if raw_stripped == "None":
        logger.warning("Sanitized Python-None tool_call arguments for %s", tool_name)
        return "{}"

    # Repair pass 0: llama.cpp backends sometimes emit literal control
    # characters (tabs, newlines) inside JSON string values. json.loads
    # with strict=False accepts these and lets us re-serialise the
    # result into wire-valid JSON without any string surgery. This is
    # the most common local-model repair case (#12068).
    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"))
        if reserialised != raw_stripped:
            logger.warning(
                "Repaired unescaped control chars in tool_call arguments for %s",
                tool_name,
            )
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Attempt common JSON repairs
    fixed = raw_stripped
    # 1. Strip trailing commas before } or ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # 2. Close unclosed structures
    open_curly = fixed.count('{') - fixed.count('}')
    open_bracket = fixed.count('[') - fixed.count(']')
    if open_curly > 0:
        fixed += '}' * open_curly
    if open_bracket > 0:
        fixed += ']' * open_bracket
    # 3. Remove excess closing braces/brackets (bounded to 50 iterations)
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith('}') and fixed.count('}') > fixed.count('{'):
                fixed = fixed[:-1]
            elif fixed.endswith(']') and fixed.count(']') > fixed.count('['):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning(
            "Repaired malformed tool_call arguments for %s: %s → %s",
            tool_name, raw_stripped[:80], fixed[:80],
        )
        return fixed
    except json.JSONDecodeError:
        pass

    # Repair pass 4: escape unescaped control chars inside JSON strings,
    # then retry. Catches cases where strict=False alone fails because
    # other malformations are present too.
    try:
        escaped = _escape_invalid_chars_in_json_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            logger.warning(
                "Repaired control-char-laced tool_call arguments for %s: %s → %s",
                tool_name, raw_stripped[:80], escaped[:80],
            )
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Last resort: replace with empty object so the API request doesn't
    # crash the entire session.
    logger.warning(
        "Unrepairable tool_call arguments for %s — "
        "replaced with empty object (was: %s)",
        tool_name, raw_stripped[:80],
    )
    return "{}"


def _strip_non_ascii(text: str) -> str:
    """Remove non-ASCII characters, replacing with closest ASCII equivalent or removing.

    Used as a last resort when the system encoding is ASCII and can't handle
    any non-ASCII characters (e.g. LANG=C on Chromebooks).
    """
    return text.encode('ascii', errors='ignore').decode('ascii')


def _sanitize_messages_non_ascii(messages: list) -> bool:
    """Strip non-ASCII characters from all string content in a messages list.

    This is a last-resort recovery for systems with ASCII-only encoding
    (LANG=C, Chromebooks, minimal containers).  Returns True if any
    non-ASCII content was found and sanitized.
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Sanitize content (string)
        content = msg.get("content")
        if isinstance(content, str):
            sanitized = _strip_non_ascii(content)
            if sanitized != content:
                msg["content"] = sanitized
                found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        sanitized = _strip_non_ascii(text)
                        if sanitized != text:
                            part["text"] = sanitized
                            found = True
        # Sanitize name field (can contain non-ASCII in tool results)
        name = msg.get("name")
        if isinstance(name, str):
            sanitized = _strip_non_ascii(name)
            if sanitized != name:
                msg["name"] = sanitized
                found = True
        # Sanitize tool_calls
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    if isinstance(fn, dict):
                        fn_args = fn.get("arguments")
                        if isinstance(fn_args, str):
                            sanitized = _strip_non_ascii(fn_args)
                            if sanitized != fn_args:
                                fn["arguments"] = sanitized
                                found = True
        # Sanitize any additional top-level string fields (e.g. reasoning_content)
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                sanitized = _strip_non_ascii(value)
                if sanitized != value:
                    msg[key] = sanitized
                    found = True
    return found


def _sanitize_tools_non_ascii(tools: list) -> bool:
    """Strip non-ASCII characters from tool payloads in-place."""
    return _sanitize_structure_non_ascii(tools)


def _strip_images_from_messages(messages: list) -> bool:
    """Remove image_url content parts from all messages in-place.

    Called when a server signals it does not support images (e.g.
    "Only 'text' content type is supported.").  Mutates messages so the
    next API call sends text only.

    Preserves message alternation invariants:
      * ``tool``-role messages whose content was entirely images are replaced
        with a plaintext placeholder, NOT deleted — deleting them would leave
        the paired ``tool_call_id`` on the prior assistant message unmatched,
        which providers reject with HTTP 400.
      * Non-tool messages whose content becomes empty are dropped.  In
        practice this only hits synthetic image-only user messages appended
        for attachment delivery; real user turns always include text.

    Returns True if any image parts were removed.
    """
    found = False
    to_delete = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"image_url", "image", "input_image"}:
                found = True
            else:
                new_parts.append(part)
        if len(new_parts) < len(content):
            if new_parts:
                msg["content"] = new_parts
            elif msg.get("role") == "tool":
                # Preserve tool_call_id linkage — providers require every
                # assistant tool_call to have a matching tool response.
                msg["content"] = "[image content removed — server does not support images]"
            else:
                # Synthetic image-only user/assistant message with no text;
                # safe to drop.
                to_delete.append(i)
    for i in reversed(to_delete):
        del messages[i]
    return found


def _sanitize_structure_non_ascii(payload: Any) -> bool:
    """Strip non-ASCII characters from nested dict/list payloads in-place."""
    found = False

    def _walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    sanitized = _strip_non_ascii(value)
                    if sanitized != value:
                        node[key] = sanitized
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    sanitized = _strip_non_ascii(value)
                    if sanitized != value:
                        node[idx] = sanitized
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


__all__ = [
    'DURABLE_IMAGE_PLACEHOLDER',
    'sanitize_durable_multimodal_payload',
    "_SURROGATE_RE",
    "_sanitize_surrogates",
    "_sanitize_structure_surrogates",
    "_sanitize_messages_surrogates",
    "_escape_invalid_chars_in_json_strings",
    "_repair_tool_call_arguments",
    "_strip_non_ascii",
    "_sanitize_messages_non_ascii",
    "_sanitize_tools_non_ascii",
    "_strip_images_from_messages",
    "_sanitize_structure_non_ascii",
]
