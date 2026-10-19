"""The Connect page: the one-time owner setup. Each account is tested before it's saved."""

from __future__ import annotations

import asyncio
import imaplib
import os
import smtplib

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import write_env
from ..engine import set_setting
from .auth import set_password

FIELDS = {
    "stripe": ["STRIPE_SECRET_KEY"],
    "email": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "IMAP_HOST", "IMAP_PORT", "EMAIL_FROM",
              "SENDER_NAME", "SENDER_ADDRESS"],
    "voice": ["VAPI_API_KEY"],
    "cards": ["PRIVACY_API_KEY", "PRIVACY_SANDBOX", "CARDHOLDER_NAME", "CARDHOLDER_ZIP"],
    "models": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "BRAVE_API_KEY", "TYPESAFE_API_KEY"],
    "site": ["PUBLIC_BASE_URL"],
}
SECRET = {"PRIVACY_API_KEY", "TYPESAFE_API_KEY", "STRIPE_SECRET_KEY", "SMTP_PASSWORD", "VAPI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
          "BRAVE_API_KEY", "JEV_API_KEY"}


async def test_stripe(key: str) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.stripe.com/v1/balance", headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise RuntimeError(f"Stripe said {r.status_code}: {r.text[:200]}")
    mode = "TEST mode" if key.startswith(("sk_test", "rk_test")) else "LIVE mode"
    return f"Stripe connected ({mode})."


def test_smtp(env: dict) -> str:
    port = int(env.get("SMTP_PORT") or 587)
    if port == 465:
        s = smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=20)
    else:
        s = smtplib.SMTP(env["SMTP_HOST"], port, timeout=20)
        s.ehlo()
        if s.has_extn("starttls"):
            s.starttls()
    s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
    s.quit()
    msg = "Email sending works."
    if env.get("IMAP_HOST"):
        with imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env.get("IMAP_PORT") or 993)) as imap:
            imap.login(env.get("IMAP_USER") or env["SMTP_USER"], env.get("IMAP_PASSWORD") or env["SMTP_PASSWORD"])
        msg += " Inbox reading works (replies will be picked up)."
    return msg


async def test_vapi(key: str) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.vapi.ai/assistant", params={"limit": 1}, headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise RuntimeError(f"Vapi said {r.status_code}: {r.text[:200]}")
    return "Vapi connected."


def register_connect(app: FastAPI, core, page) -> None:
    env_path = core.settings.home / ".env"

    def state() -> dict:
        return {k: ("•••• set" if os.environ.get(k) and k in SECRET else os.environ.get(k, "")) for g in FIELDS.values() for k in g}

    @app.get("/connect", response_class=HTMLResponse)
    async def connect(request: Request, msg: str = "", err: str = ""):
        from ..business import public_base
        return page(request, "connect.html", values=state(), status=core.connectors.status(), msg=msg, err=err,
                    cards=core.cards.name, browser_ok=core.browser.installed(),
                    public_url=public_base(core.db, core.settings), has_password=bool(os.environ.get("BIZOPS_PASSWORD_HASH")))

    @app.post("/connect/{group}")
    async def connect_save(request: Request, group: str):
        form = {k: str(v).strip() for k, v in (await request.form()).items()}
        if group == "password":
            if len(form.get("password", "")) < 10:
                return RedirectResponse("/connect?err=Use+at+least+10+characters", status_code=303)
            set_password(env_path, form["password"], db=core.db)  # logs out every session
            return RedirectResponse("/connect?msg=Password+saved", status_code=303)
        keys = FIELDS.get(group)
        if not keys:
            return RedirectResponse("/connect", status_code=303)
        updates = {k: form[k] for k in keys if form.get(k) and not form[k].startswith("••••")}
        merged = {k: updates.get(k, os.environ.get(k, "")) for k in keys}
        try:
            if group == "stripe" and merged.get("STRIPE_SECRET_KEY"):
                result = await test_stripe(merged["STRIPE_SECRET_KEY"])
            elif group == "email" and merged.get("SMTP_HOST"):
                if not (merged.get("SENDER_NAME") and merged.get("SENDER_ADDRESS")):
                    raise RuntimeError("Sender name and postal address are required by anti-spam law (CAN-SPAM).")
                result = await asyncio.to_thread(test_smtp, merged)
            elif group == "voice" and merged.get("VAPI_API_KEY"):
                result = await test_vapi(merged["VAPI_API_KEY"])
            elif group == "cards" and merged.get("PRIVACY_API_KEY"):
                from ..cards import test_privacy
                result = await test_privacy(merged["PRIVACY_API_KEY"],
                                            (merged.get("PRIVACY_SANDBOX") or "").lower() in ("1", "true", "yes"))
            else:
                result = "Saved."
        except Exception as e:
            return RedirectResponse(f"/connect?err={type(e).__name__}: {str(e)[:200]}", status_code=303)
        write_env(env_path, updates)
        if group == "site" and merged.get("PUBLIC_BASE_URL"):
            set_setting(core.db, "public_base_url", merged["PUBLIC_BASE_URL"].rstrip("/"))
        core.reload_connectors()
        from ..owner_setup import resolve_connected
        await resolve_connected(core)
        return RedirectResponse(f"/connect?msg={result}", status_code=303)
