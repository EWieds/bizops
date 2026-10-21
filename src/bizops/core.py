"""The service container: settings, database, model client, web, browser, payments, cards, approver."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .approver import Approver
from .browser import Browser
from .cards import CardProvider, make_cards
from .connectors import Connectors
from .decide import Decider
from .config import Settings
from .db import Database
from .llm.base import LLMClient
from .payments import PaymentProvider, make_provider
from .webclient import WebClient


def detect_backend(settings: Settings) -> tuple[str, str]:
    """Pick how to reach Claude with zero setup. Returns (backend, why).

    Order: explicit choice in models.yaml -> an API key or `ant` login profile -> the Claude Code CLI
    logged in on this machine -> the scripted dry-run model (nothing is spent)."""
    if settings.dry_run:
        return "dry_run", "started with --dry-run"
    choice = str(settings.models.get("backend", "auto")).lower()
    if choice in ("api", "claude_cli", "dry_run"):
        return choice, "set in config/models.yaml"
    pref = (settings.secret("MODEL_BACKEND") or "auto").lower()  # the owner's choice on the Connect page
    if pref == "claude_token" and settings.secret("CLAUDE_CODE_OAUTH_TOKEN"):
        from .llm.claude_cli import find_claude
        if find_claude():
            return "claude_cli", "Claude token (your subscription), chosen on the Connect page"
    if pref == "api_key" and settings.secret("ANTHROPIC_API_KEY"):
        return "api", "Anthropic API key, chosen on the Connect page"
    if settings.secret("ANTHROPIC_API_KEY") or settings.secret("ANTHROPIC_AUTH_TOKEN"):
        return "api", "ANTHROPIC_API_KEY found"
    if (Path.home() / ".config" / "anthropic").is_dir() and shutil.which("ant"):
        return "api", "ant login profile found"
    from .llm.claude_cli import find_claude
    binary = find_claude()
    if binary:
        try:
            out = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, timeout=20, text=True)
            if json.loads(out.stdout or "{}").get("loggedIn"):
                return "claude_cli", "using the Claude login on this machine (claude CLI)"
        except Exception:
            pass
        return "dry_run", "claude CLI found but not logged in: run `claude` once to log in"
    return "dry_run", "no Claude access found: install Claude Code and log in, or add an API key"


def make_llm(settings: Settings, backend: str | None = None) -> LLMClient:
    backend = backend or detect_backend(settings)[0]
    if backend == "dry_run":
        from .llm.dry_run import DryRunClient
        return DryRunClient(settings)
    if backend == "claude_cli":
        from .llm.claude_cli import ClaudeCLIClient
        return ClaudeCLIClient(settings)
    from .llm.anthropic_client import AnthropicClient
    return AnthropicClient(settings)


# Grants added to roles after agents of that role may already exist: (version, grant, roles).
GRANT_UPGRADES = [
    ("0005", "approve_purchases", ("platform_cfo",)),
    ("0005", "browser", ("ceo", "marketer", "coder", "designer", "product_engineer", "customer_success")),
    ("0007", "files", ("platform_cfo", "platform_coo")),
    ("0007", "ideas", ("platform_cfo",)),
    ("0007", "crm", ("researcher",)),
]


def upgrade_grants(db: Database) -> int:
    """Give existing agents the grants their role gained in a later version. Runs once per upgrade."""
    from .engine import get_setting, set_setting
    from .util import dumps, loads
    n = 0
    for version, grant, roles in GRANT_UPGRADES:
        key = f"grants_upgrade:{version}:{grant}"
        if get_setting(db, key):
            continue
        marks = ",".join("?" * len(roles))
        for a in db.all(f"SELECT id, grants_json FROM agents WHERE role IN ({marks})", roles):
            g = loads(a["grants_json"], []) or []
            if grant not in g:
                db.execute("UPDATE agents SET grants_json = ? WHERE id = ?", (dumps(g + [grant]), a["id"]))
                n += 1
        set_setting(db, key, "1")
    return n


# Roles whose prompt changed after agents of that role were created: (version, role). Their base prompt is
# replaced once; any per-org focus they were given is kept.
PROMPT_UPGRADES = [("0009", "red_team"), ("0009", "venture_analyst"), ("0009", "ceo"), ("0009", "marketer"),
                   ("0009", "platform_cfo"), ("0010", "platform_ceo")]


def upgrade_prompts(db: Database, settings) -> int:
    from .engine import get_setting, set_setting
    n = 0
    for version, role in PROMPT_UPGRADES:
        key = f"prompt_upgrade:{version}:{role}"
        if get_setting(db, key) or role not in settings.roles:
            continue
        base = settings.role(role)["prompt"].strip()
        for a in db.all("SELECT id, system_prompt FROM agents WHERE role = ?", (role,)):
            focus = a["system_prompt"].split("\n\nYour specific focus in this org:", 1)
            db.execute("UPDATE agents SET system_prompt = ? WHERE id = ?",
                       (base + ("\n\nYour specific focus in this org:" + focus[1] if len(focus) > 1 else ""), a["id"]))
            n += 1
        set_setting(db, key, "1")
    return n


class Core:
    def __init__(self, settings: Settings, *, db: Database | None = None, llm: LLMClient | None = None,
                 payments: PaymentProvider | None = None, cards: CardProvider | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        if llm is None:
            self.backend, self.backend_why = detect_backend(settings)
            llm = make_llm(settings, self.backend)
        else:
            self.backend, self.backend_why = llm.name, "provided"
        self.llm = llm
        self.payments = payments or make_provider(settings)
        self.cards = cards or make_cards(settings)
        self.browser = Browser(settings)
        self.connectors = Connectors.from_settings(settings)
        self.approver = Approver(settings, self.llm)
        self.web = WebClient(settings, self.llm)
        self.decider = Decider(settings, self.llm)
        self.approver.decider = self.decider

    def reload_connectors(self) -> None:
        """After the owner connects accounts on the Connect page."""
        self.connectors = Connectors.from_settings(self.settings)
        self.cards = make_cards(self.settings)
        if self.llm.name != "dry-run" or not self.settings.dry_run:
            backend, why = detect_backend(self.settings)
            if backend != self.backend:
                self.backend, self.backend_why = backend, why
                self.llm = make_llm(self.settings, backend)
                self.approver = Approver(self.settings, self.llm)
                self.web = WebClient(self.settings, self.llm)
                self.decider = Decider(self.settings, self.llm)
                self.approver.decider = self.decider

    def init(self) -> "Core":
        self.db.migrate()
        upgrade_grants(self.db)
        upgrade_prompts(self.db, self.settings)
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def aclose(self) -> None:
        await self.web.aclose()
        await self.browser.aclose()
