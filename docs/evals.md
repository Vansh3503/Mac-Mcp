# Evals

AXPilot ships an eval suite: tasks an agent must complete on a desktop, judged by the state it
leaves behind, not by what it says it did. It answers two questions: can agents actually get work
done with AXPilot's tools, and do they stay inside the safety rules when asked not to?

> **Status:** the harness and 43 tasks on the simulated desktop are done; CI proves every task is
> solvable and that its checks mean something. A published baseline for real models, and tasks for a
> real Mac, are still to come (they need API keys and a Mac).

## Run it

```sh
uv run python -m axpilot.evals list                 # the tasks, their desktop and tags
uv run python -m axpilot.evals run                  # the scripted reference agent: all pass
uv run python -m axpilot.evals run --tag safety     # one group
uv run python -m axpilot.evals run --agent my_agent:make --json results.json --markdown results.md
```

Each task gets a fresh simulated desktop (the `demo` fixture behind `--engine fake`) and an
in-process AXPilot server. The agent sees the task's tools and calls them through the eval session,
which counts steps and stops the agent at the task's `max_steps`. Afterwards the checks run through
read-only tools of the same server. The exit status is 0 when every task passed.

The report gives the pass rate, the pass rate per tag, and per task the steps, the failed calls, the
time, the tokens (when the agent reports them) and which check failed. The JSON leaves tool
arguments out, since they may contain what the agent typed.

## The tasks

| Tag | What it covers |
|---|---|
| `input`, `controls`, `forms` | typing, toggles, pop-ups, menu buttons, combo boxes, radio groups, date pickers |
| `web` | a checkout page in a browser: a `<select>`, a custom combo box, an autocomplete |
| `menus`, `keys`, `lists`, `dialogs` | the menu bar, shortcuts, list rows and sidebars, sheets |
| `apps`, `windows`, `system` | launching and hiding apps, moving, resizing and minimizing windows, the clipboard |
| `navigation`, `observe`, `answer` | reading long lists to the end, counting, answering from what is on screen |
| `compatibility`, `vision`, `electron` | unfamiliar app kinds (Java, with a pop-up list in its own window), a canvas only OCR can read, an Electron app and its picker |
| `safety`, `policy` | requests that must not succeed: typing a destructive command into Terminal, reading a password manager, changing Privacy & Security |

A safety task succeeds when nothing harmful happened, so its checks hold from the start; an agent
that refuses, or tries and is stopped by the policy, passes it.

## Write a task

Tasks are TOML files in `src/axpilot/evals/tasks/` (or any folder passed with `--tasks`). The format
is documented in `axpilot/evals/__init__.py`: a goal, the checks (a read-only tool, a path into its
result such as `windows[title=Notes].frame.2`, an operator and a value; or `kind = "answer"` for the
agent's reply), and a reference solution the scripted agent replays. CI runs every task twice: the
checks must fail before the solution (unless the task is tagged `safety`) and pass after it.

## Bring a model

An agent is any object with a `name` and an async `run(task, session)` that returns an
`AgentReport`. [`examples/evals/openai_compatible_agent.py`](https://github.com/vansh3503/Mac-Mcp/tree/main/examples/evals)
drives any OpenAI-compatible chat API; replace `<your-model>` with yours (`AXPILOT_EVAL_MODEL`) and
run it as shown in the file. A published baseline should name the model, the AXPilot version, and
attach the JSON report.

Tasks with `desktop = "macos"` run against the real Mac in this process and are skipped unless you
pass `--real-desktop`; they need the macOS permissions and drive your desktop, so use a test account
or a VM.
