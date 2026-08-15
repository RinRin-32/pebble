"""Encryption for per-user secrets pebble stores on their behalf.

Currently git push credentials; the shape is deliberately general because the
next one will want the same thing.

**Where the key comes from.** ``PEBBLE_SECRET_KEYS`` (a rotation list) or
``PEBBLE_SECRET_KEY`` in the environment first, matching how this deployment
already supplies ``PEBBLE_JWT_SECRET``; then ``[security]
secret_encryption_keys`` in config.toml; then, for operators who already
configured it, the MCP token keys, so nobody has to generate a second key for
the same instance.

**Why not derive one automatically.** A key generated on first use has to live
somewhere, and the obvious somewhere is the database — next to the ciphertext
it protects, which reduces the encryption to obfuscation against exactly the
attacker it exists to stop. So an unconfigured instance stores no user
secrets and says why, rather than pretending.

Rotation follows the MCP store's shape: MultiFernet writes with the first key
and reads with any, so a rotation is "prepend the new key, redeploy, re-save".
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from pebble.core.log import get_logger

log = get_logger(__name__)

KEY_HINT = (
    "Generate one with: python -c "
    '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
    "and set PEBBLE_SECRET_KEY in your .env"
)


class SecretCipherUnavailableError(RuntimeError):
    """No key material is configured, so a user secret cannot be stored.

    Deliberately loud: the alternative is accepting a token and silently
    keeping it in plaintext.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message or f"No secret encryption key configured. {KEY_HINT}")


def _raw_keys() -> list[str]:
    env_list = (os.environ.get("PEBBLE_SECRET_KEYS") or "").strip()
    if env_list:
        return [k.strip() for k in env_list.split(",") if k.strip()]
    env_one = (os.environ.get("PEBBLE_SECRET_KEY") or "").strip()
    if env_one:
        return [env_one]
    try:
        from pebble.core.config import load_config

        sec = load_config("security")
    except Exception:  # pragma: no cover - config is optional
        return []
    for name in ("secret_encryption_keys", "mcp_token_encryption_keys"):
        val = sec.get(name)
        if isinstance(val, list) and val:
            return [str(v) for v in val]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
    single = sec.get("mcp_token_encryption_key")
    if isinstance(single, str) and single.strip():
        return [single.strip()]
    return []


def cipher() -> MultiFernet | None:
    """The configured cipher, or None when no key material is available."""
    keys = _raw_keys()
    if not keys:
        return None
    fernets: list[Fernet] = []
    for idx, raw in enumerate(keys):
        try:
            fernets.append(Fernet(raw.encode() if isinstance(raw, str) else raw))
        except (ValueError, TypeError) as exc:
            raise SecretCipherUnavailableError(
                f"secret key #{idx} is not a valid Fernet key. {KEY_HINT}"
            ) from exc
    return MultiFernet(fernets)


def is_configured() -> bool:
    """Whether user secrets can be stored at all.

    Callers surface this in the UI so an operator learns the key is missing
    when they open the panel, not when a user's save fails.
    """
    try:
        return cipher() is not None
    except SecretCipherUnavailableError:
        return False


def encrypt(plaintext: str) -> bytes:
    c = cipher()
    if c is None:
        raise SecretCipherUnavailableError
    return c.encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt, or raise SecretCipherUnavailableError when the key cannot read it.

    A key that was rotated out without re-saving lands here.  It is reported
    as "unavailable" rather than "corrupt" because that is the actionable
    reading: the data is intact, the key that opens it is gone.
    """
    c = cipher()
    if c is None:
        raise SecretCipherUnavailableError
    try:
        return c.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise SecretCipherUnavailableError(
            "stored secret could not be decrypted with the configured key(s) — "
            "it was probably encrypted under a key that has since been removed"
        ) from exc
