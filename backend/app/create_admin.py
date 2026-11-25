"""Provision one local administrator interactively; never enable public admin registration."""
import asyncio
import getpass

from sqlalchemy import select

from .db import Session
from .main import hash_password
from .models import AuditEvent, User


async def create_admin(email: str, name: str, password: str):
    async with Session() as db:
        if await db.scalar(select(User).where(User.email == email)):
            raise ValueError("Email already registered")
        user = User(email=email, full_name=name, role="admin", password_hash=hash_password(password), verification_status="not_applicable")
        db.add(user)
        await db.flush()
        db.add(AuditEvent(actor_id=user.id, action="admin_provision", resource=f"user:{user.id}", outcome="success"))
        await db.commit()


def main():
    email = input("Administrator email: ").strip().lower()
    name = input("Administrator name: ").strip()
    password = getpass.getpass("Administrator password (at least 12 characters): ")
    if "@" not in email or not name or not 12 <= len(password) <= 256:
        raise SystemExit("Invalid email, name, or password length")
    asyncio.run(create_admin(email, name, password))
    print("Administrator created")


if __name__ == "__main__":
    main()
