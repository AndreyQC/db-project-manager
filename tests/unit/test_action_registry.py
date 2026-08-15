"""Unit tests for the GUI action registry."""

import pytest
from pydantic import BaseModel

from db_project_manager.presentation.gui.actions.registry import (
    ACTIONS,
    ActionSpec,
    get_action,
)


def test_action_ids_unique():
    ids = [a.action_id for a in ACTIONS]
    assert len(ids) == len(set(ids))


def test_expected_actions_present():
    ids = {a.action_id for a in ACTIONS}
    assert ids == {
        "reverse_engineer", "deploy_validate", "deploy_analyze", "graph_prepare", "compare",
    }


def test_specs_are_complete():
    for spec in ACTIONS:
        assert isinstance(spec, ActionSpec)
        assert spec.title.strip(), spec.action_id
        assert issubclass(spec.settings_model, BaseModel)
        assert callable(spec.make_dialog)
        assert callable(spec.make_worker)
        assert callable(spec.build_cli)


def test_required_fields_exist_in_model():
    for spec in ACTIONS:
        model_fields = set(spec.settings_model.model_fields)
        for field in spec.required_fields:
            assert field in model_fields, f"{spec.action_id}: {field}"


def test_get_action_lookup():
    for spec in ACTIONS:
        assert get_action(spec.action_id) is spec


def test_get_action_unknown_raises():
    with pytest.raises(KeyError):
        get_action("no_such_action")
