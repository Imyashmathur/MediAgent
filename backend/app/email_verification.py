"""Email-ownership verification; never logs addresses or tokens."""
import hashlib
import hmac
import os
import re
from email.message import EmailMessage

from . import mail_transport


def verification_token(verification_id: str) -> str:
    secret = os.getenv('JWT_SECRET', 'local-development-secret-change-me').encode()
    signature = hmac.new(secret, ('mediagent-email-verification:' + verification_id).encode(), hashlib.sha256).hexdigest()
    return f'{verification_id}.{signature}'


def valid_token(token: str) -> str | None:
    if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[0-9a-f]{64}', token):
        return None
    verification_id = token.split('.', 1)[0]
    return verification_id if hmac.compare_digest(token, verification_token(verification_id)) else None


def send_verification_message(verification_id: str, email: str) -> None:
    settings = mail_transport.config()
    if settings.transport == 'disabled':
        raise RuntimeError('Mail transport is not configured')
    message = EmailMessage()
    message['From'] = settings.sender
    message['To'] = email
    message['Subject'] = 'Verify your MediAgent email address'
    message['Message-ID'] = f'<verify-{verification_id}@mediagent.local>'
    base = settings.public_url
    message.set_content(f'Confirm this email address within 15 minutes:\n\n{base}/verify-email#token={verification_token(verification_id)}\n\nIf you did not request this, ignore this message.')
    mail_transport.send(message, settings)
