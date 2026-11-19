#!/usr/bin/env python3
"""Web UI for podcast_movies.

Local:      python app.py   -> http://127.0.0.1:5000
Production: gunicorn -w 1 --threads 8 -t 0 -b 0.0.0.0:$PORT app:app   (see Dockerfile)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import urllib.error
from contextlib import contextmanager
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import podcast_movies as pm

HERE = Path(__file__).parent
ROOT = HERE / "out"
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm", ".mp4", ".txt"}
MODELS = {"tiny", "base", "small", "medium", "large-v3"}
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "medium")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", 300)) * 1024 * 1024
jobs: dict[str, dict] = {}
run_lock = threading.Lock()  # one pipeline at a time: Whisper is CPU/RAM heavy


TRANSCRIPTS = ROOT / "transcripts"  # cache: retrying the same audio skips Whisper


def friendly_error(e: Exception) -> str:
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return ("Anthropic rechazó la clave (401). Usa una API key de console.anthropic.com → API Keys "
                "(empieza por «sk-ant-api»), sin espacios.")
    if isinstance(e, anthropic.NotFoundError):
        return f"La clave de Anthropic no tiene acceso al modelo {pm.EXTRACT_MODEL} (404)."
    if isinstance(e, anthropic.APIConnectionError):
        return "No se pudo conectar con Anthropic. Inténtalo de nuevo."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "La clave de Anthropic no tiene permiso para usar este modelo (403)."
    if isinstance(e, anthropic.APIStatusError):
        return f"Error de Anthropic ({e.status_code}): {e.message}"
    if isinstance(e, urllib.error.HTTPError) and "themoviedb" in (e.url or ""):
        if e.code == 401:
            return ("TMDB rechazó la clave (401). Usa la «API Key» o el «API Read Access Token» "
                    "de themoviedb.org → Ajustes → API.")
        return f"Error de TMDB ({e.code})."
    return f"{type(e).__name__}: {e}"


def worker(job_id: str, source: Path, model: str, keys: dict, cached: Path):
    job = jobs[job_id]
    out = ROOT / job_id
    try:
        if run_lock.locked():
            job.update(status="En cola, esperando a que termine otro audio…")
        with run_lock:
            if cached.exists():
                shutil.copy(cached, out / "transcript.txt")  # run_pipeline reuses it
            movies = pm.run_pipeline(source, out, model, lambda m: job.update(status=m), **keys)
        job.update(state="done", status="Listo", movies=[m.to_dict() for m in movies])
    except Exception as e:  # surface any failure in the UI
        job.update(state="error", status=friendly_error(e))
    finally:
        if (out / "transcript.txt").exists() and not cached.exists():
            TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
            shutil.copy(out / "transcript.txt", cached)


def check_keys(anthropic_key: str | None, tmdb_key: str | None) -> str | None:
    """Catch keys pasted into the wrong field before spending a transcription on them."""
    if anthropic_key and not anthropic_key.startswith("sk-ant-"):
        hint = " Parece un token de TMDB: ¿están las claves cambiadas?" if anthropic_key.startswith("eyJ") else ""
        return "La clave de Anthropic debe empezar por «sk-ant-»." + hint
    if tmdb_key and tmdb_key.startswith("sk-ant-"):
        return "En el campo de TMDB hay una clave de Anthropic: ¿están las claves cambiadas?"
    return None


def form_keys() -> tuple[dict, str | None]:
    """Keys typed in the form (this request only; never written to os.environ), plus a
    validation error. The keys are checked against both APIs so a bad one fails in
    seconds instead of after a long transcription."""
    keys = {"anthropic_key": request.form.get("anthropic_api_key", "").strip() or None,
            "tmdb_key": request.form.get("tmdb_api_key", "").strip() or None}
    for env, k in (("ANTHROPIC_API_KEY", "anthropic_key"), ("TMDB_API_KEY", "tmdb_key")):
        if not (keys[k] or os.environ.get(env)):
            return keys, f"Falta {env} (en el servidor o en el formulario)"
    if err := check_keys(**keys):
        return keys, err
    try:
        pm.check_anthropic_key(keys["anthropic_key"])
        pm.check_tmdb_key(keys["tmdb_key"] or os.environ["TMDB_API_KEY"])
    except Exception as e:
        return keys, friendly_error(e)
    return keys, None


@app.post("/api/check-keys")
def check_keys_route():
    _, err = form_keys()
    return (jsonify(error=err), 400) if err else jsonify(ok=True)


@app.post("/api/jobs")
def create_job():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Falta el archivo"), 400
    name = secure_filename(f.filename) or "episodio.mp3"
    if Path(name).suffix.lower() not in AUDIO_EXT:
        return jsonify(error="Formato no soportado. Sube un .mp3, .m4a, .wav… o un .txt"), 400
    keys, err = form_keys()
    if err:
        return jsonify(error=err), 400
    model = request.form.get("model") if request.form.get("model") in MODELS else DEFAULT_MODEL
    job_id = uuid.uuid4().hex
    dest = ROOT / job_id
    dest.mkdir(parents=True)
    source = dest / name
    f.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:32]
    cached = TRANSCRIPTS / f"{digest}-{model}.txt"
    jobs[job_id] = {"state": "running", "status": "Empezando…", "name": Path(name).stem}
    threading.Thread(target=worker, args=(job_id, source, model, keys, cached), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    return jsonify(jobs.get(job_id) or {"state": "error", "status": "Trabajo no encontrado"})


@app.get("/files/<job_id>/<name>")
def files(job_id, name):
    if name not in ("movies.csv", "movies.md", "movies.json", "transcript.txt"):
        return "", 404
    return send_from_directory(ROOT / secure_filename(job_id), name, as_attachment=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"Archivo demasiado grande (máx. {app.config['MAX_CONTENT_LENGTH'] // 2**20} MB)"), 413


@app.get("/healthz")
def healthz():
    return "ok"



@app.get("/api/config")
def config():
    return jsonify(keys_on_server=bool(os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("TMDB_API_KEY")))


# ----------------------------------------------------------------- saved lists
# Same contract as worker/index.ts, which serves these routes on Cloudflare
# (there the container's disk is ephemeral, so lists live in the Durable Object).
MAX_NAME, MAX_MOVIES, MAX_BYTES = 120, 1000, 2_000_000
db_lock = threading.Lock()


@contextmanager
def db():
    """Serialized connection that commits on success and always closes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DATA_DIR / "lists.db")
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("""CREATE TABLE IF NOT EXISTS lists (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                movies TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            with conn:
                yield conn
        finally:
            conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate(body, partial: bool):
    """Returns (fields, error)."""
    if not isinstance(body, dict):
        return None, "JSON inválido"
    out = {}
    if "name" in body or not partial:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
            return None, f"El nombre es obligatorio (máx. {MAX_NAME} caracteres)"
        out["name"] = name.strip()
    if "source" in body:
        out["source"] = str(body.get("source") or "")[:MAX_NAME]
    if "movies" in body or not partial:
        movies = body.get("movies")
        if not isinstance(movies, list) or len(movies) > MAX_MOVIES or not all(isinstance(m, dict) for m in movies):
            return None, f"«movies» debe ser una lista de hasta {MAX_MOVIES} películas"
        out["movies"] = json.dumps(movies, ensure_ascii=False)
        if len(out["movies"]) > MAX_BYTES:
            return None, "Lista demasiado grande"
    return out, None


def list_summary(row) -> dict:
    movies = json.loads(row["movies"])
    return {"id": row["id"], "name": row["name"], "source": row["source"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "count": sum(1 for m in movies if m.get("matched") is not False),
            "posters": [m["poster_url"] for m in movies if m.get("poster_url")][:4]}


@app.get("/api/lists")
def lists_index():
    with db() as conn:
        rows = conn.execute("SELECT * FROM lists ORDER BY updated_at DESC").fetchall()
    return jsonify(lists=[list_summary(r) for r in rows])


@app.post("/api/lists")
def lists_create():
    fields, err = validate(request.get_json(silent=True), partial=False)
    if err:
        return jsonify(error=err), 400
    list_id, ts = uuid.uuid4().hex, now()
    with db() as conn:
        conn.execute("INSERT INTO lists (id, name, source, movies, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (list_id, fields["name"], fields.get("source", ""), fields["movies"], ts, ts))
    return jsonify(id=list_id), 201


@app.get("/api/lists/<list_id>")
def lists_get(list_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        return jsonify(error="Lista no encontrada"), 404
    return jsonify({k: row[k] for k in ("id", "name", "source", "created_at", "updated_at")} | {"movies": json.loads(row["movies"])})


@app.put("/api/lists/<list_id>")
def lists_update(list_id):
    fields, err = validate(request.get_json(silent=True), partial=True)
    if err:
        return jsonify(error=err), 400
    fields["updated_at"] = now()
    with db() as conn:
        cur = conn.execute(f"UPDATE lists SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                           (*fields.values(), list_id))
    if not cur.rowcount:
        return jsonify(error="Lista no encontrada"), 404
    return jsonify(ok=True)


@app.delete("/api/lists/<list_id>")
def lists_delete(list_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    if not cur.rowcount:
        return jsonify(error="Lista no encontrada"), 404
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
