/**
 * Cloudflare Worker: forwards every request to a single container instance
 * running the Flask app (see ../Dockerfile). One instance on purpose: jobs
 * live in that process's memory and run one at a time.
 */
import { DurableObject } from "cloudflare:workers";

interface Env {
  APP: DurableObjectNamespace<App>;
  // Optional secrets (`wrangler secret put …`). Without them, visitors enter their own keys in the form.
  ANTHROPIC_API_KEY?: string;
  TMDB_API_KEY?: string;
  WHISPER_MODEL?: string;
}

const PORT = 8080;
const INSTANCE = "standard-3"; // 2 vCPU, 8 GiB: Whisper "medium" fits comfortably
const IDLE_MS = 30 * 60 * 1000; // the results page polls, so a running job keeps it awake
const PASS_THROUGH = ["ANTHROPIC_API_KEY", "TMDB_API_KEY", "WHISPER_MODEL"] as const;

export class App extends DurableObject<Env> {
  private starting?: Promise<void>;

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
    try {
      await this.ensureRunning();
    } catch (err) {
      return new Response(`El servidor está arrancando, prueba de nuevo en un minuto. (${err})`, { status: 503 });
    }
    const url = new URL(request.url);
    const forwarded = new Request(`http://container${url.pathname}${url.search}`, request);
    return this.container.getTcpPort(PORT).fetch(forwarded);
  }
}

export default {
  fetch(request, env) {
    return env.APP.getByName("main").fetch(request);
  },
} satisfies ExportedHandler<Env>;
