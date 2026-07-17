"""Расшифровка значений конфигурации в формате crypto__<ENV>__<fernet_token>."""

from __future__ import annotations

import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

SMGR_CIPHER_PREFIX = "crypto"
CIPHER_PREFIX = SMGR_CIPHER_PREFIX

logger = logging.getLogger(__name__)


def _is_cipher_token(s: str) -> bool:
    parts = s.strip().split("__")
    return len(parts) == 3 and parts[0] == CIPHER_PREFIX


def get_decrypted_text(encrypted_data: str) -> str:
    """
    Расшифровывает строку вида ``crypto__<ИМЯ_ПЕРЕМЕННОЙ>__<ciphertext>``.
    Ключ Fernet берётся из ``os.environ[ИМЯ_ПЕРЕМЕННОЙ]``.
    """
    if not _is_cipher_token(encrypted_data):
        logger.warning("Строка не в ожидаемом crypto-формате, возвращаю как есть.")
        return encrypted_data

    parts = encrypted_data.strip().split("__")
    key_env_variable = parts[1]
    token = parts[2]

    try:
        raw_key = os.environ[key_env_variable]
    except KeyError as exc:
        raise KeyError(
            f"Для расшифровки нужна переменная окружения {key_env_variable}"
        ) from exc

    key_material: bytes | str
    if isinstance(raw_key, str):
        key_material = raw_key.strip()
    else:
        key_material = raw_key

    cipher = Fernet(key_material)
    try:
        decrypted = cipher.decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        try:
            decrypted = cipher.decrypt(token)
        except Exception:
            raise ValueError("Ошибка расшифровки Fernet") from exc
    return decrypted.decode("utf-8")


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _resolve_fernet_key(env_var: str) -> bytes | str:
    raw_key = os.environ[env_var]
    if isinstance(raw_key, str):
        return raw_key.strip()
    return raw_key


def format_cipher_token(env_var: str, ciphertext: str) -> str:
    return f"{CIPHER_PREFIX}__{env_var}__{ciphertext}"


def get_encrypted_text(plaintext: str, env_var: str) -> str:
    key_material = _resolve_fernet_key(env_var)
    cipher = Fernet(key_material)
    ciphertext = cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return format_cipher_token(env_var, ciphertext)


def get_decrypted_nested_dict(data: Any) -> Any:
    """Рекурсивно обходит dict/list/str и расшифровывает строки в формате crypto__."""
    if isinstance(data, dict):
        return {k: get_decrypted_nested_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [get_decrypted_nested_dict(item) for item in data]
    if isinstance(data, str):
        stripped = data.strip()
        if _is_cipher_token(stripped):
            try:
                return get_decrypted_text(stripped)
            except Exception:
                logger.warning("Не удалось расшифровать поле, оставляю исходную строку.")
                return stripped
        return data
    return data
