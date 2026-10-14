// hjkl, anonymously: the README's "press anonymously" links land here. GET shows the key with a
// press button behind Turnstile (a plain link can't press anything, so link previewers and
// crawlers can't either); POST checks Turnstile and the rate limits, then sends a
// repository_dispatch that .github/workflows/hjkl.yml turns into a keypress.
//
// Nothing about the visitor is stored. The rate limit is keyed on a salted hash of the IP, which
// the binding forgets within a minute. Without GITHUB_TOKEN (local dev), presses are a dry run.

const KEYS = { gg: "back to the first pane", h: "focus west", j: "focus south", k: "focus north", l: "focus east" };

export default {
  async fetch(request, env) {
    const key = new URL(request.url).pathname.slice(1);
    const profile = `https://github.com/${env.OWNER}`;
    if (!Object.hasOwn(KEYS, key)) return Response.redirect(profile, 302);
    if (request.method === "GET") return page(env, key, "ask");
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });

    const ip = request.headers.get("CF-Connecting-IP") || "";
    if (env.TURNSTILE_SECRET) {
      const form = await request.formData();
      const check = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
        method: "POST",
        body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: form.get("cf-turnstile-response") || "", remoteip: ip }),
      }).then((r) => r.json()).catch(() => ({ success: false }));
      if (!check.success) return page(env, key, "bot", 403);
    }

    const visitor = await sha256(`${env.SALT || "hjkl"}:${ip}`);
    if (!(await env.PER_VISITOR.limit({ key: visitor })).success || !(await env.EVERYONE.limit({ key: "all" })).success) {
      return page(env, key, "slow", 429);
    }

    if (env.GITHUB_TOKEN) {
      const res = await fetch(`https://api.github.com/repos/${env.OWNER}/${env.REPO}/dispatches`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "hjkl-worker",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ event_type: "hjkl", client_payload: { key } }),
      });
      if (res.status !== 204) return page(env, key, "github", 502);
    } else {
      console.log(`dry run: would press ${key}`);
    }
    return page(env, key, "sent");
  },
};

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// One small page for every state, in the profile's colors. `key` is always one of KEYS.
function page(env, key, state, status = 200) {
  const profile = `https://github.com/${env.OWNER}`;
  const note = {
    ask: `Anonymous: nothing about you is recorded. About half a minute after you press, the focus moves on
          <a href="${profile}">the profile</a> for everyone.`,
    sent: `Sent. The focus moves in about half a minute; taking you back to <a href="${profile}">the profile</a>…`,
    bot: `That didn't pass the bot check. <a href="/${key}">Try again</a>?`,
    slow: `Easy there: a few presses a minute, please. <a href="/${key}">Try again</a> in a moment.`,
    github: `GitHub didn't take that one. <a href="/${key}">Try again</a> in a minute.`,
  }[state];
  const form = state !== "ask" ? "" : `
    <form method="post" action="/${key}">
      ${env.TURNSTILE_SITEKEY ? `<div class="cf-turnstile" data-sitekey="${env.TURNSTILE_SITEKEY}" data-theme="dark"></div>` : ""}
      <button type="submit">press <b>${key}</b></button>
    </form>`;
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${key} · hjkl</title>
${state === "sent" ? `<meta http-equiv="refresh" content="4;url=${profile}">` : ""}
${state === "ask" && env.TURNSTILE_SITEKEY ? `<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>` : ""}
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #121212; color: #bfbfbf;
         font: 15px/1.6 ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  main { width: min(420px, calc(100vw - 32px)); text-align: center; }
  .cap { display: inline-grid; place-items: center; width: 96px; height: 96px; border-radius: 16px; background: #1f1f1f;
         border: 1px solid #333; box-shadow: 0 6px 0 #070707; color: #f9f9f9; font-size: 34px; font-weight: 700; }
  .sent .cap { border-color: #f77f00; box-shadow: 0 6px 0 #070707, 0 0 24px #f77f0055; }
  .what { color: #f77f00; margin: 18px 0 4px; }
  p { margin: 8px 0 22px; }
  a { color: #f77f00; }
  form { display: grid; gap: 14px; justify-items: center; }
  button { font: inherit; color: #121212; background: #f77f00; border: 0; border-radius: 8px; padding: 10px 26px; cursor: pointer; }
  button:hover { background: #ff9a33; }
</style></head>
<body><main class="${state}">
  <div class="cap">${key}</div>
  <div class="what">${KEYS[key]}</div>
  <p>${note}</p>${form}
</main></body></html>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}
