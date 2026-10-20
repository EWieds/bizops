"""The Connect page: the one-time owner setup. Each account is tested before it's saved."""

from __future__ import annotations

import asyncio
import imaplib
import os
import re
import smtplib
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import write_env
from ..connectors.email import smtp_login
from ..engine import set_setting
from .auth import set_password

FIELDS = {
    "stripe": ["STRIPE_SECRET_KEY"],
    "email": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "IMAP_HOST", "IMAP_PORT", "EMAIL_FROM",
              "SENDER_NAME", "SENDER_ADDRESS"],
    "voice": ["VAPI_API_KEY"],
    "cards": ["PRIVACY_API_KEY", "PRIVACY_SANDBOX", "CARDHOLDER_NAME", "CARDHOLDER_ZIP"],
    "models": ["MODEL_BACKEND", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "BRAVE_API_KEY", "TYPESAFE_API_KEY",
               "YOUTUBE_API_KEY"],
    "site": ["PUBLIC_BASE_URL"],
}
SECRET = {"YOUTUBE_API_KEY", "PRIVACY_API_KEY", "TYPESAFE_API_KEY", "STRIPE_SECRET_KEY", "SMTP_PASSWORD", "VAPI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
          "BRAVE_API_KEY", "JEV_API_KEY"}


async def test_stripe(key: str) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.stripe.com/v1/balance", headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise RuntimeError(f"Stripe said {r.status_code}: {r.text[:200]}")
    mode = "TEST mode" if key.startswith(("sk_test", "rk_test")) else "LIVE mode"
    return f"Stripe connected ({mode})."


def clean_email_settings(env: dict) -> dict:
    """Pasted values often carry spaces, non-breaking spaces or a scheme; app passwords are shown in groups."""
    out = dict(env)
    for k in ("SMTP_HOST", "IMAP_HOST"):
        if out.get(k):
            out[k] = re.sub(r"^\w+://", "", out[k].replace("\xa0", " ").strip()).strip("/ ").split(":")[0]
    for k in ("SMTP_USER", "EMAIL_FROM", "SMTP_PORT", "IMAP_PORT"):
        if out.get(k):
            out[k] = out[k].replace("\xa0", " ").strip()
    if out.get("SMTP_PASSWORD"):
        out["SMTP_PASSWORD"] = re.sub(r"[\s\u00a0\u200b]+", "", out["SMTP_PASSWORD"])
    return out


def _smtp_login(host: str, port: int, user: str, password: str) -> None:
    step = "connect"
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            step = "encryption (STARTTLS)"
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls()
                s.ehlo()
        step = "login"
        smtp_login(s, user, password)
        s.quit()
    except Exception as e:
        raise RuntimeError(f"SMTP {host}:{port} failed at {step}: {type(e).__name__}: {e}") from e


def test_smtp(env: dict) -> str:
    port = int(env.get("SMTP_PORT") or 587)
    try:
        _smtp_login(env["SMTP_HOST"], port, env["SMTP_USER"], env["SMTP_PASSWORD"])
    except RuntimeError as first:
        if "failed at login" in str(first):
            raise
        alt = 465 if port != 465 else 587  # the port and the connection type often get mixed up
        try:
            _smtp_login(env["SMTP_HOST"], alt, env["SMTP_USER"], env["SMTP_PASSWORD"])
        except RuntimeError:
            raise first
        env["SMTP_PORT"] = str(alt)
    msg = "Email sending works."
    if env.get("IMAP_HOST"):
        try:
            with imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env.get("IMAP_PORT") or 993)) as imap:
                imap.login(env.get("IMAP_USER") or env["SMTP_USER"], env.get("IMAP_PASSWORD") or env["SMTP_PASSWORD"])
        except Exception as e:
            raise RuntimeError(f"Sending works, but reading the inbox failed (IMAP {env['IMAP_HOST']}): "
                               f"{type(e).__name__}: {e}") from e
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
        if group == "email":
            updates = clean_email_settings(updates)
        for k in list(updates):  # keys and tokens never contain spaces: a terminal line wrap adds them when copied
            if k in SECRET and k != "SMTP_PASSWORD":
                updates[k] = re.sub(r"[\s\u00a0\u200b]+", "", updates[k])
        merged = {k: updates.get(k, os.environ.get(k, "")) for k in keys}
        try:
            if group == "stripe" and merged.get("STRIPE_SECRET_KEY"):
                result = await test_stripe(merged["STRIPE_SECRET_KEY"])
            elif group == "email" and merged.get("SMTP_HOST"):
                if not (merged.get("SENDER_NAME") and merged.get("SENDER_ADDRESS")):
                    raise RuntimeError("Sender name and postal address are required by anti-spam law (CAN-SPAM).")
                result = await asyncio.to_thread(test_smtp, merged)
                if merged.get("SMTP_PORT") and merged["SMTP_PORT"] != updates.get("SMTP_PORT"):
                    updates["SMTP_PORT"] = merged["SMTP_PORT"]
            elif group == "voice" and merged.get("VAPI_API_KEY"):
                result = await test_vapi(merged["VAPI_API_KEY"])
            elif group == "models" and (updates.get("CLAUDE_CODE_OAUTH_TOKEN") or merged.get("MODEL_BACKEND") == "claude_token"):
                from ..llm.claude_cli import test_token
                if not merged.get("CLAUDE_CODE_OAUTH_TOKEN"):
                    raise RuntimeError("paste a Claude token first (from `claude setup-token`)")
                result = await test_token(merged["CLAUDE_CODE_OAUTH_TOKEN"])
            elif group == "cards" and merged.get("PRIVACY_API_KEY"):
                from ..cards import test_privacy
                result = await test_privacy(merged["PRIVACY_API_KEY"],
                                            (merged.get("PRIVACY_SANDBOX") or "").lower() in ("1", "true", "yes"))
            else:
                result = "Saved."
        except Exception as e:
            from ..events import log_event
            log_event(core.db, actor_kind="human", kind="human", action=f"connect.{group}.failed",
                      result={"error": f"{type(e).__name__}: {str(e)[:400]}",
                              "fields": {k: (f"<{len(v)} chars>" if k in SECRET else v) for k, v in merged.items()}},
                      outcome="error")
            return RedirectResponse(f"/connect?err={quote(type(e).__name__ + ': ' + str(e)[:300])}", status_code=303)
        write_env(env_path, updates)
        if group == "site" and merged.get("PUBLIC_BASE_URL"):
            set_setting(core.db, "public_base_url", merged["PUBLIC_BASE_URL"].rstrip("/"))
        core.reload_connectors()
        from ..owner_setup import resolve_connected
        await resolve_connected(core)
        return RedirectResponse(f"/connect?msg={result}", status_code=303)
