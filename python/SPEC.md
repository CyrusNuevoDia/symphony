# Symphony × fastactor: Coding Agent Handoff

## 0. Goal

Reimplement OpenAI's Symphony as a Python service with the **same external behavior** as the Elixir reference (poll Linear, dispatch one Codex agent per active issue, supervise crashes, recover state from the tracker on restart) — built on `fastactor` + the official Codex Python SDK.

**Long-running sessions are first-class.** Codex turns can run for many minutes; orchestrator restarts must not lose accumulated thread context. We get this via SDK thread-resume + an append-only session log, not via runtime hot reload (see §5.7, §7).

Optimize for legibility and ~30–40% less code than the Elixir version. Target: **under 2,500 LOC under `src/`** vs. ~6,000 LOC in `elixir/lib/`.

The OG Symphony repo is already checked into `./elixir/` of this repo. Use it as the canonical reference for behavior, not for code style.

---

## 1. Reading order — do this before writing anything

Read these in order. Do not skip ahead.

1. **`./elixir/SPEC.md`** — language-agnostic spec. **This is the contract.** When the Elixir impl and SPEC disagree, SPEC wins.
2. `./elixir/README.md` — operational behavior, Linear setup, terminal states.
3. `./elixir/lib/symphony_elixir.ex` — the supervision tree. ~50 lines; sets the whole shape.
4. `./elixir/lib/symphony_elixir/orchestrator.ex` — read end-to-end (~1,650 lines). This is the brain. Half of it is bookkeeping for invariants the SPEC states in three sentences; your job is to write the three sentences.
5. `./elixir/lib/symphony_elixir/agent_runner.ex` + `codex/app_server.ex` — what we replace with the Codex SDK.
6. **fastactor**: README + `llms.txt` (full API reference) + `writing/why_actors.md`. The `llms.txt` file in particular is built for you.
7. **Codex Python SDK**: https://github.com/openai/codex/tree/main/sdk/python — `README.md`, `examples/`, and the `codex_app_server.AsyncCodex` reference. The SDK wraps the same `codex app-server` binary that the Elixir version manually drives via JSON-RPC.

> **Why this order matters**: SPEC tells you _what is required_. Elixir tells you _how OpenAI implemented it_. fastactor + Codex SDK tell you _the shape of the primitives you'll compose_. If you start coding from the Elixir source, you will port translation artifacts (Port quirks, hand-rolled JSON-RPC framing) that have no business in the Python version.

---

## 2. Stack (pinned)

- **Python 3.13+** (fastactor requires it)
- **`uv`** for everything; no global `pip`
- `fastactor @ git+https://github.com/CyrusNuevoDia/fastactor`
- `codex-app-server-sdk` — official OpenAI Codex Python SDK (importable as `codex_app_server`)
- `httpx` — Linear GraphQL transport
- `pydantic` + `pydantic-settings` — config and wire models
- `python-frontmatter` + `markdown-it-py` — `WORKFLOW.md` parsing
- `structlog` — structured JSON logs
- `typer` — CLI
- `pytest` + `anyio[trio]` (with pytest plugin) — tests
- `ruff` + `pyright` — lint/typecheck

```bash
uv init --package symphony
uv add fastactor codex-app-server-sdk httpx pydantic pydantic-settings \
       python-frontmatter markdown-it-py structlog typer
uv add --dev pytest "anyio[trio]" ruff pyright
```

---

## 3. Architecture map (Symphony Elixir → Python)

| Symphony Elixir               | Python equivalent                                     | Notes                                |
| ----------------------------- | ----------------------------------------------------- | ------------------------------------ |
| `SymphonyElixir.Application`  | `fastactor.run(main)` in `cli.py`                     | Runtime owns root supervisor         |
| `Orchestrator` GenServer      | `Orchestrator(GenServer)`                             | Single-writer dispatch state machine |
| `Task.Supervisor` (agents)    | `DynamicSupervisor("agents", max_children=N)`         | One child per active issue           |
| `AgentRunner.run` (Task)      | `IssueAgent(GenServer)`                               | One per issue; named via Registry    |
| `Codex.AppServer` (1,097 LOC) | `AsyncCodex` from `codex_app_server`                  | **Whole module deletes**             |
| `Codex.DynamicTool`           | SDK's tool registration                               | Defer custom tools to v1.1           |
| `WorkflowStore` GenServer     | `WorkflowStore(GenServer)`                            | Direct port — same shape             |
| `Workflow` parser             | `workflow.py` (pure)                                  | frontmatter + markdown               |
| `Workspace`                   | `workspace.py` (pure)                                 | git worktrees                        |
| `Linear.Client`               | `tracker/linear.py` (httpx GraphQL)                   |                                      |
| `Linear.Issue`                | `Issue(BaseModel)`                                    | pydantic                             |
| `Tracker` + `Tracker.Memory`  | `Tracker(Protocol)` + `LinearTracker`/`MemoryTracker` | Test seam — preserve it              |
| `Config` + `Config.Schema`    | `pydantic-settings.BaseSettings`                      | env: `SYMPHONY_*`                    |
| `PathSafety`                  | `path_safety.py` (pure)                               |                                      |
| `PromptBuilder`               | `prompts.py` (pure)                                   |                                      |
| `StatusDashboard` (TUI)       | **Defer to v1.1**                                     | structlog only in v1                 |
| `HttpServer` (observability)  | **Defer to v1.1**                                     |                                      |
| `SSH` (remote workers)        | **Defer to v1.1**                                     | local-only                           |
| `LogFile`                     | `logging.py` (structlog config)                       |                                      |
| `CLI`                         | `cli.py` (typer)                                      |                                      |

The big two replacements:

1. **`Codex.AppServer` deletes.** The SDK already speaks the JSON-RPC protocol; you import `AsyncCodex` and call `thread_start()` / `thread.run()`. ~1,000 lines vanish.
2. **The orchestrator's `:DOWN`/monitor bookkeeping collapses.** Use `DynamicSupervisor(restart="transient")` + `Registry` + a tiny `Down` clause in `handle_info`. The hand-rolled retry-token state in the Elixir version is mostly there because Tasks aren't supervised children — once they are, half of it disappears.

---

## 4. Project layout

```
src/symphony/
  __init__.py
  cli.py                   # typer entry; calls fastactor.run(main)
  main.py                  # async def main(): Runtime + child specs
  config.py                # pydantic-settings (env: SYMPHONY_*)

  orchestrator.py          # Orchestrator(GenServer)
  issue_agent.py           # IssueAgent(GenServer)
  codex_session.py         # thin wrapper over AsyncCodex

  workflow.py              # parse WORKFLOW.md (pure)
  workflow_store.py        # WorkflowStore(GenServer)
  workspace.py             # git worktree per issue (pure)
  prompts.py               # build prompt from (issue, workflow, turn) (pure)
  path_safety.py           # pure

  tracker/
    __init__.py            # Tracker protocol + Issue model
    memory.py              # MemoryTracker (tests + dev)
    linear.py              # LinearTracker (httpx GraphQL)

  logging.py               # structlog config

tests/
  unit/                    # pure module tests
  actors/                  # fastactor deterministic-sync tests
  e2e/                     # full pipeline w/ MemoryTracker + fake Codex

WORKFLOW.md                # the user's project workflow
pyproject.toml
.env.example
```

---

## 5. Component specs

### 5.1 `Tracker` protocol

The Elixir code already has this seam (`Tracker.Memory`). **Preserve it** — it's the difference between testable and untestable.

```python
# src/symphony/tracker/__init__.py
from typing import Protocol
from pydantic import BaseModel

class Issue(BaseModel):
    id: str
    identifier: str          # "ENG-123"
    title: str
    description: str | None
    state: str               # "Todo" | "In Progress" | "Human Review" | ...
    url: str

class Tracker(Protocol):
    async def list_active_issues(self) -> list[Issue]: ...
    async def fetch_issues_by_ids(self, ids: list[str]) -> list[Issue]: ...
```

`MemoryTracker` is dict-backed with explicit state-transition methods for tests. `LinearTracker` is the httpx-based GraphQL client. Orchestrator depends on `Tracker`, never on a concrete impl.

### 5.2 `CodexSession`

Thin wrapper. Owns one `AsyncCodex` thread; lifetime is bound to the owning `IssueAgent`.

```python
# src/symphony/codex_session.py
from codex_app_server import AsyncCodex, AppServerConfig

class CodexSession:
    def __init__(self, *, workspace: str, model: str, config_overrides: dict):
        self._codex: AsyncCodex | None = None
        self._thread = None
        self._workspace = workspace
        self._model = model
        self._config_overrides = config_overrides

    async def start(self) -> None:
        self._codex = AsyncCodex(AppServerConfig(
            cwd=self._workspace,
            config_overrides=self._config_overrides,
        ))
        await self._codex.__aenter__()
        self._thread = await self._codex.thread_start(model=self._model)

    async def run_turn(self, prompt: str, on_event=None):
        async for event in self._thread.stream(prompt):
            if on_event:
                on_event(event)
        return self._thread.last_result()

    async def stop(self) -> None:
        if self._codex is not None:
            await self._codex.__aexit__(None, None, None)
            self._codex = None
```

> **Critical invariant**: `IssueAgent.terminate()` MUST call `session.stop()`. The SDK's context-manager guarantees mean the codex subprocess dies with the agent. **This is the equivalent of Elixir's Port-owner-dies-process-dies semantic. Don't skip it.** Subprocess leaks here will surface as zombie codex processes burning your API budget.

If `codex_session.py` exceeds **200 lines**, you are rebuilding the SDK. Stop and re-read the SDK README.

### 5.3 `IssueAgent(GenServer)`

```python
# src/symphony/issue_agent.py
from fastactor.otp import GenServer, Continue
from .codex_session import CodexSession
from .workspace import ensure_worktree
from .prompts import build_turn_prompt

class IssueAgent(GenServer):
    async def init(self, *, issue, workflow, settings, parent, via):
        self.issue = issue
        self.workflow = workflow
        self.settings = settings
        self.parent = parent          # orchestrator pid for event forwarding
        self.turn = 0
        self.workspace = await ensure_worktree(issue, settings)
        self.session = CodexSession(
            workspace=self.workspace,
            model=workflow.model,
            config_overrides=workflow.codex_config,
        )
        await self.session.start()
        return Continue("first_turn")

    async def handle_continue(self, term):
        match term:
            case "first_turn" | "next_turn":
                self.turn += 1
                prompt = build_turn_prompt(self.issue, self.workflow, self.turn)
                await self.session.run_turn(
                    prompt,
                    on_event=lambda e: self.parent.cast(("codex_event", self.issue.id, e)),
                )
                if await self._should_continue() and self.turn < self.workflow.max_turns:
                    return Continue("next_turn")
                # else: fall through; agent exits normally

    async def terminate(self, reason):
        await self.session.stop()
        # Workspace persists across runs by design (SPEC §6 — preserved across restarts)
        await super().terminate(reason)

    async def _should_continue(self) -> bool:
        refreshed = await self.parent.call(("refresh_issue", self.issue.id))
        return refreshed.state in self.settings.tracker.active_states
```

Key design choices:

- **`init` returns `Continue("first_turn")`**. The constructor sets up the session and returns fast — the actual codex turn happens _after_ `init` exits, which avoids deadlocking the orchestrator that's awaiting `start()`.
- **Tracker reads route through the parent.** All "is this issue still active?" lookups go through the orchestrator via `call`, not directly to the tracker. Single-writer for tracker reads = no rate-limit thundering herd when 20 agents check simultaneously.
- **Session lifetime = actor lifetime.** Crash → `terminate()` → subprocess dies. No zombies.

### 5.4 `Orchestrator(GenServer)`

The brain. Owns dispatch state. Single writer for `running` / `claimed` / `completed` / `retries`.

```python
# src/symphony/orchestrator.py (sketch — fill in retry math from elixir/lib/.../orchestrator.ex)
from dataclasses import dataclass, field
from fastactor.otp import GenServer, Call, Cast, Down

@dataclass
class State:
    poll_interval_s: float
    max_concurrent: int
    running: dict[str, "Pid"] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    retries: dict[str, "RetryEntry"] = field(default_factory=dict)

class Orchestrator(GenServer):
    async def init(self, *, tracker, workflow_store, agents_sup, settings):
        self.tracker = tracker
        self.workflow_store = workflow_store
        self.agents_sup = agents_sup
        self.settings = settings
        self.state = State(
            poll_interval_s=settings.polling.interval_s,
            max_concurrent=settings.agent.max_concurrent,
        )
        self._schedule_tick(0)

    async def handle_info(self, msg):
        match msg:
            case ("tick",):
                await self._poll_and_dispatch()
                self._schedule_tick(self.state.poll_interval_s)
            case Down(pid, reason):
                await self._handle_agent_down(pid, reason)
            case ("retry", issue_id, attempt):
                await self._dispatch_one(issue_id, attempt)

    async def handle_call(self, call: Call):
        match call.message:
            case ("refresh_issue", issue_id):
                [issue] = await self.tracker.fetch_issues_by_ids([issue_id])
                return issue

    async def handle_cast(self, cast: Cast):
        match cast.message:
            case ("codex_event", issue_id, event):
                logger.info("codex_event", issue_id=issue_id, kind=event.kind)
```

**SPEC invariants you MUST preserve** (from `elixir/SPEC.md`):

- **Single writer.** All mutation of `running`/`claimed`/`completed` happens in this actor. No shared dicts.
- **Bounded concurrency.** `max_concurrent` caps active agents.
- **Exponential backoff with jitter** for non-`normal` exits. Mirror the Elixir constants: `@failure_retry_base_ms = 10_000`, `@continuation_retry_delay_ms = 1_000`.
- **No DB.** State is in-memory; on restart, re-derive from tracker + filesystem (SPEC §11).
- **Terminal-state cleanup.** When tracker reports an issue moved to `Done`/`Closed`/`Cancelled`/`Duplicate`, stop the agent and clean its workspace.
- **Re-poll after normal exit.** A normal agent exit triggers a continuation check on the next tick, not just `state.completed.add()` and forget.

### 5.5 `WorkflowStore(GenServer)`

Direct port from `elixir/lib/symphony_elixir/workflow_store.ex` (~150 lines). Polls `WORKFLOW.md` `(mtime, size, content_hash)`; reloads when the stamp changes; serves `current()` via `call`. Translates almost line-for-line. Use `pathlib.Path.stat()` + `hashlib.blake2b`.

### 5.6 `Workspace`

Pure functions, no actor:

- `ensure_worktree(issue, settings) -> Path` — `git worktree add` under `settings.workspace.root`, named `<issue.identifier>-<short-hash>`, idempotent.
- `cleanup_worktree(issue) -> None` — `git worktree remove --force`.

Path-safety: every workspace path passes through `path_safety.is_within(root, candidate)` before any write. Mirror the Elixir `PathSafety` module — it's small and well-specified.

---

## 6. Phased implementation

Strictly ordered. Each phase ends with green tests; do not advance until that phase is green. Time estimates are complexity buckets, not deadlines.

> **Phase 1 — Scaffold (S)**
> `uv init --package`, install deps, set up `ruff`/`pyright`/anyio test plugin, smoke test that does `async with Runtime(): pass` and exits cleanly. Push.

> **Phase 2 — Pure modules (M)**
> `config.py`, `workflow.py`, `prompts.py`, `path_safety.py`, `tracker/__init__.py` (Issue model + Protocol), `tracker/memory.py`. **100% unit-test coverage on these** — they're pure, there's no excuse.

> **Phase 3 — WorkflowStore actor (S)**
> Port the Elixir version. Test with `tmp_path`: write a `WORKFLOW.md`, start the GenServer, mutate the file, assert `current()` returns the new content within one poll cycle.

> **Phase 4 — CodexSession + IssueAgent (M)**
> Build `CodexSession` against the SDK. Write an integration test gated on `OPENAI_API_KEY` (skip otherwise) that runs one trivial turn. Then build `IssueAgent` and test it under a `DynamicSupervisor` with a fake parent and a stub tracker that returns "active" once then "done".

> **Phase 5 — Orchestrator (L)**
> The bulk of the work. Build incrementally:
>
> 1. Polling loop that lists issues and logs them. No dispatch yet.
> 2. Add dispatch with `max_concurrent=1`; verify exactly one agent runs.
> 3. Add `Down` handling and retry with exponential backoff. Test by injecting a `CodexSession` that raises.
> 4. Add terminal-state cleanup. Test by flipping a `MemoryTracker` issue to `Done` mid-run.

> **Phase 6 — Linear adapter (M)**
> `LinearTracker` against the real GraphQL API. Use the queries from `elixir/lib/symphony_elixir/linear/client.ex` as the wire-level reference (the GraphQL is identical regardless of language). Add a recorded-cassette test.

> **Phase 7 — CLI + main (S)**
> `typer` CLI. `symphony run [WORKFLOW_PATH]` is the only command for v1. Wire `MemoryTracker` for `--dry-run`; default is `LinearTracker`.

> **Phase 8 — End-to-end (S)**
> Run against the user's actual Linear board with a single test issue. Verify: claim → workspace created → codex turn ran → PR opened → issue moved to Human Review → agent stopped cleanly.

---

## 7. Non-goals for v1

Hard-cut these. **Do not let them creep in.** Each has a clean extension point in the architecture; documenting _why_ each is deferred matters more than building it.

- **TUI dashboard.** structlog JSON to stderr only.
- **HTTP observability endpoint.** Logs are enough for v1.
- **SSH remote workers.** Local-only; agents run on the orchestrator host. (The biggest deferral. SPEC supports it but adds a transport layer we don't need to prove the architecture.)
- **Hot reload.** Restart the daemon to pick up code changes. (This is the one thing Elixir genuinely beats us on; accept the loss.)
- **Multi-tracker support.** Linear only, with `MemoryTracker` for tests.
- **Custom Codex tools beyond what the SDK ships.** No `Codex.DynamicTool` port.

---

## 8. Testing strategy

Three layers:

1. **Pure unit tests** (`tests/unit/`) — config, workflow parsing, prompt building, path safety, MemoryTracker. Fast, hermetic, no actors. Coverage target: 100%.
2. **Actor tests** (`tests/actors/`) — fastactor deterministic-sync patterns from `llms.txt`. One actor per test, fake parents/children, assert message exchanges.
3. **End-to-end** (`tests/e2e/`) — `MemoryTracker` + a `FakeCodexSession` that returns canned events. Drives the full orchestrator through dispatch → retry → terminal-state lifecycle.

The real Codex SDK and real Linear API only run under `RUN_LIVE_TESTS=1`. CI does not require API keys.

---

## 9. Acceptance criteria

A reviewer should be able to:

1. `uv sync && uv run pytest` — green, no API keys needed.
2. `RUN_LIVE_TESTS=1 OPENAI_API_KEY=... uv run pytest tests/e2e/test_codex_smoke.py` — green with a real key.
3. `cp .env.example .env && uv run symphony run WORKFLOW.md` — pulls one open Linear issue, runs a Codex turn, opens a PR. Same external behavior as Elixir Symphony for that issue.
4. **Total LOC under `src/`: target ~2,500 lines.** Elixir `lib/` is ~6,000. If you're over 3,500, you're re-implementing the SDK or the actor runtime — stop and audit.
5. `uv run pyright --strict src/` — clean. No `Any` leaks at module boundaries.
6. `uv run ruff check src/ tests/` — clean.

---

## 10. Trip hazards (real ones, learned the hard way)

- **The Codex SDK is a context manager.** Forgetting `__aexit__` leaks the subprocess. Always rely on `IssueAgent.terminate()` or `try/finally`.
- **fastactor `call` has a default 5s timeout.** Codex turns take minutes. Use `cast` for fire-and-forget telemetry; for sync waits across long turns, set `FASTACTOR_CALL_TIMEOUT` or wrap in `with fail_after(N):`.
- **Don't `Process.monitor` your own children.** Use `DynamicSupervisor` + `Registry` + `restart="transient"`. The Elixir code monitors raw pids only because `Task.Supervisor.start_child` returns a pid; with `DynamicSupervisor` you get supervised lifecycle for free, and the orchestrator only needs the `Down` notification.
- **Issue-state polling races.** SPEC §7.3: if the tracker says "Done" between dispatch and turn-1 starting, the agent must self-terminate. Implement this in `IssueAgent._should_continue`, not just in the orchestrator's poll loop.
- **Workspace cleanup on terminal states.** Easy to forget. SPEC §11 is explicit: terminal-state transition → stop agent → clean workspace.
- **Don't let `cast` silently swallow exceptions.** Wrap event-forwarding callbacks in try/except + log; an exception inside `lambda e: parent.cast(...)` will crash the codex stream iterator if you're not careful.
- **Linear's GraphQL pagination.** `list_active_issues` must follow the `pageInfo` cursor. The Elixir client does this; don't drop it on the floor.

---

## 11. When in doubt

- **SPEC.md > Elixir impl > this doc.** If they conflict, escalate.
- **Single-writer state > clever sharing.** If two actors mutate the same dict, one is wrong.
- **fastactor primitives > custom plumbing.** If you find yourself writing `asyncio.Lock`, you're routing around the actor model.
- **Delete more than you write.** The Codex AppServer module deletes outright. The orchestrator's retry-token state mostly deletes. If `src/` is bigger than you expected, you're porting translation artifacts that don't apply.

---

_End of handoff. When you're ready to start, post your phase 1 PR and tag for review before advancing to phase 2._
