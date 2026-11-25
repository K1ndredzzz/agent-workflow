#!/usr/bin/env bash
set -euo pipefail

ROOT="$(dirname -- "$(realpath -e -- "${BASH_SOURCE[0]}")")"
TS="$(date +%Y%m%d-%H%M%S)"

AGENT_SKILLS_DIR="${AGENT_SKILLS_DIR:-/root/.agent/skills}"
AGY_PLUGINS_DIR="${AGY_PLUGINS_DIR:-/root/.gemini/config/plugins}"
AGY_PLUGINS_BACKUP_DIR="${AGY_PLUGINS_BACKUP_DIR:-$(dirname -- "$AGY_PLUGINS_DIR")/plugins.bak}"
CODEX_SKILLS_DIR="${CODEX_SKILLS_DIR:-/root/.codex/skills}"
CODEX_AGENTS_MD="${CODEX_AGENTS_MD:-/root/.codex/AGENTS.md}"
BIN_TARGET="${BIN_TARGET:-/usr/local/bin/agent-dispatch}"

# Full designer roster for Antigravity
AGENT_ROSTER=(grill spec slice dispatch diagnose research retro)
# Execution/interactive roster for Codex (deliberately excludes orchestrator skills: dispatch, slice, spec)
CODEX_ROSTER=(grill diagnose research retro)

sources=(
  "$ROOT/codex/AGENTS.md"
  "$ROOT/agy/plugin/plugin.json"
  "$ROOT/agy/plugin/rules/AGENTS.md"
  "$ROOT/bin/agent-dispatch"
)
for s in "${AGENT_ROSTER[@]}"; do
  sources+=("$ROOT/skills/$s/SKILL.md")
done
for src in "${sources[@]}"; do
  if [[ ! -f "$src" ]]; then
    echo "Missing installation source: $src" >&2
    exit 1
  fi
done

mkdir -p -- "$AGENT_SKILLS_DIR" "$AGY_PLUGINS_DIR" "$CODEX_SKILLS_DIR" "$(dirname "$CODEX_AGENTS_MD")" "$(dirname "$BIN_TARGET")"
if [[ "$CODEX_SKILLS_DIR" -ef "$AGENT_SKILLS_DIR" || "$CODEX_SKILLS_DIR" -ef "$ROOT/skills" ]]; then
  echo "Codex worker skills require a separate directory from the full skill roster." >&2
  exit 1
fi
plugins_real="$(realpath -m -- "$AGY_PLUGINS_DIR")"
plugins_bak_real="$(realpath -m -- "$AGY_PLUGINS_BACKUP_DIR")"
if [[ "$plugins_bak_real" = "$plugins_real" || "$plugins_bak_real" = "$plugins_real/"* ]]; then
  echo "AGY_PLUGINS_BACKUP_DIR must be outside AGY_PLUGINS_DIR ($AGY_PLUGINS_DIR)." >&2
  exit 1
fi

link_managed() {
  local src="$1"
  local dst="$2"
  local backup_dir="${3:-}"
  if [[ -e "$dst" && "$(realpath -- "$dst")" = "$src" ]]; then
    return
  fi
  if [ -L "$dst" ] || [ -e "$dst" ]; then
    local base_bak
    if [[ -n "$backup_dir" ]]; then
      mkdir -p -- "$backup_dir"
      base_bak="${backup_dir}/$(basename -- "$dst").bak.${TS}"
    else
      base_bak="${dst}.bak.${TS}"
    fi
    local bak="$base_bak"
    local n=0
    while [ -e "$bak" ] || [ -L "$bak" ]; do
      n=$((n + 1))
      bak="${base_bak}.${n}"
    done
    mv -T -- "$dst" "$bak"
    echo "Backed up $dst -> $bak"
  fi
  ln -sT -- "$src" "$dst"
}

# Remove only retired managed entries from the worker roster.
for s in dispatch slice spec; do
  entry="$CODEX_SKILLS_DIR/$s"
  if [[ -L "$entry" && "$entry" -ef "$ROOT/skills/$s" ]]; then
    rm -- "$entry"
  fi
done

for s in "${AGENT_ROSTER[@]}"; do
  link_managed "$ROOT/skills/$s" "$AGENT_SKILLS_DIR/$s"
done

for s in "${CODEX_ROSTER[@]}"; do
  link_managed "$ROOT/skills/$s" "$CODEX_SKILLS_DIR/$s"
done

link_managed "$ROOT/agy/plugin" "$AGY_PLUGINS_DIR/agent-workflow" "$AGY_PLUGINS_BACKUP_DIR"
link_managed "$ROOT/codex/AGENTS.md" "$CODEX_AGENTS_MD"
link_managed "$ROOT/bin/agent-dispatch" "$BIN_TARGET"

echo "Installed workflow from $ROOT (non-destructive, preserving unrelated skills/files)"
