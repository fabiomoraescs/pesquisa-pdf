"""Tokens efêmeros de recuperação; o valor enviado por e-mail nunca é persistido."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta, timezone

from sqlalchemy import delete, select, update

from .extensions import db
from .models import PasswordRecoveryToken, User, utcnow
from .services import access_is_active, record_audit


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue_token(user: User, ttl_seconds: int) -> str:
    """Cria um token aleatório; apenas seu digest SHA-256 entra no banco."""
    if ttl_seconds <= 0:
        raise ValueError("O prazo de recuperação deve ser positivo.")
    token = secrets.token_urlsafe(32)
    db.session.add(PasswordRecoveryToken(
        user_id=user.id,
        token_digest=_digest(token),
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    ))
    db.session.commit()
    return token


def revoke_token(token: str) -> None:
    db.session.execute(delete(PasswordRecoveryToken).where(PasswordRecoveryToken.token_digest == _digest(token)))
    db.session.commit()


def valid_token(token: str) -> tuple[PasswordRecoveryToken, User] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        return None
    record = db.session.scalar(select(PasswordRecoveryToken).where(
        PasswordRecoveryToken.token_digest == _digest(token),
    ))
    if record is None or record.consumed_at is not None:
        return None
    expiry = record.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= utcnow():
        return None
    user = db.session.get(User, record.user_id)
    if user is None or not access_is_active(user):
        return None
    return record, user


def consume_token(token: str, new_password: str) -> bool:
    pair = valid_token(token)
    if pair is None:
        return False
    record, user = pair
    now = utcnow()
    claimed = db.session.execute(update(PasswordRecoveryToken).where(
        PasswordRecoveryToken.id == record.id,
        PasswordRecoveryToken.consumed_at.is_(None),
        PasswordRecoveryToken.expires_at > now,
    ).values(consumed_at=now).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.session.rollback()
        return False
    previously_required = user.must_change_password
    user.set_password(new_password)
    user.must_change_password = False
    db.session.execute(update(PasswordRecoveryToken).where(
        PasswordRecoveryToken.user_id == user.id,
        PasswordRecoveryToken.consumed_at.is_(None),
    ).values(consumed_at=now))
    record_audit(None, "password_recovered", "user", user.id,
                 {"must_change_password": previously_required}, {"must_change_password": False})
    db.session.commit()
    return True
