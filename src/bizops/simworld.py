"""The sandbox market: simulated small businesses that read outreach, reply, object, unsubscribe and pay.

It exists so the whole loop (research -> outreach -> replies -> payment link -> payment -> onboarding
-> fulfillment) can run and be verified end to end before real email and Stripe accounts are connected.
Everything it produces is marked sandbox: sandbox revenue never counts as real revenue.
Prospects are deterministic per lead, so runs are reproducible.
"""

from __future__ import annotations

import json
import random
import re
from datetime import timedelta
from typing import Any

from .config import Settings
from .db import Database, insert
from .util import iso, new_id, now

FIRST = ["Maple", "Summit", "Bright", "Lakeside", "Oak", "Riverbend", "Golden", "Northside", "Evergreen", "Cedar",
         "Harbor", "Prairie", "Granite", "Sunrise", "Willow", "Keystone", "Bluebird", "Ironwood", "Liberty", "Pioneer"]
CONTACTS = ["Dana", "Luis", "Priya", "Mike", "Sarah", "Tom", "Aisha", "Greg", "Mei", "Carlos", "Jen", "Omar"]
SUFFIX = {"dental": ["Dental", "Family Dentistry", "Smiles"], "hvac": ["Heating & Air", "HVAC", "Comfort Systems"],
          "plumbing": ["Plumbing", "Plumbing & Drain", "Rooter"], "salon": ["Salon", "Hair Studio", "Beauty Bar"],
          "law": ["Law Group", "Legal", "Attorneys"], "roofing": ["Roofing", "Roof Co", "Exteriors"],
          "auto": ["Auto Repair", "Garage", "Auto Care"], "vet": ["Veterinary", "Animal Clinic", "Pet Hospital"]}
OBJECTIONS = ["We already have a receptionist.", "Worried it will sound robotic to patients.",
              "Not sure about the price.", "We get maybe 20 calls a day, is it worth it?",
              "How does it book into our calendar?", "Can it handle Spanish-speaking callers?"]


def _industry_key(industry: str) -> str:
    t = industry.lower()
    for k in SUFFIX:
        if k in t:
            return k
    return "dental" if "dent" in t else "hvac" if "heat" in t or "air" in t else "plumbing" if "plumb" in t else "auto"


def generate_businesses(org_slug: str, industry: str, city: str, count: int) -> list[dict[str, Any]]:
    key = _industry_key(industry)
    rng = random.Random(f"{org_slug}|{key}|{city.lower()}")
    out, used = [], set()
    for i in range(count * 3):
        if len(out) >= count:
            break
        name = f"{rng.choice(FIRST)} {rng.choice(SUFFIX[key])}"
        if name in used:
            continue
        used.add(name)
        slug = re.sub(r"[^a-z0-9]+", "", name.lower())
        contact = rng.choice(CONTACTS)
        sim = {"interest": round(rng.random(), 2), "budget_usd": rng.choice([99, 149, 199, 299, 399, 499]),
               "objection": rng.choice(OBJECTIONS), "unsub": rng.random() < 0.12, "calls_per_day": rng.randint(10, 80),
               "hours": rng.choice(["Mon-Fri 8-5", "Mon-Sat 9-6", "Mon-Fri 7:30-6, Sat 8-12"]),
               "services": {"dental": "cleanings, fillings, crowns, whitening, emergencies",
                            "hvac": "AC repair, furnace repair, installs, maintenance plans",
                            "plumbing": "leaks, drains, water heaters, emergencies 24/7",
                            "salon": "cuts, color, styling, extensions", "law": "personal injury, family law, estate planning",
                            "roofing": "repairs, replacements, inspections, storm damage",
                            "auto": "oil changes, brakes, diagnostics, tires", "vet": "wellness exams, vaccines, surgery, dental"}[key]}
        out.append({"company": name, "contact_name": contact, "email": f"{contact.lower()}@{slug}.example",
                    "website": f"https://{slug}.example", "city": city, "industry": industry,
                    "source_url": f"https://directory.sandbox.example/{key}/{re.sub(r'[^a-z0-9]+', '-', city.lower())}/{slug}",
                    "sim": sim})
    return out


def _enqueue(db: Database, kind: str, payload: dict, delay_s: float) -> None:
    with db.tx() as conn:
        insert(conn, "sim_actions", {"id": new_id("sim"), "due_at": iso(now() + timedelta(seconds=delay_s)),
                                     "kind": kind, "payload_json": json.dumps(payload), "done": 0})


def schedule_reaction(db: Database, settings: Settings, *, email_id: str, lead: dict, subject: str, body: str) -> None:
    sim = json.loads(lead.get("sim_json") or "{}")
    if not sim:
        return
    delay = float(settings.policy.get("sandbox", {}).get("reply_delay_s", 45))
    rng = random.Random(f"{lead['id']}|{lead['emails_sent']}|{email_id[-4:]}")
    contact = lead["contact_name"] or "the owner"
    re_subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply = lambda text, d=delay: _enqueue(db, "reply", {"lead_id": lead["id"], "org_id": lead["org_id"], "from": lead["email"],
                                                         "subject": re_subj, "body": f"{text}\n\n{contact}\n{lead['company']}"}, d)
    if lead["stage"] == "won":
        if not sim.get("sent_onboarding"):
            sim["sent_onboarding"] = True
            db.execute("UPDATE leads SET sim_json = ? WHERE id = ?", (json.dumps(sim), lead["id"]))
            reply(f"Thanks! Here's what you need. Hours: {sim['hours']}. Services: {sim['services']}. "
                  f"We get about {sim['calls_per_day']} calls a day. For booking, take the caller's name, number and "
                  "what they need, and text it to our front desk. Common questions: do we take new customers (yes), "
                  "do we offer financing (yes, through CareCredit), where do we park (lot behind the building).")
        return
    if sim.get("unsub") and rng.random() < 0.7:
        reply("Please remove me from your list.")
        return
    link = re.search(r"/pay/(offer_[0-9a-f]+)", body) or re.search(r"client_reference_id=(lead_[0-9a-f]+)", body)
    if link and sim["interest"] >= 0.45:
        offer_id = link.group(1) if link.group(1).startswith("offer_") else None
        price = None
        if offer_id:
            row = db.one("SELECT price_cents FROM offers WHERE id = ?", (offer_id,))
            price = row["price_cents"] / 100 if row else None
        if price is not None and price <= sim["budget_usd"]:
            _enqueue(db, "pay", {"offer_id": offer_id, "lead_id": lead["id"], "email": lead["email"]}, delay * 1.5)
            reply("Looks good, I just signed up through the link. What do you need from us to set it up?", delay * 1.6)
        else:
            reply(f"Honestly that's more than we can spend right now. Our budget would be closer to ${sim['budget_usd']}/month.")
        return
    if sim["interest"] >= 0.45:
        reply(f"This sounds interesting. {sim['objection']} What does it cost, and how fast can we try it? "
              f"We miss a lot of calls when the front desk is busy (about {sim['calls_per_day']} calls a day).")
    elif sim["interest"] >= 0.3 and rng.random() < 0.5:
        reply("Not right now, maybe check back next quarter.")


def tick(db: Database, settings: Settings) -> int:
    """Run due simulated reactions. Called by the maintenance loop."""
    from .business import record_inbound, record_payment
    from .connectors.billing import sandbox_ref
    n = 0
    for a in db.all("SELECT * FROM sim_actions WHERE done = 0 AND due_at <= ? ORDER BY due_at LIMIT 50", (iso(),)):
        db.execute("UPDATE sim_actions SET done = 1 WHERE id = ?", (a["id"],))
        p = json.loads(a["payload_json"])
        if a["kind"] == "reply":
            record_inbound(db, org_id=p["org_id"], from_addr=p["from"], to_addr="sandbox", subject=p["subject"],
                           body=p["body"], provider="sandbox", provider_id=new_id("simin"))
        elif a["kind"] == "pay":
            offer = db.one("SELECT * FROM offers WHERE id = ?", (p["offer_id"],))
            if offer:
                record_payment(db, settings, provider="sandbox", provider_ref=sandbox_ref(), amount_cents=offer["price_cents"],
                               offer_id=offer["id"], lead_id=p["lead_id"], payer_email=p["email"], sandbox=True)
        n += 1
    return n
