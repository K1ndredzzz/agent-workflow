#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p /root/.agent/skills /root/.codex/skills /usr/local/bin

# Remove stale symlinks/dirs in /root/.agent/skills not in our workflow roster
ROSTER=(grill spec slice dispatch diagnose research retro)
for existing in /root/.agent/skills/*; do
  [ -e "$existing" ] || [ -L "$existing" ] || continue
  base="$(basename "$existing")"
  keep=0
  for s in "${ROSTER[@]}"; do
    if [ "$base" = "$s" ]; then keep=1; break; fi
  done
  if [ "$keep" -eq 0 ]; then
    rm -rf "$existing"
  fi
done

for s in "${ROSTER[@]}"; do
  rm -rf "/root/.agent/skills/$s" "/root/.codex/skills/$s"
  ln -s "$ROOT/skills/$s" "/root/.agent/skills/$s"
  ln -s "$ROOT/skills/$s" "/root/.codex/skills/$s"
done

rm -f /root/.codex/AGENTS.md /usr/local/bin/agent-dispatch
ln -s "$ROOT/codex/AGENTS.md" /root/.codex/AGENTS.md
chmod 755 "$ROOT/bin/agent-dispatch"
ln -s "$ROOT/bin/agent-dispatch" /usr/local/bin/agent-dispatch

echo "Installed workflow from $ROOT -> ~/.agent/skills, ~/.codex/skills, ~/.codex/AGENTS.md, /usr/local/bin/agent-dispatch"
