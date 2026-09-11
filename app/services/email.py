"""Branded HTML email over a configurable SMTP provider.

Works unchanged with Gmail SMTP, SendGrid (`apikey` / API key), Resend
(`resend` / `re_…`) or Mailtrap — it is only ever SMTP host + credentials.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.email")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def naira(kobo: int) -> str:
    return "₦{:,.0f}".format((kobo or 0) / 100)


_env.filters["naira"] = naira


@dataclass(slots=True)
class EmailResult:
    success: bool
    provider: str
    message_id: str | None = None
    error: str | None = None


@dataclass(slots=True)
class InlineImage:
    cid: str
    content: bytes
    subtype: str = "png"
    filename: str = "image.png"


def render_template(template_name: str, **context) -> str:
    context.setdefault("company", {
        "name": settings.COMPANY_NAME,
        "email": settings.COMPANY_EMAIL,
        "phone": settings.COMPANY_PHONE,
        "whatsapp": settings.COMPANY_WHATSAPP,
        "social": settings.COMPANY_SOCIAL,
        "web_url": settings.WEB_BASE_URL,
    })
    return _env.get_template(template_name).render(**context)


def _build_message(
    to: str, subject: str, html: str, text: str | None, images: list[InlineImage]
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.EMAIL_FROM_NAME, settings.EMAIL_FROM))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain="ecojindu.ng")
    msg.set_content(text or "This email needs an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")

    if images:
        html_part = msg.get_payload()[-1]
        for img in images:
            html_part.add_related(
                img.content, maintype="image", subtype=img.subtype, cid=f"<{img.cid}>",
                filename=img.filename,
            )
    return msg


def _send_smtp_blocking(msg: EmailMessage) -> EmailResult:
    try:
        if settings.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
            if settings.SMTP_USE_TLS:
                server.starttls()
        with server:
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
        return EmailResult(True, "smtp", message_id=msg["Message-ID"])
    except Exception as exc:  # noqa: BLE001 - a mail outage must not fail a booking
        return EmailResult(False, "smtp", error=str(exc)[:400])


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    images: list[InlineImage] | None = None,
) -> EmailResult:
    if not settings.EMAIL_ENABLED:
        return EmailResult(False, "disabled", error="EMAIL_ENABLED=false")
    if not to:
        return EmailResult(False, settings.EMAIL_PROVIDER, error="No recipient address")

    msg = _build_message(to, subject, html, text, images or [])

    if settings.EMAIL_PROVIDER == "console":
        log_event(
            logger,
            logging.INFO,
            "Email (console)",
            to=to,
            subject=subject,
            html_bytes=len(html),
            inline_images=len(images or []),
        )
        return EmailResult(True, "console", message_id=msg["Message-ID"])

    # smtplib is blocking — keep the event loop free.
    return await asyncio.to_thread(_send_smtp_blocking, msg)
