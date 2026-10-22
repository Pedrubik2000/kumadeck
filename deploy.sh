#!/usr/bin/env bash
# Build the frontend and rsync the plugin straight into the Deck's
# ~/homebrew/plugins/, then restart Decky. No Docker, no zip.
#
# Reads connection settings from .vscode/settings.json (created from
# .vscode/defsettings.json on first run of the "settingscheck" task, or
# copy it yourself). Requires an SSH key that the Deck accepts (see
# README for the one-time setup).
set -euo pipefail
cd "$(dirname "$0")"

CFG=.vscode/settings.json
[[ -f $CFG ]] || { echo "Missing $CFG — copy .vscode/defsettings.json and edit it."; exit 1; }

cfgget() { python3 -c "import json,sys; print(json.load(open('$CFG'))['$1'])"; }
DECKIP=$(cfgget deckip)
DECKPORT=$(cfgget deckport)
DECKUSER=$(cfgget deckuser)
DECKPASS=$(cfgget deckpass)
KEYFILE=$(cfgget deckkey | sed -e 's/^-i //' -e "s|\${env:HOME}|$HOME|")
PLUGIN_DIR_NAME="kumadeck"
REMOTE_PLUGINS="/home/deck/homebrew/plugins"

SSH=(ssh -p "$DECKPORT" -i "$KEYFILE" "$DECKUSER@$DECKIP")

# Run a command as root on the Deck, feeding the sudo password over stdin
# (never embedded in the remote command string, so special characters and
# quoting can't corrupt it).
sudo_ssh() {
  printf '%s\n' "$DECKPASS" | "${SSH[@]}" "sudo -S -p '' sh -c '$1'"
}

if [[ "${1:-}" != "--no-build" ]]; then
  pnpm build
fi

echo "==> Preparing staging dir"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$PLUGIN_DIR_NAME"
cp -R dist main.py plugin.json package.json py_modules "$STAGE/$PLUGIN_DIR_NAME/"
[[ -d defaults ]] && cp -R defaults/. "$STAGE/$PLUGIN_DIR_NAME/" || true

# Decky root-owns loaded plugin dirs and its hot-reload watcher re-locks
# them the moment anything changes, so rsyncing into plugins/ directly is a
# losing race. Instead: rsync to a deck-owned staging dir, then swap it into
# place as root (same idea as the template's zip + sudo bsdtar flow).
REMOTE_STAGE="/home/deck/.kumadeck-deploy"

echo "==> Rsyncing plugin to staging dir on Deck"
# no --chmod: macOS ships openrsync which rejects it; perms are fixed remotely
rsync -az --delete \
  --rsh="ssh -p $DECKPORT -i $KEYFILE" \
  "$STAGE/$PLUGIN_DIR_NAME/" "$DECKUSER@$DECKIP:$REMOTE_STAGE/"

echo "==> Installing as root + restarting Decky (plugin_loader)"
sudo_ssh "rm -rf $REMOTE_PLUGINS/$PLUGIN_DIR_NAME \
  && cp -a $REMOTE_STAGE $REMOTE_PLUGINS/$PLUGIN_DIR_NAME \
  && chmod -R u+rwX,go+rX $REMOTE_PLUGINS/$PLUGIN_DIR_NAME \
  && rm -rf $REMOTE_STAGE \
  && systemctl restart plugin_loader"

echo "==> Done. Check the Quick Access menu on the Deck."
