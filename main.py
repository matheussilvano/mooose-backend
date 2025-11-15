# main.py
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import models
from database import engine
from corrige_redacao_enem import router as enem_router, verify_api_key
from auth_routes import router as auth_router
from app_routes import router as app_router

# Cria tabelas do banco
models.Base.metadata.create_all(bind=engine)

# Garante diretório de uploads
UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Cooorrige by Mooose",
    description=(
        "Plataforma web para correção automática de redações do ENEM, "
        "com cadastro de alunos, login e controle de créditos."
    ),
    version="1.0.0",
)

# CORS – pode restringir depois para o domínio do front
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois: ["https://cooorrige.mooose.com.br"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir arquivos enviados (fotos/PDFs das redações)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.get("/", tags=["healthcheck"])
async def root():
    return {
        "status": "ok",
        "message": "Cooorrige by Mooose API está ativa.",
        "modules": {
            "auth": ["/auth/register", "/auth/login", "/auth/me"],
            "app": [
                "/app/checkout/simular",
                "/app/enem/corrigir-texto",
                "/app/enem/corrigir-arquivo",
                "/app/enem/historico",
            ],
            "enem_raw": [
                "POST /api/enem/corrigir-texto",
                "POST /api/enem/corrigir-arquivo",
            ],
        },
    }


# Rotas de autenticação e app
app.include_router(auth_router)
app.include_router(app_router)

# Mantém seu router original de correção ENEM
app.include_router(
    enem_router,
    prefix="/api",
    tags=["enem"],
    # dependencies=[Depends(verify_api_key)],  # pode reativar se quiser proteger
)
