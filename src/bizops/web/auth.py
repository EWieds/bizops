"""Owner login for the dashboard. Public customer-facing routes never need it.

The password hash lives in .env (BIZOPS_PASSWORD_HASH). Without a password, the dashboard only answers
requests from this machine, so it is never accidentally open on the internet.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..config import write_env

COOKIE = "bizops_session"
TTL = 30 * 86400


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${dk}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, salt), stored)


def ensure_secret(env_path) -> str:
    s = os.environ.get("BIZOPS_SECRET")
    if not s:
        s = secrets.token_hex(24)
        write_env(env_path, {"BIZOPS_SECRET": s})
    return s


def set_password(env_path, pw: str) -> None:
    write_env(env_path, {"BIZOPS_PASSWORD_HASH": hash_password(pw)})


def _sign(exp: int) -> str:
    key = (os.environ.get("BIZOPS_SECRET") or "dev").encode()
    return f"{exp}.{hmac.new(key, str(exp).encode(), hashlib.sha256).hexdigest()}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp = token.split(".", 1)[0]
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(_sign(int(exp)), token)


def is_local(request: Request) -> bool:
    """A direct request from this machine (not relayed by a proxy or tunnel)."""
    if any(request.headers.get(h) for h in ("x-forwarded-for", "cf-connecting-ip", "fly-client-ip", "x-real-ip")):
        return False
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("testclient", "localhost")


LOGIN = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>bizops login</title>
<style>body{font-family:system-ui;background:#0f172a;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#fff;padding:28px;border-radius:14px;width:300px}input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cbd5e1;border-radius:8px;margin:10px 0}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#0f172a;color:#fff;font-size:15px}</style></head>
<body><form method="post" action="/login"><b>bizops</b><div style="color:#64748b;font-size:13px">owner login</div>
<input type="password" name="password" placeholder="password" autofocus>{err}<button>Log in</button></form></body></html>"""


def install_auth(app, public_prefixes: tuple[str, ...]) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith(public_prefixes):
            return await call_next(request)
        stored = os.environ.get("BIZOPS_PASSWORD_HASH")
        if stored:
            if valid_session(request.cookies.get(COOKIE)):
                return await call_next(request)
            return RedirectResponse("/login", status_code=303)
        if is_local(request):
            return await call_next(request)
        return PlainTextResponse("Dashboard locked: set an owner password first (bizops set-password).", status_code=403)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(LOGIN.replace("{err}", ""))

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        stored = os.environ.get("BIZOPS_PASSWORD_HASH") or ""
        if stored and check_password(str(form.get("password", "")), stored):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, _sign(int(time.time()) + TTL), max_age=TTL, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https" or bool(request.headers.get("x-forwarded-proto") == "https"))
            return resp
        await asyncio.sleep(0.5)
        return HTMLResponse(LOGIN.replace("{err}", '<div style="color:#b91c1c;font-size:13px">Wrong password</div>'),
                            status_code=401)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp
