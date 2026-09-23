FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    HF_HOME=/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

# A V3 utiliza embeddings em CPU. Esta wheel oficial não inclui as bibliotecas
# CUDA/NVIDIA distribuídas pelas builds aceleradas do PyTorch.
ARG TORCH_VERSION=2.7.1+cpu
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}"

# requirements.txt não fixa torch; a instalação anterior já satisfaz a
# dependência de sentence-transformers sem substituir a build CPU-only.
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home appuser

COPY --chown=appuser:appuser . /app
RUN mkdir -p /app/uploads /app/outputs /app/.cache/huggingface /app/.cache/sentence-transformers \
    && chown -R appuser:appuser /app/uploads /app/outputs /app/.cache

USER appuser

EXPOSE 5000

# O banco e os snapshots ficam em /app/outputs; monte um volume persistente.
# A migração e o seed são idempotentes e rodam antes de aceitar requisições.
CMD ["sh", "-c", "flask --app app db upgrade && flask --app app seed-platform && gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 2 --timeout 900 app:app"]
