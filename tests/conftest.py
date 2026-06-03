"""Shared pytest fixtures.

Isolate the per-system credential store so NO test ever touches the real OS keychain or the
user's ~/.config: force the encrypted-file backend into a throwaway dir with a fixed
passphrase. Autouse + session-scoped, so it's in effect before the first test runs (the TUI
reads the store while rendering the Overview)."""

from __future__ import annotations

import os

import pytest

_VARS = ("AITOMATION_SECRETS_BACKEND", "AITOMATION_SECRETS_FILE", "AITOMATION_VAULT_PASSPHRASE")


@pytest.fixture(autouse=True, scope="session")
def _isolated_secret_store(tmp_path_factory):
    d = tmp_path_factory.mktemp("aito-secrets")
    prev = {k: os.environ.get(k) for k in _VARS}
    os.environ["AITOMATION_SECRETS_BACKEND"] = "file"
    os.environ["AITOMATION_SECRETS_FILE"] = str(d / "secrets.enc")
    os.environ["AITOMATION_VAULT_PASSPHRASE"] = "test-passphrase"
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
