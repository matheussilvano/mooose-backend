import os
import json
import base64
import logging
from io import BytesIO
from typing import Optional, Set

from openai import OpenAI
from fastapi import (
    APIRouter,
    File,
    UploadFile,
    HTTPException,
    Header,
    Depends,
    Form,
)
from pydantic import BaseModel
from PyPDF2 import PdfReader  # pip install PyPDF2

# ============================
# Logger
# ============================
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

router = APIRouter()

# ============================
# Configuração da OpenAI
# ============================

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise Exception(
        "ERRO: Verifique se a variável de ambiente OPENAI_API_KEY está configurada.\n"
        "Exemplo: export OPENAI_API_KEY='sua_chave_aqui'"
    )

# Modelo default – pode sobrescrever com OPENAI_MODEL no ambiente
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

client = OpenAI(api_key=OPENAI_API_KEY)

# ============================
# Configuração de segurança
# ============================

def _load_api_keys_from_env() -> Set[str]:
    """
    Lê API_KEYS do ambiente, ex:
    API_KEYS="chave1,chave2,chave3"
    Se vazio, não exige API key (modo dev).
    """
    raw = os.environ.get("API_KEYS", "").strip()
    if not raw:
        logger.warning(
            "Nenhuma API key configurada (variável API_KEYS vazia). "
            "A API está aceitando requisições sem autenticação. "
            "Configure API_KEYS em produção."
        )
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


ALLOWED_API_KEYS = _load_api_keys_from_env()


async def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    """
    Dependência para proteger endpoints com API Key.
    - Se ALLOWED_API_KEYS estiver vazio => não exige key (dev).
    - Se tiver valores => exige header X-API-Key com uma dessas chaves.
    """
    if not ALLOWED_API_KEYS:
        return  # modo sem autenticação (dev/local)

    if x_api_key is None or x_api_key not in ALLOWED_API_KEYS:
        raise HTTPException(
            status_code=401,
            detail="API Key inválida ou ausente. Envie X-API-Key no header.",
        )
    return x_api_key


# ============================
# Config gerais
# ============================

# Limite de 5 MB por arquivo (ajuste conforme necessidade)
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


class TextoEnemRequest(BaseModel):
    texto: str
    tema: str


PROMPT_ENEM_CORRECTOR = """
Você é um avaliador de redações do ENEM, treinado e calibrado de acordo com a Matriz de Referência e as cartilhas oficiais do INEP. Sua função é realizar uma correção técnica, rigorosa e, acima de tudo, educativa.

**Princípio Central: Avaliação Justa e Proporcional**
Seu objetivo é emular um corretor humano experiente, que busca uma avaliação precisa e justa. Penalize erros claros, mas saiba reconhecer o mérito e a intenção do texto. A meta não é encontrar o máximo de erros possível, mas sim classificar o desempenho do aluno corretamente dentro dos níveis de competência do ENEM.

---
**Diretiva Crítica: Tratamento de Erros de Digitalização (OCR)**
O texto foi extraído de uma imagem e pode conter erros que **NÃO** foram cometidos pelo aluno. Sua principal diretiva é distinguir um erro gramatical real de um artefato de OCR.

1.  **Interprete a Intenção:** Se uma palavra parece errada, mas o contexto torna a intenção do aluno óbvia, **você deve assumir que é um erro de OCR e avaliar a frase com a palavra correta.**
2.  **Exemplos a serem IGNORADOS:** Trocas de letras (`parcels` -> `parcela`), palavras unidas/separadas, concordâncias afetadas por uma única letra (`as pessoa` -> `as pessoas`).
3.  **Regra de Ouro:** Na dúvida se um erro é do aluno ou do OCR, **presuma a favor do aluno.** Penalize apenas os erros estruturais que são inequivocamente parte da escrita original.
---
**EXEMPLO DE CALIBRAÇÃO (ONE-SHOT LEARNING)**

**Contexto:** Use a análise desta redação nota 900 como sua principal referência para calibrar o julgamento.

* **Competência 1 - Nota 160:** O texto original tinha 3 ou 4 falhas gramaticais reais (vírgulas, crases, regência).
    * **Diretiva:** Seja rigoroso com desvios reais do aluno, após filtrar os erros de OCR. A nota 200 é para um texto com no máximo 1 ou 2 falhas leves.
* **Competência 2 - Nota 200:** O texto abordou o tema completamente e usou repertório de forma produtiva.
* **Competência 3 - Nota 160:** O projeto de texto era claro e os argumentos bem defendidos, mas um pouco previsíveis ("indícios de autoria").
    * **Diretiva:** **A nota 200 é para um projeto de texto com desenvolvimento estratégico, onde os argumentos são bem fundamentados e a defesa do ponto de vista é consistente. Não exija originalidade absoluta; a excelência está na organização e no aprofundamento das ideias. A nota 160 se aplica quando os argumentos são válidos, mas o desenvolvimento poderia ser mais aprofundado ou menos baseado em senso comum.**
* **Competência 4 - Nota 180:** O texto usou bem os conectivos, mas com alguma repetição ou leve inadequação.
    * **Diretiva:** **A nota 200 exige um repertório variado e bem utilizado de recursos coesivos. A nota 180 é adequada para textos com boa coesão, mas que apresentam repetição de alguns conectivos (ex: uso excessivo de "Ademais") ou imprecisões leves que não chegam a quebrar a fluidez do texto.**
* **Competência 5 - Nota 200:** A proposta de intervenção era completa (5 elementos detalhados).

**Diretiva Geral de Calibração:**
Use o exemplo acima como uma âncora. Ele representa um texto excelente (Nota 900) que não atinge a perfeição. Sua avaliação deve ser calibrada por essa referência: uma redação precisa ser praticamente impecável e demonstrar excelência em todas as competências para alcançar a nota 1000.

---
**Instruções de Avaliação:**

1.  **Análise Calibrada:** Avalie cada competência usando o exemplo acima e, fundamentalmente, a **Regra de Ouro do OCR**.
2.  **Feedback Justificado:** Cite trechos para justificar a nota. Ao apontar um erro, certifique-se de que é um erro de escrita, não de digitalização.
3.  **Tema da Proposta:** Você receberá o TEMA da proposta de redação. Avalie com atenção a **adequação ao tema**, especialmente na Competência 2. Se houver fuga total ou parcial ao tema, explique isso claramente na análise.
4.  **Múltiplos de 40**: A nota de cada competência deve ser um múltiplo de 40 (0, 40, 80, 120, 160, 200).
5.  **Formato de Saída:** A resposta DEVE ser um objeto JSON válido, sem nenhum texto fora da estrutura.
6.  **Feedback Construtivo:** Forneça feedback que ajude o aluno a entender seus erros e como melhorar, sempre com base na escrita real, não nos erros de OCR. Foque em falar apenas o que será útil para o aprendizado do aluno.

---
**Estrutura de Saída JSON Obrigatória:**
{
  "nota_final": <soma das notas>,
  "analise_geral": "<um parágrafo com o resumo do desempenho do aluno, destacando os pontos fortes e as principais áreas para melhoria. Inclua sempre um comentário explícito sobre a adequação ou não ao tema proposto.>",
  "competencias": [
    { "id": 1, "nota": <nota_c1>, "feedback": "<feedback_c1>" },
    { "id": 2, "nota": <nota_c2>, "feedback": "<feedback_c2>" },
    { "id": 3, "nota": <nota_c3>, "feedback": "<feedback_c3>" },
    { "id": 4, "nota": <nota_c4>, "feedback": "<feedback_c4>" },
    { "id": 5, "nota": <nota_c5>, "feedback": "<feedback_c5>" }
  ]
}

A redação do aluno para análise será enviada após o TEMA, no final deste prompt.
"""


# ============================
# Funções auxiliares
# ============================

async def extrair_texto_imagem(arquivo: UploadFile) -> str:
    """
    Extrai texto de uma imagem usando apenas OpenAI (visão).
    Espera imagens do tipo jpeg/jpg/png.
    """
    try:
        content = await arquivo.read()
        if not content:
            raise HTTPException(status_code=400, detail="Arquivo de imagem vazio.")

        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Arquivo de imagem muito grande (máx. 5 MB).",
            )

        mime_type = arquivo.content_type or "image/png"

        # Converte a imagem para base64 e monta um data URL
        b64 = base64.b64encode(content).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        logger.info(
            f"[IMAGEM] Extraindo texto com OpenAI - tipo={mime_type}, "
            f"tamanho={len(content)} bytes"
        )

        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Transcreva todo o texto da redação presente nesta imagem. "
                                "Retorne SOMENTE o texto puro da redação, sem comentários, "
                                "sem explicações e sem formatação extra."
                            ),
                        },
                        {
                            "type": "input_image",
                            # Responses API espera uma string com a URL/base64
                            "image_url": data_url,
                        },
                    ],
                }
            ],
        )

        # Responses API: texto direto em output_text
        texto_extraido = (response.output_text or "").strip()

        if not texto_extraido:
            raise HTTPException(
                status_code=400,
                detail="Nenhum texto detectado na imagem pela OpenAI.",
            )

        return texto_extraido

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao extrair texto da imagem com OpenAI")
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao extrair texto da imagem com OpenAI: {str(e)}",
        )


async def extrair_texto_pdf(arquivo: UploadFile) -> str:
    """
    Extrai texto de um PDF usando PyPDF2 (local, sem Google).
    """
    try:
        content = await arquivo.read()
        if not content:
            raise HTTPException(status_code=400, detail="Arquivo PDF vazio.")

        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
            detail="Arquivo PDF muito grande (máx. 5 MB).",
            )

        logger.info(
            f"[PDF] Extraindo texto - tipo={arquivo.content_type}, "
            f"tamanho={len(content)} bytes"
        )

        reader = PdfReader(BytesIO(content))
        texto = ""

        for page in reader.pages:
            page_text = page.extract_text() or ""
            texto += page_text + "\n"

        if not texto.strip():
            raise HTTPException(
                status_code=400,
                detail="Nenhum texto detectado no PDF.",
            )

        return texto
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro no processamento do PDF")
        raise HTTPException(
            status_code=500,
            detail=f"Erro no processamento do PDF: {str(e)}",
        )


async def gerar_correcao_openai(prompt_completo: str):
    """
    Chama a API da OpenAI com o prompt completo e retorna o JSON carregado.
    Usa o endpoint Responses. O prompt obriga a saída em JSON.
    """
    try:
        logger.info("[OPENAI] Solicitando correção de redação...")
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt_completo,
            temperature=0,
        )

        raw_text = (response.output_text or "").strip()

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(
                "Falha ao decodificar JSON. Resposta bruta da OpenAI: %s", raw_text
            )
            raise HTTPException(
                status_code=500,
                detail="Falha ao interpretar resposta da OpenAI como JSON.",
            ) from e

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro na API da OpenAI")
        raise HTTPException(
            status_code=500,
            detail=f"Erro na API da OpenAI ou ao interpretar a resposta: {str(e)}",
        )


# ============================
# Endpoints
# ============================

@router.post(
    "/enem/corrigir-texto",
    summary="Corrige redação do ENEM via texto (JSON)",
)
async def corrigir_texto_enem(
    request: TextoEnemRequest,
    api_key: str = Depends(verify_api_key),
):
    """
    Exemplo de body:
    {
      "texto": "Minha redação completa aqui...",
      "tema": "Caminhos para combater a intolerância religiosa no Brasil"
    }
    """
    logger.info("[API] Correção via texto solicitada")
    logger.info(f"[API] Tema recebido: {request.tema!r}")

    prompt_completo = (
        f"{PROMPT_ENEM_CORRECTOR}\n\n"
        f"TEMA DA PROPOSTA DE REDAÇÃO (ENEM):\n\"{request.tema}\"\n\n"
        "Avalie a redação considerando rigorosamente a adequação a esse tema, "
        "especialmente na Competência 2.\n\n"
        "REDAÇÃO DO ALUNO:\n"
        f"{request.texto}"
    )

    resultado_json = await gerar_correcao_openai(prompt_completo)
    return resultado_json


@router.post(
    "/enem/corrigir-arquivo",
    summary="Corrige redação do ENEM via arquivo (imagem jpeg/png ou PDF)",
)
async def corrigir_arquivo_enem(
    arquivo: UploadFile = File(...),
    tema: str = Form(...),
    api_key: str = Depends(verify_api_key),
):
    """
    Aceita:
    - Imagem: image/jpeg, image/jpg, image/png
    - PDF: application/pdf

    O campo 'tema' é OBRIGATÓRIO e deve ser enviado como form-data junto com o arquivo.

    Exemplo com curl (imagem):

    curl -X POST "http://localhost:8000/api/enem/corrigir-arquivo" \
      -H "X-API-Key: SUA_CHAVE_AQUI" \
      -F "arquivo=@/caminho/para/redacao.png" \
      -F "tema=Caminhos para combater a intolerância religiosa no Brasil"

    Exemplo com curl (PDF):

    curl -X POST "http://localhost:8000/api/enem/corrigir-arquivo" \
      -H "X-API-Key: SUA_CHAVE_AQUI" \
      -F "arquivo=@/caminho/para/redacao.pdf" \
      -F "tema=Desafios para a formação educacional de surdos no Brasil"
    """
    content_type = (arquivo.content_type or "").lower()
    logger.info(f"[API] Correção via arquivo - content_type={content_type}")
    logger.info(f"[API] Tema recebido: {tema!r}")

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
        "REDAÇÃO DO ALUNO (transcrita do arquivo enviado):\n"
        f"{texto_extraido}"
    )

    resultado_json = await gerar_correcao_openai(prompt_completo)
    return resultado_json
