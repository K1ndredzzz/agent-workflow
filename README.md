# Local Multi-Agent Workflow (`agent-workflow`)

Version-controlled custom engineering skills, global Codex rules, and the cross-harness `agent-dispatch` orchestrator.

## Install / Relink

```bash
./install.sh
```

The installer creates the following links. Existing matching links keep their inode. Conflicting files or links receive a uniquely named backup.

- `skills/*` -> `/root/.agent/skills/*` (all 7 skills)
- `agy/plugin` -> `/root/.gemini/config/plugins/agent-workflow` (global Antigravity engineering & dual-agent review convergence rules)
- Worker-safe skills (`grill`, `diagnose`, `research`, `retro`) -> `/root/.codex/skills/*` (excludes orchestrator-only `dispatch`/`slice`/`spec` to prevent recursive dispatch)
- `codex/AGENTS.md` -> `/root/.codex/AGENTS.md`
- `bin/agent-dispatch` -> `/usr/local/bin/agent-dispatch`

When overriding the installation paths, give Codex worker skills their own directory so they receive the worker roster. You can invoke the installer through a symlink to this checkout.

## Verify

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
RUN_LIVE_LINEAR_TESTS=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
agent-dispatch doctor
agent-dispatch doctor --repo /path/to/repo
```

Unit and end-to-end tests use temporary homes, caches, local Git remotes and simulated harnesses. The opt-in live suite sends GraphQL queries to the official Linear endpoint. `doctor` checks the current repository, including its verification command and Linear project mapping. Use `--project <name-or-id>` when the repository and its Linear project have different names.

The CLI runs on Linux. The Pi adapter uses `memfd_create` and `/proc` to retain a redacted, resumable session. See [dispatch](skills/dispatch/SKILL.md) for model aliases, delivery stages and recovery.
