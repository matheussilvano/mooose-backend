import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from referrals_service import (
    REFERRAL_REWARD_CREDITS,
    apply_referral_on_signup,
    attempt_referral_activation,
    generate_referral_code,
)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    models.Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def create_user(db, *, email, referral_code=None, is_verified=False, signup_ip=None):
    if not referral_code:
        referral_code = generate_referral_code(db)
    user = models.User(
        email=email,
        full_name=None,
        hashed_password="hash",
        credits=0,
        is_verified=is_verified,
        referral_code=referral_code,
        signup_ip=signup_ip,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_essay(db, user_id):
    essay = models.Essay(
        user_id=user_id,
        tema="Tema",
        input_type="texto",
        texto="Texto",
        resultado_json="{}",
    )
    db.add(essay)
    db.commit()
    return essay


def test_invalid_ref_ignored(db_session):
    create_user(db_session, email="ref@example.com")
    new_user = models.User(
        email="new@example.com",
        full_name=None,
        hashed_password="hash",
        credits=0,
        is_verified=False,
        referral_code=generate_referral_code(db_session),
    )
    db_session.add(new_user)
    db_session.flush()

    referral = apply_referral_on_signup(
        db_session,
        new_user,
        "INVALIDCODE",
        signup_ip="1.1.1.1",
        device_fingerprint=None,
    )
    db_session.commit()

    assert referral is None
    assert new_user.referred_by is None
    assert db_session.query(models.Referral).count() == 0


def test_self_referral_rejected(db_session):
    user = create_user(db_session, email="self@example.com", referral_code="SELFREF1")

    referral = apply_referral_on_signup(
        db_session,
        user,
        user.referral_code,
        signup_ip="1.1.1.1",
        device_fingerprint=None,
    )
    db_session.commit()

    assert referral is None
    assert user.referred_by is None
    assert db_session.query(models.Referral).count() == 0


def test_activation_without_criteria_does_not_credit(db_session):
    referrer = create_user(
        db_session,
        email="referrer@example.com",
        is_verified=True,
        signup_ip="10.0.0.1",
    )
    referred = create_user(
        db_session,
        email="referred@example.com",
        is_verified=False,
        signup_ip="10.0.0.2",
    )

    apply_referral_on_signup(
        db_session,
        referred,
        referrer.referral_code,
        signup_ip="10.0.0.2",
        device_fingerprint=None,
    )
    db_session.commit()

    result = attempt_referral_activation(
        db_session,
        referred.id,
        trigger="manual",
    )

    referrer_db = db_session.get(models.User, referrer.id)
    referral = (
        db_session.query(models.Referral)
        .filter(models.Referral.referred_id == referred.id)
        .first()
    )

    assert result["credited"] is False
    assert referrer_db.credits == 0
    assert referral.status == "pending"


def test_activation_with_criteria_credits_referrer(db_session):
    referrer = create_user(
        db_session,
        email="referrer2@example.com",
        is_verified=True,
        signup_ip="10.0.0.1",
    )
    referred = create_user(
        db_session,
        email="referred2@example.com",
        is_verified=True,
        signup_ip="10.0.0.3",
    )

    apply_referral_on_signup(
        db_session,
        referred,
        referrer.referral_code,
        signup_ip="10.0.0.3",
        device_fingerprint=None,
    )
    db_session.commit()
    create_essay(db_session, referred.id)

    result = attempt_referral_activation(
        db_session,
        referred.id,
        trigger="first_correction_done",
    )

    referrer_db = db_session.get(models.User, referrer.id)
    referred_db = db_session.get(models.User, referred.id)
    referral = (
        db_session.query(models.Referral)
        .filter(models.Referral.referred_id == referred.id)
        .first()
    )

    assert result["credited"] is True
    assert referrer_db.credits == REFERRAL_REWARD_CREDITS
    assert referred_db.referral_rewarded is True
    assert referral.status == "confirmed"


def test_activation_idempotency(db_session):
    referrer = create_user(
        db_session,
        email="referrer3@example.com",
        is_verified=True,
        signup_ip="10.0.0.1",
    )
    referred = create_user(
        db_session,
        email="referred3@example.com",
        is_verified=True,
        signup_ip="10.0.0.4",
    )

    apply_referral_on_signup(
        db_session,
        referred,
        referrer.referral_code,
        signup_ip="10.0.0.4",
        device_fingerprint=None,
    )
    db_session.commit()
    create_essay(db_session, referred.id)

    first = attempt_referral_activation(db_session, referred.id, trigger="manual")
    second = attempt_referral_activation(db_session, referred.id, trigger="manual")

    referrer_db = db_session.get(models.User, referrer.id)

    assert first["credited"] is True
    assert second["credited"] is False
    assert referrer_db.credits == REFERRAL_REWARD_CREDITS
