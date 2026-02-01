import json
import os
from datetime import datetime
from io import BytesIO
from typing import Optional

import cloudinary
import cloudinary.uploader
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy.orm import Session, object_session

from anon_service import (
    ANON_IP_SOFT_LIMIT,
    ANON_IP_WINDOW_SECONDS,
    consume_free,
    effective_free_used,
    free_remaining,
    get_or_create_anon_session,
)
from auth_routes import get_current_user_optional
from corrige_redacao_enem import (
    PROMPT_ENEM_CORRECTOR,
    extrair_texto_imagem,
    extrair_texto_pdf,
    gerar_correcao_openai,
)
from database import get_db
from models import Essay, User
from rate_limiter import is_rate_limited
from referrals_service import attempt_referral_activation
from schemas import CorrectionResponse, CorrectionTextRequest
from utils import get_client_ip

router = APIRouter(tags=["corrections"])

try:
    cloudinary.config(
        cloud_name=os.environ.get("CLOUD_NAME"),
        api_key=os.environ.get("API_KEY"),
        api_secret=os.environ.get("API_SECRET"),
        secure=True,
    )
except Exception as e:
    print(f"Alerta: Cloudinary não configurado. Uploads de arquivo falharão. Erro: {e}")


def _notas_por_competencia(resultado_json):
    notas = {}
    comps = resultado_json.get("competencias") or []
    if isinstance(comps, list):
        for comp in comps:
            if isinstance(comp, dict):
                cid = comp.get("id")
                nota = comp.get("nota")
                if isinstance(cid, int) and isinstance(nota, int):
                    notas[cid] = nota
    return notas


def _gate_response(
    *,
    remaining: int,
    requires_auth: bool,
    requires_payment: bool,
) -> CorrectionResponse:
    if requires_auth:
        next_action = "PROMPT_SIGNUP"
    elif requires_payment:
        next_action = "PROMPT_PAYWALL"
    else:
        next_action = "CONTINUE"
    return CorrectionResponse(
        free_remaining=remaining,
        requires_auth=requires_auth,
        requires_payment=requires_payment,
        next_action=next_action,
    )


def _maybe_require_auth_for_anon(
    *,
    anon_free_used: int,
    ip: Optional[str],
) -> bool:
    if not ip:
        return False
    suspicious = is_rate_limited(
        f"anon-free:{ip}",
        limit=ANON_IP_SOFT_LIMIT,
        window_seconds=ANON_IP_WINDOW_SECONDS,
    )
    return suspicious and anon_free_used >= 1


def _debit_credit(db: Session, user: User) -> None:
    if object_session(user) is not db:
        user = db.merge(user)
    if user.credits is None:
        user.credits = 0
    if user.credits <= 0:
        raise HTTPException(
            status_code=402,
            detail="Créditos insuficientes. Compre mais créditos para continuar.",
        )
    user.credits -= 1
    db.add(user)


def _ensure_user_attached(db: Session, user: Optional[User]) -> Optional[User]:
    if user is None:
        return None
    if object_session(user) is db:
        return user
    return db.merge(user)


async def _build_text_correction(
    *,
    tema: str,
    texto: str,
) -> dict:
    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f'TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n"{tema}"\n\n'
        "Avalie a redação considerando rigorosamente a adequação a esse tema, "
        "especialmente na Competência 2.\n\n"
        "REDAÇÃO DO ALUNO:\n"
        f"{texto}"
    )
    resultado_json = await gerar_correcao_openai(prompt_completo)
    return resultado_json


async def _build_file_correction(
    *,
    tema: str,
    arquivo: UploadFile,
    user_id: Optional[int],
) -> tuple[str, dict, str]:
    content_type = (arquivo.content_type or "").lower()
    raw_bytes = await arquivo.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")

    arquivo_url_final = ""
    upload_result = {}
    try:
        upload_result = cloudinary.uploader.upload(
            BytesIO(raw_bytes),
            resource_type="auto",
            folder="cooorrige_uploads",
            public_id=f"redacao_{user_id or 'anon'}_{datetime.utcnow().timestamp()}",
        )
        arquivo_url_final = upload_result.get("secure_url")
        if not arquivo_url_final:
            raise Exception("Cloudinary não retornou uma URL.")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Erro ao salvar arquivo no Cloudinary: {str(e)}"
        )

    await arquivo.seek(0)
    texto_extraido = ""
    try:
        if content_type in ["image/jpeg", "image/jpg", "image/png"]:
            texto_extraido = await extrair_texto_imagem(arquivo)
        elif content_type == "application/pdf":
            texto_extraido = await extrair_texto_pdf(arquivo)
        else:
            raise HTTPException(
                status_code=400,
                detail="Tipo de arquivo não suportado. Use jpeg/jpg/png ou PDF.",
            )
    except Exception as e:
        if "public_id" in upload_result:
            cloudinary.uploader.destroy(upload_result["public_id"])
        raise e

    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f'TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n"{tema}"\n\n'
        "Avalie a redação considerando rigorosamente a adequação a esse tema, "
        "especialmente na Competência 2.\n\n"
        "REDAÇÃO DO ALUNO (transcrita do arquivo enviado):\n"
        f"{texto_extraido}"
    )
    resultado_json = await gerar_correcao_openai(prompt_completo)
    return texto_extraido, resultado_json, arquivo_url_final


@router.post("/corrections", response_model=CorrectionResponse)
async def correction_text(
    payload: CorrectionTextRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    x_anon_id: Optional[str] = Header(default=None, alias="X-ANON-ID"),
    x_device_id: Optional[str] = Header(default=None, alias="X-DEVICE-ID"),
):
    if not x_anon_id:
        raise HTTPException(status_code=400, detail="X-ANON-ID obrigatório.")

    client_ip = get_client_ip(request)
    device_id = payload.device_id or x_device_id
    anon_session = get_or_create_anon_session(
        db,
        anon_id=x_anon_id,
        ip=client_ip,
        device_id=device_id,
    )

    effective_used = effective_free_used(current_user, anon_session)
    remaining = free_remaining(effective_used)
    current_user = _ensure_user_attached(db, current_user)

    if current_user is None:
        if remaining <= 0:
            return _gate_response(
                remaining=remaining,
                requires_auth=True,
                requires_payment=False,
            )
        if _maybe_require_auth_for_anon(
            anon_free_used=anon_session.free_used or 0,
            ip=client_ip,
        ):
            return _gate_response(
                remaining=remaining,
                requires_auth=True,
                requires_payment=False,
            )
    else:
        if remaining <= 0 and (current_user.credits or 0) <= 0:
            return _gate_response(
                remaining=remaining,
                requires_auth=False,
                requires_payment=True,
            )

    resultado_json = await _build_text_correction(tema=payload.tema, texto=payload.texto)
    notas_comp = _notas_por_competencia(resultado_json)
    nota_final = resultado_json.get("nota_final")
    nota_final_int = int(nota_final) if isinstance(nota_final, (int, float)) else None

    essay = Essay(
        user_id=current_user.id if current_user else None,
        anon_id=x_anon_id,
        tema=payload.tema,
        input_type="texto",
        texto=payload.texto,
        nota_final=nota_final_int,
        c1_nota=notas_comp.get(1),
        c2_nota=notas_comp.get(2),
        c3_nota=notas_comp.get(3),
        c4_nota=notas_comp.get(4),
        c5_nota=notas_comp.get(5),
        resultado_json=json.dumps(resultado_json, ensure_ascii=False),
    )

    if current_user is None:
        new_used = consume_free(
            user=None,
            anon_session=anon_session,
            effective_used=effective_used,
        )
        remaining = free_remaining(new_used)
    else:
        if remaining > 0:
            new_used = consume_free(
                user=current_user,
                anon_session=anon_session,
                effective_used=effective_used,
            )
            remaining = free_remaining(new_used)
        else:
            _debit_credit(db, current_user)

    db.add(essay)
    db.add(anon_session)
    if current_user:
        db.add(current_user)
    db.commit()
    db.refresh(essay)
    if current_user:
        db.refresh(current_user)
        attempt_referral_activation(db, current_user.id, trigger="first_correction_done")

    return CorrectionResponse(
        free_remaining=remaining,
        requires_auth=False,
        requires_payment=False,
        next_action="CONTINUE",
        resultado=resultado_json,
        essay_id=essay.id,
        credits=current_user.credits if current_user else None,
    )


@router.post("/corrections/file", response_model=CorrectionResponse)
async def correction_file(
    request: Request,
    arquivo: UploadFile = File(...),
    tema: str = Form(...),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    x_anon_id: Optional[str] = Header(default=None, alias="X-ANON-ID"),
    x_device_id: Optional[str] = Header(default=None, alias="X-DEVICE-ID"),
):
    if not x_anon_id:
        raise HTTPException(status_code=400, detail="X-ANON-ID obrigatório.")

    client_ip = get_client_ip(request)
    device_id = x_device_id
    anon_session = get_or_create_anon_session(
        db,
        anon_id=x_anon_id,
        ip=client_ip,
        device_id=device_id,
    )

    effective_used = effective_free_used(current_user, anon_session)
    remaining = free_remaining(effective_used)
    current_user = _ensure_user_attached(db, current_user)

    if current_user is None:
        if remaining <= 0:
            return _gate_response(
                remaining=remaining,
                requires_auth=True,
                requires_payment=False,
            )
        if _maybe_require_auth_for_anon(
            anon_free_used=anon_session.free_used or 0,
            ip=client_ip,
        ):
            return _gate_response(
                remaining=remaining,
                requires_auth=True,
                requires_payment=False,
            )
    else:
        if remaining <= 0 and (current_user.credits or 0) <= 0:
            return _gate_response(
                remaining=remaining,
                requires_auth=False,
                requires_payment=True,
            )

    texto_extraido, resultado_json, arquivo_url_final = await _build_file_correction(
        tema=tema,
        arquivo=arquivo,
        user_id=current_user.id if current_user else None,
    )

    notas_comp = _notas_por_competencia(resultado_json)
    nota_final = resultado_json.get("nota_final")
    nota_final_int = int(nota_final) if isinstance(nota_final, (int, float)) else None

    essay = Essay(
        user_id=current_user.id if current_user else None,
        anon_id=x_anon_id,
        tema=tema,
        input_type="arquivo",
        texto=texto_extraido,
        arquivo_path=arquivo_url_final,
        nota_final=nota_final_int,
        c1_nota=notas_comp.get(1),
        c2_nota=notas_comp.get(2),
        c3_nota=notas_comp.get(3),
        c4_nota=notas_comp.get(4),
        c5_nota=notas_comp.get(5),
        resultado_json=json.dumps(resultado_json, ensure_ascii=False),
    )

    if current_user is None:
        new_used = consume_free(
            user=None,
            anon_session=anon_session,
            effective_used=effective_used,
        )
        remaining = free_remaining(new_used)
    else:
        if remaining > 0:
            new_used = consume_free(
                user=current_user,
                anon_session=anon_session,
                effective_used=effective_used,
            )
            remaining = free_remaining(new_used)
        else:
            _debit_credit(db, current_user)

    db.add(essay)
    db.add(anon_session)
    if current_user:
        db.add(current_user)
    db.commit()
    db.refresh(essay)
    if current_user:
        db.refresh(current_user)
        attempt_referral_activation(db, current_user.id, trigger="first_correction_done")

    return CorrectionResponse(
        free_remaining=remaining,
        requires_auth=False,
        requires_payment=False,
        next_action="CONTINUE",
        resultado=resultado_json,
        essay_id=essay.id,
        credits=current_user.credits if current_user else None,
    )
