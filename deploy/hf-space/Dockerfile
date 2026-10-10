# Hugging Face Space (Docker SDK) — builds DocMind straight from the public GitHub repo,
# so the Space only needs this file + README.md. Redeploy = "Factory rebuild" in Space settings.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/home/user/.cache/huggingface \
    PATH=/home/user/.local/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential libmagic1 poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 user

USER user
WORKDIR /home/user/app

RUN git clone --depth 1 https://github.com/ig-ViraL/doc-mind-backend.git /home/user/repo \
    && cp -r /home/user/repo/src/. /home/user/app/ \
    && cp /home/user/repo/requirements.txt /home/user/requirements.txt

# CPU-only torch keeps the image small enough for the free tier.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r /home/user/requirements.txt

# Bake models into the image so cold starts don't download them.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

ENV EMBEDDING_PROVIDER=local EMBEDDING_DIM=384 EXTRACT_IMAGES=false
EXPOSE 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
