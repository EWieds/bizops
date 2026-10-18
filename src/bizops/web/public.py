"""Public routes: what customers and prospects see. No login. Everything else needs the owner login.

/s/<business>/<page>   business websites the agents publish
/demo/<voice agent>    live web demo of a voice agent (talk to it by text)
/pay/<offer>           checkout: redirects to Stripe (or the test checkout in dry runs)
/u/<token>             one-click unsubscribe
/f/<artifact>/<sig>    shared files (deliverables)
"""

from __future__ import annotations

import html
import json
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import ledger
from ..business import record_payment, suppress, unsub_email, unsub_token
from ..connectors.billing import sandbox_ref
from ..events import log_event
from ..llm.base import response_cost

PUBLIC_PREFIXES = ("/s/", "/demo/", "/pay/", "/u/", "/f/", "/healthz", "/login", "/logout")

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1e293b;background:#f8fafc}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px}}button{{background:#0f172a;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:15px;cursor:pointer}}
input,textarea{{width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:8px;padding:10px;font-size:15px}}.muted{{color:#64748b;font-size:13px}}
.msg{{margin:8px 0;padding:8px 12px;border-radius:10px;max-width:85%}}.me{{background:#0f172a;color:#fff;margin-left:auto}}.bot{{background:#e2e8f0}}</style></head>
<body>{body}</body></html>"""


def register_public(app: FastAPI, core) -> None:
    db, settings = core.db, core.settings
    hits: dict[str, deque] = defaultdict(deque)

    def limited(key: str, per_hour: int) -> bool:
        q, t = hits[key], time.time()
        while q and t - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return True
        q.append(t)
        return False

    @app.get("/s/{slug}/{path:path}", response_class=HTMLResponse)
    async def site(slug: str, path: str = ""):
        org = db.one("SELECT id, name, status FROM orgs WHERE slug = ?", (slug,))
        if not org or org["status"] == "retired":
            raise HTTPException(404)
        page = db.one("SELECT * FROM sites WHERE org_id = ? AND path = ?", (org["id"], path.strip("/").lower() or "index"))
        if not page:
            raise HTTPException(404)
        body = page["html"]
        if "<html" not in body.lower():
            body = PAGE.format(title=html.escape(page["title"]), body=body)
        return HTMLResponse(body)

    @app.get("/demo/{vid}", response_class=HTMLResponse)
    async def demo(vid: str):
        va = db.one("SELECT * FROM voice_agents WHERE id = ? AND status != 'paused'", (vid,))
        if not va:
            raise HTTPException(404)
        cfg = json.loads(va["config_json"])
        greeting = cfg.get("greeting") or f"Thanks for calling {va['business_name']}, how can I help?"
        body = f"""<div class="card"><h2 style="margin-top:0">{html.escape(va['business_name'])}: AI receptionist demo</h2>
<p class="muted">Type what a caller would say. This is the same assistant that answers the phone.</p>
<div id="log"><div class="msg bot">{html.escape(greeting)}</div></div>
<form id="f" style="display:flex;gap:8px;margin-top:12px"><input id="t" placeholder="Hi, do you have anything open Friday?" autocomplete="off"><button>Send</button></form></div>
<script>
const hist=[{{role:'assistant',content:{json.dumps(greeting)}}}];const log=document.getElementById('log');
function add(c,t){{const d=document.createElement('div');d.className='msg '+c;d.textContent=t;log.appendChild(d);}}
document.getElementById('f').onsubmit=async e=>{{e.preventDefault();const i=document.getElementById('t');const v=i.value.trim();if(!v)return;i.value='';
add('me',v);hist.push({{role:'user',content:v}});const r=await fetch('/demo/{vid}/say',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{history:hist}})}});
const j=await r.json();add('bot',j.reply);hist.push({{role:'assistant',content:j.reply}});}};
</script>"""
        return HTMLResponse(PAGE.format(title=f"{html.escape(va['business_name'])} demo", body=body))

    @app.post("/demo/{vid}/say")
    async def demo_say(vid: str, request: Request):
        va = db.one("SELECT * FROM voice_agents WHERE id = ? AND status != 'paused'", (vid,))
        if not va:
            raise HTTPException(404)
        ip = request.client.host if request.client else "?"
        if limited(f"ip:{ip}", 60) or limited(f"va:{vid}", 300):
            return JSONResponse({"reply": "This demo is busy right now, please try again later."})
        data = await request.json()
        history = [m for m in (data.get("history") or [])[-12:] if m.get("role") in ("user", "assistant")]
        history = [{"role": m["role"], "content": str(m.get("content", ""))[:600]} for m in history]
        if not history or history[-1]["role"] != "user":
            return JSONResponse({"reply": "Sorry, I didn't catch that."})
        from ..tools.sales import voice_reply
        try:
            reply, resp = await voice_reply(core, json.loads(va["config_json"]), history)
        except Exception:
            return JSONResponse({"reply": "Sorry, I'm having trouble right now. Please try again."})
        if resp is not None:
            cost = response_cost(settings, resp)
            if cost:
                ledger.record(db, org_id=va["org_id"], kind="model_cost", amount_micros=cost, source="model",
                              memo=f"voice demo {vid}")
        return JSONResponse({"reply": reply})

    @app.get("/pay/{offer_id}", response_class=HTMLResponse)
    async def pay(offer_id: str, ref: str = ""):
        offer = db.one("SELECT * FROM offers WHERE id = ? AND status = 'active'", (offer_id,))
        if not offer:
            raise HTTPException(404)
        if offer["provider"] != "sandbox":
            return RedirectResponse(core.connectors.billing.link_for(offer, ref or None, ""), status_code=302)
        price = f"${offer['price_cents'] / 100:,.2f}" + ("/month" if offer["interval"] == "month" else "")
        body = f"""<div class="card"><p style="background:#fef3c7;padding:8px;border-radius:8px">TEST CHECKOUT: no real money moves.
Real checkouts go through Stripe once the owner connects it.</p><h2>{html.escape(offer['name'])}</h2><p>{html.escape(offer['description'])}</p>
<p style="font-size:22px"><b>{price}</b></p><form method="post"><input type="hidden" name="ref" value="{html.escape(ref)}">
<input name="email" placeholder="your email" style="margin-bottom:8px"><button>Complete test payment</button></form></div>"""
        return HTMLResponse(PAGE.format(title=html.escape(offer["name"]), body=body))

    @app.post("/pay/{offer_id}", response_class=HTMLResponse)
    async def pay_post(offer_id: str, ref: str = Form(""), email: str = Form("")):
        offer = db.one("SELECT * FROM offers WHERE id = ? AND status = 'active' AND provider = 'sandbox'", (offer_id,))
        if not offer:
            raise HTTPException(404)
        record_payment(db, settings, provider="sandbox", provider_ref=sandbox_ref(), amount_cents=offer["price_cents"],
                       offer_id=offer["id"], lead_id=ref or None, payer_email=email or None, sandbox=True)
        return HTMLResponse(PAGE.format(title="Thanks", body="<div class='card'><h2>Thank you!</h2><p>(Test payment recorded.) "
                                                              "We'll be in touch by email to get you set up.</p></div>"))

    @app.get("/u/{token}", response_class=HTMLResponse)
    async def unsubscribe(token: str):
        email = unsub_email(settings, token)
        if not email:
            raise HTTPException(404)
        suppress(db, email, "unsubscribe link")
        log_event(db, actor_kind="human", kind="system", action="email.unsubscribe", args={"email": email}, outcome="ok")
        return HTMLResponse(PAGE.format(title="Unsubscribed", body=f"<div class='card'><h2>You're unsubscribed</h2>"
                                                                    f"<p>{html.escape(email)} won't receive any more emails from us.</p></div>"))

    @app.post("/u/{token}")
    async def unsubscribe_post(token: str):  # RFC 8058 one-click
        email = unsub_email(settings, token)
        if email:
            suppress(db, email, "one-click unsubscribe")
        return JSONResponse({"ok": True})

    @app.get("/f/{aid}/{sig}")
    async def shared_file(aid: str, sig: str):
        if unsub_token(settings, aid).split(".")[1] != sig:
            raise HTTPException(404)
        row = db.one("SELECT * FROM artifacts WHERE id = ?", (aid,))
        if not row:
            raise HTTPException(404)
        path = (settings.artifacts_dir / row["path"]).resolve()
        if settings.artifacts_dir.resolve() not in path.parents:
            raise HTTPException(404)
        return FileResponse(path, filename=row["name"], media_type=row["mime"])
