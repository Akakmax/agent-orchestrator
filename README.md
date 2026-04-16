# Universal Agent Orchestrator

A config-driven build orchestrator that works with **any CLI agent** — Claude Code, Codex, Gemini, Qwen, Ollama, or your own. Swap agents per role by editing a TOML file. No code changes needed.

## How it works

```
You (or a master session)
  │
  ▼
orchestrator build "implement auth system"
  │
  ├─► Planner agent    → breaks work into sprints
  ├─► Generator agent  → builds each sprint (in tmux)
  ├─► Evaluator agent  → reviews & tests (in tmux)
  └─► Retrospective    → learns from the build
      │
      ▼
  Contract negotiation between Generator ↔ Evaluator
  Auto-retry on failure (max 3 attempts)
  Tick loop monitors health every 2 min
```

Each role can be assigned to a **different agent**. The orchestrator doesn't care what's behind the CLI — it dispatches prompts, monitors tmux sessions, and manages state via SQLite.

## Prerequisites

- Python 3.11+ (for `tomllib`; or install `tomli` for 3.10)
- tmux (for agent session management)
- At least one CLI agent installed (e.g. `claude`, `codex`, `gemini`, `ollama`)

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/youruser/agent-orchestrator.git ~/repos/agent-orchestrator
```

### 2. Set up config

```bash
mkdir -p ~/.kanban
cp ~/repos/agent-orchestrator/agents.toml.example ~/.kanban/agents.toml
# Edit agents.toml to match your installed agents — see Configuration below
```

### 3. Add the CLI alias

```bash
echo 'alias orch="python3 ~/repos/agent-orchestrator/scripts/orchestrator-cli.py"' >> ~/.zshrc
source ~/.zshrc
```

### 4. (Optional) Create a virtualenv

```bash
python3 -m venv ~/repos/agent-orchestrator/.venv
source ~/repos/agent-orchestrator/.venv/bin/activate
pip install tomli  # Only needed for Python < 3.11
```

### 5. Verify setup

```bash
PYTHONPATH=~/repos/agent-orchestrator python3 -c "
from orchestrator.adapter import load_config, build_command
cfg = load_config()
print('Agents:', list(cfg['agents'].keys()))
print('Roles:', cfg['roles'])
for role in cfg['roles']:
    print(f'  {role}: {build_command(role, \"test\")[:60]}...')
"
```

Or just:

```bash
orch status  # Should return empty build list
```

## Configuration

All config lives in `~/.kanban/agents.toml`. If this file doesn't exist, everything defaults to `claude -p` (backward compatible).

### Agent profiles

Each agent defines how the orchestrator invokes it:

```toml
[agents.claude-code]
cli = "claude"                  # CLI binary name (resolved via PATH)
prompt_style = "flag"           # How to pass prompts: flag | subcommand | stdin
prompt_flag = "-p"              # The flag/subcommand used
extra_args = ["--model", "sonnet", "--output-format", "json"]
session_resumable = true        # Supports --resume for continuing sessions
resume_flag = "--resume"
session_lock = true             # Needs exclusive session lock (CC-specific)

  [agents.claude-code.communication]
  protocol = "sendmessage"      # How agent signals completion (see below)
  callback_in_prompt = true
```

### Prompt styles

| Style | Command pattern | Example |
|-------|----------------|---------|
| `flag` | `cli -p "prompt" [args]` | `claude -p "build auth" --model sonnet` |
| `subcommand` | `cli exec [args] "prompt"` | `codex exec --ephemeral "review code"` |
| `stdin` | `cat prompt.txt \| cli [args]` | `cat task.txt \| gemini` |

### Role mapping

Map orchestrator roles to your installed agents:

```toml
[roles]
planner = "claude-code"         # Who breaks the work into sprints
generator = "claude-code"       # Who builds the code
evaluator = "codex"             # Who reviews and tests
retrospective = "gemini"        # Who analyses what went well/badly
```

### Adding a new agent

Just add a profile and point a role at it:

```toml
[agents.qwen]
cli = "qwen-chat"
prompt_style = "flag"
prompt_flag = "--prompt"
extra_args = ["--model", "qwen3-235b"]
session_resumable = false
session_lock = false

  [agents.qwen.communication]
  protocol = "file"

# Then assign it
[roles]
evaluator = "qwen"
```

No code changes required. The orchestrator reads the config at runtime.

## Communication protocols

Each agent can use a different protocol to signal completion:

| Protocol | How it works | Best for |
|----------|-------------|----------|
| `file` | Spawner writes `.exit` file; tick loop polls | Any agent (default) |
| `sendmessage` | Calls `orchestrator-cli.py msg send` after exit | CC agents (prompt-injected) |
| `http` | POSTs to `localhost:PORT/callback/SPRINT_ID` | Remote agents, webhooks |
| `unix_socket` | Writes to Unix domain socket via `socat` | Local agents, no port conflicts |

**Default is `file`** — works with every agent, zero config. The tick loop checks `.exit` files every 2 minutes.

```toml
# HTTP example (for a remote agent)
[agents.remote-worker.communication]
protocol = "http"
callback_port = 8787

# Unix socket example
[agents.local-llm.communication]
protocol = "unix_socket"
socket_path = "/tmp/orchestrator.sock"
```

## Usage

### Start a build

```bash
# Create a new build from a prompt
orch build "implement user authentication with JWT tokens"

# The planner breaks it into sprints automatically
```

### Monitor progress

```bash
# List all builds
orch status

# Show sprints for a build
orch sprints --build BUILD_ID

# Pretty-print the message log
orch msgs --build BUILD_ID

# Attach to the tmux session (watch agents work)
orch watch BUILD_ID
```

### Sprint management

```bash
# Create sprints from a plan file
orch sprint create-from-plan --build BUILD_ID --plan sprint_plan.json

# Manually transition a sprint
orch sprint update --id SPRINT_ID --status building
```

### Contract negotiation

Contracts define success criteria that the generator and evaluator agree on before building starts:

```bash
# Propose success criteria
orch contract propose --sprint SPRINT_ID \
  --criteria '{"tests_pass": true, "no_lint_errors": true, "coverage_above": 80}'

# Evaluator approves or rejects
orch contract approve --sprint SPRINT_ID --version 1
orch contract reject --sprint SPRINT_ID --version 1 --notes "missing edge case tests"

# View current contract
orch contract show --sprint SPRINT_ID
```

### Inter-agent messaging

```bash
# Send a message between agents
orch msg send --build BUILD_ID --from generator --to evaluator \
  --type update --body "Sprint 1 code complete, ready for review"

# List messages for a build
orch msg list --build BUILD_ID --to evaluator
```

### Run the tick loop

The tick loop monitors active builds, checks agent health, advances sprints, and handles timeouts:

```bash
# Run once
orch tick

# Dry run (show what would happen)
orch tick --dry-run

# Run continuously (every 2 min)
watch -n 120 orch tick
```

## Architecture

```
~/.kanban/                           ← Runtime data (created automatically)
├── agents.toml                      ← Agent profiles + role mapping
├── orchestrator.db                  ← SQLite state (builds, sprints, contracts, messages)
├── logs/                            ← Agent session logs + prompt files
│   ├── build-abc123-planner.log
│   ├── build-abc123-planner.log.prompt
│   ├── build-abc123-sprint1-generator.log
│   └── build-abc123-sprint1-evaluator.log
└── builds/                          ← Build project directories

~/repos/agent-orchestrator/          ← This repo
├── README.md
├── agents.toml.example              ← Copy this to ~/.kanban/agents.toml
├── scripts/
│   └── orchestrator-cli.py          ← CLI entry point
└── orchestrator/                    ← Python package
    ├── __init__.py                  ← Public API
    ├── adapter.py                   ← Config reader + CLI command builder
    ├── communication.py             ← Pluggable completion backends
    ├── cli.py                       ← CLI (argparse subcommands)
    ├── db.py                        ← SQLite DAL (WAL mode)
    ├── models.py                    ← Status enums + config defaults
    ├── state_machine.py             ← Build/sprint state transitions
    ├── contracts.py                 ← Contract negotiation logic
    ├── spawner.py                   ← tmux + headless spawn backends
    ├── tick.py                      ← Orchestration health-check loop
    ├── generator.py                 ← Generator agent prompt builder
    ├── evaluator.py                 ← Evaluator agent prompt builder
    ├── planner.py                   ← Planner agent prompt builder
    ├── messaging.py                 ← Message bus helpers
    ├── notifications.py             ← Pluggable notification hooks
    └── prompts/                     ← Prompt templates (Markdown)
        ├── generator.md
        ├── evaluator.md
        ├── planner.md
        └── retrospective.md
```

## State machines

### Build lifecycle

```
planning ──► building ──► reviewing ──► done
   │            │            │
   └────────────┴────────────┴──► failed
```

### Sprint lifecycle

```
pending ──► contracted ◄──► building ──► evaluating ──► passed
               │ (negotiate      │            │
               │  max 3 rounds)  │            ▼
               │                 │         building (retry, max 3)
               │                 │            │
               │                 ▼            ▼
               └─────────────► failed ──► escalated
```

## Default config values

| Setting | Default | Description |
|---------|---------|-------------|
| `SPRINT_MAX_ATTEMPTS` | 3 | Max retries per sprint |
| `CONTRACT_MAX_ROUNDS` | 3 | Max negotiation rounds |
| `CONTRACT_TIMEOUT_MINUTES` | 30 | Negotiation timeout |
| `BUILD_TIMEOUT_HOURS` | 6 | Build-level timeout |
| `TICK_INTERVAL_MINUTES` | 2 | Health check frequency |
| `SPAWN_BACKEND` | tmux | tmux or headless |
| `EVALUATOR_STRICTNESS` | strict | How strict the evaluator is |
| `BUSINESS_HOURS` | 8am–10pm | When builds can run |

## Two-brain pattern

The orchestrator natively supports the "two brains" workflow:

1. **Brain A** (e.g. Claude Code as planner) breaks the work into sprints
2. **Brain B** (e.g. Codex as evaluator) defines success criteria via contracts
3. Both brains negotiate until they agree (max 3 rounds)
4. **Generator** (sub-agent) builds each sprint in a tmux session
5. **Evaluator** (sub-agent) reviews and tests the code
6. If tests fail → retry (max 3 attempts) → escalate if still failing
7. When all sprints pass → build moves to "reviewing" → both brains verify
8. If satisfied → done. If not → loop back.

To set this up, just assign different agents to the roles in `agents.toml`.

## Notifications

The orchestrator sends notifications on key events (build created, sprint failed, build complete, escalation needed). By default it prints to stderr. Plug in your own backend:

```python
from orchestrator.notifications import set_notifier

# Slack webhook
set_notifier(lambda msg: requests.post(SLACK_WEBHOOK, json={"text": msg}))

# Telegram
set_notifier(lambda msg: subprocess.run(["telegram-send", msg]))

# Lark
set_notifier(lambda msg: subprocess.run(["lark-send.sh", msg]))

# macOS notification
set_notifier(lambda msg: subprocess.run(["osascript", "-e", f'display notification "{msg}"']))
```

Or set it in your wrapper script before starting a build.

## Troubleshooting

**"Agent 'X' not defined in agents.toml"** — Add a `[agents.X]` section to your config.

**"Unknown spawn backend"** — Check `SPAWN_BACKEND` is "tmux" or "headless". Install tmux if missing.

**Build stuck in "building"** — Run `orch tick` to check agent health. Dead agents trigger auto-retry.

**No agents.toml found** — That's fine. Everything defaults to `claude -p`. Create one only when you want to use multiple agents.

**Session lock conflicts** — Only agents with `session_lock = true` contend for the CC session lock. Set `session_lock = false` for non-CC agents.
