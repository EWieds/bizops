"""Voice agents: the product a voice-agent business sells (an AI receptionist for a small business).

Every agent gets a live web demo on the public app (/demo/<id>, text chat with the same brain), which
works with no provider at all. With VAPI_API_KEY set, the agent is also created on Vapi so it can
answer real phone calls (attaching a phone number is done in the owner's Vapi account: telephony
needs identity verification). Inbound calls only: outbound AI calls to consumers are a hard stop.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..config import Settings

VAPI = "https://api.vapi.ai"


def receptionist_prompt(cfg: dict[str, Any]) -> str:
    faqs = "\n".join(f"- Q: {f.get('q')}\n  A: {f.get('a')}" for f in cfg.get("faqs", []) if isinstance(f, dict))
    return (f"You are the friendly phone receptionist for {cfg.get('business_name')}. "
            f"{cfg.get('instructions', '')}\n\nBusiness hours: {cfg.get('hours', 'not specified')}.\n"
            f"Services: {cfg.get('services', 'not specified')}.\n"
            f"Booking: {cfg.get('booking', 'take the caller name, number and what they need, and say someone will confirm')}.\n"
            f"FAQs:\n{faqs or '- (none)'}\n\n"
            "Rules: be brief and warm (this is a phone call). Never invent prices, availability or policies that are "
            "not listed; offer to take a message instead. If the caller asks, say you are an AI assistant. "
            "Never collect card numbers or other sensitive data.")


class VoiceProvider(Protocol):
    name: str
    live: bool

    async def upsert(self, *, agent_id: str, provider_ref: str | None, cfg: dict[str, Any]) -> dict[str, Any]: ...


class LocalVoice:
    """Web demo only (no telephony). Always available."""

    name = "local"
    live = False

    async def upsert(self, *, agent_id: str, provider_ref: str | None, cfg: dict[str, Any]) -> dict[str, Any]:
        return {"provider_ref": None, "phone_number": None}


class VapiVoice:
    name = "vapi"
    live = True

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.key = settings.secret("VAPI_API_KEY") or ""
        self.model = settings.models.get("voice_model", "claude-sonnet-5-5")
        self.http = http

    async def upsert(self, *, agent_id: str, provider_ref: str | None, cfg: dict[str, Any]) -> dict[str, Any]:
        body = {
            "name": f"{cfg.get('business_name', 'Business')[:30]} receptionist",
            "firstMessage": cfg.get("greeting") or f"Thanks for calling {cfg.get('business_name')}, how can I help?",
            "model": {"provider": "anthropic", "model": self.model,
                      "messages": [{"role": "system", "content": receptionist_prompt(cfg)}]},
            "metadata": {"bizops_agent": agent_id},
        }
        client = self.http or httpx.AsyncClient(timeout=30)
        try:
            headers = {"Authorization": f"Bearer {self.key}"}
            if provider_ref:
                r = await client.patch(f"{VAPI}/assistant/{provider_ref}", json=body, headers=headers)
            else:
                r = await client.post(f"{VAPI}/assistant", json=body, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"Vapi: {r.status_code} {r.text[:300]}")
            data = r.json()
        finally:
            if self.http is None:
                await client.aclose()
        return {"provider_ref": data.get("id"), "phone_number": None}


def make_voice(settings: Settings) -> VoiceProvider:
    return VapiVoice(settings) if settings.secret("VAPI_API_KEY") else LocalVoice()
