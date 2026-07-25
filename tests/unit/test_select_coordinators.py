"""Pure unit tests for __init__._select_coordinators - no hass fixture required."""

from custom_components.sensus_analytics import _select_coordinators


def test_none_id_returns_all_coordinators():
    coordinators = {"entry_a": "coord_a", "entry_b": "coord_b"}
    result = _select_coordinators(coordinators, None)
    assert sorted(result) == ["coord_a", "coord_b"]


def test_matching_id_returns_single_item_list():
    coordinators = {"entry_a": "coord_a", "entry_b": "coord_b"}
    assert _select_coordinators(coordinators, "entry_b") == ["coord_b"]


def test_unknown_id_returns_empty_list():
    coordinators = {"entry_a": "coord_a"}
    assert _select_coordinators(coordinators, "does_not_exist") == []


def test_empty_coordinators_with_none_id_returns_empty_list():
    assert _select_coordinators({}, None) == []


def test_empty_coordinators_with_id_returns_empty_list():
    assert _select_coordinators({}, "entry_a") == []
