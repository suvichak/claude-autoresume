# Auto-resume Claude Code after the 5-hour limit

> [!WARNING]
> **This is a PUBLIC repository.** Anyone can read every file and commit.
> Never commit secrets, API keys, Pushover tokens, transcripts, job notes,
> logs, or paths and names from private or work projects. Keys belong in the
> macOS Keychain (see `limit-resume.py` → Pushover), never in this repo.
>
> Personal tool, provided as is. It runs `claude` unattended (headless
> resumes, background jobs in `bypassPermissions` mode) — read
> [INSTALL.md](INSTALL.md) and [runbook.md](runbook.md) before installing.

When a Claude Code session runs out of its 5-hour quota, the turn dies mid-task
and the conversation sits there until a human comes back. This picks it up again
by itself, the moment the quota resets, and costs **nothing** while it waits.

Built 2026-09-20. Lives in `~/.claude/tools/limit-resume.py`, run by the
LaunchAgent `com.suvichak.claude-limit-resume`.

Runs on **macOS** (everything) and **Linux** (no Remote Control watchdog).
**Windows is not supported.** To install, follow [INSTALL.md](INSTALL.md), or
paste its "Install with Claude Code" prompt into Claude Code on the machine.

```
Claude session hits quota
        │
        ▼
 transcript record:  error=rate_limit  apiErrorStatus=429
                     quotaLimits={status:rejected, rateLimitType:five_hour,
                                  resetsAt:<unix epoch>}
        │
        │   (session is dead; no turn is granted, so Claude itself
        │    cannot schedule anything from here)
        ▼
 launchd wakes limit-resume.py every 5 min  ── plain Python, 0 tokens ──┐
        │                                                               │
        ├─ reads transcript tails, finds quota stops                    │ loops
        ├─ now < resetsAt + 90s ?  → exit, do nothing ──────────────────┘
        └─ past the reset ─┬─ Remote Control chat (phone/web)?
                           │     └─ Haiku relay: SendMessage "continue…" to the
                           │        chat's inbox /tmp/cc-socks/<pid>.sock;
                           │        the chat runs the turn itself, phone sees it
                           ├─ still open elsewhere (claude --resume <id>, or a
                           │  claude working in the chat's folder)?
                           │     └─ alert "type continue there", no headless turn
                           └─ terminal session, closed
                                 └─ claude --resume <session-id> -p "continue…"
                                    detached; a later sweep collects the reply
                                       │
                                       └─ last line DONE → session retired
```

## Remote Control chats are continued in the chat (2026-10-26)

A headless `claude --resume -p` only writes the local transcript. A Remote
Control chat is a live child process that streams to claude.ai and keeps its
own context, so a headless turn is invisible on the phone and unknown to the
chat. That's how a resumed turn once submitted a pending form while the phone
still asked whether to submit it.

So for a transcript owned by Remote Control (its first user `uuid` is in
`rc-debug/bridge-transcript-cse_<id>.jsonl`), the sweep finds the live child
(`--session-id cse_<id>`) and its inbox `/tmp/cc-socks/<pid>.sock`. It then
runs a throwaway `claude -p --model claude-haiku-5-5 --allowedTools SendMessage`
(Bash, Edit, Write, NotebookEdit, WebFetch and WebSearch denied; run from
`~/.claude/tools`, which auto mode never resumes) in the chat's permission
mode, which sends the resume prompt to `uds:<sock>`.
The chat wakes, even when idle, and continues where it stopped. The prompt
appears in the chat as a message from another session. If there is no live
child or the relay doesn't confirm `SENT`, it sends one "open it and type
continue" alert and never falls back to a headless resume. Details and the dead
ends: [[UPGRADE]] → 2026-10-26.

A chat that is not a Remote Control chat is resumed headless only when nothing
else has it open. Two checks: another process with `--resume`, `-r` or
`--session-id <id>` on its command line, and (since the 2026-11-22 decisions)
a live `claude` process whose working directory is the chat's folder - a
terminal or desktop-app chat started fresh has no `--resume` to match. One
`lsof -a -p … -d cwd` call reads every `claude` process's folder; Remote
Control servers and their chats, job sessions and the tool's own processes do
not count. Either way it sends "type continue there" and launches nothing; when
`ps` or `lsof` cannot tell, it does the same (fail safe). A second `claude`
open in the same folder on another chat holds the resume back too: type
continue in the stopped chat.

**Several folders.** Remote Control can serve more than one folder, one
server each, named `auto-<folder name>` (install.sh asks, or
`LIMIT_RESUME_RC_DIRS`). Each folder has its own launcher and Login Item, and
the watchdog keeps each server up on its own: its own tries, breaker,
throttle, exit status, lock and stale/hung checks. Only a folder's own server
counts for it: the one named for it, or one you started by hand in that folder
(Remote Control allows one server per folder; a second gets a 409, so the
watchdog leaves it alone and starts nothing next to it). A server for another
folder does not count. Auto-resume still covers sessions in every folder; the
list only says which folders get a Remote Control server.

## Obsidian is kept open (2026-11-25)

`limit-resume.py obsidian on` (macOS, opt-in) adds a second watchdog to the
sweep: if the Obsidian app is not running (`pgrep -x Obsidian`), it is reopened
in the background with `open -g -a Obsidian` and an alert says so. A vault
synced by an Obsidian plugin (LiveSync and the like) only syncs while the app
runs, so a crash or an accidental quit would otherwise stop the sync until
someone noticed. At most one reopen per 10 min; after 3 reopens in a row that
leave it closed, it stands down with one alert until `obsidian on`. When
`pgrep` cannot answer, it does nothing. `obsidian off` and `disarm all` stop it.
It never quits or restarts a running Obsidian.

## Background jobs: spend the quota the week would otherwise lose (2026-11-18)

Weekly quota left over at the weekly reset is gone. The same sweep can run a
backlog of unattended jobs in the run-up to the reset, on what is left, without
ever eating into the quota the week still needs for interactive work. Each job
runs as a **Remote Control session**, so it appears in the Remote Control list
on the phone and claude.ai, where you can watch it or answer it.

A job is a Markdown note in one folder (an Obsidian folder works well). The
note's frontmatter is the job's state; its body is the prompt:

```markdown
---
status: queued
order: 10
cwd: /Users/suvichak/Project/my-app
---
# Short title

What to do, what to read, where to write the output (a vault path), when to stop.
```

```bash
python3 ~/.claude/tools/limit-resume.py jobs on "<folder>" --cwd <default dir>
python3 ~/.claude/tools/limit-resume.py jobs status
python3 ~/.claude/tools/limit-resume.py jobs stop     # stop the running job now (note set to hold)
```

**When a job runs.** All of these must hold. The sweep checks every 5 minutes.

| rule | value |
|---|---|
| 5-hour usage | pause trigger 60% (`JOB_FIVE_HOUR_MAX`): a job starts only below it, and a running job seen at or above it by a sweep is stopped and set `paused`; it resumes in a later window. Sweeps poll every 5 min, so a job lands at about 70-80% — intended, the trigger is not a hard cap |
| weekly usage | below today's cap: 90% from local midnight the day before **this account's** weekly reset until the reset, 80% otherwise (`WEEKLY_CAP_LAST_DAY`, `WEEKLY_CAP_DEFAULT`). Reset Mon 03:00 → 90% all Sunday and Monday until 03:00; reset Thu 14:00 → 90% all Wednesday and Thursday until 14:00 |
| weekly gate | opens 72 h before this account's weekly reset with ≥ 50 points left under the cap, 30 h before with ≤ 5 left, linear in between |
| usage known | both the 5-hour and the weekly figure, each read in the last 10 min (`USAGE_MAX_AGE`) and from a window that has not reset since. **Unknown counts as over:** no job starts, and a running job is paused (`usage unknown: …`). The first failed probe sends one alert, until a probe works again |
| concurrency | **one task at a time, chat resumes included.** Order: (1) a chat whose limit has reset (high), (2) notes with `priority: top` (medium), (3) the rest of the backlog (standard). Within (2) and (3) a paused job resumes before a queued one starts, then by `order`. A due chat resume waits for a running job to end; a job does not start while a resume is due or a resumed chat is still working (transcript written in the last `RESUME_BUSY`, 15 min). A blocked job you answer becomes the running job again (a job running meanwhile is paused). A finished (done/failed) job you type into while its session is kept open is the running task the same way as a resumed chat: the next job and due resumes wait until it has been quiet for `RESUME_BUSY`; it has no caps and no `JOB_MAX_H`, and its note stays done/failed |
| context size | a paused job whose session has more than 400k tokens of context (`JOB_NEW_AT`) restarts in a **new** session instead of `--resume`: the note gets the new `session_id`, `previous_session` and a `## Run … (new session)` line. A chat over 700k (`CHAT_NEW_AT`) continues in a new Remote Control session named `continued: …`; the old session is retired. Tokens = the last turn's input + cache tokens, as `/context` counts them |

**Every account has its own weekly reset, and it moves.** The runner takes it
from the usage figures (`seven_day.resets_at`), so each machine follows
whichever account it is logged in to, and nothing is hard-coded. When Claude
hands out a usage reset, usage drops and the weekly reset moves to a new day
and time. The runner always follows the newest reading, and when the reset
lands on a different weekly slot (more than `RESET_SLACK`, 1 h, off the old one
modulo a week) it logs and alerts "Weekly reset moved: was …, now …". The 90%
day and the 72 h/30 h gate move with it. `jobs status` shows the last move.

Where no reading carries a reset time, the runner uses the last one it saw,
rolled forward a week at a time. `jobs on --reset "Thu 14:00"` is only for a
machine that has never seen one: the first real reading replaces it (logged
if it disagreed). `--reset auto` clears it. With no reset time at all the gate
stays shut.

**How a job runs.** An interactive session, not `claude -p`:

```
claude "<rules + note body>" --session-id <uuid> --remote-control "job: <title>"
       --model opus --permission-mode bypassPermissions --settings job-settings.json
```

Interactive Claude needs a terminal, so it runs under `limit-resume.py jobpty`,
a small detached helper that gives it a pseudo-terminal (200×50), logs the
screen to `~/.claude/tools/jobs/<session_id>.pty.log` and never types into it.
The session takes input only from Remote Control. The helper runs in its own
process session, so launchd (`AbandonProcessGroup`) or systemd
(`KillMode=process`) does not kill it when the sweep exits.

An interactive session doesn't exit when the work is done, so the transcript
says when it is:

| the session's last message | the runner |
|---|---|
| last non-blank line is `JOB DONE` (any case; bullets, quotes, markdown or an emoji around it are fine) | `done`, appends `## Run …` with the reply, leaves the session open (idle) for `JOB_KEEP_OPEN` (30 min) so you can read it on the phone, then closes it. Type in it meanwhile and it counts as the running task, uncapped (see concurrency) |
| last non-blank line is `JOB FAILED: <why>` | `failed`, same |
| anything else, idle for `JOB_IDLE` (20 min) | `blocked`: it is waiting on a question. Alert "needs an answer in Remote Control", the question goes in the note, the session stays open for a day. The queue moves on meanwhile. Answer it on the phone: once you have typed in it, it is the running job again (note `running`; a job running meanwhile is `paused`), under the caps; when it then ends with `JOB DONE` the note becomes `done` |
| a quota stop | session stopped, `paused` ("usage limit until …") with `paused_until`: it is not resumed before that limit resets, whatever the readings say, and nothing else starts meanwhile. A resume that stopped before doing any work is not counted against `MAX_RESUMES` |
| 5-hour or weekly cap reached, or usage unknown | session stopped, `paused` with the reason |
| the weekly reset passes | session stopped, `paused`: the new week's gate decides again |
| you set its note to `hold` (or any status but `running`), or `jobs stop` | session stopped at the next sweep (`jobs stop`: now), your status kept (`jobs stop` sets `hold`) |
| session exited without a marker | `paused`, resumed later (at most `MAX_RESUMES` times) |
| no transcript after `JOB_START_TIMEOUT` (5 min) | stopped, `failed`: something is waiting on the terminal; see the `.pty.log` |
| one run past `JOB_MAX_H` (8 h; each resume is a new run) | stopped, `failed` |

Stopping a session stops everything it started: SIGTERM, then after
`JOB_STOP_GRACE` (30 s) SIGKILL to claude's process group and to every process
that was below it, so no build or shell is left running in the repository.

Resuming starts `claude "<continue prompt>" --resume <id> --remote-control …`.
Only transcript lines written after the resume count, so an older run's
`JOB DONE` can't end a new one. A job is never resumed while another process
has its session open (`--resume` / `--session-id` in `ps`). If the runner loses
track of a running job (a sweep killed mid-launch, a lost state file), the next
sweep finds its `jobpty` helper and watches it again instead of starting a
second session; a second live session of a note is stopped. A paused job whose
transcript is gone restarts in a new session, like one over 400k tokens.

**Nothing may wait on the terminal.** Nobody is there to answer a dialog, so:
the job's `--settings` turn off the bypass-mode confirmation
(`skipDangerousModePermissionPrompt`), and a job whose folder is not trusted
(`~/.claude.json` → `hasTrustDialogAccepted`, a trusted parent counts) fails
before launch with "run `claude` there once and accept the trust prompt".

**Rules.** The prompt tells the job nobody is watching: no questions, no
destructive actions, git only to read, end with `JOB DONE` or `JOB FAILED`.
`job-settings.json` (passed with `--settings`, so job sessions only) enforces
it with a `PreToolUse` hook on **every** tool (`limit-resume.py guard`):

- Shell commands (Bash, Monitor, any tool with a `command`) are parsed, not
  matched: every command through `;` `&&` `|`, `$( )`, backticks, heredocs,
  `sh -c`, `eval`, launchers (`env`, `xargs`, `timeout`, `nice`, `nohup`,
  `find -exec`, …), script files run with `sh`/`bash`/`source`/`./x`, and
  python/node/ruby/perl code (`-c`/`-e`, a script file or a heredoc).
- git may run only read-only subcommands (`status`, `log`, `diff`, `show`,
  `rev-parse`, `blame`, `grep`, `branch`/`tag` listing, `stash list`,
  `config --get`, …). `git -c alias.x=…` is followed; any other `-c`,
  `--exec-path=` and `GIT_*` variables that make git run commands are refused.
  So `commit`, `push`, `merge`, `pull`, `revert`, `cherry-pick`, `stash`,
  `update-ref`, aliases, `G=git; $G commit` and `xargs git` are all blocked.
- Writes to `~/.claude/tools`, to Claude settings files (they can turn hooks
  off) and into a `.git` directory are refused, as is `AskUserQuestion` (a
  dialog nobody can answer: the job writes its question and stops instead).
- It fails closed: unreadable input, a command it cannot parse, a script it
  cannot read (write it first, run it in a separate command) or any error of
  its own blocks the call; the hook command ends in `|| exit 2`, so a python
  that has gone away blocks too.
- Limits: it is a static check. Code that builds the word `git` at run time,
  or a program such as `make` or an npm script that runs git, is beyond it.

The second layer is a `git` wrapper, `~/.claude/tools/jobbin/git`, first on a
job session's PATH (continued chats do not get it). The tool rewrites it with
each job launch, with its own version and the real git's path baked in (the
first git on PATH that is not the wrapper, so it never calls itself). With
`LIMIT_RESUME_JOB=1` it runs `limit-resume.py git-check <args>` - the guard's
git rules - before the real git, and refuses when that says no or cannot run
at all; outside a job session it is plain git. So git run by name from
anywhere in a job - a script, make, npm, a command built at run time - is
checked. Not covered: a command that runs git by an absolute path built at run
time, or that unsets `LIMIT_RESUME_JOB` first. Unverified live: that Claude
Code's Bash tool keeps the session's PATH order (a shell profile that puts a
folder holding git first would bypass the wrapper; the hook still applies).

Job sessions are excluded from auto-resume; the job runner resumes them itself.

**Lifecycle.** `queued` → `running` → `done` / `failed` / `blocked`, or
`paused` (`limit` means the same) and back to `running`. `hold` parks a job;
`order` sorts (notes without one, or with one that is not a number, run last,
by name); notes named `_…`, and notes in subfolders, are ignored. A status the
runner does not know is logged once and the note left alone. The runner adds
`session_id`, `started`, `finished`, `resumes`, `paused_why`, `paused_until`
and `runner` (this machine's name: another machine leaves a paused, running
or blocked note of this one alone; a queued note is anyone's). Setting a
`done` note back to `queued` runs it again, with one alert in case a sync
revert did it. A fresh start (`queued`, the documented retry) resets
`resumes` and sends the note body without the runner's `## Run …` sections.
Frontmatter may carry a byte-order mark, Windows line endings and trailing
`# comments`. Every start, pause, resume, finish, failure and question sends
an alert.

**Commit between jobs (2026-11-23).** A note with `commit: <absolute path>`
names a git repo the job works in. The job session itself still cannot commit
(guard + wrapper unchanged). The **runner** commits instead: after `JOB DONE`,
and before the next job can start in the same sweep, it runs the real git
(never the job wrapper) in that repo: `add -A`, then `commit -F -` with the
note's title, the job's last ```` ```change ```` block and `Job:` / `Session:`
trailers. It never pushes and skips no hooks. The note gets `committed: <sha>`
and a line in its Run section; nothing staged → "Nothing to commit", still
done. The path must be the top of a work tree. A staged file over 10 MB
(`JOB_COMMIT_MAX_BYTES`) or a failed `add`/`commit` unstages everything and
turns the job `failed` (`commit failed: …`), so jobs that depend on it stop.
A **queued** job with `commit:` does not start while its repo has uncommitted
or untracked changes: one alert ("… waits - <repo> has N uncommitted
path(s)"), the queue waits, and it starts by itself once the tree is clean.
Paused jobs resume on their own changes. A blocked job answered later and
ending `JOB DONE` is committed the same way.

**Both figures are required.** A probe that reports only the binding window
(seen on 2026-11-18 on the Mac VM: one `seven_day` event, no `five_hour`)
leaves the 5-hour figure unknown, and unknown counts as over: no job starts.
Since 2026-11-20 the probe reads `rate_limit_info.unifiedWindows`, which
carries both windows.

**Reading usage costs (almost) nothing.** In order of preference:

1. The job session's own status line: `job-settings.json` points it at
   `limit-resume.py statusline`, which caches the plan's `rate_limits`
   (5-hour and weekly `used_percentage`, `resets_at`) every time it renders.
2. Your own sessions' status line, if you point it at
   `limit-resume.py statusline` (optionally `-- <your own command>`).
3. A probe: one `claude -p` turn on Haiku, read for its `rate_limit_event`
   records. While a job runs, at most once per sweep; otherwise at most every
   15 min. It skips the probe when no reading could open the gate anyway (the
   weekly reset is more than 72 h out, or the 5-hour window is already over
   60%), but only for `JOB_PROBE_MAX_SKIP` (6 h): a usage reset from Claude
   can change both, so a 6 h old reading is never trusted to keep the gate
   shut.

Unknown usage keeps the gate shut, and a running job without a fresh reading
is paused: a job never starts or runs blind. The cache is merged, not
overwritten: within one window the higher figure is kept (usage only grows),
a figure from an earlier window (an idle session re-rendering an old reply)
is ignored, under an flock with a temp file per writer.

## Weekly maintenance: a Claude agent check-up (2026-11-23)

The weekly maintenance is a **Claude scheduled task**, "Auto-resume weekly
maintenance", set for 10 minutes after **this account's** weekly reset. It is
not part of the 5-minute sweep, and it is an agent, not a script: each week a
fresh Claude session on this machine runs the check-up in `TASK_PROMPT`.

Where it runs: a **Claude Code scheduled task** (`create_trigger`, new session
per run) in this machine's Remote Control environment, so the session runs on
this machine (macOS and Linux); or the Claude desktop app's own scheduler.
Either way it only fires while that server or app is up.

Each run:

1. **Maintenance.** Pipes the app's usage (`get_usage` → `plan`) into
   `maintain --scheduled --usage-json -`, or runs `maintain --scheduled`
   (one Haiku probe) where that tool is missing. `maintain`:
   - **records the weekly reset** and flags a move to another day (`MOVED`, plus
     the "Weekly reset moved" alert). The caps and the job gate use it from then on.
   - **tidies the auto-resume state:** drops sessions whose transcript is gone
     or untouched for `MAINT_KEEP_DAYS` (30).
   - **tidies the job queue:** forgets dead open sessions, removes job
     terminal logs older than 30 days, counts the queue.
   - **rotates** `limit-resume.log` past 5 MB.
   - **sends one summary line**, logged and alerted:
     `Weekly maintenance (scheduled): weekly reset Thu 26 Nov 13:00; weekly 3% used; jobs: 4 done this week, 2 queued`.
2. **Check.** `doctor`, and the last 200 lines of the log and the error log.
3. **Safe repairs** for what it found: kickstart a stale scheduler, `rc on`
   for a tripped Remote Control breaker (macOS), and an upgrade from the
   `src=` checkout in `install.conf` (`pull --ff-only`, tests must pass, then
   `install.sh`). Never commits, pushes, deletes, edits the kit or job notes,
   switches auto mode / jobs / the watch, or uses `--force`.
4. **Moves itself** to `task-spec`'s schedule when a reset from Claude moved the day.
5. **Reports** in up to 10 lines, and alerts (`notify`) when something is left for you.

**Installing it.** A shell script can't create a Claude scheduled task, so
`install.sh` runs the first maintenance itself (`maintain --install`) and ends
by printing a paste-in prompt (`limit-resume.py task-setup`, saved as
`~/.claude/tools/weekly-check-setup.txt`). The human pastes it into a Claude
Code session on that machine, and that Claude creates the task and runs it
once. `doctor` reports the last maintenance run and the last run of the
scheduled task itself, and says `STALE` once the scheduled task has not run
for 8 days (counted from the first maintenance until it has run once): install
runs do not hide a task that never fires.

## Why not do the waiting inside Claude

The obvious designs are more expensive and less reliable:

| approach | cost while waiting | survives CLI exit / reboot |
|---|---|---|
| `CronCreate` job polling every 15 min | a **billed turn per tick**, ~20 per window | no — jobs are in-memory, session-only |
| a skill using `ScheduleWakeup` | clamped to 3600 s, so 5+ billed turns per wait | no |
| **launchd + `claude --resume`** | **zero** — it is Python, not a model turn | yes |

There is also a hard blocker for the in-session designs: when the 429 lands,
Claude gets **no turn**. It cannot write a handoff note, set a timer or schedule
anything — it is already cut off. Whatever waits has to live outside the process.

And nothing needs to poll or guess *when* to come back: the CLI writes the exact
reset time into the transcript. Verified against 7 real limit hits — `resetsAt`
matched the displayed "resets 4:20am" text every time.

## Only ever resumes a quota stop

This is the safety-critical part. A session can die many ways, and most of them
must **not** be retried unattended. Resume requires four fields together:

```python
error == "rate_limit"  and  apiErrorStatus == 429
  and quotaLimits.status == "rejected"
  and numeric quotaLimits.resetsAt  and  quotaLimits.rateLimitType
```

Replayed against every error record in 74 local transcripts:

```
RESUMABLE (quota stops):      8  rate_limit
NOT resumable (left alone):  12  Connection error.
                              7  529 overloaded
                              1  server_error
                              1  connection lost mid-response
```

Checking `apiErrorStatus == 429` alone would also have passed that test — but
only by luck, because nothing else in this history happens to carry a 429. A
transient 429 from API-level throttling would have triggered a blind retry. The
four-field check excludes it by construction.

## Guards

| guard | value | what it prevents |
|---|---|---|
| `AUTO_MAX_AGE_H` | 12 h | resuming long-abandoned sessions. A real transcript here sat on an unresolved 429 for **305 hours** — auto mode would have revived it. Counted from the reset, i.e. how long the stop has been resumable, so a weekly stop is resumed after the weekly reset. A stop skipped for age is logged and alerted once (if it reset within the last 7 days), so a stalled sweep leaves a trail. Explicitly armed sessions are exempt; sessions auto mode resumed before are not, and are ignored while auto mode is off. |
| `MAX_PER_SWEEP` | 2 | a pile-up of blocked sessions all resuming at once and re-burning the window the instant it opens. One task at a time: a headless resume runs detached and the next starts only once a later sweep has collected it, so they stagger at least 5 min apart. A Remote Control chat (or a new session for an over-large chat) counts as running until it goes quiet. |
| `MAX_RESUMES` | 8 in a row | an endless resume→limit→resume loop chewing every future window. The count restarts when you type in the session; reaching it alerts, and the session is watched again once you work in it. |
| `BUFFER` | 90 s | racing the reset and getting re-rejected. |
| `DONE` sentinel | — | a finished task being resumed forever. A reply whose last line is exactly `DONE` retires the session, until you work in it again. |
| open-elsewhere check | — | a headless turn behind a terminal or app that still has the session open (`claude --resume <id>`, `-r`, `--session-id` in `ps`, or a `claude` working in the session's folder, by `lsof`): alert instead; `ps` or `lsof` failing counts as open. |
| state lock | — | a sweep and a command (`disarm`, `auto`, `rc`, `jobs`, `maintain`) saving over each other's changes. |
| mark-before-launch | — | a crashed or timed-out resume being retried on the next sweep (see below). |

## Crash safety

The resume attempt is written to the state file **before** `claude` is launched,
not after. If the sweep is killed, times out, or the machine loses power
mid-resume, the block stays marked `handled` and the next sweep will not fire it
again. Getting this backwards turns one crash into an infinite resume loop.

Every failure mode of the launch is caught and logged rather than raised:
`claude` missing from PATH, `OSError` on fork. A headless resume runs
detached in a process group of its own; one still running after an hour is
stopped, with every process it started, by the next sweep. One transcript that
cannot be read is skipped and logged once; the rest are still resumed. A
sweep that crashes logs its traceback, alerts once per run of crashes, and
`doctor` shows `last crash` until a sweep runs cleanly. A state file that does
not parse is kept aside as `.corrupt-<time>`, never saved over, and the sweep
carries on from `.bak`, the copy of the last good save.

## Survives crashes and reboots

* `RunAtLoad` — sweeps immediately when loaded, and LaunchAgents load at user
  login, so it comes back on its own after a reboot.
* `StartInterval: 300` — reruns the sweep every 5 min, so a crashed sweep is
  retried at the next tick. `KeepAlive` / `ThrottleInterval` / `ProcessType`
  / `LowPriorityIO` were dropped on 2026-11-21 as redundant; that did not cure
  the 2026-11-20/21 stall, whose cause is unverified (runbook → Troubleshooting).
* `deadman` — launchd can hold the agent without a word, and a sweep that never
  runs cannot say so. The Remote Control launcher, which Terminal starts, runs
  `limit-resume.py deadman` every 10 min and alerts once per stall (last sweep
  over 15 min old on two checks in a row, no sweep running). `doctor` reports
  such an agent as `held`, not healthy.

A **LaunchAgent** (user-level, `~/Library/LaunchAgents/`) is deliberate — not a
LaunchDaemon. `claude` needs the user's login session and credentials; a root
daemon running before login would not be able to authenticate.

The cost of that choice: on macOS the watchdog only runs once someone has logged
in, and with FileVault the machine waits at the unlock screen before that. For
planned reboots `sudo fdesetup authrestart` skips it while keeping encryption —
see [[runbook]]. Linux has no such gap, which is what `loginctl enable-linger`
in the installer is for.

## The resume prompt matters

Verified end to end against the live CLI on 2026-09-20: a synthetic quota stop
appended to a real session, swept, resumed, `DONE`, retired.

The first attempt exposed a flaw. With a mild prompt ("if the task is already
finished, reply DONE"), the resumed session **asked clarifying questions**
instead — "Is there a specific file, branch or task you'd like to resume?" No
one is at the keyboard during an unattended resume, so that burns a turn, never
retires, and leaves the session sitting in state. The prompt now says so
explicitly:

> Nobody is at the keyboard, so do not ask questions and do not wait for
> confirmation. If there is no unfinished work, **or you cannot proceed without
> an answer from the user**, reply with exactly DONE and stop.

With that wording the same session replied exactly `DONE` and retired cleanly.
Any custom prompt passed to `arm` should keep that escape hatch, or an
un-answerable resume will hang until `RESUME_TIMEOUT`.

## Known limitations

* The resume path is proven with a *synthetic* quota stop on a real session.
  What has never been observed is the CLI's behaviour resuming a session that
  the server is still rate-limiting — if the reset time is ever wrong, the
  resume simply fails and is logged, but it is not retried.
* If a blocked session was continued in a *new* session (rather than resumed in
  place), the old transcript still ends on a quota stop and auto mode will
  resume it. The 12 h age guard bounds the damage; there is no reliable
  fork-detection signal in the transcript.
* A resume runs in the permission mode of the session's last user turn (e.g.
  `auto`), so it has the same rights the session had. Only when the transcript
  names no mode does it fall back to `PERM` (`acceptEdits`).
  `LIMIT_RESUME_PERMISSION_MODE` forces one mode for every resume.
* Auto mode covers sessions in **every** project directory, not just this vault.

Installing on another machine: [[INSTALL]] — `./install.sh` covers macOS (launchd) and Linux (systemd --user).
Updating a machine that already runs an older version: [[UPGRADE]].
Auto-starting and watchdogging `claude rc` (Remote Control): [[remote-control-autostart]].
Day-to-day commands and troubleshooting: [[runbook]].
Record formats this relies on: [[transcript-records]].

## Tests

```bash
python3 tests/test_limit_resume.py
```

926 checks, ~90 s, no network and no Claude spend: synthetic transcripts plus a fake `claude` on
PATH exercise detection, both guards, the pile-up throttle, crash handling, the heartbeat and the Remote Control watchdog (process matching, throttle, breaker, replays of the 2026-09-27 outage and the 2026-10-07 lost login, alert routing, reaping orphaned sessions before a relaunch, restarting a server left on an old binary or frozen with a silent debug log, Remote Control chats relayed to their inbox instead of resumed). The launcher itself runs in a throwaway HOME with fake `claude`, `ps` and `curl`, including its `deadman` loop; so do `install.sh` and `uninstall.sh`, with fake `launchctl`, `osascript` and `ps` (refusals before any write, saved folder, reload only on change, the in-flight and downgrade guards, bootstrap retries, uninstall and purge). `deadman`, `reload-check`, atomic state writes and doctor's `held` line are tested in-process. Background jobs (J1–J10), with a fake interactive `claude` on a real pseudo-terminal: caps and gate against different accounts' weekly resets and `--reset`, a weekly reset moved by a usage reset (alert, caps follow, old readings not trusted), a probe with only the weekly window, weekly maintenance from the app's usage card (reset recorded and MOVED flagged, pruning, summary, probe fallback), the scheduled task's cron following the reset, usage parsing, Remote Control launch arguments and job settings, queue order, JOB DONE / JOB FAILED, finished sessions kept open then closed, paused-first resume that ignores an older run's marker, pause at the cap and at a quota stop (session and claude process both gone), exit without a marker, blocked on a question then finished after an answer, a session that never starts, untrusted folder, gate shut / usage unknown / jobs off, lost-state recovery, job sessions excluded from auto-resume, the commit/push guard and the status line feed. Audit batch 3 (J17–J30, J10b): stale or partial usage failing closed for starts and running jobs (one alert), a lost session adopted instead of resumed twice, a resume refused while another process has the session, an answered blocked job taking over from the running one, `paused_until` after a quota stop, a re-queued job starting clean, the whole process tree stopped (also with the helper dead), `jobs stop` and a status set in the note, another machine's notes left alone, frontmatter edge cases, the weekly reset pausing a run, a vanished transcript, unwritable job settings, doctor's job health and scheduled-maintenance lines, the monotonic usage cache, last-line markers, and the guard against 72 read-only commands and 55 bypass forms. The 2026-11-22 decisions (D1–D7, 22b–22d, 31d): the folder list from install.conf (one-folder format, a taken name skipped), which server serves which folder (named, started by hand in the folder, another folder's, lsof or ps failing), per-folder watchdog state, alerts, exit status, reaping and doctor lines, the launcher's per-folder check, tag and once-per-machine dead-man check, the lsof owner check (owner, fail safe, what does not count), a typed-in finished job holding the queue and due resumes without caps, the git wrapper (allowed, refused, plain git outside a job, no recursion, fails closed, job PATH), and install.sh with several folders, the terminal prompt, a name collision and uninstall of every launcher.
