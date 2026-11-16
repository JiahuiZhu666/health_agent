# NHIS Lab

An English conversation and research workspace. Chat naturally, get help with writing, explore ideas, and ask follow-up questions. When you request a survey analysis, the application uses real NHIS 2024 public data and a fixed statistical pipeline.

Ordinary conversation and survey computation are separate. A general discussion about Canada, diabetes, or causality is a conversation, not an unsupported survey request. Asking the application to calculate NHIS results still requires a registered population, outcome, and analysis.

## Start on this Mac

```bash
bash /Users/jiahui.zhu/nhis-health-agent/scripts/start.command
```

Open **http://127.0.0.1:8765**. Keep the terminal open; press Control-C to stop. Restart the service after backend changes. The optional Streamlit interface uses the same engine:

```bash
bash scripts/start.command --streamlit
```

First-time setup installs project-local Python/R dependencies and downloads the official public files:

```bash
bash scripts/setup.command
```

Existing installation and launch `.command` files remain compatible. Nothing is publicly deployed.

## Conversation

Saved chats can be deleted individually from the sidebar or cleared with **Clear all chats**. Both actions ask for confirmation. A chat with a running message is protected; clearing is rejected before deleting anything when a conversation is busy. Deletion removes conversation messages and context, while official data and research run records remain available.

You can start with ordinary messages:

- “Hi, my name is Alex.” Then: “What is my name?”
- “I had a rough day. Can we talk?”
- “Help me write a concise email to a colleague.”
- “Explain black holes in simple terms.”
- “What is 2 + 2?”

The real model receives the current question and up to 18 recent user/assistant messages from that conversation. Each message is bounded in length, credentials and private paths are redacted, and other conversations are excluded. This is conversational context, not a claim of permanent model memory.

Normal chat does not run R, start blind model measurement, or attach an old survey result as a new source. If the model service cannot be reached, the app reports the connection failure instead of substituting a research menu or pretending to have generated a reply.

Research context is retained separately. You can chat between analyses without losing the last successful specification. A follow-up such as “Explain that” after ordinary chat refers to the conversation, while a clear research request returns to the survey workflow.

## English interface and replies

The web and Streamlit interfaces, default model replies, research explanations, status messages, calculation steps, and newly exported reports are in English. Chinese research questions remain supported. An explicit request for another response language can be handled in ordinary conversation.

The English interface starts a fresh conversation on its first load. Existing conversations and historical research artifacts remain available, preserving their original text and provenance. They are not silently rewritten or translated.

## Research examples

```text
You: Compare fair/poor self-rated health by education.
Assistant: Computes the registered survey estimates and explains the result.
You: What about women?
Assistant: Retains education grouping and restricts the population to women.
You: Switch to income.
Assistant: Changes the grouping and retains the sex filter.
You: How did you calculate that?
Assistant: Explains the saved result without rerunning survey statistics.
You: Plot all results we have discussed.
Assistant: Shows separate charts in the chat, preserving each result's population.
You: Show bar charts.
Assistant: Changes the presentation using the same saved estimates.
```

Other supported requests include education × race/ethnicity comparisons, education or income gradients, and the registered logistic regression. “Low income” requires an explicit poverty cutoff; the application does not choose one silently.

Medical-service distributions are also supported. Ask “How are health insurance coverage and timely access to care distributed?” or “有无医疗保险、是否能及时就医等医疗服务指标如何分布？”. The service calculates coverage and lack of coverage at interview (`NOTCOV_A`), delayed medical care due to cost in the past 12 months (`MEDDL12M_A`), and needed care not obtained due to cost in the past 12 months (`MEDNG12M_A`). Follow-ups such as “What about women?” and “Switch to income” retain all active indicators and their population filters. Each item keeps its own valid-response denominator, confidence interval, chart, and source record. Absence of a reported cost barrier does not establish timely care for every need. Definitions and coding are in the [official 2024 codebook](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/Adult-codebook.pdf), printed pages 187, 332, and 333, and in `config/registry.json`.

Research replies lead with a conclusion and compact table, followed by brief methods and relevant qualifications. Ask “step by step,” “how was this calculated?” or “more detail” for the full calculation, denominators, and interval method. Income analyses retain multiple-imputation qualifications; regression distinguishes odds ratios from risks and causal effects.

The web chat draws plots directly from released statistical records. Proportion charts include the recorded 95% confidence intervals; gradient charts use percentage points and a zero reference; adjusted odds-ratio forest plots use a logarithmic axis and a reference of one. Each panel has a source-record link and PNG/SVG downloads. Suppressed or invalid estimates never become plotted marks. Asking to plot existing results does not rerun R or blind measurement, and only results referenced by the current conversation are included. Large conversations show the most recent twelve matching results with an explicit notice about older records.

“Compare all groups” or “都做一遍” runs the five registered comparisons: education, income, sex, race/ethnicity, and age. The overview keeps the current population filters and shows separate charts for released results. It does not start independent blind measurements. If only some comparisons finish, their successful results and the failures remain distinct. A request to translate or explain these instructions does not execute them.

The model edits a structured, English evidence document. Numeric results, tables, formulas, and limitations are inserted unchanged from released statistical output. Every evidence block must appear once and retain its order. Added prose cannot introduce numbers or obvious unsupported directional, significance, causal, or clinical findings. These checks do not constitute general natural-language factual verification.

## Server-only model connection

The server reads its model and credentials from `.env`. The configured model is `gpt-5.6-sol`. The credential file is ignored by Git and has mode `0600`. There are no credential, provider, model, or endpoint controls in either interface. Browser JSON removes connection identifiers and raw API response records; full audit records remain on the server.

An example without credentials is provided in `.env.example`:

```text
NHIS_MODEL_PROVIDER=openai
NHIS_MODEL_BASE_URL=https://api.openai.com/v1
NHIS_MODEL_NAME=gpt-5.6-sol
OPENAI_API_KEY=your-server-side-key
```

The official OpenAI connection uses the [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create) with `store:false`. The selected model uses low reasoning effort for chat and research prose; the minimal connection check uses none. No browsing, retrieval, tools, generated statistical code, or automatic retries are enabled.

To make one real connection request from your normal Mac terminal:

```bash
bash scripts/start.command --test-api
```

The command reports the actual result and saves safe metadata to `.cache/dialogue-connection-check.json`. Configuration alone is not proof of connectivity. The managed execution environment used for implementation could not connect to the external API; successful live service validation is still pending. Isolated tests are not live model responses.

## Independent blind measurement

In local or private mode, the optional **Independent blind measurement** checkbox starts an additional experiment only for survey analyses. Ordinary chat continues to use the conversation model regardless of this checkbox. Public hosting disables independent blind measurement.

Blind measurement receives registered demographic definitions and English aggregate-percentage questions. It receives no benchmark estimates, sample sizes, individual records, or conversation history. The default plan uses three registered wordings with five repetitions per group. Failures and refusals are kept and are not retried until success. For the selected model, the bounded blind-measurement request explicitly uses no reasoning; this is recorded in call parameters.

The conversation model can see published aggregate evidence and recent messages. Its prose is therefore not an independent blind prediction and never enters the model–survey measurement comparison.

## Fixed statistical rules

- Data: official NHIS 2024 Sample Adult public file. Population: US civilian noninstitutionalized adults aged 25–64.
- Outcomes: fair/poor self-rated health (`PHSTAT_A` codes 4/5 vs 1–3); insurance coverage and its complement (`NOTCOV_A` codes 2 vs 1 and 1 vs 2); cost-related delayed care (`MEDDL12M_A` 1 vs 2); and cost-related unmet care (`MEDNG12M_A` 1 vs 2). Refused, unknown, unascertained, and missing responses are excluded from each outcome's denominator rather than treated as a negative response.
- Design: construct the full adult design using WTFA_A, PSTRAT, and PPSU before taking age and categorical domains.
- Education: less than high school; high school/GED; some college/associate degree; bachelor’s degree or higher.
- Race/ethnicity: Hispanic of any race; non-Hispanic single-race White, Black, Asian; non-Hispanic other/multiple race.
- Income-to-poverty groups: below 100%, 100% to <200%, 200% to <400%, and at least 400%.
- Income: analyze all ten official imputations and pool using Rubin rules. Do not use one imputation or multiply respondent counts by ten.
- Age groups: 25–34, 35–44, 45–54, and 55–64, including in regression.
- Uncertainty: design-based Taylor standard errors and t-Wald intervals; income analyses include pooled uncertainty and degrees of freedom.
- Release: unweighted n must be at least 100. Additional gates cover events, non-events, design degrees of freedom, effective sample size, RSE, and interval reliability. These application gates are not a complete certification of NCHS presentation standards.
- Gradients retain joint covariance, including between-group covariance. Published contrasts require released target and reference groups.
- Adjusted and unadjusted education regressions use the same complete-case domain. OR is not a risk ratio. Complete-case exclusions are counted from the category-filtered domain, including outcome missingness.
- Cross-sectional associations do not establish causality. Self-rated fair/poor health is not a clinical diagnosis.

Definitions, reference groups, and reliability thresholds are in `config/registry.json`. Detailed methods are in `docs/statistics.md`; the original project plan is retained in `docs/project-plan.md`.

## Local records and interfaces

Local and private conversations are saved under `conversations/`. A research run saves registration, actual process events, the statistical result, and released exports under `runs/<run_id>/`. Hosted public visitors have separate workspaces under `visitors/<visitor-id>/`, selected by a signed browser cookie; chats, jobs and released run access remain within that visitor’s scope. Official public data are shared. Clearing the cookie or using another browser does not recover the previous workspace. Validated survey results can be reused only when inputs, specifications, code/registry fingerprints, and runtime versions match. Model conversation and blind-measurement responses are not reused as cached survey results.

Messages record their actual generation metadata privately. An optional intent-selection request is recorded separately from the final research explanation. The UI shows a model reply or a fixed explanation honestly; a failed request is never marked as a successful model answer.

Main local interfaces:

- `POST /api/conversations`, `GET /api/conversations`, `GET /api/conversations/{id}`
- `DELETE /api/conversations/{id}` and `DELETE /api/conversations`: delete one or all saved chats
- `POST /api/conversations/{id}/messages`: returns a job ID
- `GET /api/jobs/{id}`: actual progress and response
- `GET /api/runs/{id}` and released report/CSV artifacts
- The legacy `/api/ask` remains a fixed survey-report interface in local/private mode; public mode disables it and `/api/setup-data`

Browser model-configuration endpoints return 405. Both applications default to loopback-only access. Data, credentials, private caches, and conversation files are not served as static files.

## GitHub and online deployment

The target source repository is [JiahuiZhu666/health_agent](https://github.com/JiahuiZhu666/health_agent). Source synchronization and a running deployment must be verified separately; a configured Git remote does not establish that a push succeeded.

[Deploy this repository to Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FJiahuiZhu666%2Fhealth_agent)

The root `render.yaml` and `Dockerfile` prepare a **free public chat preview** with Python and R. The Blueprint sets `NHIS_PUBLIC_ACCESS=true`, allowing visitors to open the shared URL without a login while keeping their chats and research runs isolated by a signed browser cookie. Credentials stay in server environment variables. `WEB_AUTH_SECRET` remains required as the server’s cookie-signing key and is auto-generated by the Blueprint; it is not removed for public access.

Public usage defaults to 20 messages per visitor per rolling hour and 200 messages globally per rolling 24 hours; new chats default to 10 per visitor per rolling hour and 200 globally per rolling 24 hours. The settings are `NHIS_PUBLIC_MESSAGES_PER_HOUR`, `NHIS_PUBLIC_MESSAGES_PER_DAY`, `NHIS_PUBLIC_CHATS_PER_HOUR`, and `NHIS_PUBLIC_CHATS_PER_DAY`; each must be an integer from 1 through 100000. These are submission caps, not a currency budget. Public mode disables independent blind measurement and legacy analysis/data-setup endpoints. API use can incur charges even on free hosting.

The preview uses temporary storage and is not a validated full-scale research host. History, visitor state, official data and usage counters can be lost on restart or redeploy; a browser cookie alone cannot preserve them. Optional paid memory and persistent storage require a separate explicit choice. See [deployment instructions](deploy/README.md) for configuration and limitations. A prepared Blueprint is not a live chat URL.

Hosted startup uses `NHIS_HOST`, `NHIS_STATE_DIR`, `NHIS_ALLOWED_HOSTS`, `NHIS_ALLOWED_ORIGINS`, and `WEB_AUTH_SECRET`. Non-loopback binding is refused without exact allowed origins and the required server secret. `NHIS_PUBLIC_ACCESS` accepts only `true` or `false` and defaults to `false` locally. Private hosted mode (`false`) uses HTTP Basic authentication with username `owner` and the secret as its password. `/healthz` is a liveness probe; `/readyz` reports chat configuration, last observed model status and cached statistical capability without sending a model request. The default local launch remains loopback-only.

## Verification

Official data and the local R environment are installed. Real-data checks cover the original education/income proportions and gradients, education × race/ethnicity, full/reduced regression, and the four new medical-service overall estimates. A new uninsured income gradient pooled all ten imputations; the uninsured adjusted and unadjusted regressions used the same complete-case sample. An independent weighted-sum and valid-count check matched the new R point estimates, coverage and noncoverage sum to one, and the original education estimates/SEs/CIs remained exactly unchanged. R reported no warnings in these runs. These checks do not establish independent variance validation.

Independent R/Stata numerical agreement, NCHS external benchmarking, and successful live model-service validation remain pending. Ordinary-conversation and English-interface checks use isolated transport fixtures; they do not certify the external service.

```bash
python3 -m unittest discover -s tests -q
node --check web/app.js
node tests/test_web_english.js
node tests/test_web_markdown.js
node tests/test_web_deletion.js
node tests/test_web_public.js
node tests/test_web_charts.js
node tests/test_web_turns.js
node tests/test_web_pagination.js
python3 scripts/validate_fixture.py --output artifacts/fixture-validation.json
```

The independent Stata reference is `scripts/reference.do` and requires a licensed Stata installation. Its presence is not evidence that numerical agreement has been verified.

Industrial upgrade: see [implementation and operator guide](docs/upgrade-implementation.md), [roadmap status](docs/industrial-agent-upgrade-roadmap.md), and [local validation record](docs/upgrade-validation.json). Durable chat jobs, idempotent submissions, explicit recovery/cancellation, bounded model/R execution, budget controls and paginated history are implemented. Container/Chromium, independent Stata, live-provider and target-deployment gates remain pending until actually executed.
