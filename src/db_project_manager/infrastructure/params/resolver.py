"""Разрешение значений параметров подстановки ``{{param}}`` при деплое.

Источники значений — по возрастанию приоритета:

1. ``dbpm.params.yaml`` в корне кодовой базы (секция ``parameters:``) —
   базовые значения, с которыми работают ``graph build`` и сравнения.
2. Секция ``params:`` файла подключения (``connections/*.yaml``) —
   значения конкретной целевой БД.
3. ``--param name=value`` CLI — разовые переопределения.

Значение может быть крипто-токеном ``crypto__<ENV_VAR>__<ciphertext>``
(Fernet, тот же механизм, что у паролей подключений) — расшифровывается
последним шагом разрешения. Открытые значения секретов никогда не попадают
в логи и сообщения об ошибках: они сообщают только имена параметров и файлы.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

import yaml

from db_project_manager.infrastructure.crypto.crypto_util import (
    get_cipher_env,
    get_decrypted_text,
)

#: Имя файла параметров в корне кодовой базы (зафиксировано пользователем,
#: задача 20261027_001).
PARAMS_FILENAME = "dbpm.params.yaml"

#: Ключ секции со значениями внутри файла параметров.
PARAMETERS_KEY = "parameters"

#: Допустимое имя параметра — совпадает с плейсхолдером ``{{name}}``.
PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ParamsFileError(ValueError):
    """``dbpm.params.yaml`` не читается или имеет неверную структуру."""


class UnresolvedParamError(Exception):
    """Fail-fast: плейсхолдеры, не разрешённые ни в одном источнике значений.

    ``missing`` — имя параметра → список файлов, где он встретился;
    ``known`` — параметры, которые были доступны при подстановке.
    В сообщении значения не раскрываются (могут быть секретами).
    """

    def __init__(self, missing: Mapping[str, list[str] | set[str]], known: list[str]) -> None:
        self.missing = {name: sorted(set(origins)) for name, origins in missing.items()}
        self.known = list(known)
        lines = [
            "Неразрешённые параметры подстановки (fail-fast) — "
            "задайте их в dbpm.params.yaml, в params: подключения или через --param:",
        ]
        for name in sorted(self.missing):
            files = ", ".join(self.missing[name])
            lines.append(f"  - {{{{{name}}}}}  (файлы: {files})")
        if self.known:
            lines.append(f"Доступные параметры: {', '.join(sorted(self.known))}")
        else:
            lines.append("Доступные параметры: нет (dbpm.params.yaml отсутствует или пуст)")
        super().__init__("\n".join(lines))


def _decrypt_value(value: str) -> str:
    """Расшифровать крипто-токен; обычное значение вернуть как есть."""
    if get_cipher_env(value) is None:
        return value
    # Строгая расшифровка: CryptoKeyMissing/ValueError пробрасываются —
    # молчаливый токен в SQL опаснее падения деплоя.
    return get_decrypted_text(value)


def load_params_file(root: str | Path) -> dict[str, str]:
    """Прочитать ``dbpm.params.yaml`` кодовой базы.

    Отсутствующий файл — пустой словарь (нулевое влияние на существующие
    кодовые базы без параметров). Скаляры приводятся к ``str``; крипто-токены
    расшифровываются.
    """
    path = Path(root) / PARAMS_FILENAME
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ParamsFileError(f"{path}: ожидается YAML-отображение верхнего уровня")
    params = data.get(PARAMETERS_KEY)
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ParamsFileError(
            f"{path}: секция '{PARAMETERS_KEY}' должна быть отображением имя → значение"
        )
    values: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not PARAM_NAME_RE.fullmatch(key):
            raise ParamsFileError(
                f"{path}: недопустимое имя параметра {key!r} "
                f"(ожидается {PARAM_NAME_RE.pattern})"
            )
        values[key] = "" if value is None else str(value)
    return {key: _decrypt_value(value) for key, value in values.items()}


def effective_params(
    root: str | Path,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Итоговые значения: файловые базовые + переопределения (побеждают они).

    ``overrides`` — уже слитые переопределения подключения и CLI
    (крипто-токены в них тоже расшифровываются — операция идемпотентна
    для обычных строк).
    """
    merged: dict[str, str] = dict(load_params_file(root))
    for key, value in (overrides or {}).items():
        merged[key] = _decrypt_value(str(value))
    return merged
