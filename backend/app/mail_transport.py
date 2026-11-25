"""Explicitly configured mail transport; no addresses or credentials are logged."""
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
import os
import re
import smtplib
import ssl
from urllib.parse import urlsplit


@dataclass(frozen=True)
class MailConfig:
    transport: str
    host: str
    port: int
    security: str
    sender: str
    public_url: str
    username: str = field(default='', repr=False)
    password: str = field(default='', repr=False)


def config() -> MailConfig:
    transport = os.getenv('MAIL_TRANSPORT', '').strip().lower()
    if not transport:
        transport = 'local' if os.getenv('RESET_MAIL_HOST') == 'mailpit' else 'disabled'
    if transport == 'disabled':
        return MailConfig('disabled', '', 0, '', '', '')
    if transport == 'local':
        if os.getenv('RESET_MAIL_HOST') != 'mailpit':
            raise ValueError('Local mail requires the Mailpit service')
        base = os.getenv('APP_PUBLIC_URL', 'http://localhost:3100').strip().rstrip('/')
        try:
            parsed = urlsplit(base)
            _ = parsed.port
            if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1')
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
                raise ValueError
        except ValueError:
            raise ValueError('Local mail requires a loopback HTTP application URL') from None
        return MailConfig('local', 'mailpit', 1025, '', 'noreply@example.test', base)
    if transport != 'smtp':
        raise ValueError('Invalid mail transport')
    host = os.getenv('SMTP_HOST', '').strip()
    security = os.getenv('SMTP_SECURITY', '').strip().lower()
    username = os.getenv('SMTP_USERNAME', '')
    password = os.getenv('SMTP_PASSWORD', '')
    sender = os.getenv('MAIL_FROM', '').strip()
    base = os.getenv('APP_PUBLIC_URL', '').strip().rstrip('/')
    try:
        port = int(os.getenv('SMTP_PORT', ''))
        parsed = urlsplit(base)
        _ = parsed.port
        address = Address(addr_spec=sender)
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host) or not 1 <= port <= 65535
            or security not in ('tls', 'starttls') or not username or not password
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in username + password)
            or address.addr_spec != sender or not address.username or not address.domain
            or parsed.scheme != 'https' or not parsed.hostname or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', parsed.hostname) or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in base)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Invalid SMTP mail configuration') from None
    return MailConfig('smtp', host, port, security, sender, base, username, password)


def send(message: EmailMessage, settings: MailConfig) -> None:
    if settings.transport == 'disabled':
        raise RuntimeError('Mail transport is not configured')
    if settings.transport == 'local':
        with smtplib.SMTP(settings.host, settings.port, timeout=5) as smtp:
            if smtp.send_message(message):
                raise RuntimeError('Mail recipient rejected')
        return
    if settings.transport != 'smtp' or settings.security not in ('tls', 'starttls'):
        raise RuntimeError('Invalid mail transport')
    context = ssl.create_default_context()
    if settings.security == 'tls':
        smtp = smtplib.SMTP_SSL(settings.host, settings.port, timeout=10, context=context)
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=10)
    with smtp:
        if settings.security == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(settings.username, settings.password)
        if smtp.send_message(message):
            raise RuntimeError('Mail recipient rejected')
