"""Declarative registry of GUI actions (the dropdown content).

Adding a new action = appending one ActionSpec here (+ settings model, dialog,
worker, build_cli). MainWindow and ActionPanelWidget stay unchanged.

Factories are lazy: dialogs/workers are imported inside the factory functions
so this module can be imported in unit tests without instantiating Qt widgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel
from PySide6.QtWidgets import QDialog, QWidget
from PySide6.QtCore import QRunnable

from db_project_manager.infrastructure.config.connection_store import ConnectionStore

from db_project_manager.presentation.gui.actions.cli import (
    build_cli_compare,
    build_cli_deploy_validate,
    build_cli_graph_prepare,
    build_cli_reverse_engineer,
)
from db_project_manager.presentation.gui.actions.models import (
    CompareSettings,
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
)


@dataclass(frozen=True)
class ActionSpec:
    """One executable GUI action."""

    action_id: str
    title: str
    settings_model: type[BaseModel]
    # (store, settings, parent) -> configured modal dialog ("Настроить…")
    make_dialog: Callable[[ConnectionStore, BaseModel, QWidget | None], QDialog]
    # (store, settings) -> runnable for "Выполнить"
    make_worker: Callable[[ConnectionStore, BaseModel], QRunnable]
    # (settings, store) -> CLI command string for "Копировать CLI"
    build_cli: Callable[[BaseModel, ConnectionStore], str]
    # Settings fields that must be non-empty before "Выполнить"
    required_fields: tuple[str, ...]


def _make_reverse_engineer_dialog(store, settings, parent):
    from db_project_manager.presentation.gui.actions.dialogs import ReverseEngineerDialog

    return ReverseEngineerDialog(store, settings, parent)


def _make_deploy_validate_dialog(store, settings, parent):
    from db_project_manager.presentation.gui.actions.dialogs import DeployValidateDialog

    return DeployValidateDialog(store, settings, parent)


def _make_graph_prepare_dialog(store, settings, parent):
    from db_project_manager.presentation.gui.actions.dialogs import GraphPrepareDialog

    return GraphPrepareDialog(store, settings, parent)


def _make_compare_dialog(store, settings, parent):
    from db_project_manager.presentation.gui.actions.dialogs import CompareDialog

    return CompareDialog(store, settings, parent)


def _make_reverse_engineer_worker(store, settings: ReverseEngineerSettings):
    from db_project_manager.presentation.gui.widgets.workers import ReverseEngineerWorker

    return ReverseEngineerWorker(store.load_by_name(settings.connection), settings.output_dir)


def _make_deploy_validate_worker(store, settings: DeployValidateSettings):
    from db_project_manager.presentation.gui.widgets.workers import DeployValidateWorker

    return DeployValidateWorker(
        store.load_by_name(settings.connection),
        settings.codebase_dir,
        prefix=settings.prefix or None,
        keep_db=settings.keep_db,
        continue_on_error=settings.continue_on_error,
    )


def _make_graph_prepare_worker(store, settings: GraphPrepareSettings):
    del store  # graph actions do not use a connection
    from db_project_manager.presentation.gui.widgets.workers import GraphBuildWorker

    return GraphBuildWorker(
        settings.codebase_dir,
        fmt=settings.format,
        validate=settings.validate_graph,
        output_dir=settings.output_dir or None,
    )


def _side_spec(label: str, connection: str, dir_: str, store: ConnectionStore):
    """Build a compare SideSpec from XOR fields.

    Exactly one of ``connection`` / ``dir_`` must be set; raises ValueError
    otherwise (both set → ambiguous, neither → not configured). The ValueError
    propagates from ``make_worker`` and is surfaced by MainWindow.
    """
    from db_project_manager.application.compare_service import SideSpec
    from db_project_manager.domain.diff import SnapshotSourceKind

    if connection and dir_:
        raise ValueError(
            f"На сторону {label} указаны и подключение, и каталог — выберите одно."
        )
    if connection:
        return SideSpec(
            SnapshotSourceKind.DB,
            str(store.path_for(connection)),
            conn_cfg=store.load_by_name(connection),
        )
    if dir_:
        return SideSpec(SnapshotSourceKind.DIR, dir_)
    raise ValueError(f"Сторона {label} не настроена: укажите подключение или каталог.")


def _make_compare_worker(store, settings: CompareSettings):
    from db_project_manager.presentation.gui.widgets.workers import CompareWorker

    return CompareWorker(
        _side_spec("source", settings.source_connection, settings.source_dir, store),
        _side_spec("target", settings.target_connection, settings.target_dir, store),
        settings.output_dir,
        keep_model_dir=settings.keep_model_dir,
    )


ACTIONS: list[ActionSpec] = [
    ActionSpec(
        action_id="reverse_engineer",
        title="Создать проект базы по подключению PG",
        settings_model=ReverseEngineerSettings,
        make_dialog=_make_reverse_engineer_dialog,
        make_worker=_make_reverse_engineer_worker,
        build_cli=build_cli_reverse_engineer,
        required_fields=("connection", "output_dir"),
    ),
    ActionSpec(
        action_id="deploy_validate",
        title="Выполнить тестовый деплой из проекта базы",
        settings_model=DeployValidateSettings,
        make_dialog=_make_deploy_validate_dialog,
        make_worker=_make_deploy_validate_worker,
        build_cli=build_cli_deploy_validate,
        required_fields=("codebase_dir", "connection"),
    ),
    ActionSpec(
        action_id="graph_prepare",
        title="Подготовить граф для просмотра в Gephi",
        settings_model=GraphPrepareSettings,
        make_dialog=_make_graph_prepare_dialog,
        make_worker=_make_graph_prepare_worker,
        build_cli=build_cli_graph_prepare,
        required_fields=("codebase_dir",),
    ),
    ActionSpec(
        action_id="compare",
        title="Сравнить состояния (БД или каталог reverse-engineer)",
        settings_model=CompareSettings,
        make_dialog=_make_compare_dialog,
        make_worker=_make_compare_worker,
        build_cli=build_cli_compare,
        required_fields=("output_dir",),
    ),
]


def get_action(action_id: str) -> ActionSpec:
    """Look up an action spec by id."""
    for spec in ACTIONS:
        if spec.action_id == action_id:
            return spec
    raise KeyError(f"Неизвестное действие: {action_id!r}")
