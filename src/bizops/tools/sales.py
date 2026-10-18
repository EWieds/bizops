"""Selling and delivering: CRM, compliant email, offers + payment links, customers, public pages,
shareable files and voice agents. All rules are enforced in business.py and the gateway."""

from __future__ import annotations

import json
import re

from .. import business
from ..engine import PolicyRefusal, get_org
from ..util import clip
from ..webclient import wrap_untrusted
from . import Ctx, ToolOutput, tool

EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_FIND = re.compile(r"(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def sandbox_world(ctx: Ctx) -> bool:
    """The simulated market is only for dry runs and tests, never when running for real."""
    return ctx.core.llm.name == "dry-run" or bool(ctx.settings.policy.get("sandbox", {}).get("force"))


def need_live(ctx: Ctx, what: str) -> None:
    if sandbox_world(ctx):
        return
    live = {"email": ctx.core.connectors.email.live, "billing": ctx.core.connectors.billing.live}[what]
    if not live:
        label = {"email": "an email account to send from (SMTP) and the sender's postal address",
                 "billing": "the umbrella Stripe account"}[what]
        raise PolicyRefusal(f"hard_stop.connect_{what}",
                            f"The owner must connect {label} once on the Connect page. Keep preparing everything else.")


# ---------------------------------------------------------------- CRM

@tool("find_businesses", grant="crm", action_class="web_read", timeout_s=120,
      description="Find real businesses to prospect: returns names, websites and source pages from a web search. "
                  "Then use extract_contacts on their site to get a public business email, and add_lead.",
      properties={"industry": {"type": "string"}, "city": {"type": "string"},
                  "count": {"type": "integer", "description": "1-20"}},
      required=["industry", "city"])
async def find_businesses(ctx: Ctx, args: dict) -> ToolOutput:
    count = max(1, min(int(args.get("count") or 10), 20))
    if sandbox_world(ctx):
        org = get_org(ctx.db, ctx.agent["org_id"])
        rows = business_sim(org["slug"], args["industry"], args["city"], count)
        return ToolOutput({"source": "sandbox market (dry run)", "businesses": rows})
    res = await ctx.core.web.search(f"{args['industry']} businesses in {args['city']} contact", count)
    lines = [f"- {r.get('title')} | {r.get('url')}" for r in res.results]
    return ToolOutput(wrap_untrusted(f"search:{res.provider}", "\n".join(lines) + f"\n\n{res.summary}"),
                      cost_micros=res.cost_micros)


def business_sim(slug: str, industry: str, city: str, count: int) -> list[dict]:
    from ..simworld import generate_businesses
    return [{k: b[k] for k in ("company", "contact_name", "email", "website", "city", "industry", "source_url")}
            for b in generate_businesses(slug, industry, city, count)]


@tool("extract_contacts", grant="crm", action_class="web_read", timeout_s=60,
      description="Fetch a business's public web page (and its /contact page) and pull out public emails and phones.",
      properties={"url": {"type": "string"}}, required=["url"])
async def extract_contacts(ctx: Ctx, args: dict) -> dict:
    url = args["url"].strip()
    if sandbox_world(ctx) and (url.endswith(".example") or ".example/" in url):
        return {"note": "sandbox business: use the email from find_businesses"}
    texts = []
    for u in (url, url.rstrip("/") + "/contact"):
        try:
            texts.append((await ctx.core.web.fetch(u))["text"])
        except Exception:
            continue
    blob = "\n".join(texts)
    emails = sorted({e.lower() for e in EMAIL_FIND.findall(blob) if not e.lower().endswith((".png", ".jpg", ".webp"))})
    phones = sorted(set(PHONE_FIND.findall(blob)))[:5]
    return {"url": url, "emails": emails[:10], "phones": phones, "source_url": url}


@tool("add_lead", grant="crm", action_class="internal", ref_type="lead",
      description="Add a business to the CRM. B2B only: record the public page where you found it (source_url).",
      properties={"company": {"type": "string"}, "email": {"type": "string"}, "source_url": {"type": "string"},
                  "contact_name": {"type": "string"}, "website": {"type": "string"}, "phone": {"type": "string"},
                  "city": {"type": "string"}, "industry": {"type": "string"}, "notes": {"type": "string"}},
      required=["company", "source_url"])
async def add_lead(ctx: Ctx, args: dict) -> ToolOutput:
    sim = None
    sandbox = False
    email = (args.get("email") or "").lower()
    if email.endswith(".example"):
        if not sandbox_world(ctx):
            raise PolicyRefusal("lead.sandbox", "that is a sandbox address; find real businesses")
        org = get_org(ctx.db, ctx.agent["org_id"])
        from ..simworld import generate_businesses
        for b in generate_businesses(org["slug"], args.get("industry", ""), args.get("city", ""), 20):
            if b["email"] == email:
                sim = b["sim"]
        sandbox = True
    lead = business.add_lead(ctx.db, org_id=ctx.agent["org_id"], agent=ctx.agent, company=args["company"],
                             email=args.get("email"), source_url=args.get("source_url", ""),
                             contact_name=args.get("contact_name", ""), website=args.get("website", ""),
                             phone=args.get("phone", ""), city=args.get("city", ""), industry=args.get("industry", ""),
                             notes=args.get("notes", ""), sandbox=sandbox, sim=sim or ({} if sandbox else None),
                             b2b_only=bool(ctx.settings.policy.get("sales", {}).get("b2b_only", True)))
    return ToolOutput({"lead_id": lead["id"], "company": lead["company"]}, ref_type="lead", ref_id=lead["id"])


@tool("update_lead", grant="crm", action_class="internal", ref_type="lead",
      description="Move a lead through the pipeline and keep notes: new, qualified, contacted, replied, interested, "
                  "demo, proposal, won, lost.",
      properties={"lead_id": {"type": "string"}, "stage": {"type": "string", "enum": list(business.LEAD_STAGES[:-1])},
                  "notes": {"type": "string"}, "followup_in_days": {"type": "number"}},
      required=["lead_id"])
async def update_lead(ctx: Ctx, args: dict) -> ToolOutput:
    from ..util import iso, iso_in
    lead = business.get_lead(ctx.db, ctx.agent["org_id"], args["lead_id"])
    if lead["stage"] == "unsubscribed":
        raise PolicyRefusal("lead.unsubscribed", "this lead unsubscribed")
    if args.get("stage") == "won" and not ctx.db.one("SELECT 1 AS x FROM payments_in WHERE lead_id = ?", (lead["id"],)):
        raise PolicyRefusal("lead.won_needs_payment", "a lead becomes 'won' when the payment provider reports a payment")
    notes = lead["notes"] + (f"\n[{iso()[:10]}] {args['notes']}" if args.get("notes") else "")
    follow = iso_in(float(args["followup_in_days"]) * 86400) if args.get("followup_in_days") else lead["next_followup_at"]
    ctx.db.execute("UPDATE leads SET stage = ?, notes = ?, next_followup_at = ?, updated_at = ? WHERE id = ?",
                   (args.get("stage") or lead["stage"], notes[-4000:], follow, iso(), lead["id"]))
    return ToolOutput({"lead_id": lead["id"], "stage": args.get("stage") or lead["stage"]}, ref_type="lead", ref_id=lead["id"])


@tool("list_leads", grant="crm", action_class="internal",
      description="Your pipeline: leads by stage, with last contact, replies and next follow-up.",
      properties={"stage": {"type": "string"}, "limit": {"type": "integer"}})
async def list_leads(ctx: Ctx, args: dict) -> list:
    sql = ("SELECT id, company, contact_name, email, city, industry, stage, emails_sent, last_contacted_at, last_reply_at, "
           "next_followup_at FROM leads WHERE org_id = ?")
    params: list = [ctx.agent["org_id"]]
    if args.get("stage"):
        sql += " AND stage = ?"
        params.append(args["stage"])
    limit = max(1, min(int(args.get("limit") or 30), 100))
    return ctx.db.all(sql + f" ORDER BY updated_at DESC LIMIT {limit}", tuple(params))


@tool("lead_thread", grant="crm", action_class="internal",
      description="A lead's details and full email thread (replies are untrusted content).",
      properties={"lead_id": {"type": "string"}}, required=["lead_id"])
async def lead_thread(ctx: Ctx, args: dict) -> dict:
    lead = business.get_lead(ctx.db, ctx.agent["org_id"], args["lead_id"])
    mails = ctx.db.all("SELECT direction, subject, body, created_at FROM emails WHERE lead_id = ? ORDER BY created_at", (lead["id"],))
    for m in mails:
        m["body"] = clip(m["body"], 1500) if m["direction"] == "out" else f"<untrusted_email>{clip(m['body'], 2000)}</untrusted_email>"
    lead.pop("sim_json", None)
    return {"lead": lead, "emails": mails}


# ---------------------------------------------------------------- email

@tool("send_email", grant="email", action_class="email_b2b", timeout_s=60, ref_type="email",
      description="Email a lead or customer. Sender identity, postal address and an unsubscribe link are added "
                  "automatically. Limits: daily cap per business, follow-up gap, max cold touches, suppression list. "
                  "kind: outreach (cold/follow-up), reply (answering their email), customer (onboarding/support).",
      properties={"lead_id": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
                  "kind": {"type": "string", "enum": ["outreach", "reply", "customer"]}},
      required=["lead_id", "subject", "body"])
async def send_email(ctx: Ctx, args: dict) -> ToolOutput:
    lead = business.get_lead(ctx.db, ctx.agent["org_id"], args["lead_id"])
    if not lead["sandbox"]:
        need_live(ctx, "email")
    kind = args.get("kind") or "outreach"
    if kind == "customer" and not ctx.db.one("SELECT 1 AS x FROM customers WHERE lead_id = ?", (lead["id"],)):
        kind = "reply" if lead["last_reply_at"] else "outreach"
    from ..decide import quality_gate
    await quality_gate(ctx.core, org_id=ctx.agent["org_id"], kind=kind, text=f"Subject: {args['subject']}\n\n{args['body']}",
                       context={"recipient": {k: lead[k] for k in ("company", "industry", "city", "contact_name")}})
    out = await business.send_email(ctx.core, agent=ctx.agent, lead_id=lead["id"], subject=args["subject"],
                                    body=args["body"], kind=kind)
    return ToolOutput(out, ref_type="email", ref_id=out["email_id"])


# ---------------------------------------------------------------- offers, payments, customers

@tool("create_offer", grant="sales", action_class="spend", timeout_s=90, ref_type="offer",
      description="Create something customers can buy: a product with a price and a payment link on the umbrella's "
                  "Stripe account (labeled with this business). interval: one_time or month.",
      properties={"name": {"type": "string"}, "description": {"type": "string"}, "price_usd": {"type": "number"},
                  "interval": {"type": "string", "enum": ["one_time", "month"]}},
      required=["name", "description", "price_usd", "interval"])
async def create_offer(ctx: Ctx, args: dict) -> ToolOutput:
    need_live(ctx, "billing")
    offer = await business.create_offer(ctx.core, agent=ctx.agent, name=args["name"], description=args["description"],
                                        price_usd=float(args["price_usd"]), interval=args["interval"])
    return ToolOutput({"offer_id": offer["id"], "payment_url": offer["payment_url"], "provider": offer["provider"]},
                      ref_type="offer", ref_id=offer["id"])


@tool("list_offers", grant="sales", action_class="internal", description="Offers (products/prices) of your business.")
async def list_offers(ctx: Ctx, args: dict) -> list:
    return ctx.db.all("SELECT id, name, description, price_cents, interval, payment_url, status FROM offers "
                      "WHERE org_id = ? ORDER BY created_at DESC", (ctx.agent["org_id"],))


@tool("send_payment_link", grant="sales", action_class="email_b2b", timeout_s=60, ref_type="email",
      description="Email a lead a checkout link for an offer (tracked to the lead). Use when they've said yes or asked "
                  "how to sign up. Payment shows up automatically when the provider confirms it.",
      properties={"lead_id": {"type": "string"}, "offer_id": {"type": "string"},
                  "subject": {"type": "string"}, "message": {"type": "string"}},
      required=["lead_id", "offer_id", "message"])
async def send_payment_link(ctx: Ctx, args: dict) -> ToolOutput:
    lead = business.get_lead(ctx.db, ctx.agent["org_id"], args["lead_id"])
    offer = ctx.db.one("SELECT * FROM offers WHERE id = ? AND org_id = ? AND status = 'active'", (args["offer_id"], ctx.agent["org_id"]))
    if not offer:
        raise PolicyRefusal("offer.missing", "no active offer with that id in your org")
    if not lead["sandbox"]:
        need_live(ctx, "email")
    link = ctx.core.connectors.billing.link_for(offer, lead["id"], business.public_base(ctx.db, ctx.settings))
    if lead["sandbox"] and ctx.core.connectors.billing.live:
        link = f"{business.public_base(ctx.db, ctx.settings)}/pay/{offer['id']}?ref={lead['id']}"
    price = f"${offer['price_cents'] / 100:,.2f}" + ("/month" if offer["interval"] == "month" else "")
    body = f"{args['message'].rstrip()}\n\n{offer['name']}: {price}\nSign up here: {link}"
    from ..decide import quality_gate
    await quality_gate(ctx.core, org_id=ctx.agent["org_id"], kind="reply", text=body,
                       context={"recipient": {k: lead[k] for k in ("company", "industry", "city")}})
    out = await business.send_email(ctx.core, agent=ctx.agent, lead_id=lead["id"], subject=args.get("subject") or f"{offer['name']}: sign-up link",
                                    body=body, kind="reply")
    ctx.db.execute("UPDATE leads SET stage = 'proposal' WHERE id = ? AND stage NOT IN ('won')", (lead["id"],))
    return ToolOutput({**out, "link": link}, ref_type="email", ref_id=out["email_id"])


@tool("list_customers", grant="sales", action_class="internal", description="Paying customers and their status.")
async def list_customers(ctx: Ctx, args: dict) -> list:
    return ctx.db.all("SELECT c.id, c.company, c.email, c.status, c.mrr_cents, c.voice_agent_id, c.lead_id, c.notes, "
                      "o.name AS offer FROM customers c LEFT JOIN offers o ON o.id = c.offer_id WHERE c.org_id = ? "
                      "ORDER BY c.created_at DESC", (ctx.agent["org_id"],))


@tool("update_customer", grant="sales", action_class="internal", ref_type="customer",
      description="Update a customer's status (onboarding, live, churned) and notes.",
      properties={"customer_id": {"type": "string"}, "status": {"type": "string", "enum": ["onboarding", "live", "churned"]},
                  "notes": {"type": "string"}},
      required=["customer_id"])
async def update_customer(ctx: Ctx, args: dict) -> ToolOutput:
    from ..util import iso
    c = ctx.db.one("SELECT * FROM customers WHERE id = ? AND org_id = ?", (args["customer_id"], ctx.agent["org_id"]))
    if not c:
        raise PolicyRefusal("customer.missing", "no such customer")
    notes = (c["notes"] + f"\n[{iso()[:10]}] {args['notes']}") if args.get("notes") else c["notes"]
    ctx.db.execute("UPDATE customers SET status = COALESCE(?, status), notes = ?, updated_at = ? WHERE id = ?",
                   (args.get("status"), notes[-4000:], iso(), c["id"]))
    return ToolOutput({"customer_id": c["id"], "status": args.get("status") or c["status"]}, ref_type="customer", ref_id=c["id"])


# ---------------------------------------------------------------- public pages and files

@tool("publish_page", grant="site", action_class="publish", ref_type="site",
      description="Publish a public web page for your business (landing page, pricing, onboarding form instructions). "
                  "Give full HTML (inline CSS fine). path 'index' is the home page. Returns its public URL.",
      properties={"path": {"type": "string"}, "title": {"type": "string"}, "html": {"type": "string"}},
      required=["path", "title", "html"])
async def publish_page(ctx: Ctx, args: dict) -> ToolOutput:
    from ..decide import quality_gate
    await quality_gate(ctx.core, org_id=ctx.agent["org_id"], kind="page", text=args["html"], context={"title": args["title"]})
    r = business.publish_page(ctx.db, agent=ctx.agent, path=args["path"], title=args["title"], html_body=args["html"])
    url = business.page_url(ctx.db, ctx.settings, ctx.agent["org_id"], r["path"])
    return ToolOutput({"url": url, **r}, ref_type="site", ref_id=r["site_id"])


@tool("share_file", grant="site", action_class="publish",
      description="Get a public link to a file your org produced (e.g. a deliverable for a customer).",
      properties={"artifact_id": {"type": "string"}}, required=["artifact_id"])
async def share_file(ctx: Ctx, args: dict) -> dict:
    row = ctx.db.one("SELECT * FROM artifacts WHERE (id = ? OR name = ?) AND org_id = ? ORDER BY created_at DESC LIMIT 1",
                     (args["artifact_id"], args["artifact_id"], ctx.agent["org_id"]))
    if not row:
        raise PolicyRefusal("files.missing", "no such file in your org")
    return {"url": f"{business.public_base(ctx.db, ctx.settings)}/f/{row['id']}/{business.file_sig(row['id'])}",
            "name": row["name"]}


# ---------------------------------------------------------------- voice agents (one product type)

VOICE_PROPS = {"business_name": {"type": "string"}, "greeting": {"type": "string"},
               "instructions": {"type": "string", "description": "how this receptionist should behave"},
               "hours": {"type": "string"}, "services": {"type": "string"},
               "booking": {"type": "string", "description": "how to handle appointment requests"},
               "faqs": {"type": "array", "items": {"type": "object", "properties": {"q": {"type": "string"}, "a": {"type": "string"}}}},
               "customer_id": {"type": "string", "description": "the paying customer this agent is for (omit for a demo)"}}


@tool("create_voice_agent", grant="voice", action_class="publish", timeout_s=90, ref_type="voice_agent",
      description="Create an AI phone receptionist for a business (a demo, or for a paying customer). It gets a public "
                  "web demo link immediately; with a voice provider connected it can also answer real calls (inbound only).",
      properties=VOICE_PROPS, required=["business_name", "greeting", "instructions"])
async def create_voice_agent(ctx: Ctx, args: dict) -> ToolOutput:
    cfg = {k: args[k] for k in VOICE_PROPS if k in args and k != "customer_id"}
    r = await business.upsert_voice_agent(ctx.core, agent=ctx.agent, cfg=cfg, customer_id=args.get("customer_id"))
    return ToolOutput(r, ref_type="voice_agent", ref_id=r["voice_agent_id"])


@tool("update_voice_agent", grant="voice", action_class="publish", timeout_s=90, ref_type="voice_agent",
      description="Update a voice agent's configuration (merges with the current one).",
      properties={"voice_agent_id": {"type": "string"}, **VOICE_PROPS}, required=["voice_agent_id"])
async def update_voice_agent(ctx: Ctx, args: dict) -> ToolOutput:
    row = ctx.db.one("SELECT * FROM voice_agents WHERE id = ? AND org_id = ?", (args["voice_agent_id"], ctx.agent["org_id"]))
    if not row:
        raise PolicyRefusal("voice.missing", "no such voice agent")
    cfg = {**json.loads(row["config_json"]), **{k: args[k] for k in VOICE_PROPS if k in args and k != "customer_id"}}
    r = await business.upsert_voice_agent(ctx.core, agent=ctx.agent, cfg=cfg, voice_agent_id=row["id"],
                                          customer_id=args.get("customer_id"))
    return ToolOutput(r, ref_type="voice_agent", ref_id=row["id"])


@tool("test_voice_agent", grant="voice", action_class="internal", timeout_s=120,
      description="Role-play a caller against a voice agent and see its answer (quality check before going live).",
      properties={"voice_agent_id": {"type": "string"}, "caller_says": {"type": "string"}},
      required=["voice_agent_id", "caller_says"])
async def test_voice_agent(ctx: Ctx, args: dict) -> ToolOutput:
    from ..connectors.voice import receptionist_prompt
    from ..llm.base import response_cost
    row = ctx.db.one("SELECT * FROM voice_agents WHERE id = ? AND org_id = ?", (args["voice_agent_id"], ctx.agent["org_id"]))
    if not row:
        raise PolicyRefusal("voice.missing", "no such voice agent")
    answer, resp = await voice_reply(ctx.core, json.loads(row["config_json"]), [{"role": "user", "content": args["caller_says"]}])
    return ToolOutput({"agent_says": answer}, cost_micros=response_cost(ctx.settings, resp) if resp else 0)


def voice_model(settings) -> str:
    return settings.models.get("voice_model") or settings.models["tiers"]["worker"]


async def voice_reply(core, cfg: dict, history: list[dict]) -> tuple[str, object]:
    """One receptionist turn, shared by test_voice_agent and the public web demo."""
    from ..connectors.voice import receptionist_prompt
    if core.llm.name == "dry-run":
        last = history[-1]["content"]
        return (f"Thanks for calling {cfg.get('business_name')}! Our hours are {cfg.get('hours', 'listed on our site')}. "
                f"I can take your name and number for: {clip(last, 80)}"), None
    model = voice_model(core.settings)
    transcript = "\n".join(f"{'Caller' if m['role'] == 'user' else 'You'}: {m['content']}" for m in history[-12:])
    data, resp = await core.llm.structured(model=model, system=receptionist_prompt(cfg),
                                           prompt=f"Phone call so far:\n{transcript}\n\nReply as the receptionist (1-3 short sentences).",
                                           schema={"type": "object", "properties": {"reply": {"type": "string"}},
                                                   "required": ["reply"], "additionalProperties": False}, max_tokens=400)
    return str(data.get("reply") or "Sorry, could you repeat that?"), resp


@tool("set_voice_agent_status", grant="voice", action_class="publish", ref_type="voice_agent",
      description="Mark a customer's voice agent live (after testing) or paused.",
      properties={"voice_agent_id": {"type": "string"}, "status": {"type": "string", "enum": ["live", "paused", "demo"]}},
      required=["voice_agent_id", "status"])
async def set_voice_agent_status(ctx: Ctx, args: dict) -> ToolOutput:
    from ..util import iso
    row = ctx.db.one("SELECT * FROM voice_agents WHERE id = ? AND org_id = ?", (args["voice_agent_id"], ctx.agent["org_id"]))
    if not row:
        raise PolicyRefusal("voice.missing", "no such voice agent")
    if args["status"] == "live" and not row["customer_id"]:
        raise PolicyRefusal("voice.live_needs_customer", "only a paying customer's agent can go live")
    ctx.db.execute("UPDATE voice_agents SET status = ?, updated_at = ? WHERE id = ?", (args["status"], iso(), row["id"]))
    return ToolOutput({"voice_agent_id": row["id"], "status": args["status"]}, ref_type="voice_agent", ref_id=row["id"])
