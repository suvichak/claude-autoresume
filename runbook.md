# Runbook — auto-resume watchdog

Design and rationale: [[README]].

## Is it working?

```bash
python3 ~/.claude/tools/limit-resume.py doctor
```

```
script     : /Users/suvichak/.claude/tools/limit-resume.py
version    : 2026-11-21
claude bin : /Users/suvichak/.local/bin/claude
launchd : loaded, runs=209 last exit=0
last sweep : 2026-11-22T10:03:01 (2m ago) OK
remote ctl : watched, running
obsidian   : watched, running
alerts     : this screen only (no Pushover keys in the Keychain)
auto mode  : True / armed: 1
maintenance: 2026-11-21T16:09 (install, 0d ago); scheduled task: never ran
jobs       : on, /Users/suvichak/_obsidian/Jobs

healthy
```

Exit code is 0 when healthy, 1 when not. `last sweep` older than 15 min means
the agent is not running — that is the signal that something broke. A stale
sweep also raises one alert by itself: the Remote Control launcher runs
`limit-resume.py deadman` every 10 min (it is started by Terminal, not launchd),
so a stall is reported even when no sweep runs to report it. With several
Remote Control folders only one launcher asks (the one holding
`rc-deadman.lock`).

With several Remote Control folders, `doctor` prints one `remote ctl` line per
folder, ending in the folder and its server name, e.g.
`remote ctl : watched, running - /Users/you/vault (auto-vault)`.

## Daily commands

```bash
python3 ~/.claude/tools/limit-resume.py status       # what is covered / blocked
python3 ~/.claude/tools/limit-resume.py auto on|off  # cover every session, or not
python3 ~/.claude/tools/limit-resume.py arm <sid>    # watch one session, no age limit
python3 ~/.claude/tools/limit-resume.py disarm all   # auto mode and rc watch off, nothing watched
python3 ~/.claude/tools/limit-resume.py disarm <sid> # never resume that session (`arm` undoes it)
python3 ~/.claude/tools/limit-resume.py rc on|off    # restart Remote Control if it dies
python3 ~/.claude/tools/limit-resume.py obsidian on|off  # reopen the Obsidian app if it quits (macOS)
python3 ~/.claude/tools/limit-resume.py notify       # send a test alert (Pushover, else on-screen)
python3 ~/.claude/tools/limit-resume.py maintain     # weekly maintenance now (probes usage): reset date, tidy, summary
python3 ~/.claude/tools/limit-resume.py task-spec    # the Claude scheduled task that runs it weekly, as JSON
python3 ~/.claude/tools/limit-resume.py task-setup   # the prompt to paste into Claude Code on this machine to create it
tail -20 ~/.claude/tools/limit-resume.log            # what it actually did
```

Background jobs ([[README]] → Background jobs):

```bash
python3 ~/.claude/tools/limit-resume.py jobs on "<folder>" --cwd <dir>  # run that folder's job notes
python3 ~/.claude/tools/limit-resume.py jobs on --reset "Thu 14:00"     # only until a real reset time is seen
python3 ~/.claude/tools/limit-resume.py jobs status   # gate, usage, every job
python3 ~/.claude/tools/limit-resume.py jobs probe    # fresh usage reading (one Haiku turn)
python3 ~/.claude/tools/limit-resume.py jobs off      # stop starting jobs (a running one finishes)
python3 ~/.claude/tools/limit-resume.py jobs stop     # stop the running job now; its note is set to hold
```

Jobs run as Remote Control sessions named `job: <title>`: open one from the
Remote Control list on the phone or claude.ai to watch it. A `blocked` job is
waiting for your answer there. A job's folder must be trusted (run `claude`
there once and accept), or it fails before launch.

To queue a job, set `status: queued` in its note. To retry a failed or blocked
one from scratch, set `status: queued` again: its resume count starts over. To
park one, `status: hold`; setting that (or any status but `running`) on a
running job stops its session at the next sweep and keeps your status. A job
stuck as `running` after a crash is watched again if its session still runs,
otherwise set back to `paused` by the next sweep and resumed. A note whose
`runner:` names another machine is left alone here; delete the line to hand
it over. `jobs status` shows a paused job's `paused_until` (a quota stop's
reset: not resumed before it).

If a job is `paused` with `usage unknown: …`, the usage figures are missing or
over 10 min old: run `jobs probe`. If the probe fails (`last probe :` in
`jobs status`, one alert), `claude /login` on this machine is the usual fix.

Free usage readings: set the status line in `~/.claude/settings.json` to

```json
"statusLine": {"type": "command",
               "command": "python3 ~/.claude/tools/limit-resume.py statusline"}
```

or, to keep your own status line, `... statusline -- <your command>`.

`arm` with a custom instruction:

```bash
python3 ~/.claude/tools/limit-resume.py arm <session-id> -- \
  "Continue the proposal work. Reply DONE when finished."
```

Session ids are the transcript filenames under
`~/.claude/projects/<project>/<session-id>.jsonl`.

## Files

| path | what |
|---|---|
| `~/.claude/tools/limit-resume.py` | the whole tool |
| `~/.claude/tools/limit-resume.state.json` | armed sessions, `handled` blocks, heartbeat, the active task |
| `~/.claude/tools/limit-resume.state.json.bak` | copy of the last good save; used if the state file cannot be read |
| `~/.claude/tools/limit-resume.state.json.lock` | the state lock (flock): held by a sweep for its whole run and by each command that writes the state |
| `~/.claude/tools/limit-resume.state.json.corrupt-<time>` | a state file that could not be read, kept for you to inspect; `doctor` says NEEDS ATTENTION until it is deleted |
| `~/.claude/tools/limit-resume.log` | every decision and resume |
| `~/.claude/tools/limit-resume.{out,err}.log` | raw launchd stdout/stderr |
| `~/.claude/tools/claude-rc.command` | Remote Control launcher of the first folder, generated from `claude-rc.command.in` |
| `~/.claude/tools/claude-rc-<name>.command` | the launcher of each other folder (`<name>`: the folder's name; its server is `auto-<name>`) |
| `~/.claude/tools/install.conf` | what the last install used: one `rc_dir=` line per Remote Control folder, in order, and `label=` |
| `~/.claude/tools/rc-last-exit` | how the first folder's launcher's last run ended: `<epoch> <auth\|network\|exit> <code> <seconds>`; `rc-last-exit-<name>` for each other folder |
| `~/.claude/tools/rc-launch.lock` | lock held while the first folder's launcher is alive (empty file); `rc-launch-<name>.lock` for each other folder |
| `~/.claude/tools/rc-deadman.lock` | held by the one launcher that runs the dead-man check |
| `~/.claude/tools/jobbin/git` | the job git wrapper: first on a job session's PATH, rewritten at each job launch; asks `limit-resume.py git-check` before the real git |
| `~/.claude/tools/rc-debug/` | Remote Control debug logs, pruned after 14 days |
| `~/.claude/tools/weekly-check-setup.txt` | the paste-in prompt that creates the weekly scheduled task (`task-setup`) |
| `~/.claude/scheduled-tasks/auto-resume-weekly-maintenance/` | the desktop app's scheduled task, when it was created there (a Claude Code scheduled task lives on claude.ai) |
| `~/.claude/tools/usage.json` | last known 5-hour / weekly usage, per window, with source and time |
| `~/.claude/tools/jobs/<session_id>.pty.log` | a job session's terminal screen (`.pid` beside it: the claude pid) |
| `~/.claude/tools/jobs/<session_id>.resume.{out,err,exit}` | a headless resume's reply, errors and exit status, read by the sweep after it |
| `~/.claude/tools/job-settings.json` | settings passed to job sessions: guard hook on every tool, status line, no bypass dialog. Written at each job launch, so it does not exist before the first job |
| `~/.claude/tools/usage.json.lock` | lock held while a status line or probe merges a reading into `usage.json` |
| `~/Library/LaunchAgents/com.suvichak.claude-limit-resume.plist` | the schedule |

## Install / reload / remove

Re-running the installer is the reload. It rewrites the scheduler file and
reloads it only if the file changed; it refuses (`--force` overrides) while a
sweep, resume or job session runs, because the reload would kill it. Run
from a terminal it asks which folders Remote Control should serve, showing the
saved list from `~/.claude/tools/install.conf`; press Enter to keep it. Without
a terminal it keeps the saved list (or takes `LIMIT_RESUME_RC_DIRS`):

```bash
cd ~/Project/claude-auto-resume
./install.sh
LIMIT_RESUME_RC_DIRS="$HOME/_obsidian:$HOME/Project/notes" ./install.sh   # set the list, no question
./uninstall.sh            # stop and remove the scheduler and every RC Login Item, jobs off; keep state and logs
./uninstall.sh --purge    # also delete the tool, the launchers, the git wrapper, state and logs
```

Two folders with the same name are refused (both servers would be
`auto-<name>`). A folder dropped from the list loses its launcher and Login
Item; a server it started keeps running until you close its Terminal window.

Full details and the manual equivalents: [[INSTALL]].

## Rebooting without losing the watchdog (macOS + FileVault)

macOS LaunchAgents start at **login**, not at boot, and a FileVault volume waits
at the unlock screen until someone types a password. So a plain `reboot` leaves
the watchdog dead until you sign in.

For a reboot you are choosing to do, use an authenticated restart instead:

```bash
sudo fdesetup authrestart              # unlock now, reboot straight to the desktop
sudo fdesetup authrestart -delayminutes 5
```

It unlocks the volume for that one reboot, so the machine comes back logged in,
the LaunchAgent loads, and `RunAtLoad` sweeps within seconds. Encryption stays
on — the key is held for a single restart, not stored.

Confirm afterwards:

```bash
python3 ~/.claude/tools/limit-resume.py doctor
```

Automatic login would remove the need for this, but macOS does not allow it
while FileVault is enabled, and turning FileVault off to get it writes your
login password into `/etc/kcpassword` with reversible obfuscation. Not worth it
on a machine holding Claude credentials and homelab keys.

**Still uncovered:** an *unplanned* reboot — power loss, a host restart, a panic
— lands at the unlock screen with no one there. The watchdog resumes as soon as
you log in; anything blocked in the meantime just waits.

## Tuning

Constants at the top of `limit-resume.py`:

| name | default | effect |
|---|---|---|
| `AUTO_MAX_AGE_H` | 12 | auto mode skips a stop whose limit reset longer ago than this (one alert per stop that reset within `AGE_ALERT_H`, 7 days) |
| `MAX_PER_SWEEP` | 2 | resume attempts per 5-min sweep. One task at a time: a launched resume, relay or new chat ends the batch until it is done |
| `RESUME_BUSY` | 900 s | a resumed chat counts as running until its transcript is this quiet; no job starts meanwhile |
| `CHAT_NEW_AT` | 700000 | context tokens above which a chat continues in a new Remote Control session |
| `JOB_NEW_AT` | 400000 | context tokens above which a paused job restarts in a new session |
| `MAX_RESUMES` | 8 | resumes in a row without you typing in the session, then it is retired (with an alert) until you do |
| `BUFFER` | 90 s | grace past the reset time |
| `RESUME_TIMEOUT` | 3600 s | a headless resume still running this long is stopped by the next sweep, with everything it started |
| `STATE_LOCK_WAIT` | 300 s | how long a sweep or command waits for the state lock before giving up, unchanged |
| `PERM` | `acceptEdits` | fallback permission mode; a resume normally reuses the session's own last mode |

| `SEND_MODEL` | `claude-haiku-5-5` | model of the relay that posts "continue" into a Remote Control chat |
| `SEND_TIMEOUT` | 300 s | when a relay is abandoned (→ alert) |
| `CC_SOCKS` | `/tmp/cc-socks` | where each local session's inbox socket `<pid>.sock` lives |
| `RC_HUNG` | 900 s | Remote Control debug log silent this long → suspected frozen |
| `RC_HUNG_CONFIRM` | 600 s | still silent this much later → killed and relaunched |

`LIMIT_RESUME_PERMISSION_MODE`, if set, forces one mode for every resume.

## Troubleshooting

**`launchd : NOT LOADED`** — re-run `install.sh`, or load it by hand:
`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.suvichak.claude-limit-resume.plist`.

**`launchd : program … MISSING`** — the python the plist names is gone (e.g.
a Homebrew python removed). Re-run `install.sh`.

**`maintenance: … scheduled task: never ran STALE`** — install runs count as
maintenance, but only the weekly Claude scheduled task clears this: paste
`limit-resume.py task-setup` into a Claude Code session on this machine
(README → Weekly maintenance) and check that it runs. Once created, it only
fires while this machine's Remote Control server or Claude app is up.

**Job lines under `jobs :`** — `job runner CRASHED` (a traceback in
`limit-resume.log`; a running job is not watched until it is fixed), `usage
probe failing` (`claude /login`, then `jobs probe`), `usage reading … old
while N job(s) wait`, `running job's session is gone` (the next sweep pauses
it), `past JOB_MAX_H` (the sweep is not running) each make doctor say NEEDS
ATTENTION. `open session:` lines only list sessions kept open after a job;
`- you are working in it, the queue waits` marks a finished job you typed into:
the next job and due resumes wait until it has been quiet for 15 min
(`RESUME_BUSY`).

**`Limit reset, but chat … may still be open: a claude process (pid N) works in
<folder>`** — a `claude` is running in that chat's folder, so the chat was not
resumed headless. If it is that chat, type `continue` in it. If it is another
chat in the same folder, type `continue` in the stopped chat yourself (open it
with `claude --resume <id>`); the stop is handled and not retried. `lsof failed`
in the same alert means the check could not run (fail safe).

**A job's git says `Blocked by limit-resume: …` or `git: not found on PATH
(limit-resume job wrapper)`** — the job git wrapper refused a git that writes
(intended; it also refuses when `limit-resume.py` or its python cannot run), or
found no real git when it was written: check `which -a git` in the sweep's
PATH and let the next job launch rewrite it.

**`last sweep : NEVER` or stale** — the agent is loaded but not executing.
Check `limit-resume.err.log` for a Python traceback. If `doctor` also prints
`held : launchd is holding the agent` (`launchctl print
gui/$UID/com.suvichak.claude-limit-resume` shows `runs` not rising and
`pended nondemand spawn = ...`), launchd is holding the agent: seen
2026-11-20/21 with both the 2026-11-18 plist and the trimmed 2026-11-21 one,
so re-running `install.sh` does not help. Cause unverified (the unified log
reportedly says "domain in on-demand-only mode"). Check System Settings →
General → Login Items & Extensions → Allow in the Background.
`launchctl kickstart gui/$UID/com.suvichak.claude-limit-resume` gets one sweep.
On 2026-11-21 the hold cleared by itself at 16:30 (`on-demand count` gone from
`launchctl print gui/$UID`, sweeps every 5 min again); what released it is
unverified. `sfltool dumpbtm` needs an admin password.

**`claude bin : NOT FOUND`** — the `PATH` key in the plist does not include
`~/.local/bin`. launchd jobs do not inherit your shell PATH.

**A session never resumed.** Confirm its transcript actually ends on a quota
stop — not a `529`, connection error or a normal exit, which are all ignored on
purpose:

```bash
python3 - <<'EOF'
import importlib.util, glob, datetime, time
spec = importlib.util.spec_from_file_location("lr", "/Users/suvichak/.claude/tools/limit-resume.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
for f in glob.glob("/Users/suvichak/.claude/projects/*/*.jsonl"):
    b = lr.find_block(f)
    if b:
        print(f.split("/")[-1][:8], b["window"],
              datetime.datetime.fromtimestamp(b["resets_at"]),
              "reset %.1fh ago" % ((time.time() - b["resets_at"]) / 3600))
EOF
```

If its limit reset more than 12 h ago, auto mode's age guard skipped it, and
the log says `not resumed - its limit reset … ago` (one alert per stop) —
`arm` it explicitly to override. `skipping transcript of <sid>, it could not
be read` means its transcript vanished or has a malformed tail; the others are
still resumed. `open in another claude process (pid N), not resumed headless`
means a terminal still has it open: type "continue" there.

**A session resumed that should not have.** `disarm <sid>` it (it is never
resumed again; `arm <sid>` undoes that), then `auto off` if auto-discovery is
too broad, and arm the specific sessions you care about instead. `auto off`
also stops every session auto mode resumed before.

**A session stopped being resumed.** `status` shows it `done (max resumes)`
(8 resumes with no turn typed by you; you got an alert) or `done (DONE)`.
Either lifts by itself once you type in the session and it stops on a limit
again.

**`state : UNREADABLE` / `was unreadable, kept as …`.** The state file did not
parse. The first sweep after moves it aside as `.corrupt-<time>`, alerts, and
carries on from `.bak` (the last good save). Check the kept copy, then delete it.

**`last crash : …`.** The last sweep raised; the traceback is in
`limit-resume.log` (`CRASH in sweep`). One alert per run of crashes; the line
goes once a sweep runs cleanly.

**A command says `the state file stayed locked`.** A sweep (a Remote Control
relay or a usage probe can take minutes) or another command held it for 5
minutes. Nothing was changed; run the command again.

**A Remote Control chat didn't continue.** Look in the log for the line
`is Remote Control session cse_… (pid N): sending via its inbox`:

| log says | meaning / fix |
|---|---|
| `has no live child/inbox (pid None)` | Remote Control restarted or the chat closed after the stop. An alert was sent; type "continue" in the chat. |
| `relay exit=0 out=SENT` but nothing happened | The chat may run in a different permission mode than its last turn recorded, and it holds the message for approval. Open the chat and approve it or type "continue". |
| `relay exit=… out=FAILED` / timeout | The relay couldn't send. An alert was sent. Check `claude -p` works from launchd (`claude bin` in `doctor`). |

Manual relay test against a live chat (costs one small Haiku turn; the chat replies):

```bash
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("lr", "/Users/suvichak/.claude/tools/limit-resume.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
sid = "<transcript uuid>"; path = lr.session_file(sid); cse = lr.rc_session(path)
print(cse, lr.send_continue(sid, {"prompt": "relay test: reply with exactly PONG."}, path, cse))
PY
```

## Remote Control is down

`doctor` says why on the `remote ctl` line:

| it says | do this |
|---|---|
| `login expired, run claude /login` | Run `claude /login`. The watchdog restarts Remote Control within about 10 minutes; no `rc on` needed. (If the Keychain cannot be read it cannot see the new login — then `rc on`.) |
| `waiting for the network` | Nothing. It starts once `api.anthropic.com` answers again. |
| it is up, but the phone lost it after a Claude Code update | Nothing. The watchdog restarts it on the new version once its sessions are idle for 10 min, or after 2 h at the latest; the log says `runs X but Y is installed`. To do it now: quit the Remote Control Terminal window. |
| it is up, but the phone lost it and the debug log has stopped moving | Nothing. After 15 min silent the log says `has logged nothing for`; 10 min later it is restarted and you get `Remote Control froze`. To do it now: quit the Remote Control Terminal window. |
| `3 restarts failed, standing down` | Read the newest `~/.claude/tools/rc-debug/rc-*.log` (`rc-<name>-*.log` for a folder after the first), fix the cause, then `limit-resume.py rc on` (resets every folder). |
| `running (a server started by hand serves it, pid N; not restarted)` | Nothing. A `claude rc` you started in that folder counts as its server; the watchdog does not stop or replace it. Once it ends, the folder's own server is started again. |
| `running? (ps or lsof cannot tell; never started blind)` | Check that `ps` and `lsof` work for this user; doctor says NEEDS ATTENTION until they do. |

## Obsidian is closed

`obsidian on` (macOS) makes each sweep check that the Obsidian app is open and
reopen it in the background (`open -g -a Obsidian`) if it is not, at most once
per 10 min, with an alert each time. `doctor` says on the `obsidian` line:

| it says | do this |
|---|---|
| `CLOSED - reopened by the next sweep` | Nothing. To keep it closed on purpose: `limit-resume.py obsidian off`. |
| `3 reopens failed, standing down` | Open Obsidian by hand and see why it will not start (moved out of /Applications, renamed?), then `limit-resume.py obsidian on`. |
| `running? (pgrep cannot tell; never reopened blind)` | Check that `pgrep -x Obsidian` works for this user. |

`cat ~/.claude/tools/rc-last-exit` shows how the last launcher-started server ended (a `kill -9` of the launcher itself leaves the previous entry). Details:
[[remote-control-autostart]].
