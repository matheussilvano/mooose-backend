import os
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from models import AnonymousSession, Essay, User


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


FREE_CORRECTIONS_LIMIT = _get_int_env("FREE_CORRECTIONS_LIMIT", 1)
ANON_IP_SOFT_LIMIT = _get_int_env("ANON_IP_SOFT_LIMIT", 5)
ANON_IP_WINDOW_SECONDS = _get_int_env("ANON_IP_WINDOW_SECONDS", 3600)


def get_or_create_anon_session(
    db: Session,
    anon_id: str,
    ip: Optional[str],
    device_id: Optional[str],
) -> AnonymousSession:
    session = (
        db.query(AnonymousSession)
        .filter(AnonymousSession.anon_id == anon_id)
        .first()
    )
    if session is None:
        session = AnonymousSession(
            anon_id=anon_id,
            free_used=0,
            last_ip=ip,
            device_id=device_id,
        )
        db.add(session)
        db.flush()
    else:
        session.last_ip = ip or session.last_ip
        session.device_id = device_id or session.device_id
        session.updated_at = datetime.utcnow()
    return session


def effective_free_used(
    user: Optional[User],
    anon_session: Optional[AnonymousSession],
) -> int:
    user_used = user.free_used if user and user.free_used is not None else 0
    anon_used = anon_session.free_used if anon_session and anon_session.free_used is not None else 0
    return max(user_used, anon_used)


def free_remaining(free_used_value: int) -> int:
    remaining = FREE_CORRECTIONS_LIMIT - free_used_value
    return remaining if remaining > 0 else 0


def consume_free(
    *,
    user: Optional[User],
    anon_session: Optional[AnonymousSession],
    effective_used: int,
) -> int:
    new_used = effective_used + 1
    if user is not None:
        user.free_used = max(user.free_used or 0, new_used)
    if anon_session is not None:
        anon_session.free_used = max(anon_session.free_used or 0, new_used)
    return new_used


def merge_anon_to_user(
    db: Session,
    user: User,
    anon_session: AnonymousSession,
) -> Tuple[int, int]:
    new_used = max(user.free_used or 0, anon_session.free_used or 0)
    user.free_used = new_used
    anon_session.linked_user_id = user.id
    anon_session.linked_at = datetime.utcnow()

    migrated = (
        db.query(Essay)
        .filter(Essay.anon_id == anon_session.anon_id, Essay.user_id.is_(None))
        .update({Essay.user_id: user.id}, synchronize_session=False)
    )
    return new_used, migrated
