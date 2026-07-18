/**
 * Telegram webhook relay for the optional real-time intake mode.
 *
 * A minimal Cloudflare Worker: Telegram delivers each update here, the Worker
 * checks the webhook secret and then does ONE thing — trigger a
 * `workflow_dispatch` on `telegram-intake.yml`, passing the raw update through
 * as an input. All logic (allowlist, commands, issue creation) stays in the
 * repository and runs in GitHub Actions; this Worker is pure delivery and holds
 * no state.
 *
 * Why a relay at all: GitHub Actions cannot receive Telegram webhooks, and once
 * a webhook is set Telegram stops serving updates over getUpdates (the poll
 * mode then answers 409 and no-ops). The Worker is the deliberately tiny step
 * outside GitHub that turns the ~10-minute poll into a seconds-level response.
 *
 * Worker secrets (Cloudflare: Settings → Variables and Secrets):
 *   WEBHOOK_SECRET    – a secret you choose; pass the SAME value as
 *                       `secret_token` on the Telegram setWebhook call. Requests
 *                       without the matching header are rejected.
 *   GITHUB_PAT        – fine-grained PAT, THIS repo only, permission
 *                       "Actions: Read and write".
 *   GITHUB_REPOSITORY – e.g. "malkreide/mcp-continuous-auditor".
 *   GITHUB_BRANCH     – optional; dispatch ref (default: main).
 *
 * Setup and teardown (deleteWebhook) are in docs/telegram/standalone-intake.md.
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Telegram webhook relay", { status: 200 });
    }
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.WEBHOOK_SECRET || secret !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const dispatch = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/telegram-intake.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_PAT}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
          "User-Agent": "mcp-continuous-auditor-telegram-relay",
        },
        body: JSON.stringify({
          ref: env.GITHUB_BRANCH || "main",
          inputs: {
            update: JSON.stringify(update),
            update_id: String(update.update_id ?? ""),
          },
        }),
      },
    );

    // A non-2xx makes Telegram retry delivery instead of losing the update
    // (e.g. a GitHub outage or an expired PAT).
    if (!dispatch.ok) {
      return new Response("dispatch failed", { status: 502 });
    }
    return new Response("OK", { status: 200 });
  },
};
