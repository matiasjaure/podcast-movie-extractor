#!/usr/bin/env python3
"""Web UI for podcast_movies.

Local:      python app.py   -> http://127.0.0.1:5000
Production: gunicorn -w 1 --threads 8 -t 0 -b 0.0.0.0:$PORT app:app   (see Dockerfile)
"""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import podcast_movies as pm

ROOT = Path(__file__).parent / "out"
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm", ".mp4", ".txt"}
MODELS = {"tiny", "base", "small", "medium", "large-v3"}
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "medium")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", 300)) * 1024 * 1024
jobs: dict[str, dict] = {}
run_lock = threading.Lock()  # one pipeline at a time: Whisper is CPU/RAM heavy


def worker(job_id: str, source: Path, model: str, keys: dict):
    job = jobs[job_id]
    try:
        if run_lock.locked():
            job.update(status="En cola, esperando a que termine otro audio…")
        with run_lock:
            movies = pm.run_pipeline(source, ROOT / job_id, model, lambda m: job.update(status=m), **keys)
        job.update(state="done", status="Listo", movies=[asdict(m) | {"imdb_url": m.imdb_url, "letterboxd_url": m.letterboxd_url} for m in movies])
    except Exception as e:  # surface any failure in the UI
        job.update(state="error", status=f"{type(e).__name__}: {e}")


@app.post("/api/jobs")
def create_job():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Falta el archivo"), 400
    name = secure_filename(f.filename) or "episodio.mp3"
    if Path(name).suffix.lower() not in AUDIO_EXT:
        return jsonify(error="Formato no soportado. Sube un .mp3, .m4a, .wav… o un .txt"), 400
    # Keys typed in the form apply to this job only; never written to os.environ.
    keys = {"anthropic_key": request.form.get("anthropic_api_key") or None,
            "tmdb_key": request.form.get("tmdb_api_key") or None}
    for env, k in (("ANTHROPIC_API_KEY", "anthropic_key"), ("TMDB_API_KEY", "tmdb_key")):
        if not (keys[k] or os.environ.get(env)):
            return jsonify(error=f"Falta {env} (en el servidor o en el formulario)"), 400
    model = request.form.get("model") if request.form.get("model") in MODELS else DEFAULT_MODEL
    job_id = uuid.uuid4().hex
    dest = ROOT / job_id
    dest.mkdir(parents=True)
    source = dest / name
    f.save(source)
    jobs[job_id] = {"state": "running", "status": "Empezando…", "name": Path(name).stem}
    threading.Thread(target=worker, args=(job_id, source, model, keys), daemon=True).start()
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


@app.get("/")
def index():
    keys_on_server = bool(os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("TMDB_API_KEY"))
    return PAGE.replace("__KEYS_HIDDEN__", "hidden" if keys_on_server else "open")


PAGE = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Podcast → Películas</title>
<style>
:root{--bg:#14181c;--card:#1f252b;--fg:#e8ecef;--mute:#8a96a0;--acc:#00c030;--imdb:#f5c518}
@media(prefers-color-scheme:light){:root{--bg:#f4f5f7;--card:#fff;--fg:#1a1d21;--mute:#667}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}h1{margin:0 0 4px}p.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--card);border-radius:12px;padding:20px;margin-bottom:20px}
.drop{border:2px dashed var(--mute);border-radius:10px;padding:28px;text-align:center;cursor:pointer}
.drop.over{border-color:var(--acc)}label{display:block;margin:12px 0 4px;color:var(--mute);font-size:14px}
input[type=text],input[type=password],select{width:100%;padding:8px;border-radius:6px;border:1px solid var(--mute);background:transparent;color:inherit}
button,.btn{background:var(--acc);color:#000;border:0;border-radius:8px;padding:10px 18px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button:disabled{opacity:.5}details{margin-top:12px;color:var(--mute)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid #8884}
a{color:var(--acc)}a.imdb{color:var(--imdb)}.actions{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.btn.alt{background:transparent;color:var(--fg);border:1px solid var(--mute)}.err{color:#ff6b6b}.mute{color:var(--mute)}
@media(max-width:600px){.hide-s{display:none}}
</style>
<main>
<h1>🎬 Podcast → Películas</h1><p class="sub">Sube un .mp3 o .m4a en español y obtén las películas mencionadas con enlaces a IMDb y Letterboxd.</p>
<form id="f" class="card">
 <div class="drop" id="drop">Arrastra el audio aquí o haz clic<br><span class="mute" id="fname"></span>
 <input type="file" id="file" name="file" accept=".mp3,.m4a,.wav,.ogg,.flac,audio/*,.txt" hidden></div>
 <label>Modelo de transcripción</label>
 <select name="model"><option value="small">small (rápido)</option><option value="medium" selected>medium (recomendado)</option><option value="large-v3">large-v3 (más preciso, lento)</option></select>
 <details __KEYS_HIDDEN__><summary>Claves API</summary>
  <label>ANTHROPIC_API_KEY</label><input type="password" name="anthropic_api_key" autocomplete="off">
  <label>TMDB_API_KEY</label><input type="password" name="tmdb_api_key" autocomplete="off"></details>
 <p><button id="go">Procesar</button> <span id="st" class="mute"></span></p>
</form>
<section id="res"></section>
</main>
<script>
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('drop').onclick=()=>$('file').click();
$('file').onchange=()=>$('fname').textContent=$('file').files[0]?.name||'';
['dragover','dragleave','drop'].forEach(e=>$('drop').addEventListener(e,ev=>{ev.preventDefault();$('drop').classList.toggle('over',e=='dragover');
 if(e=='drop'){$('file').files=ev.dataTransfer.files;$('file').onchange()}}));
$('f').onsubmit=async ev=>{ev.preventDefault();$('res').innerHTML='';$('go').disabled=true;$('st').className='mute';$('st').textContent='Subiendo…';
 const r=await fetch('/api/jobs',{method:'POST',body:new FormData($('f'))});const j=await r.json();
 if(!r.ok){$('st').className='err';$('st').textContent=j.error;$('go').disabled=false;return}
 const t=setInterval(async()=>{const s=await (await fetch('/api/jobs/'+j.id)).json();$('st').textContent=s.status;
  if(s.state=='done'){clearInterval(t);$('go').disabled=false;show(j.id,s)}
  if(s.state=='error'){clearInterval(t);$('go').disabled=false;$('st').className='err'}},1500)};
function show(id,s){const ok=s.movies.filter(m=>m.matched),bad=s.movies.filter(m=>!m.matched);
 let h=`<div class="card"><b>${ok.length} películas encontradas</b>
 <div class="actions"><a class="btn" href="/files/${id}/movies.csv">Descargar CSV para Letterboxd</a>
 <a class="btn alt" target="_blank" rel="noopener" href="https://letterboxd.com/list/new/">Abrir Letterboxd → Import</a>
 <a class="btn alt" href="/files/${id}/movies.md">Markdown</a><a class="btn alt" href="/files/${id}/transcript.txt">Transcripción</a></div>
 <table><tr><th>Película</th><th>Año</th><th>IMDb</th><th>Letterboxd</th><th class="hide-s">Mencionada como</th></tr>`;
 for(const m of ok)h+=`<tr><td><b>${esc(m.title)}</b></td><td>${m.year||''}</td>
  <td>${m.imdb_id?`<a class="imdb" target="_blank" rel="noopener" href="${m.imdb_url}">${m.imdb_id}</a>`:'—'}</td>
  <td>${m.imdb_id?`<a target="_blank" rel="noopener" href="${m.letterboxd_url}">ver</a>`:'—'}</td><td class="hide-s mute">${esc(m.mentioned_as)}</td></tr>`;
 h+='</table>';
 if(bad.length)h+=`<h3>Sin identificar</h3><ul>${bad.map(m=>`<li>${esc(m.mentioned_as||m.title)} <span class="mute">— ${esc(m.context)}</span></li>`).join('')}</ul>`;
 $('res').innerHTML=h+'</div>'}
</script></html>"""

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
