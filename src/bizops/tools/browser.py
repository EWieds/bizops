"""Browser tools: drive a real browser, keep logins, read signup emails. See browser.py."""

from __future__ import annotations

import re
import secrets
import string

from .. import purchases
from ..db import insert
from ..engine import PolicyRefusal
from ..util import iso, new_id
from ..webclient import wrap_untrusted
from . import Ctx, ToolOutput, tool

PLACEHOLDER = re.compile(r"\{\{\s*(card|login)\s*:\s*([A-Za-z0-9_.-]+)\s*:\s*([a-z_]+)\s*\}\}")
CARD_FIELDS = ("number", "cvv", "exp", "exp_month", "exp_year", "name", "zip")
LOGIN_FIELDS = ("username", "password")

REF = {"ref": {"type": "string", "description": "element ref from the latest snapshot, e.g. e12 or f1e3"}}


async def _resolve(ctx: Ctx, text: str) -> tuple[str, list[str], bool]:
    """Fill {{card:<purchase_id>:<field>}} and {{login:<name>:<field>}}. Returns (text, secret values, has_card)."""
    found: list[str] = []
    has_card = False
    out = text
    for m in PLACEHOLDER.finditer(text):
        kind, key, field = m.groups()
        if kind == "card":
            if field not in CARD_FIELDS:
                raise PolicyRefusal("browser.placeholder", f"card fields: {', '.join(CARD_FIELDS)}")
            values = await purchases.card_values(ctx.core, org_id=ctx.agent["org_id"], purchase_id=key)
            value = values[field]
            if not value:
                raise PolicyRefusal("browser.placeholder", f"card {field} isn't set (owner: CARDHOLDER_NAME / CARDHOLDER_ZIP)")
            if field in ("number", "cvv"):
                found.append(value)
            has_card = True
        else:
            if field not in LOGIN_FIELDS:
                raise PolicyRefusal("browser.placeholder", "login fields: username, password")
            row = ctx.db.one("SELECT * FROM credentials WHERE org_id = ? AND name = ?", (ctx.agent["org_id"], key))
            if not row:
                raise PolicyRefusal("browser.placeholder", f"no saved login named {key!r}; use save_login")
            value = row["secret"] if field == "password" else row["username"]
            if field == "password":
                found.append(value)
        out = out.replace(m.group(0), value)
    return out, found, has_card


@tool("browser_open", grant="browser", action_class="browse", timeout_s=60,
      description="Open a web page in your browser tab and get it back as text: readable text plus a numbered list "
                  "of links, buttons and form fields (refs). Your business's logins and cookies are kept between runs.",
      properties={"url": {"type": "string"}}, required=["url"])
async def browser_open(ctx: Ctx, args: dict) -> str:
    return await ctx.core.browser.open(ctx.agent, str(args["url"]))


@tool("browser_snapshot", grant="browser", action_class="browse", timeout_s=45,
      description="Read the current page again (after waiting for something to load).")
async def browser_snapshot(ctx: Ctx, args: dict) -> str:
    return await ctx.core.browser.snapshot(ctx.agent)


@tool("browser_click", grant="browser", action_class="browse", timeout_s=45,
      description="Click a link, button, checkbox or tab by its ref. Returns the page after the click.",
      properties=REF, required=["ref"])
async def browser_click(ctx: Ctx, args: dict) -> str:
    return await ctx.core.browser.click(ctx.agent, str(args["ref"]))


@tool("browser_type", grant="browser", action_class="browse", timeout_s=60,
      description="Type into a form field by its ref (replaces what's there). Never type secrets yourself: use "
                  "placeholders the browser fills in: {{login:<name>:username}}, {{login:<name>:password}} for saved "
                  "logins, and {{card:<purchase_id>:number|exp|cvv|exp_month|exp_year|name|zip}} for an approved "
                  "purchase's card. submit=true presses Enter afterwards.",
      properties={**REF, "text": {"type": "string"}, "submit": {"type": "boolean"}},
      required=["ref", "text"])
async def browser_type(ctx: Ctx, args: dict) -> str:
    text, found, has_card = await _resolve(ctx, str(args["text"]))
    return await ctx.core.browser.type(ctx.agent, str(args["ref"]), text, secrets=found,
                                       submit=bool(args.get("submit")), slowly=has_card)


@tool("browser_select", grant="browser", action_class="browse", timeout_s=45,
      description="Choose an option in a dropdown by its ref and the option's visible text.",
      properties={**REF, "option": {"type": "string"}}, required=["ref", "option"])
async def browser_select(ctx: Ctx, args: dict) -> str:
    return await ctx.core.browser.select(ctx.agent, str(args["ref"]), str(args["option"]))


@tool("browser_press", grant="browser", action_class="browse", timeout_s=45,
      description="Press a key in the page (Enter, Tab, Escape, ArrowDown, PageDown...).",
      properties={"key": {"type": "string"}}, required=["key"])
async def browser_press(ctx: Ctx, args: dict) -> str:
    return await ctx.core.browser.press(ctx.agent, str(args["key"]))


@tool("audit_website", grant="web", action_class="web_read", timeout_s=120,
      description="Scan a public web page in a real browser: accessibility against WCAG 2.0/2.1/2.2 A/AA rules "
                  "(axe-core: violations by impact with the failing elements and the WCAG criteria) and speed "
                  "(time to first byte, largest contentful paint, load time, page weight, requests). No API key "
                  "needed. Use it for prospects' sites, customer deliverables and your own pages. mobile=true scans "
                  "at phone size.",
      properties={"url": {"type": "string"}, "mobile": {"type": "boolean"}}, required=["url"])
async def audit_website(ctx: Ctx, args: dict) -> str:
    import json
    report = await ctx.core.browser.audit(ctx.agent, str(args["url"]), mobile=bool(args.get("mobile")))
    return wrap_untrusted(report["url"], json.dumps(report, ensure_ascii=False)[:30000])


@tool("save_login", grant="browser", action_class="internal",
      description="Save a website login for your business before signing up. Leave password empty to generate a "
                  "strong one (you never see it). Then type {{login:<name>:username}} / {{login:<name>:password}}.",
      properties={"name": {"type": "string", "description": "short id, e.g. instagram"},
                  "site": {"type": "string"}, "username": {"type": "string", "description": "username or email"},
                  "password": {"type": "string", "description": "only if the site gave you one; else leave empty"}},
      required=["name", "site", "username"])
async def save_login(ctx: Ctx, args: dict) -> ToolOutput:
    name = str(args["name"]).strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{1,40}", name):
        raise PolicyRefusal("login.name", "name: 1-40 chars of a-z 0-9 _ . -")
    org_id = ctx.agent["org_id"]
    if ctx.db.one("SELECT 1 AS x FROM credentials WHERE org_id = ? AND name = ?", (org_id, name)):
        raise PolicyRefusal("login.exists", f"a login named {name!r} already exists; pick another name")
    pw = str(args.get("password") or "")
    if not pw:
        alphabet = string.ascii_letters + string.digits
        pw = "".join(secrets.choice(alphabet) for _ in range(18)) + secrets.choice("!#%+") + secrets.choice(string.digits)
    with ctx.db.tx() as conn:
        insert(conn, "credentials", {"id": new_id("cred"), "org_id": org_id, "name": name, "site": str(args["site"]),
                                     "username": str(args["username"]), "secret": pw, "created_by": ctx.agent["id"],
                                     "created_at": iso()})
    return ToolOutput({"saved": name, "username": args["username"],
                       "use": f"{{{{login:{name}:username}}}} and {{{{login:{name}:password}}}} in browser_type"})


@tool("list_logins", grant="browser", action_class="internal",
      description="Saved website logins for your business (names, sites, usernames; never passwords).")
async def list_logins(ctx: Ctx, args: dict) -> list:
    return ctx.db.all("SELECT name, site, username, created_at FROM credentials WHERE org_id = ? ORDER BY name",
                      (ctx.agent["org_id"],))


def signup_address(ctx: Ctx) -> str | None:
    from ..owner_setup import business_address
    return business_address(ctx.core, ctx.agent["org_id"])


@tool("signup_email", grant="browser", action_class="internal",
      description="The email address to sign up for services with. Mail sent to it (verification links and codes) "
                  "shows up in read_account_mail for your business only.")
async def signup_email(ctx: Ctx, args: dict) -> dict:
    addr = signup_address(ctx)
    if not addr:
        return {"address": None, "note": "The owner hasn't connected a mailbox yet (Connect page), so signups that "
                                         "need email verification aren't possible yet. Use request_human if blocked."}
    return {"address": addr}


@tool("read_account_mail", grant="browser", action_class="internal",
      description="Recent emails to your business's signup address (verification codes, confirmation links, receipts).",
      properties={"from_contains": {"type": "string", "description": "filter by sender, e.g. instagram"},
                  "minutes": {"type": "integer", "description": "how far back, default 60"}})
async def read_account_mail(ctx: Ctx, args: dict) -> str:
    from ..util import now
    from datetime import timedelta
    since = iso(now() - timedelta(minutes=max(1, min(int(args.get("minutes") or 60), 7 * 24 * 60))))
    q = ("SELECT from_addr, subject, body, created_at FROM emails WHERE org_id = ? AND direction = 'in' "
         "AND status = 'account_mail' AND created_at >= ?")
    params: list = [ctx.agent["org_id"], since]
    if args.get("from_contains"):
        q += " AND lower(from_addr) LIKE ?"
        params.append(f"%{str(args['from_contains']).lower()}%")
    rows = ctx.db.all(q + " ORDER BY created_at DESC LIMIT 10", tuple(params))
    if not rows:
        return "No account mail in that window. Verification emails can take a minute; the inbox is checked every minute."
    body = "\n\n".join(f"From: {r['from_addr']}\nAt: {r['created_at']}\nSubject: {r['subject']}\n\n{r['body'][:3000]}"
                       for r in rows)
    return wrap_untrusted("account mail", body)
