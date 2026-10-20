#!/bin/bash
# ./install.sh               copy the tools to ~/.local-first/lib and link the commands into ~/.local/bin
# ./install.sh --meter       also build the menu-bar meter, start it at login, and refresh usage + suggestions every 15 min
# ./install.sh --relay       also schedule relay (hands routine work to cheap mode when Claude usage is empty; see README)
# ./install.sh --uninstall   unload jobs, move the lib folder and links to the Trash
#
# Why copy? launchd jobs cannot read ~/Desktop, ~/Documents or ~/Downloads on macOS (privacy / TCC). If you cloned this repo
# there, the scheduled wrapper would fail. The copy lives in ~/.local-first/lib, which launchd can read.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
LF="${LOCAL_FIRST_HOME:-$HOME/.local-first}"; LIB="$LF/lib"; BIN="$HOME/.local/bin"; LA="$HOME/Library/LaunchAgents"
TRASH="$HOME/.Trash"; STAMP="$(date +%s)"
CMDS="localjob ladder scriptify localfirst claude-cheap claude-cheap-key cheap-job"
WANT_METER=0; WANT_RELAY=0
for a in "$@"; do case "$a" in
  --meter) WANT_METER=1;; --relay) WANT_RELAY=1;;
  --uninstall)
    for j in com.local-first.usage-meter; do launchctl bootout "gui/$(id -u)/$j" 2>/dev/null || true
      [ -e "$LA/$j.plist" ] && mv "$LA/$j.plist" "$TRASH/$j.plist.$STAMP"; done
    for n in localfirst-refresh relay; do [ -e "$LF/jobs/$n.json" ] && "$LIB/bin/localjob" remove "$n" || true; done
    for c in $CMDS; do [ -L "$BIN/$c" ] && mv "$BIN/$c" "$TRASH/$c.link.$STAMP"; done
    [ -d "$LIB" ] && mv "$LIB" "$TRASH/local-first-lib.$STAMP"
    echo "Uninstalled. Your config, logs, INBOX.md and decisions in $LF were left alone."; exit 0;;
  *) echo "unknown option $a"; exit 2;;
esac; done

mkdir -p "$LF" "$BIN" "$LIB"
if [ -d "$LIB/local_first" ]; then mv "$LIB" "$TRASH/local-first-lib.$STAMP"; mkdir -p "$LIB"; fi
for d in local_first bin relay cheap meter; do cp -R "$HERE/$d" "$LIB/$d"; done
find "$LIB" -name __pycache__ -prune -exec mv {} "$TRASH/pycache.$STAMP.$$" \; 2>/dev/null || true
[ -e "$LF/config.json" ] || cp "$HERE/config.example.json" "$LF/config.json"
for c in $CMDS; do
  if [ -e "$BIN/$c" ] && [ ! -L "$BIN/$c" ]; then echo "skipped $BIN/$c (a real file is already there; move it away to use ours)"; continue; fi
  ln -sfn "$LIB/bin/$c" "$BIN/$c"
done
echo "Installed to $LIB. Commands linked into $BIN (add it to your PATH if needed)."

if [ "$WANT_METER" = 1 ]; then
  "$LIB/meter/build.sh"
  mkdir -p "$LA"; PL="$LA/com.local-first.usage-meter.plist"
  sed "s#__HOME__#$HOME#g" "$LIB/meter/launchd/com.local-first.usage-meter.plist.template" > "$PL"; plutil -lint "$PL" >/dev/null
  launchctl bootout "gui/$(id -u)/com.local-first.usage-meter" 2>/dev/null || true; launchctl bootstrap "gui/$(id -u)" "$PL"
  "$LIB/bin/localjob" add localfirst-refresh --every 15m --inbox never --run-at-load -- "$LIB/bin/localfirst" refresh --estimate
  echo "Meter started. Calibrate the token budgets in $LF/config.json (README, 'Calibrating the meter')."
fi
if [ "$WANT_RELAY" = 1 ]; then
  "$LIB/bin/localjob" add relay --every 10m --inbox never -- /usr/bin/python3 "$LIB/relay/relay.py"
  echo "relay scheduled. It only tells you when usage is empty unless you set auto_run (README)."
fi
echo "Try:  localjob add disk-check --every 1h -- df -h /     then   localjob list"
