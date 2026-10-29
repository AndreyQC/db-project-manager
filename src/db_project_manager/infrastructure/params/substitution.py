"""Замена плейсхолдеров ``{{param_name}}`` в тексте SQL-файлов.

Подстановка выполняется над текстом файла ДО парсинга, поэтому граф, дельта
и исполняемые скрипты строятся уже на развёрнутых значениях. Autodoc-заголовок
файла (``[<[autodoc-yaml]] … [[autodoc-yaml]>]``) из подстановки исключён:
identity объекта там записана уже развёрнутыми именами (задача 20261027_001).

Неразрешённые плейсхолдеры не роняют обработку немедленно — они собираются
в :class:`ParamSubstitutor` и агрегированно поднимаются через
:meth:`ParamSubstitutor.raise_if_missing` (fail-fast со списком параметров
и файлов). Так один прогон показывает ВСЕ проблемы, а не первую.
"""

from __future__ import annotations

import re
from typing import Mapping

from db_project_manager.infrastructure.params.resolver import UnresolvedParamError
from db_project_manager.infrastructure.sql.autodoc import MARKER_CLOSE, MARKER_OPEN

#: Плейсхолдер ``{{name}}`` (jinja-стиль; пробелы внутри скобок допустимы).
#: Имя ограничено ``[A-Za-z_][A-Za-z0-9_]*`` — конструкции вида ``{{9x}}`` или
#: ``{{a.b}}`` плейсхолдерами не считаются и остаются в тексте как есть.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _split_autodoc(text: str) -> tuple[str, str, str]:
    """Разрезать текст на (до autodoc, autodoc-блок, после autodoc).

    Если маркеров нет — весь текст считается «телом» (голова и блок пусты).
    """
    open_idx = text.find(MARKER_OPEN)
    if open_idx == -1:
        return "", "", text
    close_idx = text.find(MARKER_CLOSE, open_idx)
    if close_idx == -1:
        return "", "", text
    close_end = close_idx + len(MARKER_CLOSE)
    return text[:open_idx], text[open_idx:close_end], text[close_end:]


class ParamSubstitutor:
    """Замена ``{{param}}`` со сбором неразрешённых плейсхолдеров.

    Один экземпляр на прогон (построение графа, деплой) — так fail-fast
    агрегирует пропуски по всем файлам, а не по первому встретившемуся.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = {str(k): str(v) for k, v in values.items()}
        # параметр → файлы (origin), где он не разрешён
        self._missing: dict[str, set[str]] = {}

    @property
    def values(self) -> dict[str, str]:
        return dict(self._values)

    def substitute(self, text: str, *, origin: str = "<text>") -> str:
        """Заменить известные плейсхолдеры; неизвестные — записать и оставить.

        Autodoc-блок (если есть) переносится в результат без изменений.
        Текст без ``{{`` возвращается как есть (fast path).
        """
        if "{{" not in text:
            return text
        head, autodoc, tail = _split_autodoc(text)

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in self._values:
                return self._values[name]
            self._missing.setdefault(name, set()).add(origin)
            return match.group(0)

        return PLACEHOLDER_RE.sub(_replace, head) + autodoc + PLACEHOLDER_RE.sub(_replace, tail)

    def raise_if_missing(self) -> None:
        """Fail-fast: поднять :class:`UnresolvedParamError`, если есть пропуски."""
        if self._missing:
            raise UnresolvedParamError(self._missing, list(self._values))


def substitute_text(text: str, values: Mapping[str, str], *, origin: str = "<text>") -> str:
    """Одноразовая замена без агрегации — для точек, идущих после fail-fast
    гейта парсера (все параметры уже гарантированно разрешены).
    """
    return ParamSubstitutor(values).substitute(text, origin=origin)
