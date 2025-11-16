import os
from typing import Set

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import DemoKeyUsage
from corrige_redacao_enem import (
    PROMPT_ENEM_CORRECTOR,
    gerar_correcao_openai,
    extrair_texto_imagem,
    extrair_texto_pdf,
)

router = APIRouter(prefix="/demo", tags=["demo"])

DEMO_MAX_USES = 10


def _load_demo_keys() -> Set[str]:
    """
    Lê as chaves demo da variável de ambiente DEMO_KEYS.

    Exemplo:
      DEMO_KEYS="ESCOLA123,CURSINHO456,DEMO-ABC"
    """
    raw = os.environ.get("DEMO_KEYS", "").strip()
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


ALLOWED_DEMO_KEYS = _load_demo_keys()


def _get_or_create_usage(db: Session, key: str) -> DemoKeyUsage:
    usage = db.query(DemoKeyUsage).filter(DemoKeyUsage.key == key).first()
    if usage is None:
        usage = DemoKeyUsage(key=key, used=0)
        db.add(usage)
        db.commit()
        db.refresh(usage)
    return usage


def _validate_demo_key(db: Session, key: str) -> DemoKeyUsage:
    if not key:
        raise HTTPException(status_code=400, detail="Chave demo não informada.")

    if key not in ALLOWED_DEMO_KEYS:
        raise HTTPException(status_code=401, detail="Chave demo inválida.")

    usage = _get_or_create_usage(db, key)
    remaining = DEMO_MAX_USES - usage.used
    if remaining <= 0:
        raise HTTPException(
            status_code=403,
            detail="Esta chave demo já atingiu o limite de usos.",
        )
    return usage


# =========
# Schemas
# =========
class DemoKeyPayload(BaseModel):
    key: str


class DemoKeyStatus(BaseModel):
    valid: bool
    remaining: int | None = None
    max_uses: int | None = None


class DemoEnemTextRequest(BaseModel):
    key: str
    tema: str
    texto: str


# =========
# Rotas demo
# =========

@router.post("/validate-key", response_model=DemoKeyStatus)
def validate_key(payload: DemoKeyPayload, db: Session = Depends(get_db)):
    """
    Verifica se a chave demo existe em DEMO_KEYS e se ainda tem usos disponíveis.
    Retorna sempre 200 com { valid: true/false, remaining, max_uses }.
    """
    key = payload.key.strip()
    if not key or key not in ALLOWED_DEMO_KEYS:
        return DemoKeyStatus(valid=False)

    usage = _get_or_create_usage(db, key)
    remaining = max(DEMO_MAX_USES - usage.used, 0)

    return DemoKeyStatus(
        valid=remaining > 0,
        remaining=remaining,
        max_uses=DEMO_MAX_USES,
    )


@router.post("/enem/corrigir-texto")
async def demo_corrigir_texto_enem(
    payload: DemoEnemTextRequest,
    db: Session = Depends(get_db),
):
    """
    Versão DEMO da correção por texto:
    - não exige login
    - não mexe em créditos / planos
    - não salva Essay no banco
    - limita a 10 usos por chave
    """
    usage = _validate_demo_key(db, payload.key.strip())

    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f"TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n\"{payload.tema}\"\n\n"
        "Avalie a redação considerando rigorosamente a adequação a esse tema, "
        "especialmente na Competência 2.\n\n"
        "REDAÇÃO DO ALUNO:\n"
        f"{payload.texto}"
    )

    resultado_json = await gerar_correcao_openai(prompt_completo)

    # incrementa uso da chave
    usage.used += 1
    db.add(usage)
    db.commit()

    remaining = max(DEMO_MAX_USES - usage.used, 0)

    return {
        "resultado": resultado_json,
        "remaining": remaining,
        "max_uses": DEMO_MAX_USES,
    }


@router.post("/enem/corrigir-arquivo")
async def demo_corrigir_arquivo_enem(
    arquivo: UploadFile = File(...),
    tema: str = Form(...),
    key: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Versão DEMO da correção via foto/PDF:
    - recebe tema + arquivo
    - extrai texto (mesma lógica do app normal)
    - chama a IA
    - NÃO salva arquivo nem Essay
    - limita a 10 usos por chave
    """
    usage = _validate_demo_key(db, key.strip())

    content_type = (arquivo.content_type or "").lower()

    if content_type in ["image/jpeg", "image/jpg", "image/png"]:
        texto_extraido = await extrair_texto_imagem(arquivo)
    elif content_type == "application/pdf":
        texto_extraido = await extrair_texto_pdf(arquivo)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Tipo de arquivo não suportado. "
                "Use jpeg/jpg/png para imagem ou application/pdf para PDF."
            ),
        )

    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f"TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n\"{tema}\"\n\n"
        "Avalie a redação considerando rigorosamente a adequação a esse tema, "
        "especialmente na Competência 2.\n\n"
        "REDAÇÃO DO ALUNO (texto extraído de imagem/pdf):\n"
        f"{texto_extraido}"
    )

    resultado_json = await gerar_correcao_openai(prompt_completo)

    usage.used += 1
    db.add(usage)
    db.commit()

    remaining = max(DEMO_MAX_USES - usage.used, 0)

    return {
        "resultado": resultado_json,
        "remaining": remaining,
        "max_uses": DEMO_MAX_USES,
    }
