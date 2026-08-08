from __future__ import annotations

from pathlib import Path

import pytest

from scripts import healbite_schema_migrate as schema
from scripts import hermes_production_staged_migrate as production


COMPONENTS = [
    item["component"] for item in production._target_migration_registry()
]


def _states(**overrides: str) -> dict[str, str]:
    result = {
        component: schema.SchemaClassification.CURRENT.value
        for component in COMPONENTS
    }
    result.update(overrides)
    return result


def test_full_registry_is_preserved_while_effective_scope_is_narrow() -> None:
    states = _states(fridge_menu=schema.SchemaClassification.ABSENT.value)

    assert COMPONENTS == [
        "household",
        "weekly",
        "shopping",
        "inventory",
        "fridge_menu",
    ]
    assert production._derive_effective_mutation_components(states, COMPONENTS) == [
        "fridge_menu"
    ]


def test_expected_scope_rejects_unknown_duplicate_and_noncanonical_order() -> None:
    for values in (
        ["unknown"],
        ["fridge_menu", "fridge_menu"],
        ["fridge_menu", "inventory"],
    ):
        with pytest.raises(
            production.ProductionGateError,
            match="EXPECTED_MUTATION_COMPONENTS_INVALID",
        ):
            production._validate_expected_mutation_components(values, COMPONENTS)


def test_unexpected_second_component_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    states = _states(
        inventory=schema.SchemaClassification.ABSENT.value,
        fridge_menu=schema.SchemaClassification.ABSENT.value,
    )
    monkeypatch.setattr(
        production,
        "_read_component_schema_states",
        lambda _path: states,
    )

    with pytest.raises(
        production.ProductionGateError,
        match="EFFECTIVE_MUTATION_COMPONENTS_MISMATCH",
    ):
        production._assert_effective_mutation_contract(
            tmp_path / "source.sqlite",
            ["fridge_menu"],
        )


def test_expected_fridge_only_scope_passes_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    states = _states(fridge_menu=schema.SchemaClassification.ABSENT.value)
    monkeypatch.setattr(
        production,
        "_read_component_schema_states",
        lambda _path: states,
    )

    actual_states, effective = production._assert_effective_mutation_contract(
        tmp_path / "source.sqlite",
        ["fridge_menu"],
    )

    assert actual_states == states
    assert effective == ["fridge_menu"]


def test_component_classification_is_bound_to_exact_source_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    states = _states(fridge_menu=schema.SchemaClassification.ABSENT.value)
    monkeypatch.setattr(
        production,
        "_read_component_schema_states",
        lambda _path: states,
    )
    monkeypatch.setattr(
        production,
        "_read_only_source",
        lambda _path: (
            {"SOURCE_SHA256": "b" * 64},
            "schema",
            "ok",
            0,
        ),
    )

    with pytest.raises(
        production.ProductionGateError,
        match="SOURCE_DRIFT_DURING_COMPONENT_CLASSIFICATION",
    ):
        production._assert_effective_mutation_contract(
            tmp_path / "source.sqlite",
            ["fridge_menu"],
            expected_source_sha256="a" * 64,
        )


def test_pre_ddl_recheck_precedes_production_authorization() -> None:
    source = Path(production.__file__).read_text(encoding="utf-8")
    execute = source.split("def _execute_plan_outcome(", 1)[1]

    assert execute.index("pre_ddl_states") < execute.index(
        "authorization = _issue_production_authorization"
    )
    assert execute.index("locked_pre_ddl_states") < execute.index(
        "return_code = _execute_authorized_staged"
    )
    assert production.PLAN_VERSION == 8
    assert production.OPERATIONS_ROOT_APPROVAL_VERSION == 3
    assert production.CLEAN_START_POLICY_VERSION == 3
