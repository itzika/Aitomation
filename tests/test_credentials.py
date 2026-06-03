"""The per-system credentials feature: the field contract, the secret stores (encrypted-file
+ keyring), profiles, the scaffold login.py + Write authoring, the CLI, and the TUI glue."""

from __future__ import annotations

from unittest.mock import MagicMock

import keyring.errors
import pytest
from textual.widgets import Input, Static
from typer.testing import CliRunner

from aitomation.cli import app as cli_app
from aitomation.credentials import (
    DEFAULT_PROFILE,
    PROFILES,
    CredentialError,
    EncryptedFileStore,
    KeyringStore,
    clear_profile,
    credential_status,
    get_store,
    load_credentials,
    needs_credentials,
    profile_fields,
    required_credentials,
    set_credential,
)
from aitomation.models import CoverageInventory, InputField, Journey
from aitomation.models import TestableElement as Element
from aitomation.scaffold import scaffold_project
from aitomation.tui import AitomationApp
from aitomation.tui.app import ConfirmScreen, CredentialsScreen
from aitomation.workspace import Workspace

runner = CliRunner()


# --------------------------------------------------------------------------------------
# Inventories
# --------------------------------------------------------------------------------------


def _inv(name="Sys", *, auth=None, source="openapi", schemes=None, elements=None):
    return CoverageInventory(
        system_name=name,
        base_url="https://sys.test",
        source=source,
        auth_strategy=auth,
        auth_schemes=schemes or [],
        elements=elements
        or [
            Element(
                kind="endpoint",
                name="get",
                location="/x",
                method="GET",
                description="d",
                priority="high",
            )
        ],
    )


def _session_inv(name="Shop"):
    return CoverageInventory(
        system_name=name,
        base_url="https://shop.test/",
        source="crawl",
        auth_strategy="session",
        elements=[
            Element(
                kind="form",
                name="login",
                location="/login",
                description="Form on /login (login)",
                preconditions=["requires authenticated session"],
                priority="high",
                inputs=[
                    InputField(
                        name="user-name",
                        type="text",
                        where="form",
                        locator='get_by_placeholder("Username")',
                    ),
                    InputField(
                        name="password",
                        type="password",
                        where="form",
                        locator='get_by_placeholder("Password")',
                    ),
                ],
            ),
            Element(
                kind="page",
                name="inventory",
                location="/inventory",
                description="list",
                priority="high",
            ),
        ],
        suggested_journeys=[
            Journey(name="Sign in and browse", description="d", priority="high", elements=["login"])
        ],
    )


# --------------------------------------------------------------------------------------
# The field contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth,expected",
    [
        ("bearer", ["AUTH_TOKEN"]),
        ("oauth2", ["AUTH_TOKEN"]),  # bring a bearer — no OAuth flow
        ("apikey", ["AUTH_TOKEN"]),
        ("basic", ["AUTH_USER", "AUTH_PASS"]),
        ("session", ["AUTH_USER", "AUTH_PASS"]),
        (None, []),
        ("none", []),
    ],
)
def test_required_credentials_per_auth_kind(auth, expected):
    assert [f.env for f in required_credentials(_inv(auth=auth))] == expected


def test_database_source_requires_connection_url():
    inv = _inv(source="db_schema", auth=None)
    assert [f.env for f in required_credentials(inv)] == ["DATABASE_URL"]
    assert needs_credentials(inv)


def test_profile_fields_prepend_base_url():
    fields = profile_fields(_inv(auth="bearer"))
    assert [f.env for f in fields] == ["BASE_URL", "AUTH_TOKEN"]
    assert fields[0].secret is False and fields[1].secret is True


def test_needs_credentials_false_without_auth():
    assert not needs_credentials(_inv(auth=None))


# --------------------------------------------------------------------------------------
# Encrypted-file store (the headless/CI backend; isolated by conftest)
# --------------------------------------------------------------------------------------


def test_encrypted_store_roundtrip_and_profile_isolation():
    inv = _session_inv("EncRoundtrip")
    slug = "enc-roundtrip"
    set_credential(slug, "dev", "AUTH_USER", "alice")
    set_credential(slug, "dev", "AUTH_PASS", "s3cret")
    set_credential(slug, "prod", "AUTH_PASS", "prodpass")
    assert credential_status(slug, "dev", inv) == {
        "BASE_URL": False,
        "AUTH_USER": True,
        "AUTH_PASS": True,
    }
    assert credential_status(slug, "prod", inv)["AUTH_USER"] is False
    assert load_credentials(slug, "dev", inv) == {"AUTH_USER": "alice", "AUTH_PASS": "s3cret"}
    assert load_credentials(slug, "prod", inv) == {"AUTH_PASS": "prodpass"}


def test_encrypted_store_clear_profile():
    inv = _session_inv("EncClear")
    slug = "enc-clear"
    set_credential(slug, "dev", "AUTH_USER", "x")
    set_credential(slug, "dev", "AUTH_PASS", "y")
    assert clear_profile(slug, "dev", inv) == 2
    assert load_credentials(slug, "dev", inv) == {}


def test_encrypted_store_does_not_persist_plaintext(tmp_path):
    store = EncryptedFileStore(tmp_path / "s.enc")
    store.set("k", "TOP-SECRET-VALUE")
    assert store.get("k") == "TOP-SECRET-VALUE"
    assert "TOP-SECRET-VALUE" not in (tmp_path / "s.enc").read_text()


def test_encrypted_store_wrong_passphrase_raises(tmp_path, monkeypatch):
    path = tmp_path / "s.enc"
    monkeypatch.setenv("AITOMATION_VAULT_PASSPHRASE", "right")
    EncryptedFileStore(path).set("k", "v")
    monkeypatch.setenv("AITOMATION_VAULT_PASSPHRASE", "wrong")
    with pytest.raises(CredentialError):
        EncryptedFileStore(path).get("k")


def test_encrypted_store_delete_absent_is_noop(tmp_path):
    EncryptedFileStore(tmp_path / "s.enc").delete("never-set")  # no error


# --------------------------------------------------------------------------------------
# Keyring store + backend selection
# --------------------------------------------------------------------------------------


class _FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise keyring.errors.PasswordDeleteError("absent")
        del self.store[(service, user)]


def test_keyring_store_roundtrip_and_delete_noop():
    store = KeyringStore(_FakeKeyring(), "Test Keychain")
    store.set("a:dev:AUTH_TOKEN", "tok")
    assert store.get("a:dev:AUTH_TOKEN") == "tok"
    store.delete("a:dev:AUTH_TOKEN")
    assert store.get("a:dev:AUTH_TOKEN") is None
    store.delete("a:dev:AUTH_TOKEN")  # already gone — suppressed, no error


def test_get_store_uses_keyring_when_available(monkeypatch):
    monkeypatch.delenv("AITOMATION_SECRETS_BACKEND", raising=False)
    monkeypatch.setattr(
        "aitomation.credentials._keyring_usable", lambda: (_FakeKeyring(), "Fake Keychain")
    )
    store = get_store()
    assert isinstance(store, KeyringStore) and store.label == "Fake Keychain"


def test_get_store_falls_back_to_file_when_no_keyring(monkeypatch):
    monkeypatch.delenv("AITOMATION_SECRETS_BACKEND", raising=False)
    monkeypatch.setattr("aitomation.credentials._keyring_usable", lambda: None)
    assert isinstance(get_store(), EncryptedFileStore)


def test_get_store_forced_keyring_unavailable_raises(monkeypatch):
    monkeypatch.setenv("AITOMATION_SECRETS_BACKEND", "keyring")
    monkeypatch.setattr("aitomation.credentials._keyring_usable", lambda: None)
    with pytest.raises(CredentialError):
        get_store()


def test_get_store_respects_file_override(monkeypatch):
    monkeypatch.setenv("AITOMATION_SECRETS_BACKEND", "file")
    assert isinstance(get_store(), EncryptedFileStore)


# --------------------------------------------------------------------------------------
# Workspace profiles
# --------------------------------------------------------------------------------------


def test_workspace_profile_defaults_and_persists(tmp_path):
    ws = Workspace(tmp_path)
    rec = ws.save(_session_inv("ProfA"), origin="x")
    assert rec.profile == DEFAULT_PROFILE == "dev"
    ws.set_profile(rec.slug, "prod")
    assert ws.list_systems()[0].profile == "prod"
    # A re-discover (save again) preserves the active profile, like the pipeline flags.
    ws.save(_session_inv("ProfA"))
    assert ws.list_systems()[0].profile == "prod"


def test_profiles_constant_is_three():
    assert PROFILES == ("dev", "stage", "prod")


# --------------------------------------------------------------------------------------
# Scaffold: login.py for session auth
# --------------------------------------------------------------------------------------


def test_session_scaffold_emits_login_py_from_form(tmp_path):
    scaffold_project(_session_inv("LoginScaffold"), tmp_path)
    login = (tmp_path / "login.py").read_text()
    assert "def perform_login(page, base_url):" in login
    assert 'os.environ.get("AUTH_USER"' in login and 'os.environ.get("AUTH_PASS"' in login
    assert 'get_by_placeholder("Username")' in login  # the discovered locator
    assert "from login import perform_login" in (tmp_path / "conftest.py").read_text()


def test_non_session_scaffold_has_no_login_py(tmp_path):
    scaffold_project(_inv(auth="bearer"), tmp_path)
    assert not (tmp_path / "login.py").exists()


def test_rescaffold_keeps_authored_login(tmp_path):
    scaffold_project(_session_inv("ReScaffold"), tmp_path)
    authored = "# AI-AUTHORED login flow. Generated by Aitomation Write.\nimport os\n"
    (tmp_path / "login.py").write_text(authored)
    scaffold_project(_session_inv("ReScaffold"), tmp_path)  # must not clobber the authored one
    assert (tmp_path / "login.py").read_text() == authored


# --------------------------------------------------------------------------------------
# Write: draft_login authors login.py
# --------------------------------------------------------------------------------------


class _StructProvider:
    """Returns `code` shaped to whatever schema is asked for (TestDraft or LoginDraft)."""

    def __init__(self, code):
        self.code = code
        self.labels: list[str] = []

    async def generate(self, *a, **k):  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema, *, system=None, label=""):
        self.labels.append(label)
        kw = {"code": self.code}
        if "review_notes" in schema.model_fields:
            kw["review_notes"] = "verify selectors"
        if "confidence" in schema.model_fields:
            kw["confidence"] = "medium"
        return schema(**kw)


_GOOD_LOGIN = (
    "import os\n\n\n"
    "def perform_login(page, base_url):\n"
    '    user = os.environ.get("AUTH_USER", "")\n'
    '    password = os.environ.get("AUTH_PASS", "")\n'
    '    page.goto(base_url.rstrip("/") + "/login")\n'
    '    page.get_by_label("Username").fill(user)\n'
    '    page.get_by_label("Password").fill(password)\n'
    '    page.get_by_role("button", name="Login").click()\n'
    '    page.wait_for_load_state("networkidle")\n'
)


async def test_draft_login_authors_from_form(tmp_path):
    from aitomation.write import draft_login

    scaffold_project(_session_inv("Authored"), tmp_path)
    provider = _StructProvider(_GOOD_LOGIN)
    result = await draft_login(_session_inv("Authored"), provider, into=tmp_path)
    assert result is not None and result.authored
    text = (tmp_path / "login.py").read_text()
    assert "Generated by Aitomation Write" in text and 'get_by_label("Username")' in text
    assert provider.labels == ["write:login"]


async def test_draft_login_skips_when_no_login_py(tmp_path):
    from aitomation.write import draft_login

    scaffold_project(_inv(auth="bearer"), tmp_path)  # API scaffold — no login.py
    assert (
        await draft_login(_inv(auth="bearer"), _StructProvider(_GOOD_LOGIN), into=tmp_path) is None
    )


async def test_draft_login_keeps_stub_on_bad_generation(tmp_path):
    from aitomation.write import draft_login

    scaffold_project(_session_inv("BadGen"), tmp_path)
    bad = "def something_else():\n    pass\n"  # no perform_login / no env reads
    result = await draft_login(_session_inv("BadGen"), _StructProvider(bad), into=tmp_path)
    assert result is not None and not result.authored
    assert "Best-effort stub" in (tmp_path / "login.py").read_text()  # stub preserved


# --------------------------------------------------------------------------------------
# CLI: aitomation creds set / list / clear
# --------------------------------------------------------------------------------------


def _seed_cli(tmp_path, name="CLI Shop"):
    """Seed a system into projects/ relative to cwd (where the CLI's PROJECTS_ROOT points)."""
    return Workspace("projects").save(_session_inv(name), origin="https://shop.test")


def test_creds_cli_set_list_clear(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _seed_cli(tmp_path)
    slug = rec.slug

    out = runner.invoke(cli_app, ["creds", "set", slug, "AUTH_PASS", "--value", "pw"])
    assert out.exit_code == 0, out.output
    assert "stored" in out.output

    listed = runner.invoke(cli_app, ["creds", "list", slug])
    assert listed.exit_code == 0
    assert "AUTH_PASS" in listed.output and "profile: dev" in listed.output

    cleared = runner.invoke(cli_app, ["creds", "clear", slug, "--all"])
    assert cleared.exit_code == 0
    inv = Workspace("projects").load_inventory(slug)
    assert load_credentials(slug, "dev", inv) == {}


def test_creds_cli_rejects_unknown_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _seed_cli(tmp_path, name="CLI Reject")
    out = runner.invoke(cli_app, ["creds", "set", rec.slug, "NOPE", "--value", "x"])
    assert out.exit_code == 1
    assert "isn't a field" in out.output


def test_creds_cli_set_honours_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _seed_cli(tmp_path, name="CLI Profile")
    runner.invoke(
        cli_app, ["creds", "set", rec.slug, "AUTH_PASS", "--value", "p", "--profile", "prod"]
    )
    inv = Workspace("projects").load_inventory(rec.slug)
    assert load_credentials(rec.slug, "prod", inv) == {"AUTH_PASS": "p"}
    assert load_credentials(rec.slug, "dev", inv) == {}


# --------------------------------------------------------------------------------------
# TUI: the Overview callout, the credentials modal, and the re-discover confirmation
# --------------------------------------------------------------------------------------


class _FakeLLM:
    async def generate(self, *a, **k):  # pragma: no cover
        return ""

    async def generate_structured(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _tui(tmp_path):
    return AitomationApp(llm=_FakeLLM(), usage_log=tmp_path / "u.jsonl", workspace_root=tmp_path)


async def test_overview_shows_credentials_callout(tmp_path):
    Workspace(tmp_path).save(_session_inv("TuiOverview"), origin="x")
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = app.query_one("#overview", Static).render().plain
        assert "creds" in text and "[dev]" in text and "Username" in text


async def test_credentials_modal_saves_to_store(tmp_path):
    rec = Workspace(tmp_path).save(_session_inv("TuiModal"), origin="x")
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_credentials()
        await pilot.pause()
        assert isinstance(app.screen, CredentialsScreen)
        app.screen.query_one("#cred-AUTH_PASS", Input).value = "hunter2"
        app.screen.query_one("#cred-BASE_URL", Input).value = "https://dev.tui"
        app.screen._save()
        await pilot.pause()
    inv = Workspace(tmp_path).load_inventory(rec.slug)
    status = credential_status(rec.slug, "dev", inv)
    assert status["AUTH_PASS"] and status["BASE_URL"]
    assert load_credentials(rec.slug, "dev", inv)["BASE_URL"] == "https://dev.tui"


async def test_credentials_action_hidden_without_auth(tmp_path):
    Workspace(tmp_path).save(_inv("NoAuthTui", auth=None), origin="x")
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.check_action("credentials", ()) is False


async def test_rediscover_requires_confirmation(tmp_path):
    Workspace(tmp_path).save(_session_inv("TuiRedisc"), origin="https://x")
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_discover = MagicMock()
        app.action_rediscover()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)  # confirm first, don't run immediately
        app._on_rediscover_confirm(False)
        app.run_discover.assert_not_called()  # cancel → no re-discovery
        app._on_rediscover_confirm(True)
        app.run_discover.assert_called_once()
