# The Dissonance Router

A personal AI agent that notices when **what you say you want** doesn't match **what you actually do**, and spends more model power when that happens. Every routing decision is logged with the reason.

Built for the Meta Global AI Developer Hackathon (Indie Developer track). Runs on Meta's Muse models through the Meta Model API.

This repository is a separate version of a project that started as an earlier hackathon entry. It was rebuilt around Meta's models: the model layer, prices, reasoning control, reports and UI text are Meta-specific, and no earlier results are included.

## Contents

- **Understand it:** [The idea](#the-idea) · [How it works](#how-it-works) · [Design guidelines](#design-guidelines) · [Meta Model API](#meta-model-api)
- **Run it:** [Quick start](#quick-start) · [The app](#the-app) · [Settings and the tutorial](#settings-and-the-tutorial) · [Models and modes](#models-and-modes) · [Public demo mode](#public-demo-mode)
- **Features:** [Personas, Hope and the team lessons](#personas-hope-and-the-team-lessons) · [Note bot](#note-bot) · [Memory](#memory-past-conversations) · [Budget limiter](#budget-limiter)
- **Evidence:** [Evaluation](#evaluation) · [Reports](#reports) · [Known limitations](#known-limitations)
- **Reference:** [Efficiency choices](#efficiency-choices) · [HTTP API](#http-api) · [Dev dashboard](#dev-dashboard) · [Project layout](#project-layout)

## The idea

Most assistants store "I prefer short answers" and treat it as fact. People often do the opposite: they keep asking for more detail, ask for rewrites, or ignore the brief reply. The assistant keeps missing, and nobody tells it.

The Dissonance Router keeps two profiles per user:

- **Stated**: what you told it, across five dimensions: tone, length, sourcing, structure, revision.
- **Observed**: what it has learned from how you actually ask for things.

When the two disagree, that disagreement is the signal. The router doesn't pick a model by how hard the question is; it picks by **how uncertain the agent is about you**.

## How it works

1. **Learn from words and from behavior.**
   - *Words:* **The Nano tier (a short Muse Spark call) reads each message** for style requests, in any language ("I'm still lost", "short version please", "no need for sources"). It must quote the user's exact words as evidence, or nothing is learned; words inside quotation marks are someone else's and don't count. One short call per message, at minimal reasoning, cached; if Nano is unavailable a keyword reader takes over.
   - *Behavior:* what you **do** with an answer: 👍, 👎 with a reason ("too long"), copy, or "try again".
   - A value only becomes a habit when it repeats recently (recent turns weigh more; old habits fade), so one offhand remark changes nothing.
2. **Score.** Each dimension gets a conflict score from 0 to 1:

   | Situation | Score |
   |---|---|
   | stated = observed, or nothing observed yet | 0.00 |
   | opposite poles (stated *concise*, observed *detailed*) | 0.70, rising to 0.80 / 0.90 / 0.95 as the same contradiction repeats |
   | a preference is emerging that was never stated | 0.40, rising to 0.65 |

3. **Route.** The strongest single contradiction decides the model tier:

   | Score | Tier | Why |
   |---|---|---|
   | >= 0.70 | **Ultra** | The agent is unsure what you want; the answer matters |
   | 0.40 - 0.70 | **Super** | A preference is emerging |
   | < 0.40 | **Nano** | No conflict, so use the cheap fast model |

   The strongest dimension is used rather than the average because one clear contradiction averages to 0.14 across five dimensions and would never escalate.
4. **Reconcile.** On an Ultra escalation the model is told about the contradiction and asked to serve both sides: for example a one-line TL;DR for the "concise" you said, then the depth you keep asking for.
5. **Explain and ask.** The decision is logged with its reason, e.g. *"you said 'concise', you behave 'detailed', seen 3x, score 0.90"*, and the agent offers to update your stated preference. Accepting it closes the conflict.

## Design guidelines

Six rules, each enforced in code and checked by its own test ([details](docs/GUIDELINES.md)):

1. **Words are claims, behavior is evidence.** Neither wins automatically; only you change what you said.
2. **Weak signals teach nothing, and old ones fade.** A habit needs repeats; recent messages weigh more.
3. **This turn's instruction beats the profile, for this turn.** "Keep it short" is obeyed now, learned only if repeated.
4. **Spend power where it matters.** Greetings, thanks and quick lookups stay on Nano whatever the profile says.
5. **Explain every choice, never change a preference silently.** Every answer says why; adopting a habit needs your yes and can be undone.
6. **Be honest and keep it private.** Say when something failed or isn't known; never invent sources.

## Meta Model API

Every tier is a Muse model served by the [Meta Model API](https://dev.meta.ai/docs/overview), an OpenAI-compatible Chat Completions endpoint (`https://api.meta.ai/v1`). The models share a 1M-token context, so the router's real dial is **reasoning effort**: how much thinking it buys for a request.

| Tier | Model (Standard mode) | Reasoning effort | Used when |
|---|---|---|---|
| Nano | `muse-spark-1.3` | minimal | Words and behaviour agree. Also reads every message for style requests. |
| Super | `muse-spark-1.3` | medium | A preference is emerging. |
| Ultra | `muse-wonder-1.0` (preview) | high | A clear contradiction. |

- **Reasoning is always on.** Muse Spark rejects `reasoning_effort: none`, so "thinking off" means `minimal`. Reasoning tokens are billed as output, and they count toward `max_completion_tokens`.
- **Standard and Contributor modes.** Standard does not use your content to train Meta models. Contributor is cheaper (`muse-spark-1.3-contributor`, $0.10 in / $0.20 out per 1M tokens) but lets Meta use your content for training, so the app asks before switching to it. Muse Wonder has no Contributor mode, so the Contributor Ultra tier is Muse Spark at `xhigh`.
- **Prices** (per 1M tokens, from the Meta docs): Muse Spark $1.25 in / $4.25 out, cached input $0.15; Muse Wonder $4.00 / $20.00, cached $0.20. They live in [`models.toml`](models.toml).
- **Prompt caching.** The client sends a stable `prompt_cache_key` per tier and prices cached input tokens at the cached rate when the API reports them.
- **Parameters.** Only parameters the API accepts are sent (`max_completion_tokens`, `reasoning_effort`, `stream`). `temperature` is sent only when a caller asks for it.

Status: the client is unit-tested against the documented API with mocked responses. Live measurements are added to [Reports](#reports) after the test batch runs.

## Quick start

Requires Python 3.11+ and a Meta Model API key from [dev.meta.ai](https://dev.meta.ai).

```bash
python -m venv venv
venv\Scripts\activate            # Windows (use: source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env           # cp on macOS/Linux; then set MODEL_API_KEY in .env
```

Step-by-step instructions for every test run and the report format: [docs/RUNNING.md](docs/RUNNING.md).

Run the tests (no models needed):

```bash
python -m pytest
```

Talk to the agent from the terminal:

```bash
python -m src.cli set me stated length concise      # you say you want short answers
python -m src.cli chat me "Explain how attention works, in more detail please"
python -m src.cli chat me "Can you elaborate on the softmax scaling?"   # contradiction: escalates
python -m src.cli profile me
python -m src.cli log me
python -m src.cli accept me length                   # adopt the observed preference
```

## The app

```bash
python -m src.main      # open http://127.0.0.1:8000
```

One page, three views (left sidebar, or `/?view=chat|both|proof`). **▶ Play the demo** runs the core contradiction in about a minute with real model calls.

- **Chat**: every answer is tagged with the model that wrote it; escalated answers say why in plain words ("you said *concise* but keep asking for *detailed*"). Under each answer: 👍, 👎 (pick a reason), Copy and ↻ Try again, all of which count as behavior. **The mirror** shows each style dimension as a spectrum, a ring for what you said and a dot for what you do, with the gap glowing as the contradiction grows. Click a word to change what you say you prefer. A one-click "Yes, update it" (with Undo) appears when the agent suggests adopting your observed habit.
- **Proof**: the evaluation report ([docs/EVAL.md](docs/EVAL.md)): success cases, known failures with reason codes, false-claim checks, and cost vs fit for always-Nano / router / always-Ultra.
- **Chat + Proof**: both side by side.
- Always visible: the evaluation summary, this session's tokens, speed and estimated cost, and the decision list in the sidebar.

Everything on the page is real data: session numbers come from your own calls, and the Proof view reads the latest `python -m src.eval` report (falling back to the committed snapshot `docs/eval-report.json`). If there is no report it says so.

## Personas, Hope and the team lessons

Five scripted demo users, each replayable with one click (Proof, or Settings → Profile). The script is fixed; the answers come from the real configured models. Write-ups: [docs/personas/](docs/personas/).

| Persona | Role in the demo | Expected outcome |
|---|---|---|
| **Hope** | Says short, asks short | Success: every answer stays on Nano |
| **Jordan** | Says short, keeps asking for detail | Success: the contradiction escalates the tier |
| **Casey** | Complains without style words | Known gap: the implicit complaint can be missed |
| **Sam** | Says "don't keep it short" | Known gap: the negation can be read backwards |
| **Kai** | A model invents a source | A planted false link is flagged (the card says it is a stand-in) |

**Hope as reviewer.** When a demo fails, Hope reviews it without calling a model (0 tokens):

- **Why** it failed, from a fixed playbook keyed by reason code.
- **A proposed fix**, checked by re-reading the failing message with the fix applied.
- **Kept rules.** A rule fix (for example "wall of text" means shorter answers) can be applied. It is stored in the database and the signal reader uses it from then on.

**Limits.** Hope cannot write a better answer for free (that needs the model) and does not change the escalation thresholds. Each run is recorded as a lesson (a code and pass/fail per persona, never message text), shown under Settings → Memory. This shows a rule getting fixed, not a model getting smarter, so fixes should be checked on held-out phrasings.

API: `POST /hope/review`, `POST /hope/apply`, `GET /lessons`.

## Models and modes

[`models.toml`](models.toml) lists the model, reasoning effort and prices for each tier in two profiles. Edit it and restart.

| | Standard | Contributor |
|---|---|---|
| Models | `muse-spark-1.3`, `muse-wonder-1.0` | `muse-spark-1.3-contributor` |
| Your content | Not used for training | May be used to train Meta models |
| Price | Muse Spark $1.25 / $4.25; Wonder $4 / $20 | $0.10 / $0.20 |
| Availability | Needs `MODEL_API_KEY` | Needs the key and a confirmation; never in the public demo |

- The **Standard / Contributor** switch is in Settings → Models.
- Values in `.env` still override the file (`MODEL_NANO`, `EFFORT_NANO`, `PRICE_NANO_IN`, ...).
- Token counts come from the API's usage field. The Budget page says which numbers are measured and which are estimated.

## Settings and the tutorial

Settings has **Overview, Profile, Reports, Budget, Models, Memory, Notes, About** and **Accessibility** (replay the tutorial, read this README).

A short guided tour starts on the first visit and walks through every view and settings tab. Skip tells you where to find it again.

## Evaluation

```bash
python -m src.eval        # 13 scripted conversations; add --live for the real models
```

It shows three outcomes side by side:

- **Success**: 8 scenarios.
- **Failure with a reason code**: 4 known limits, e.g. `MISSED_SIGNAL`, `FALSE_SIGNAL`, `OVER_ROUTED`.
- **False claims**: every explanation the agent gives is checked against its database and invented links are flagged. 7 of 7 injected false claims are caught.

It also prices always-Nano vs always-Ultra vs the router. Method and limits: [docs/EVAL.md](docs/EVAL.md).

## Reports

Every evaluation run is saved as one file in [`docs/reports/`](docs/reports/) and shown as a card on the **Proof** page: what it was, which models, where it ran, and the headline. Click a card for the full detail: every scenario with its turns, tiers, tokens, cost and reason codes, or for the blind comparison, every answer and the judge's reason.

**Filters**

- **Meta**: runs on the real Muse models.
- **Archived**: older runs, moved to [`docs/reports/archive/`](docs/reports/archive/) and kept as evidence.

Where a run happened is read from the tier providers, not typed in.

**Saving runs**

```bash
python -m src.eval --save              # scenario run
python -m src.eval compare --save      # blind Nano-vs-Ultra comparison
python -m src.eval.demos --save        # one report per persona
```

API: `GET /reports`, `GET /reports/{id}`.

## Note bot

The square **Note bot** button left of the message box keeps notes on what the agent learns about you and why:

- *"You said you prefer short answers"*
- *"Learned: you keep wanting detailed answers. Latest sign: "Can you elaborate…""*
- *"Contradiction on length… answers moved up to Ultra"*
- *"You marked 2 answers as too long"*
- *"The budget stepped down a tier once"*

Hover it for a description; **!** opens Settings → Notes, with a short "where things stand" summary on top. A badge counts new notes.

It uses **no tokens**: notes are written from records the agent already keeps (an event log of the moments that matter, plus feedback, routing decisions and saved conversations) with fixed sentences, so nothing is invented. Quotes from a conversation that Memory deletes are erased from the notes too.

API: `GET /notes/{user_id}` (`tokens_used` is always 0).

## Memory: past conversations

**＋ New chat** saves the conversation you're leaving to Memory as a compressed file (`data/memory/…/conversation-N.json.gz`) and starts a clean thread. Your profile stays, since a new chat isn't a new person, and the model only sees the current conversation.

- **Retention:** each saved conversation is **deleted after 10 days unless you press Keep**. The file and the message text both go; routing numbers like tier and score stay.
- **Cleanup:** runs at startup, every hour, and when Memory is opened.
- **Reminder:** three days before a deletion the chat reminds you (at most once a day), with no model call and no tokens.
- **Actions:** Keep/Unkeep, Open, Download the `.json.gz`, or Delete now.

API: `POST /conversations/new`, `GET /memory/{user_id}`, `GET /memory/{user_id}/{id}` (+ `/download`), `POST /memory/keep`, `DELETE /memory/{user_id}/{id}`. Retention is set with `MEMORY_RETENTION_DAYS`.

## Budget limiter

Settings → Budget sets a $ limit (a simulated wallet: real token counts × Meta list prices, no real money). The router plans every answer within it:

- If the tier it wants would take more than 20% of what's left, it steps down (Ultra → Super → Nano) and says so: *"the router chose Ultra, but ~$0.006 is more than 20% of the $0.0097 left, so Super answered"*.
- Under 25% left, reasoning drops to minimal and answers are shorter.
- Every answer's length is capped so it can't cost more than what's left. When nothing useful fits, it says the budget is used up (`402`).

Each answer shows the cost **predicted** before the call next to the cost **measured** after. Typical answer lengths are learned from real answers, so the predictions improve with use.

API: `GET /budget/{user_id}`, `POST /budget {user_id, limit_usd, reset_spent}`.

## Efficiency choices

- **Thinking only where it pays.** Nano answers at `minimal` reasoning, Super at `medium`, Ultra at `high`. Muse Spark always reasons, so the saving is in how much, not whether.
- **The cheapest tier does the reading.** The signal reader is Nano at minimal reasoning, a small output cap that leaves room for the reasoning tokens, and a cache, so re-weighing history costs nothing.
- **Cached input is priced as cached.** The system prompt repeats on every call; when the API reports cached tokens, the cost uses the cached rate.

## Public demo mode

Set `PUBLIC_DEMO=1` for a hosted link:

- Random private session ids (short, guessable ids are refused, and the "you are" box is hidden).
- A per-IP rate limit (`RATE_LIMIT_PER_MIN`, default 6).
- Output caps per tier.
- A daily spend cut-off (`DAILY_BUDGET_USD`, default $1).

Limits are in memory: right for a single-instance demo, not for production.

## HTTP API

Served by the same app (interactive docs at `/docs`).

| Method | Path | What it does |
|---|---|---|
| `POST` | `/chat` | `{user_id, message}` → answer, tier chosen, reason, conflict scores, stats, and the "update your preference?" suggestion when there is one |
| `GET` | `/profiles/{user_id}` | Stated vs observed profiles, live conflict scores, and the tier the next request would get. Read-only |
| `GET` | `/decision-log/{user_id}?limit=10` | Recent routing decisions, newest first (timestamps in UTC) |
| `POST` | `/update-style` | `{user_id, dimension, value}` sets a stated preference |
| `POST` | `/accept` | `{user_id, dimension}` adopts the observed value as the stated one and closes the conflict |
| `POST` | `/feedback` | `{user_id, interaction_id, kind: up\|down\|copy, reason?}`: behavior on an answer; returns the updated profiles |
| `POST` | `/regenerate` | `{user_id}`: another answer to the last message (counts as a rejected draft) |
| `GET` | `/proof` | The latest evaluation reports: keyword reader, Nano reader, and the blind Nano-vs-Ultra comparison (`null` where missing) |
| `GET` | `/health` | Status and which model serves each tier |

If the model backend is unreachable, `/chat` returns `503` with the reason.

## Dev dashboard

A small local dashboard runs the test suite and live-model scenarios and shows progress in real time: pass/fail, the model chosen for each request and why, tokens, speed (tok/s), time to first token, context used, and an estimated cost per run compared with sending everything to Ultra. Runs are saved to `data/history/`.

```bash
python -m src.devdash      # opens http://127.0.0.1:8765
```

The cost figures use the Meta list prices in `models.toml` and the token counts the API reports. Prices can be overridden with `PRICE_*` variables in `.env`.

## Project layout

```
src/agent/        profiles, conflict detection, routing, signals, prompts, LLM client, decision logger
src/agent/hope.py zero-token failure review and kept reader rules
src/api/          FastAPI routes, request schemas, dependencies
src/main.py       app entry point (API + product page)
src/web/          product page (single HTML file)
src/db/           SQLite connection and schema
src/eval/         scenarios, grading with reason codes, false-claim checks, saved reports
src/devdash/      local test dashboard (stdlib server + one HTML page)
src/cli.py        terminal interface
models.toml       Meta models, reasoning effort and prices per tier, in two modes
docs/reports/     saved runs (.json.gz); older ones in docs/reports/archive/
tests/            unit tests (no network)
```

## Known limitations

- The Nano reader keeps only the first request it finds in a message (extra keys were mostly invented in early tests), so "no bullets and be casual" teaches one of the two. It misses some phrasings rather than guessing.
- Behavior signals cover thumbs, reasons, copy and retry; not yet edits to an answer or reading time.
- The prompts were first tuned on other models. Results on the Meta models are added to Reports after the live test batch, and the prompts are re-tuned where they miss.

## License

[MIT](LICENSE)
