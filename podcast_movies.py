#!/usr/bin/env python3
"""Spanish podcast (.mp3/.m4a) -> transcript -> movies mentioned -> IMDb/Letterboxd lists.

Pipeline:
  1. Transcribe with faster-whisper (local, language forced to Spanish).
  2. Ask Claude to extract every film mentioned (Spanish or English titles).
  3. Resolve each one on TMDB (gives the IMDb id) and write:
       movies.md   nicely formatted list with IMDb + Letterboxd links
       movies.csv  Letterboxd import file (letterboxd.com/list/new -> Import)
       movies.json raw data, including titles that could not be matched

Env vars: ANTHROPIC_API_KEY, TMDB_API_KEY (v3 key or v4 read token).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path



def _load_dotenv() -> None:
    """Load KEY=value lines from a .env file next to this script (real env vars win)."""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_dotenv()
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-5-5")
CHUNK_CHARS = 30_000

EXTRACT_PROMPT = """Este es un fragmento de la transcripción automática de un podcast en español.
Extrae TODAS las películas que se mencionan (no series, a menos que se hable de una miniserie
estrenada como película). Los títulos pueden estar en español o en inglés, y la transcripción
puede tener errores fonéticos: corrígelos si estás razonablemente seguro de la película.

Responde SOLO con un array JSON, sin texto extra. Cada elemento:
{"title_original": "título original (normalmente inglés) o null si no lo sabes",
 "title_mentioned": "como se dijo en el podcast",
 "year": año de estreno como entero o null,
 "context": "frase corta (<=120 caracteres) de qué se dice de ella"}

Si no hay películas, responde []."""


# ----------------------------------------------------------------- transcription
_models: dict = {}


def transcribe(audio: Path, model_size: str, progress=lambda msg: None) -> str:
    from faster_whisper import WhisperModel

    if model_size not in _models:
        progress(f"Cargando modelo {model_size}…")
        _models[model_size] = WhisperModel(model_size, compute_type="auto")
    model = _models[model_size]
    segments, info = model.transcribe(str(audio), language="es", vad_filter=True)
    total = info.duration or 1
    lines = []
    for seg in segments:
        lines.append(seg.text.strip())
        progress(f"Transcribiendo… {min(seg.end / total, 1):.0%}")
    return "\n".join(lines)


# ----------------------------------------------------------------- extraction
def chunks(text: str, size: int = CHUNK_CHARS):
    buf, n = [], 0
    for line in text.splitlines():
        if n + len(line) > size and buf:
            yield "\n".join(buf)
            buf, n = [], 0
        buf.append(line)
        n += len(line) + 1
    if buf:
        yield "\n".join(buf)


def parse_json_array(raw: str) -> list[dict]:
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]


def extract_movies(transcript: str, progress=lambda msg: None, api_key: str | None = None) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)  # None -> ANTHROPIC_API_KEY
    found: dict[str, dict] = {}
    parts = list(chunks(transcript))
    for i, part in enumerate(parts, 1):
        progress(f"Buscando películas en el texto… {i}/{len(parts)}")
        msg = client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": f"{EXTRACT_PROMPT}\n\n<transcripcion>\n{part}\n</transcripcion>"}],
        )
        for item in parse_json_array("".join(b.text for b in msg.content if b.type == "text")):
            name = item.get("title_original") or item.get("title_mentioned")
            if name:
                found.setdefault(name.casefold().strip(), item)
    return list(found.values())


# ----------------------------------------------------------------- TMDB
class Tmdb:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, key: str):
        self.key = key

    def get(self, path: str, **params):
        headers = {}
        if len(self.key) > 40:  # v4 read access token
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            return json.load(r)

    def search(self, title: str, year: int | None):
        for lang in ("es-ES", "en-US"):
            params = {"query": title, "language": lang, "include_adult": "false"}
            if year:
                params["year"] = year
            results = self.get("/search/movie", **params).get("results", [])
            if not results and year:  # year guess from the model may be off
                params.pop("year")
                results = self.get("/search/movie", **params).get("results", [])
            if results:
                return results[0]
        return None

    def imdb_id(self, tmdb_id: int) -> str | None:
        return self.get(f"/movie/{tmdb_id}/external_ids").get("imdb_id") or None


@dataclass
class Movie:
    title: str
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    mentioned_as: str = ""
    context: str = ""
    matched: bool = True
    original_title: str = field(default="")

    @property
    def imdb_url(self):
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else ""

    @property
    def letterboxd_url(self):
        # letterboxd.com/imdb/<id> redirects to the film page
        return f"https://letterboxd.com/imdb/{self.imdb_id}/" if self.imdb_id else ""


def resolve(items: list[dict], tmdb: Tmdb, progress=lambda msg: None) -> list[Movie]:
    movies: list[Movie] = []
    seen: set[int] = set()
    for n, it in enumerate(items, 1):
        progress(f"Buscando en TMDB/IMDb… {n}/{len(items)}")
        mentioned = it.get("title_mentioned") or ""
        year = it.get("year") if isinstance(it.get("year"), int) else None
        hit = None
        for cand in (it.get("title_original"), mentioned):
            if cand and (hit := tmdb.search(cand, year)):
                break
        if not hit:
            movies.append(Movie(title=it.get("title_original") or mentioned, year=year,
                                mentioned_as=mentioned, context=it.get("context") or "", matched=False))
            continue
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        movies.append(Movie(
            title=hit.get("title") or hit.get("original_title"),
            original_title=hit.get("original_title", ""),
            year=int(hit["release_date"][:4]) if hit.get("release_date") else year,
            tmdb_id=hit["id"], imdb_id=tmdb.imdb_id(hit["id"]),
            mentioned_as=mentioned, context=it.get("context") or "",
        ))
    return sorted(movies, key=lambda m: (not m.matched, m.title.casefold()))


# ----------------------------------------------------------------- output
def write_outputs(movies: list[Movie], out: Path, name: str):
    ok = [m for m in movies if m.matched]
    bad = [m for m in movies if not m.matched]

    md = [f"# Películas de «{name}»", "", f"{len(ok)} películas encontradas.", "",
          "| # | Película | Año | IMDb | Letterboxd | Mencionada como |", "|---|---|---|---|---|---|"]
    for i, m in enumerate(ok, 1):
        title = m.title if m.title == m.original_title or not m.original_title else f"{m.title} ({m.original_title})"
        imdb = f"[{m.imdb_id}]({m.imdb_url})" if m.imdb_id else "—"
        lb = f"[ver]({m.letterboxd_url})" if m.imdb_id else "—"
        md.append(f"| {i} | **{title}** | {m.year or ''} | {imdb} | {lb} | {m.mentioned_as} |")
    if bad:
        md += ["", "## Sin identificar (revisar a mano)", ""]
        md += [f"- {m.mentioned_as or m.title} — _{m.context}_" for m in bad]
    (out / "movies.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # Letterboxd importer reads imdbID / tmdbID / Title / Year columns.
    with (out / "movies.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Title", "Year", "imdbID", "tmdbID"])
        for m in ok:
            w.writerow([m.title, m.year or "", m.imdb_id or "", m.tmdb_id or ""])

    (out / "movies.json").write_text(json.dumps([asdict(m) for m in movies], ensure_ascii=False, indent=2),
                                     encoding="utf-8")


def run_pipeline(source: Path, out: Path, model: str = "medium", progress=lambda msg: None,
                 anthropic_key: str | None = None, tmdb_key: str | None = None) -> list[Movie]:
    """audio (.mp3/.m4a/...) or transcript (.txt) -> files in `out`. Returns the movies.
    Keys default to ANTHROPIC_API_KEY / TMDB_API_KEY from the environment."""
    out.mkdir(parents=True, exist_ok=True)
    tpath = out / "transcript.txt"
    if source.suffix == ".txt":
        transcript = source.read_text(encoding="utf-8")
    elif tpath.exists():
        progress("Reusando transcripción anterior…")
        transcript = tpath.read_text(encoding="utf-8")
    else:
        transcript = transcribe(source, model, progress)
        tpath.write_text(transcript, encoding="utf-8")
    items = extract_movies(transcript, progress, anthropic_key)
    movies = resolve(items, Tmdb(tmdb_key or os.environ["TMDB_API_KEY"]), progress)
    write_outputs(movies, out, source.stem)
    return movies


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", type=Path, help="audio file: .mp3, .m4a, .wav… (or a .txt transcript to skip transcription)")
    ap.add_argument("-o", "--out", type=Path, help="output dir (default: ./out/<file name>)")
    ap.add_argument("--model", default="medium", help="faster-whisper model: small, medium, large-v3 (default medium)")
    args = ap.parse_args()

    for var in ("ANTHROPIC_API_KEY", "TMDB_API_KEY"):
        if not os.environ.get(var):
            sys.exit(f"Missing env var {var}")

    out = args.out or Path("out") / args.audio.stem
    run_pipeline(args.audio, out, args.model, lambda m: print(f"\r{m:60}", end="", file=sys.stderr))
    print(f"\nDone: {out}/movies.md, movies.csv, movies.json", file=sys.stderr)


if __name__ == "__main__":
    main()
