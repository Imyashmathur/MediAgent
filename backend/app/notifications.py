"""Transactional in-app notification records; no external delivery."""
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Notification


def queue_notification(db: AsyncSession, *, recipient_id: str, patient_id: str, kind: str, event_key: str, resource_id: str, relationship_id: str | None = None, required_scope: str | None = None) -> None:
    db.add(Notification(recipient_id=recipient_id, patient_id=patient_id, kind=kind, event_key=event_key, resource_id=resource_id, relationship_id=relationship_id, required_scope=required_scope))
