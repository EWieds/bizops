"""Virtual cards for CIO-approved purchases. One card per purchase, locked to the vendor it is first
used at, with a spend limit equal to the approved amount. The issuer enforces that limit, so even a
bug here can't spend more than the CIO approved (limits are checked twice: in purchases.py and at the
issuer).

Providers:
  privacy  Privacy.com API (PRIVACY_API_KEY; PRIVACY_SANDBOX=1 uses their sandbox). One-time purchases
           get a single-use card, monthly ones a merchant-locked card with a monthly limit.
  sandbox  Records cards and returns a test card number. No money moves.

Card numbers are never stored or shown to agents: the browser asks the provider for them at the moment
it types them into a checkout form (see browser.py).
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .config import Settings
from .util import new_id


class CardProvider(Protocol):
    name: str
    live: bool

    async def create(self, *, memo: str, limit_cents: int, period: str) -> dict[str, Any]: ...

    async def details(self, token: str) -> dict[str, Any]: ...

    async def set_state(self, token: str, state: str) -> None: ...


class SandboxCards:
    """Test cards. The number is Visa's standard test number; nothing can be charged to it."""

    name = "sandbox"
    live = False

    async def create(self, *, memo: str, limit_cents: int, period: str) -> dict[str, Any]:
        return {"token": new_id("sbxcard"), "last4": "1111"}

    async def details(self, token: str) -> dict[str, Any]:
        return {"number": "4111111111111111", "cvv": "123", "exp_month": "12", "exp_year": "2030"}

    async def set_state(self, token: str, state: str) -> None:
        return None


class PrivacyCards:
    name = "privacy"
    live = True

    def __init__(self, api_key: str, sandbox: bool = False):
        self.base = "https://sandbox.privacy.com/v1" if sandbox else "https://api.privacy.com/v1"
        self.live = not sandbox
        self.headers = {"Authorization": f"api-key {api_key}"}

    async def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, self.base + path, headers=self.headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Privacy.com said {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def create(self, *, memo: str, limit_cents: int, period: str) -> dict[str, Any]:
        body = {"memo": memo[:100], "spend_limit": int(limit_cents), "state": "OPEN"}
        if period == "monthly":
            body |= {"type": "MERCHANT_LOCKED", "spend_limit_duration": "MONTHLY"}
        else:
            body |= {"type": "SINGLE_USE", "spend_limit_duration": "TRANSACTION"}
        card = await self._req("POST", "/cards", json=body)
        return {"token": card["token"], "last4": card.get("last_four", "")}

    async def details(self, token: str) -> dict[str, Any]:
        data = (await self._req("GET", "/cards", params={"card_token": token})).get("data") or []
        if not data:
            raise RuntimeError("card not found at Privacy.com")
        c = data[0]
        if c.get("state") != "OPEN":
            raise RuntimeError(f"card is {c.get('state')}")
        return {"number": c["pan"], "cvv": c["cvv"], "exp_month": str(c["exp_month"]).zfill(2),
                "exp_year": str(c["exp_year"])}

    async def set_state(self, token: str, state: str) -> None:
        await self._req("PUT", "/cards", json={"card_token": token, "state": state})


async def test_privacy(key: str, sandbox: bool = False) -> str:
    p = PrivacyCards(key, sandbox)
    await p._req("GET", "/cards", params={"page_size": 1})
    return f"Privacy.com connected ({'sandbox' if sandbox else 'LIVE'})."


def make_cards(settings: Settings) -> CardProvider:
    key = settings.secret("PRIVACY_API_KEY")
    if key:
        return PrivacyCards(key, sandbox=(settings.secret("PRIVACY_SANDBOX") or "").lower() in ("1", "true", "yes"))
    return SandboxCards()
