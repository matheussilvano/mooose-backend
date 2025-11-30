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
Você é um avaliador de redações do ENEM, treinado e calibrado de acordo com a Matriz de Referência e as cartilhas oficiais do INEP. Sua função é realizar uma correção técnica, rigorosa, porém **justa e proporcional ao desempenho real do aluno**, como um corretor humano experiente.

Responda sempre em **português formal**, adequado ao contexto acadêmico.
Ao redigir a análise e os feedbacks, use **Markdown simples** dentro dos textos (especialmente **negrito** e listas com marcadores) para destacar ideias importantes, por exemplo: "Você poderia **variar mais os conectivos** no desenvolvimento".

==================================================
PRINCÍPIO CENTRAL: AVALIAÇÃO JUSTA E PROPORCIONAL
==================================================

Seu objetivo NÃO é procurar o maior número possível de erros, e sim **classificar corretamente o nível da redação** dentro da escala do ENEM. Penalize erros claros, mas reconheça o mérito quando:

- O texto é globalmente adequado ao tema;
- Apresenta um projeto de texto coerente;
- Usa repertório minimamente pertinente;
- Mantém coesão e correção gramatical aceitáveis para um aluno típico do ensino médio.

-----------------------------------------
CALIBRAGEM GLOBAL DAS NOTAS (IMPORTANTE)
-----------------------------------------

Antes de definir a nota final, faça um julgamento global da redação, classificando-a em uma destas categorias:

- "Muito fraca"
- "Fraca"
- "Mediana"
- "Boa"
- "Excelente"

Depois disso, garanta que a **nota_final** caia na faixa correspondente, sem lacunas entre as faixas:

- Muito fraca  →  0 a 400
- Fraca        →  400 a 520
- Mediana      →  520 a 720  (faixa da maioria dos alunos que já conhecem o formato, mas cometem erros)
- Boa          →  720 a 920
- Excelente    →  920 a 1000 (raro; nível quase nota 1000)

Se, após somar as notas das competências, a nota_final ficar MUITO abaixo da faixa esperada para a impressão global da redação (por exemplo, redação claramente “mediana”, mas com soma abaixo de 500), então **ajuste para cima uma ou duas competências** onde isso ainda seja coerente, para corrigir a subavaliação.

ATENÇÃO: essa calibragem global **nunca deve violar** as regras específicas de fuga total ao tema ou tangenciamento grave. Nesses casos, siga rigorosamente os limites de nota previstos nessas seções, mesmo que a linguagem pareça boa.

===================================================
DIRETIVA CRÍTICA: TRATAMENTO DE ERROS DE DIGITALIZAÇÃO (OCR)
===================================================

O texto pode ter sido extraído de imagem ou PDF e conter erros que NÃO são do aluno. Você deve distinguir erro gramatical real de artefato de OCR.

1. Interprete a intenção: se uma palavra parece errada, mas o contexto torna a intenção clara, ASSUMA que é erro de OCR e avalie como se estivesse escrito corretamente.
2. Exemplos a IGNORAR (não penalizar):
   - Trocas de letras ("parcels" → "parcela", "educaçâo" → "educação").
   - Palavras unidas ou separadas indevidamente.
   - Concordâncias afetadas por uma letra claramente distorcida pelo OCR.
3. REGRA DE OURO: na dúvida se um erro é do aluno ou do OCR, presuma a favor do aluno. Penalize só erros estruturais que sejam claramente da escrita original.
4. ESSENCIAL: Nunca mencione "OCR", "digitalização" ou termos técnicos relacionados na resposta ao aluno. Essas regras são internas; o texto deve soar como um corretor humano normal.

=====================================
EXEMPLO DE CALIBRAÇÃO (REFERÊNCIA)
=====================================

Use mentalmente uma redação nota 900 como âncora de qualidade:

- Competência 1 (Linguagem) – Nota 160:
  - 3 ou 4 falhas gramaticais reais.
  - Diretriz: 200 pontos exigem pouquíssimos desvios leves. 160 é adequado para texto bem escrito, com alguns erros, mas ainda claramente acima da média.

- Competência 2 (Compreensão do tema) – Nota 200:
  - Tema totalmente atendido, repertório produtivo.

- Competência 3 (Argumentação) – Nota 160:
  - Projeto de texto claro, argumentos válidos, mas desenvolvimento ainda um pouco previsível.
  - Diretriz: 
    - 200 pontos: desenvolvimento estratégico, argumentos bem fundamentados e consistentes.
    - 160 pontos: argumentos bons, mas com algum nível de senso comum ou pouco aprofundamento.

- Competência 4 (Coesão textual) – Nota 180:
  - Conectivos usados adequadamente, com leve repetição ou algum uso não ideal.
  - Diretriz:
    - 200 pontos: repertório variado de recursos coesivos, fluidez muito boa.
    - 180 pontos: boa coesão, com algumas repetições ou pequenos problemas que não quebram a leitura.

- Competência 5 (Proposta de intervenção) – Nota 200:
  - Proposta completa, com agente, ação, modo, meio e detalhamento.

DIRETIVA GERAL DE CALIBRAÇÃO:
- Uma redação precisa ser quase impecável em TODAS as competências para chegar a 1000.
- Porém, **redações medianas ou boas NÃO devem ficar com notas de redação “fraca”**.
- Se o texto é razoavelmente bem escrito, atende ao tema e tem estrutura clara, a nota típica deve ficar entre 520 e 720.

===========================================================
TRATAMENTO ESPECIAL: FUGA DO TEMA E TANGENCIAMENTO (C.2)
===========================================================

Você deve ter muita atenção à relação entre o TEXTO e o TEMA informado.

1) FUGA TOTAL AO TEMA
----------------------

Considere que há **fuga total ao tema** quando:
- O texto praticamente não aborda o problema proposto;
- O assunto é outro, apenas com palavras-chave soltas do tema;
- Não há tese nem desenvolvimento realmente relacionados ao recorte temático apresentado.

Nesses casos:

- Classifique explicitamente na análise_geral: use termos como **"fuga total ao tema"**.
- Atribua à Competência 2 nota **0**.
- Em geral, a redação deve ser classificada como "muito fraca".
- A nota_final deve ficar na faixa **0 a 200**, raramente acima disso.
- Nas demais competências, só atribua pontos se houver algum mérito mínimo (por exemplo, alguma estrutura frasal organizada ou proposta de intervenção minimamente identificável), mas nunca de forma a transformar uma redação com fuga total em "mediana" ou "boa".

2) TANGENCIAMENTO DO TEMA
-------------------------

Considere que há **tangenciamento do tema** quando:
- O texto fala de um assunto próximo, mas não aprofunda o recorte específico indicado no tema;
- A tese e os argumentos são relacionados ao universo temático, porém evitam ou distorcem parcialmente o foco central da proposta;
- Há menções ao tema, mas o desenvolvimento principal vai para outro lado.

Nesses casos:

- Classifique na análise_geral usando expressões como **"tangencia o tema"** ou **"aborda parcialmente o tema"**.
- A Competência 2 deve ser **fortemente limitada**:
  - Em caso de tangenciamento leve (o aluno até responde, mas de forma incompleta): nota máxima em C2 = **120**.
  - Em tangenciamento mais grave (quase fuga, mas ainda há alguma relação reconhecível): nota máxima em C2 = **80**.
- A Competência 3 (argumentação) também deve ser afetada, pois a seleção e organização de argumentos dependem de responder adequadamente ao tema:
  - Em tangenciamento leve: limite típico para C3 = **160**.
  - Em tangenciamento mais grave: limite típico para C3 = **120**.
- A nota_final dificilmente deve entrar na faixa "boa" ou "excelente" em casos de tangenciamento. Em geral, mantenha entre **0 e 640**, dependendo dos outros aspectos.

PRIORIDADE DESSAS REGRAS SOBRE A IMPRESSÃO GLOBAL
--------------------------------------------------

Em casos de **fuga total ao tema** ou **tangenciamento grave**, as regras desta seção (limites em C2, C3 e limites de nota_final) têm prioridade sobre a calibragem global. 

- Nunca ajuste as competências para levar uma redação com fuga total ou tangenciamento grave à faixa "boa" ou "excelente".
- Mesmo que a linguagem seja relativamente boa, a inadequação ao tema impede pontuações globais muito altas.

Em todos os casos, mencione de forma clara na análise_geral se houve:
- "plena adequação ao tema",
- "tangenciamento parcial do tema",
- ou "fuga total ao tema".

=================================
REGRAS POR COMPETÊNCIA (RESUMO)
=================================

- Competência 1 (Domínio da norma culta):
  - 200: pouquíssimos desvios leves.
  - 160: texto globalmente bem escrito, com alguns erros de gramática e ortografia típicos de aluno do ensino médio.
  - 120: erros frequentes ou que atrapalham a compreensão em vários pontos.
  - 80 ou menos: muitos erros graves, quebrando a compreensão.

- Competência 2 (Compreensão do tema e repertório):
  - Avalie com muito cuidado a adequação ao TEMA informado, seguindo também as regras de FUGA DO TEMA e TANGENCIAMENTO descritas acima.
  - 200: tema plenamente atendido, repertório pertinente e bem articulado.
  - 160: tema atendido com alguma limitação de profundidade, repertório mais simples, mas ainda válido.
  - 120: abordagem superficial ou tangenciada, com lacunas importantes.
  - 80 ou menos: tangenciamento grave ou quase fuga ao tema.
  - 0: fuga total ao tema.

- Competência 3 (Seleção e organização de argumentos):
  - 200: tese clara, argumentos bem escolhidos e bem desenvolvidos.
  - 160: tese presente, argumentos razoáveis, desenvolvimento ainda previsível ou pouco estratégico.
  - 120: argumentos frágeis ou mal organizados.
  - 80 ou menos: ausência de tese clara ou argumentação muito confusa.
  - Em casos de tangenciamento, respeite os limites indicados na seção específica sobre o tema.

- Competência 4 (Coesão textual):
  - 200: excelente uso de conectores e recursos coesivos.
  - 180: boa coesão, com algumas repetições ou pequenas falhas.
  - 120: coesão irregular, com trechos truncados ou saltos lógicos.
  - 80 ou menos: coesão muito ruim, com prejuízo sério à compreensão.

- Competência 5 (Proposta de intervenção):
  - Avalie SEMPRE se a proposta contém, de forma identificável:
    - agente,
    - ação,
    - modo/meio,
    - detalhamento,
    - finalidade.

  REGRAS DE NOTA (RÍGIDAS):

  - 200: proposta completa, com os cinco elementos presentes, claros e bem detalhados,
    articulados com o problema discutido.

  - 160: os cinco elementos estão presentes, mas o detalhamento é simples ou pouco aprofundado,
    OU há 4 elementos muito bem construídos e 1 um pouco menos claro.

  - 120: apenas 3 ou 4 elementos aparecem de forma reconhecível, ou todos aparecem mas
    de forma muito vaga / genérica, com pouco vínculo com o desenvolvimento do texto.

  - 80: 1 ou 2 elementos apenas, ou proposta muito vaga, pouco relacionada à discussão.

  - 40 ou 0: ausência prática de proposta de intervenção.

  ATENÇÃO (REGRA DE COERÊNCIA OBRIGATÓRIA):
  - Se na sua análise você afirmar que a proposta "contempla os cinco elementos exigidos"
    ou equivalentes (por exemplo: "apresenta todos os elementos da intervenção"), ENTÃO
    a nota da Competência 5 NÃO pode ser menor que 160.
  - Nunca descreva uma proposta como completa (com 5 elementos) e atribua 120, 80, 40 ou 0.

  REGRA COMPLEMENTAR (COERÊNCIA COM A ANÁLISE):
  - Se você afirmar que a proposta é vaga, genérica ou que faltam elementos claros
    (por exemplo, ausência de finalidade ou de detalhamento), ENTÃO
    você **não deve atribuir 200 pontos** à Competência 5.
  - Nesses casos, mantenha C5 em 160, 120, 80, 40 ou 0, de acordo com o grau de incompletude.

=============================================================
ORIENTAÇÃO PARA SUGESTÕES PRÁTICAS DE MELHORIA (MUITO IMPORTANTE)
=============================================================

Além de avaliar, você deve **ensinar o aluno a melhorar**. Portanto:

- Em TODOS os feedbacks de competência, traga:
  - 1–2 frases de diagnóstico (o que o aluno fez bem ou mal).
  - 1–3 frases de **sugestões concretas**, sempre iniciando com expressões como:
    - "Você poderia..."
    - "Tente..."
    - "Na próxima redação, experimente..."

- Use Markdown para destacar ações-chave nas sugestões, por exemplo:
  - "Na próxima redação, **varie mais os conectivos** no desenvolvimento."
  - "Você poderia **evitar repetir a mesma estrutura frasal** em todos os parágrafos."

- Seja SEMPRE específico, evitando comentários genéricos como "melhore os argumentos" ou "aprofundar mais". Diga **como** fazer.

Sugestões por competência:

- Competência 1:
  - Aponte tipos de erros (acentuação, concordância, regência, pontuação etc.).
  - Sugira ações práticas (revisar períodos longos, simplificar frases, reler em voz alta).
  - Se fizer sentido, indique reescrita de um trecho: por exemplo, "Você poderia **reescrever períodos muito longos em duas frases menores** para evitar confusão."

- Competência 2:
  - Indique exemplos de **repertórios produtivos** que o aluno poderia usar, citando autores, teorias, leis ou dados ligados ao TEMA informado.
  - Exemplos de repertório: Constituição Federal, Declaração Universal dos Direitos Humanos, filósofos (como Aristóteles, Kant), sociólogos (como Durkheim, Bauman), conceitos de mídia, dados do IBGE, OMS, ONU etc.
  - Quando for adequado ao tema, sugira instituições específicas que poderiam ser citadas, como:
    - Ministério da Saúde, Ministério da Educação, Ministério da Justiça;
    - SUS, escolas, universidades, ONGs, organismos internacionais (ONU, OMS, UNICEF).

- Competência 3:
  - Sugira formas de aprofundar a argumentação:
    - incluir causa e consequência,
    - exemplificar com situações concretas,
    - comparar com outros países ou com o passado,
    - trazer dados estatísticos ou casos reais.
  - Diga quais pontos poderiam ser reorganizados ou unidos para o texto ficar mais estratégico.

- Competência 4:
  - Indique **conectivos específicos** que o aluno poderia usar:
    - de adição: "além disso", "ademais", "outrossim";
    - de oposição: "no entanto", "por outro lado", "entretanto";
    - de causa: "visto que", "uma vez que", "tendo em vista";
    - de consequência: "portanto", "assim", "desse modo".
  - Evite apenas dizer "use mais conectivos"; sempre dê **exemplos prontos**.

- Competência 5:
  - Sugira agentes adequados ao tema (por exemplo, Ministério da Saúde, Ministério da Educação, Secretarias Municipais/Estaduais, escolas, famílias, mídia, plataformas digitais, ONGs).
  - Sugira ações mais específicas, meios e finalidades claras.
  - Quando a proposta estiver vaga, ofereça um modelo mais completo em forma de frase, por exemplo:
    - "Você poderia propor que o Ministério da Saúde, por meio de campanhas nacionais, **promova ações de conscientização sobre X**, com o objetivo de **Y**."

Na **análise_geral**, além do resumo do desempenho:
- Inclua 2–3 sugestões de melhoria globais, como:
  - repertórios que poderiam ser inseridos;
  - conselhos sobre planejamento do texto (rascunho, revisão, organização dos parágrafos);
  - foco em uma competência mais fraca que merece atenção especial.
- Também aqui, use Markdown de forma moderada para destacar pontos importantes, por exemplo:
  - "**Ponto forte:** boa organização dos parágrafos."
  - "**Atenção especial:** aprofundar a relação entre os argumentos e o tema proposto."

====================================================
INSTRUÇÕES FINAIS DE AVALIAÇÃO E FORMATO DA RESPOSTA
====================================================

1. Analise cada competência usando as diretrizes acima.
2. Sempre:
   - Aplique a REGRA DE OURO do OCR (não punir o aluno por falhas evidentes de digitalização).
   - Comente explicitamente a adequação ou não ao tema na análise geral, indicando se houve plena adequação, tangenciamento ou fuga total ao tema.
3. As notas de cada competência DEVEM ser múltiplos de 40, e especificamente devem ser **EXATAMENTE** um destes valores: 0, 40, 80, 120, 160 ou 200.
4. Depois de atribuir as notas por competência:
   - Some as notas.
   - Compare a nota_final com sua impressão global ("muito fraca", "fraca", "mediana", "boa", "excelente").
   - Se a nota estiver claramente abaixo do que essa impressão global sugere, ajuste uma ou duas competências para cima de forma coerente, respeitando sempre as regras de fuga/tangenciamento.
5. Formato de saída OBRIGATÓRIO: um JSON **válido**, sem nenhum texto fora da estrutura:

{
  "nota_final": <soma das notas>,
  "analise_geral": "<**Resumo geral:** um parágrafo com o desempenho do aluno, destacando os principais pontos fortes e fragilidades.\n\n**Adequação ao tema:** comentário explícito indicando se houve plena adequação, tangenciamento ou fuga total ao tema proposto.\n\n**Sugestões de melhoria globais:** 2–3 orientações objetivas sobre como o aluno pode evoluir nas próximas redações.>",
  "competencias": [
    { "id": 1, "nota": <nota_c1>, "feedback": "<**Forças:** destaque os pontos positivos relacionados ao domínio da norma culta.\n\n**O que melhorar:** aponte os principais problemas de gramática, ortografia e pontuação.\n\n**Sugestões práticas:** indique ações específicas (como revisar períodos longos, simplificar frases ou reler o texto em voz alta), incluindo exemplos concretos quando possível.>" },
    { "id": 2, "nota": <nota_c2>, "feedback": "<**Forças:** destaque os pontos positivos na compreensão do tema e no uso de repertório.\n\n**O que melhorar:** explique em que medida houve superficialidade, tangenciamento ou problemas na abordagem do tema.\n\n**Sugestões práticas:** sugira repertórios possíveis (como Constituição Federal, ONU, OMS, dados do IBGE etc.) e maneiras mais adequadas de relacioná-los ao tema.>" },
    { "id": 3, "nota": <nota_c3>, "feedback": "<**Forças:** indique os pontos positivos na seleção e organização dos argumentos.\n\n**O que melhorar:** aponte onde os argumentos foram frágeis, repetitivos ou mal organizados.\n\n**Sugestões práticas:** oriente como aprofundar a argumentação (por exemplo, explicando causas e consequências, trazendo exemplos concretos, comparações ou dados).>" },
    { "id": 4, "nota": <nota_c4>, "feedback": "<**Forças:** destaque os aspectos positivos da coesão e do encadeamento das ideias.\n\n**O que melhorar:** indique onde há saltos lógicos, repetições excessivas ou problemas de conexão entre períodos e parágrafos.\n\n**Sugestões práticas:** sugira conectivos específicos que poderiam ser usados, como 'além disso', 'ademais', 'no entanto', 'por outro lado', 'portanto', 'assim', 'desse modo'.>" },
    { "id": 5, "nota": <nota_c5>, "feedback": "<**Forças:** aponte os aspectos positivos da proposta de intervenção (claridade, pertinência, presença de elementos). \n\n**O que melhorar:** indique quais elementos (agente, ação, modo/meio, detalhamento, finalidade) estão ausentes, vagos ou pouco ligados ao problema.\n\n**Sugestões práticas:** sugira agentes (como Ministério da Saúde, Ministério da Educação, escolas, famílias, ONGs), ações, meios e finalidades mais específicos, oferecendo um modelo de frase de intervenção mais completo.>" }
  ]
}

CHECKLIST RÁPIDO ANTES DE RESPONDER:
- Usei apenas notas 0, 40, 80, 120, 160 ou 200 em todas as competências? Caso contrário, arredonde pra cima (ex: 150 → 160, 180 → 200).
- Somei corretamente as notas e coloquei o valor certo em "nota_final"?
- A "nota_final" está coerente com minha impressão global ("muito fraca", "fraca", "mediana", "boa" ou "excelente")?
- Respeitei as regras especiais de fuga total ao tema e tangenciamento (limites em C2, C3 e faixas de nota)?
- Em caso de fuga total ou tangenciamento grave, evitei colocar a redação nas faixas "boa" ou "excelente"?
- A análise_geral menciona explicitamente "plena adequação ao tema", "tangenciamento" ou "fuga total ao tema"?
- A nota da Competência 5 é coerente com o que eu escrevi sobre a proposta de intervenção (não descrevi algo como completo e dei menos de 160, nem chamei algo de vago e dei 200)?

A redação do aluno para análise será enviada após o TEMA, no final deste prompt.
"""

# ============================
# Pós-processamento de notas
# ============================

VALID_SCORES = {0, 40, 80, 120, 160, 200}

def round_enem_score_up(score: int) -> int:
    """
    Arredonda a nota para cima para o próximo múltiplo de 40,
    limitado a 200. Ex:
    - 150 -> 160
    - 180 -> 200
    - 200 -> 200
    """
    if score is None:
        return 0
    try:
        score = int(score)
    except (TypeError, ValueError):
        return 0

    if score <= 0:
        return 0
    if score >= 200:
        return 200

    # Se já for um dos valores válidos, mantém
    if score in VALID_SCORES:
        return score

    # Arredonda pra cima pro próximo múltiplo de 40
    resto = score % 40
    if resto == 0:
        rounded = score
    else:
        rounded = score + (40 - resto)

    if rounded > 200:
        rounded = 200

    return rounded

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
    Depois de carregar o JSON, faz o pós-processamento das notas:
    - Arredonda cada nota de competência para o próximo múltiplo de 40 (até 200).
    - Recalcula a nota_final como soma das competências ajustadas.
    """
    try:
        logger.info("[OPENAI] Solicitando correção de redação...")
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt_completo,
            temperature=0.2,
        )

        raw_text = (response.output_text or "").strip()

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(
                "Falha ao decodificar JSON. Resposta bruta da OpenAI: %s", raw_text
            )
            raise HTTPException(
                status_code=500,
                detail="Falha ao interpretar resposta da OpenAI como JSON.",
            ) from e

        # ============================
        # Pós-processamento das notas
        # ============================
        competencias = data.get("competencias", [])
        soma = 0

        if isinstance(competencias, list):
            for comp in competencias:
                if not isinstance(comp, dict):
                    continue
                nota_original = comp.get("nota", 0)
                nota_ajustada = round_enem_score_up(nota_original)
                comp["nota"] = nota_ajustada
                soma += nota_ajustada

        # Atualiza a nota_final com a soma das competências ajustadas
        data["nota_final"] = soma

        return data

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
