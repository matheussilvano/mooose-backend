import hashlib
import hmac
import json
import logging
import os
from typing import Optional

import mercadopago
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth_routes import get_current_user
from database import get_db
from models import MercadoPagoPayment, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

# === Config Mercado Pago ===
MP_ENV = os.environ.get("MP_ENV", "test").lower()
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
MP_ACCESS_TOKEN_TEST = os.environ.get("MP_ACCESS_TOKEN_TEST")
MP_PUBLIC_KEY = os.environ.get("MP_PUBLIC_KEY")
MP_PUBLIC_KEY_TEST = os.environ.get("MP_PUBLIC_KEY_TEST")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET")
MP_NOTIFICATION_URL = os.environ.get("MP_NOTIFICATION_URL")
MP_BACK_URL_SUCCESS = os.environ.get("MP_BACK_URL_SUCCESS")
MP_BACK_URL_FAILURE = os.environ.get("MP_BACK_URL_FAILURE")
MP_BACK_URL_PENDING = os.environ.get("MP_BACK_URL_PENDING")

# === Produto ===
PACKAGE_CURRENCY = "BRL"
PACKAGES = {
    "individual": {
        "title": "Plano Individual - 1 correção",
        "credits": 1,
        "price": 1.90,
    },
    "padrao": {
        "title": "Plano Padrão - 4 correções",
        "credits": 4,
        "price": 9.90,
    },
    "intensivao": {
        "title": "Plano Intensivão - 25 correções",
        "credits": 25,
        "price": 19.90,
    },
}


def _is_production() -> bool:
    return MP_ENV in {"prod", "production"}


def _get_access_token() -> str:
    if _is_production():
        if not MP_ACCESS_TOKEN:
            raise HTTPException(
                status_code=500,
                detail="MP_ACCESS_TOKEN nao configurado no ambiente.",
            )
        return MP_ACCESS_TOKEN
    if not MP_ACCESS_TOKEN_TEST:
        raise HTTPException(
            status_code=500,
            detail="MP_ACCESS_TOKEN_TEST nao configurado no ambiente.",
        )
    return MP_ACCESS_TOKEN_TEST


def _get_sdk() -> mercadopago.SDK:
    return mercadopago.SDK(_get_access_token())


def _parse_signature(x_signature: str) -> dict:
    parts = {}
    for item in x_signature.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts[key.strip()] = value.strip()
    return parts


def _build_manifest(data_id: Optional[str], request_id: str, ts: str) -> str:
    manifest_parts = []
    if data_id:
        manifest_parts.append(f"id:{data_id};")
    manifest_parts.append(f"request-id:{request_id};")
    manifest_parts.append(f"ts:{ts};")
    return "".join(manifest_parts)


def _validate_webhook_signature(
    *,
    data_id: Optional[str],
    x_signature: Optional[str],
    x_request_id: Optional[str],
) -> None:
    if not x_signature or not x_request_id:
        raise HTTPException(status_code=401, detail="Assinatura ausente.")
    if not MP_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="MP_WEBHOOK_SECRET não configurado no ambiente.",
        )

    parts = _parse_signature(x_signature)
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not v1:
        raise HTTPException(status_code=401, detail="Assinatura inválida.")

    data_id_lower = data_id.lower() if data_id else None
    manifest = _build_manifest(data_id_lower, x_request_id, ts)
    digest = hmac.new(
        MP_WEBHOOK_SECRET.encode("utf-8"),
        msg=manifest.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, v1):
        raise HTTPException(status_code=401, detail="Assinatura inválida.")


def _create_payment_preference(
    *,
    plan_id: str,
    db: Session,
    current_user: User,
):
    plan = PACKAGES.get(plan_id)
    if not plan:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    sdk = _get_sdk()

    preference_data = {
        "items": [
            {
                "title": plan["title"],
                "quantity": 1,
                "unit_price": plan["price"],
                "currency_id": PACKAGE_CURRENCY,
            }
        ],
        "external_reference": str(current_user.id),
        "metadata": {
            "user_id": current_user.id,
            "credits": plan["credits"],
            "plan_id": plan_id,
        },
    }

    if not MP_NOTIFICATION_URL:
        raise HTTPException(
            status_code=500,
            detail="MP_NOTIFICATION_URL nao configurado no ambiente.",
        )
    preference_data["notification_url"] = MP_NOTIFICATION_URL

    back_urls = {}
    if MP_BACK_URL_SUCCESS:
        back_urls["success"] = MP_BACK_URL_SUCCESS
    if MP_BACK_URL_FAILURE:
        back_urls["failure"] = MP_BACK_URL_FAILURE
    if MP_BACK_URL_PENDING:
        back_urls["pending"] = MP_BACK_URL_PENDING
    if back_urls:
        preference_data["back_urls"] = back_urls
        preference_data["auto_return"] = "approved"

    try:
        preference_response = sdk.preference().create(preference_data)
    except Exception as exc:
        logger.exception("Erro ao criar preferencia Mercado Pago")
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao criar preferencia Mercado Pago: {str(exc)}",
        )

    preference = (preference_response or {}).get("response") or {}
    if _is_production():
        checkout_url = preference.get("init_point") or preference.get("sandbox_init_point")
    else:
        checkout_url = preference.get("sandbox_init_point") or preference.get("init_point")
    preference_id = preference.get("id")
    if not checkout_url or not preference_id:
        raise HTTPException(
            status_code=502,
            detail="Resposta invalida do Mercado Pago.",
        )

    return {
        "checkout_url": checkout_url,
        "preference_id": preference_id,
        "plan_id": plan_id,
        "credits": plan["credits"],
        "price": plan["price"],
    }


@router.post("/payments/create")
def create_payment_preference(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # compatibilidade: plano padrão por padrão
    return _create_payment_preference(
        plan_id="padrao",
        db=db,
        current_user=current_user,
    )


@router.post("/payments/create/individual")
def create_payment_preference_individual(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _create_payment_preference(
        plan_id="individual",
        db=db,
        current_user=current_user,
    )


@router.post("/payments/create/padrao")
def create_payment_preference_padrao(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _create_payment_preference(
        plan_id="padrao",
        db=db,
        current_user=current_user,
    )


@router.post("/payments/create/intensivao")
def create_payment_preference_intensivao(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _create_payment_preference(
        plan_id="intensivao",
        db=db,
        current_user=current_user,
    )


@router.post("/webhooks/mercadopago")
async def mercadopago_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_signature: Optional[str] = Header(default=None, alias="x-signature"),
    x_request_id: Optional[str] = Header(default=None, alias="x-request-id"),
):
    query_params = request.query_params
    data_id = query_params.get("data.id") or query_params.get("id")
    if data_id:
        data_id = data_id.strip()

    _validate_webhook_signature(
        data_id=data_id,
        x_signature=x_signature,
        x_request_id=x_request_id,
    )

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    payment_id = data_id
    if not payment_id:
        payment_id = (
            body.get("data", {}).get("id")
            or body.get("id")
            or body.get("payment_id")
        )

    if not payment_id:
        raise HTTPException(status_code=400, detail="payment_id nao encontrado.")

    sdk = _get_sdk()
    try:
        payment_response = sdk.payment().get(payment_id)
    except Exception as exc:
        logger.exception("Erro ao buscar pagamento Mercado Pago")
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao buscar pagamento Mercado Pago: {str(exc)}",
        )

    payment = (payment_response or {}).get("response") or {}
    status = payment.get("status")
    status_detail = payment.get("status_detail")
    external_reference = payment.get("external_reference")
    preference_id = payment.get("order", {}).get("id") or payment.get("preference_id")
    metadata = payment.get("metadata") or {}

    user_id = None
    if external_reference:
        try:
            user_id = int(external_reference)
        except ValueError:
            user_id = None
    if not user_id and metadata.get("user_id"):
        try:
            user_id = int(metadata.get("user_id"))
        except ValueError:
            user_id = None

    credits_to_add = PACKAGE_CREDITS
    if metadata.get("credits"):
        try:
            credits_to_add = int(metadata.get("credits"))
        except (TypeError, ValueError):
            credits_to_add = PACKAGE_CREDITS

    payment_record = (
        db.query(MercadoPagoPayment)
        .filter(MercadoPagoPayment.payment_id == str(payment_id))
        .with_for_update()
        .first()
    )

    if not payment_record:
        payment_record = MercadoPagoPayment(
            payment_id=str(payment_id),
            preference_id=preference_id,
            user_id=user_id,
            credits=credits_to_add,
            status=status,
            status_detail=status_detail,
            credited=False,
            raw_json=json.dumps(payment, ensure_ascii=False),
        )
        db.add(payment_record)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            payment_record = (
                db.query(MercadoPagoPayment)
                .filter(MercadoPagoPayment.payment_id == str(payment_id))
                .with_for_update()
                .first()
            )

    if payment_record:
        payment_record.status = status
        payment_record.status_detail = status_detail
        payment_record.raw_json = json.dumps(payment, ensure_ascii=False)

    if payment_record and payment_record.credited:
        db.commit()
        return {"status": "ok", "message": "Pagamento ja processado."}

    if status == "approved" and payment_record and not payment_record.credited:
        if not user_id:
            raise HTTPException(
                status_code=400,
                detail="Nao foi possivel identificar o usuario do pagamento.",
            )
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(
                status_code=404,
                detail="Usuario nao encontrado para o pagamento.",
            )
        if user.credits is None:
            user.credits = 0
        user.credits += credits_to_add
        payment_record.credited = True
        db.add(user)

    db.add(payment_record)
    db.commit()

    return {"status": "ok"}
