FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the Whisper model into the image so a cold start doesn't download it.
ARG WHISPER_MODEL=medium
ENV HF_HOME=/models WHISPER_MODEL=${WHISPER_MODEL} PORT=5000
RUN python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_MODEL'], device='cpu', compute_type='int8')"
COPY app.py podcast_movies.py ./
EXPOSE 5000
# One worker: jobs live in memory and run one at a time. -t 0: long uploads are fine.
CMD gunicorn -w 1 --threads 8 -t 0 -b 0.0.0.0:${PORT} app:app
