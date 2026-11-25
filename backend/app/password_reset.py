"""Password-reset token construction and configured mail delivery."""
import asyncio
import hashlib
import hmac
import os
import re
import time
from email.message import EmailMessage

from .db import Session
from .models import PasswordReset, User
from . import mail_transport


def reset_token(reset_id: str) -> str:
    secret = os.getenv("JWT_SECRET", "local-development-secret-change-me").encode()
    signature = hmac.new(secret, ("mediagent-password-reset:" + reset_id).encode(), hashlib.sha256).hexdigest()
    return f"{reset_id}.{signature}"


def valid_token(token: str) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[0-9a-f]{64}", token):
        return None
    reset_id, separator, _ = token.partition(".")
    if not separator or len(reset_id) != 36 or not hmac.compare_digest(token, reset_token(reset_id)):
        return None
    return reset_id


def send_reset_message(reset_id: str, email: str) -> None:
    settings = mail_transport.config()
    if settings.transport == "disabled":
        raise RuntimeError("Mail transport is not configured")
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = email
    message["Subject"] = "Reset your MediAgent password"
    message["Message-ID"] = f"<reset-{reset_id}@mediagent.local>"
    base = settings.public_url
    message.set_content(f"Use this link within 15 minutes to reset your password:\n\n{base}/reset#token={reset_token(reset_id)}\n\nIf you did not request this, ignore this message.")
    mail_transport.send(message, settings)


async def deliver_reset(reset_id: str) -> None:
    # Compatibility for messages queued by an older worker before the upgrade.
    if os.getenv("RESET_MAIL_HOST") != "mailpit":
        return
    async with Session() as db:
        reset = await db.get(PasswordReset, reset_id)
        if not reset or reset.used_at is not None or reset.expires_at <= time.time():
            return
        user = await db.get(User, reset.user_id)
        if not user or not user.active:
            return
        await asyncio.to_thread(send_reset_message, reset.id, user.email)
