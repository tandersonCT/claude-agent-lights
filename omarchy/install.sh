#!/usr/bin/env bash
# Install the Omarchy bar widget: copies the widget and the engine into
# ~/.config/omarchy/plugins/tanderson.agent-lights/ and restarts the shell.
# Then add {"id": "tanderson.agent-lights"} to a bar section in
# ~/.config/omarchy/shell.json (e.g. under "left", after your other widgets).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/tanderson.agent-lights"

mkdir -p "$dest"
install -m 644 "$here"/*.qml "$here/manifest.json" "$dest/"
install -m 755 "$here/../core/agent_lights.py" "$dest/agent-lights"
echo "Installed to $dest"

if ! grep -q '"tanderson.agent-lights"' "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json" 2>/dev/null; then
  echo 'Now add {"id": "tanderson.agent-lights"} to a bar section in ~/.config/omarchy/shell.json'
fi
omarchy restart shell >/dev/null 2>&1 || echo "Restart the Omarchy shell to load it."
