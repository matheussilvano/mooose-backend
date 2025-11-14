FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependências do sistema (se PyPDF2 reclamar, pode precisar de mais coisas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Em produção, considere workers adicionais com --workers e --loop uvloop
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
