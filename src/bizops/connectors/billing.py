"""Collecting money: offers (products + prices + payment links) and incoming payments.

Real mode: Stripe (STRIPE_SECRET_KEY, ideally a restricted key with Products, Prices, Payment Links
write and Checkout Sessions/Events/Invoices/Charges/Disputes read). Money is tracked by polling Stripe
events, so no public webhook is needed: first checkouts, subscription renewals (invoice.paid), refunds
(charge.refunded) and chargebacks (charge.dispute.*). Money lands in the owner's Stripe balance and pays
out to their bank.
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

    async def _req(self, method: str, path: str, data: dict | None = None, params: Any = None) -> dict:
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
        """Money events since `cursor`, oldest first, normalized (see parse_event)."""
        events: list[dict] = []
        for _ in range(20):  # at most 1000 events per poll; the rest come next poll
            params: list[tuple[str, Any]] = [("limit", 50)] + [("types[]", t) for t in EVENT_TYPES]
            if cursor:
                params.append(("ending_before", cursor))
            data = await self._req("GET", "/events", params=params)
            page = data.get("data", [])  # newest first
            if not page:
                break
            events = page + events
            cursor = page[0]["id"]
            if not cursor or not data.get("has_more"):
                break
        out = [p for ev in reversed(events) if (p := parse_event(ev))]
        return out, (events[0]["id"] if events else cursor)


EVENT_TYPES = ("checkout.session.completed", "invoice.paid", "charge.refunded", "charge.dispute.created",
               "charge.dispute.funds_withdrawn", "charge.dispute.funds_reinstated", "charge.dispute.closed")


def _id(v: Any) -> str | None:
    return v.get("id") if isinstance(v, dict) else v


def parse_event(ev: dict) -> dict[str, Any] | None:
    """One Stripe event -> {"kind": payment|refund|dispute, ...} or None if it moves no money for us."""
    t, o = ev.get("type", ""), ev["data"]["object"]
    if t == "checkout.session.completed":
        if o.get("payment_status") not in ("paid", "no_payment_required"):
            return None
        return {"kind": "payment", "provider_ref": o["id"], "amount_cents": int(o.get("amount_total") or 0),
                "currency": o.get("currency") or "usd",
                "payer_email": (o.get("customer_details") or {}).get("email"),
                "lead_id": o.get("client_reference_id"), "offer_id": (o.get("metadata") or {}).get("bizops_offer"),
                "payment_link": o.get("payment_link"), "payment_intent": _id(o.get("payment_intent")),
                "invoice": _id(o.get("invoice")), "subscription": _id(o.get("subscription"))}
    if t == "invoice.paid":
        if o.get("billing_reason") == "subscription_create":
            return None  # the first invoice of a subscription is the checkout session we already counted
        if not int(o.get("amount_paid") or 0):
            return None
        sub = _id(o.get("subscription")) or _id(((o.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
        meta = (o.get("subscription_details") or (o.get("parent") or {}).get("subscription_details") or {}).get("metadata") or {}
        lines = (o.get("lines") or {}).get("data") or []
        price_id = None
        if lines:
            line = lines[0]
            price_id = _id(line.get("price")) or _id(((line.get("pricing") or {}).get("price_details") or {}).get("price"))
        return {"kind": "payment", "provider_ref": o["id"], "amount_cents": int(o["amount_paid"]),
                "currency": o.get("currency") or "usd", "payer_email": o.get("customer_email"),
                "lead_id": None, "offer_id": meta.get("bizops_offer"), "payment_link": None, "price_id": price_id,
                "payment_intent": _id(o.get("payment_intent")), "invoice": o["id"], "charge": _id(o.get("charge")),
                "subscription": sub, "renewal": True}
    if t == "charge.refunded":
        return {"kind": "refund", "provider_ref": ev["id"], "charge": o["id"],
                "payment_intent": _id(o.get("payment_intent")), "invoice": _id(o.get("invoice")),
                "refunded_total_cents": int(o.get("amount_refunded") or 0), "currency": o.get("currency") or "usd"}
    if t.startswith("charge.dispute."):
        return {"kind": "dispute", "provider_ref": ev["id"], "event": t.removeprefix("charge.dispute."),
                "dispute_id": o["id"], "charge": _id(o.get("charge")), "payment_intent": _id(o.get("payment_intent")),
                "amount_cents": int(o.get("amount") or 0), "status": o.get("status") or ""}
    return None


def make_billing(settings: Settings) -> BillingProvider:
    return StripeBilling(settings) if settings.secret("STRIPE_SECRET_KEY") else SandboxBilling()


def sandbox_ref() -> str:
    return new_id("sbxpay")
