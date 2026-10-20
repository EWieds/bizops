"""Owner login for the dashboard. Public customer-facing routes never need it.

- The password hash lives in .env (BIZOPS_PASSWORD_HASH). Without a password, the dashboard only answers
  requests from this machine, so it is never accidentally open on the internet.
- Sessions are server-side (table `sessions`): the cookie holds a random token, the database only its
  sha256. Logging out, changing the password or "log out everywhere" revokes them at once.
- Lockout: 5 failed logins from one address within 15 minutes lock that address for 15 minutes (and a
  global ceiling stops a spread-out guessing attack).
- CSRF: every state-changing request to the dashboard must carry the session's token, as the
  X-CSRF-Token header (htmx, fetch) or a csrf_token form field (added to every form by base.html).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from datetime import timedelta
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ..config import write_env
from ..db import Database
from ..security import new_secret
from ..util import iso, new_id, now, parse_iso

COOKIE = "bizops_session"
CSRF_COOKIE = "bizops_csrf"     # double-submit token when there is no login (local-only dashboard)
TTL = 30 * 86400
LOCK_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)
GLOBAL_FAILURES = 50            # across all addresses per window, against distributed guessing
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


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


def init_secret(env_path) -> str:
    """Write a random BIZOPS_SECRET to .env if there is none (an explicit setup step, never implicit)."""
    s = os.environ.get("BIZOPS_SECRET")
    if not s:
        s = new_secret()
        write_env(env_path, {"BIZOPS_SECRET": s})
    return s


def set_password(env_path, pw: str, db: Database | None = None) -> None:
    write_env(env_path, {"BIZOPS_PASSWORD_HASH": hash_password(pw)})
    if db is not None:
        revoke_all(db)


# ---------------------------------------------------------------- client address

def client_ip(request: Request) -> str:
    """The visitor's address. Proxy headers are trusted only when we know we sit behind that proxy
    (Fly sets FLY_APP_NAME; `serve --tunnel` sets BIZOPS_BEHIND_CLOUDFLARE), so they can't be spoofed."""
    if os.environ.get("FLY_APP_NAME") and request.headers.get("fly-client-ip"):
        return request.headers["fly-client-ip"].strip()
    if os.environ.get("BIZOPS_BEHIND_CLOUDFLARE") and request.headers.get("cf-connecting-ip"):
        return request.headers["cf-connecting-ip"].strip()
    return request.client.host if request.client else "?"


def is_local(request: Request) -> bool:
    """A direct request from this machine (not relayed by a proxy or tunnel)."""
    if any(request.headers.get(h) for h in ("x-forwarded-for", "cf-connecting-ip", "fly-client-ip", "x-real-ip")):
        return False
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("testclient", "localhost")


# ---------------------------------------------------------------- sessions

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Database, *, ip: str, user_agent: str) -> tuple[str, str]:
    token, csrf, ts = secrets.token_urlsafe(32), secrets.token_urlsafe(24), iso()
    db.execute("INSERT INTO sessions (id, csrf, created_at, expires_at, last_seen_at, ip, user_agent) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (_hash(token), csrf, ts, iso(now() + timedelta(seconds=TTL)), ts, ip, user_agent[:200]))
    return token, csrf


def get_session(db: Database, token: str | None) -> dict | None:
    if not token:
        return None
    s = db.one("SELECT * FROM sessions WHERE id = ? AND revoked_at IS NULL AND expires_at > ?", (_hash(token), iso()))
    if s and s["last_seen_at"] < iso(now() - timedelta(minutes=5)):
        db.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (iso(), s["id"]))
    return s


def revoke(db: Database, session_id: str) -> None:
    db.execute("UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (iso(), session_id))


def revoke_all(db: Database, except_id: str | None = None) -> None:
    db.execute("UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL AND id != ?", (iso(), except_id or ""))


def active_sessions(db: Database) -> list[dict]:
    return db.all("SELECT id, created_at, last_seen_at, expires_at, ip, user_agent FROM sessions "
                  "WHERE revoked_at IS NULL AND expires_at > ? ORDER BY last_seen_at DESC", (iso(),))


# ---------------------------------------------------------------- lockout

def lockout_remaining(db: Database, ip: str) -> int:
    """Seconds until this address may try again (0 = not locked)."""
    since = iso(now() - LOCK_WINDOW)
    last_ok = db.scalar("SELECT MAX(ts) AS t FROM login_attempts WHERE ip = ? AND ok = 1", (ip,)) or ""
    fails = db.all("SELECT ts FROM login_attempts WHERE ip = ? AND ok = 0 AND ts >= ? AND ts > ? ORDER BY ts DESC",
                   (ip, since, last_ok))
    total = db.scalar("SELECT COUNT(*) AS n FROM login_attempts WHERE ok = 0 AND ts >= ?", (since,)) or 0
    if len(fails) < LOCK_FAILURES and total < GLOBAL_FAILURES:
        return 0
    newest = parse_iso(fails[0]["ts"] if fails else iso())
    return max(1, int((newest + LOCK_WINDOW - now()).total_seconds()))


def record_attempt(db: Database, ip: str, ok: bool) -> None:
    db.execute("INSERT INTO login_attempts (id, ts, ip, ok) VALUES (?, ?, ?, ?)", (new_id("la"), iso(), ip, int(ok)))
    db.execute("DELETE FROM login_attempts WHERE ts < ?", (iso(now() - timedelta(days=2)),))


# ---------------------------------------------------------------- CSRF

async def _submitted_csrf(request: Request) -> str:
    token = request.headers.get("x-csrf-token")
    if token:
        return token
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/x-www-form-urlencoded"):
        return (parse_qs((await request.body()).decode(errors="replace")).get("csrf_token") or [""])[0]
    if ctype.startswith("multipart/form-data"):
        body = await request.body()  # cached, so the route still receives the full upload

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}
        form = await Request(request.scope, replay).form()
        try:
            return str(form.get("csrf_token") or "")
        finally:
            await form.close()
    return ""


LOGIN = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>bizops login</title>
<style>body{font-family:system-ui;background:#0f172a;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#fff;padding:28px;border-radius:14px;width:300px}input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cbd5e1;border-radius:8px;margin:10px 0}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#0f172a;color:#fff;font-size:15px}</style></head>
<body><form method="post" action="/login"><b>bizops</b><div style="color:#64748b;font-size:13px">owner login</div>
<input type="password" name="password" placeholder="password" autofocus>{err}<button>Log in</button></form></body></html>"""


def _err(msg: str) -> str:
    return f'<div style="color:#b91c1c;font-size:13px">{msg}</div>'


def install_auth(app, public_prefixes: tuple[str, ...], db: Database) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith(public_prefixes):
            return await call_next(request)
        stored = os.environ.get("BIZOPS_PASSWORD_HASH")
        new_csrf_cookie = None
        if stored:
            session = get_session(db, request.cookies.get(COOKIE))
            if not session:
                if request.headers.get("hx-request"):
                    return PlainTextResponse("login required", status_code=401, headers={"HX-Redirect": "/login"})
                return RedirectResponse("/login", status_code=303)
            request.state.session_id, request.state.csrf = session["id"], session["csrf"]
        elif is_local(request):
            request.state.session_id = None
            request.state.csrf = request.cookies.get(CSRF_COOKIE) or ""
            if not request.state.csrf:
                request.state.csrf = new_csrf_cookie = secrets.token_urlsafe(24)
        else:
            return PlainTextResponse("Dashboard locked: set an owner password first (bizops set-password).", status_code=403)
        if request.method in UNSAFE:
            sent = await _submitted_csrf(request)
            if not (new_csrf_cookie is None and sent and hmac.compare_digest(sent, request.state.csrf)):
                return JSONResponse({"error": "CSRF token missing or wrong; reload the page and try again"}, status_code=403)
        resp = await call_next(request)
        if new_csrf_cookie:
            resp.set_cookie(CSRF_COOKIE, new_csrf_cookie, httponly=True, samesite="strict")
        return resp

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(LOGIN.replace("{err}", ""))

    @app.post("/login")
    async def login(request: Request):
        ip = client_ip(request)
        wait = lockout_remaining(db, ip)
        if wait:
            return HTMLResponse(LOGIN.replace("{err}", _err(f"Too many attempts. Try again in {wait // 60 + 1} minutes.")),
                                status_code=429)
        form = await request.form()
        stored = os.environ.get("BIZOPS_PASSWORD_HASH") or ""
        ok = bool(stored) and check_password(str(form.get("password", "")), stored)
        record_attempt(db, ip, ok)
        if not ok:
            return HTMLResponse(LOGIN.replace("{err}", _err("Wrong password")), status_code=401)
        token, _ = create_session(db, ip=ip, user_agent=request.headers.get("user-agent", ""))
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=TTL, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        s = get_session(db, request.cookies.get(COOKIE))
        if s:
            revoke(db, s["id"])
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/sessions/{sid}/revoke")
    async def session_revoke(sid: str):
        revoke(db, sid)
        return RedirectResponse("/system", status_code=303)

    @app.post("/sessions/revoke-others")
    async def session_revoke_others(request: Request):
        revoke_all(db, except_id=getattr(request.state, "session_id", None))
        return RedirectResponse("/system", status_code=303)
