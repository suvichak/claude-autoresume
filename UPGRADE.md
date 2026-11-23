# Upgrading an existing machine

For a machine already running an earlier version of this kit. New installs
should follow [[INSTALL]] instead — it does all of this.

## Current version: 2026-11-23.3

`limit-resume.py doctor` prints the version on its second line. A machine whose
`doctor` has no `version` line is running a build older than this one.

| file | sha256 (first 12) |
|---|---|
| `limit-resume.py` | `1f200f74bbc3` |
| `claude-rc.command.in` | `a0ca0788fe92` |
| `install.sh` | `c40ec630aef1` |
| `uninstall.sh` | `069d57d0bac3` |
| `tests/test_limit_resume.py` | `8f71eda40ff4` |

```bash
shasum -a 256 limit-resume.py ~/.claude/tools/limit-resume.py | cut -c1-12,65-
```

## 2026-11-23.3: weekly check-up by a Claude agent; Windows refused

Re-run `install.sh` once, then paste the prompt it prints at the end into a
Claude Code session on that machine (through Remote Control or the desktop
app). On Linux the install also removes 2026-11-23.2's maintenance timer.
A Mac that already has the desktop-app task: paste the prompt anyway; it
updates the task to the new check-up prompt.

| change | why it matters |
|---|---|
| The weekly task is now an agent check-up (`TASK_PROMPT`): `maintain --scheduled`, `doctor` and the logs, safe repairs (kickstart the scheduler, `rc on` for a tripped breaker on macOS, upgrade from the `src=` checkout once its tests pass), schedule kept in step, a 10-line report, `notify` when something is left | Someone looks at the system every week and fixes what is safe to fix. It never commits, pushes, deletes, edits the kit or job notes, flips auto/jobs/watch, or uses `--force`. |
| `limit-resume.py task-setup`: the paste-in prompt that creates the task, with a Claude Code scheduled task (`create_trigger`, fresh session per run on this machine, `CRON_TZ`) or the desktop app's scheduler; install.sh prints it on every OS and saves it as `weekly-check-setup.txt` | One way to set it up on macOS and Linux; the human only pastes. Replaces `weekly-maintenance-task.json`. |
| `task-spec` adds `timezone` and `cron` (`CRON_TZ=<zone> <cron>`) | A Claude Code scheduled task reads cron in UTC unless told the zone. |
| `install.conf` records `src=<kit folder>` | The check-up knows where to upgrade from. |
| Linux: 2026-11-23.2's `<label>-maintenance.timer` removed by install.sh | One weekly maintainer: the Claude task. `rc on` still refuses on Linux. |
| Windows (Git Bash, MSYS, Cygwin): install.sh refuses with "Windows is not supported"; INSTALL.md has a support table and an "Install with Claude Code" prompt | No half-working install on Windows; another Claude can install the kit from the docs alone. |
| Test 7 no longer finds the real `claude` through `CLAUDE_PATH_HINT`; tests 51b/51c and J11 checks for `task-spec`/`task-setup` | The suite now exits 0 on a machine with `claude` in `~/.local/bin`, which the check-up's upgrade step relies on. 964 checks. |

## 2026-11-23.2: Linux - no Remote Control watch, maintenance by timer

On Linux, re-run `install.sh` once: it writes and enables
`<label>-maintenance.{service,timer}`. On a Mac nothing changes in behaviour;
re-run `install.sh` only to pick up the new version.

| change | why it matters |
|---|---|
| `rc on` refuses on Linux; the watchdog skips Linux even if an older build left `rc_watch` on; doctor prints `remote ctl : not used here` | The watchdog reopens its launcher with macOS `open` (Terminal). On Linux doctor used to say "not watched (`rc on` to enable)", inviting a command that cannot work. |
| Linux install writes a weekly `<label>-maintenance.timer` running `maintain --scheduled` (10 min after the weekly reset known at install; `Persistent=true`); uninstall removes it | Linux has no Claude desktop app, so the weekly scheduled task could never be created and doctor would go STALE after 8 days. The sweep still never runs maintenance itself. Re-run `install.sh` if the weekly reset moves. |
| Linux install no longer prints the desktop-app LAST STEP or writes `weekly-maintenance-task.json`; doctor says `weekly timer` there | No instruction that cannot be followed on that machine. |
| Tests 15b and 51b (12 checks) | `rc on` refused, old `rc_watch` ignored, doctor wording; Linux install/uninstall of both timers, with `uname`/`systemctl`/`loginctl` stubbed. |

## 2026-11-23.1: commit between jobs

Re-run `install.sh` once (tool only; the plist is unchanged, so launchd is not reloaded).

| change | why it matters |
|---|---|
| Job note key `commit: <repo>`: after `JOB DONE` the runner commits the repo (`add -A`, message = title + the job's last ```` ```change ```` block + `Job:`/`Session:` trailers), records `committed: <sha>`, never pushes | Each job's work lands in its own commit before the next job starts; job sessions stay unable to commit. |
| A queued `commit:` job waits (one alert) while its repo has uncommitted or untracked changes | A job never starts on another job's (or a person's) uncommitted work, so commits never mix two jobs. |
| A staged file over 10 MB, or a failed `add`/`commit`, unstages and fails the job | A stray download or a hook failure can't slip into history; dependent jobs stop on the failed note. |
| Tests J31 (21 checks) | Message format, work-tree checks, commit after done, nothing-to-commit, dirty-tree wait and alert, size refusal, notes without `commit:` unaffected. |

## 2026-11-22.1: installer, uninstaller, launcher (audit 2026-11-21, batch 1)

Re-run `install.sh` once: the launcher and `install.conf` are new. On a machine
installed before `install.conf` existed, it keeps the folder the current
launcher serves; otherwise pass `LIMIT_RESUME_RC_DIR` once.

| change | why it matters |
|---|---|
| Remote Control folder saved in `~/.claude/tools/install.conf` (`rc_dir=`, `label=`) | A re-run without `LIMIT_RESUME_RC_DIR` no longer points the launcher at a folder that does not exist. A missing folder stops the install before anything is written. `limit-resume.py` and `uninstall.sh` read the label from it. |
| Reload only when the plist changed; refused while a sweep, resume or job session runs (`--force` overrides) | A reload kills the sweep with its resume inside (that stop is already marked handled, so it is never retried). |
| Downgrade guard, version printed | A sync revert of the kit no longer installs an older tool silently (`--force` to downgrade). |
| Bootstrap retried 3 times, confirmed with `launchctl print`; failure prints one line with the fix and the install still finishes | A failed bootstrap no longer leaves the scheduler unloaded with the later steps skipped. |
| `maintain --install` runs before the bootstrap; the state file is written to a temp file and renamed | The first sweep no longer reads a half-written state file. |
| `doctor`: `held` when the sweep is stale and launchd shows `pended nondemand spawn`; `program … MISSING` when the plist's python is gone | A held agent is no longer reported as healthy. |
| `deadman`, run every 10 min by the Remote Control launcher | One alert per stall even when no sweep runs. Not while a sweep runs (a long resume is not a stall). |
| `uninstall.sh` | Also switches the job queue off, removes the Remote Control Login Item (`--obsidian`: Obsidian too), and with `--purge` the launcher, `install.conf`, `jobs/`, `rc-debug/` and the usage files. Refuses while a sweep or job session runs. Prints the scheduled-task step. Backups (`*.bak*`) are kept and listed. |
| Linux unit: no `Restart=`, no `Persistent=`, quoted paths | Older systemd rejects `Restart=` on a oneshot unit; `Persistent=` did nothing here. |

## 2026-11-22.1: sweep, resume, Remote Control watchdog (audit 2026-11-21, batch 2)

| change | why it matters |
|---|---|
| Headless resumes run detached, one at a time, tracked as the active task; a later sweep logs the reply and exit status (`jobs/<sid>.resume.*`) and stops one past `RESUME_TIMEOUT` with its whole process group | A sweep no longer sits in a resume for up to 2 h: the Remote Control watchdog, the job queue and the heartbeat keep running. Resumes now stagger at least one sweep (5 min) apart. |
| State lock (`limit-resume.state.json.lock`): the sweep holds it throughout; `arm`, `auto`, `rc`, `disarm`, `jobs`, `maintain` wait up to 5 min, then give up unchanged | A `disarm` or `auto off` typed during a sweep is no longer undone by the sweep's save. |
| Unreadable state file kept as `.corrupt-<time>`, alert, carry on from `.bak` (written with every save); `doctor` says NEEDS ATTENTION | A corrupt file no longer silently switches auto mode and the Remote Control watch off. |
| Bad or vanished transcript skipped and logged once; sweep crash alerts once and shows in `doctor` (`last crash`); watchdog crash no longer stops the sweep | One broken transcript no longer stops every resume while `doctor` says healthy. |
| Auto mode's entries tagged `origin: auto`: ignored while auto mode is off, age guard kept. `disarm <sid>` retires the session (handled stops kept); `disarm all` keeps the heartbeat and maintenance record | `auto off` now stops everything auto mode resumed; a disarmed stop is never resumed twice. Entries written before this change count as explicit arms until `disarm all` or maintenance forgets them. |
| Age guard counted from the reset; a skipped stop is logged and alerted once | A stalled sweep or a sleeping Mac no longer drops stops silently. Weekly-limit stops are now auto-resumed after the weekly reset. |
| `MAX_RESUMES` counts resumes in a row without a typed turn; reaching it alerts; a retired session (`DONE` or max resumes) is watched again once you work in it; `DONE` must be the whole last line | A chat that hits the limit daily is no longer retired for good after 8 days; "NOT DONE" no longer retires. |
| No headless resume while another `claude` has the session open (`--resume`, `-r`, `--session-id` in `ps`): alert instead | No second writer behind an open terminal window. A `claude` in the chat's folder counts too since the decisions step. |
| Relay runs from `~/.claude/tools` with Bash, Edit, Write, NotebookEdit, WebFetch, WebSearch denied, in its own process group | Its transcript is never taken for a chat; a timeout kills its tools too. |
| Stale / hung checks match this folder's server by `--name auto-<folder>`; heartbeat also in epoch seconds; Remote Control timers reset after a sweep gap; throttle ignores a launch time ahead of the clock; legacy `job` entries moved to the job list; job transcripts never auto-resumed; transcripts read from the end; maintenance also rotates `limit-resume.{out,err}.log` | Smaller audit items B2-L1 … B2-L9, B2-I1. |

## 2026-11-22.1: background jobs, usage, maintenance (audit 2026-11-21, batch 3)

Nothing to re-run: the sweep picks it up. The next job launch rewrites
`job-settings.json` with the new guard.

| change | why it matters |
|---|---|
| Usage counts as known only with both windows, each read in the last 10 min (`USAGE_MAX_AGE`) from a window that has not reset; unknown counts as over: no start, a running job is paused; one alert on the first failed probe | A failed or partial probe no longer lets a job start or run on an old 3% reading. A probe that reports only the weekly window now keeps the gate shut. |
| `usage.json` merged under an flock: within a window the higher figure is kept, a figure from an earlier window is ignored; temp file per writer | An idle session's status line can no longer lower a running job's figure; two writers can't corrupt the file. |
| A lost running job is adopted (its `jobpty` helper found by session id), a second live session of a note stopped; the start is saved before and after the launch; no `--resume` while another process has the session | A lost state never makes a second Opus session on one transcript. |
| A blocked job you answer becomes the running job again (caps apply); a job running meanwhile is paused | Two jobs no longer run at once after an answer on the phone. Typing in a *finished* job's session: see the decisions step. |
| Guard: a `PreToolUse` hook on every tool that parses commands (read-only git allowlist, aliases, launchers, `sh -c`, `eval`, scripts, interpreter code), refuses writes to the kit, Claude settings and `.git`, blocks `AskUserQuestion`, and fails closed (`… guard \|\| exit 2`) | The audited bypasses (aliases, merge/pull/revert, `$G commit`, `xargs git`, python code, scripts) are blocked; a broken guard blocks rather than allows. Read-only git is unchanged. |
| Quota stop: `paused_until` in the note, not resumed before it; a resume that did no work is not counted | No longer burns 8 resumes in 40 min on a limit the readings do not show. |
| `jobs stop`; a status you set in a running job's note stops it and is kept | You can stop a job; `hold` set during a run is no longer overwritten. |
| Stop kills claude's process group and every process below it, also when the helper died | A stopped job leaves no build or shell running. |
| A fresh start resets `resumes` and `previous_session` and sends the body without old `## Run` sections; a paused job whose transcript is gone restarts in a new session | The documented retry works; no 8-launch loop on a vanished transcript. |
| `runner:` in the note; another machine's paused/running/blocked notes are left alone; done → queued alerted | Two machines on one synced folder no longer pause and resume each other's jobs. |
| A running job is paused when the weekly reset passes | It no longer runs on into the new week's quota. |
| Markers: only the last non-blank line counts, any case, bullets/quotes/emoji allowed | `- JOB DONE` or `JOB DONE ✅` finishes the job; an earlier "JOB FAILED" line no longer overrides a final JOB DONE. |
| Frontmatter: BOM, CRLF, `# comments`, non-numeric `order`, a `---`-prefixed line; unknown status logged once | Such notes no longer silently never run. |
| doctor: job runner crash (also alerted once), probe failing, stale usage while jobs wait, dead or overlong running job, open sessions; maintenance STALE follows the scheduled task (`scheduled task: never ran`) | doctor no longer says healthy while the job runner cannot read usage, and install runs no longer hide a scheduled task that never fires. |
| `job-settings.json` written atomically and read back before a launch; vanished transcripts no longer crash the job tick or maintenance; `Current session` read as the 5-hour window; a weekly variant's reset no longer moves the binding one | Smaller audit items B3-L5 … B3-L8. |

## 2026-11-22.1: What changed (decisions, 2026-11-22)

Re-run `install.sh` once: the launcher template and `install.conf` change. From
a terminal it now asks which folders Remote Control should serve (Enter keeps
the saved list); without a terminal it keeps the saved list. A one-folder
`install.conf` (a single `rc_dir=`) is already a list of one, so nothing is
migrated by hand. The first folder keeps `claude-rc.command`, `rc-last-exit`,
`rc-launch.lock` and its Login Item, so a running server (named
`auto-<folder>`) keeps being watched without a restart. Job sessions pick up
the git wrapper at their next launch.

| change | why it matters |
|---|---|
| Several Remote Control folders: `LIMIT_RESUME_RC_DIRS` (`:` or newline separated), the prompt, or the saved list; `LIMIT_RESUME_RC_DIR` still works for one. One `rc_dir=` line per folder. Each folder gets its own server `auto-<name>`, launcher (`claude-rc-<name>.command` after the first), Login Item, exit status, lock, debug-log name and watchdog state (tries, breaker, throttle, stale and hung markers; `rc on` resets all). Two folders with one name are refused; a folder dropped from the list loses its launcher and Login Item | One machine can keep several folders reachable from the phone, and one folder's failures no longer hold another back. |
| A folder's server is the one named for it, or one started by hand there (its working directory is the folder) - in the launcher, the watchdog, reaping, the stale and hung checks and `doctor` (one `remote ctl` line per folder) | Another folder's server no longer makes this folder look up, and a hand-started server in the folder no longer makes the launcher loop on 409s or trip the breaker. It is never stopped by the watchdog. |
| Dead-man check once per machine: the launcher holding `rc-deadman.lock` runs it, another takes over when it ends | Two launchers no longer double the checks, which would make a sweep that is merely late after a wake look stalled. |
| Before a headless resume, a live `claude` working in the chat's folder (one `lsof` call over every `claude` process; Remote Control servers and chats, job sessions and the tool's own processes excluded) counts as the chat's owner: "type continue" alert, no resume. lsof missing or failing counts the same (fail safe) | A chat started fresh in a terminal or the desktop app (no `--resume` on its command line) is no longer resumed headless behind its open window. |
| Typing in a finished (done/failed) job session while it is kept open makes it the running task: the next job and due resumes wait until it has been quiet for `RESUME_BUSY` (15 min); no caps, no `JOB_MAX_H`; the note keeps its status | One task at a time also covers a finished job you carry on in. |
| Job git wrapper `~/.claude/tools/jobbin/git`, first on a job session's PATH, rewritten with each job launch: with `LIMIT_RESUME_JOB=1` it asks `limit-resume.py git-check` (the guard's rules) before the real git, and refuses when the tool cannot run | Git run by name from anywhere in a job - a script, make, npm, a command built at run time - is checked too, not only what the hook can read. |

## What changed (2026-11-21)

**One task at a time, chat resumes included.** Order: a chat whose limit has
reset, then job notes with `priority: top`, then the rest. A due resume waits
for a running job; no job starts while a resume is due or a resumed chat is
still working (`RESUME_BUSY`, 15 min quiet).

**Context-size limits.** A chat over 700k tokens (`CHAT_NEW_AT`) continues in a
new Remote Control session; a paused job over 400k (`JOB_NEW_AT`) restarts in a
new session, tracked in its note (`previous_session`, `## Run … (new session)`).

**Usage parsing** reads `rate_limit_info.unifiedWindows` (field moved 2026-11).
**5-hour 60%** is described as a pause trigger (landing 70–80% intended).

**LaunchAgent plist trimmed** to `StartInterval` + `RunAtLoad` +
`AbandonProcessGroup`. This did not cure the 2026-11-20/21 stall (launchd kept
the agent pending; it cleared by itself, cause unverified) — see runbook.

Upgrade: copy `limit-resume.py` over `~/.claude/tools/limit-resume.py` (no
reload needed). Re-run `install.sh` only to get the new plist, and only when no
resume or job is running — the reload kills them.

## What changed (2026-11-18)

**Background jobs.** The sweep can now run a folder of job notes, one at a
time, as **Remote Control sessions**, on weekly quota that would otherwise
expire unused at the weekly reset. It starts jobs only in the run-up to the
account's own weekly reset, with the 5-hour window under 60% and weekly usage
under today's cap. A job that reaches a cap is paused and resumed later. Full
rules: [[README]] → Background jobs.

| change | why it matters |
|---|---|
| `jobs on <folder> [--cwd dir] [--reset "Thu 14:00"]` / `off` / `status` / `probe` | Turns the queue on and shows the gate, usage and every job. Off by default: an upgrade starts no job until you run `jobs on`. |
| Caps and gate follow **this account's** weekly reset | Accounts reset on different days. The reset comes from the usage figures; `--reset` is the per-machine fallback. The 90% day is the day before the reset, not a fixed Sunday. |
| The weekly reset is followed, not assumed | A usage reset from Claude moves the reset to another day. The newest reading wins, a move to a new slot alerts "Weekly reset moved", `--reset` is dropped once a real reading exists, and a reading older than 6 h is never trusted to keep the gate shut. |
| Weekly maintenance as a **Claude scheduled task** (`auto-resume-weekly-maintenance`, spec from `task-spec`) | Runs 10 min after the account's weekly reset. It reads the reset from the app's own usage (`get_usage`: no probe, no CLI login), records it, prunes old state and job logs, rotates the log, alerts a weekly summary, and reschedules itself when the reset moves. `install.sh` does the first run and writes the task spec; creating the task is the one step done in the Claude app. |
| Only the weekly figure is required to start a job | A probe reports only the binding window (Mac VM, 2026-11-18: `seven_day` only). Requiring the 5-hour figure as well would have kept every job from starting. |
| Jobs are interactive `claude --remote-control` sessions under a pty helper (`jobpty`) | Visible and answerable on the phone. `JOB DONE` / `JOB FAILED:` in the last message ends a job; a question left idle 20 min makes it `blocked` and alerts you. |
| `AbandonProcessGroup` (launchd) / `KillMode=process` (systemd) | Without it the scheduler kills the job when the sweep that started it exits. **Re-run `install.sh`** so the scheduler file gets the key. |
| Job `--settings`: `guard` hook, status line, `skipDangerousModePermissionPrompt` | Job sessions can't `git commit` / `git push`; they feed the usage cache; no dialog waits on the terminal. Interactive sessions are untouched. |
| Untrusted job folder → failed before launch | The trust dialog would wait on a terminal nobody sees. |
| Job and probe sessions excluded from auto-resume | Otherwise a job that hit a limit would be resumed twice. |
| `disarm all` keeps the job queue | It has its own on/off. |

**Upgrading a machine that runs the 2026-11-09 build** (the first job runner,
`jobs` via `claude -p`, state in `limit-resume.jobs.json`; its source was never
in the kit, only its bytecode in `__pycache__/limit-resume.cpython-315.pyc`):
this version does not read `limit-resume.jobs.json`. A note left `running`
is taken as interrupted, set `paused` and resumed, so a `claude -p` job still
running from the old build would get a second copy. First:

```bash
python3 ~/.claude/tools/limit-resume.py jobs off      # old build: start nothing new
python3 ~/.claude/tools/limit-resume.py jobs status   # wait until nothing is running
```

then upgrade, then `jobs on "<folder>" --cwd <dir>` again (the folder setting
lives in the new state file).

The field names come from that build, which ran against the real service:
`rate_limit_event.rate_limit_info.utilization` (a 0–1 fraction) and the status
line's `rate_limits.*.used_percentage` / `resets_at`.

**Not verified live.** Built on a Mac whose `claude` was logged out, so none of
this has run against the real service. Check on the first machine:

1. `jobs probe`, then `jobs status`: the usage lines and `weekly reset` must
   show figures. The field names match the 2026-11-09 build, which read them
   live; if they still say `unknown`, look at one probe's output. Until then
   the gate stays shut, so nothing runs blind.
2. Queue a small job and watch it appear as `job: <title>` in Remote Control.
   If it doesn't start within 5 min, read `~/.claude/tools/jobs/<id>.pty.log`:
   a dialog the settings didn't suppress shows up there.

## What changed (2026-10-28)

A Remote Control server that **froze while still running** is now restarted.
On 2026-10-27 the server stopped at 01:04 and stayed silent for 23 h: no
polls, no heartbeats, no token refreshes. Its sessions ran out of tokens and
exited, the server dropped the machine, and messages from the phone were never
delivered. The watchdog only asked "is a process running?", so it did nothing.
At 00:00 the process ran again, got a `401`, and exited; the watchdog then
restarted it. The same happened on 2026-10-21 and 2026-10-22, and all three
ended at exactly 00:00 local. Cause unknown; see
[[2026-10-27 Remote Control freeze]].

Only `limit-resume.py` and the tests changed. Copy the script over; no reinstall.

| change | why it matters |
|---|---|
| **Debug log is the heartbeat** | A live server writes its `--debug-file` at least every ~9.5 min with no sessions (work polls), every ~3 min with any open (heartbeats). Measured over 8 runs, 2026-10-14 → 28. Silent for `RC_HUNG` (15 min) → the watchdog logs `has logged nothing for N min` and records the sighting. |
| **Second look before killing** | Still silent `RC_HUNG_CONFIRM` (10 min) later, same pid, same log mtime → hung. A VM just woken from suspend logs again within ~9.5 min, so it is never taken for hung. A different pid starts a new sighting. |
| **Never during an outage** | API unreachable → waits; a quiet server may only be waiting for the network. |
| **Stop and relaunch** | One alert ("Remote Control froze: running, but silent since …"), then the same stop as a stale server (SIGTERM, SIGKILL after 20 s) via the shared `rc_stop()`. Orphaned sessions are reaped and the launcher started in the same sweep; it counts as a launch try. |
| **No debug log → left alone** | Same rule as the version check: never kill blind. |

Worst case a freeze now costs ~25–30 min instead of until it happens to die.
Tests 33–33g cover it (276 checks). Live on the Mac VM since 2026-10-28 01:20;
not yet seen to fire on a real freeze.

## What changed (2026-10-26)

Remote Control chats are now continued **inside the chat**, so the phone shows
the turn. Before, the sweep ran `claude --resume -p` on the local transcript.
The Remote Control child process streams to claude.ai and keeps its context in
memory, so that turn never appeared on the phone, and the live chat didn't know
about it. On 2026-10-26 a headless turn submitted a pending form while the
phone still asked whether to submit it, so a "go" would have submitted it twice.

How it works:
1. **Ownership.** The transcript's first user-message `uuid` also appears in
   `~/.claude/tools/rc-debug/bridge-transcript-cse_<id>.jsonl` (`rc_session`).
2. **Inbox.** The live child for that session is the process with
   `--sdk-url … --session-id cse_<id>`. Its cross-session inbox is
   `/tmp/cc-socks/<pid>.sock` (`rc_child_pid`).
3. **Relay.** A throwaway `claude -p --model claude-haiku-5-5 --allowedTools
   SendMessage` runs in the chat's own permission mode (a receiver holds
   messages from a different mode for approval). It sends the resume prompt
   with `to: "uds:/tmp/cc-socks/<pid>.sock"` and prints `SENT` or `FAILED`
   (`send_continue`).
4. The chat receives it as a `<cross-session-message>` and runs the turn itself,
   even when idle.
5. If the child or its socket is missing, or the relay doesn't print `SENT`:
   one alert, "open it and type continue". Never a headless resume.

The block is recorded before the send and counts toward `MAX_RESUMES`, the same
as a resume. Terminal sessions are still resumed headless.

Tested 2026-10-26 against a live chat. A message from another session arrived
mid-turn (PONG2) and while idle (PONG3). The real `send_continue` code path
logged `relay exit=0 out=SENT` 12 s after starting, and the idle chat answered
PONG4. Dead ends: a `SendMessage` from the chat to itself never arrives, a local
headless `claude` has no claude-code-remote tools, and `create_session` is
refused without a trusted device.

The fallback alert reaches the phone only with Pushover keys in the Keychain
(`limit-resume.py doctor` → `alerts`).

## What changed (2026-10-19)

Remote Control now comes back after a Claude Code update. Before, an update left
the running server on its old binary (a server never updates itself, and its
sessions run with `DISABLE_AUTOUPDATER=1`). Remote Control enforces a
server-side minimum version (`tengu_bridge_min_version`). So the old server
could stop serving while its process stayed up, and the watchdog, which only
asked "is a process running?", left it alone. Only `limit-resume.py` and the
tests changed; copy the script over, no reinstall needed.

| change | why it matters |
|---|---|
| **Stale server is replaced** | When the server's binary (from `lsof -d txt`) differs from the version `~/.local/bin/claude` points at, the watchdog stops it (SIGTERM, SIGKILL after 20 s) and starts the launcher in the same sweep. It counts as a normal launch try. |
| **Never mid-turn** | It restarts only when no session process has a child (a tool's shell, or the CLI's `caffeinate`) and the server's debug log has had no `bridge:ws`/`bridge:activity` line for `RC_IDLE` (10 min). Heartbeats don't count. If the log is missing or unreadable, it treats the server as busy. Waiting is logged once. |
| **At most 2 h stale** | If the sessions stay busy for `RC_STALE_MAX` (2 h) after the mismatch is first seen, it restarts anyway and logs `still busy after 120 min`. That kills any turn still running. |
| **Unknown → untouched** | Not a native install (no `versions/` path), or `lsof` gives nothing: no restart, same as before. |

## What changed (2026-10-18)

Resumes now run in the directory the session was started in. launchd starts
the sweep in `/`, and `claude --resume` from `/` still finds the transcript but
loads none of the project's CLAUDE.md, memory or `.claude/settings.json`. On
2026-10-18 a session cut off mid-turn was resumed that way. It came back
without its memory, answered `DONE` in 8 s, and the user had to type "continue"
by hand. Only `limit-resume.py` and the tests changed; copy the script over, no
reinstall needed.

| change | why it matters |
|---|---|
| **Resume in the session's start dir** | `session_cwd()` takes the first transcript `cwd` that is not `/` and still exists, and falls back to `$HOME`. Later `cwd`s follow `cd`, so they are ignored. The log line now reads `permission mode X, cwd Y`. |

## What changed (2026-10-14)

Resumes now run in the session's own permission mode instead of a fixed
`acceptEdits`. On 2026-10-14 a session that ran in `auto` was resumed in
`acceptEdits`: its `ssh` and `python3 -` calls were refused, and it wrapped up
early. Only `limit-resume.py` and the tests changed; copy the script over, no
reinstall needed.

| change | why it matters |
|---|---|
| **Same permissions as the session** | The mode comes from the last user turn in the transcript, skipping the resume's own prompt so one fallback cannot stick. Unknown or missing mode → `PERM`; `default` → `manual`. |
| **`LIMIT_RESUME_PERMISSION_MODE` now forces** | Set, it overrides every session's mode. Unset, `PERM` is only the fallback. |
| **Log line** | Each resume logs `permission mode <mode>`. |

## What changed (2026-10-08)

Two real outages drove this. On 2026-09-27 a network outage used up the three
restart tries in 20 minutes and Remote Control stayed down until a reboot. On
2026-10-07 the Login Item started Remote Control seconds after login on a slow
network, a token refresh was lost, the login died, and it stayed down ~23 hours
without telling anyone. Background: [[remote-control-autostart]].

| change | why it matters |
|---|---|
| **Launcher waits for the network** | It starts `claude` only after `api.anthropic.com` answers within 3 s, three times in a row; gives up after ~5 min without touching the login. This is what protects the login at boot. |
| **One launcher at a time** | A kernel lock on `rc-launch.lock`. The Login Item and the watchdog can no longer start two servers that refresh the token concurrently. |
| **Launcher reports how it ended** | It no longer `exec`s `claude`; when the server stops it writes `rc-last-exit` (`auth`, `network` or `exit`), including after Ctrl-C or a closed window. |
| **Outages cost no tries** | API unreachable → the watchdog waits, logs once, and relaunches when it is back. A launch the launcher abandoned at its own network gate is refunded. |
| **Dead login → one alert, no blind retries** | "Run `claude /login`." After that it restarts by itself, with a clean breaker. |
| **Breaker trip → one alert** | Three failed starts with the network up still stand down until `rc on`, but no longer silently. `rc on` now also lifts the 10-minute throttle. |
| **Alerts** | Pushover if its keys are in the login Keychain, else a notification on the Mac's screen. A Pushover post that fails is re-sent each sweep until it lands. New `notify` command to test. |
| **`doctor`** | Shows the version, why Remote Control is down, and where alerts go. |
| **Launcher is a template** | `claude-rc.command.in`, filled in by `install.sh` (paths with spaces, `&` or `|` now survive). Edit the template, not the installed copy. |

Reviewed by an independent audit on 2026-10-08; the defects it found in the
changes above are fixed in this version. What it found in older code and is
**not** changed yet is listed under "Known gaps" below.

## What changed (2026-09-21)

| change | why it matters |
|---|---|
| **Remote Control watchdog** | `claude rc` dies and never comes back on its own. The 5-minute sweep now restarts it. No new scheduler — it rides the one already installed. |
| **Terminal launcher + Login Item** | `~/.claude/tools/claude-rc.command` starts Remote Control in a visible Terminal window, and runs at login so a reboot brings it back. |
| **Hardened resume prompt** | The old prompt let a resumed session ask clarifying questions nobody was there to answer, wasting the turn. It now must either work or reply `DONE`. |
| **Crash-safe resumes** | The resume attempt is recorded *before* `claude` launches. Previously a timeout or power loss mid-resume left the block unmarked, and the next sweep resumed it again — an infinite loop. |
| **`doctor` is cross-platform** | Understands both launchd and `systemd --user`, and now reports Remote Control state. |
| **`rc on|off` command** | Opt in or out of the Remote Control watchdog. |
| **Obsidian opens at login** | The vault — and LiveSync — is up after a reboot without anyone opening it. Added as a Login Item next to Remote Control. |

## Doing the upgrade

```bash
cd <this folder>
python3 tests/test_limit_resume.py     # 926 checks, ~90s, no network, no spend
./install.sh                           # idempotent: reloads only what changed
python3 ~/.claude/tools/limit-resume.py rc on
python3 ~/.claude/tools/limit-resume.py doctor
```

`install.sh` is safe to re-run. It overwrites the tool, regenerates the
launcher and the scheduler file with this machine's paths, reloads the
scheduler only when its file changed, and adds each Login Item — Remote Control
and Obsidian — only if it is not already there. Running it twice gives you one
of each, not two. A reload kills a sweep in flight (and the resume inside it)
and maybe job sessions, so it refuses while one runs; `--force` overrides. Your
`limit-resume.state.json` — armed sessions, handled blocks — is left alone.

**It does not restart a running Remote Control server**, so sessions in
progress are not interrupted. The server that is already up keeps running under
the launcher that started it; the new launcher takes over the next time it
starts (reboot, or the watchdog after the server dies). To switch at once, when
nothing is running through it, close its Terminal window — the watchdog
reopens it within ten minutes.

Then set up alerts. Each command prompts for the value; nothing goes in a file:

```bash
security add-generic-password -U -s limit-resume-pushover -a token -w   # Pushover app token
security add-generic-password -U -s limit-resume-pushover -a user  -w   # Pushover user key
python3 ~/.claude/tools/limit-resume.py notify                          # expect: sent via pushover
```

Without the keys everything still works; alerts only appear on the Mac's own
screen. Linux has no Keychain, so alerts there go to the log only.

Expected `doctor` output afterwards:

```
script     : /Users/<you>/.claude/tools/limit-resume.py
version    : 2026-11-21
claude bin : /Users/<you>/.local/bin/claude
launchd : loaded, runs=1 last exit=0
last sweep : <a moment ago> OK
remote ctl : watched, running
alerts     : Pushover
auto mode  : True / armed: N
maintenance: <today> (install, 0d ago)
jobs       : off (`jobs on <folder>` to enable)

healthy
```

If `remote ctl` says `not watched`, you skipped `rc on`. If it says
`MISSING - will restart`, Remote Control is down and the next sweep will
handle it — wait five minutes and re-check. The `DOWN - …` variants are
explained in [[runbook]].

Check the Login Items afterwards:

```bash
osascript -e 'tell application "System Events" to get the name of every login item'
# Obsidian, Claude, claude-rc.command
```

### Verified, and not verified

Verified on the live machine: the script and launcher install cleanly, the
sweep and `doctor` run, the launcher stands down next to the running server,
and a test alert reaches the screen. The start path itself — network gate,
lock, login check, status file, signals — is verified only in the test
sandbox, with a fake `claude`; the live server was not restarted to prove it.
Never observed for real: how an expired login looks to `claude auth status`.

## Known gaps

Found by the 2026-10-08 audit in code that predates this version; not changed.

| gap | effect |
|---|---|
| `disarm all` also turns the Remote Control watch off | Run `rc on` again after it. `uninstall.sh` relies on this. |
| Entries auto mode wrote before 2026-11-22 carry no `origin` | They count as explicit arms (no age guard, watched with auto mode off) until `disarm all` or maintenance forgets them after 30 days idle. |
| Lock screen after an unplanned reboot | Nothing runs until someone unlocks; needs a probe from another machine. |
| A server that is alive but disconnected counts as up | Since 2026-10-28 a *frozen* server (debug log silent) is restarted. One that keeps logging but no longer serves is still counted as up. |

## Per-machine: Obsidian at login

The installer looks for `/Applications/Obsidian.app` or
`~/Applications/Obsidian.app` and adds whichever it finds. On a machine without
Obsidian it skips this silently. To install without it where Obsidian *is*
present:

```bash
LIMIT_RESUME_NO_OBSIDIAN=1 ./install.sh
```

## Per-machine: which folders does Remote Control serve?

Each launcher serves one folder, and only one Remote Control server may serve a
folder at a time. The installer takes `LIMIT_RESUME_RC_DIRS` (several, `:` or
newline separated), else `LIMIT_RESUME_RC_DIR` (one); without either it asks
when run from a terminal, showing the folders saved in
`~/.claude/tools/install.conf` by the last install (Enter keeps them), and
otherwise takes those, else `~/_obsidian`. It stops before changing anything if
a folder does not exist, or if two folders have the same name:

```bash
LIMIT_RESUME_RC_DIRS="$HOME/_obsidian:$HOME/projects/thing" ./install.sh   # saved for later runs
```

The first folder's launcher is `claude-rc.command`; each other folder's is
`claude-rc-<name>.command`, with its own Login Item.

Each folder must already be trusted — run `claude` there once interactively and
accept the workspace dialog, or Remote Control exits with `Workspace not
trusted`. The `--name` shown in claude.ai/code is derived from the folder name
(`auto-<folder>`).

On Linux there is no Login Item and no `open`; the sweep watchdog will still
try, but a headless server has no Terminal to open into. Leave `rc off` there
unless you have changed the launch method.

## After upgrading, `claude rc` in a terminal will fail

Once the launcher is serving the folder, starting a second one by hand is
refused:

```
409: This folder is already served by a terminal `claude remote-control` on this device.
```

That is the intended end state. To go back to a hand-run server, stop the
running one first. Since the decisions step a server started by hand in a
served folder counts as that folder's server: the watchdog and the launcher
leave it alone and start nothing next to it (no 409 loop), and once it ends
the watchdog starts the folder's own server again unless you `rc off`.

## Rolling back

```bash
python3 ~/.claude/tools/limit-resume.py rc off     # stop restarting Remote Control
./uninstall.sh                                     # scheduler, RC Login Item, job queue
./uninstall.sh --obsidian                          # the Obsidian Login Item as well
```

`uninstall.sh` leaves a running Remote Control server alone (close its
Terminal window) and cannot delete the Claude scheduled task
`auto-resume-weekly-maintenance`; delete that in the Claude desktop app.
