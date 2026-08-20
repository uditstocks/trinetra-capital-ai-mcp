"""Credential vault — envelope encryption for broker secrets.

Users bring their own broker API keys. Holding those means holding the ability to
trade someone's real account, so this module is written to a stricter standard
than the rest of the codebase.

How it works
------------
Envelope encryption. Every credential bundle gets its own random data key; the
bundle is sealed with that data key, and the data key is then sealed with a
master key held outside the database. A database dump therefore yields nothing —
the attacker has ciphertext and wrapped keys, and neither opens without the
master key.

Both layers are AES-256-GCM, which authenticates as well as encrypts: a tampered
record fails to open rather than decrypting to something attacker-chosen. Each
record's ciphertext embeds the id of the master key that wrapped it, so keys can
be rotated without a flag day — old records stay readable while new ones use the
new key, and `rewrap` moves them across.

Rules this module enforces
--------------------------
- Plaintext exists only inside a `SecretBundle`, which refuses to render itself
  in logs, reprs, f-strings, tracebacks, JSON, or pickles.
- Nothing here writes to a log at any level with a secret in scope.
- Decryption happens at the moment of a broker call and nowhere else.

Configuration:
    TRINETRA_MASTER_KEY     base64 32-byte key (single-key setup)
    TRINETRA_MASTER_KEYS    "v1:<base64>,v2:<base64>" for rotation
    TRINETRA_ACTIVE_KEY     which id new records are sealed with (default: last)

Generate a key with:  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
DEFAULT_KEY_ID = "v1"


class VaultError(RuntimeError):
    """Vault misconfiguration or a credential that will not open."""


# --------------------------------------------------------------------------- #
# the plaintext holder
# --------------------------------------------------------------------------- #
class SecretBundle:
    """Decrypted credentials that refuse to be displayed.

    Every path Python might use to render an object — repr, str, format, pickle —
    is overridden, so a stray f-string or an exception traceback cannot leak a
    key. `reveal()` is deliberately ugly to type and easy to grep for in review.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, str]) -> None:
        object.__setattr__(self, "_data", dict(data))

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._data.get(key, default)

    def require(self, key: str) -> str:
        value = self._data.get(key)
        if not value:
            raise VaultError(f"Stored credentials are missing {key!r}.")
        return value

    def keys(self) -> list[str]:
        """Field names only — useful for diagnostics, reveals no values."""
        return sorted(self._data)

    def reveal(self) -> dict[str, str]:
        """The plaintext. Call this only when handing credentials to a broker SDK."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"<SecretBundle fields={self.keys()} REDACTED>"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return self.__repr__()

    def __getstate__(self):
        raise TypeError("SecretBundle cannot be pickled or serialised.")

    def __reduce__(self):
        raise TypeError("SecretBundle cannot be pickled or serialised.")

    def __iter__(self):
        raise TypeError("Refusing to iterate a SecretBundle; use reveal() explicitly.")


# --------------------------------------------------------------------------- #
# master keys
# --------------------------------------------------------------------------- #
def _decode_key(raw: str, key_id: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise VaultError(f"Master key {key_id!r} is not valid base64.") from exc
    if len(key) != KEY_BYTES:
        raise VaultError(
            f"Master key {key_id!r} must be {KEY_BYTES} bytes, got {len(key)}."
        )
    return key


def master_keys() -> dict[str, bytes]:
    """All configured master keys by id. Never logged, never cached to disk."""
    multi = os.getenv("TRINETRA_MASTER_KEYS", "").strip()
    if multi:
        keys: dict[str, bytes] = {}
        for entry in multi.split(","):
            if not entry.strip():
                continue
            if ":" not in entry:
                raise VaultError(
                    "TRINETRA_MASTER_KEYS entries must look like 'v1:<base64>'."
                )
            key_id, raw = entry.split(":", 1)
            keys[key_id.strip()] = _decode_key(raw, key_id.strip())
        if not keys:
            raise VaultError("TRINETRA_MASTER_KEYS is set but empty.")
        return keys

    single = os.getenv("TRINETRA_MASTER_KEY", "").strip()
    if not single:
        raise VaultError(
            "No vault master key configured. Set TRINETRA_MASTER_KEY to a base64 "
            "32-byte value. Generate one with: python -c \"import os,base64; "
            "print(base64.b64encode(os.urandom(32)).decode())\""
        )
    return {DEFAULT_KEY_ID: _decode_key(single, DEFAULT_KEY_ID)}


def active_key_id() -> str:
    """The key id new records are sealed with."""
    keys = master_keys()
    chosen = os.getenv("TRINETRA_ACTIVE_KEY", "").strip()
    if chosen:
        if chosen not in keys:
            raise VaultError(f"TRINETRA_ACTIVE_KEY={chosen!r} is not a configured key.")
        return chosen
    # Natural order, so v10 follows v9 rather than sorting between v1 and v2.
    def rank(key_id: str) -> tuple[int, str]:
        digits = "".join(c for c in key_id if c.isdigit())
        return (int(digits) if digits else -1, key_id)

    return sorted(keys, key=rank)[-1]


def is_configured() -> bool:
    """True when a usable master key is present. Never raises."""
    try:
        master_keys()
        return True
    except VaultError:
        return False


# --------------------------------------------------------------------------- #
# seal / open
# --------------------------------------------------------------------------- #
def encrypt(data: dict[str, str]) -> tuple[bytes, str]:
    """Seal a credential bundle. Returns (ciphertext, key_id).

    Layout: data_nonce | wrapped_data_key(60) | payload_nonce | sealed_payload.
    The key id travels in the database column beside it, and is also bound into
    the payload as authenticated data so a record cannot be relabelled.
    """
    if not data:
        raise VaultError("Refusing to store an empty credential bundle.")

    key_id = active_key_id()
    master = master_keys()[key_id]

    data_key = os.urandom(KEY_BYTES)
    data_nonce = os.urandom(NONCE_BYTES)
    wrapped = AESGCM(master).encrypt(data_nonce, data_key, key_id.encode())

    payload_nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(data_key).encrypt(
        payload_nonce, json.dumps(data).encode("utf-8"), key_id.encode()
    )
    return data_nonce + wrapped + payload_nonce + sealed, key_id


def decrypt(ciphertext: bytes, key_id: str) -> SecretBundle:
    """Open a sealed bundle. Raises VaultError if the key is wrong or the record
    was tampered with — never returns partial or attacker-chosen plaintext."""
    keys = master_keys()
    master = keys.get(key_id)
    if master is None:
        raise VaultError(
            f"Credential was sealed with master key {key_id!r}, which is not "
            "configured. Restore that key or have the user re-link their broker."
        )

    try:
        wrapped_len = KEY_BYTES + 16  # AES-GCM appends a 16-byte tag
        data_nonce = ciphertext[:NONCE_BYTES]
        wrapped = ciphertext[NONCE_BYTES:NONCE_BYTES + wrapped_len]
        rest = ciphertext[NONCE_BYTES + wrapped_len:]
        payload_nonce, sealed = rest[:NONCE_BYTES], rest[NONCE_BYTES:]

        data_key = AESGCM(master).decrypt(data_nonce, wrapped, key_id.encode())
        plaintext = AESGCM(data_key).decrypt(payload_nonce, sealed, key_id.encode())
    except InvalidTag as exc:
        raise VaultError(
            "Stored credentials failed their integrity check — wrong master key, "
            "or the record was modified."
        ) from exc
    except (IndexError, ValueError) as exc:
        raise VaultError("Stored credentials are malformed.") from exc

    try:
        return SecretBundle(json.loads(plaintext.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VaultError("Stored credentials are not readable.") from exc


def rewrap(ciphertext: bytes, key_id: str) -> tuple[bytes, str]:
    """Re-seal a record under the currently active master key.

    Used by rotation: read with the old key, write with the new one, without the
    plaintext ever leaving this function.
    """
    return encrypt(decrypt(ciphertext, key_id).reveal())


def redact(value: Any) -> str:
    """A safe rendering of anything that might be a secret, for diagnostics."""
    if isinstance(value, SecretBundle):
        return repr(value)
    text = str(value)
    return f"<redacted {len(text)} chars>" if text else "<empty>"
