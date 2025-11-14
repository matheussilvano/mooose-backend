from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from corrige_redacao_enem import router as enem_router, verify_api_key

app = FastAPI(
    title="API de Correção de Redações do ENEM",
    description=(
        "API que corrige redações do ENEM via texto, imagem ou PDF, "
        "retornando notas por competência e feedback estruturado."
    ),
    version="1.0.0",
)

# CORS – em produção você pode restringir as origens
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajuste para domínios específicos depois
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["healthcheck"])
async def root():
    return {
        "status": "ok",
        "message": "API de correção do ENEM está ativa.",
        "endpoints": [
            "POST /api/enem/corrigir-texto",
            "POST /api/enem/corrigir-arquivo  (imagem jpeg/png ou PDF)",
        ],
    }


# Inclui o router da correção ENEM.
# Se quiser exigir API key também no prefixo todo, pode passar Depends aqui.
app.include_router(
    enem_router,
    prefix="/api",
    tags=["enem"],
    # dependencies=[Depends(verify_api_key)],  # opção: proteger tudo de uma vez
)
