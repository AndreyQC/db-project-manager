"""Pytest configuration and shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

# Name of the environment variable used by the crypto__ token format in tests.
TEST_CRYPTO_ENV = "DBPM_TEST_CRYPTO_KEY"


@pytest.fixture
def crypto_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Provide a Fernet key in TEST_CRYPTO_ENV and yield the env variable name.

    The crypto_util reads the key lazily from os.environ[<env_var>] where
    <env_var> is embedded in the token itself (crypto__<env_var>__<token>).
    """
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(TEST_CRYPTO_ENV, key)
    yield TEST_CRYPTO_ENV
    monkeypatch.delenv(TEST_CRYPTO_ENV, raising=False)
