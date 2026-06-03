"""Per-system credentials for the system under test (NOT the LLM provider key).

A discovered system often sits behind auth — a bearer token, an API key, HTTP basic, a
login form, or a database URL. To actually *run* the generated tests against it you need to
supply those secrets. This module is the secure collect → store → inject layer over the
exact env-var contract the scaffold already reads (`AUTH_TOKEN` / `AUTH_USER` + `AUTH_PASS` /
`DATABASE_URL`, plus an optional per-profile `BASE_URL`).

Design constraints (see CLAUDE.md):
- Secrets NEVER enter an LLM prompt and are NEVER part of the CoverageInventory or the
  Workspace index — they live only here, keyed by (system slug, profile, env var).
- Secrets are NEVER written to the run directory or a committed file. They reach pytest via
  the child process env only.
- Stored at rest in the OS keychain when one is usable (the enterprise-trustworthy answer);
  on headless Linux/CI with no keychain we fall back to an encrypted local file so the
  feature still works. The env var is the portable substrate either way — a value set in the
  process env always wins, which is how CI (its own secret store → env) stays first-class.

`required_credentials(inv)` is the single source of truth for *which* fields a system needs;
it's derived from the same `_auth_context` the scaffold uses, so the prompt for credentials
can never drift from the fixtures that consume them.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .models import CoverageInventory
from .scaffold.generator import _auth_context


class CredentialError(RuntimeError):
    """Raised when the secret store can't be read/written (e.g. an undecryptable vault)."""


# The three deployment targets a system can be tested against. A profile scopes BOTH the
# credential set and the BASE_URL, so dev/stage/prod can point at different hosts with
# different secrets without re-discovering.
PROFILES: tuple[str, ...] = ("dev", "stage", "prod")
DEFAULT_PROFILE = "dev"

_KEYRING_SERVICE = "aitomation"


@dataclass(frozen=True, slots=True)
class CredentialField:
    """One value a system needs to be tested. `env` is the variable the scaffold's fixtures
    read; `secret` decides whether the UI masks it and is the signal that it's sensitive."""

    env: str
    label: str
    secret: bool
    kind: str  # token | username | password | url — drives the input hint, not behaviour
    help: str = ""


# A non-secret, always-available per-profile override of the test target. Prefilled from the
# inventory's base_url; lets dev/stage/prod aim at different hosts. Kept apart from
# required_credentials so the "this system needs auth" signal stays driven by real auth.
BASE_URL_FIELD = CredentialField(
    env="BASE_URL",
    label="Base URL",
    secret=False,
    kind="url",
    help="Override the target host for this profile (blank = the discovered URL).",
)


def required_credentials(inv: CoverageInventory) -> list[CredentialField]:
    """The auth credential fields this system requires, or [] if no auth was discovered.

    Reuses the scaffold's `_auth_context` so what we ask for is exactly what the generated
    `conftest.py` fixtures consume. oauth2/openidconnect collapse to a bearer token (you bring
    one — the toolkit does not run an OAuth flow). A database-sourced system needs its
    connection URL (which embeds the password)."""
    fields: list[CredentialField] = []
    kind = _auth_context(inv)["auth_kind"]
    if kind == "bearer":
        fields.append(
            CredentialField(
                "AUTH_TOKEN", "Bearer token", True, "token", "Sent as Authorization: Bearer …"
            )
        )
    elif kind == "apikey":
        fields.append(
            CredentialField(
                "AUTH_TOKEN",
                "API key",
                True,
                "token",
                "Sent in the discovered API-key header/param.",
            )
        )
    elif kind in ("basic", "session"):
        verb = "HTTP Basic" if kind == "basic" else "login form"
        fields.append(
            CredentialField("AUTH_USER", "Username", False, "username", f"For the {verb}.")
        )
        fields.append(
            CredentialField("AUTH_PASS", "Password", True, "password", f"For the {verb}.")
        )
    # A database system's URL carries credentials; offer it as a (secret) field regardless of
    # the auth scheme so DB contract tests can actually connect.
    if inv.source == "db_schema":
        fields.append(
            CredentialField(
                "DATABASE_URL", "Database URL", True, "url", "e.g. postgresql://user:pass@host/db"
            )
        )
    return fields


def profile_fields(inv: CoverageInventory) -> list[CredentialField]:
    """Every field a profile can hold: the BASE_URL override plus the required auth fields.
    This is the set the modal/CLI edit and that `load_credentials` injects."""
    return [BASE_URL_FIELD, *required_credentials(inv)]


def needs_credentials(inv: CoverageInventory) -> bool:
    """True if the system has discovered auth worth collecting (drives the Overview callout)."""
    return bool(required_credentials(inv))


# --------------------------------------------------------------------------------------
# Secret stores
# --------------------------------------------------------------------------------------


def _cred_key(slug: str, profile: str, env: str) -> str:
    """The opaque per-secret key — also the keyring 'username'. Components are constrained
    (kebab slug, fixed profiles, upper-snake env), so ':' is an unambiguous separator."""
    return f"{slug}:{profile}:{env}"


class SecretStore:
    """A small get/set/delete secret store. Subclasses back it with the OS keychain or an
    encrypted local file. `label` names the backing store for the UI badge."""

    label = "store"

    def get(self, key: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class KeyringStore(SecretStore):
    """Backed by the OS keychain via the `keyring` library (macOS Keychain, Windows
    Credential Manager, Linux Secret Service / KWallet). Encrypted at rest by the OS and
    unlocked by the user's login session — no key management on our side."""

    def __init__(self, keyring_module, label: str) -> None:
        self._kr = keyring_module
        self.label = label

    def get(self, key: str) -> str | None:
        return self._kr.get_password(_KEYRING_SERVICE, key)

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(_KEYRING_SERVICE, key, value)

    def delete(self, key: str) -> None:
        import keyring.errors

        # Deleting an absent secret is a no-op (different keychains raise different errors here).
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            self._kr.delete_password(_KEYRING_SERVICE, key)


def _secrets_file() -> Path:
    if override := os.getenv("AITOMATION_SECRETS_FILE"):
        return Path(override)
    config = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "aitomation" / "secrets.enc"


class EncryptedFileStore(SecretStore):
    """Headless/CI fallback: an encrypted JSON file (Fernet/AES) at
    ``$XDG_CONFIG_HOME/aitomation/secrets.enc``. The encryption key comes from
    ``AITOMATION_VAULT_PASSPHRASE`` when set (strongest); otherwise a random key is generated
    once into a ``0600`` ``vault.key`` next to the file — zero-ceremony and headless-friendly,
    keeping secrets out of the repo/backups, with the honest caveat that a local key file only
    protects against casual exposure, not a local attacker. Set the passphrase for more."""

    label = "encrypted file"

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _secrets_file()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- crypto -------------------------------------------------------------------------

    def _fernet(self, salt: bytes):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        secret = self._secret_material()
        key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(secret)
        return Fernet(base64.urlsafe_b64encode(key))

    def _secret_material(self) -> bytes:
        """The bytes the file key is derived from: the configured passphrase, or a persisted
        random key generated on first use (created 0600, never in the repo)."""
        if passphrase := os.getenv("AITOMATION_VAULT_PASSPHRASE"):
            return passphrase.encode()
        keyfile = self._path.with_name("vault.key")
        if not keyfile.exists():
            keyfile.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
            with contextlib.suppress(OSError):
                keyfile.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        return keyfile.read_bytes()

    # -- file ---------------------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        from cryptography.fernet import InvalidToken

        raw = json.loads(self._path.read_text(encoding="utf-8"))
        salt = base64.b64decode(raw["salt"])
        try:
            data = self._fernet(salt).decrypt(base64.b64decode(raw["data"]))
        except (InvalidToken, KeyError, ValueError) as e:
            raise CredentialError(
                f"Cannot decrypt {self._path} — wrong AITOMATION_VAULT_PASSPHRASE or a lost "
                "vault.key. Fix the key, or delete the file to start over."
            ) from e
        return json.loads(data)

    def _save(self, store: dict[str, str]) -> None:
        salt = os.urandom(16)
        token = self._fernet(salt).encrypt(json.dumps(store).encode())
        payload = {
            "salt": base64.b64encode(salt).decode(),
            "data": base64.b64encode(token).decode(),
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        store = self._load()
        store[key] = value
        self._save(store)

    def delete(self, key: str) -> None:
        store = self._load()
        if store.pop(key, None) is not None:
            self._save(store)


# --------------------------------------------------------------------------------------
# Backend selection + the public API the TUI / CLI / run injection use
# --------------------------------------------------------------------------------------


def _keyring_usable() -> tuple[object, str] | None:
    """Return (keyring_module, label) if a real OS keychain is reachable, else None. Probes
    with a read of a sentinel so a present-but-broken backend (e.g. Secret Service with no
    D-Bus on a headless box) is treated as unavailable rather than blowing up at set-time."""
    try:
        import keyring
        import keyring.backends.fail
    except ImportError:
        return None
    kr = keyring.get_keyring()
    if isinstance(kr, keyring.backends.fail.Keyring):
        return None
    try:
        kr.get_password(_KEYRING_SERVICE, "__probe__")  # read-only; None if absent
    except Exception:
        return None
    return keyring, _keyring_label(kr)


def _keyring_label(kr: object) -> str:
    name = type(kr).__module__ + "." + type(kr).__name__
    if "macOS" in name or "Keychain" in name:
        return "macOS Keychain"
    if "Windows" in name or "WinVault" in name:
        return "Windows Credential Manager"
    if "SecretService" in name:
        return "Secret Service"
    if "kwallet" in name.lower():
        return "KWallet"
    return type(kr).__name__


def get_store() -> SecretStore:
    """Pick the secret backend: keyring when a real keychain is reachable, else the encrypted
    file. Override with ``AITOMATION_SECRETS_BACKEND=keyring|file``."""
    choice = (os.getenv("AITOMATION_SECRETS_BACKEND") or "").strip().lower()
    if choice == "file":
        return EncryptedFileStore()
    if choice == "keyring":
        usable = _keyring_usable()
        if usable is None:
            raise CredentialError(
                "AITOMATION_SECRETS_BACKEND=keyring but no OS keychain is reachable."
            )
        return KeyringStore(*usable)  # type: ignore[arg-type]
    usable = _keyring_usable()
    return KeyringStore(*usable) if usable is not None else EncryptedFileStore()  # type: ignore[arg-type]


def set_credential(
    slug: str, profile: str, env: str, value: str, *, store: SecretStore | None = None
) -> None:
    (store or get_store()).set(_cred_key(slug, profile, env), value)


def clear_credential(
    slug: str, profile: str, env: str, *, store: SecretStore | None = None
) -> None:
    (store or get_store()).delete(_cred_key(slug, profile, env))


def clear_profile(
    slug: str, profile: str, inv: CoverageInventory, *, store: SecretStore | None = None
) -> int:
    """Delete every stored value for one profile of a system. Returns how many were removed."""
    store = store or get_store()
    removed = 0
    for f in profile_fields(inv):
        key = _cred_key(slug, profile, f.env)
        if store.get(key) is not None:
            store.delete(key)
            removed += 1
    return removed


def credential_status(
    slug: str, profile: str, inv: CoverageInventory, *, store: SecretStore | None = None
) -> dict[str, bool]:
    """Map each field's env var → whether a value is stored for this (system, profile). Used
    by the Overview/modal to show what's set without ever revealing the values."""
    store = store or get_store()
    return {
        f.env: store.get(_cred_key(slug, profile, f.env)) is not None for f in profile_fields(inv)
    }


def load_credentials(
    slug: str, profile: str, inv: CoverageInventory, *, store: SecretStore | None = None
) -> dict[str, str]:
    """The stored {ENV: value} for a (system, profile), for merging into the pytest subprocess
    env at run time. Only set fields are returned; an unset BASE_URL falls back to the
    scaffold's baked-in default. Never logged — callers must not echo the values."""
    store = store or get_store()
    out: dict[str, str] = {}
    for f in profile_fields(inv):
        val = store.get(_cred_key(slug, profile, f.env))
        if val:
            out[f.env] = val
    return out
