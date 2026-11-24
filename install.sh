#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"

AGENT_SKILLS_DIR="${AGENT_SKILLS_DIR:-/root/.agent/skills}"
CODEX_SKILLS_DIR="${CODEX_SKILLS_DIR:-/root/.codex/skills}"
CODEX_AGENTS_MD="${CODEX_AGENTS_MD:-/root/.codex/AGENTS.md}"
BIN_TARGET="${BIN_TARGET:-/usr/local/bin/agent-dispatch}"

mkdir -p "$AGENT_SKILLS_DIR" "$CODEX_SKILLS_DIR" "$(dirname "$CODEX_AGENTS_MD")" "$(dirname "$BIN_TARGET")"

# Full designer roster for Antigravity
AGENT_ROSTER=(grill spec slice dispatch diagnose research retro)
# Execution/interactive roster for Codex (deliberately excludes orchestrator skills: dispatch, slice, spec)
CODEX_ROSTER=(grill diagnose research retro)

link_managed() {
  local src="$1"
  local dst="$2"
  if [ -L "$dst" ]; then
    rm -f "$dst"
  elif [ -e "$dst" ]; then
    local bak="${dst}.bak.${TS}"
    mv "$dst" "$bak"
    echo "Backed up existing non-symlink $dst -> $bak"
  fi
  ln -s "$src" "$dst"
}

# Clean ONLY stale symlinks that previously pointed into this workflow repo
for dir in "$AGENT_SKILLS_DIR" "$CODEX_SKILLS_DIR"; do
  for entry in "$dir"/*; do
    [ -L "$entry" ] || continue
    target="$(readlink "$entry" || true)"
    case "$target" in
      "$ROOT"/skills/*)
        rm -f "$entry"
        ;;
    esac
  done
done

for s in "${AGENT_ROSTER[@]}"; do
  link_managed "$ROOT/skills/$s" "$AGENT_SKILLS_DIR/$s"
done

for s in "${CODEX_ROSTER[@]}"; do
  link_managed "$ROOT/skills/$s" "$CODEX_SKILLS_DIR/$s"
done

link_managed "$ROOT/codex/AGENTS.md" "$CODEX_AGENTS_MD"
chmod 755 "$ROOT/bin/agent-dispatch"
link_managed "$ROOT/bin/agent-dispatch" "$BIN_TARGET"

echo "Installed workflow from $ROOT (non-destructive, preserving unrelated skills/files)"
