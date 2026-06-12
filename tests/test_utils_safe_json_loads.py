from utils import safe_json_loads


def test_safe_json_loads_accepts_fenced_json():
    raw = "```json\n{\"ok\": true, \"count\": 1}\n```"
    assert safe_json_loads(raw) == {"ok": True, "count": 1}


def test_safe_json_loads_extracts_json_from_prose():
    raw = "Here is the result: {\"success\": true, \"analysis\": \"done\"} Thanks!"
    assert safe_json_loads(raw) == {"success": True, "analysis": "done"}


def test_safe_json_loads_returns_default_on_unparseable_input():
    sentinel = {"fallback": True}
    assert safe_json_loads("not json at all", sentinel) is sentinel
