# Install on a new machine

Copy this whole folder to the target machine, then follow the section for its
OS. Design notes: [[README]]. Daily use: [[runbook]].

Requirements: `python3` (3.7+, stdlib only), the `claude` CLI on PATH, and a
logged-in user account — **not** root. Resumes reuse that user's Claude
credentials.

## The short version

```bash
python3 tests/test_limit_resume.py    # 926 checks, ~90s, no network, no spend
./install.sh                          # asks for the Remote Control folder(s), installs, starts
python3 ~/.claude/tools/limit-resume.py auto on
python3 ~/.claude/tools/limit-resume.py doctor
```

`install.sh` writes the scheduler file with this machine's real home directory
and `python3` path, so nothing needs hand-editing. Remote Control serves one
or more folders, one server each, named `auto-<folder name>`. Run from a
terminal, the installer asks for them - one per line, or several separated by
`:`; an empty first line keeps the folders the last install saved in
`~/.claude/tools/install.conf` (one `rc_dir=` line per folder, `label=`).
Without a terminal it takes `LIMIT_RESUME_RC_DIRS` (`:` or newline separated),
else `LIMIT_RESUME_RC_DIR` (one folder), else the saved list, else
`~/_obsidian`; either env var also skips the question. It stops before
changing anything when a folder does not exist, or when two folders have the
same name (both servers would be `auto-<name>`). It also stops,
without `--force`, when the plist changed and reloading it would kill a sweep
(with the resume inside it) or a job session, and when the kit's
`limit-resume.py` is older than the installed one. It ends with the first weekly
maintenance run (`maintain --install`), which records this account's weekly
reset (one Haiku turn; needs `claude` logged in), and writes
`~/.claude/tools/weekly-maintenance-task.json`.

**Last step, in the Claude desktop app on that machine.** Ask Claude to
create the scheduled task described in `~/.claude/tools/weekly-maintenance-task.json`
with `mcp__scheduled-tasks__create_scheduled_task`, using its taskId, title,
description, cronExpression and prompt exactly, and then to run it once.
That task does the weekly maintenance from then on, 10 min after the
account's weekly reset, and moves itself when the reset moves. Machines without
the desktop app have no scheduled task; run `limit-resume.py maintain` there
weekly by hand, and `doctor` will flag it as `STALE` after 8 days. On macOS it
also adds a Login Item per Remote Control folder (`claude-rc.command` for the
first, `claude-rc-<name>.command` for each other; a folder dropped from the
list loses its launcher and Login Item) and one for Obsidian (if installed;
`LIMIT_RESUME_NO_OBSIDIAN=1` skips Obsidian). `./install.sh --no-start`
writes the files without loading the scheduler now; on macOS launchd still
loads the plist at the next login. `./uninstall.sh [--purge] [--obsidian]`
removes the scheduler and every Remote Control Login Item and switches the job
queue off; `--purge` also deletes the tool, the launchers, the job git wrapper,
state and logs. It
refuses while a sweep or job session runs (`--force` leaves it running), and it
cannot delete the Claude scheduled task: it prints that step.

Run the tests *before* installing on a new box — they catch a missing `claude`,
a broken `python3` and a wrong path in about half a minute.

The rest of this page is what the installer does, for when you would rather do
it by hand or need to audit it.

## 1. Both platforms

```bash
mkdir -p ~/.claude/tools
cp limit-resume.py ~/.claude/tools/
chmod +x ~/.claude/tools/limit-resume.py
```

## 2a. macOS — LaunchAgent

Save as `~/Library/LaunchAgents/com.suvichak.claude-limit-resume.plist`,
replacing `/Users/suvichak` with the real home directory and
`/opt/homebrew/bin/python3` with the output of `command -v python3`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.suvichak.claude-limit-resume</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/suvichak/.claude/tools/limit-resume.py</string>
    <string>sweep</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>AbandonProcessGroup</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/suvichak/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>/Users/suvichak/.claude/tools/limit-resume.out.log</string>
  <key>StandardErrorPath</key><string>/Users/suvichak/.claude/tools/limit-resume.err.log</string>
</dict>
</plist>
```

```bash
plutil -lint ~/Library/LaunchAgents/com.suvichak.claude-limit-resume.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.suvichak.claude-limit-resume.plist
```

What each key buys: `RunAtLoad` sweeps immediately and again at every login, so
it returns after a reboot. `StartInterval` reruns it every 5 min, which also
retries a crashed sweep, so the 2026-11-18 `KeepAlive`, `ThrottleInterval`,
`ProcessType` and `LowPriorityIO` keys were dropped as redundant. Dropping them
did not cure the 2026-11-20/21 stall (launchd kept the agent pending); cause
unverified, see runbook → Troubleshooting.
`AbandonProcessGroup` lets a background job outlive the sweep that started it
(systemd: `KillMode=process`).

Because launchd can hold the agent without a word, the Remote Control launcher
(started by Terminal, not launchd) runs `limit-resume.py deadman` every 10 min
while its server runs - once per machine however many folders are served (the
launcher holding `~/.claude/tools/rc-deadman.lock` asks; another takes over
when it ends). It alerts once per stall when the last sweep is over 15 min old
on two checks in a row and no sweep is running.

The python path is fixed at install time: if that python goes away, `doctor`
says `program … MISSING`; re-run `install.sh`.

## 2b. Linux — systemd user timer

`~/.config/systemd/user/com.suvichak.claude-limit-resume.service` (the unit is
named after `LIMIT_RESUME_LABEL`, like the plist; `doctor` looks for that name):

```ini
[Unit]
Description=Resume Claude Code sessions stopped by the usage limit

[Service]
Type=oneshot
Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/python3 %h/.claude/tools/limit-resume.py sweep
KillMode=process
```

No `Restart=`: the timer reruns a crashed sweep within 5 min, and systemd
before about version 244 refuses `Restart=` on a `oneshot` unit.

`~/.config/systemd/user/com.suvichak.claude-limit-resume.timer`:

```ini
[Unit]
Description=Sweep for rate-limited Claude sessions every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now com.suvichak.claude-limit-resume.timer
sudo loginctl enable-linger $USER     # required on headless servers
```

`enable-linger` is the step people forget: without it, user units stop the
moment you log out, so the watchdog dies with your SSH session and never runs at
boot. Sweeps missed while the machine was off are not caught up; the first one
runs 2 min after boot (`Persistent=` only applies to `OnCalendar=` timers).

## 3. Turn it on

```bash
python3 ~/.claude/tools/limit-resume.py auto on
python3 ~/.claude/tools/limit-resume.py doctor
```

`doctor` exits 0 only when the script and `claude` are found, the scheduler is
loaded (and its python exists), a sweep has run in the last 15 minutes, Remote
Control is up when it is watched (`rc on`) and not suspected hung, maintenance
is not stale (the Claude scheduled task has run in the last 8 days, or the
first maintenance is younger than that), the job folder exists when jobs are
on, the job runner has not crashed, the usage probe is not failing, a running
job's session is alive and within `JOB_MAX_H`, usage is fresh while jobs wait,
the state file reads and no `.corrupt-*` copy of it is left, and the last
sweep did not crash. A stale sweep while launchd shows `pended
nondemand spawn` is reported as `held`, with the kickstart command. It knows
both platforms and reports whichever applies.

## Background jobs (optional)

Off until you turn it on. Each job runs as a Remote Control session, so the
machine needs a `claude /login` account (not a setup-token), and every job
folder (`cwd`) must be trusted: run `claude` there once and accept.

```bash
python3 ~/.claude/tools/limit-resume.py jobs on "<job folder>" --cwd <default dir>
python3 ~/.claude/tools/limit-resume.py jobs probe    # one Haiku turn: usage + weekly reset
python3 ~/.claude/tools/limit-resume.py jobs status   # gate, caps, queue
```

How the queue works: [[README]] → Background jobs. `job-settings.json`
appears only at the first job launch. Before the first real job, run a smoke
job in a throwaway git repo (trusted folder, a note that edits a file, runs
`git status` and tries `git commit`): it is the first live test of the pty
runner, the guard on every tool and the status line feed.

## Per-machine settings

Nothing in the tool is host-specific, but two things are worth deciding per box:

| setting | where | note |
|---|---|---|
| `PERM` | top of `limit-resume.py`, or `LIMIT_RESUME_PERMISSION_MODE` | Resumes reuse the session's own last permission mode; `PERM` (`acceptEdits`) is the fallback when the transcript has none. Setting `LIMIT_RESUME_PERMISSION_MODE` (e.g. `manual`) forces that mode for every resume. |
| `LIMIT_RESUME_LABEL` | env var for `install.sh` | Rename the unit if you dislike the `com.suvichak.` prefix. Saved in `install.conf`, which `limit-resume.py` and `uninstall.sh` read, so later commands need no env var. |
| `LIMIT_RESUME_RC_DIRS` / `LIMIT_RESUME_RC_DIR` | env var for `install.sh`, or its prompt | The folders Remote Control serves (several, `:` separated / one). Each must exist and be trusted; no two may share a name. Saved in `install.conf` for later runs. |

On a shared account, note that auto mode covers **every** project directory that
user has — not just one repo.

## Uninstall

`./uninstall.sh` does all of this. By hand:

```bash
python3 ~/.claude/tools/limit-resume.py jobs status   # wait until nothing runs or is open
python3 ~/.claude/tools/limit-resume.py disarm all
python3 ~/.claude/tools/limit-resume.py jobs off      # disarm all keeps the queue on
# macOS
launchctl bootout gui/$UID/com.suvichak.claude-limit-resume
rm ~/Library/LaunchAgents/com.suvichak.claude-limit-resume.plist
osascript -e 'tell application "System Events" to delete login item "claude-rc.command"'
# and for each other folder: delete login item "claude-rc-<name>.command"
# Linux
systemctl --user disable --now com.suvichak.claude-limit-resume.timer
rm ~/.config/systemd/user/com.suvichak.claude-limit-resume.{service,timer}
```

Then delete the Claude scheduled task `auto-resume-weekly-maintenance` in the
Claude desktop app.
