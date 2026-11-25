"""Deliver committed password-reset requests with bounded retry."""
import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .db import Session, engine
from .models import AuditEvent, EmailVerification, PasswordReset, ResetDelivery, User
from .password_reset import send_reset_message
from .email_verification import send_verification_message
from . import mail_transport

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


async def deliver_one(factory=None, sender=None):
    async with (factory or Session)() as db:
        delivery = await db.scalar(select(ResetDelivery).where(ResetDelivery.status == "pending", ResetDelivery.next_attempt_at <= time.time()).order_by(ResetDelivery.created_at, ResetDelivery.reset_id).limit(1).with_for_update(skip_locked=True))
        if delivery is None:
            return False
        reset = await db.get(PasswordReset, delivery.reset_id)
        user = await db.get(User, reset.user_id) if reset else None
        now = time.time()
        if not reset or reset.used_at is not None or reset.expires_at <= now or not user or not user.active:
            delivery.status = "discarded"
            delivery.completed_at = now
        else:
            delivery.attempts += 1
            try:
                await asyncio.to_thread(sender or send_reset_message, reset.id, user.email)
            except Exception:
                delivery.error_code = "mail_unavailable"
                delivery.next_attempt_at = time.time() + min(120, 2 ** min(delivery.attempts, 7))
                if delivery.attempts >= MAX_ATTEMPTS:
                    delivery.status = "failed"
                    delivery.completed_at = time.time()
                    db.add(AuditEvent(actor_id=user.id, action="password_reset_delivery", resource="account", outcome="failed"))
            else:
                delivery.status = "accepted"
                delivery.completed_at = time.time()
                delivery.error_code = None
                db.add(AuditEvent(actor_id=user.id, action="password_reset_delivery", resource="account", outcome="success"))
        await db.commit()
        return True


async def deliver_verification_one(factory=None, sender=None):
    async with (factory or Session)() as db:
        delivery = await db.scalar(select(EmailVerification).where(EmailVerification.delivery_status == "pending", EmailVerification.next_attempt_at <= time.time()).order_by(EmailVerification.created_at, EmailVerification.id).limit(1).with_for_update(skip_locked=True))
        if delivery is None:
            return False
        user = await db.get(User, delivery.user_id)
        now = time.time()
        if delivery.used_at is not None or delivery.expires_at <= now or not user or not user.active or user.email_verified_at is not None:
            delivery.delivery_status, delivery.completed_at = "discarded", now
        else:
            delivery.attempts += 1
            try:
                await asyncio.to_thread(sender or send_verification_message, delivery.id, user.email)
            except Exception:
                delivery.error_code = "mail_unavailable"
                delivery.next_attempt_at = time.time() + min(120, 2 ** min(delivery.attempts, 7))
                if delivery.attempts >= MAX_ATTEMPTS:
                    delivery.delivery_status, delivery.completed_at = "failed", time.time()
                    db.add(AuditEvent(actor_id=user.id, action="email_verification_delivery", resource="account", outcome="failed"))
            else:
                delivery.delivery_status, delivery.completed_at, delivery.error_code = "accepted", time.time(), None
                db.add(AuditEvent(actor_id=user.id, action="email_verification_delivery", resource="account", outcome="success"))
        await db.commit()
        return True


async def run():
    # Reject invalid configuration before consuming queued mail.
    mail_transport.config()
    try:
        while True:
            try:
                reset_sent = await deliver_one()
                verification_sent = await deliver_verification_one()
                if not reset_sent and not verification_sent:
                    await asyncio.sleep(1)
            except SQLAlchemyError:
                log.warning("Reset delivery database unavailable; retrying")
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
