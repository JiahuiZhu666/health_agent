# Deploy NHIS Lab

These files prepare a deployment; they do not create a cloud resource or establish that a public service is running. The default Blueprint is a **free, public chat preview** with temporary storage and bounded usage. It does not provision a paid plan or disk.

## Free chat preview on Render

1. Push the source repository to [JiahuiZhu666/health_agent](https://github.com/JiahuiZhu666/health_agent), then use [Deploy to Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FJiahuiZhu666%2Fhealth_agent) or connect that repository to a new Render Blueprint. Select the root `render.yaml`. This is a deployment shortcut, not the live chat URL.
2. Review the generated service before creating it. The default service uses `plan: free`, with no persistent disk and automatic deploys disabled.
3. In Render’s environment settings, provide `OPENAI_API_KEY` through the `sync: false` input. The Blueprint generates a separate random `WEB_AUTH_SECRET` using `generateValue: true`. This server-only signing key must remain configured and must contain at least 32 characters. It is different from the model API key; it is not a public login password in public mode.
4. The entrypoint uses Render’s exact `RENDER_EXTERNAL_URL` to configure the allowed HTTPS origin and host. For a custom domain, set `NHIS_PUBLIC_URL=https://your-domain.example`. Explicit comma-separated `NHIS_ALLOWED_HOSTS` and `NHIS_ALLOWED_ORIGINS` can override these derived settings. Do not use wildcards.
5. After Render finishes building and the `/healthz` check passes, open the URL shown in the Render dashboard. The default Blueprint sets `NHIS_PUBLIC_ACCESS=true`, so visitors can chat without a login. Confirm a real chat reply before treating model connectivity as verified.

The preview starts without downloading NHIS data. Normal chat requires a reachable, funded model API account and can incur API charges even when hosting is free. Do not put a model API key in a browser prompt, repository, Docker build argument, or public issue.

**Preview limitations:** conversations, runs, caches, usage-limit records and any downloaded data live in the container’s ephemeral `/var/lib/nhis` directory and can disappear when the instance restarts or redeploys. Free instances can spin down, so the first request may wait for startup. The free instance’s memory is not a validated capacity for the complete survey/MI pipeline; do not treat the preview as reliable research hosting.

## Public visitors, isolation and usage caps

Public access is an explicit server setting: `NHIS_PUBLIC_ACCESS` accepts only `true` or `false`. Its local default is `false`; the Render Blueprint deliberately sets `true`. A value such as `yes`, `1` or a misspelling fails startup instead of opening the service by accident.

In public mode, a signed browser cookie selects a visitor-specific workspace. Chats, jobs and released research runs are accessible only within that visitor’s cookie scope. They are stored under `visitors/<visitor-id>/` beneath the state directory; the official public survey data are shared. A different browser or cleared cookie does not inherit another visitor’s history. Cookies identify browser sessions, not verified people or recoverable accounts. Server operators can access server-side records.

Keep `WEB_AUTH_SECRET` stable to preserve cookie signatures. With persistent storage and the same signing key, visitor scopes can survive server restarts. On the free ephemeral preview, history, visitor records and rate-limit counters may reset after a restart or redeploy, even if a browser still holds its cookie. Rotating the signing key invalidates existing visitor cookies. Neither ephemeral counters nor browser cookies are a durable anti-abuse identity system.

The default public admission limits are:

| Setting | Default | Scope and window |
|---|---:|---|
| `NHIS_PUBLIC_MESSAGES_PER_HOUR` | 20 | Messages per visitor over a rolling hour |
| `NHIS_PUBLIC_MESSAGES_PER_DAY` | 200 | Messages across all visitors over rolling 24 hours |
| `NHIS_PUBLIC_CHATS_PER_HOUR` | 10 | New chats per visitor over a rolling hour |
| `NHIS_PUBLIC_CHATS_PER_DAY` | 200 | New chats across all visitors over rolling 24 hours |

Every limit must be an integer from 1 through 100000. The daily caps are rolling windows, not UTC calendar-day resets. A limit rejection does not start a paid model request. These are submission limits, not a guaranteed token or currency budget: an admitted message can involve multiple model requests. Free hosting does not make API use free. The service also limits public jobs to four running at once.

Public mode disables independent blind measurement and the legacy `/api/ask` and `/api/setup-data` endpoints. Official data preparation remains an operator action in the runtime shell or an explicit background-startup setting. Public users cannot configure model credentials or start the blind-measurement experiment.

To use the hosted service privately, explicitly set `NHIS_PUBLIC_ACCESS=false` and keep the signing/access secret configured. Private hosted mode uses HTTP Basic authentication with username `owner` and `WEB_AUTH_SECRET` as the password. Private mode is a shared owner workspace, distinct from public cookie isolation. The default local launch remains loopback-only.

Render provides public URLs for **web services**. A Render **private service** is reachable only on its private network and does not provide the requested public chat URL. See [web services](https://render.com/docs/web-services) and [private services](https://render.com/docs/private-services).

## Optional persistent research service — paid, requires an explicit choice

For dependable research storage and more memory, first review the current [Render compute and storage prices](https://render.com/pricing). This repository does not activate paid resources. After choosing a paid service, replace the free plan in `render.yaml` with the following service fields, keeping its existing runtime, environment and health-check settings:

```yaml
plan: 1c-2g
disk:
  name: nhis-state
  mountPath: /var/lib/nhis
  sizeGB: 1
```

This is an initial capacity choice, not a measured production guarantee. Monitor memory, request duration and disk use with actual workloads. Increase capacity only after reviewing the resulting charge.

The mounted path holds shared `data/`, the private workspace, `visitors/` workspaces, runs and caches; application code, the registry and the fixed R scripts remain in `/app`. The entrypoint prepares new mount ownership and then drops to the fixed non-root application user. Existing data are not copied into the image.

Render disks require a paid service, are available only at runtime, and can be attached to only one instance. Disk-backed services cannot scale to multiple instances and have brief downtime when redeployed. Back up important conversations and research records outside the instance. See [persistent disk behavior and limitations](https://render.com/docs/disks).

## Prepare official data separately from chat startup

Official data are excluded from Git and from the image. Survey requests stay unavailable until the official CSVs have downloaded and passed the existing integrity checks. There are no synthetic or demo results.

On a running service with sufficient capacity, use the Render shell:

```bash
python deploy/entrypoint.py --download-data
```

This downloads and validates the fixed official CDC adult file and all ten income imputations into the configured state directory. It makes no model calls. Check the `/api/status` data and statistics readiness fields after completion; a successful chat or health check does not establish survey readiness.

Alternatively, explicitly set `NHIS_AUTO_DOWNLOAD_DATA=true` and restart. Download and validation then run in a background thread, allowing chat startup to proceed. The default is `false`. A download failure leaves research unavailable and does not manufacture estimates. Avoid simultaneous setup requests while data preparation is running. On the free preview, these files are still ephemeral.

Do not put data preparation in an image build or a Render pre-deploy command: a persistent disk is available only to the running service. The existing downloader records its official sources, integrity checks and checksums.

## Container and runtime configuration

The image uses the official `python:3.11-slim-bookworm` image to run the Python 3.9-compatible stdlib server. Debian packages provide R and `survey`, `jsonlite`, and `mitools`; no local `.venv` or `.r-env` is included. The Docker build checks that the required R packages load. The optional Streamlit interface is not part of this production image.

The build context is an allowlist in `.dockerignore`, and the Dockerfile separately copies only runtime source. `.env`, local data, history, model audit records, caches, Git metadata and private keys are excluded. No secret is consumed at build time. Render also exposes environment settings as build arguments; the Dockerfile deliberately declares no secret arguments. See [Render’s Docker guidance](https://render.com/docs/docker) and [Docker build practices](https://docs.docker.com/build/building/best-practices/).

| Setting | Purpose |
|---|---|
| `PORT` | Listener port; defaults to 10000 in the entrypoint. Render supplies it. |
| `NHIS_HOST` | Listener address; defaults to `0.0.0.0`. |
| `NHIS_STATE_DIR` | Writable state, separate from source; defaults to `/var/lib/nhis`. |
| `NHIS_PUBLIC_ACCESS` | Strict `true`/`false`; local default `false`, Blueprint `true`. |
| `WEB_AUTH_SECRET` | Required server-only signing key in public mode and Basic-auth password in private hosted mode, at least 32 characters. Blueprint auto-generates it. |
| `NHIS_PUBLIC_MESSAGES_PER_HOUR` | Public messages per visitor per rolling hour, default 20. |
| `NHIS_PUBLIC_MESSAGES_PER_DAY` | Public messages globally per rolling 24 hours, default 200. |
| `NHIS_PUBLIC_CHATS_PER_HOUR` | Public new chats per visitor per rolling hour, default 10. |
| `NHIS_PUBLIC_CHATS_PER_DAY` | Public new chats globally per rolling 24 hours, default 200. |
| `NHIS_PUBLIC_URL` | Optional explicit HTTPS origin for a custom domain. |
| `NHIS_ALLOWED_HOSTS` | Exact allowed request hosts; derived from the configured public URL if absent. |
| `NHIS_ALLOWED_ORIGINS` | Exact allowed browser origins; derived from the public URL if absent. |
| `OPENAI_API_KEY` | Server-only model API credential. |
| `NHIS_MODEL_PROVIDER` | `openai`. |
| `NHIS_MODEL_BASE_URL` | `https://api.openai.com/v1`. |
| `NHIS_MODEL_NAME` | `gpt-5.6-sol`; successful live access must be verified separately. |
| `NHIS_AUTO_DOWNLOAD_DATA` | Explicit opt-in background official-data preparation, default `false`. |

`/healthz` is a liveness check and does not call the model or require downloaded survey data. `/readyz` is a configuration probe for the conversation connection; it does not make a live model request or establish survey readiness. Private hosted mode requires access authentication for readiness; public mode uses the public access policy. Use the `/api/status` data and statistics fields for survey readiness. Do not publish private API audit records as static files or make a paid-API test a health check.

## Verification and deployment status

GitHub Actions runs the isolated Python suite on Python 3.9 and 3.11 and the browser JavaScript checks on Node 22. These tests use controlled fixtures, do not download official CSVs, do not require R and do not call a live model. A manually selected workflow option builds the Docker image and installs OS packages; it still downloads no survey data and consumes no API secrets.

Local checks:

```bash
python3 -m unittest discover -s tests -q
node --check web/app.js
node tests/test_web_english.js
node tests/test_web_markdown.js
node tests/test_web_deletion.js
node tests/test_web_public.js
docker build --pull --tag nhis-lab:local .
```

A reviewed Blueprint or a built image is not proof of deployment, API connectivity, persistence or statistical validation. After an authorized deployment, verify its actual URL, visitor isolation, usage caps, allowed origins, real chat connectivity, state retention and research readiness separately. The [Blueprint reference](https://render.com/docs/blueprint-spec) describes `generateValue: true`, `sync: false`, Docker service fields and manual-deploy configuration.
