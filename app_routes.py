# app_routes.py
import json
import os
import cloudinary  # NOVO
import cloudinary.uploader  # NOVO
import cloudinary.api  # NOVO
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import User, Essay, EssayReview
from auth_routes import get_current_user
from corrige_redacao_enem import (
    PROMPT_ENEM_CORRECTOR,
    gerar_correcao_openai,
    extrair_texto_imagem,
    extrair_texto_pdf,
)
from schemas import EnemTextRequest, EssayReviewCreate
from referrals_service import attempt_referral_activation

router = APIRouter(prefix="/app", tags=["app"])

# --- Configuração S3 REMOVIDA ---

# +++ NOVA CONFIGURAÇÃO CLOUDINARY +++
# Lê as variáveis de ambiente que você configurou no Render
try:
    cloudinary.config(
        cloud_name=os.environ.get("CLOUD_NAME"),
        api_key=os.environ.get("API_KEY"),
        api_secret=os.environ.get("API_SECRET"),
        secure=True,  # Sempre usar HTTPS
    )
except Exception as e:
    print(f"Alerta: Cloudinary não configurado. Uploads de arquivo falharão. Erro: {e}")


class SimulateCheckout(BaseModel):
    plano: str  # "individual" | "padrao" | "intensivao"


PLANOS_LANCAMENTO = {
    "individual": {"name": "Plano Individual", "credits": 1, "price": 1.90},
    "padrao": {"name": "Plano Padrão", "credits": 4, "price": 9.90},
    "intensivao": {"name": "Plano Intensivão", "credits": 25, "price": 19.90},
}


def _apply_plan_credit(
    *,
    plano_id: str,
    db: Session,
    current_user: User,
):
    if plano_id not in PLANOS_LANCAMENTO:
        raise HTTPException(status_code=400, detail="Plano inválido.")
    plano = PLANOS_LANCAMENTO[plano_id]
    user_db = db.get(User, current_user.id)
    if not user_db:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if user_db.credits is None:
        user_db.credits = 0
    user_db.credits += plano["credits"]
    db.add(user_db)
    db.commit()
    db.refresh(user_db)
    message = (
        f"SUPER promoção de lançamento 🎉 {plano['name']} por R$ {plano['price']:.2f}/mês "
        f"para os primeiros alunos. Você recebeu +{plano['credits']} créditos."
    )
    return {
        "message": message,
        "credits": user_db.credits,
        "plano": plano_id,
        "launch_price": plano["price"],
        "launch_promo": True,
    }


@router.post("/checkout/simular")
def simular_checkout(
    payload: SimulateCheckout,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _apply_plan_credit(
        plano_id=payload.plano,
        db=db,
        current_user=current_user,
    )


@router.post("/checkout/simular/individual")
def simular_checkout_individual(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _apply_plan_credit(
        plano_id="individual",
        db=db,
        current_user=current_user,
    )


@router.post("/checkout/simular/padrao")
def simular_checkout_padrao(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _apply_plan_credit(
        plano_id="padrao",
        db=db,
        current_user=current_user,
    )


@router.post("/checkout/simular/intensivao")
def simular_checkout_intensivao(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _apply_plan_credit(
        plano_id="intensivao",
        db=db,
        current_user=current_user,
    )


# ============================
# Helpers internos
# ============================


def _require_credits(user: User):
    """
    Verifica se o usuário tem créditos suficientes.
    """
    if user.credits is None or user.credits <= 0:
        raise HTTPException(
            status_code=402,
            detail="Créditos insuficientes. Compre mais créditos para continuar.",
        )


def _debitar_credito(db: Session, user: User) -> User:
    """
    Debita 1 crédito do usuário e devolve o registro atualizado.
    """
    user_db = db.get(User, user.id)
    if not user_db:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if user_db.credits is None:
        user_db.credits = 0
    if user_db.credits <= 0:
        raise HTTPException(
            status_code=402,
            detail="Créditos insuficientes. Compre mais créditos para continuar.",
        )
    user_db.credits -= 1
    db.add(user_db)
    return user_db


def _notas_por_competencia(resultado_json: Dict[str, Any]) -> Dict[int, int]:
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


# ============================
# Correção por TEXTO (app)
# ============================


@router.post("/enem/corrigir-texto")
async def app_corrigir_texto_enem(
    payload: EnemTextRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_credits(current_user)
    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f'TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n"{payload.tema}"\n\n'
        "Avalie a redação considerando rigorosamente a adequação a esse tema, "
        "especialmente na Competência 2.\n\n"
        "REDAÇÃO DO ALUNO:\n"
        f"{payload.texto}"
    )
    resultado_json = await gerar_correcao_openai(prompt_completo)
    notas_comp = _notas_por_competencia(resultado_json)
    nota_final = resultado_json.get("nota_final")
    nota_final_int = int(nota_final) if isinstance(nota_final, (int, float)) else None
    essay = Essay(
        user_id=current_user.id,
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
    user_db = _debitar_credito(db, current_user)
    db.add(essay)
    db.commit()
    db.refresh(user_db)
    db.refresh(essay)
    attempt_referral_activation(db, current_user.id, trigger="first_correction_done")
    return {
        "credits": user_db.credits,
        "resultado": resultado_json,
        "essay_id": essay.id,
    }


# ============================
# Correção por ARQUIVO (foto/PDF)
# ============================


@router.post("/enem/corrigir-arquivo")
async def app_corrigir_arquivo_enem(
    arquivo: UploadFile = File(...),
    tema: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_credits(current_user)
    content_type = (arquivo.content_type or "").lower()
    raw_bytes = await arquivo.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")

    # +++ LÓGICA CLOUDINARY +++
    arquivo_url_final = ""
    upload_result = {}
    try:
        upload_result = cloudinary.uploader.upload(
            BytesIO(raw_bytes),
            resource_type="auto",
            folder="cooorrige_uploads",  # Organiza numa pasta
            public_id=f"redacao_{current_user.id}_{datetime.utcnow().timestamp()}",
        )
        arquivo_url_final = upload_result.get("secure_url")
        if not arquivo_url_final:
            raise Exception("Cloudinary não retornou uma URL.")
    except Exception as e:
        print(f"ERRO CLOUDINARY: {e}")
        raise HTTPException(
            status_code=500, detail=f"Erro ao salvar arquivo no Cloudinary: {str(e)}"
        )
    # --- Fim da lógica Cloudinary ---

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
    notas_comp = _notas_por_competencia(resultado_json)
    nota_final = resultado_json.get("nota_final")
    nota_final_int = int(nota_final) if isinstance(nota_final, (int, float)) else None
    essay = Essay(
        user_id=current_user.id,
        tema=tema,
        input_type="arquivo",
        texto=texto_extraido,
        arquivo_path=arquivo_url_final,  # Salva a URL do Cloudinary
        nota_final=nota_final_int,
        c1_nota=notas_comp.get(1),
        c2_nota=notas_comp.get(2),
        c3_nota=notas_comp.get(3),
        c4_nota=notas_comp.get(4),
        c5_nota=notas_comp.get(5),
        resultado_json=json.dumps(resultado_json, ensure_ascii=False),
    )
    user_db = _debitar_credito(db, current_user)
    db.add(essay)
    db.commit()
    db.refresh(user_db)
    db.refresh(essay)
    attempt_referral_activation(db, current_user.id, trigger="first_correction_done")
    return {
        "credits": user_db.credits,
        "resultado": resultado_json,
        "essay_id": essay.id,
    }


# ============================
# Histórico + evolução do aluno
# ============================


@router.get("/enem/historico")
def historico_enem(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    essays: List[Essay] = (
        db.query(Essay)
        .filter(Essay.user_id == current_user.id)
        .order_by(Essay.created_at.asc())
        .all()
    )
    essay_ids = [essay.id for essay in essays]
    reviews_by_essay = {}
    if essay_ids:
        reviews = (
            db.query(EssayReview)
            .filter(
                EssayReview.user_id == current_user.id,
                EssayReview.essay_id.in_(essay_ids),
            )
            .all()
        )
        reviews_by_essay = {review.essay_id: review for review in reviews}
    historico = []
    notas = []
    for essay in essays:
        try:
            resultado = json.loads(essay.resultado_json)
        except Exception:
            resultado = None
        if essay.nota_final is not None:
            notas.append(essay.nota_final)

        # 'arquivo_path' já é a URL completa do Cloudinary
        arquivo_url = essay.arquivo_path
        review = reviews_by_essay.get(essay.id)
        review_payload = None
        if review:
            review_payload = {
                "review_id": review.id,
                "stars": review.stars,
                "comment": review.comment,
                "created_at": review.created_at.isoformat()
                if review.created_at
                else None,
                "updated_at": review.updated_at.isoformat()
                if review.updated_at
                else None,
            }

        historico.append(
            {
                "id": essay.id,
                "created_at": essay.created_at.isoformat()
                if essay.created_at
                else None,
                "tema": essay.tema,
                "input_type": essay.input_type,
                "nota_final": essay.nota_final,
                "c1_nota": essay.c1_nota,
                "c2_nota": essay.c2_nota,
                "c3_nota": essay.c3_nota,
                "c4_nota": essay.c4_nota,
                "c5_nota": essay.c5_nota,
                "arquivo_url": arquivo_url,
                "resultado": resultado,
                "review": review_payload,
            }
        )
    stats = {}
    if notas:
        stats = {
            "media_nota_final": sum(notas) / len(notas),
            "melhor_nota": max(notas),
            "pior_nota": min(notas),
            "ultima_nota": notas[-1],
        }
    return {"total": len(essays), "stats": stats, "historico": historico}


# ============================
# Avaliação da correção
# ============================


@router.post("/enem/avaliar")
def avaliar_correcao(
    payload: EssayReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    essay = db.get(Essay, payload.essay_id)
    if not essay or essay.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Redação não encontrada.")

    review = (
        db.query(EssayReview)
        .filter(
            EssayReview.user_id == current_user.id,
            EssayReview.essay_id == payload.essay_id,
        )
        .first()
    )
    if review:
        review.stars = payload.stars
        review.comment = payload.comment
    else:
        review = EssayReview(
            user_id=current_user.id,
            essay_id=payload.essay_id,
            stars=payload.stars,
            comment=payload.comment,
        )
        db.add(review)

    db.commit()
    db.refresh(review)
    return {
        "review_id": review.id,
        "essay_id": review.essay_id,
        "stars": review.stars,
        "comment": review.comment,
    }
