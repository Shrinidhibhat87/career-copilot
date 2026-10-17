from __future__ import annotations

from pathlib import Path

import pytest

from career_copilot.adapters.model_catalog import load_catalog
from career_copilot.adapters.profiles import load_profile
from career_copilot.domain.model_settings import (
    ROLES,
    Catalog,
    CatalogEntry,
    LiveModel,
    gate_cloud_model,
    parse_catalog,
)

CLOUD = Path(__file__).resolve().parents[2] / "deploy" / "profiles" / "cloud-openrouter.yaml"


def live(model_id: str, caps: set[str], dynamic: bool = False) -> LiveModel:
    return LiveModel(model_id, model_id, frozenset(caps), None if dynamic else 0.3, None, dynamic)


def test_the_shipped_catalog_is_valid() -> None:
    catalog = load_catalog()
    assert catalog.problems() == []
    for role in ROLES:
        assert catalog.recommended(role).tier == "recommended"


def test_the_cloud_profile_is_the_catalog_recommended_set_with_every_role_explicit() -> None:
    roles = load_profile(CLOUD)["models"]["roles"]
    catalog = load_catalog()
    assert set(roles) == set(ROLES)
    for role in ROLES:
        assert roles[role]["backend"] == "openrouter"
        assert roles[role]["model"] == catalog.recommended(role).id


def test_a_role_lists_each_model_once_best_tier_first() -> None:
    entries = load_catalog().for_role("profiler")
    ids = [entry.id for entry in entries]
    assert len(ids) == len(set(ids))
    assert entries[0].tier == "recommended"


def test_catalog_problems_name_a_missing_baseline_and_a_failing_cap() -> None:
    catalog = Catalog(
        (CatalogEntry("a/x", ("assistant",), "recommended", ("response_format",), ""),)
    )
    problems = catalog.problems()
    assert any("assistant: no baseline" in p for p in problems)
    assert any("a/x: listed for assistant but lacks tools" in p for p in problems)


def test_a_bad_tier_is_refused() -> None:
    with pytest.raises(ValueError, match="bad tier"):
        parse_catalog({"models": [{"id": "a/x", "roles": ["triage"], "tier": "best"}]})


def test_a_model_without_tools_is_refused_for_the_assistant() -> None:
    problem, _ = gate_cloud_model(
        "assistant", "a/x", live("a/x", {"response_format"}), load_catalog(), None
    )
    assert problem == "a/x does not support tools, which assistant needs"


def test_a_dynamic_price_needs_a_reserve() -> None:
    catalog = Catalog(())
    model = live("a/x", {"response_format"}, True)
    problem, _ = gate_cloud_model("triage", "a/x", model, catalog, None)
    assert problem is not None and "reserve" in problem
    problem, reserve = gate_cloud_model(
        "triage", "a/x", live("a/x", {"response_format"}, True), catalog, 0.03
    )
    assert (problem, reserve) == (None, 0.03)


def test_jev_router_takes_its_reserve_from_the_catalog() -> None:
    model = live("typesafe/jev-router", {"tools", "response_format"}, True)
    problem, reserve = gate_cloud_model("triage", model.id, model, load_catalog(), None)
    assert problem is None and reserve == 0.05


def test_without_a_live_list_only_catalog_models_pass() -> None:
    catalog = load_catalog()
    assert gate_cloud_model("match", "google/gemini-2.5-pro", None, catalog, None)[0] is None
    problem, _ = gate_cloud_model("match", "some/unknown", None, catalog, None)
    assert problem == "some/unknown is not on OpenRouter's model list"
