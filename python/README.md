# Symphony Python Port

## Why this exists

This directory is a Python reimplementation of Symphony with the same external behavior as the Elixir reference: poll Linear, dispatch one Codex-backed worker per active issue, supervise crashes, and recover state from the tracker on restart. The point of the port is to keep that behavior while moving the runtime onto `fastactor` and the official Codex Python SDK so it can sit next to Python tooling, tests, and worktrees (`SPEC.md:3-11`).

## Architecture in a glance

```text
Runtime
|- Registry("symphony.agents", "unique")
|- DynamicSupervisor("symphony.agents_sup", max_children=N)
|  `- IssueAgent(issue_id)
|     `- CodexSession
|        `- codex app-server subprocess
|- WorkflowStore("symphony.workflow_store")
`- Orchestrator("symphony.orchestrator")
```

On each poll tick, the orchestrator handles `("tick", token)`, refreshes workflow-derived settings, fetches candidate issues from the tracker, sorts them by priority, `created_at`, and identifier, and starts `IssueAgent` children through the supervisor. Each agent builds its worktree, opens a Codex thread, streams turn events back to the orchestrator as casts, and eventually exits; the orchestrator then receives a `Down` and decides whether to clean up or retry (`src/symphony/orchestrator.py:134-145`, `src/symphony/orchestrator.py:195-211`, `src/symphony/orchestrator.py:247-269`, `src/symphony/orchestrator.py:407-456`, `src/symphony/workflow_store.py:17-25`, `src/symphony/workflow_store.py:53-78`).

## By the numbers

Python is 1,974 LOC vs an apples-to-apples 5,985 LOC in Elixir — a 67% reduction. The Elixir baseline removes 3,473 LOC of SPEC §7 non-goals from the full 9,458-LOC `elixir/lib` tree.

### Python LOC

```text
560  orchestrator.py
309  tracker/linear.py
241  workspace.py
222  config.py
166  codex_session.py
100  issue_agent.py
 90  main.py
 59  workflow_store.py
 52  tracker/memory.py
 40  tracker/__init__.py
 37  logging.py
 29  workflow.py
 27  cli.py
 21  prompts.py
 20  path_safety.py
  1  __init__.py
1974 TOTAL
```

### Per-module deltas

| Elixir | Python | Δ | Why |
|---|---|---|---|
| codex/app_server.ex 1,096 | codex_session.py 166 | −85% | SDK replaces hand-rolled JSON-RPC framing (`../elixir/lib/symphony_elixir/codex/app_server.ex:930`, `../elixir/lib/symphony_elixir/codex/app_server.ex:943`, `../elixir/lib/symphony_elixir/codex/app_server.ex:1059`) |
| orchestrator.ex 1,655 | orchestrator.py 560 | −66% | DynamicSupervisor + Registry + monitor(proc) replace hand-rolled `Process.monitor`/`Process.demonitor` bookkeeping (`../elixir/lib/symphony_elixir/orchestrator.ex:433`, `../elixir/lib/symphony_elixir/orchestrator.ex:698`) |
| linear/{client,adapter,issue}.ex 720 | tracker/linear.py 309 | −57% | httpx + pydantic field aliases (`Field(alias="branchName")`) replace hand-rolled JSON walking; same GraphQL queries verbatim |
| workspace.ex 483 | workspace.py 241 | −50% | `anyio.run_process` replaces `asyncio.create_subprocess_*`; SSH paths dropped |
| config.ex + config/schema.ex 711 | config.py 222 | −69% | pydantic-settings `env_nested_delimiter='__'` does schema validation + env-var overlay |
| workflow_store.ex 153 | workflow_store.py 59 | −61% | Direct port; less ceremony around stamping |
| agent_runner.ex 203 | issue_agent.py 100 | −51% | GenServer lifecycle replaces hand-rolled retry/cleanup |
| cli.ex 191 | cli.py + main.py 117 | −39% | typer + anyio replace argparse boilerplate |

## Why fastactor (OTP semantics in Python)

### Single-writer state

The mutable orchestration state is concentrated in one dataclass, `State`, with `running`, `claimed`, `completed`, and `retry_attempts` fields at `src/symphony/orchestrator.py:90-99`. That state is driven by mailbox-backed callbacks beginning at `handle_info`, `handle_down`, `handle_call`, and `handle_cast` (`src/symphony/orchestrator.py:134-180`), not by shared locks. The sharpest line is `running: dict[str, RunningEntry] = field(default_factory=dict)` (`src/symphony/orchestrator.py:95`). A repo-wide search for `asyncio.Lock`, `Semaphore`, or `asyncio.Semaphore` in `src/` and `tests/` returns no matches.

### Crash isolation per issue

Each issue runs inside its own `IssueAgent`, so a workspace setup failure, hook error, or Codex SDK error is contained to that child. The orchestrator only sees the resulting `Down` and decides whether it was a normal continuation or a failure retry (`src/symphony/orchestrator.py:143-145`, `src/symphony/orchestrator.py:230-245`). The critical branch is `await self._schedule_retry(issue_id, next_attempt, kind="failure")` (`src/symphony/orchestrator.py:245`).

### Subprocess-lifetime coupling

The repo-owned cleanup path is explicit: `IssueAgent.terminate()` awaits `session.stop()` (`src/symphony/issue_agent.py:76-87`), and `CodexSession.stop()` awaits the SDK client's `__aexit__()` (`src/symphony/codex_session.py:95-103`). The important line is `await client.__aexit__(None, None, None)` (`src/symphony/codex_session.py:102`). That keeps Codex session teardown coupled to actor teardown instead of relying on a separate orphan reaper.

### Backpressure for free

The hard concurrency cap lives in the supervisor setup, not in an application-managed semaphore: `max_children=settings.agent.max_concurrent_agents` (`src/symphony/main.py:67-70`). The orchestrator also sets `restart="temporary"` on each child spec so retry policy stays in the orchestrator rather than the supervisor (`src/symphony/orchestrator.py:409-422`). The line that makes the ownership explicit is `restart="temporary"` (`src/symphony/orchestrator.py:421`).

## What the SDK deleted

The largest single deletion is `elixir/lib/symphony_elixir/codex/app_server.ex` at 1,096 LOC collapsing into `src/symphony/codex_session.py` at 166 LOC. In Elixir, the app server owns JSON encoding, newline framing, port I/O, and response demultiplexing:

`../elixir/lib/symphony_elixir/codex/app_server.ex:1057-1060`

```elixir
defp send_message(port, message) do
  line = Jason.encode!(message) <> "\n"
  Port.command(port, line)
end
```

In Python, the wrapper mostly delegates that to the SDK:

`src/symphony/codex_session.py:40-46`

```python
async def start(self) -> None:
    if self._thread is not None:
        return
    client = CodexClient(StdioTransport(self._command(), cwd=str(self._workspace)))
    try:
        await client.__aenter__()
        thread = await client.start_thread(config=self._thread_config)
```

The turn loop is equally thin: `CodexSession.run_turn()` is an `async for step in thread.chat(...)` wrapper with a little result shaping and callback logging (`src/symphony/codex_session.py:56-93`). That replaces the manual response loop in `handle_response/4` (`../elixir/lib/symphony_elixir/codex/app_server.ex:943-950`).

## What the supervisor deleted

The Elixir orchestrator has to `Process.monitor(pid)` when it spawns a worker and `Process.demonitor(ref, [:flush])` when it cleans one up (`../elixir/lib/symphony_elixir/orchestrator.ex:693-699`, `../elixir/lib/symphony_elixir/orchestrator.ex:420-440`). The Python port keeps the same failure semantics, but the bookkeeping collapses into `DynamicSupervisor.start_child(...)`, `Registry.new(...)`, and one explicit `self.monitor(process)` (`src/symphony/main.py:63-81`, `src/symphony/orchestrator.py:407-456`).

## What stayed the same

- Linear GraphQL queries copied verbatim from `../elixir/lib/symphony_elixir/linear/client.ex`
- WorkflowStore: 1-second poll on `(mtime, size, blake2b)` stamp
- Retry math: `failure_retry_delay = min(10_000 * 2^min(attempt-1, 10), max_retry_backoff_ms)`; continuation at attempt=1 → 1,000 ms (`src/symphony/orchestrator.py:60-67` cites `../elixir/lib/symphony_elixir/orchestrator.ex:928-938`)
- Dispatch sort: priority → created_at → identifier (`src/symphony/orchestrator.py:258-265` cites `../elixir/lib/symphony_elixir/orchestrator.ex:224-273`)
- Terminal-state cleanup on startup AND on transition

## fastactor adaptations

- `restart="temporary"` not `"transient"` — orchestrator owns retry
- Monitor messages route to `handle_down(self, msg: Down)`, not `handle_info`
- `Registry.new(name, "unique")` is a synchronous setup call, not a spawned process
- Must `monitor(proc)` explicitly after `start_child(...)` — fastactor follows BEAM, no `start_and_monitor_child` helper

## Deferred to v1.1

- TUI dashboard
- HTTP observability endpoint
- SSH remote workers
- Hot reload
- Multi-tracker support
- Custom Codex tools
- Codex thread-resume across orchestrator restarts (SDK supports it; no persistence yet)

## Running it

Install dependencies:

```bash
cd python
uv sync
```

Dry-run with the in-memory tracker:

```bash
uv run symphony --dry-run --workflow WORKFLOW.md
```

`WORKFLOW.md` keeps the same basic shape as the Elixir workflow: YAML frontmatter between `---` markers, then the prompt body (`../elixir/WORKFLOW.md:1-37`, `src/symphony/workflow.py:15-19`).

Environment variables to know about:

- `SYMPHONY_TRACKER__KIND=memory|linear` is supported by the settings overlay; nested groups use `__` as the delimiter (e.g. `SYMPHONY_AGENT__MAX_TURNS=20`, `SYMPHONY_CODEX__COMMAND='["codex","serve"]'`).
- Linear auth follows the Elixir port 1:1: `tracker.api_key` in `WORKFLOW.md` is resolved at config load with the same rules as `elixir/.../config/schema.ex` `finalize_settings/1` — `$VAR` references are dereferenced, and an unset `tracker.api_key` falls back to `LINEAR_API_KEY` (`src/symphony/config.py:17-37`, `src/symphony/config.py:221`).
- Codex auth is delegated to the CLI/SDK path. Make sure `codex login` has been run, or that `OPENAI_API_KEY` is already available to the Codex process before using the real tracker.

## Tests & verification

The current test suite runs locally with:

```bash
uv run pytest
```

At the time of writing that is 33 passed, 1 skipped.

The live smoke test is opt-in:

```bash
RUN_LIVE_TESTS=1 OPENAI_API_KEY=... uv run pytest tests/e2e/test_codex_smoke.py
```

Static checks:

```bash
uv run pyright src/
uv run ruff check src/ tests/
```
