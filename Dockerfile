FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Whisper models are downloaded on first use and cached here (mount a volume to keep them)
ENV HF_HOME=/models PORT=5000 WHISPER_MODEL=medium
EXPOSE 5000
# One worker: jobs live in memory and run one at a time. -t 0: long uploads are fine.
CMD gunicorn -w 1 --threads 8 -t 0 -b 0.0.0.0:${PORT} app:app
