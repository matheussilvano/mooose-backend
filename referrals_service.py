import json
import logging
import os
import secrets
import string
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Essay, Referral, User

logger = logging.getLogger(__name__)

def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


REFERRAL_REWARD_CREDITS = _get_int_env("REFERRAL_REWARD_CREDITS", 2)
REFERRAL_CODE_LENGTH = _get_int_env("REFERRAL_CODE_LENGTH", 10)
REFERRAL_CODE_LENGTH = max(8, min(12, REFERRAL_CODE_LENGTH))
_REFERRAL_ALPHABET = string.ascii_uppercase + string.digits


def normalize_referral_code(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    normalized = "".join(ch for ch in code.strip().upper() if ch.isalnum())
    return normalized or None


def generate_referral_code(db: Session) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_REFERRAL_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
        exists = db.query(User.id).filter(User.referral_code == code).first()
        if not exists:
            return code
    raise RuntimeError("Nao foi possivel gerar referral_code unico.")


def apply_referral_on_signup(
    db: Session,
    new_user: User,
    ref_code: Optional[str],
    signup_ip: Optional[str],
    device_fingerprint: Optional[str],
) -> Optional[Referral]:
    code = normalize_referral_code(ref_code)
    if not code:
        return None

    referrer = db.query(User).filter(User.referral_code == code).first()
    if not referrer:
        return None

    if referrer.id == new_user.id:
        logger.info(
            "referral_rejected reason=self_referral referrer_id=%s referred_id=%s",
            referrer.id,
            new_user.id,
        )
        return None

    existing = db.query(Referral).filter(Referral.referred_id == new_user.id).first()
    if existing:
        return existing

    new_user.referred_by = referrer.id

    metadata = {
        "signup_ip": signup_ip,
        "device_fingerprint": device_fingerprint,
        "ref_code": code,
    }
    referral = Referral(
        referrer_id=referrer.id,
        referred_id=new_user.id,
        status="pending",
        metadata_json=metadata,
    )
    db.add(referral)
    logger.info(
        "referral_created referrer_id=%s referred_id=%s",
        referrer.id,
        new_user.id,
    )
    return referral


def attempt_referral_activation(
    db: Session,
    referred_user_id: int,
    trigger: str,
    request_ip: Optional[str] = None,
) -> Dict[str, Any]:
    with db.begin():
        referred_q = _maybe_for_update(
            db.query(User).filter(User.id == referred_user_id), db
        )
        referred = referred_q.first()
        if not referred:
            return {"credited": False, "credits_added": 0, "reason": "user_not_found"}

        if not referred.referred_by:
            return {"credited": False, "credits_added": 0, "reason": "no_referrer"}

        if referred.referral_rewarded:
            return {"credited": False, "credits_added": 0, "reason": "already_rewarded"}

        eligible, reason = _check_activation_criteria(db, referred)
        if not eligible:
            return {"credited": False, "credits_added": 0, "reason": reason}

        referral_q = _maybe_for_update(
            db.query(Referral).filter(Referral.referred_id == referred.id), db
        )
        referral = referral_q.first()
        if not referral:
            referral = Referral(
                referrer_id=referred.referred_by,
                referred_id=referred.id,
                status="pending",
                metadata_json={"created_by": "system"},
            )
            db.add(referral)

        if referral.status == "confirmed":
            return {"credited": False, "credits_added": 0, "reason": "already_confirmed"}
        if referral.status == "rejected":
            return {"credited": False, "credits_added": 0, "reason": "rejected"}

        referrer_q = _maybe_for_update(
            db.query(User).filter(User.id == referred.referred_by), db
        )
        referrer = referrer_q.first()
        if not referrer:
            referral.status = "rejected"
            referral.metadata_json = _merge_metadata(
                referral.metadata_json,
                {
                    "reason": "referrer_missing",
                    "trigger": trigger,
                    "activation_ip": request_ip,
                },
            )
            logger.info(
                "referral_rejected reason=referrer_missing referred_id=%s",
                referred.id,
            )
            return {"credited": False, "credits_added": 0, "reason": "referrer_missing"}

        if (
            referred.signup_ip
            and referrer.signup_ip
            and referred.signup_ip == referrer.signup_ip
        ):
            referral.status = "rejected"
            referral.metadata_json = _merge_metadata(
                referral.metadata_json,
                {
                    "reason": "same_signup_ip",
                    "trigger": trigger,
                    "activation_ip": request_ip,
                },
            )
            logger.info(
                "referral_rejected reason=same_signup_ip referrer_id=%s referred_id=%s",
                referrer.id,
                referred.id,
            )
            return {"credited": False, "credits_added": 0, "reason": "same_signup_ip"}

        referrer.credits = (referrer.credits or 0) + REFERRAL_REWARD_CREDITS
        referred.referral_rewarded = True
        referral.status = "confirmed"
        referral.confirmed_at = datetime.utcnow()
        referral.metadata_json = _merge_metadata(
            referral.metadata_json,
            {"trigger": trigger, "activation_ip": request_ip},
        )
        logger.info(
            "referral_confirmed referrer_id=%s referred_id=%s credits=%s",
            referrer.id,
            referred.id,
            REFERRAL_REWARD_CREDITS,
        )
        return {
            "credited": True,
            "credits_added": REFERRAL_REWARD_CREDITS,
            "reason": "credited",
        }


def _check_activation_criteria(db: Session, referred: User):
    if not referred.is_verified:
        return False, "email_unverified"
    essays_count = (
        db.query(func.count(Essay.id)).filter(Essay.user_id == referred.id).scalar()
        or 0
    )
    if essays_count < 1:
        return False, "no_corrections"
    return True, "eligible"


def _merge_metadata(existing: Optional[Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    if existing is None:
        base: Dict[str, Any] = {}
    elif isinstance(existing, dict):
        base = dict(existing)
    else:
        try:
            base = json.loads(existing)
        except Exception:
            base = {}
    for key, value in extra.items():
        if value is not None:
            base[key] = value
    return base


def _maybe_for_update(query, db: Session):
    if db.bind and db.bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query
