"""Strict JSON override byte-preservation regressions."""

import json

import pytest

from ops.secret_remediation_r1.override_transform import (
    OverrideTransformError,
    transform_override,
)


def _transform(tmp_path, content: bytes, *, same: bool = False) -> bytes:
    source = tmp_path / "override.json"
    source.write_bytes(content)
    destination = source if same else tmp_path / "result.json"
    transform_override(str(source), str(destination))
    return destination.read_bytes()


def _document(environment: bytes) -> bytes:
    return (
        b'{"meta":{"note":"keep"},"services":{"hermes-bot":{"environment":'
        + environment
        + b',"image":"legacy"},"other":{"environment":{"TELEGRAM_BOT_TOKEN":"keep"}}}}'
    )


def test_json_protected_member_only(tmp_path):
    result = _transform(tmp_path, _document(b'{"TELEGRAM_BOT_TOKEN":"synthetic"}'))
    assert b'"environment":{}' in result


def test_json_protected_first_member(tmp_path):
    result = _transform(
        tmp_path,
        _document(b'{"TELEGRAM_BOT_TOKEN":"synthetic",  "NORMAL":"1"}'),
    )
    assert b'"environment":{"NORMAL":"1"}' in result


def test_json_protected_middle_member(tmp_path):
    result = _transform(
        tmp_path,
        _document(b'{"FIRST":"1", "TELEGRAM_BOT_TOKEN":"synthetic", "LAST":"2"}'),
    )
    assert b'"environment":{"FIRST":"1", "LAST":"2"}' in result


def test_json_protected_last_member(tmp_path):
    result = _transform(
        tmp_path,
        _document(b'{"NORMAL":"1",  "TELEGRAM_BOT_TOKEN":"synthetic"}'),
    )
    assert b'"environment":{"NORMAL":"1"}' in result


def test_json_multiple_protected_members(tmp_path):
    result = _transform(
        tmp_path,
        _document(
            b'{"TELEGRAM_BOT_TOKEN":"synthetic","NORMAL":"1",'
            b'"DASHSCOPE_API_KEY":"synthetic-two"}'
        ),
    )
    assert json.loads(result)["services"]["hermes-bot"]["environment"] == {
        "NORMAL": "1"
    }


def test_json_nonprotected_members_preserved(tmp_path):
    content = _document(
        b'{"NORMAL":"quoted value","TELEGRAM_BOT_TOKEN":"synthetic","COUNT":7}'
    )
    result = _transform(tmp_path, content)
    assert b'"NORMAL":"quoted value"' in result
    assert b'"COUNT":7' in result
    assert b'"other":{"environment":{"TELEGRAM_BOT_TOKEN":"keep"}}' in result


def test_json_nested_unrelated_content_preserved_byte_exact(tmp_path):
    prefix = b'{"meta":{"array":[1,{"nested":"value"}],"enabled":true},"services":'
    suffix = b',"tail":{"x":[false,null,3.5]}}'
    content = (
        prefix
        + b'{"hermes-bot":{"environment":{"TELEGRAM_BOT_TOKEN":"synthetic"}}}'
        + suffix
    )
    result = _transform(tmp_path, content)
    assert result.startswith(prefix)
    assert result.endswith(suffix)


def test_json_spaces_preserved(tmp_path):
    content = _document(
        b'{  "NORMAL"  :  "1"  ,  "TELEGRAM_BOT_TOKEN" : "synthetic"  }'
    )
    result = _transform(tmp_path, content)
    assert b'{  "NORMAL"  :  "1"  }' in result


def test_json_newlines_preserved(tmp_path):
    content = (
        b'{\n  "services": {\n    "hermes-bot": {\n'
        b'      "environment": {\n        "NORMAL": "1",\n'
        b'        "TELEGRAM_BOT_TOKEN": "synthetic"\n      }\n'
        b"    }\n  }\n}\n"
    )
    result = _transform(tmp_path, content)
    assert result == (
        b'{\n  "services": {\n    "hermes-bot": {\n'
        b'      "environment": {\n        "NORMAL": "1"\n      }\n'
        b"    }\n  }\n}\n"
    )


def test_json_escaped_quote_preserved(tmp_path):
    result = _transform(
        tmp_path,
        _document(b'{"NORMAL":"say \\"hello\\"","TELEGRAM_BOT_TOKEN":"synthetic"}'),
    )
    assert b'say \\"hello\\"' in result


def test_json_escaped_backslash_preserved(tmp_path):
    result = _transform(
        tmp_path,
        _document(b'{"NORMAL":"C:\\\\safe\\\\path","TELEGRAM_BOT_TOKEN":"synthetic"}'),
    )
    assert b"C:\\\\safe\\\\path" in result


@pytest.mark.parametrize(
    "content",
    [
        b'{"services":{"hermes-bot":{"environment":{"TELEGRAM_BOT_TOKEN":"a","TELEGRAM_BOT_TOKEN":"b"}}}}',
        b'{"services":{},"services":{"hermes-bot":{"environment":{}}}}',
        b'{"services":{"hermes-bot":{"environment":{}},"hermes-bot":{"environment":{}}}}',
        b'{"services":{"hermes-bot":{"environment":{},"environment":{}}}}',
    ],
    ids=[
        "duplicate-protected",
        "duplicate-services",
        "duplicate-hermes-bot",
        "duplicate-environment",
    ],
)
def test_json_duplicate_keys_rejected(tmp_path, content):
    with pytest.raises(OverrideTransformError, match="Duplicate JSON object key"):
        _transform(tmp_path, content)


def test_json_malformed_rejected(tmp_path):
    with pytest.raises(OverrideTransformError, match="Malformed"):
        _transform(tmp_path, b'{"services":{"hermes-bot":{"environment":{')


def test_json_unsupported_environment_shape_rejected(tmp_path):
    with pytest.raises(OverrideTransformError, match="Unsupported JSON environment"):
        _transform(
            tmp_path,
            b'{"services":{"hermes-bot":{"environment":["NORMAL=1"]}}}',
        )


def test_json_nested_environment_value_rejected(tmp_path):
    with pytest.raises(
        OverrideTransformError, match="Unsupported JSON environment value"
    ):
        _transform(
            tmp_path,
            b'{"services":{"hermes-bot":{"environment":{"NORMAL":{"x":1}}}}}',
        )


def test_json_no_protected_member_is_byte_identical(tmp_path):
    content = _document(b'{"NORMAL":"1","COUNT":2,"ENABLED":true}')
    assert _transform(tmp_path, content) == content


def test_json_source_equals_destination_safe(tmp_path):
    content = _document(b'{"NORMAL":"1","TELEGRAM_BOT_TOKEN":"synthetic","LAST":"2"}')
    result = _transform(tmp_path, content, same=True)
    assert json.loads(result)["services"]["hermes-bot"]["environment"] == {
        "NORMAL": "1",
        "LAST": "2",
    }
    assert b"synthetic" not in result
