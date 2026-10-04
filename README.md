# AI Task Worker

## Overview

AI Task Worker is a prototype of an autonomous agent that takes a natural-language task ("Find the latest invoice from Company X and enter it into the finance system") and completes it by operating a real browser (Playwright/Chromium) against a mock company environment: a vendor portal and an internal finance system. An LLM (Gemini) decides one action per turn from a text rendering of the page; the harness executes it, guards risky actions behind human approval, recovers from errors, and, when the agent claims it is done, independently checks the real system state before reporting success. Every run produces a trace, screenshots and a Markdown report.

## Demo

Demo video: <ADD LINK>

An example run (report and screenshots) is committed in [`examples/run_task1/`](examples/run_task1/report.md).

## Quick start

Requires Python 3.11.

Linux / macOS / WSL:

```bash
git clone https://github.com/HetviThakkar-025/AI-Task-Worker.git && cd AI-Task-Worker
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # then put your GEMINI_API_KEY in .env
```

Windows PowerShell:

```powershell
git clone https://github.com/HetviThakkar-025/AI-Task-Worker.git
cd AI-Task-Worker
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env      # then put your GEMINI_API_KEY in .env
```

**Linux / WSL: Chromium system libraries.** If Chromium fails with `error while loading shared libraries: libnspr4.so` (or `libnss3`, `libasound`), install them once:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

Without sudo, download and unpack them locally and point `PW_LIB_PATH` at the folder (`main.py` adds it to `LD_LIBRARY_PATH` before starting the browser):

```bash
mkdir -p ~/.local/share/pw-libs/debs && cd ~/.local/share/pw-libs/debs
apt-get download libnspr4 libnss3 libasound2t64
for f in *.deb; do dpkg-deb -x "$f" ~/.local/share/pw-libs/root; done
# in .env:
# PW_LIB_PATH=/home/<you>/.local/share/pw-libs/root/usr/lib/x86_64-linux-gnu
```

**Run.** The mock app (FastAPI on port 8000) is started automatically if it is not running, and reset before every run.

```bash
python main.py "Find the latest invoice from Company X, extract the amount and due date, enter it into the finance system, and tell me when it is done."
python main.py "..." --headed            # watch the browser (add SLOW_MO_MS=400 for recordings)
pytest                                    # 28 unit tests, no LLM or browser needed
```

To start the mock app manually: `python -m uvicorn mock_env.app:app --port 8000`, then open http://localhost:8000.

## Example tasks

Results from live runs during development. The main-task run used the final code. The vendor-creation and "enter all / pay oldest" runs came just before the last two fixes: tolerant JSON parsing in the verifier, and the exact-name prompt rule. The `--flaky`, Acme and ambiguous-name runs used earlier prompt versions (see Known limitations).

| Command | What it shows | Result |
|---|---|---|
| `python main.py "Find the latest invoice from Company X, extract the amount and due date, enter it into the finance system, and tell me when it is done."` | Main task: choose the right vendor and the newest invoice, convert DD/MM/YYYY to YYYY-MM-DD and `4,700.50` to `4700.50` | `done`, verified (10 steps) |
| same task with `--flaky` | First save returns HTTP 503; agent re-enters the form and retries | `done`, verified, no duplicate bill |
| `python main.py "Find the latest invoice from Acme Supplies, enter it into the finance system, and tell me when it is done."` | Amount 124,050 is above the approval threshold. Answer `n`: agent stops with `failed`, nothing saved. Answer `y`: bill saved | `failed` / `done` |
| `python main.py "Find the latest invoice from Company and enter it into the finance system."` | Ambiguous name ("Company X" vs "Company X Ltd"): agent asks the user | asks, then `done` after answer "Company X" |
| `python main.py "Create a new vendor called Globex Corp in the finance system."` | Different workflow, no code changes | `done`, verified (7 steps) |
| `python main.py "Enter all invoices from Company X Ltd into the finance system, then mark the oldest one as paid."` | Multi-record task plus an irreversible action ("Mark as paid" requires approval) | `done`, verified (26 steps) |

Flags: `--headed`, `--flaky` (inject one 503 on save), `--auto-approve` (tests only, prints a warning), `--no-reset`, `--max-steps N`.

## Architecture

```mermaid
flowchart TD
    U[User task] --> L[Agent loop]
    L -->|system prompt + task + memory + trimmed history| M[LLM: Gemini<br/>exactly one tool call]
    M --> T{Tool call}
    T -->|remember| MEM[Memory]
    T -->|ask_user / request_approval| H[Human via stdin]
    T -->|browser_* / read_pdf| P[Policy guard<br/>runs before execution]
    P -->|risky and not approved| H
    P -->|allowed| B[Browser and tools<br/>Playwright, retries, timeouts]
    B --> O[Observation:<br/>URL, errors, visible text,<br/>numbered elements]
    H --> O
    MEM --> O
    O --> LD[Loop detector] --> L
    T -->|finish| V[Verifier: separate LLM call]
    S[Harness fetches real state<br/>/api/bills, /api/vendors, /api/invoices] --> V
    V -->|verified| D[done]
    V -->|not verified, first time| L
    V -->|not verified twice| UV[unverified]
    L -.-> TR[Trace: trace.jsonl, screenshots, report.md]
```

| Module | Responsibility |
|---|---|
| `main.py` | CLI, env setup, starts and resets the mock app, runs the agent, writes the report |
| `agent/loop.py` | The agent loop, system prompt, history trimming, approval flow, finish and verification handling |
| `agent/llm.py` | The only provider-specific file: Gemini calls, message conversion, backoff, model fallback chain |
| `agent/browser.py` | Playwright session; turns a page into text state with numbered elements; click, type, select |
| `agent/tools.py` | Tool schemas and dispatcher; tool errors become `ERROR: ...` observations |
| `agent/policy.py` | Task-agnostic approval rules based on visible labels and values |
| `agent/approval.py` | Human approval prompt (y/N), `AUTO_APPROVE` for tests |
| `agent/verifier.py` | Snapshot of the real system state and an independent LLM audit of the outcome |
| `agent/reliability.py` | Loop detection, retry of transient browser errors, tool timeout |
| `agent/memory.py` | Key-value working memory, re-injected into every prompt |
| `agent/trace.py` | Per-run directory with JSONL trace, screenshots and `report.md` |
| `mock_env/` | FastAPI + Jinja2 vendor portal and finance system with deliberate traps |

## Key design decisions

- **Text page state instead of screenshots.** The browser layer gives the model the URL, title, visible errors, a compact text version of the page (table rows kept as `a | b | c`) and a numbered list of interactive elements. The numbers are written into the DOM (`data-agent-id`), so actions address elements exactly. This is cheap, deterministic and easy to debug from logs. The trade-off is that it fails on canvas-heavy or visually encoded pages.
- **One tool call per turn.** Function calling is forced (`mode=ANY`), and every action returns the fresh page state. The model always decides from what actually happened, never from a plan written several steps earlier.
- **Errors are observations.** Tool failures, app validation errors and denials come back as text such as `ERROR: Element 12 not found; call get_state, ids change after navigation`. The model can read them and adapt, and the loop never crashes on a bad action.
- **Verification by the harness.** When the agent calls `finish`, the harness itself fetches `/api/bills`, `/api/vendors` and `/api/invoices`; the agent can't influence this snapshot. A separate verifier prompt compares every value with the source data. A failed verification goes back to the agent once; a second failure ends the run as `unverified`.
- **Approval guard before execution, task-agnostic.** `policy.check` runs before any browser action. It requires approval for clicks whose label means payment, deletion, cancellation or sending, and for Save/Submit/Create when an amount field is at or above `APPROVAL_THRESHOLD`. The model can also call `request_approval` itself; a granted approval covers the next guarded action. After a denial the human is not asked again for the same task.
- **Provider-neutral messages and a fallback chain.** The loop uses a simple `{role, content, tool_name, tool_args, meta}` format. Only `llm.py` knows Gemini; `meta` carries Gemini 3 thought signatures back unchanged. On quota exhaustion or repeated 503s, `llm.py` moves to the next model in `GEMINI_FALLBACK_MODELS` and prints that it did so. Server-suggested waits longer than 90s are not slept through.
- **History trimming plus memory.** The last 6 tool results are kept in full and older ones are cut to 300 characters. Facts the agent saved with `remember` are re-injected every turn, so trimming does not lose them.
- **Reliability.** Transient browser errors (timeouts, `net::ERR`, navigation failures) are retried with backoff, but validation errors are not. A loop detector warns on 3 identical actions or A-B-A-B-A-B alternation; 3 warnings in a row stop the run.
- **Deliberate traps in the mock environment**, each testing one capability:

| Trap | Tests |
|---|---|
| Invoice list not sorted by date (newest Company X invoice is in the middle) | Comparing dates instead of taking the first row |
| "Company X Ltd" has a more recent invoice than "Company X" | Entity disambiguation |
| Portal shows dates as DD/MM/YYYY, finance form requires YYYY-MM-DD | Format conversion; the form rejects wrong dates with a visible error |
| Amounts shown as `4,700.50`, finance field is `type=number` | Number normalisation |
| Issue date and due date both on the invoice | Using the field the task asks for |
| First invoice-list request after reset takes 2 seconds | Waiting for slow pages |
| Duplicate invoice numbers rejected ("Duplicate bill") | Not creating duplicates on retry |
| Acme invoice over 100,000 | Approval policy |
| `--flaky`: first valid save returns 503 without saving | Recovery from transient server errors |
| Invoice also available as PDF | `read_pdf` tool |

## Models, APIs, frameworks and components used

- **LLM:** Google Gemini via the `google-genai` SDK (Google AI Studio free tier). Runs used `gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-3-flash-preview` and `gemini-3.5-flash-lite`, depending on quota.
- **Browser automation:** Playwright (sync API, Chromium headless shell).
- **Mock environment:** FastAPI, Jinja2, Uvicorn, python-multipart, reportlab (PDF generation).
- **Other:** pypdf (PDF text extraction), httpx, python-dotenv, pytest.
- No agent framework (LangChain etc.) is used; the loop, tools, policy and verifier are written from scratch.
- Built with the help of AI coding tools (Claude Code).

## Assumptions

- The worker operates websites the way a person would, through the UI. The `/api/*` endpoints are read-only ground truth used only by the verifier and the harness, never by the agent.
- "Latest" means the most recent issue date. A bill is "entered" when it exists in the finance system with the correct vendor, invoice number, amount and due date.
- Payments, deletions, sends and high-value submissions are irreversible enough to need a human. Ordinary form saves below the threshold are not.
- A human operator is available on the terminal for questions and approvals. With no input (EOF), approvals count as denied and questions end the run with status `asked_user`.
- One task per run; state does not persist between runs.
- The base URL and starting paths are environment configuration (`START_PATHS` in `.env`), not task logic.

## Evaluation mapping

| Criterion | Where it is implemented |
|---|---|
| Autonomy | `agent/loop.py`: the model plans each step from the page state; the task only states the goal. Generic prompt rules: compare dates, convert formats, ask only when ambiguous |
| Execution | `agent/browser.py`, `agent/tools.py`: real Chromium actions against a running web app; results visible in `/api/bills` |
| Reliability | `agent/reliability.py` (retries, timeout, loop detection and hard stop), `agent/llm.py` (backoff, fallback chain), errors as observations, `--flaky` run |
| Verification | `agent/verifier.py` plus `Agent.on_finish`: harness-fetched state, independent audit prompt, one retry, then `unverified` |
| Generalization | Same code ran vendor creation and "enter all and pay oldest" with no changes to `agent/`; no task-specific logic in `agent/` |
| Engineering quality | Small modules with one job each, provider isolated in `llm.py`, 28 unit tests with fake LLM and fake session, per-run traces |
| Product thinking | Asks only when genuinely ambiguous, approval for irreversible or high-value actions, concise summary with evidence, `report.md` for audit |
| Technical understanding | This README; trace and report make every decision inspectable (`runs/<timestamp>/trace.jsonl`) |

## Known limitations

- **Same-model verifier.** The verifier is a separate prompt on the same model family, so it can share the worker's blind spots. Its judgement is grounded in harness-fetched data, but it is still an LLM judgement.
- **Timeout is not a hard kill.** The 15s tool timeout uses Playwright's per-operation timeouts, and retries stop once 15s have passed. With the sync API, a stuck call cannot be interrupted from outside.
- **Free-tier quota.** The Gemini free tier allows about 20 requests per day per model, and one run uses 10 to 26 requests. Live testing needed several models and the fallback chain. Some runs used a lite model, and quota cut short one late re-check of the ambiguous-name task with the final prompt wording.
- **Single mock environment.** Only one small site has been tested. There is no login, captcha, file upload, iframe, pop-up window or multi-tab flow.
- **No long-term memory.** Memory lasts for one run only.
- **Prompts changed during development.** Some earlier runs (the `--flaky` and approval scenarios) were done with earlier versions of the system prompt. The prompt was later tightened (approval threshold in the prompt, no approval requests for routine actions, exact-name matching), and those scenarios were not all re-run with the final wording.
- **Few tasks tested.** About eight distinct live scenarios, with no success-rate measurement over repeated runs.
- **Gemini 3 thought signatures** are handled in `llm.py` only. A different provider would need its own adapter, although the rest of the code would not change.
- **Approval rules are label-based.** A button labelled ambiguously (e.g. "Confirm" that actually pays) would not be caught unless the model requests approval itself.
- **Noisy screenshot trigger.** The "left a form" trigger also fires on pages that only have a search box.

## Issues found during testing

- **Verifier rejected a correct result.** The verifier model replied `"verified": true` but put a raw newline inside a JSON string, so strict parsing failed and the run was marked unverified. Fixed with tolerant JSON parsing, plus a regression test.
- **Agent asked about an exact match.** An ambiguity rule in the prompt made the agent call `ask_user` even when the vendor name matched exactly. The rule was rewritten: exact matches are used directly, and only partial matches trigger `ask_user`.
- **Approval re-requested after a denial.** After the operator denied an action, the agent asked for approval again. Further approval requests in the same task are now refused without asking the operator.
- **Loop warnings ignored.** The agent received 10 loop warnings in a row and kept going. The run now stops after 3 consecutive warnings.
- **Quota exhausted mid-testing.** The Gemini free-tier daily quota ran out during testing, and the server-suggested retry delay would have slept for hours. Added a 90s cap on retry waits and a model fallback chain (`GEMINI_FALLBACK_MODELS`).
- **Gemini 3 thought signatures.** Gemini 3 models require thought signatures from earlier turns to be passed back. These are carried in an opaque `meta` field on each message and handled only in `llm.py`.

## What I would build next

1. **Evaluation suite:** scripted scenarios (traps, chaos, approvals, ambiguity) run N times each, with success rate, steps, cost and verifier agreement tracked per commit.
2. **Independent verifier:** a different model or vendor for verification, plus deterministic checks where the expected state can be computed.
3. **Resumable runs:** checkpoints of memory, history and browser state, so a run can pause for approval and resume later.
4. **Richer permissions:** per-tool and per-site policies, allow/deny lists, budget limits, and approval through Slack or email instead of stdin.
5. **DOM and vision hybrid:** fall back to screenshots and coordinates on pages the text state cannot represent.
6. **Long-term memory:** learned facts about each site (where things are, required formats) reused across runs.
7. **Parallel workers** with a task queue, and real integrations (email, ERP APIs) behind the same tool interface.

## Safety note

The agent only operated the local mock environment in this repository. No real company systems, credentials or confidential data were used. `.env` (API key) is git-ignored; `.env.example` contains placeholders only.
