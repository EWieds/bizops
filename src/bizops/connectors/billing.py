"""Collecting money: offers (products + prices + payment links) and incoming payments.

Real mode: Stripe (STRIPE_SECRET_KEY, ideally a restricted key with Products, Prices, Payment Links
write and Checkout Sessions/Events read). Payments are found by polling Stripe events, so no public
webhook is needed. Money lands in the owner's Stripe balance and pays out to their bank.
Sandbox mode: payment links point at the local public app's test checkout (/pay/<offer>).
"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from ..config import Settings
from ..util import new_id

STRIPE = "https://api.stripe.com/v1"


class BillingProvider(Protocol):
    name: str
    live: bool

    async def create_offer(self, *, offer_id: str, name: str, description: str, price_cents: int, currency: str,
                           interval: str, public_base: str, org_id: str = "", org_name: str = "") -> dict[str, Any]: ...

    def link_for(self, offer: dict, lead_id: str | None, public_base: str) -> str: ...

    async def poll_payments(self, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]: ...


class SandboxBilling:
    name = "sandbox"
    live = False

    async def create_offer(self, *, offer_id: str, name: str, description: str, price_cents: int, currency: str,
                           interval: str, public_base: str, org_id: str = "", org_name: str = "") -> dict[str, Any]:
        return {"provider_product_id": None, "provider_price_id": None, "provider_link_id": None,
                "payment_url": f"{public_base}/pay/{offer_id}"}

    def link_for(self, offer: dict, lead_id: str | None, public_base: str) -> str:
        base = f"{public_base}/pay/{offer['id']}"
        return f"{base}?{urlencode({'ref': lead_id})}" if lead_id else base

    async def poll_payments(self, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        return [], cursor  # sandbox payments are recorded directly by the test checkout


class StripeBilling:
    name = "stripe"
    live = True

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.key = settings.secret("STRIPE_SECRET_KEY") or ""
        self.http = http

    async def _req(self, method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        client = self.http or httpx.AsyncClient(timeout=30)
        try:
            r = await client.request(method, f"{STRIPE}{path}", data=data, params=params,
                                     headers={"Authorization": f"Bearer {self.key}"})
            if r.status_code >= 400:
                raise RuntimeError(f"Stripe {path}: {r.status_code} {r.text[:300]}")
            return r.json()
        finally:
            if self.http is None:
                await client.aclose()

    async def create_offer(self, *, offer_id: str, name: str, description: str, price_cents: int, currency: str,
                           interval: str, public_base: str, org_id: str = "", org_name: str = "") -> dict[str, Any]:
        # One Stripe account for the whole umbrella: every object is labeled with the business it belongs to,
        # so the Stripe dashboard (and exports) can be filtered by business.
        labels = {"metadata[bizops_offer]": offer_id, "metadata[bizops_org]": org_id,
                  "metadata[bizops_business]": org_name[:100]}
        product = await self._req("POST", "/products", {"name": name, "description": description[:500] or name, **labels})
        price_data = {"product": product["id"], "unit_amount": price_cents, "currency": currency, **labels}
        if interval == "month":
            price_data["recurring[interval]"] = "month"
        price = await self._req("POST", "/prices", price_data)
        link_data = {"line_items[0][price]": price["id"], "line_items[0][quantity]": 1, **labels}
        if interval == "month":
            for k, v in labels.items():
                link_data[f"subscription_data[metadata][{k[9:-1]}]"] = v
        else:
            for k, v in labels.items():
                link_data[f"payment_intent_data[metadata][{k[9:-1]}]"] = v
        link = await self._req("POST", "/payment_links", link_data)
        return {"provider_product_id": product["id"], "provider_price_id": price["id"],
                "provider_link_id": link["id"], "payment_url": link["url"]}

    def link_for(self, offer: dict, lead_id: str | None, public_base: str) -> str:
        # Stripe Payment Links pass client_reference_id through to the checkout session.
        return f"{offer['payment_url']}?{urlencode({'client_reference_id': lead_id})}" if lead_id else offer["payment_url"]

    async def poll_payments(self, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {"type": "checkout.session.completed", "limit": 50}
        if cursor:
            params["ending_before"] = cursor
        data = await self._req("GET", "/events", params=params)
        events = data.get("data", [])
        out = []
        for ev in events:
            s = ev["data"]["object"]
            if s.get("payment_status") not in ("paid", "no_payment_required"):
                continue
            out.append({"provider_ref": s["id"], "amount_cents": int(s.get("amount_total") or 0),
                        "currency": s.get("currency") or "usd",
                        "payer_email": (s.get("customer_details") or {}).get("email"),
                        "lead_id": s.get("client_reference_id"),
                        "offer_id": (s.get("metadata") or {}).get("bizops_offer"),
                        "payment_link": s.get("payment_link")})
        new_cursor = events[0]["id"] if events else cursor
        return out, new_cursor


def make_billing(settings: Settings) -> BillingProvider:
    return StripeBilling(settings) if settings.secret("STRIPE_SECRET_KEY") else SandboxBilling()


def sandbox_ref() -> str:
    return new_id("sbxpay")
