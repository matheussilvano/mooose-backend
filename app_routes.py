# app_routes.py
import json
import os # NOVO
import boto3 # NOVO
from botocore.exceptions import NoCredentialsError # NOVO
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
from models import User, Essay
from auth_routes import get_current_user
from corrige_redacao_enem import (
    PROMPT_ENEM_CORRECTOR,
    gerar_correcao_openai,
    extrair_texto_imagem,
    extrair_texto_pdf,
)
from schemas import EnemTextRequest

router = APIRouter(prefix="/app", tags=["app"])

# NOVO: Configuração S3 (lê do ambiente)
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_REGION")

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)


class SimulateCheckout(BaseModel):
    plano: str  # "solo" | "intensivo" | "unlimited"


# Planos com SUPER promoção de lançamento
PLANOS_LANCAMENTO = {
    "solo": {
        "name": "Plano Enem Solo",
        "credits": 4,
        "price": 9.90,
    },
    "intensivo": {
        "name": "Plano Intensivo",
        "credits": 10,
        "price": 19.90,
    },
    "unlimited": {
        "name": "Plano Unlimited",
        # no MVP, damos um número bem alto de créditos para simular "ilimitado"
        "credits": 9999,
        "price": 29.90,
    },
}


@router.post("/checkout/simular")
def simular_checkout(
    payload: SimulateCheckout,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plano_id = payload.plano

    if plano_id not in PLANOS_LANCAMENTO:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    plano = PLANOS_LANCAMENTO[plano_id]

    # garante campo de créditos
    user_db = db.get(User, current_user.id)
    if not user_db:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    if user_db.credits is None:
        user_db.credits = 0

    # adiciona créditos da promoção de lançamento
    user_db.credits += plano["credits"]

    db.add(user_db)
    db.commit()
    db.refresh(user_db)

    message = (
        "SUPER promoção de lançamento 🎉 "
        f"{plano['name']} por R$ {plano['price']:.2f}/mês "
        "para os primeiros alunos. "
        f"Você recebeu +{plano['credits']} créditos."
    )

    return {
        "message": message,
        "credits": user_db.credits,
        "plano": plano_id,
        "launch_price": plano["price"],
        "launch_promo": True,
    }


# ============================
# Helpers internos
# ============================


def _require_credits(user: User):
    """
    Apenas valida o objeto que veio do auth.
    (a checagem real de débito é feita em _debitar_credito, já na sessão atual)
    """
    if user.credits is None or user.credits <= 0:
        raise HTTPException(
            status_code=402,
            detail="Você não tem créditos suficientes para corrigir uma redação.",
        )


def _debitar_credito(db: Session, user: User) -> User:
    """
    Debita 1 crédito usando SEMPRE o usuário na sessão atual (db),
    evitando conflito de sessões do SQLAlchemy.
    """
    user_db = db.get(User, user.id)
    if not user_db:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    if user_db.credits is None:
        user_db.credits = 0

    if user_db.credits <= 0:
        raise HTTPException(
            status_code=402,
            detail="Você não tem créditos suficientes.",
        )

    user_db.credits -= 1
    return user_db


def _notas_por_competencia(resultado_json: Dict[str, Any]) -> Dict[int, int]:
    """
    Lê o campo 'competencias' retornado pela IA e devolve
    {1: nota_comp1, 2: nota_comp2, ...}
    """
    notas = {}
    comps = resultado_json.get("competencias") or []
    if isinstance(comps, list):
        for comp in comps:
            if not isinstance(comp, dict):
                continue
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
    """
    Endpoint usado pelo front:
    - debita 1 crédito
    - chama a IA
    - salva a redação + correção na tabela essays
    - retorna o JSON da IA + créditos atualizados
    """
    _require_credits(current_user)

    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f"TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n\"{payload.tema}\"\n\n"
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

    # 🔑 debita crédito no usuário da sessão atual
    user_db = _debitar_credito(db, current_user)

    db.add(essay)
    db.commit()
    db.refresh(user_db)
    db.refresh(essay)

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
    """
    Versão app da correção via arquivo:
    - recebe tema + arquivo (imagem/pdf)
    - extrai o texto
    - chama a IA
    - salva arquivo em /uploads -> MUDADO PARA S3
    - salva redação + correção na tabela essays
    """
    _require_credits(current_user)

    content_type = (arquivo.content_type or "").lower()

    # Lê o conteúdo uma vez para guardar o arquivo
    raw_bytes = await arquivo.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")

    # --- LÓGICA DE SALVAR LOCAL REMOVIDA ---
    # uploads_dir = Path("uploads")
    # ...
    # with open(filepath, "wb") as f:
    #     f.write(raw_bytes)
    
    # +++ NOVA LÓGICA: Salva o arquivo no S3 +++
    if not S3_BUCKET or not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY or not AWS_REGION:
        raise HTTPException(status_code=500, detail="Servidor não configurado para upload de arquivos S3.")

    ext = Path(arquivo.filename or "redacao").suffix or ".bin"
    filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{current_user.id}{ext}"
    filepath_s3_key = f"uploads/{filename}" # Caminho no S3

    arquivo_url_s3 = "" # URL final
    try:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=filepath_s3_key,
            Body=BytesIO(raw_bytes), # Reusa os bytes já lidos
            ContentType=content_type,
            # ACL="public-read" # Descomente se seu bucket for público
        )
        # Assumindo ACL public-read ou bucket público.
        # Para buckets privados, você precisaria gerar uma URL assinada.
        arquivo_url_s3 = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{filepath_s3_key}"

    except NoCredentialsError:
        raise HTTPException(status_code=500, detail="Credenciais S3 não configuradas no servidor.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao salvar arquivo no S3: {str(e)}")
    # --- Fim da lógica S3 ---


    # 🔁 Reseta o ponteiro do arquivo para o início
    # (Necessário pois `extrair_texto` também lê o arquivo)
    await arquivo.seek(0)

    # Extrai texto conforme o tipo
    if content_type in ["image/jpeg", "image/jpg", "image/png"]:
        texto_extraido = await extrair_texto_imagem(arquivo)
    elif content_type == "application/pdf":
        texto_extraido = await extrair_texto_pdf(arquivo)
    else:
        # Limpa o arquivo do S3 se o tipo for inválido
        s3_client.delete_object(Bucket=S3_BUCKET, Key=filepath_s3_key)
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
        arquivo_path=arquivo_url_s3, # Salva a URL do S3
        nota_final=nota_final_int,
        c1_nota=notas_comp.get(1),
        c2_nota=notas_comp.get(2),
        c3_nota=notas_comp.get(3),
        c4_nota=notas_comp.get(4),
        c5_nota=notas_comp.get(5),
        resultado_json=json.dumps(resultado_json, ensure_ascii=False),
    )

    # 🔑 debita crédito no usuário da sessão atual
    user_db = _debitar_credito(db, current_user)

    db.add(essay)
    db.commit()
    db.refresh(user_db)
    db.refresh(essay)

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
    """
    Retorna o histórico de redações do usuário logado + estatísticas simples
    para acompanhar a evolução.
    """
    essays: List[Essay] = (
        db.query(Essay)
        .filter(Essay.user_id == current_user.id)
        .order_by(Essay.created_at.asc())
        .all()
    )

    historico = []
    notas = []

    for essay in essays:
        try:
            resultado = json.loads(essay.resultado_json)
        except Exception:
            resultado = None

        if essay.nota_final is not None:
            notas.append(essay.nota_final)

        # MUDANÇA: 'arquivo_path' agora é a URL completa do S3.
        # Não precisamos mais montar a URL com "/uploads/".
        arquivo_url = None
        if essay.arquivo_path:
            arquivo_url = essay.arquivo_path # Apenas repassa a URL salva

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