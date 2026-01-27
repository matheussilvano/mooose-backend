import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

import schemas
from auth_routes import get_current_user
from database import get_db
from models import Referral, User
from rate_limiter import enforce_rate_limit
from referrals_service import (
    REFERRAL_REWARD_CREDITS,
    attempt_referral_activation,
    generate_referral_code,
)
from utils import get_client_ip

router = APIRouter(tags=["referrals"])


@router.get("/me/referral", response_model=schemas.ReferralMeResponse)
def get_my_referral(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_db = db.get(User, current_user.id)
    if not user_db:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")

    if not user_db.referral_code:
        user_db.referral_code = generate_referral_code(db)
        db.add(user_db)
        db.commit()
        db.refresh(user_db)

    frontend_url = os.environ.get("FRONTEND_URL", "https://mooose.com.br").rstrip("/")
    referral_link = f"{frontend_url}/register?ref={user_db.referral_code}"

    pending = (
        db.query(func.count(Referral.id))
        .filter(Referral.referrer_id == user_db.id, Referral.status == "pending")
        .scalar()
        or 0
    )
    confirmed = (
        db.query(func.count(Referral.id))
        .filter(Referral.referrer_id == user_db.id, Referral.status == "confirmed")
        .scalar()
        or 0
    )

    total_earned = confirmed * REFERRAL_REWARD_CREDITS

    return {
        "referral_code": user_db.referral_code,
        "referral_link": referral_link,
        "reward_per_referral": REFERRAL_REWARD_CREDITS,
        "stats": {
            "pending": pending,
            "confirmed": confirmed,
            "total_earned_credits": total_earned,
        },
    }


@router.post("/referrals/activate", response_model=schemas.ReferralActivateResponse)
def activate_referral(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    client_ip = get_client_ip(request)
    enforce_rate_limit(f"referral-activate:{client_ip}", limit=5, window_seconds=60)

    result = attempt_referral_activation(
        db,
        current_user.id,
        trigger="manual",
        request_ip=client_ip,
    )

    return result
