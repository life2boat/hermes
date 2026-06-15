import json

from toolsets import resolve_toolset
from tools import healbite_public_search as hps


class FakeProvider:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = []

    def search(self, query: str, limit: int = 5):
        self.calls.append({"query": query, "limit": limit})
        if self.error is not None:
            raise self.error
        return {"success": True, "data": {"web": self.results}}


def _payload(raw: str) -> dict:
    return json.loads(raw)


def test_food_calorie_query_is_anonymized():
    query = hps.PublicNutritionSearchQuery(
        topic="калорийность гуляша с картофельным пюре",
        category="food_calories",
    )

    sanitized = hps.sanitize_public_query(query)

    assert sanitized.topic == "гуляша с картофельным пюре"
    assert sanitized.normalized_query.endswith("гуляша с картофельным пюре")


def test_personal_data_is_removed_before_public_search():
    query = hps.PublicNutritionSearchQuery(
        topic=(
            "меня зовут Анастасия, мой вес 65 кг, telegram id 248875361, "
            "калорийность гуляша"
        ),
        category="food_calories",
    )

    sanitized = hps.sanitize_public_query(query)

    assert "анастас" not in sanitized.topic.lower()
    assert "248875361" not in sanitized.topic
    assert "65" not in sanitized.topic
    assert "telegram" not in sanitized.topic.lower()
    assert sanitized.removed_sensitive_data is True


def test_diagnosis_is_not_forwarded_to_public_search():
    query = hps.PublicNutritionSearchQuery(
        topic="у меня диабет 2 типа, Harvard Plate",
        category="nutrition",
    )

    sanitized = hps.sanitize_public_query(query)

    assert "диаб" not in sanitized.topic.lower()
    assert "harvard" in sanitized.topic.lower()


def test_internal_reference_short_circuits_external_search(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = FakeProvider()
    monkeypatch.setattr(hps, "get_active_search_provider", lambda: provider)

    result = _payload(
        hps.healbite_public_search_tool(
            "тарелка гарварда",
            category="harvard_plate",
        )
    )

    assert result["ok"] is True
    assert result["source_mode"] == "internal"
    assert result["sources"][0]["domain"] == "nutritionsource.hsph.harvard.edu"
    assert provider.calls == []


def test_external_search_runs_when_internal_data_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = FakeProvider(
        results=[
            {
                "title": "USDA result",
                "url": "https://fdc.nal.usda.gov/fdc-app.html#/food-details/12345",
                "description": "Reference entry with energy information per serving.",
            }
        ]
    )
    monkeypatch.setattr(hps, "get_active_search_provider", lambda: provider)

    result = _payload(
        hps.healbite_public_search_tool(
            "редкий азиатский соус",
            category="food_calories",
        )
    )

    assert result["ok"] is True
    assert result["source_mode"] == "external"
    assert result["sources"][0]["domain"] == "fdc.nal.usda.gov"
    assert provider.calls, "external provider should be used when local data is absent"


def test_search_unavailable_returns_safe_fallback_without_raw_details(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = FakeProvider(error=RuntimeError("401 unauthorized api key invalid"))
    monkeypatch.setattr(hps, "get_active_search_provider", lambda: provider)

    result = _payload(
        hps.healbite_public_search_tool(
            "редкий азиатский соус",
            category="food_calories",
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "auth"
    assert "401" not in result["user_facing_text"]
    assert "api key" not in result["user_facing_text"].lower()
    assert "unauthorized" not in result["user_facing_text"].lower()


def test_external_results_include_uncertainty_and_citations(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = FakeProvider(
        results=[
            {
                "title": "USDA result",
                "url": "https://fdc.nal.usda.gov/fdc-app.html#/food-details/12345",
                "description": "Reference entry with energy information per serving.",
            }
        ]
    )
    monkeypatch.setattr(hps, "get_active_search_provider", lambda: provider)

    result = _payload(
        hps.healbite_public_search_tool(
            "редкий азиатский соус",
            category="food_calories",
        )
    )

    assert "примерная" in result["user_facing_text"].lower()
    assert result["sources"]
    assert result["uncertainty"]


def test_cache_prevents_repeated_provider_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = FakeProvider(
        results=[
            {
                "title": "USDA result",
                "url": "https://fdc.nal.usda.gov/fdc-app.html#/food-details/12345",
                "description": "Reference entry with energy information per serving.",
            }
        ]
    )
    monkeypatch.setattr(hps, "get_active_search_provider", lambda: provider)

    first = _payload(
        hps.healbite_public_search_tool("редкий азиатский соус", category="food_calories")
    )
    second = _payload(
        hps.healbite_public_search_tool("редкий азиатский соус", category="food_calories")
    )

    assert first["source_mode"] == "external"
    assert second["source_mode"] == "external"
    assert len(provider.calls) == 1


def test_telegram_toolset_includes_safe_nutrition_search_without_browser_or_terminal_approval_logic():
    tools = resolve_toolset("hermes-telegram")

    assert "healbite_public_search" in tools
    assert hps.HEALBITE_PUBLIC_SEARCH_SCHEMA["name"] == "healbite_public_search"
