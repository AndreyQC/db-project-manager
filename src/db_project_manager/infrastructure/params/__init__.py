"""Параметры подстановки деплоя: dbpm.params.yaml + подключение + CLI.

Публичный API:
  - :func:`load_params_file` — секция ``parameters:`` из ``dbpm.params.yaml``;
  - :func:`effective_params` — файловые значения, слитые с переопределениями;
  - :class:`ParamSubstitutor` — замена ``{{param}}`` со сбором неразрешённых;
  - :class:`UnresolvedParamError` / :class:`ParamsFileError` — fail-fast.
"""

from db_project_manager.infrastructure.params.resolver import (
    PARAMS_FILENAME,
    ParamsFileError,
    UnresolvedParamError,
    effective_params,
    load_params_file,
)
from db_project_manager.infrastructure.params.substitution import (
    PLACEHOLDER_RE,
    ParamSubstitutor,
    substitute_text,
)

__all__ = [
    "PARAMS_FILENAME",
    "PLACEHOLDER_RE",
    "ParamSubstitutor",
    "ParamsFileError",
    "UnresolvedParamError",
    "effective_params",
    "load_params_file",
    "substitute_text",
]
