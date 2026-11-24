#!/bin/sh
# Install the Claude auto-resume watchdog on this machine.
#
#   ./install.sh             install, start, health check
#   ./install.sh --no-start  write the files but do not load the scheduler now
#                            (macOS: launchd still loads the plist at next login)
#   ./install.sh --force     reload the scheduler even while a sweep, resume or
#                            job runs (the reload kills it), or install an older
#                            limit-resume.py over a newer one
#
# macOS -> LaunchAgent      (sweeps at login, then every 5 min)
# Linux -> systemd --user   (sweeps at boot, needs lingering on a headless box)
#
# Remote Control folders, one server each, named auto-<folder name>:
#   LIMIT_RESUME_RC_DIRS  several, separated by ':' or newlines
#   LIMIT_RESUME_RC_DIR   one (as before)
# Without either it asks, when run from a terminal, showing the list the last
# install saved in ~/.claude/tools/install.conf (Enter keeps it); otherwise it
# takes that list, else ~/_obsidian. Each folder must exist, and two folders
# may not share a name (their servers would both be auto-<name>).
#
# Everything it touches: ~/.claude/tools/ and one scheduler file. See
# INSTALL.md for what each scheduler key is for, and uninstall.sh to undo.

set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
TOOLS="$HOME/.claude/tools"
TOOL="$TOOLS/limit-resume.py"
CONF="$TOOLS/install.conf"
LAUNCHER="$TOOLS/claude-rc.command"
START=1
FORCE=0
for arg in "$@"; do
  case "$arg" in
  --no-start) START=0 ;;
  --force) FORCE=1 ;;
  *) echo "unknown option: $arg (known: --no-start, --force)"; exit 2 ;;
  esac
done

# install.conf: one key=value per line. rc_dir repeats, one line per folder; for
# any other key the first line wins.
conf_get() { [ -f "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | head -n 1 || true; }
conf_all() { [ -f "$CONF" ] && sed -n "s/^$1=//p" "$CONF" || true; }
# VERSION = "..." of a copy of limit-resume.py, empty if there is none.
version_of() { [ -f "$1" ] && sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$1" | head -n 1 || true; }
# True when version $1 sorts before $2 (versions are dates, YYYY-MM-DD).
older() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort | head -n 1)" = "$1" ]; }
NL='
'
# Paths land inside zsh double quotes (launcher), XML (plist), AppleScript
# strings and systemd units. Refuse what any of them would expand or misparse
# rather than escape it four ways.
unsafe() {
  case "$1" in
  *\"* | *\$* | *\`* | *\\* | *"$NL"*) return 0 ;;
  esac
  return 1
}

LABEL="${LIMIT_RESUME_LABEL:-$(conf_get label)}"
LABEL="${LABEL:-com.suvichak.claude-limit-resume}"
case "$LABEL" in
*[!A-Za-z0-9._-]*) echo "LIMIT_RESUME_LABEL may hold only letters, digits, . _ -: $LABEL"; exit 1 ;;
esac

# The Remote Control folders as given, one per line, in RC_LIST. An install.conf
# from the one-folder kit (a single rc_dir=) is a list of one, and is rewritten
# below in the same format.
SAVED=$(conf_all rc_dir)
if [ -z "$SAVED" ] && [ -f "$LAUNCHER" ]; then
  # Installed before install.conf existed: keep the folder the launcher serves.
  SAVED=$(sed -n 's/^cd "\(.*\)" || exit 1$/\1/p' "$LAUNCHER" | head -n 1)
fi
SAVED="${SAVED:-$HOME/_obsidian}"
if [ -n "${LIMIT_RESUME_RC_DIRS:-}" ]; then
  RC_LIST=$(printf '%s\n' "$LIMIT_RESUME_RC_DIRS" | tr ':' '\n')
elif [ -n "${LIMIT_RESUME_RC_DIR:-}" ]; then
  RC_LIST="$LIMIT_RESUME_RC_DIR"
elif [ -t 0 ]; then
  echo "Remote Control serves these folders now (one server each):"
  printf '%s\n' "$SAVED" | sed 's/^/  /'
  echo "Folders to serve: one per line, or several on a line separated by ':'."
  echo "An empty line ends the list; an empty first line keeps the list above."
  RC_LIST=""
  while printf 'folder: ' && IFS= read -r line && [ -n "$line" ]; do
    RC_LIST="$RC_LIST$(printf '%s\n' "$line" | tr ':' '\n')$NL"
  done
  [ -n "$RC_LIST" ] || RC_LIST="$SAVED"
else
  RC_LIST="$SAVED"
fi

PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "python3 not found - install it first"; exit 1; }
CLAUDE_BIN=$(command -v claude || echo "$HOME/.local/bin/claude")

# Every check runs before anything is written.
refuse_path() {
  echo "Not installing: this path holds a \" \$ \` \\ or newline, which the launcher"
  echo "and the scheduler file cannot carry safely: $1"
  exit 1
}
for value in "$HOME" "$PYTHON" "$CLAUDE_BIN"; do
  if unsafe "$value"; then refuse_path "$value"; fi
done
# RC_DIRS: the folders, resolved, in order, one per line; RC_BASES: their names.
RC_DIRS=""
RC_BASES=""
while IFS= read -r dir; do
  case "$dir" in
  "") continue ;;
  "~") dir="$HOME" ;;
  "~/"*) dir="$HOME/${dir#"~/"}" ;;
  esac
  if unsafe "$dir"; then refuse_path "$dir"; fi
  if [ ! -d "$dir" ]; then
    echo "Not installing: the Remote Control folder $dir does not exist."
    echo "Name the folders to serve:  LIMIT_RESUME_RC_DIRS=<folder>[:<folder>...] ./install.sh"
    echo "(they are saved in $CONF for later runs)"
    exit 1
  fi
  dir=$(cd "$dir" && pwd)
  case "$NL$RC_DIRS" in *"$NL$dir$NL"*) continue ;; esac     # listed twice
  if [ "$dir" = / ]; then
    echo "Not installing: Remote Control cannot serve / (a server is named after its folder)."
    exit 1
  fi
  base=$(basename "$dir")
  case "$NL$RC_BASES" in
  *"$NL$base$NL"*)
    # Refused, not renamed: the server name is auto-<folder name>, and the
    # watchdog, the launcher and doctor all derive it from the folder alone.
    echo "Not installing: two Remote Control folders are named \"$base\", and both"
    echo "servers would be auto-$base. Serve one of them, or rename one: $dir"
    exit 1
    ;;
  esac
  RC_DIRS="$RC_DIRS$dir$NL"
  RC_BASES="$RC_BASES$base$NL"
done <<EOF
$RC_LIST
EOF
[ -n "$RC_DIRS" ] || { echo "Not installing: no Remote Control folder given."; exit 1; }
NEW_VERSION=$(version_of "$SRC_DIR/limit-resume.py")
OLD_VERSION=$(version_of "$TOOL")
[ -n "$NEW_VERSION" ] || { echo "Not installing: $SRC_DIR/limit-resume.py has no VERSION line"; exit 1; }
echo "limit-resume.py $NEW_VERSION (installed: ${OLD_VERSION:-none})"
if [ -n "$OLD_VERSION" ] && older "$NEW_VERSION" "$OLD_VERSION" && [ "$FORCE" = 0 ]; then
  echo "Not installing: $NEW_VERSION is older than the installed $OLD_VERSION."
  echo "A sync tool may have reverted this folder. Check it; ./install.sh --force to downgrade."
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "WARNING: 'claude' is not on PATH here. Resumes will fail until it is."
fi

# Schedulers do not inherit your shell PATH, so spell it out for them.
AGENT_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
OS=$(uname -s)
SCHED_FAILED=0

case "$OS" in
Darwin)
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  DOMAIN="gui/$(id -u)"
  PLIST_NEW=$(mktemp "${TMPDIR:-/tmp}/limit-resume-plist.XXXXXX")
  trap 'rm -f "$PLIST_NEW"' EXIT
  # StartInterval + RunAtLoad only: a crashed sweep needs no KeepAlive or
  # ThrottleInterval, the next interval reruns it within 5 min. Trimming the
  # keys did NOT cure the 2026-11-20/21 stall: launchd kept the agent pending
  # ("pended nondemand spawn", runs not rising) even with this plist. Cause
  # unverified; see the runbook, "last sweep : NEVER or stale".
  cat > "$PLIST_NEW" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$TOOL</string>
    <string>sweep</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>AbandonProcessGroup</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$AGENT_PATH</string></dict>
  <key>StandardOutPath</key><string>$TOOLS/limit-resume.out.log</string>
  <key>StandardErrorPath</key><string>$TOOLS/limit-resume.err.log</string>
</dict>
</plist>
PLISTEOF
  if ! plutil -lint "$PLIST_NEW" >/dev/null; then
    echo "Not installing: the generated plist does not lint (an & or < in a path?)"
    exit 1
  fi
  if grep -qE '<key>(KeepAlive|ThrottleInterval|ProcessType|LowPriorityIO)</key>' "$PLIST_NEW"; then
    echo "Not installing: the plist has keys dropped on 2026-11-21 - is install.sh an old copy?"
    exit 1
  fi
  CHANGED=1
  [ -f "$PLIST" ] && cmp -s "$PLIST_NEW" "$PLIST" && CHANGED=0
  LOADED=0
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && LOADED=1
  # A reload (bootout) kills a running sweep with the resume inside it, whose
  # stop is already marked handled and never retried, and maybe job sessions.
  if [ "$START" = 1 ] && [ "$CHANGED" = 1 ] && [ "$LOADED" = 1 ] && [ "$FORCE" = 0 ]; then
    if ! "$PYTHON" "$SRC_DIR/limit-resume.py" reload-check; then
      echo "Not installing: the scheduler file changed, and reloading it would kill the"
      echo "work above. Re-run once it has ended (\`limit-resume.py jobs status\`), or"
      echo "./install.sh --force to reload anyway."
      exit 1
    fi
  fi
  ;;
Linux) ;;
*)
  echo "Not installing: $OS is not supported. The kit runs on macOS and Linux only"
  echo "(it needs fcntl, a pty, ps and a launchd or systemd scheduler); Windows is not supported."
  exit 1
  ;;
esac

echo "installing $TOOL"
mkdir -p "$TOOLS"
# Renamed into place: a sweep or a jobpty starting mid-copy never reads half a file.
cp "$SRC_DIR/limit-resume.py" "$TOOL.new"
chmod +x "$TOOL.new"
mv "$TOOL.new" "$TOOL"

# What this install used, for the next run and for limit-resume.py (label).
# One key=value per line; rc_dir repeats, one line per folder, in order.
{
  printf '%s' "$RC_DIRS" | sed 's/^/rc_dir=/'
  printf 'label=%s\n' "$LABEL"
  printf 'src=%s\n' "$SRC_DIR"            # the weekly check-up upgrades from here
} > "$CONF.new"
mv "$CONF.new" "$CONF"
echo "wrote $CONF"

# Every Claude Code session on this machine starts in auto mode, not manual:
# permissions.defaultMode = "auto" in the user settings, merged into what is
# there, with a backup before a change. A file that does not parse is left alone.
SETTINGS="$HOME/.claude/settings.json"
"$PYTHON" - "$SETTINGS" <<'SETEOF'
import json, os, shutil, sys, time
path = sys.argv[1]
try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
except ValueError as exc:
    print("NOTE: %s does not parse (%s); default permission mode not set" % (path, exc))
    sys.exit(0)
if not isinstance(data, dict) or not isinstance(data.get("permissions", {}), dict):
    print("NOTE: %s has an unexpected shape; default permission mode not set" % path)
    sys.exit(0)
perms = data.setdefault("permissions", {})
if perms.get("defaultMode") == "auto":
    print("default permission mode already auto (%s)" % path)
    sys.exit(0)
if os.path.exists(path):
    shutil.copy2(path, "%s.bak-%s" % (path, time.strftime("%Y%m%d%H%M%S")))
old = perms.get("defaultMode")
perms["defaultMode"] = "auto"
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".new", "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(path + ".new", path)
print("default permission mode: %s -> auto (%s)" % (old if old not in (None, "default") else "manual", path))
SETEOF

# One Remote Control launcher per folder. Generated rather than copied so paths
# and the served folder match this machine. The template is filled in with sed,
# never a shell heredoc: an unquoted heredoc expands $(pwd) at install time and
# executes anything in backticks. The values land in zsh double quotes, hence
# `unsafe`. The first folder keeps the one-folder kit's names (claude-rc.command,
# rc-last-exit, rc-launch.lock), so a server it started stays watched; each
# other folder gets claude-rc-<name>.command and files tagged -<name>.
# Escape what sed would read as syntax in a replacement, and write to a temp
# file first: a launcher that is running right now is still being read by zsh.
sed_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
LAUNCHERS=""
while IFS= read -r dir; do
  [ -n "$dir" ] || continue
  base=$(basename "$dir")
  TAG="-$base"
  [ -n "$LAUNCHERS" ] || TAG=""             # the first folder
  file="$TOOLS/claude-rc$TAG.command"
  sed -e "s|@RC_DIR@|$(sed_escape "$dir")|g" \
      -e "s|@CLAUDE_BIN@|$(sed_escape "$CLAUDE_BIN")|g" \
      -e "s|@PYTHON@|$(sed_escape "$PYTHON")|g" \
      -e "s|@RC_NAME@|$(sed_escape "auto-$base")|g" \
      -e "s|@RC_TAG@|$(sed_escape "$TAG")|g" \
      "$SRC_DIR/claude-rc.command.in" > "$file.new"
  mv "$file.new" "$file"
  chmod +x "$file"
  echo "wrote $file (serves $dir)"
  LAUNCHERS="$LAUNCHERS$file$NL"
done <<EOF
$RC_DIRS
EOF
# A folder dropped from the list: its launcher goes, and its Login Item below.
DROPPED=""
for file in "$TOOLS"/claude-rc-*.command; do
  [ -f "$file" ] || continue
  case "$NL$LAUNCHERS" in *"$NL$file$NL"*) continue ;; esac
  rm -f "$file"
  echo "removed $file (folder no longer served; a server it started keeps running)"
  DROPPED="$DROPPED$file$NL"
done

if [ "$START" = 1 ]; then
  # Weekly maintenance, first run: records this account's weekly reset (one
  # Haiku turn; needs `claude` logged in) and tidies old state. Before the
  # scheduler loads, so it does not race the first sweep for the state file.
  echo "weekly maintenance (records this account's weekly reset):"
  "$PYTHON" "$TOOL" maintain --install || \
    echo "NOTE: maintenance failed - is claude logged in? The scheduled task below reads usage from the app instead."
fi

# launchctl print succeeds once the agent is loaded.
loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
# Bootout returns before the service is gone, and bootstrap can fail
# transiently ("5: Input/output error", unverified here): wait, then retry.
reload_agent() {
  if loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    n=0
    while loaded && [ "$n" -lt 10 ]; do sleep 1; n=$((n + 1)); done
  fi
  n=0
  while [ "$n" -lt 3 ]; do
    launchctl bootstrap "$DOMAIN" "$PLIST" && loaded && return 0
    n=$((n + 1))
    sleep 1
  done
  return 1
}

case "$OS" in
Darwin)
  mkdir -p "$HOME/Library/LaunchAgents"
  if [ "$CHANGED" = 1 ]; then
    mv "$PLIST_NEW" "$PLIST"
    echo "wrote $PLIST"
  else
    echo "$PLIST unchanged"
  fi
  if [ "$START" = 0 ]; then
    echo "not loaded now (--no-start); launchd loads $PLIST at the next login"
  elif [ "$CHANGED" = 0 ] && [ "$LOADED" = 1 ]; then
    echo "scheduler unchanged and loaded - not reloaded"
  elif reload_agent; then
    echo "loaded into launchd"
  else
    SCHED_FAILED=1
    echo "SCHEDULER NOT LOADED - no sweep will run. Fix: launchctl bootstrap $DOMAIN $PLIST"
  fi
  ;;
Linux)
  UNITS="$HOME/.config/systemd/user"
  mkdir -p "$UNITS"
  # % is a systemd specifier; quotes keep a path with spaces in one argument.
  sd_escape() { printf '%s' "$1" | sed -e 's/%/%%/g'; }
  cat > "$UNITS/$LABEL.service" <<UNITEOF
[Unit]
Description=Resume Claude Code sessions stopped by the usage limit

[Service]
Type=oneshot
Environment="PATH=$(sd_escape "$AGENT_PATH")"
ExecStart="$(sd_escape "$PYTHON")" "$(sd_escape "$TOOL")" sweep
# A background job outlives the sweep that started it; don't kill it with the unit.
KillMode=process
# No Restart=: the timer reruns a crashed sweep within 5 min, and systemd
# before ~244 refuses Restart= on a oneshot unit.
UNITEOF
  cat > "$UNITS/$LABEL.timer" <<UNITEOF
[Unit]
Description=Sweep for rate-limited Claude sessions every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNITEOF
  echo "wrote $UNITS/$LABEL.{service,timer}"
  # 2026-11-23.2 ran the weekly maintenance from a timer here; the Claude
  # scheduled task does it now (see the end of this script).
  if [ -f "$UNITS/$LABEL-maintenance.timer" ]; then
    systemctl --user disable --now "$LABEL-maintenance.timer" 2>/dev/null || true
    rm -f "$UNITS/$LABEL-maintenance.service" "$UNITS/$LABEL-maintenance.timer"
    echo "removed $LABEL-maintenance.{service,timer} (the Claude scheduled task replaces it)"
  fi
  if [ "$START" = 1 ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "$LABEL.timer"
    echo "enabled $LABEL.timer"
    # Without lingering, user units stop at logout: the watchdog would die with
    # your SSH session and never run at boot.
    if ! loginctl show-user "$(id -un)" 2>/dev/null | grep -q "Linger=yes"; then
      loginctl enable-linger "$(id -un)" 2>/dev/null || \
        echo "NOTE: enable lingering or this stops at logout: sudo loginctl enable-linger $(id -un)"
    fi
  fi
  ;;
esac

if [ "$OS" = "Darwin" ] && [ "$START" = 1 ]; then
  # A Login Item per folder, so Terminal opens Remote Control after a reboot.
  # The sweep watchdog restarts a server that dies mid-session; this covers a
  # cold boot. The list names each item after its file.
  ITEMS=$(osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null || true)
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    item=$(basename "$file")
    if printf '%s\n' "$ITEMS" | grep -qF "$item"; then
      echo "login item $item already present"
    else
      osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$file\", hidden:false, name:\"Claude Remote Control\"}" >/dev/null 2>&1 \
        && echo "registered $file as a Login Item" \
        || echo "NOTE: could not add the Login Item; add $file by hand in System Settings > General > Login Items"
    fi
  done <<EOF
$LAUNCHERS
EOF
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    item=$(basename "$file")
    if osascript -e "tell application \"System Events\" to delete login item \"$item\"" >/dev/null 2>&1; then
      echo "removed the Login Item $item"
    fi
  done <<EOF
$DROPPED
EOF

  # Obsidian at login too, so the vault (and LiveSync) is up after a reboot.
  # Skipped silently on machines without Obsidian; LIMIT_RESUME_NO_OBSIDIAN=1 opts out.
  OBSIDIAN_APP=""
  for a in /Applications/Obsidian.app "$HOME/Applications/Obsidian.app"; do
    [ -d "$a" ] && OBSIDIAN_APP="$a" && break
  done
  if [ -n "$OBSIDIAN_APP" ] && [ "${LIMIT_RESUME_NO_OBSIDIAN:-0}" != 1 ]; then
    if osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null \
       | grep -q "Obsidian"; then
      echo "Obsidian login item already present"
    else
      osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$OBSIDIAN_APP\", hidden:false}" >/dev/null 2>&1 \
        && echo "registered $OBSIDIAN_APP as a Login Item" \
        || echo "NOTE: could not add Obsidian; add it in System Settings > General > Login Items"
    fi
  fi
fi

# Every week after that it is run by a Claude scheduled task: a Claude Code
# scheduled task that starts a fresh session on this machine through its
# Remote Control server, or the desktop app's scheduler. Only Claude can create
# one, so print the prompt that does it, for a human to paste into Claude Code.
SETUP="$TOOLS/weekly-check-setup.txt"
"$PYTHON" "$TOOL" task-setup > "$SETUP"
rm -f "$TOOLS/weekly-maintenance-task.json"     # before 2026-11-23.3
echo
echo "LAST STEP - set up the weekly check-up (a Claude agent that runs maintenance,"
echo "checks health and makes safe repairs). Open a Claude Code session ON THIS"
echo "MACHINE - through Remote Control (the Claude app or claude.ai/code) or the"
echo "Claude desktop app - and paste everything between the lines (also in $SETUP):"
echo "================================================================================"
cat "$SETUP"
echo "================================================================================"

echo
echo "next:"
echo "  $PYTHON $TOOL auto on     # cover every session on this machine"
if [ "$OS" = "Darwin" ]; then
  echo "  $PYTHON $TOOL rc on       # restart Remote Control if it dies"
fi
echo "  $PYTHON $TOOL doctor      # confirm it is live"
echo "  $PYTHON $TOOL notify      # test the alert; prints how to add Pushover keys"

if [ "$SCHED_FAILED" = 1 ]; then
  echo
  echo "SCHEDULER NOT LOADED - see above."
  exit 1
fi
