# Industrial upgrade implementation and release record

The roadmap's local implementation is in place. Production release remains gated on actual container, Chromium, live-provider and independent statistical evidence. Passing offline tests does not establish those external checks.

This record accompanies [the original roadmap](industrial-agent-upgrade-roadmap.md). Its original findings refer to `caa5825`. The local rollback tag is `upgrade-baseline-caa5825`; private baseline and verification artifacts are under `.cache/upgrades/`. No environment file, survey input, conversation or private run is included in Git.

## Implemented behavior

| Roadmap stage | Result | Main implementation |
|---|---|---|
| 0: baseline | Original 353 Python and 41 JavaScript checks recorded; private state backed up; independent restore rehearsed | Local baseline tag, private manifests, `state_admin.py` |
| 1: intent | Unknown explicit outcomes/populations/groupings produce clarification; typed inclusion/exclusion/clear operations retain original spans; all research entrances are guarded | `intent.py`, `spec.py`, `conversation.py`, versioned semantic corpus |
| 2: execution | Durable turns/jobs/events/checkpoints/attempts; client UUID idempotency; quota and job admission commit atomically; leases, cancellation and explicit recovery | `job_store.py`, `execution_context.py`, `jobs.py`, `access.py` |
| 3: model boundary | One bounded transport for dialogue, interpretation and blind measurement; truncation/refusal/tools/malformed payloads fail explicitly; distinct blind attempts and no automatic retries | `transport.py`, `chat_api.py`, `models.py` |
| 4: operations | Separate HTTP/job/R/model capacities; deadlines; budget reservations, unknown-usage holds and a persistent stop switch; private metadata and operator tools | `runtime.py`, `r_worker.py`, `budget.py`, `telemetry.py`, `metrics.py` |
| 4: state | Byte checks, atomic publication, derived summary indexes, cursor pagination, conservative retention and checksum-verified backup/restore | `storage_policy.py`, `history_index.py`, `state_admin.py` |
| 5: release evidence | Required real-R synthetic gate, real-data baseline regression, browser scenarios and container smoke jobs | Validation scripts, `tests/browser`, GitHub Actions, Docker validation stage |
| 6: maintainability/UI | Versioned intent/patch/frozen-plan/publication objects; shared execution boundaries; submitted text does not erase the next draft | Typed modules, turn recovery UI, frontend regression suites |

The existing deterministic evidence renderer, fixed statistical script, survey design, registered outcomes and English page layout remain the basis of the product. The conversation and engine modules still contain orchestration; this is an incremental extraction, not a completed architectural rewrite. Natural-language guarantees are bounded by the versioned test corpus.

## Execution and uncertainty

SQLite stores `turns`, `jobs`, `job_events`, `step_results`, and `external_attempts` in the global `.cache/public-access.sqlite3`, alongside the existing public quota ledger. The unique key is `(workspace_id, conversation_id, client_message_id)`. Same ID and request returns the same job, including after quota exhaustion; conflicting content returns HTTP 409. A worker cannot begin until the admission transaction commits. Legacy callers without an ID retain compatibility but cannot recover a lost submission acknowledgement reliably.

Canonical execution states are `queued`, `running`, `succeeded`, `failed`, `blocked`, `cancelled`, and `unknown`. Legacy polling fields remain available. Restart reconciles abandoned leases without silently executing work. The page offers an explicit resume action only for safe saved work; it offers cancellation for paused queued work. A committed assistant reply can be recovered after the original deadline without regenerating it.

External delivery uncertainty is permanent until independently resolved. A timeout, process death or lost response can represent a billed request. Such an attempt is not automatically retried, refunded or resubmitted through resume. Completed external results are reusable only within the same registered attempt of the same saved turn. A new blind experiment creates new attempts. Cancellation stops later steps; it cannot promise rollback of an already sent request.

The `/api/conversations/:id/messages` flow has durable recovery. The legacy synchronous `/api/ask` and direct Python analysis interface retain their response shape and receive per-turn budgets/deadlines; they do not provide durable chat-job recovery. New browser work uses the durable endpoint.

## Resource defaults

| Boundary | Default | Configuration |
|---|---|---|
| HTTP | 32 active requests, backlog 64, idle read 10 s, absolute connection lifetime 30 s | `NHIS_HTTP_MAX_THREADS`, `NHIS_HTTP_LISTEN_BACKLOG`, `NHIS_HTTP_READ_TIMEOUT`, `NHIS_HTTP_CONNECTION_DEADLINE` |
| Jobs | Four admitted jobs globally, one active job per conversation, 900 s from original start | SQL admission; `NHIS_JOB_WALL_SECONDS` |
| R analysis | One process, CPU/wall 240 s, memory setting 1536 MB, output 16 MiB, 12 actual launches per turn | `NHIS_R_MAX_WORKERS`, `NHIS_R_CPU_SECONDS`, `NHIS_R_WALL_SECONDS`, `NHIS_R_MEMORY_MB`, `NHIS_R_OUTPUT_BYTES`, `NHIS_R_STEPS_PER_TURN` |
| R readiness | Shared single-flight observation, 60 s cache; fixed probe uses the same worker slot and bounded process wrapper | Explicit force refresh is available to Python/operator checks |
| Model | Four concurrent calls; each call at most 60 s and clipped to the turn deadline | Shared transport; registered blind-measurement deadline also applies |
| Storage | Conversation 8 MiB, workspace managed files 64 MiB, one run 16 MiB | `NHIS_STORAGE_CONVERSATION_BYTES`, `NHIS_STORAGE_WORKSPACE_BYTES`, `NHIS_STORAGE_RUN_BYTES` |
| Retention | Anonymous state 90 d, eligible completed payloads 30 d, survey cache 30 d, audit archival eligibility 365 d | `NHIS_RETENTION_ANONYMOUS_DAYS`, `NHIS_RETENTION_COMPLETED_JOBS_DAYS`, `NHIS_RETENTION_CACHE_DAYS`, `NHIS_RETENTION_AUDIT_DAYS` |

HTTP overload returns 503/Retry-After. Conflicting/busy task admission returns 409 without quota charge. Storage admission returns 507. Runtime limits are single-instance limits: this deployment does not support multiple replicas writing independent disks or uncoordinated worker processes.

Linux R children use CPU/address-space/data/file-size limits and process-group termination. macOS uses CPU/file-size limits and R's vector-heap limit; the memory setting is not a total-RSS cap there. Container/host memory and disk alerts still require deployment monitoring. An optional [Nginx edge example](../deploy/nginx.conf.example) supplies TLS-edge connection/rate/read limits, but must be installed and checked on the target host; Render manages its own edge.

`/healthz` stays a cheap liveness check. `/readyz` distinguishes configured chat, last observed model status, observed R readiness and research readiness. It does not send a model request. HTTP status 200 on this endpoint means chat is configured; inspect capability fields before assuming research is ready. Initial R state may be `checking`; cached observations include time/staleness. A separately invoked real provider check is required for a verified model reply.

## Budgets and operator controls

Reservations are global across visitor workspaces. Defaults per turn are 400 calls, 2,000,000 input tokens, 100,000 output tokens and 2,500,000 microUSD. Rolling 24-hour defaults are 2,000 calls, 10,000,000 input tokens, 1,000,000 output tokens and 25,000,000 microUSD. Override with `NHIS_BUDGET_TURN_*`/`NHIS_BUDGET_DAY_*` using suffixes `CALLS`, `INPUT_TOKENS`, `OUTPUT_TOKENS`, `COST_MICROS`.

Planning prices default to USD 4/20 per million input/output tokens and are configurable through `NHIS_BUDGET_INPUT_USD_PER_MILLION` and `NHIS_BUDGET_OUTPUT_USD_PER_MILLION`. These are configurable reservation assumptions, not a claim about the selected provider's price or bill. Input capacity is conservatively reserved from encoded request bytes plus overhead; observed complete usage is reconciled afterward. Missing usage and unknown delivery keep the reservation, including across day boundaries. Message quotas remain a separate admission ledger.

Run these on the server with its actual state path. The tools do not load `.env` or make model calls:

```bash
python scripts/model_admin.py --root /var/lib/nhis status
python scripts/model_admin.py --root /var/lib/nhis disable
python scripts/model_admin.py --root /var/lib/nhis reservations --state held
python scripts/model_admin.py --root /var/lib/nhis events --limit 100
python scripts/model_admin.py --root /var/lib/nhis metrics
python scripts/model_admin.py --root /var/lib/nhis enable
```

`disable` persists a stop on new calls. `NHIS_MODEL_KILL_SWITCH=true` also stops new calls and takes precedence over enabling the database switch. Reading saved chats/results remains possible. Explicit reconciliation requires an independent evidence reference and either both observed token counts or `--confirmed-not-sent`. Never release a reservation merely because a response was lost.

Telemetry records allowlisted IDs, error categories, exception types, stack filenames/functions/lines, durations and observed usage. It excludes prompts, response bodies, exception messages, source excerpts, locals, cookies and credentials. HTTP access logs omit request URLs/query strings. Event retention is 30 days/20,000 records. The local metrics command reports bounded retained-sample counts, latency/queue quantiles, unknown outcomes, budget/error alerts and host disk space. It is not a hosted alert delivery service or a complete historical invoice.

## Storage, compatibility and rollback

Old conversation JSON with no version or version 1 still loads. New conversation/registration/result JSON uses version 2 with additive fields. Durable-job, conversation-index and run-history schemas currently use version 1 and reject unsupported versions. Existing run/cache files remain readable; changed statistics bridge hashes invalidate old statistical cache identities. Public visitor IDs and ownership remain server-verified.

Lists default to 30 summaries. Conversations use opaque `cursor`; runs use `before=run_id`. A conversation defaults to the last 50 messages with `before=message_id` for earlier pages; completed job snapshots also contain only the last 50. Job events default to 200 and accept `event_limit`/`before_event`. The page merges stable IDs, preserves loaded messages/drafts and rejects responses from a previous workspace or conversation. Summary indexes are derived and repairable; unchanged large result/conversation files are not repeatedly decoded.

Final successful `result.json` is published atomically only after the artifact-size gate. Reports/tables require a successful result record at download time. Size failures preserve private statistical audit files, remove exports and publish a blocked result with no released rows. Deleting a chat removes conversational content and job payloads while preserving necessary non-content audit tombstones and research records; retained research records can include the original research question. The deletion dialog states this.

Backups omit `.env`, official input data, runtime environments, preview/build files and nested backups. SQLite uses online snapshots, not uncoordinated WAL copies. Restore validates checksums, member types/paths, state IDs, schemas and database integrity into a new directory only. Retention is a dry run by default, protects active/referenced/blind/external audit records, and requires explicit `--apply`. Audits eligible for archival are not automatically destroyed.

```bash
python scripts/state_admin.py status --root /var/lib/nhis
python scripts/state_admin.py backup --root /var/lib/nhis --destination /backups/nhis-new.tar.gz
python scripts/state_admin.py restore --archive /backups/nhis-new.tar.gz --destination /recovery/nhis-new
python scripts/state_admin.py validate --root /recovery/nhis-new
python scripts/state_admin.py retention --root /var/lib/nhis
```

Stop admission and drain workers before release rollback or destructive maintenance. Keep the current code/state together for forensic review. Roll back code to the baseline tag and restore the **pre-upgrade backup into a separate state directory**, then switch `NHIS_STATE_DIR`. Do not overwrite the live tree or point old code at newly created execution databases. Restore secrets and official data separately using their existing secure sources; the state archive intentionally excludes them.

## Verification and pending gates

Machine-readable local evidence is in [upgrade-validation.json](upgrade-validation.json). Required local commands:

```bash
OPENAI_API_KEY='' python3 -m unittest discover -s tests -q
node --check web/app.js
node tests/test_web_english.js
node tests/test_web_markdown.js
node tests/test_web_deletion.js
node tests/test_web_public.js
node tests/test_web_charts.js
node tests/test_web_turns.js
node tests/test_web_pagination.js
python3 scripts/validate_fixture.py --output artifacts/fixture-validation.json
python3 scripts/validate_real_data.py --output .cache/upgrades/new-real-validation.json
```

The synthetic gate executes the real fixed R script on 8,000 deterministic test adults with 50 strata/two PSUs and ten income imputations. Ten scenarios/499 checks cover design-before-domain, independent weighted points, joint covariance contrasts, finite-df MI pooling, regression samples and suppression. It has no production fallback path. The controlled real-NHIS check compares the upgraded bridge against the saved Git baseline using the same official inputs, including the original education estimates and a ten-imputation income gradient. This is a regression check, not independent Stata certification.

GitHub Actions defines required Python/Node, real-R fixture, Chromium and container-build/startup jobs. The Docker runtime depends on the successful validation-stage artifact and excludes synthetic fixtures. The browser fixture exercises actual production HTML/CSS/JavaScript with isolated fake API transport; it does not certify the live backend/provider.

The following remain release gates:

- Execute the configured container/Chromium jobs on a networked runner; the managed local shell has no Docker/browser installation and cannot bind HTTP ports.
- Commit a reviewed immutable base digest and a real npm lockfile after registry access is available. The verified Python tag, direct Debian R package versions and Playwright version are pinned; CI records the resolved digest/package inventory/lock artifact. Transitive OS packages are not fully snapshot locked.
- Complete independent R/Stata variance/CI/MI comparisons and definition-compatible external NHIS benchmarking. They remain `pending` in research output.
- Run a separate, explicit low-budget live-provider check and verify target-host proxy/TLS, memory/disk alerting, persistence and restore operations. No real provider request was sent for this upgrade.
- Verify a successful Git push and target deployment. Local commits/configuration alone do not prove that GitHub or the online site changed. Required-status branch protection must also be enabled on GitHub; a workflow file alone does not enforce merge policy.

The [latest conversation regression fix](conversation-quality-fix.md) adds shared outcome parsing, dataset metadata intent, persistent Chinese replies and compact paired age comparisons. Its current verification is recorded separately from the original upgrade baseline.


The CI cleanup regression reported on Python 3.11/Linux was caused by an untracked secondary test coordinator continuing SQLite writes during temporary-directory removal. Durable-job fixtures now register every coordinator, release all service gates, and assert every shutdown is drained before deleting state. Runtime shutdown also starts workers atomically with registration and waits for each worker's owned heartbeat before reporting it drained; the caller's timeout still reports pending work without blocking indefinitely. An unstarted heartbeat is not joined after startup failure. No exception suppression or cleanup sleeps were added. Validation: 563 local tests (one platform skip), 250 repetitions of the reported concurrent case, and 200 event-gated lifecycle checks. The corresponding old behaviors are rejected by fault-injection regressions. The actual hosted Python 3.11 run still requires a CI rerun.
