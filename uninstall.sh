#!/bin/sh
# Remove the Claude auto-resume watchdog from this machine.
#
#   ./uninstall.sh            stop and remove the scheduler and the Remote Control
#                             Login Items (one per folder), switch the job queue
#                             off; keep logs and state
#   ./uninstall.sh --purge    also remove the tool, the launchers, the job git
#                             wrapper, its state and logs
#   ./uninstall.sh --obsidian also remove the Obsidian Login Item
#   ./uninstall.sh --force    go ahead while a sweep or job session runs (the
#                             scheduler stops; the session is left running, unwatched)

set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
TOOLS="$HOME/.claude/tools"
TOOL="$TOOLS/limit-resume.py"
CONF="$TOOLS/install.conf"
PURGE=0
OBSIDIAN=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
  --purge) PURGE=1 ;;
  --obsidian) OBSIDIAN=1 ;;
  --force) FORCE=1 ;;
  *) echo "unknown option: $arg (known: --purge, --obsidian, --force)"; exit 2 ;;
  esac
done

conf_get() { [ -f "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | head -n 1 || true; }
LABEL="${LIMIT_RESUME_LABEL:-$(conf_get label)}"
LABEL="${LABEL:-com.suvichak.claude-limit-resume}"
PYTHON=$(command -v python3 || true)
# The kit's own copy knows `reload-check` even when the installed tool is older.
CHECK="$SRC_DIR/limit-resume.py"
[ -f "$CHECK" ] || CHECK="$TOOL"

# Nothing is changed while work runs that would be left without a watchdog:
# a job session runs with bypassPermissions, and nothing would enforce its caps.
if [ "$FORCE" = 0 ] && [ -n "$PYTHON" ] && [ -f "$CHECK" ]; then
  if ! "$PYTHON" "$CHECK" reload-check; then
    echo "Not uninstalling: the work above would run on unwatched. First:"
    echo "  $PYTHON $TOOL jobs off       # start no new job"
    echo "  $PYTHON $TOOL jobs status    # wait until nothing is running or open"
    echo "then re-run, or ./uninstall.sh --force to leave it running."
    exit 1
  fi
fi

# Disarm and stop the queue first, so nothing starts between stopping and removing.
if [ -n "$PYTHON" ] && [ -f "$TOOL" ]; then
  "$PYTHON" "$TOOL" disarm all || true
  "$PYTHON" "$TOOL" jobs off || true      # `disarm all` keeps the queue on
fi

case "$(uname -s)" in
Darwin)
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
  echo "removed the LaunchAgent"
  # One Login Item per Remote Control folder; the list names each after its
  # file: claude-rc.command for the first folder, claude-rc-<name>.command.
  if osascript -e 'tell application "System Events" to delete login item "claude-rc.command"' \
       >/dev/null 2>&1; then
    echo "removed the Remote Control Login Item"
  else
    echo "NOTE: no Remote Control Login Item removed; check System Settings > General > Login Items"
  fi
  for f in "$TOOLS"/claude-rc-*.command; do
    [ -f "$f" ] || continue
    item=$(basename "$f")
    if osascript -e "tell application \"System Events\" to delete login item \"$item\"" \
         >/dev/null 2>&1; then
      echo "removed the Remote Control Login Item $item"
    else
      echo "NOTE: no Login Item $item removed; check System Settings > General > Login Items"
    fi
  done
  if [ "$OBSIDIAN" = 1 ]; then
    if osascript -e 'tell application "System Events" to delete login item "Obsidian"' \
         >/dev/null 2>&1; then
      echo "removed the Obsidian Login Item"
    else
      echo "NOTE: no Obsidian Login Item removed"
    fi
  fi
  ;;
Linux)
  systemctl --user disable --now "$LABEL.timer" "$LABEL-maintenance.timer" 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/$LABEL.service" \
        "$HOME/.config/systemd/user/$LABEL.timer" \
        "$HOME/.config/systemd/user/$LABEL-maintenance.service" \
        "$HOME/.config/systemd/user/$LABEL-maintenance.timer"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "removed the systemd units"
  # Other user services may rely on lingering, so it is left as it is.
  echo "NOTE: lingering is left on; to turn it off: sudo loginctl disable-linger $(id -un)"
  ;;
esac

echo "Running Remote Control servers are left running; close their Terminal windows to stop them."
echo "Delete the weekly Claude scheduled task too: ask Claude (in Claude Code or the desktop app) to"
echo "delete the scheduled task \"Auto-resume weekly maintenance\" (id auto-resume-weekly-maintenance in the app)."

if [ "$PURGE" = 1 ]; then
  rm -f "$TOOL" "$TOOLS/limit-resume.state.json" "$TOOLS/limit-resume.state.json.lock" \
        "$TOOLS/limit-resume.state.json.bak" \
        "$TOOLS/limit-resume.log" "$TOOLS/limit-resume.log.1" \
        "$TOOLS/limit-resume.out.log" "$TOOLS/limit-resume.err.log" \
        "$TOOLS/limit-resume.out.log.1" "$TOOLS/limit-resume.err.log.1" \
        "$TOOLS/limit-resume.usage.json" "$TOOLS/rc-last-exit" "$TOOLS/claude-rc.command" \
        "$CONF" "$TOOLS/sweep-stale" "$TOOLS/weekly-maintenance-task.json" "$TOOLS/weekly-check-setup.txt" \
        "$TOOLS/job-settings.json" "$TOOLS/usage.json" "$TOOLS/usage.json.lock" \
        "$TOOLS"/claude-rc-*.command "$TOOLS"/rc-last-exit-* "$TOOLS/rc-deadman.lock"
  rm -rf "$TOOLS/rc-launch.lock" "$TOOLS"/rc-launch-*.lock "$TOOLS/jobs" "$TOOLS/rc-debug" \
         "$TOOLS/jobbin"
  echo "purged the tool, the launchers, its state and its logs"
  for f in "$TOOLS"/*.bak* "$TOOLS"/*.retired-* "$TOOLS"/*.corrupt-*; do
    if [ -e "$f" ]; then
      echo "kept backup $f - remove it by hand if unwanted"
    fi
  done
else
  echo "kept $TOOLS/limit-resume.{py,state.json,log} and the launcher - rerun with --purge to remove"
fi
