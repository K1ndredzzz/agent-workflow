# Local Multi-Agent Workflow (`agent-workflow`)

Version-controlled custom engineering skills, global Codex rules, and the cross-harness `agent-dispatch` orchestrator.

## Install / Relink

```bash
./install.sh
```

This non-destructively symlinks (preserving any unrelated skills and backing up real files):
- `skills/*` -> `/root/.agent/skills/*` (all 7 skills)
- Worker-safe skills (`grill`, `diagnose`, `research`, `retro`) -> `/root/.codex/skills/*` (excludes orchestrator-only `dispatch`/`slice`/`spec` to prevent recursive dispatch)
- `codex/AGENTS.md` -> `/root/.codex/AGENTS.md`
- `bin/agent-dispatch` -> `/usr/local/bin/agent-dispatch`

## Verify

```bash
python3 -m unittest discover -s tests -v
RUN_LIVE_LINEAR_TESTS=1 python3 -m unittest discover -s tests -v
agent-dispatch doctor
agent-dispatch doctor --repo /path/to/repo
```
