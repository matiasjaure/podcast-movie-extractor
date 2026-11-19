# 🎬 Podcast → Películas

Website that takes a Spanish podcast (**.mp3**, .m4a, .wav…), transcribes it, finds every movie mentioned
(Spanish or English titles) and gives you a list with **IMDb** and **Letterboxd** links plus a CSV you can import as a Letterboxd list.

```
audio ──faster-whisper (es)──▶ transcript ──Claude──▶ titles ──TMDB──▶ IMDb ids ──▶ table · movies.csv · movies.md
```

## What you need
| | Where | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com | a few cents per episode |
| `TMDB_API_KEY` (v3 key or v4 read token) | themoviedb.org → Settings → API | free |

Transcription runs inside the app (faster-whisper), no transcription API needed.

## Run locally
```bash
cp .env.example .env      # add your keys
./run.sh                  # http://127.0.0.1:5000
```
or with Docker: `docker compose up --build` (http://localhost:5000).

## Deploy (Render, Railway, Fly.io, any VPS)
Deploy the repo as a **Docker** service and set `ANTHROPIC_API_KEY` and `TMDB_API_KEY` as environment variables.
The app listens on `$PORT`; health check: `/healthz`.

- **RAM**: Whisper `small` ≈ 1 GB, `medium` ≈ 2–3 GB, `large-v3` ≈ 4–5 GB. Pick the plan accordingly, or set `WHISPER_MODEL=small`.
- **Speed (CPU)**: `medium` takes roughly the length of the audio; `small` about 3× faster.
- Jobs run one at a time (queued) and live in memory, so keep a single instance.
- Serverless platforms (Vercel, Netlify functions) won't work: uploads are capped at a few MB and functions time out.

> ⚠️ The site has no login. If it is public and the keys are set on the server, anyone with the URL can spend your
> Anthropic credits. Either keep the URL private, or leave the keys unset on the server so each visitor enters their own in the form.

## Letterboxd
Letterboxd's API is invite-only, so lists can't be created automatically. After processing, click
**Descargar CSV para Letterboxd**, then go to letterboxd.com/list/new → **Import** and upload it (matches by IMDb/TMDB id).

## Command line
```bash
python podcast_movies.py episodio.mp3        # -> out/episodio/{transcript.txt,movies.md,movies.csv,movies.json}
python podcast_movies.py transcript.txt      # skip transcription
```

## Tests
```bash
python -m unittest discover tests
```
