"""Envio SMTP mínimo; em TESTING a mensagem fica apenas na memória do teste."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

from flask import current_app, url_for


class MailDeliveryError(RuntimeError):
    """Falha de configuração ou entrega; não inclui endereço, link ou segredo."""


def send_password_recovery(email: str, token: str, ttl_minutes: int) -> None:
    base = current_app.config.get("PESQUISAPDF_PUBLIC_URL") or (
        "http://localhost:5000" if current_app.testing else ""
    )
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise MailDeliveryError("URL pública de recuperação indisponível.")
    link = base.rstrip("/") + url_for("auth.reset_password", token=token)
    message = EmailMessage()
    message["Subject"] = "Recuperação de senha — Raspagem de Dados"
    message["To"] = email
    message["From"] = current_app.config.get("MAIL_FROM") or "Raspagem de Dados <no-reply@example.invalid>"
    message.set_content(
        "Foi solicitada a redefinição da senha da sua conta.\n\n"
        f"Abra este link para definir uma nova senha: {link}\n\n"
        f"O link expira em {ttl_minutes} minutos e pode ser usado uma única vez.\n"
        "Se você não fez a solicitação, ignore esta mensagem."
    )
    if current_app.testing:
        current_app.extensions.setdefault("password_recovery_outbox", []).append(message)
        return

    host = current_app.config.get("MAIL_SERVER")
    sender = current_app.config.get("MAIL_FROM")
    if not host or not sender:
        raise MailDeliveryError("Servidor de e-mail indisponível.")
    try:
        port = int(current_app.config.get("MAIL_PORT", 587))
        if not 1 <= port <= 65535:
            raise ValueError("porta inválida")
        use_ssl = bool(current_app.config.get("MAIL_USE_SSL", False))
        use_tls = bool(current_app.config.get("MAIL_USE_TLS", True))
        server = (smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context())
                  if use_ssl else smtplib.SMTP(host, port, timeout=10))
        with server as smtp:
            if use_tls and not use_ssl:
                smtp.starttls(context=ssl.create_default_context())
            username = current_app.config.get("MAIL_USERNAME")
            password = current_app.config.get("MAIL_PASSWORD")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as error:
        raise MailDeliveryError("O envio de recuperação não foi concluído.") from error
