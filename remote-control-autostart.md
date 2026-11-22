# Auto-starting `claude rc` (Remote Control) at login

`claude rc` is `claude remote-control` — the persistent server that lets
claude.ai/code and the mobile app drive sessions on this machine. Unlike the
[[README|resume watchdog]], it has no scheduler of its own, so after a reboot it
is simply gone until someone types it in a terminal.

Observed 2026-09-21: machine booted 23:27:25, `claude rc` did not appear until
01:34:33 when it was started by hand. The watchdog LaunchAgent came back on its
own at login; Remote Control did not.

## It runs headless

Worth confirming before wiring it to launchd, since it has a TUI ("press space
for QR code"). It does not need a terminal — probed with stdin from
`/dev/null` and no TTY:

```
·✔︎· Connected · my-project · HEAD
    Capacity: 1/32 · New sessions will be created in the current directory
```

Alive and serving sessions after 20 s. A LaunchAgent works.

## What is actually installed

Not the LaunchAgent below — that was the first design. What shipped is simpler
and was verified in production:

1. `~/.claude/tools/claude-rc.command` opens Remote Control in a Terminal
   window, with a guard so it stands down if one is already serving the folder.
2. That launcher is a **Login Item**, so a reboot brings it back.
3. The existing 5-minute sweep (`limit-resume.py`, already installed for the
   resume watchdog) checks every cycle whether Remote Control is alive and
   re-opens the launcher if not. No second scheduler.

Enable the watchdog with `limit-resume.py rc on`; `doctor` then reports
`remote ctl : watched, running`.

### Verified by killing it

2026-09-21, on the live machine:

```
killed 01:51:23   PID 3834   claude rc                    (hand-started)
01:54:53          rc watchdog: Remote Control is not running, starting it (try 1/3)
now running       PID 5494   claude remote-control --name auto-obsidian
```

Back in ~3.5 minutes, within the sweep cadence. The `--name auto-obsidian` flag
comes only from the launcher, so that is provably the watchdog's work. It also
settles a design risk: `open` **can** raise a Terminal window from launchd's
context, so no headless fallback is needed.

Guards, so a restart loop cannot run away: 10-minute minimum between launches,
three consecutive tries then it stands down (`rc on` resets), and if `ps` itself
fails `rc_running()` returns true — assume it is up rather than spawn blind.
Since 2026-10-08 a network outage or a dead login no longer counts as a try —
see "Keeping the login alive" below.

Old sessions die before a new server starts: right before each relaunch the
watchdog kills Remote Control sessions whose server is gone (`--sdk-url
…/v1/code/sessions/…` processes reparented to launchd, ppid 1) — SIGTERM, then
SIGKILL after 5 s. Sessions of a live server and the desktop app's own sessions
are never touched; if `ps` fails nothing is killed. Added 2026-09-28 after the
22:38 internet outage.

## Keeping the login alive (2026-10-08)

Remote Control only works with a claude.ai `/login`; a long-lived
`claude setup-token` token is refused. So the login itself has to survive.

It was lost on 2026-10-07. The Login Item started Remote Control at 02:07:44,
seconds after unlock, on a network that was not yet steady:

```
02:07:49  GrowthBook: pre-init OAuth refresh failed (timeout)
02:07:55  Registration: 401 received, attempting token refresh
02:07:55  OAuth refresh failed: Request failed with status code 400
```

Likely cause (the server side is not visible): the first refresh reached the
server and rotated the token, the reply was lost to the 5 s timeout, and the
second refresh then used a dead token. Three blind watchdog retries tripped the
breaker by 02:30 and Remote Control stayed down ~23 h without a word, until
`claude /login` and a restart.

What changed:

| where | change |
|---|---|
| launcher | Waits until `api.anthropic.com` answers within 3 s, three times in a row, before starting `claude`. Gives up after ~5 min without touching the token. |
| launcher | One launcher at a time: a kernel lock on `rc-launch.lock`, held while the launcher lives and released however it dies. The watchdog probes it and does not launch, or spend a try, while one is alive. |
| launcher | No longer `exec`s. When the server ends it writes `~/.claude/tools/rc-last-exit`: `<end epoch> <auth\|network\|exit> <code> <seconds>`. `auth` = logged out, or the server's own `[bridge:api] Registration: Token refresh failed` line in the debug log (anchored — the log also carries session chatter). Written after Ctrl-C or a closed window too. |
| watchdog | API unreachable → waits, **no try spent**, logged once. An outage of any length no longer trips the breaker (the 2026-09-27 case). |
| watchdog | Last exit was `auth` → one alert ("run `claude /login`"), no retries. It relaunches by itself, with a clean breaker, once the stored login changes — the Keychain item being rewritten after the failure, by `claude /login` or by another session renewing it — or on `rc on`. |
| watchdog | A launch the launcher abandoned at its own network gate is refunded, so a flaky network cannot trip the breaker either. |
| watchdog | Breaker trip (3 failed starts with the network up) → one alert. Still stands down until `rc on`, which now also lifts the 10-minute throttle. |
| watchdog | Claude Code update (2026-10-19) → a running server stays on its old binary, and its sessions run that binary with the updater off. Below Remote Control's minimum version it can stay up without serving, and a live process used to count as healthy. Now, when the server's binary (`lsof`) differs from what `~/.local/bin/claude` points at, it is restarted on the new version, but only once no session is mid-turn: no session process has a child (tool shell or `caffeinate`) and the debug log shows no `bridge:ws`/`bridge:activity` traffic for 10 minutes. Busy → waits, logged once, for at most 2 h; then it restarts anyway. Version unknown, or debug log unreadable → left alone. |
| watchdog | Frozen server (2026-10-28) → a process that is alive but stuck counts as running, and on 2026-10-27 one stayed so for 23 h. Now, if its debug log has had no line at all for 15 min, and still none 10 min later (same pid), it is stopped and relaunched, with one alert. A healthy server logs every ~3 min with sessions open, every ~9.5 min without. Network down, or debug log unreadable → left alone. |
| alerts | If Pushover is set up but the post fails, the alert is re-sent every sweep until it lands. |

Alerts go to Pushover when its keys are in the login Keychain, otherwise to a
notification on the Mac's own screen. Nothing is stored in a file:

```bash
security add-generic-password -U -s limit-resume-pushover -a token -w   # app token
security add-generic-password -U -s limit-resume-pushover -a user  -w   # user key
~/.claude/tools/limit-resume.py notify                                  # test it
```

Not covered: the machine sitting at the FileVault unlock screen after an
unplanned reboot (nothing of the user's runs yet), and a server that is alive
and still logging but no longer connected (a *silent* one is caught since
2026-10-28).

The launcher is generated from `claude-rc.command.in`; edit the template and
re-run `install.sh`, not the installed copy.

## Alternative: a dedicated LaunchAgent

`~/Library/LaunchAgents/com.suvichak.claude-remote-control.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.suvichak.claude-remote-control</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/suvichak/.local/bin/claude</string>
    <string>remote-control</string>
    <string>--name</string>
    <string>auto-obsidian</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/suvichak/_obsidian</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/Users/suvichak/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>/Users/suvichak/.claude/tools/remote-control.out.log</string>
  <key>StandardErrorPath</key><string>/Users/suvichak/.claude/tools/remote-control.err.log</string>
</dict>
</plist>
```

Use this instead of the launcher if you want restart-on-crash without a
visible window. Do not run both.

```bash
plutil -lint ~/Library/LaunchAgents/com.suvichak.claude-remote-control.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.suvichak.claude-remote-control.plist
tail -5 ~/.claude/tools/remote-control.out.log      # expect "Connected"
```

`KeepAlive` is `true` rather than `SuccessfulExit: false` — this is a server
that should always be up, so launchd restarts it whenever it exits, including a
clean exit. `ThrottleInterval` keeps a failing restart loop to every 30 s.

`--name auto-obsidian` makes the launchd-started one distinguishable from a
hand-started one in claude.ai/code.

## One server per folder

This is the catch. Remote Control registers a folder, and a second instance in
the same folder is refused:

```
Error: Registration: Failed with status 409: This folder is already served by
a terminal `claude remote-control` on this device. Stop it first.
```

So once the LaunchAgent is serving `~/_obsidian`, typing `claude rc` there fails.
That is the intended end state — you should not need to type it — but it changes
the habit. To go back to a hand-run one:

```bash
launchctl bootout gui/$UID/com.suvichak.claude-remote-control
claude rc
```

To serve a *second* folder, add another LaunchAgent with a different `Label`,
`WorkingDirectory` and `--name`. The folder must already be trusted — run
`claude` there once interactively and accept the dialog, or Remote Control exits
with `Workspace not trusted`.

## Before enabling it

This makes the machine remotely drivable from boot, unattended, by anyone with
access to the Claude account — not just while you have a terminal open. On a VM
that is usually the point; it is still a wider door than a hand-started server
you close when you walk away. `--permission-mode` in `ProgramArguments` controls
what those remote sessions may do without asking; leaving it unset keeps the
default (ask).

## Checking it after a reboot

```bash
launchctl print gui/$UID/com.suvichak.claude-remote-control | grep -E "state =|runs ="
pgrep -fl "remote-control"
tail -3 ~/.claude/tools/remote-control.out.log
```
