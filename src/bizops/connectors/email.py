"""Email: send (SMTP, any provider) and receive (IMAP), or the sandbox outbox.

Real mode needs SMTP_HOST/SMTP_USER/SMTP_PASSWORD (+ IMAP_* to read replies) and the sender identity
(SENDER_NAME, SENDER_ADDRESS = a physical mailing address, required by CAN-SPAM). Everything else
(unsubscribe links, suppression, rate limits) is enforced in tools/outreach.py.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from ..config import Settings
from ..util import new_id


@dataclass
class Inbound:
    from_addr: str
    to_addr: str
    subject: str
    body: str
    provider_id: str


class EmailProvider(Protocol):
    name: str
    live: bool

    async def send(self, *, from_addr: str, to_addr: str, subject: str, body: str, reply_to: str | None = None,
                   headers: dict[str, str] | None = None) -> str: ...

    async def fetch_inbound(self) -> list[Inbound]: ...


class SandboxEmail:
    """Records mail without sending it. The simulated market (simworld.py) answers sandbox leads."""

    name = "sandbox"
    live = False

    async def send(self, *, from_addr: str, to_addr: str, subject: str, body: str, reply_to: str | None = None,
                   headers: dict[str, str] | None = None) -> str:
        return new_id("sbxmail")

    async def fetch_inbound(self) -> list[Inbound]:
        return []


def smtp_login(smtp: smtplib.SMTP, user: str, password: str) -> None:
    """Log in with AUTH LOGIN when offered. Gmail drops some cloud servers' connections on AUTH PLAIN (smtplib's
    default) instead of answering, while AUTH LOGIN works and gives proper errors."""
    smtp.ehlo_or_helo_if_needed()
    if "login" in (smtp.esmtp_features.get("auth") or "").lower().split():
        smtp.user, smtp.password = user, password
        smtp.auth("LOGIN", smtp.auth_login, initial_response_ok=False)
    else:
        smtp.login(user, password)


class SMTPEmail:
    name = "smtp"
    live = True

    def __init__(self, settings: Settings):
        s = settings.secret
        self.host, self.port = s("SMTP_HOST"), int(s("SMTP_PORT") or 587)
        self.user, self.password = s("SMTP_USER"), s("SMTP_PASSWORD")
        self.imap_host, self.imap_port = s("IMAP_HOST"), int(s("IMAP_PORT") or 993)
        self.imap_user = s("IMAP_USER") or self.user
        self.imap_password = s("IMAP_PASSWORD") or self.password

    def _send_sync(self, msg: EmailMessage) -> None:
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as smtp:
                smtp_login(smtp, self.user, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
                if self.user:
                    smtp_login(smtp, self.user, self.password)
                smtp.send_message(msg)

    async def send(self, *, from_addr: str, to_addr: str, subject: str, body: str, reply_to: str | None = None,
                   headers: dict[str, str] | None = None) -> str:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = from_addr, to_addr, subject
        msg["Message-ID"] = email.utils.make_msgid(domain=(from_addr.split("@")[-1].strip("> ") or "bizops.local"))
        msg["Date"] = email.utils.formatdate(localtime=True)
        if reply_to:
            msg["Reply-To"] = reply_to
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(body)
        await asyncio.to_thread(self._send_sync, msg)
        return msg["Message-ID"]

    def _fetch_sync(self) -> list[Inbound]:
        if not self.imap_host:
            return []
        out: list[Inbound] = []
        with imaplib.IMAP4_SSL(self.imap_host, self.imap_port) as imap:
            imap.login(self.imap_user, self.imap_password)
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else [])[:50]:
                _, parts = imap.fetch(num, "(RFC822)")
                raw = parts[0][1] if parts and parts[0] else b""
                m = email.message_from_bytes(raw)
                body = ""
                if m.is_multipart():
                    for part in m.walk():
                        if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                            break
                else:
                    body = m.get_payload(decode=True).decode(m.get_content_charset() or "utf-8", "replace")
                out.append(Inbound(from_addr=email.utils.parseaddr(m.get("From", ""))[1].lower(),
                                   to_addr=email.utils.parseaddr(m.get("To", ""))[1].lower(),
                                   subject=m.get("Subject", ""), body=body[:20000],
                                   provider_id=m.get("Message-ID") or new_id("imap")))
                imap.store(num, "+FLAGS", "\\Seen")
        return out

    async def fetch_inbound(self) -> list[Inbound]:
        return await asyncio.to_thread(self._fetch_sync)


def make_email(settings: Settings) -> EmailProvider:
    if settings.secret("SMTP_HOST") and settings.secret("SENDER_ADDRESS"):
        return SMTPEmail(settings)
    return SandboxEmail()
