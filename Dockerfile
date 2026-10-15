FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/opt/hf

WORKDIR /app

# CPU-версия torch: без CUDA образ в разы меньше
COPY service/requirements.txt service/requirements.txt
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu torch \
 && pip install -r service/requirements.txt

# модель эмбеддингов кладём в образ, чтобы старт не зависел от huggingface.co
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"

COPY index.html admin.html embed-example.html ./
COPY service/*.py service/

# данные (SQLite + Chroma) живут в томе /data
ENV CHAT_LOG_DB=/data/chat_log.db CHROMA_PATH=/data/chroma HF_HUB_OFFLINE=1
VOLUME /data

WORKDIR /app/service
EXPOSE 8000
# один воркер: SQLite и Chroma не рассчитаны на несколько процессов
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
