/**
 * Cloudflare Worker in front of the Python app.
 *
 * - static/index.html is bundled in (a Text module, see wrangler.jsonc "rules") and served at "/".
 * - /api/config and /api/lists* are answered here, from the Durable Object's
 *   SQLite storage, so saved lists persist and browsing them never wakes the container.
 * - Everything else (/api/jobs, /files, /api/check-keys) goes to a single container
 *   instance per deployed version, running the Flask app (see ../Dockerfile). One
 *   instance on purpose: jobs live in that process's memory and run one at a time.
 *
 * The lists API mirrors the Flask implementation in ../app.py (used locally).
 */
import { DurableObject } from "cloudflare:workers";
import PAGE from "../static/index.html";

interface Env {
  APP: DurableObjectNamespace<App>;
  CF_VERSION_METADATA: WorkerVersionMetadata;
  // Optional secrets (`wrangler secret put …`). Without them, visitors enter their own keys in the form.
  ANTHROPIC_API_KEY?: string;
  TMDB_API_KEY?: string;
  WHISPER_MODEL?: string;
}

const PORT = 8080;
const INSTANCE = "standard-3"; // 2 vCPU, 8 GiB: Whisper "medium" fits comfortably
const IDLE_MS = 30 * 60 * 1000; // the results page polls, so a running job keeps it awake
const PASS_THROUGH = ["ANTHROPIC_API_KEY", "TMDB_API_KEY", "WHISPER_MODEL"] as const;
const MAX_NAME = 120;
const MAX_MOVIES = 1000;
const MAX_BYTES = 2_000_000;

type Movie = Record<string, unknown>;
type ListRow = { id: string; name: string; source: string; movies: string; created_at: string; updated_at: string };

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8" } });

function validate(body: unknown, partial: boolean): { fields?: Record<string, string>; error?: string } {
  if (!body || typeof body !== "object" || Array.isArray(body)) return { error: "JSON inválido" };
  const b = body as Record<string, unknown>;
  const fields: Record<string, string> = {};
  if ("name" in b || !partial) {
    if (typeof b.name !== "string" || !b.name.trim() || b.name.length > MAX_NAME)
      return { error: `El nombre es obligatorio (máx. ${MAX_NAME} caracteres)` };
    fields.name = b.name.trim();
  }
  if ("source" in b) fields.source = String(b.source ?? "").slice(0, MAX_NAME);
  if ("movies" in b || !partial) {
    const m = b.movies;
    if (!Array.isArray(m) || m.length > MAX_MOVIES || !m.every((x) => x && typeof x === "object" && !Array.isArray(x)))
      return { error: `«movies» debe ser una lista de hasta ${MAX_MOVIES} películas` };
    fields.movies = JSON.stringify(m);
    if (fields.movies.length > MAX_BYTES) return { error: "Lista demasiado grande" };
  }
  return { fields };
}

function summary(row: ListRow) {
  const movies = JSON.parse(row.movies) as Movie[];
  return {
    id: row.id, name: row.name, source: row.source, created_at: row.created_at, updated_at: row.updated_at,
    count: movies.filter((m) => m.matched !== false).length,
    posters: movies.map((m) => m.poster_url).filter((p): p is string => typeof p === "string" && !!p).slice(0, 4),
  };
}

const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

export class App extends DurableObject<Env> {
  private starting?: Promise<void>;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS lists (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
      movies TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`);
  }

  private async lists(request: Request, id: string | undefined): Promise<Response> {
    const sql = this.ctx.storage.sql;
    const find = () => sql.exec<ListRow>("SELECT * FROM lists WHERE id = ?", id).toArray()[0];
    const body = async () => request.json().catch(() => null);

    if (!id) {
      if (request.method === "GET") {
        const rows = sql.exec<ListRow>("SELECT * FROM lists ORDER BY updated_at DESC").toArray();
        return json({ lists: rows.map(summary) });
      }
      if (request.method === "POST") {
        const { fields, error } = validate(await body(), false);
        if (error) return json({ error }, 400);
        const newId = crypto.randomUUID().replaceAll("-", "");
        const ts = now();
        sql.exec("INSERT INTO lists (id, name, source, movies, created_at, updated_at) VALUES (?,?,?,?,?,?)",
          newId, fields!.name, fields!.source ?? "", fields!.movies, ts, ts);
        return json({ id: newId }, 201);
      }
      return json({ error: "Método no permitido" }, 405);
    }

    if (!find()) return json({ error: "Lista no encontrada" }, 404);
    if (request.method === "GET") {
      const { movies, ...rest } = find()!;
      return json({ ...rest, movies: JSON.parse(movies) });
    }
    if (request.method === "PUT") {
      const { fields, error } = validate(await body(), true);
      if (error) return json({ error }, 400);
      const set = { ...fields, updated_at: now() };
      const cols = Object.keys(set); // whitelisted by validate()
      sql.exec(`UPDATE lists SET ${cols.map((c) => `${c} = ?`).join(", ")} WHERE id = ?`, ...Object.values(set), id);
      return json({ ok: true });
    }
    if (request.method === "DELETE") {
      sql.exec("DELETE FROM lists WHERE id = ?", id);
      return json({ ok: true });
    }
    return json({ error: "Método no permitido" }, 405);
  }

  private get container(): Container {
    return this.ctx.container!;
  }

  private async ensureRunning(): Promise<void> {
    if (this.container.running) return;
    this.starting ??= this.start().finally(() => (this.starting = undefined));
    await this.starting;
  }

  private async start(): Promise<void> {
    const c = this.container;
    // Workers caps request bodies at 100 MB, so keep the app's own limit below that.
    const env: Record<string, string> = { PORT: String(PORT), MAX_UPLOAD_MB: "95" };
    for (const key of PASS_THROUGH) {
      const value = this.env[key];
      if (value) env[key] = value;
    }
    c.start({ image: c.images.base, instance: INSTANCE, enableInternet: true, env });
    c.monitor().catch((err) => console.error("container exited", err));
    await c.setInactivityTimeout(IDLE_MS);

    for (let i = 0; i < 180; i++) {
      try {
        const res = await c.getTcpPort(PORT).fetch("http://container/healthz");
        if (res.ok) return;
      } catch {
        // not listening yet
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("Container did not become healthy in time");
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/api\/lists(?:\/([\w-]+))?\/?$/);
    if (m) return this.lists(request, m[1]);
    if (url.pathname === "/api/config")
      return json({ keys_on_server: !!(this.env.ANTHROPIC_API_KEY && this.env.TMDB_API_KEY) });

    try {
      await this.ensureRunning();
    } catch (err) {
      return new Response(`El servidor está arrancando, prueba de nuevo en un minuto. (${err})`, { status: 503 });
    }
    const forwarded = new Request(`http://container${url.pathname}${url.search}`, request);
    return this.container.getTcpPort(PORT).fetch(forwarded);
  }
}

export default {
  fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (request.method === "GET" && (pathname === "/" || pathname === "/index.html"))
      return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" } });
    // Saved lists live in the "main" object's storage. The container runs in an object
    // named after the deployed version: a running container keeps its old image until
    // it goes idle, so this makes each deploy take effect on the next request. The
    // previous version's instance gets no more traffic and sleeps after IDLE_MS.
    const lists = pathname.startsWith("/api/lists") || pathname === "/api/config";
    const name = lists ? "main" : `app-${env.CF_VERSION_METADATA.id}`;
    return env.APP.getByName(name).fetch(request);
  },
} satisfies ExportedHandler<Env>;
