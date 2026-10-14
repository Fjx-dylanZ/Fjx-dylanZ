# hjkl worker

The anonymous route for the keys on the profile: no GitHub account, and nothing about who pressed
is recorded. A visitor opens `/<key>` (one of `gg h j k l`), presses the button, passes
[Turnstile](https://developers.cloudflare.com/turnstile/), and the Worker sends a
`repository_dispatch` that [`hjkl.yml`](../.github/workflows/hjkl.yml) turns into a keypress,
the same as the issue route.

## Try it locally

```sh
cp .dev.vars.example .dev.vars
npx wrangler dev        # http://localhost:8787/j
```

Without `GITHUB_TOKEN`, presses are a dry run that only logs.

## Deploy

1. `npx wrangler login`
2. **Turnstile:** in the Cloudflare dashboard, add a Turnstile widget for the hostname
   `hjkl.<your-subdomain>.workers.dev`. Put its site key in `wrangler.toml` (`TURNSTILE_SITEKEY`),
   then `npx wrangler secret put TURNSTILE_SECRET` with its secret key.
3. **GitHub token:** create a [fine-grained token](https://github.com/settings/personal-access-tokens/new)
   with access to only the `Fjx-dylanZ/Fjx-dylanZ` repository and one permission,
   *Contents: Read and write* (what `repository_dispatch` needs). Then
   `npx wrangler secret put GITHUB_TOKEN`. It expires; set a reminder to renew it.
4. Optionally `npx wrangler secret put SALT` with any random string (for the rate limiter's IP hash).
5. `npx wrangler deploy`, then put the URL it prints in `profile.toml` under `[hjkl] anonymous`.
   The README shows the anonymous route from the next build.
