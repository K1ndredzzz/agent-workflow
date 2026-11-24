# Local Multi-Agent Workflow (`agent-workflow`)

Version-controlled custom engineering skills, global Codex rules, and the cross-harness `agent-dispatch` orchestrator.

## Install / Relink

```bash
./install.sh
```

This symlinks:
- `skills/*` -> `/root/.agent/skills/*` and `/root/.codex/skills/*`
- `codex/AGENTS.md` -> `/root/.codex/AGENTS.md`
- `bin/agent-dispatch` -> `/usr/local/bin/agent-dispatch`

## Verify

```bash
python3 -m unittest discover -s tests -v
agent-dispatch doctor --repo /path/to/repo
```
