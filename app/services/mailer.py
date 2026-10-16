"""Envoi des emails de la file `email_outbox` via un fournisseur : Postmark, Brevo ou SMTP.

- `EMAIL_PROVIDER=outbox` (défaut) : rien n'est envoyé, les emails restent consultables (développement).
- Emails marketing : en-têtes List-Unsubscribe + désabonnement en un clic (RFC 8058), flux « broadcast » chez Postmark.
- Réessais avec délai croissant (5 tentatives), erreurs conservées ; aucun secret journalisé.
- Le consentement est revérifié au moment de l'envoi : un client désabonné entre-temps ne reçoit rien.
"""
import logging
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import or_, select

from ..config import settings
from ..models import Customer, EmailOutbox, Shop, now

log = logging.getLogger("shopforge.mailer")
MAX_ATTEMPTS = 5


class MailError(Exception):
    def __init__(self, msg, retryable=True):
        super().__init__(msg)
        self.retryable = retryable


def _from(shop: Shop) -> str:
    if not settings.email_from:
        raise MailError("EMAIL_FROM non configuré (adresse d'un domaine vérifié chez le fournisseur)", retryable=False)
    return f"{shop.name} <{settings.email_from}>" if "<" not in settings.email_from else settings.email_from


def _reply_to(shop: Shop) -> str | None:
    return (shop.legal or {}).get("email") or None


class PostmarkProvider:
    name = "postmark"

    def __init__(self, token: str, transport=None):
        if not token:
            raise MailError("POSTMARK_SERVER_TOKEN manquant", retryable=False)
        self.http = httpx.Client(base_url="https://api.postmarkapp.com", timeout=15, transport=transport,
                                 headers={"X-Postmark-Server-Token": token, "Accept": "application/json"})

    def send(self, shop, mail: EmailOutbox) -> str:
        payload = {"From": _from(shop), "To": mail.to, "Subject": mail.subject, "TextBody": mail.body,
                   "MessageStream": "broadcast" if mail.kind == "marketing" else "outbound",
                   "Headers": [{"Name": k, "Value": v} for k, v in (mail.headers or {}).items()]}
        if _reply_to(shop):
            payload["ReplyTo"] = _reply_to(shop)
        r = self.http.post("/email", json=payload)
        if r.status_code == 200 and r.json().get("ErrorCode") == 0:
            return r.json()["MessageID"]
        raise MailError(f"Postmark {r.status_code} : {r.text[:300]}", retryable=r.status_code >= 500 or r.status_code == 429)


class BrevoProvider:
    name = "brevo"

    def __init__(self, api_key: str, transport=None):
        if not api_key:
            raise MailError("BREVO_API_KEY manquant", retryable=False)
        self.http = httpx.Client(base_url="https://api.brevo.com/v3", timeout=15, transport=transport,
                                 headers={"api-key": api_key, "Accept": "application/json"})

    def send(self, shop, mail: EmailOutbox) -> str:
        frm = _from(shop)
        name, addr = (frm.split("<")[0].strip(), frm.split("<")[1].rstrip(">")) if "<" in frm else ("", frm)
        payload = {"sender": {"email": addr, **({"name": name} if name else {})}, "to": [{"email": mail.to}],
                   "subject": mail.subject, "textContent": mail.body, "headers": mail.headers or {}}
        if _reply_to(shop):
            payload["replyTo"] = {"email": _reply_to(shop)}
        r = self.http.post("/smtp/email", json=payload)
        if r.status_code in (200, 201):
            return r.json().get("messageId", "")
        raise MailError(f"Brevo {r.status_code} : {r.text[:300]}", retryable=r.status_code >= 500 or r.status_code == 429)


class SmtpProvider:
    name = "smtp"

    def __init__(self, url: str):
        u = urlparse(url)
        if u.scheme not in ("smtp", "smtps") or not u.hostname:
            raise MailError("SMTP_URL invalide (smtp://user:pass@hote:587 ou smtps://…:465)", retryable=False)
        self.u = u

    def send(self, shop, mail: EmailOutbox) -> str:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = _from(shop), mail.to, mail.subject
        if _reply_to(shop):
            msg["Reply-To"] = _reply_to(shop)
        for k, v in (mail.headers or {}).items():
            msg[k] = v
        msg.set_content(mail.body)
        u = self.u
        try:
            if u.scheme == "smtps":
                server = smtplib.SMTP_SSL(u.hostname, u.port or 465, context=ssl.create_default_context(), timeout=20)
            else:
                server = smtplib.SMTP(u.hostname, u.port or 587, timeout=20)
                server.starttls(context=ssl.create_default_context())
            with server:
                if u.username:
                    server.login(unquote(u.username), unquote(u.password or ""))
                server.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"Destinataire refusé : {exc}", retryable=False) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"SMTP : {type(exc).__name__}") from exc
        return msg.get("Message-ID", "")


def provider(transport=None):
    p = settings.email_provider
    if p == "postmark":
        return PostmarkProvider(settings.postmark_token, transport)
    if p == "brevo":
        return BrevoProvider(settings.brevo_api_key, transport)
    if p == "smtp":
        return SmtpProvider(settings.smtp_url)
    return None  # outbox : pas d'envoi réel


def _still_consenting(db, mail: EmailOutbox) -> bool:
    if mail.kind != "marketing":
        return True
    c = db.scalar(select(Customer).where(Customer.shop_id == mail.shop_id, Customer.email == mail.to))
    return bool(c and c.marketing_consent and not c.unsubscribed_at)


def send_pending(db, limit: int = 50, transport=None) -> dict:
    prov = provider(transport)
    stats = {"sent": 0, "failed": 0, "held": 0, "provider": settings.email_provider}
    if prov is None:
        return stats
    q = (select(EmailOutbox).where(EmailOutbox.status == "queued",
                                   or_(EmailOutbox.next_attempt_at.is_(None), EmailOutbox.next_attempt_at <= now()))
         .order_by(EmailOutbox.id).limit(limit))
    for mail in db.scalars(q).all():
        shop = db.get(Shop, mail.shop_id)
        if not _still_consenting(db, mail):
            mail.status, mail.error = "held", "Consentement retiré avant l'envoi"
            stats["held"] += 1
            continue
        mail.attempts = (mail.attempts or 0) + 1
        try:
            mail.provider_id = prov.send(shop, mail)
            mail.status, mail.sent_at, mail.provider, mail.error = "sent", now(), prov.name, None
            stats["sent"] += 1
        except MailError as exc:
            mail.error, mail.provider = str(exc)[:1000], prov.name
            if not exc.retryable or mail.attempts >= MAX_ATTEMPTS:
                mail.status = "failed"
                stats["failed"] += 1
            else:
                mail.next_attempt_at = now() + timedelta(minutes=2 ** mail.attempts)
            log.warning("email not sent", extra={"shop_id": mail.shop_id, "kind": mail.kind})
    db.commit()
    return stats
