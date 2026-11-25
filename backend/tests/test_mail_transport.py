import ssl
from email.message import EmailMessage

import pytest

from app import email_verification, mail_transport, password_reset


@pytest.fixture
def smtp_config(monkeypatch):
    for key, value in {
        'MAIL_TRANSPORT':'smtp', 'SMTP_HOST':'smtp.example.test', 'SMTP_PORT':'587',
        'SMTP_SECURITY':'starttls', 'SMTP_USERNAME':'synthetic-user', 'SMTP_PASSWORD':'synthetic-secret',
        'MAIL_FROM':'noreply@example.test', 'APP_PUBLIC_URL':'https://app.example.test',
    }.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_local_default_and_external_requires_complete_tls_config(monkeypatch, smtp_config):
    assert 'synthetic-secret' not in repr(mail_transport.config())
    for key, value in [('SMTP_SECURITY','none'), ('SMTP_PASSWORD',''), ('APP_PUBLIC_URL','http://app.example.test'), ('APP_PUBLIC_URL','https://user:pass@app.example.test'), ('MAIL_FROM','bad\naddress@example.test'), ('SMTP_PORT','0')]:
        with monkeypatch.context() as local:
            local.setenv(key, value)
            with pytest.raises(ValueError):
                mail_transport.config()
    monkeypatch.delenv('MAIL_TRANSPORT')
    monkeypatch.setenv('RESET_MAIL_HOST', 'mailpit')
    monkeypatch.setenv('APP_PUBLIC_URL','http://localhost:3100')
    local=mail_transport.config()
    assert (local.transport, local.host, local.port) == ('local','mailpit',1025)
    monkeypatch.setenv('APP_PUBLIC_URL','http://127.0.0.1:3200')
    assert mail_transport.config().public_url == 'http://127.0.0.1:3200'
    monkeypatch.setenv('APP_PUBLIC_URL','https://external.example.test')
    with pytest.raises(ValueError): mail_transport.config()


def test_starttls_authenticates_after_verified_tls_and_sends_links(smtp_config, monkeypatch):
    calls=[]
    class FakeSMTP:
        def __init__(self, host, port, **kwargs): calls.append(('connect',host,port,kwargs))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): calls.append(('ehlo',))
        def starttls(self, *, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            calls.append(('tls',))
        def login(self, username, password): calls.append(('login',username,password))
        def send_message(self, message): calls.append(('send',message)); return {}
    monkeypatch.setattr(mail_transport.smtplib,'SMTP',FakeSMTP)
    password_reset.send_reset_message('00000000-0000-4000-8000-000000000000','patient@example.test')
    assert [call[0] for call in calls] == ['connect','ehlo','tls','ehlo','login','send']
    message=calls[-1][1]
    assert message['From']=='noreply@example.test' and message['To']=='patient@example.test'
    assert 'https://app.example.test/reset#token=' in message.get_content()
    calls.clear()
    email_verification.send_verification_message('00000000-0000-4000-8000-000000000000','patient@example.test')
    assert 'https://app.example.test/verify-email#token=' in calls[-1][1].get_content()


def test_tls_failure_never_falls_back_or_sends(smtp_config, monkeypatch):
    calls=[]
    class FakeSMTP:
        def __init__(self,*args,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def ehlo(self): pass
        def starttls(self, **kwargs): raise ssl.SSLError('synthetic failure')
        def login(self,*args): calls.append('login')
        def send_message(self,*args): calls.append('send')
    monkeypatch.setattr(mail_transport.smtplib,'SMTP',FakeSMTP)
    with pytest.raises(ssl.SSLError):
        mail_transport.send(EmailMessage(),mail_transport.config())
    assert calls == []


def test_implicit_tls_uses_certificate_validation(smtp_config, monkeypatch):
    monkeypatch.setenv('SMTP_SECURITY','tls')
    seen=[]
    class FakeSMTP:
        def __init__(self, host, port, *, timeout, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            seen.append((host,port))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def login(self,*args): seen.append('login')
        def send_message(self,*args): seen.append('send'); return {}
    monkeypatch.setattr(mail_transport.smtplib,'SMTP_SSL',FakeSMTP)
    mail_transport.send(EmailMessage(),mail_transport.config())
    assert seen == [('smtp.example.test',587),'login','send']


def test_real_implicit_tls_to_synthetic_local_server(smtp_config, monkeypatch, tmp_path):
    import base64
    import socketserver
    import subprocess
    import threading

    certificate, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(certificate),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    messages=[]
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'220 synthetic ESMTP\r\n'); self.wfile.flush()
            while line := self.rfile.readline(8192):
                command=line.decode('ascii',errors='replace').strip()
                verb=command.split(' ',1)[0].upper()
                if verb in ('EHLO','HELO'):
                    self.wfile.write(b'250-synthetic\r\n250 AUTH PLAIN LOGIN\r\n')
                elif verb == 'AUTH':
                    assert base64.b64decode(command.split(' ',2)[2]).endswith(b'synthetic-user\x00synthetic-secret')
                    self.wfile.write(b'235 2.7.0 authenticated\r\n')
                elif verb in ('MAIL','RCPT'):
                    self.wfile.write(b'250 2.1.0 ok\r\n')
                elif verb == 'DATA':
                    self.wfile.write(b'354 send data\r\n'); self.wfile.flush()
                    body=[]
                    while (row := self.rfile.readline(65536)) != b'.\r\n':
                        if not row: return
                        body.append(row)
                    messages.append(b''.join(body))
                    self.wfile.write(b'250 2.0.0 queued\r\n')
                elif verb == 'QUIT':
                    self.wfile.write(b'221 goodbye\r\n'); self.wfile.flush(); return
                else:
                    self.wfile.write(b'500 unknown\r\n')
                self.wfile.flush()
    class Server(socketserver.TCPServer):
        allow_reuse_address=True
        def get_request(self):
            sock, addr=super().get_request()
            return context.wrap_socket(sock,server_side=True), addr
    server=Server(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    monkeypatch.setenv('SMTP_HOST','localhost')
    monkeypatch.setenv('SMTP_PORT',str(server.server_address[1]))
    monkeypatch.setenv('SMTP_SECURITY','tls')
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            password_reset.send_reset_message('00000000-0000-4000-8000-000000000000','synthetic@example.test')
        assert messages == []
        monkeypatch.setenv('SSL_CERT_FILE',str(certificate))
        monkeypatch.setenv('SMTP_HOST','127.0.0.1')
        with pytest.raises(ssl.SSLCertVerificationError):
            password_reset.send_reset_message('00000000-0000-4000-8000-000000000000','synthetic@example.test')
        assert messages == []
        monkeypatch.setenv('SMTP_HOST','localhost')
        password_reset.send_reset_message('00000000-0000-4000-8000-000000000000','synthetic@example.test')
        assert len(messages)==1 and b'https://app.example.test/reset#token=' in messages[0]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
