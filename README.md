# Weather-Advisory Support Bot

A chat bot (LangGraph agent + FastAPI + plain-HTML frontend) that answers "is it safe to ...?" outdoor questions
**only from written SOPs**, using live Open-Meteo weather. The model never decides what good advice is.

## Setup and run

Requires Python 3.11+ (developed on 3.14).

```bash
cd application
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env   then put your GitHub Models token in GITHUB_TOKEN
uvicorn app:app --reload          # backend + frontend on http://127.0.0.1:8000
```

* **Frontend**: open http://127.0.0.1:8000 (static `static/index.html`, served by the same server; no build step). Every reply has a
  "Why this answer?" trace (SOP matched, evidence lines, graph path, grounding check). "New session" wipes memory.
* **Model**: any OpenAI-compatible endpoint. `.env.example` shows GitHub Models; the tested setup is Groq via `LLM_BASE_URL`/`LLM_API_KEY` (`AI_MODEL_FAST` = understanding,
  `AI_MODEL_SMART` = wording). `.env` is git-ignored; `.env.example` holds placeholders only.
* **Two separate suites, on purpose**:
  * **Unit tests** (`tests/`, 40 tests): `pytest -q`. Offline: fake model, hand-built weather, no network, no key. Fast and deterministic;
    they cover SOP matching, conflict rules, every graph branch, the grounding gate, memory and the 11th-SOP hot reload.
  * **Evals** (`evals/run_evals.py`, 19 cases): `python -m evals.run_evals`. Real model + live Open-Meteo, prints PASS/FAIL/SKIP and writes
    `evals/RESULTS.md`. Not in pytest because paraphrase/injection cases are meaningless with a fake model, and live weather and
    rate limits would make `pytest` flaky. `--stub-llm` swaps in a keyword stub to check the harness itself; it never writes results.
  * The empty `__init__.py` files in `tests/` and `evals/` only make them importable packages (`python -m evals.run_evals`, `from tests.helpers import ...`).

## The SOPs: `policies/sops.yaml`

14 SOPs, 5 categories (severe_weather, outdoor_exercise, travel, vulnerable_groups, leisure), severities info -> critical.

**Form: declarative YAML** with conditions as data (`all`/`any` trees over any Open-Meteo variable, time-of-day windows, thresholds),
plus a `topics` taxonomy. Why: the team that maintains policy edits a file, not code; the loader validates it and the fetcher asks
Open-Meteo for exactly the variables the file mentions, so even an SOP on a *new* weather variable needs no code change.

Three kinds of rule live there:

| Kind | Example | How it is decided |
|---|---|---|
| Threshold | `SOP-CYC-WIND-01` wind > 40 km/h or gusts >= 55 for cycling/two-wheelers | deterministic comparison on API numbers |
| Situational | `SOP-SEV-RAIN-SYSTEM-01` organised heavy-rain system, `applies_to: ["*"]`, `lead_with` | fires on a *combination* of signals (daily rain >= 64.5 mm, **or** >= 35 mm with gusts >= 45, **or** a heavy-rain WMO code on a >= 25 mm day), so no single mild-looking number has to be extreme; it supersedes the plain rain SOPs and leads the answer for any activity |
| Fuzzy | `SOP-LEISURE-PICNIC-01` "good day for a picnic" | graded score: each factor scores 1 / 0.5 / 0 against ideal / tolerable ranges, weighted mean picks a band (good / fair / poor) with its own severity and wording. The fuzziness is in *understanding the question* (model); the grading is written policy (code) |

## Architecture (`bot/graph.py`)

```
understand (LLM -> topic ids) -+-> explain            (deterministic, from the session decision log)
                               +-> no_guidance        (off-topic / activity not in any SOP)
                               +-> ask_location
                               +-> geocode -> fetch_weather -+-> failure_reply   (geocode OR weather OR LLM failure: same honest branch)
                                                             +-> window_passed
                                                             +-> evaluate_sops (code) -+-> no_sop_reply
                                                                                       +-> compose (LLM) -> validate -+-> finalize_llm
                                                                                              ^---- retry once ------+-> template_reply (no LLM)
every terminal node -> commit (adds code-owned header/footer, updates session memory) -> END
```

Deterministic vs model, and why:

* **Model**: (1) map free text to *topic ids from the YAML's taxonomy* (so paraphrases work: "scooter", "grandfather in the courtyard",
  "lunch on the grass"), extract place/day/time-of-day; (2) rephrase the already-chosen guidance. Nothing else.
* **Code**: geocoding, forecast fetch, which SOPs match, conflict resolution, severity, citations, numbers, memory.
* **Boundary hardening**: the wording step never sees the raw user message (only structured request + SOP extract), so injected text
  has nothing to hijack. Topic ids returned by the model are filtered to the taxonomy.

### Non-negotiables, and where each is enforced

| Requirement | Enforcement |
|---|---|
| Traceable to an SOP or explicit "none applies" | `commit` appends `Policy basis: <id> - <title> (severity)` from code, never from the model; `none applies` / `none (no advice given)` otherwise. `explain` answers "why?" from the stored evidence |
| Policy change without touching fetch/model code | SOPs + topics are only in YAML; hot-reloaded by mtime (a broken edit keeps the last good file and sets `error`, visible at `/api/sops`) |
| Never a forecast it doesn't have | geocode failure, empty geocode result, weather HTTP/parse error and LLM outage all route to `failure_reply`: no numbers, no advice |
| Never generic advice with no policy | no topic / unsupported activity -> `no_guidance`; topic fine but nothing fired -> `no_sop_reply` ("not a safety clearance") |
| Numbers come from the API | `check_reply` (graph.py): every number in the model's text must appear in the extract it was given (API values + SOP text); every `SOP-...` id must be one that matched; the draft must also quote a real number from the primary SOP's evidence. Failure -> one retry -> deterministic `template_reply`. A model-invented figure or converted unit is rejected unless the same number appears in the extract (units themselves are not checked) |
| 11th SOP live | append to `policies/sops.yaml`, send the next message. Tested end to end (`test_eleventh_sop_added_live...`, eval E18) |

### Decisions I made on purpose

* **Multiple SOPs apply**: drop SOPs another matched SOP `supersedes`; "all clear" (info) SOPs only show if nothing >= low fired;
  rank by severity, then `lead_with`, then file order; answer with the primary in full and **surface up to two more** as "also".
  Why: hiding a second genuine hazard (wind *and* UV) is worse than a slightly longer answer; capping at three keeps it readable.
* **Ambiguous place names**: take the best geocoding match (optionally filtered by a country/state the user gave) and always show the
  resolved place; if several distinct places matched, the reply says so.
* **Time handling**: question -> (today/tomorrow, part of day) -> local hour window. Hourly conditions are evaluated only over the window
  (and an SOP's own `hours`, e.g. UV 11-16, intersect it); daily conditions use that day's value. A window already past today is reported, not guessed.
* **Session memory**: LangGraph `MemorySaver` keyed by session id holds (a) structured context (place, topics, day, part) for follow-ups
  like "what about this evening?", (b) a decision log (SOP id, severity, evidence) fed to the wording step so it stays consistent and says
  so when conditions changed, (c) raw history. A named activity with no SOP ("scuba diving in Pune") does **not** inherit the previous
  activity. Weather is always re-fetched; memory never stores forecasts as facts. Resets with the process or "New session".

## Evals (`evals/run_evals.py`, report in `evals/RESULTS.md`)

19 cases mapped to the brief: clear SOP hits (E01-E02), paraphrases (E03-E05), several SOPs at once (E06), severe live weather (E07 +
replay E07b, E08 live Bhopal invariants), no-SOP cases (E09-E11), unreachable API and unresolvable place (E12-E13), adversarial
(E14 prompt injection + fake policy, E15 forcing ungrounded numbers/units, E16 asking about a non-existent policy), session memory (E17),
11th SOP live (E18). Each case lists what it checks, what a pass looks like, and records PASS/FAIL/SKIP.

**Honest notes**

* **Where I ran what.** 40 offline tests pass. The full eval suite was run once against Groq (`openai/gpt-oss-120b` wording,
  `openai/gpt-oss-20b` understanding) with live Open-Meteo: **19 PASS / 0 FAIL / 0 SKIP** (see `evals/RESULTS.md`, with sample replies).
  One run, one model: LLM behaviour is not deterministic, so re-run before trusting it. In a manual run the first wording draft was sometimes
  rejected by the grounding gate and retried (the gate working as designed), and an earlier run exposed two bugs now fixed (geocoder picking a
  fuzzy match for 'Goa'; a new activity inheriting the previous topic).
* **E07 (severe live weather) is date-dependent.** It scans a 28-city pool *at run time*, picks the wettest place, and requires >= 35 mm
  daily rain; if nothing in the pool qualifies it reports SKIP (it SKIPped on an earlier calm day; on the recorded run it found Hong Kong at 85.9 mm and passed), never PASS. It asserts
  invariants (cited SOP, reply quotes the API's rain total, independent re-fetch agrees within 20%/5 mm) rather than a hardcoded event.
  **E07b** replays a synthetic system with a fixed forecast so the "system bigger than any one number" behaviour is pinned regardless of
  the weather, but it is synthetic and says so. What I'd do beyond that for a suite that must keep working after the system passes:
  record real Open-Meteo responses during a real event as fixtures and replay them offline in CI, keep the live scan as a nightly job
  that is allowed to SKIP.
* **E08** (the assignment's Bhopal question) can only assert invariants, since today's weather isn't controllable.
* **Cases tagged "synthetic weather"** use a hand-built forecast so the expected SOP is known; the model calls are real. Live-weather cases
  use the real API.
* Rate limits: free tiers are small (GitHub Models allows ~50 strong-model requests/day; Groq has its own limits). The suite uses ~2 calls per case and sleeps between cases.

## Known gaps / risks

* **Small talk and multiple places** (added after manual testing, covered by unit tests, not by evals): greetings, thanks and goodbyes get a
  fixed code-written reply (the model only classifies them, so no advice or facts can leak); if one message names several places
  ("cycling in Delhi and Bhopal") the bot asks which one first instead of merging or failing. A missing API key shows a friendly
  "Setup problem" message in the chat instead of a 500.

* Thresholds are my judgment (the IMD 64.5 mm "heavy rain" line is the only external number); a real deployment needs domain review.
* Open-Meteo is a model forecast, not an IMD alert feed: the rain-system SOP detects the *weather signature* of such a system from forecast
  numbers; it does not read official warnings. The guidance tells the user to check official alerts.
* Extreme weather in a place where `precipitation_sum` is low but the model misclassifies the question (wrong topic) yields no SOP rather than
  a wrong one; the classifier is the single model-dependent gate, and E03-E05 test it.
* The number gate compares magnitudes only (a minus sign is ignored) and permits any number that appears in the extract, e.g. a threshold
  from the rule text. A new SOP that names a variable Open-Meteo does not recognise degrades to base variables (its rule then never fires)
  instead of breaking every request.
* Memory is in-process (`MemorySaver`) by design; multiple server workers would not share sessions.
* Python 3.14 shows LangSmith deprecation warnings from dependencies; harmless.

## Layout

```
app.py                FastAPI: /api/chat, /api/sops, serves the page
bot/graph.py          LangGraph nodes, routing, grounding gate, memory
bot/sops.py           YAML validation, hot reload, matching, conflict resolution
bot/weather.py        Open-Meteo geocoding + forecast, time windows
bot/llm.py            OpenAI-compatible client with rate-limit retry
policies/sops.yaml    the SOPs and topic taxonomy (the only place advice lives)
static/index.html     chat UI
tests/                offline tests (engine, graph branches, API)
evals/run_evals.py    eval suite
```
