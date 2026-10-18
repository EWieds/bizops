"""External connectors. Each has a real adapter (used when its credentials exist) and a sandbox one."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from .billing import BillingProvider, make_billing
from .email import EmailProvider, make_email
from .voice import VoiceProvider, make_voice


@dataclass
class Connectors:
    email: EmailProvider
    billing: BillingProvider
    voice: VoiceProvider

    @classmethod
    def from_settings(cls, settings: Settings) -> "Connectors":
        return cls(email=make_email(settings), billing=make_billing(settings), voice=make_voice(settings))

    @property
    def sandbox(self) -> bool:
        """True while outreach or billing is simulated: revenue is then sandbox revenue."""
        return not (self.email.live and self.billing.live)

    def status(self) -> dict[str, str]:
        return {"email": self.email.name, "billing": self.billing.name, "voice": self.voice.name}
