"""The service container: settings, database, model client, web, payments, approver."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .approver import Approver
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


class Core:
    def __init__(self, settings: Settings, *, db: Database | None = None, llm: LLMClient | None = None,
                 payments: PaymentProvider | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        if llm is None:
            self.backend, self.backend_why = detect_backend(settings)
            llm = make_llm(settings, self.backend)
        else:
            self.backend, self.backend_why = llm.name, "provided"
        self.llm = llm
        self.payments = payments or make_provider(settings)
        self.approver = Approver(settings, self.llm)
        self.web = WebClient(settings, self.llm)

    def init(self) -> "Core":
        self.db.migrate()
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def aclose(self) -> None:
        await self.web.aclose()
