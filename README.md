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
```

Other supported requests include education × race/ethnicity comparisons, education or income gradients, and the registered logistic regression. “Low income” requires an explicit poverty cutoff; the application does not choose one silently.

Research replies lead with a conclusion, show a compact result table, explain the calculation, and distinguish percentage points, uncertainty, and association. Income analyses explain multiple imputation; regression explains reference categories, complete cases, and odds ratios.

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

The optional **Independent blind measurement** checkbox starts an additional experiment only for survey analyses. Ordinary chat continues to use the conversation model regardless of this checkbox.

Blind measurement receives registered demographic definitions and English aggregate-percentage questions. It receives no benchmark estimates, sample sizes, individual records, or conversation history. The default plan uses three registered wordings with five repetitions per group. Failures and refusals are kept and are not retried until success. For the selected model, the bounded blind-measurement request explicitly uses no reasoning; this is recorded in call parameters.

The conversation model can see published aggregate evidence and recent messages. Its prose is therefore not an independent blind prediction and never enters the model–survey measurement comparison.

## Fixed statistical rules

- Data: official NHIS 2024 Sample Adult public file. Population: US civilian noninstitutionalized adults aged 25–64.
- Outcome: PHSTAT_A codes 4/5 are fair/poor; codes 1–3 are the other valid responses. Refused, unknown, unascertained, and missing responses are not treated as healthy.
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

Conversations are saved under `conversations/`. A research run saves registration, actual process events, the statistical result, and released exports under `runs/<run_id>/`. Validated survey results can be reused only when inputs, specifications, code/registry fingerprints, and runtime versions match. Model conversation and blind-measurement responses are not reused as cached survey results.

Messages record their actual generation metadata privately. An optional intent-selection request is recorded separately from the final research explanation. The UI shows a model reply or a fixed explanation honestly; a failed request is never marked as a successful model answer.

Main local interfaces:

- `POST /api/conversations`, `GET /api/conversations`, `GET /api/conversations/{id}`
- `DELETE /api/conversations/{id}` and `DELETE /api/conversations`: delete one or all saved chats
- `POST /api/conversations/{id}/messages`: returns a job ID
- `GET /api/jobs/{id}`: actual progress and response
- `GET /api/runs/{id}` and released report/CSV artifacts
- The legacy `/api/ask` remains a fixed survey-report interface

Browser model-configuration endpoints return 405. Both applications default to loopback-only access. Data, credentials, private caches, and conversation files are not served as static files.

## GitHub and online deployment

The target source repository is [JiahuiZhu666/health_agent](https://github.com/JiahuiZhu666/health_agent). Source synchronization and a running deployment must be verified separately; a configured Git remote does not establish that a push succeeded.

[Deploy this repository to Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FJiahuiZhu666%2Fhealth_agent)

The root `render.yaml` and `Dockerfile` prepare a **free chat preview** with Python and R. The preview is protected by a separate workspace password (username `owner`); credentials stay in server environment variables. The preview uses temporary storage and is not a validated full-scale research host. Optional paid memory and persistent storage require a separate explicit choice. See [deployment instructions](deploy/README.md) for configuration and limitations.

Hosted startup uses `NHIS_HOST`, `NHIS_STATE_DIR`, `NHIS_ALLOWED_HOSTS`, `NHIS_ALLOWED_ORIGINS`, and `WEB_AUTH_SECRET`. Non-loopback binding is refused without access protection and exact allowed origins. `/healthz` is a liveness probe; authenticated `/readyz` checks model configuration only and does not test a live reply. The default local launch remains loopback-only.

## Verification

Official data and the local R environment are installed. Seven real-data analyses have been executed: education and income proportions/gradients, education × race/ethnicity, full regression, and the reduced model. R reported no warnings in those runs.

Independent R/Stata numerical agreement, NCHS external benchmarking, and successful live model-service validation remain pending. Ordinary-conversation and English-interface checks use isolated transport fixtures; they do not certify the external service.

```bash
python3 -m unittest discover -s tests -q
node --check web/app.js
node tests/test_web_english.js
node tests/test_web_markdown.js
node tests/test_web_deletion.js
```

The independent Stata reference is `scripts/reference.do` and requires a licensed Stata installation. Its presence is not evidence that numerical agreement has been verified.
