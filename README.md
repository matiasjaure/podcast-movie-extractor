# 🎬 Podcast → Películas

Website that takes a Spanish podcast (**.mp3**, .m4a, .wav…), transcribes it, finds every movie mentioned
(Spanish or English titles) and shows each one with poster, synopsis, director, writers, cast, genres, runtime,
TMDB rating, trailer and **IMDb** / **Letterboxd** / TMDB links.

Results can be **saved as named lists** ("Mis listas"), reopened later, renamed, merged (add a new episode's movies to an
existing list, without duplicates), edited (remove movies) and exported as a CSV you can import into Letterboxd.

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

## Deploy

### Cloudflare (current: https://podcast-movie-extractor.matiasjaure.workers.dev)
A small Worker (`worker/index.ts`) forwards every request to one **Cloudflare Container** running the Python app
(`Dockerfile`, `standard-3`: 2 vCPU / 8 GiB). Requires the Workers Paid plan.

```bash
npm install
npx wrangler login            # or export CLOUDFLARE_API_TOKEN=…
npm run deploy                # needs Docker running: builds the image, pushes it, deploys the Worker
# optional: keys on the server (otherwise each visitor types their own in the form)
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put TMDB_API_KEY
```
- The container sleeps after 30 min without requests. The first visit after that takes ~20 s to wake it up.
- Uploads go through the Worker, which caps request bodies at 100 MB (the app allows 95 MB). A 1 h mp3 at 128 kbps is ~57 MB.
- The Whisper model is baked into the image (`--build-arg WHISPER_MODEL=small` for a smaller, faster one).

### Any Docker host (Render, Railway, Fly.io, VPS)
Deploy the repo as a **Docker** service and set `ANTHROPIC_API_KEY` and `TMDB_API_KEY` as environment variables.
The app listens on `$PORT`; health check: `/healthz`.

- **RAM**: Whisper `small` ≈ 1 GB, `medium` ≈ 2–3 GB, `large-v3` ≈ 4–5 GB.
- **Speed (CPU)**: `medium` takes roughly the length of the audio; `small` about 3× faster.
- Jobs run one at a time (queued) and live in memory, so keep a single instance.
- Serverless platforms (Vercel, Netlify functions) won't work: uploads are capped at a few MB and functions time out.

> ⚠️ The site has no login. If it is public and the keys are set on the server, anyone with the URL can spend your
> Anthropic credits. Either keep the URL private, or leave the keys unset on the server so each visitor enters their own in the form.

## Saved lists
| Where it runs | Storage |
|---|---|
| Cloudflare | SQLite inside the Worker's Durable Object (`worker/index.ts`). Browsing lists never wakes the container. |
| Local / Docker | SQLite file `data/lists.db` (`DATA_DIR` to move it; mount it as a volume in Docker). |

API (same on both): `GET/POST /api/lists`, `GET/PUT/DELETE /api/lists/<id>`. A list is `{name, source, movies[]}`;
each movie keeps the full details, so saved lists don't need TMDB to be displayed again.

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
