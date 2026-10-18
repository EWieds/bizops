"""The service container: settings, database, model client, web, payments, approver."""

from __future__ import annotations

from .approver import Approver
from .config import Settings
from .db import Database
from .llm.base import LLMClient
from .payments import PaymentProvider, make_provider
from .webclient import WebClient


def make_llm(settings: Settings) -> LLMClient:
    if settings.dry_run:
        from .llm.dry_run import DryRunClient
        return DryRunClient(settings)
    from .llm.anthropic_client import AnthropicClient
    return AnthropicClient(settings)


class Core:
    def __init__(self, settings: Settings, *, db: Database | None = None, llm: LLMClient | None = None,
                 payments: PaymentProvider | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.llm = llm or make_llm(settings)
        self.payments = payments or make_provider(settings)
        self.approver = Approver(settings, self.llm)
        self.web = WebClient(settings, self.llm)

    def init(self) -> "Core":
        self.db.migrate()
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def aclose(self) -> None:
        await self.web.aclose()
