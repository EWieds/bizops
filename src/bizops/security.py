"""The signing secret and keys derived from it.

BIZOPS_SECRET is required: there is no fallback, so a missing or weak secret can never yield forgeable
cookies or links. Each use gets its own derived key (sessions, shared-file links, ...), so one leaked
signature can't be replayed somewhere else. Unsubscribe links keep the master key so links already sent
stay valid.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

MIN_LEN = 32
WEAK = {"dev", "bizops-local-secret", "changeme", "change-me", "secret", "test"}


class MissingSecret(RuntimeError):
    pass


def master_secret() -> bytes:
    s = os.environ.get("BIZOPS_SECRET", "")
    if not s or len(s) < MIN_LEN or s.lower() in WEAK:
        raise MissingSecret(
            "BIZOPS_SECRET is missing or too short (need at least 32 random characters). "
            "Run `bizops init-secret` to write one to .env, or set it in your deploy secrets "
            "(e.g. `fly secrets set BIZOPS_SECRET=$(openssl rand -hex 32)`).")
    return s.encode()


def check_secret() -> None:
    master_secret()


def new_secret() -> str:
    return secrets.token_hex(32)


def derived_key(purpose: str) -> bytes:
    return hmac.new(master_secret(), f"bizops:{purpose}".encode(), hashlib.sha256).digest()


def sign(purpose: str, msg: str, length: int = 32) -> str:
    return hmac.new(derived_key(purpose), msg.encode(), hashlib.sha256).hexdigest()[:length]


def verify(purpose: str, msg: str, sig: str, length: int = 32) -> bool:
    return hmac.compare_digest(sign(purpose, msg, length), sig or "")
