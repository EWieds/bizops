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
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def aclose(self) -> None:
        await self.web.aclose()
        await self.browser.aclose()
