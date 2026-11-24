#!/usr/bin/env python3
"""End-to-end tests for limit-resume.py. No pytest, no network, no real Claude.

Builds a throwaway ~/.claude/projects tree of synthetic transcripts and puts a
fake `claude` on PATH that records its argv, so the whole sweep runs for real:
detection, guards, the launch, state transitions.

    python3 tests/test_limit_resume.py
"""
import datetime
import fcntl
import glob
import importlib.util
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid as uuidlib

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "limit-resume.py")
if not os.path.exists(TOOL):                      # fall back to an installed copy
    TOOL = os.path.expanduser("~/.claude/tools/limit-resume.py")

PASS, FAIL = [], []
REAL_RUN = subprocess.run


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    flag = "ok  " if got == want else "FAIL"
    print("  %s %s" % (flag, name) + ("" if got == want else "\n       got %r want %r" % (got, want)))


def load_tool(root, fake_claude_out):
    """Import limit-resume.py with every path pointed into the sandbox."""
    spec = importlib.util.spec_from_file_location("lr_%s" % uuidlib.uuid4().hex, TOOL)
    lr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lr)
    lr.PROJECTS = os.path.join(root, "projects")
    lr.STATE = os.path.join(root, "state.json")
    lr.LOG = os.path.join(root, "log.txt")
    lr.RC_STATUS = os.path.join(root, "rc-last-exit")
    lr.RC_LOCK = os.path.join(root, "rc-launch.lock")
    lr.RC_DEBUG = os.path.join(root, "rc-debug")
    lr.TOOLS = os.path.join(root, "tools")
    lr.USAGE = os.path.join(root, "usage.json")
    lr.JOB_LOGS = os.path.join(root, "jobs")
    lr.JOB_SETTINGS = os.path.join(root, "job-settings.json")
    lr.DEADMAN = os.path.join(root, "sweep-stale")
    lr.INSTALL_CONF = os.path.join(root, "install.conf")
    lr.JOB_BIN = os.path.join(root, "jobbin")
    lr.ARGV_LOG = fake_claude_out
    return lr


def fake_claude(root, stdout="working\nDONE", exit_code=0):
    """A stand-in `claude` that records argv and prints what we tell it to."""
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    argv_log = os.path.join(root, "argv.txt")
    path = os.path.join(bindir, "claude")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n"
                 'printf "%s\\n" "$*" >> ' + argv_log + "\n"
                 'pwd >> ' + os.path.join(root, "cwd.txt") + "\n"
                 "cat <<'OUT'\n" + stdout + "\nOUT\n"
                 "exit %d\n" % exit_code)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
    return argv_log


def write_transcript(root, sid, kind="quota", reset_delta=-600, age_hours=0.1, modes=()):
    """Synthesise a session transcript ending the way `kind` says."""
    d = os.path.join(root, "projects", "-test-project")
    os.makedirs(d, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    when = (now - datetime.timedelta(hours=age_hours)).isoformat().replace("+00:00", "Z")
    resets_at = int((now + datetime.timedelta(seconds=reset_delta)).timestamp())
    recs = [{"type": "user", "message": {"role": "user", "content": "do the thing"},
             "timestamp": when}]
    recs[:0] = [{"type": "user", "permissionMode": mode, "timestamp": when,  # oldest first
                 "message": {"role": "user", "content": text}} for mode, text in modes]
    quota = {"type": "assistant", "error": "rate_limit", "apiErrorStatus": 429,
             "uuid": "stop-" + sid, "timestamp": when, "sessionId": sid,
             "quotaLimits": {"status": "rejected", "resetsAt": resets_at,
                             "rateLimitType": "five_hour"},
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "hit your session limit"}]}}
    if kind == "quota":
        recs += [{"type": "atis-latch"}, quota, {"type": "last-prompt"}]
    elif kind == "continued":                     # quota stop, then real work after it
        recs += [quota, {"type": "assistant", "uuid": "after",
                         "message": {"role": "assistant", "content": "carried on"}}]
    elif kind == "overloaded":
        recs += [{"type": "assistant", "isApiErrorMessage": True, "uuid": "x",
                  "error": {"message": '529 {"type":"error"}'}, "timestamp": when}]
    elif kind == "connection":
        recs += [{"type": "assistant", "isApiErrorMessage": True, "uuid": "y",
                  "error": {"message": "Connection error."}, "timestamp": when}]
    elif kind == "normal":
        recs += [{"type": "assistant", "uuid": "z",
                  "message": {"role": "assistant", "content": "all done"}}]
    path = os.path.join(d, sid + ".jsonl")
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return path


def launches(argv_log):
    if not os.path.exists(argv_log):
        return []
    return [l.strip() for l in open(argv_log) if l.strip()]


def settle(lr, secs=10):
    """Wait for every process the tool spawned (detached resumes) to exit."""
    for proc in list(lr._SPAWNED.values()):
        try:
            proc.wait(secs)
        except subprocess.TimeoutExpired:
            pass


def sweeps(lr, n):
    """n sweeps, each followed by its detached resume finishing."""
    for _ in range(n):
        lr.cmd_sweep([])
        settle(lr)


def scenario(name):
    print("\n%s" % name)
    root = tempfile.mkdtemp(prefix="lrtest-")
    argv_log = fake_claude(root)
    return root, argv_log, load_tool(root, argv_log)


class Clock:
    """Stands in for the `time` module so throttle windows can be stepped."""
    def __init__(self, t=1790500000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, secs):
        self.t += secs


def fake_run(ps_output="", open_raises=None, calls=None, net=None, keys=None):
    """subprocess.run stand-in: `ps` returns ps_output, `open` is recorded.

    `net` is a one-item list holding whether curl reaches the API, so a test can
    flip it mid-scenario. `keys` maps Keychain account -> secret for `security`.
    """
    def run(cmd, *a, **k):
        if calls is not None:
            calls.append(cmd)
        if cmd[0] == "ps" and isinstance(ps_output, Exception):
            raise ps_output
        if cmd[0] == "open" and open_raises:
            raise open_raises
        out, code = "", 0
        if cmd[0] == "ps":
            out = ps_output
        elif cmd[0] == "curl" and net is not None and not net[0]:
            code = 28
        elif cmd[0] == "security" and "-w" in cmd:
            out = (keys or {}).get(cmd[cmd.index("-a") + 1], "")
            code = 0 if out else 44
        return type("R", (), {"stdout": out, "stderr": "", "returncode": code})()
    return run


def rc_scenario(name):
    """A sandboxed tool with a launcher, a fake clock and a recording run()."""
    root, argv_log, lr = scenario(name)
    lr.RC_LAUNCHER = os.path.join(root, "claude-rc.command")
    open(lr.RC_LAUNCHER, "w").write("#!/bin/sh\ntrue\n")
    lr.time = Clock()
    calls = []
    lr.subprocess.run = fake_run(calls=calls)
    opens = lambda: [c for c in calls if c[0] == "open"]
    return root, lr, calls, opens


def rc_watchdog_scenarios():
    real_run, real_kill = subprocess.run, os.kill
    try:
        _rc_watchdog_scenarios()
        _rc_reap_scenarios()
        _rc_stale_scenarios()
        _rc_hung_scenarios()
    finally:
        subprocess.run, os.kill = real_run, real_kill


class FakeProcs:
    """A process table: `ps` lists it, `kill` edits it and records signals."""
    def __init__(self, rows, stubborn=()):
        self.rows = {pid: (ppid, cmd) for pid, ppid, cmd in rows}
        self.stubborn = set(stubborn)       # ignore SIGTERM
        self.sent = []                      # (pid, signal) excluding probes
        self.calls = []                     # every run() argv, in order

    def ps(self):
        return "".join("%6d %6d %s\n" % (pid, ppid, cmd)
                       for pid, (ppid, cmd) in sorted(self.rows.items()))

    def run(self, cmd, *a, **k):
        self.calls.append(cmd)
        out = ""
        if cmd[0] == "ps":
            out = self.ps() if "pid=,ppid=,command=" in cmd[2] else "\n".join(
                c for _, c in self.rows.values())
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()

    def kill(self, pid, sig):
        if pid not in self.rows:
            raise ProcessLookupError(pid)
        if sig == 0:
            return
        self.sent.append((pid, sig))
        if sig == signal.SIGKILL or pid not in self.stubborn:
            del self.rows[pid]


SDK = ("/Users/x/.local/share/claude/versions/2.1.282 --print --sdk-url "
       "https://api.anthropic.com/v1/code/sessions/cse_%s --session-id cse_%s")
SERVER = "/Users/x/.local/bin/claude remote-control --name auto-_obsidian"
APP_SESSION = ("/Applications/Claude.app/Contents/Resources/claude --print --sdk-url "
               "https://api.anthropic.com/v1/code/sessions/cse_app")


def reap_scenario(name, rows, stubborn=()):
    root, lr, calls, opens = rc_scenario(name)
    procs = FakeProcs(rows, stubborn)
    lr.subprocess.run, lr.os.kill = procs.run, procs.kill
    return lr, procs


def _rc_reap_scenarios():
    # ---- 17. old sessions die before a new server is spawned ------------
    lr, procs = reap_scenario("17. orphaned sessions are reaped before relaunch", [
        (4101, 1, SDK % ("orphanA", "orphanA")),        # server died, left behind
        (4102, 1, SDK % ("orphanB", "orphanB")),
        (5000, 1, "/bin/zsh -l"),                       # unrelated, ppid 1
        (5001, 1, "/Users/x/.local/bin/claude --resume abc"),  # interactive CLI
        (5002, 856, APP_SESSION),                       # desktop app's own session
        (5003, 1, "ps -Ao pid=,ppid=,command="),
        (5004, 1, "garbage"),
    ])
    lr.rc_running = lambda _folder: False
    st = {lr.CONFIG: {"rc_watch": True}}
    check("launches", lr.rc_watchdog(st), True)
    check("both orphans got SIGTERM",
          sorted(procs.sent), [(4101, signal.SIGTERM), (4102, signal.SIGTERM)])
    check("orphans are gone", sorted(procs.rows), [5000, 5001, 5002, 5003, 5004])
    check("nothing else was signalled", {p for p, _ in procs.sent}, {4101, 4102})
    kinds = [c[0] for c in procs.calls]
    check("reap (ps) happens before open",
          kinds.index("open") > max(i for i, k in enumerate(kinds) if k == "ps"), True)
    log = open(lr.LOG).read()
    check("each kill logged", log.count("killing orphaned session"), 2)
    check("no SIGKILL when SIGTERM worked", "SIGKILL" in log, False)

    lr, procs = reap_scenario("18. a live server's sessions are never touched", [
        (1306, 896, SERVER),
        (1767, 1306, SDK % ("live1", "live1")),
        (1975, 1306, SDK % ("live2", "live2")),
    ])
    check("rc_orphans ignores children of a live server", lr.rc_orphans(lr.rc_folders()[0]), [])
    check("reap with only live sessions kills nothing", lr.rc_reap_orphans(lr.rc_folders()[0]), 0)
    check("no signals sent", procs.sent, [])
    lr.rc_running = lambda _folder: True
    lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}})
    check("RC running -> watchdog does not reap or launch", procs.sent, [])

    lr, procs = reap_scenario("19. a session ignoring SIGTERM gets SIGKILL", [
        (4201, 1, SDK % ("stuck", "stuck")),
        (4202, 1, SDK % ("polite", "polite")),
    ], stubborn=[4201])
    t0 = lr.time.t
    check("reaped count", lr.rc_reap_orphans(lr.rc_folders()[0]), 2)
    check("stubborn one got TERM then KILL",
          [s for p, s in procs.sent if p == 4201], [signal.SIGTERM, signal.SIGKILL])
    check("polite one only got TERM",
          [s for p, s in procs.sent if p == 4202], [signal.SIGTERM])
    check("waited the grace period first", lr.time.t - t0 >= lr.REAP_GRACE, True)
    check("all gone", procs.rows, {})
    check("SIGKILL logged", "sending SIGKILL" in open(lr.LOG).read(), True)

    lr, procs = reap_scenario("20. reaping never blocks or breaks the launch", [
        (4301, 1, SDK % ("vanish", "vanish")),
    ])
    real = procs.kill
    def gone_already(pid, sig):
        procs.rows.pop(pid, None)            # exited between ps and kill
        return real(pid, sig)
    lr.os.kill = gone_already
    lr.rc_running = lambda _folder: False
    st = {lr.CONFIG: {"rc_watch": True}}
    check("vanished pid -> still launches", lr.rc_watchdog(st), True)
    check("vanished pid -> open still called", "open" in [c[0] for c in procs.calls], True)

    def denied(pid, sig):
        raise PermissionError(pid)
    lr, procs = reap_scenario("20b. kill not permitted", [(4401, 1, SDK % ("root", "root"))])
    lr.os.kill = denied
    lr.rc_running = lambda _folder: False
    check("EPERM -> still launches", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), True)
    check("EPERM logged", "could not signal pid 4401" in open(lr.LOG).read(), True)

    lr, procs = reap_scenario("20c. ps failing -> kill nothing", [(4501, 1, SDK % ("x", "x"))])
    lr.subprocess.run = fake_run(ps_output=OSError("no ps"))
    check("rc_orphans returns nothing when ps fails", lr.rc_orphans(lr.rc_folders()[0]), [])
    check("nothing signalled", procs.sent, [])

    lr, procs = reap_scenario("20d. throttled or stood down -> no reap either", [
        (4601, 1, SDK % ("y", "y"))])
    lr.rc_running = lambda _folder: False
    lr.rc_watchdog({lr.CONFIG: {"rc_watch": True, "rc_last_launch": lr.time.t}})
    check("throttled -> orphan left alone", procs.sent, [])
    lr.rc_watchdog({lr.CONFIG: {"rc_watch": True, "rc_tries": lr.RC_MAX_TRIES}})
    check("breaker tripped -> orphan left alone", procs.sent, [])


class VersionedProcs(FakeProcs):
    """FakeProcs whose processes have executables, as `lsof -d txt` reports."""
    def __init__(self, rows, exe, stubborn=()):
        FakeProcs.__init__(self, rows, stubborn)
        self.exe = exe

    def run(self, cmd, *a, **k):
        if cmd[0] != "lsof":
            return FakeProcs.run(self, cmd, *a, **k)
        self.calls.append(cmd)
        pid = int(cmd[cmd.index("-p") + 1])
        path = self.exe.get(pid) if pid in self.rows else None
        out = "p%d\nftxt\nn%s\nn/usr/lib/dyld\n" % (pid, path) if path else ""
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()


VERSIONS = "/Users/x/.local/share/claude/versions/"


def stale_scenario(name, kids=(), running="2.1.292", installed="2.1.300",
                   quiet=3600, stubborn=(), log=True):
    """A live server on `running` while `installed` is behind `claude`.

    `kids` are extra (pid, ppid, cmd) rows; `quiet` is how many seconds ago
    the server's debug log last saw session traffic."""
    root, lr, _calls, _opens = rc_scenario(name)
    debug = os.path.join(root, "rc-debug.log")
    if log:
        last = datetime.datetime.fromtimestamp(lr.time.time() - quiet, datetime.timezone.utc)
        stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)
        with open(debug, "w") as fh:
            fh.write("%s [DEBUG] [bridge:ws] sessionId=[REDACTED] <<< {}\n" % stamp(last))
            for i in range(1, 4):                     # heartbeats since: not traffic
                t = last + datetime.timedelta(seconds=i * quiet / 4.0)
                fh.write("%s [DEBUG] [bridge:api] POST .../heartbeat -> 200\n" % stamp(t))
    rows = [(1306, 896, SERVER + " --debug-file " + debug),
            (1767, 1306, SDK % ("idle1", "idle1")),
            (1975, 1306, SDK % ("idle2", "idle2"))] + list(kids)
    procs = VersionedProcs(rows, {1306: VERSIONS + running}, stubborn)
    lr.subprocess.run, lr.os.kill = procs.run, procs.kill
    lr.installed_version = lambda: installed
    lr.rc_running = lambda _folder: 1306 in procs.rows
    return lr, procs


def _rc_stale_scenarios():
    # ---- 32. an update leaves the server on its old binary -------------
    lr, procs = stale_scenario("32. stale server, sessions idle -> restarted on the new version")
    st = {lr.CONFIG: {"rc_watch": True}}
    check("watchdog launches", lr.rc_watchdog(st), True)
    check("old server got SIGTERM", procs.sent[:1], [(1306, signal.SIGTERM)])
    check("server is gone", 1306 in procs.rows, False)
    kinds = [c[0] for c in procs.calls]
    check("launcher opened after the stop", "open" in kinds, True)
    log = open(lr.LOG).read()
    check("restart logged with both versions",
          "restarting Remote Control on 2.1.300 (was 2.1.292)" in log, True)
    check("counts as a launch", st[lr.CONFIG].get("rc_tries"), 1)

    lr, procs = stale_scenario("32b. a session running a tool -> waits", kids=[
        (2001, 1767, "/bin/zsh -c source snapshot.sh && make test")])
    st = {lr.CONFIG: {"rc_watch": True}}
    check("does not launch", lr.rc_watchdog(st), False)
    check("nothing signalled", procs.sent, [])
    lr.rc_watchdog(st)
    log = open(lr.LOG).read()
    check("waiting logged once over two sweeps", log.count("once no session is mid-turn"), 1)
    del procs.rows[2001]                          # the tool finishes
    check("restarts once the session is idle", lr.rc_watchdog(st), True)
    check("wait marker cleared", "rc_stale_wait" in st[lr.CONFIG], False)

    lr, procs = stale_scenario("32b2. busy for RC_STALE_MAX -> restarted anyway", kids=[
        (2001, 1767, "/bin/zsh -c source snapshot.sh && sleep 99999")])
    st = {lr.CONFIG: {"rc_watch": True}}
    check("first sweep waits", lr.rc_watchdog(st), False)
    lr.time.t += lr.RC_STALE_MAX - 300
    check("still waits just before the cap", lr.rc_watchdog(st), False)
    check("nothing signalled yet", procs.sent, [])
    lr.time.t += 300
    check("restarts at the cap", lr.rc_watchdog(st), True)
    check("old server got SIGTERM", procs.sent[:1], [(1306, signal.SIGTERM)])
    log = open(lr.LOG).read()
    check("says it was forced", "still busy after 120 min" in log, True)
    check("timer cleared", "rc_stale_since" in st[lr.CONFIG], False)

    lr, procs = stale_scenario("32b3. versions match again -> wait timer resets", kids=[
        (2001, 1767, "/bin/zsh -c make")])
    st = {lr.CONFIG: {"rc_watch": True}}
    lr.rc_watchdog(st)
    check("timer started", "rc_stale_since" in st[lr.CONFIG], True)
    lr.installed_version = lambda: "2.1.292"       # e.g. rolled back
    lr.rc_watchdog(st)
    check("timer cleared", "rc_stale_since" in st[lr.CONFIG], False)

    lr, procs = stale_scenario("32c. caffeinate under a session -> waits", kids=[
        (2002, 1975, "caffeinate -i -t 300")])
    check("does not launch", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), False)
    check("nothing signalled", procs.sent, [])

    lr, procs = stale_scenario("32d. session traffic 2 min ago -> waits", quiet=120)
    check("does not launch", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), False)
    check("nothing signalled", procs.sent, [])

    lr, procs = stale_scenario("32e. debug log missing -> waits, never kills blind", log=False)
    check("does not launch", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), False)
    check("nothing signalled", procs.sent, [])

    lr, procs = stale_scenario("32f. server already on the installed version", installed="2.1.292")
    check("does not launch", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), False)
    check("nothing signalled", procs.sent, [])

    for label, inst in (("not a native install", None), ("lsof gives nothing", "2.1.300")):
        lr, procs = stale_scenario("32g. version unknown (%s) -> left alone" % label, installed=inst)
        if inst:
            procs.exe = {}
        check("does not launch", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), False)
        check("nothing signalled", procs.sent, [])

    lr, procs = stale_scenario("32h. stale server ignoring SIGTERM gets SIGKILL", stubborn=[1306])
    check("watchdog launches", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), True)
    check("SIGTERM then SIGKILL",
          [x for x in procs.sent if x[0] == 1306], [(1306, signal.SIGTERM), (1306, signal.SIGKILL)])

    print("\n32i. versions read from a native install's layout")
    root = tempfile.mkdtemp(prefix="lrtest-")
    vdir = os.path.join(root, "share", "claude", "versions")
    bindir = os.path.join(root, "vbin")
    os.makedirs(vdir)
    os.makedirs(bindir)
    open(os.path.join(vdir, "2.1.300"), "w").write("#!/bin/sh\n")
    os.chmod(os.path.join(vdir, "2.1.300"), 0o755)
    os.symlink(os.path.join(vdir, "2.1.300"), os.path.join(bindir, "claude"))
    lr = load_tool(root, os.path.join(root, "argv.txt"))
    saved = os.environ["PATH"]
    os.environ["PATH"] = bindir + os.pathsep + saved
    try:
        check("installed_version follows the symlink", lr.installed_version(), "2.1.300")
    finally:
        os.environ["PATH"] = saved
    check("version_of a non-versions path", lr.version_of("/opt/homebrew/bin/claude"), None)
    check("rc_server finds the debug log",
          lr.rc_server([(7, 1, SERVER + " --debug-file /tmp/x.log")], lr.rc_folders()[0]), (7, "/tmp/x.log"))


def hung_scenario(name, silent, **kw):
    """A server on the installed version whose debug log was last written
    `silent` seconds ago (fake clock)."""
    lr, procs = stale_scenario(name, installed="2.1.292", **kw)
    lr.notify = lambda msg: lr.log("ALERT: %s" % msg) or "log"
    debug = lr.rc_server(lr.ps_rows(), lr.rc_folders()[0])[1]
    age = lambda secs: os.utime(debug, (lr.time.t - secs,) * 2)
    age(silent)
    return lr, procs, age


def _rc_hung_scenarios():
    # ---- 33. running but frozen: 2026-10-27, silent 23 h ----------------
    lr, procs, age = hung_scenario("33. silent log -> restarted after a second look",
                                   16 * 60)
    st = {lr.CONFIG: {"rc_watch": True}}
    check("first sighting does not launch", lr.rc_watchdog(st), False)
    check("nothing signalled", procs.sent, [])
    lr.time.t += 300
    check("5 min later, not yet confirmed", lr.rc_watchdog(st), False)
    lr.time.t += 300
    check("confirmed -> watchdog launches", lr.rc_watchdog(st), True)
    check("frozen server got SIGTERM", procs.sent[:1], [(1306, signal.SIGTERM)])
    check("launcher opened after the stop", "open" in [c[0] for c in procs.calls], True)
    log = open(lr.LOG).read()
    check("warning logged once", log.count("has logged nothing for"), 1)
    check("alert says it froze", "ALERT: Remote Control froze" in log, True)
    check("marker cleared", "rc_hung_seen" in st[lr.CONFIG], False)
    check("counts as a launch", st[lr.CONFIG].get("rc_tries"), 1)

    lr, procs, age = hung_scenario("33b. log quiet 14 min -> healthy", 14 * 60)
    st = {lr.CONFIG: {"rc_watch": True}}
    check("does not launch", lr.rc_watchdog(st), False)
    check("no marker", "rc_hung_seen" in st[lr.CONFIG], False)

    lr, procs, age = hung_scenario("33c. woken VM: log moves before the second look", 3600)
    st = {lr.CONFIG: {"rc_watch": True}}
    lr.rc_watchdog(st)
    check("first sighting marked", "rc_hung_seen" in st[lr.CONFIG], True)
    lr.time.t += 700
    age(60)
    check("does not launch", lr.rc_watchdog(st), False)
    check("marker cleared", "rc_hung_seen" in st[lr.CONFIG], False)
    check("nothing signalled", procs.sent, [])

    lr, procs, age = hung_scenario("33d. network down -> waits, then restarts", 3600)
    st = {lr.CONFIG: {"rc_watch": True}}
    net = [False]
    lr.net_ok = lambda: net[0]
    lr.rc_watchdog(st)
    lr.time.t += 900
    check("outage -> does not launch", lr.rc_watchdog(st), False)
    check("outage -> nothing signalled", procs.sent, [])
    net[0] = True
    check("network back -> launches", lr.rc_watchdog(st), True)

    lr, procs, age = hung_scenario("33e. server replaced between looks -> new sighting", 3600)
    st = {lr.CONFIG: {"rc_watch": True}}
    lr.rc_watchdog(st)
    procs.rows[2306] = procs.rows.pop(1306)
    lr.rc_running = lambda _folder: 2306 in procs.rows
    lr.time.t += 900
    check("does not launch", lr.rc_watchdog(st), False)
    check("marker now names the new pid", st[lr.CONFIG]["rc_hung_seen"][0], 2306)

    lr, procs, age = hung_scenario("33f. frozen server ignoring SIGTERM gets SIGKILL", 3600,
                                   stubborn=[1306])
    st = {lr.CONFIG: {"rc_watch": True}}
    lr.rc_watchdog(st)
    lr.time.t += 600
    check("watchdog launches", lr.rc_watchdog(st), True)
    check("SIGTERM then SIGKILL",
          [x for x in procs.sent if x[0] == 1306], [(1306, signal.SIGTERM), (1306, signal.SIGKILL)])

    lr, procs = stale_scenario("33g. debug log missing -> never killed blind",
                               installed="2.1.292", log=False)
    st = {lr.CONFIG: {"rc_watch": True}}
    lr.rc_watchdog(st)
    lr.time.t += 3600
    check("does not launch", lr.rc_watchdog(st), False)
    check("nothing signalled", procs.sent, [])


def _rc_watchdog_scenarios():
    # ---- 10. what counts as "Remote Control is running" ------------------
    root, lr, calls, opens = rc_scenario("10. rc_running matches the real server only")
    cases = [
        ("/Users/x/.local/bin/claude remote-control --name auto-_obsidian", True),
        ("/Users/x/.local/bin/claude remote-control", True),
        ("claude rc", True),
        ("claude rc --debug-file /tmp/x.log", True),
        ("/bin/zsh /Users/x/.claude/tools/claude-rc.command", False),
        ("/bin/zsh -c claude rc", False),
        ("grep -qE ^(/[^ ]*/)?claude (rc|remote-control)", False),
        ("python3 limit-resume.py rc on", False),
        ("/Users/x/.local/bin/claude --resume abc -p go", False),
        ("claude rcx", False),
        ("claude remote-controller", False),
        ("", False),
    ]
    anywhere = lr.rc_folders()[0]               # no install.conf: any server counts
    for line, want in cases:
        lr.subprocess.run = fake_run(ps_output="    1     0 /sbin/launchd\n  100     1 " + line + "\n")
        check("ps line %r -> %s" % (line, want), lr.rc_running(anywhere), want)
    lr.subprocess.run = fake_run(ps_output=subprocess.TimeoutExpired("ps", 20))
    check("ps times out -> assume running (never spawn blind)", lr.rc_running(anywhere), True)
    lr.subprocess.run = fake_run(ps_output=OSError("no ps"))
    check("ps missing -> assume running", lr.rc_running(anywhere), True)

    # ---- 11. config edge cases -------------------------------------------
    root, lr, calls, opens = rc_scenario("11. watchdog config edge cases")
    lr.rc_running = lambda _folder: False
    st = {}
    check("no config at all -> no launch", lr.rc_watchdog(st), False)
    check("no config at all -> config created", lr.CONFIG in st, True)
    check("rc_watch false -> no launch",
          lr.rc_watchdog({lr.CONFIG: {"rc_watch": False}}), False)
    check("nothing opened while disabled", opens(), [])
    lr.rc_running = lambda _folder: True
    st = {lr.CONFIG: {"rc_watch": True, "rc_tries": 0}}
    lr.rc_watchdog(st)
    check("running with clean breaker -> state untouched",
          st[lr.CONFIG], {"rc_watch": True, "rc_tries": 0})

    # ---- 12. throttle boundary -------------------------------------------
    root, lr, calls, opens = rc_scenario("12. RC_MIN_INTERVAL boundary")
    lr.rc_running = lambda _folder: False
    st = {lr.CONFIG: {"rc_watch": True}}
    lr.rc_watchdog(st)
    t0 = lr.time.t
    check("first launch stamps rc_last_launch", st[lr.CONFIG]["rc_last_launch"], t0)
    lr.time.t = t0 + lr.RC_MIN_INTERVAL - 1
    check("1s before the interval -> throttled", lr.rc_watchdog(st), False)
    check("throttled attempt does not count as a try", st[lr.CONFIG]["rc_tries"], 1)
    lr.time.t = t0 + lr.RC_MIN_INTERVAL
    check("exactly at the interval -> launches", lr.rc_watchdog(st), True)
    check("second launch counted", st[lr.CONFIG]["rc_tries"], 2)
    check("launcher path passed to open", opens()[-1], ["open", lr.RC_LAUNCHER])

    # ---- 13. the launch itself failing ----------------------------------
    root, lr, calls, opens = rc_scenario("13. `open` raising is logged, not fatal")
    lr.rc_running = lambda _folder: False
    lr.subprocess.run = fake_run(open_raises=subprocess.TimeoutExpired("open", 30))
    st = {lr.CONFIG: {"rc_watch": True}}
    check("still reports an attempt", lr.rc_watchdog(st), True)
    check("failed launch still counts toward the breaker", st[lr.CONFIG]["rc_tries"], 1)
    check("failure logged", "failed to launch" in open(lr.LOG).read(), True)

    # ---- 14. wired into sweep, state persisted ---------------------------
    root, lr, calls, opens = rc_scenario("14. sweep runs the watchdog and persists it")
    lr.rc_running = lambda _folder: False
    lr.save_state({lr.CONFIG: {"rc_watch": True}})
    check("sweep returns 0", lr.cmd_sweep([]), 0)
    cfg = json.load(open(lr.STATE))[lr.CONFIG]
    check("sweep launched RC once", len(opens()), 1)
    check("rc_tries persisted", cfg.get("rc_tries"), 1)
    check("rc_last_launch persisted", cfg.get("rc_last_launch"), lr.time.t)
    check("launch logged with try count",
          "starting it (try 1/%d)" % lr.RC_MAX_TRIES in open(lr.LOG).read(), True)
    lr.cmd_sweep([])
    check("next sweep inside the interval does not relaunch", len(opens()), 1)
    open(lr.STATE, "w").write("{corrupt")
    check("corrupt state file -> sweep survives", lr.cmd_sweep([]), 0)

    # ---- 15. the `rc on|off` command -------------------------------------
    root, lr, calls, opens = rc_scenario("15. `rc on|off` toggles and resets the breaker")
    lr.rc_running = lambda _folder: False
    lr.save_state({lr.CONFIG: {"rc_watch": True, "rc_tries": lr.RC_MAX_TRIES,
                               "auto_all": True}})
    check("`rc` with no arg is a usage error", lr.cmd_rc([]), 2)
    check("`rc maybe` is a usage error", lr.cmd_rc(["maybe"]), 2)
    check("`rc on` returns 0", lr.cmd_rc(["on"]), 0)
    cfg = json.load(open(lr.STATE))[lr.CONFIG]
    check("`rc on` resets a tripped breaker", cfg["rc_tries"], 0)
    check("`rc on` keeps other config", cfg.get("auto_all"), True)
    lr.cmd_sweep([])
    check("after `rc on` the next sweep launches", len(opens()), 1)
    lr.cmd_rc(["off"])
    lr.time.t += 10 * lr.RC_MIN_INTERVAL
    lr.cmd_sweep([])
    check("after `rc off` sweeps never launch", len(opens()), 1)

    # ---- 15b. Linux: no Remote Control watch ------------------------------
    import io, contextlib
    root, lr, calls, opens = rc_scenario("15b. Linux: `rc on` refused, watchdog idle, doctor says so")
    lr.IS_MAC = False
    lr.rc_running = lambda _folder: False
    lr.save_state({lr.CONFIG: {"rc_watch": True}})         # left on by an older build
    with contextlib.redirect_stdout(io.StringIO()):
        code = lr.cmd_rc(["on"])
    check("`rc on` refused", code, 2)
    check("state untouched", json.load(open(lr.STATE))[lr.CONFIG].get("rc_tries"), None)
    lr.cmd_sweep([])
    check("an old rc_watch never launches", len(opens()), 0)
    lr.scheduler_status = lambda: ("systemd : timer is active", True, False)
    lr.save_state({lr.CONFIG: {"last_sweep_at": lr.time.time(), "last_sweep": "x",
                               "maint_last": lr.time.time(), "maint_first": lr.time.time()}})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_doctor([])
    out = buf.getvalue()
    check("doctor: Remote Control not used here", "remote ctl : not used here" in out, True)
    check("doctor: the scheduled task, named for `task-setup`",
          ("scheduled task: never ran" in out, "weekly timer" in out), (True, False))
    check("`rc off` still works", lr.cmd_rc(["off"]), 0)

    # ---- 16. replay of the 2026-09-27 network outage ---------------------
    # 22:38 DNS died; RC gave up at 22:49; launchd swept every 300s. Back then
    # each relaunch died on POST /v1/environments/bridge, three tries tripped
    # the breaker by 23:15 and nothing restarted RC until the 01:43 reboot.
    # Now an outage is waited out: no launch, no try spent, however long.
    root, lr, calls, opens = rc_scenario("16. outage replay: waits, then relaunches")
    net = [False]
    lr.subprocess.run = fake_run(calls=calls, net=net)
    lr.rc_running = lambda _folder: False
    lr.save_state({lr.CONFIG: {"rc_watch": True}})
    start, sweep = lr.time.t, 300
    for i in range(int(2.75 * 3600 / sweep)):          # 22:55 -> 01:40
        lr.time.t = start + i * sweep
        lr.cmd_sweep([])
    check("no launch while the API is unreachable", opens(), [])
    cfg = json.load(open(lr.STATE))[lr.CONFIG]
    check("no try spent on the outage", cfg.get("rc_tries", 0), 0)
    check("outage logged once, not every sweep",
          open(lr.LOG).read().count("waiting for the network"), 1)
    check("an outage is not an alert", "ALERT" in open(lr.LOG).read(), False)
    net[0] = True                                      # network returns
    lr.time.t += sweep
    lr.cmd_sweep([])
    check("network back -> launches on the next sweep", len(opens()), 1)
    check("recovery logged", "network is back" in open(lr.LOG).read(), True)
    check("that launch is try 1", json.load(open(lr.STATE))[lr.CONFIG]["rc_tries"], 1)
    lr.rc_running = lambda _folder: True
    lr.cmd_sweep([])
    lr.rc_running = lambda _folder: False                      # and it can drop again
    lr.time.t += lr.RC_MIN_INTERVAL
    lr.cmd_sweep([])
    check("after recovery the watchdog is armed again", len(opens()), 2)

    # ---- 16b. the network probe ------------------------------------------
    root, lr, calls, opens = rc_scenario("16b. net_ok: a broken probe never blocks")
    lr.subprocess.run = fake_run(net=[True])
    check("curl succeeds -> up", lr.net_ok(), True)
    lr.subprocess.run = fake_run(net=[False])
    check("curl fails -> down", lr.net_ok(), False)
    def no_curl(cmd, *a, **k):
        raise FileNotFoundError("curl")
    lr.subprocess.run = no_curl
    check("curl missing -> assume up", lr.net_ok(), True)
    def hung(cmd, *a, **k):
        raise subprocess.TimeoutExpired("curl", 13)
    lr.subprocess.run = hung
    check("curl hangs -> down", lr.net_ok(), False)

    # ---- 16c. replay of the 2026-10-07 lost login -------------------------
    # 02:07 the Login Item start failed on a token refresh; three blind
    # retries tripped the breaker by 02:30 and RC stayed down ~23 h, silently.
    # Now the launcher records `auth`, the watchdog alerts once and waits.
    root, lr, calls, opens = rc_scenario("16c. dead login: one alert, no retries")
    lr.rc_running = lambda _folder: False
    changed = lr.time.t - 3600                         # login last changed an hour ago
    lr.cred_mtime = lambda: changed
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "log"
    open(lr.RC_STATUS, "w").write("%d auth 1 13\n" % (lr.time.t - 5))
    lr.save_state({lr.CONFIG: {"rc_watch": True}})
    start = lr.time.t
    for i in range(24):                                # two hours of sweeps
        lr.time.t = start + i * 300
        lr.cmd_sweep([])
    check("never relaunched on a dead login", opens(), [])
    check("alerted exactly once", len(alerts), 1)
    check("alert says what to do", "claude /login" in alerts[0], True)
    check("no try spent", json.load(open(lr.STATE))[lr.CONFIG].get("rc_tries", 0), 0)
    lr.cred_mtime = lambda: lr.time.t                  # `claude /login` rewrote it
    lr.time.t += 300
    lr.cmd_sweep([])
    check("login renewed -> relaunches by itself", len(opens()), 1)
    check("alert marker cleared",
          "rc_auth_alerted" in json.load(open(lr.STATE))[lr.CONFIG], False)

    root, lr, calls, opens = rc_scenario("16d. dead login: `rc on` and other exits")
    lr.rc_running = lambda _folder: False
    lr.cred_mtime = lambda: 0
    lr.notify = lambda msg: "log"
    open(lr.RC_STATUS, "w").write("%d auth 1 13\n" % (lr.time.t - 5))
    lr.save_state({lr.CONFIG: {"rc_watch": True}})
    lr.cmd_sweep([])
    check("blocked before `rc on`", opens(), [])
    lr.time.t += 60
    lr.cmd_rc(["on"])
    lr.cmd_sweep([])
    check("`rc on` forgets the old failure and launches", len(opens()), 1)
    for text in ("%d exit 143 9000\n" % lr.time.t, "%d network 1 300\n" % lr.time.t,
                 "garbage", ""):
        root, lr, calls, opens = rc_scenario("16d. status %r is not a login failure" % text.strip())
        lr.rc_running = lambda _folder: False
        open(lr.RC_STATUS, "w").write(text)
        check("launches", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), True)

    # ---- 16e. breaker trip is no longer silent ---------------------------
    root, lr, calls, opens = rc_scenario("16e. breaker trip alerts once")
    lr.rc_running = lambda _folder: False
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "log"
    lr.save_state({lr.CONFIG: {"rc_watch": True}})
    start = lr.time.t
    for i in range(24):
        lr.time.t = start + i * 300
        lr.cmd_sweep([])
    check("exactly RC_MAX_TRIES launches", len(opens()), lr.RC_MAX_TRIES)
    check("one alert when it stands down", len(alerts), 1)
    check("still stood down afterwards",
          lr.rc_watchdog(json.load(open(lr.STATE))), False)
    lr.rc_running = lambda _folder: True
    lr.cmd_sweep([])
    cfg = json.load(open(lr.STATE))[lr.CONFIG]
    check("RC seen running -> breaker reset", cfg["rc_tries"], 0)
    check("RC seen running -> alert marker cleared", "rc_trip_alerted" in cfg, False)

    # ---- 16f. where alerts go --------------------------------------------
    root, lr, calls, opens = rc_scenario("16f. notify: Pushover with keys, screen without")
    posts = []
    lr.urllib.request.urlopen = lambda url, body, timeout=0: posts.append((url, body))
    check("no keys -> local notification", lr.notify("hello"), "local")
    check("no keys -> nothing posted", posts, [])
    check("no keys -> osascript used", [c[0] for c in calls][-1], "osascript")
    check("always logged", "ALERT: hello" in open(lr.LOG).read(), True)
    lr.subprocess.run = fake_run(calls=calls, keys={"token": "T0K", "user": "US3R"})
    check("keys in Keychain -> pushover", lr.notify("hello"), "pushover")
    check("posted to Pushover", posts[0][0], lr.PUSHOVER_URL)
    check("token and user sent", b"token=T0K" in posts[0][1] and b"user=US3R" in posts[0][1], True)
    check("secrets never logged", "T0K" in open(lr.LOG).read(), False)
    def refuse(url, body, timeout=0):
        raise OSError("no route")
    lr.urllib.request.urlopen = refuse
    check("Pushover failing -> asks for a retry, does not raise", lr.notify("hello"), "retry")

    check("mdat parsed from Keychain attributes",
          (lambda: (setattr(lr.subprocess, "run", lambda *a, **k: type("R", (), {
              "stdout": '    "mdat"<timedate>=0x3230  "20261008024650Z\\000"\n',
              "stderr": "", "returncode": 0})()), lr.cred_mtime())[1])(),
          datetime.datetime(2026, 10, 8, 2, 46, 50,
                            tzinfo=datetime.timezone.utc).timestamp())


    # ---- 16g. findings from the 2026-10-08 audit --------------------------
    root, lr, calls, opens = rc_scenario("16g. launcher gave up at its network gate -> try refunded")
    lr.rc_running = lambda _folder: False
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "log"
    lr.save_state({lr.CONFIG: {"rc_watch": True}})
    for i in range(8):                                 # flaky net: probe ok, gate not
        lr.cmd_sweep([])
        open(lr.RC_STATUS, "w").write("%d network 1 300\n" % (lr.time.t + 300))
        lr.time.t += lr.RC_MIN_INTERVAL
    check("keeps trying through a flaky network", len(opens()), 8)
    check("breaker never trips", alerts, [])
    check("tries stay low", json.load(open(lr.STATE))[lr.CONFIG]["rc_tries"] <= 1, True)
    open(lr.RC_STATUS, "w").write("%d exit 1 2\n" % lr.time.t)
    for i in range(6):                                 # real failed starts still count
        lr.cmd_sweep([])
        lr.time.t += lr.RC_MIN_INTERVAL
    check("genuine failures still trip the breaker", len(alerts), 1)

    root, lr, calls, opens = rc_scenario("16h. login fails on the last try, then is renewed")
    lr.rc_running = lambda _folder: False
    changed = [0]
    lr.cred_mtime = lambda: changed[0]
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "log"
    lr.save_state({lr.CONFIG: {"rc_watch": True, "rc_tries": lr.RC_MAX_TRIES,
                               "rc_last_launch": lr.time.t - 30}})
    open(lr.RC_STATUS, "w").write("%d auth 1 13\n" % (lr.time.t - 5))
    lr.cmd_sweep([])
    check("login alert, not the breaker alert", ["claude /login" in a for a in alerts], [True])
    changed[0] = lr.time.t + 60                        # `claude /login`
    lr.time.t += lr.RC_MIN_INTERVAL
    lr.cmd_sweep([])
    check("restarts by itself as the alert promised", len(opens()), 1)
    check("breaker restarted from one", json.load(open(lr.STATE))[lr.CONFIG]["rc_tries"], 1)
    check("no stand-down alert", len(alerts), 1)

    root, lr, calls, opens = rc_scenario("16i. a live launcher is not raced or counted")
    lr.rc_running = lambda _folder: False
    open(lr.RC_LOCK, "w").close()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR); "
         "fcntl.lockf(fd,fcntl.LOCK_EX); print('held',flush=True); time.sleep(30)",
         lr.RC_LOCK], stdout=subprocess.PIPE)
    try:
        holder.stdout.readline()
        check("lock held by another process -> busy", lr.launcher_busy(lr.rc_folders()[0]), True)
        st = {lr.CONFIG: {"rc_watch": True}}
        check("watchdog does not launch a second one", lr.rc_watchdog(st), False)
        check("and spends no try", st[lr.CONFIG].get("rc_tries", 0), 0)
    finally:
        holder.kill()
        holder.wait()
    check("holder gone -> lock is free again", lr.launcher_busy(lr.rc_folders()[0]), False)
    check("then it launches", lr.rc_watchdog({lr.CONFIG: {"rc_watch": True}}), True)
    os.remove(lr.RC_LOCK)
    os.makedirs(lr.RC_LOCK)
    check("an old directory lock does not count as busy", lr.launcher_busy(lr.rc_folders()[0]), False)

    root, lr, calls, opens = rc_scenario("16j. `rc on` lifts the throttle; failed alerts retry")
    lr.rc_running = lambda _folder: False
    lr.save_state({lr.CONFIG: {"rc_watch": True, "rc_tries": lr.RC_MAX_TRIES,
                               "rc_last_launch": lr.time.t - 5}})
    lr.cmd_rc(["on"])
    lr.cmd_sweep([])
    check("launches on the very next sweep", len(opens()), 1)
    results = ["retry", "retry", "pushover"]
    sent = []
    lr.notify = lambda msg: sent.append(msg) or results[len(sent) - 1]
    st = {lr.CONFIG: {"rc_watch": True, "rc_tries": lr.RC_MAX_TRIES}}
    for _ in range(5):
        lr.rc_watchdog(st)
    check("undelivered alert is re-sent until it lands, then stops", len(sent), 3)
    posts = []
    lr.urllib.request.urlopen = lambda url, body, timeout=0: (_ for _ in ()).throw(OSError("x"))
    lr.subprocess.run = fake_run(calls=calls, keys={"token": "T", "user": "U"})
    root2, lr2, _c, _o = rc_scenario("16j. notify reports retry when Pushover is set up but fails")
    lr2.subprocess.run = fake_run(keys={"token": "T", "user": "U"})
    lr2.urllib.request.urlopen = lambda url, body, timeout=0: (_ for _ in ()).throw(OSError("x"))
    check("keys present, post fails -> retry", lr2.notify("hello"), "retry")


LAUNCHER_SRC = os.path.join(os.path.dirname(HERE), "claude-rc.command.in")


def lock_free(path):
    if not os.path.isfile(path):
        return not os.path.exists(path)
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def launcher_run(name, claude="exit 0", ps="", curl="exit 0", lock_pid=None,
                 hold_lock=False, send=None, env_extra=None, lsof="", tag="", hold=None):
    """Run the real launcher in a throwaway HOME with fake claude/ps/curl/lsof.
    ps: the `ps -Ao pid=,command=` table; lsof: what lsof prints (every pid's
    cwd is asked one at a time, so {root} stands for the launcher's folder);
    hold: a file under tools/ another process keeps locked."""
    print("\n%s" % name)
    root = tempfile.mkdtemp(prefix="lrlaunch-")
    bindir = os.path.join(root, "bin")
    tools = os.path.join(root, ".claude", "tools")
    os.makedirs(bindir)
    os.makedirs(tools)
    bins = {
        "claude": 'echo "$*" >> "%s/claude.argv"\n' % root
                  + 'if [ "$1" = auth ]; then %s; fi\n' % os.environ.get("_AUTH", "exit 0")
                  + 'for a in "$@"; do prev="$cur"; cur="$a"; '
                    '[ "$prev" = --debug-file ] && DEBUG="$a"; done\n' + claude + "\n",
        "ps": "cat <<'OUT'\n" + ps + "\nOUT\n",
        "curl": 'echo x >> "%s/curl.calls"\n' % root + curl + "\n",
        "lsof": 'echo "$*" >> "%s/lsof.argv"\n' % root
                + "cat <<'OUT'\n" + lsof.replace("{root}", os.path.realpath(root)) + "\nOUT\n",
        "python": 'echo "$*" >> "%s/python.argv"\n' % root,
    }
    for exe, body in bins.items():
        path = os.path.join(bindir, exe)
        open(path, "w").write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
    text = open(LAUNCHER_SRC).read()
    for key, val in (("@RC_DIR@", root), ("@CLAUDE_BIN@", os.path.join(bindir, "claude")),
                     ("@PYTHON@", os.path.join(bindir, "python")), ("@RC_NAME@", "auto-test"),
                     ("@RC_TAG@", tag)):
        text = text.replace(key, val)
    launcher = os.path.join(root, "claude-rc.command")
    open(launcher, "w").write(text)
    os.chmod(launcher, 0o755)
    if lock_pid is not None:
        os.makedirs(os.path.join(tools, "rc-launch.lock"))
        open(os.path.join(tools, "rc-launch.lock", "pid"), "w").write(str(lock_pid))
    env = dict(os.environ, HOME=root, PATH=bindir + ":/usr/bin:/bin",
               RC_PAUSE="0", RC_GATE_SLEEP="0", RC_GATE_OK_SLEEP="0", RC_GATE_TRIES="6",
               **(env_extra or {}))
    read = lambda p: open(p).read() if os.path.exists(p) else ""
    lock = os.path.join(tools, "rc-launch.lock")
    holder = None
    if hold:
        lock_of_hold, lock = lock, os.path.join(tools, hold)
    if hold_lock or hold:                      # another launcher (or loop) is alive
        open(lock, "w").close()
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR); "
             "fcntl.lockf(fd,fcntl.LOCK_EX); print('held',flush=True); time.sleep(60)",
             lock], stdout=subprocess.PIPE)
        holder.stdout.readline()
    if hold:
        lock = lock_of_hold
    child = subprocess.Popen(["/bin/zsh", launcher], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    if send:                                   # (signal, whole group?) once the server is up
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=20)
        while "remote-control" not in read(os.path.join(root, "claude.argv")):
            if datetime.datetime.now() > deadline:
                break
            signal.sigtimedwait([signal.SIGUSR2], 0.05) if hasattr(signal, "sigtimedwait") \
                else subprocess.call(["sleep", "0.05"])
        subprocess.call(["sleep", "0.3"])
        (os.killpg if send[1] else os.kill)(child.pid, send[0])
    out, _err = child.communicate(timeout=60)
    proc = type("R", (), {"returncode": child.returncode, "stdout": out})()
    if holder:
        holder.kill()
        holder.wait()
    status = read(os.path.join(tools, "rc-last-exit" + tag)).split()
    argv = [l for l in read(os.path.join(root, "claude.argv")).splitlines() if l]
    return {"code": proc.returncode, "out": proc.stdout, "status": status,
            "started": [a for a in argv if a.startswith("remote-control")],
            "argv": argv, "curls": len(read(os.path.join(root, "curl.calls")).split()),
            "lock": not lock_free(lock), "lsof": read(os.path.join(root, "lsof.argv")),
            "tools": tools,
            "python": [l for l in read(os.path.join(root, "python.argv")).splitlines() if l],
            "pyargv": os.path.join(root, "python.argv")}


def launcher_scenarios():
    if not os.path.exists(LAUNCHER_SRC) or not os.path.exists("/bin/zsh"):
        print("\n(launcher template or zsh not available - launcher tests skipped)")
        return
    r = launcher_run("21. launcher: clean start and stop")
    check("server started once", len(r["started"]), 1)
    check("named, in auto mode, and given a debug file",
          "--name auto-test --permission-mode auto --debug-file" in r["started"][0], True)
    check("three good probes before starting", r["curls"], 3)
    check("status records a plain exit", r["status"][1:3], ["exit", "0"])
    check("lock released", r["lock"], False)

    r = launcher_run("22. launcher: this folder's server already running -> stands down",
                     ps="  1047 /Users/x/.local/bin/claude remote-control --name auto-test "
                        "--debug-file /x/rc.log")
    check("nothing started", r["argv"], [])
    check("network not even probed", r["curls"], 0)
    check("no status written", r["status"], [])
    check("says this folder is served, by which pid",
          "Remote Control already serves" in r["out"] and "(pid 1047)" in r["out"], True)
    check("named: no lsof needed", r["lsof"], "")

    r = launcher_run("22b. launcher: another folder's server -> starts this one",
                     ps="  1047 /Users/x/.local/bin/claude remote-control --name auto-_obsidian\n"
                        "  1048 /Users/x/.local/bin/claude remote-control --name auto-tester",
                     lsof="p1047\nfcwd\nn/Users/x/_obsidian")
    check("started", len(r["started"]), 1)
    check("asked lsof about the unnamed-for-here servers", r["lsof"].count("-d cwd"), 2)

    r = launcher_run("22c. launcher: a server started by hand in this folder -> stands down",
                     ps="  2001 claude rc", lsof="p2001\nfcwd\nn{root}")
    check("nothing started (no 409 loop)", (r["argv"], r["curls"]), ([], 0))
    check("names the hand-started pid", "(pid 2001)" in r["out"], True)

    r = launcher_run("22d. launcher: second folder's files carry its tag", tag="-test")
    check("its own status file", r["status"][1:3], ["exit", "0"])
    check("its own lock", os.path.exists(os.path.join(r["tools"], "rc-launch-test.lock")), True)
    check("first folder's files untouched",
          [f for f in os.listdir(r["tools"]) if f in ("rc-last-exit", "rc-launch.lock")], [])
    check("debug log tagged", "rc-debug/rc-test-" in r["started"][0], True)

    r = launcher_run("23. launcher: network down -> never touches the login",
                     curl="exit 28")
    check("claude never invoked at all", r["argv"], [])
    check("gave up after the configured probes", r["curls"], 6)
    check("status says network", r["status"][1], "network")
    check("exits non-zero", r["code"], 1)
    check("lock released", r["lock"], False)

    flaky = ('n=$(wc -l < "$(dirname "$0")/../curl.calls"); '
             '[ "$n" -eq 3 ] && exit 28; exit 0')
    r = launcher_run("24. launcher: one bad probe restarts the count", curl=flaky)
    check("needs three good probes in a row", r["curls"], 6)
    check("then starts", len(r["started"]), 1)

    os.environ["_AUTH"] = "echo '{\"loggedIn\": false}'; exit 1"
    try:
        r = launcher_run("25. launcher: logged out -> reports auth, does not start")
    finally:
        del os.environ["_AUTH"]
    check("server not started", r["started"], [])
    check("status says auth", r["status"][1], "auth")
    check("tells the user what to run", "claude /login" in r["out"], True)

    r = launcher_run("26. launcher: token refresh fails at start (2026-10-07)",
                     claude='echo "2026-10-06T19:07:56.868Z [DEBUG] [bridge:api] Registration: Token refresh failed" '
                            '>> "$DEBUG"; exit 1')
    check("status says auth", r["status"][1:3], ["auth", "1"])
    check("exit code passed through", r["code"], 1)

    r = launcher_run("27. launcher: server dies for another reason", claude="exit 143")
    check("status says exit with the code", r["status"][1:3], ["exit", "143"])

    r = launcher_run("28. launcher: directory lock left by the first 2026-10-08 build",
                     lock_pid=1)
    check("replaces it and starts", len(r["started"]), 1)
    check("lock released afterwards", r["lock"], False)

    r = launcher_run("29. launcher: another launcher is alive -> stands down",
                     hold_lock=True)
    check("says so", "Another launcher" in r["out"], True)
    check("claude never invoked", r["argv"], [])
    check("no status written", r["status"], [])

    r = launcher_run("30. launcher: session chatter is not a login failure",
                     claude='echo "2026-10-08T07:34:23.370Z [DEBUG] [bridge:ws] sessionId=x <<< '
                            '{\\"text\\":\\"Registration: Token refresh failed\\"}" >> "$DEBUG"; '
                            'echo "2026-10-08T07:35:00.000Z [DEBUG] [bridge:activity] grep '
                            'Token refresh failed" >> "$DEBUG"; exit 143')
    check("status stays a plain exit", r["status"][1:3], ["exit", "143"])

    r = launcher_run("30b. launcher: the genuine timestamped line is a login failure",
                     claude='echo "2026-10-06T19:07:56.868Z [DEBUG] [bridge:api] Registration: '
                            'Token refresh failed" >> "$DEBUG"; exit 1')
    check("status says auth", r["status"][1], "auth")

    for label, sig, group in (("Ctrl-C", signal.SIGINT, True),
                              ("window closed", signal.SIGHUP, True)):
        r = launcher_run("31. launcher: %s still leaves a status" % label,
                         claude="sleep 20", send=(sig, group))
        check("status written", r["status"][1:2], ["exit"])
        check("exit code is the signal's", int(r["status"][2]) > 128, True)
        check("lock released", r["lock"], False)

    r = launcher_run("31b. launcher: asks `deadman` about the sweep while the server runs",
                     claude="sleep 3", env_extra={"RC_DEADMAN_EVERY": "1"})
    check("deadman run at least once", len(r["python"]) >= 1, True)
    check("with the installed tool", all(a.endswith(".claude/tools/limit-resume.py deadman")
                                         for a in r["python"]), True)
    seen = len(r["python"])
    subprocess.call(["sleep", "2.5"])
    after = len([l for l in open(r["pyargv"]).read().splitlines() if l])
    check("the loop ends with the launcher", after, seen)

    r = launcher_run("31c. launcher: deadman waits its interval", claude="exit 0",
                     env_extra={"RC_DEADMAN_EVERY": "600"})
    check("a short server run asks nothing", r["python"], [])

    r = launcher_run("31d. launcher: another folder's launcher runs the dead-man check",
                     claude="sleep 3", env_extra={"RC_DEADMAN_EVERY": "1"},
                     hold="rc-deadman.lock")
    check("once per machine: this one asks nothing while the lock is held", r["python"], [])
    check("the server still ran", len(r["started"]), 1)


# ---- install.sh / uninstall.sh / doctor / deadman (audit batch 1) -----------

def deadman_scenarios():
    import io, contextlib
    root, _argv, lr = scenario("40. deadman: one alert per stall, from outside launchd")
    stale = (datetime.datetime.now() - datetime.timedelta(minutes=40)).isoformat(timespec="seconds")
    fresh = (datetime.datetime.now() - datetime.timedelta(minutes=2)).isoformat(timespec="seconds")
    step = lr.deadman_step
    check("fresh sweep -> quiet, mark cleared", step(fresh, 120, False, "x"), (None, ""))
    check("stale but a sweep is running -> quiet", step(stale, 2400, True, stale), (None, ""))
    check("first stale sighting -> remembered, no alert", step(stale, 2400, False, ""),
          (None, stale))
    alert, mark = step(stale, 2400, False, stale)
    check("second sighting -> alert", "has not run since %s (40 min)" % stale in (alert or ""), True)
    check("alert names the kickstart", lr.kickstart_hint() in (alert or ""), True)
    check("marked alerted", mark, stale + " alerted")
    check("same stall -> no second alert", step(stale, 3000, False, mark), (None, mark))
    alert, _ = step("never", None, False, "never")
    check("never swept -> alert says since install", "since install" in (alert or ""), True)

    sent = []
    lr.notify = lambda msg: sent.append(msg) or "local"
    lr.ps_rows = lambda: []
    lr.save_state({lr.CONFIG: {"last_sweep": stale}})
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(4):
            lr.cmd_deadman([])
    check("four checks of one stall -> one alert", len(sent), 1)
    lr.save_state({lr.CONFIG: {"last_sweep": fresh}})
    with contextlib.redirect_stdout(io.StringIO()):
        lr.cmd_deadman([])
    check("a fresh sweep clears the mark", open(lr.DEADMAN).read().strip(), "")
    lr.save_state({lr.CONFIG: {"last_sweep": stale}})
    with contextlib.redirect_stdout(io.StringIO()):
        lr.cmd_deadman([])
        lr.cmd_deadman([])
    check("the next stall alerts again", len(sent), 2)

    sent[:] = []
    open(lr.DEADMAN, "w").write("")
    lr.ps_rows = lambda: [(4242, 1, "/opt/homebrew/bin/python3 /h/.claude/tools/limit-resume.py sweep")]
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(3):
            lr.cmd_deadman([])
    check("a long sweep (resume inside) is not a stall", sent, [])

    lr.ps_rows = lambda: []
    lr.notify = lambda msg: sent.append(msg) or "retry"
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(3):
            lr.cmd_deadman([])
    check("Pushover failed -> alert again at the next check", len(sent), 2)

    print("\n41. reload-check: what a scheduler reload would kill")
    rows = [(1, 0, "/sbin/launchd"),
            (50, 1, "/opt/homebrew/bin/python3 /h/.claude/tools/limit-resume.py sweep"),
            (60, 1, "/opt/homebrew/Cellar/python/Python /h/.claude/tools/limit-resume.py jobpty "
                    "/h/.claude/tools/jobs/s1.pty.log -- claude --remote-control"),
            (70, 1, "grep limit-resume.py sweep"),
            (80, 1, "vim limit-resume.py"),
            (100, 50, "/bin/sh ./install.sh")]
    state = {lr.JOBS_KEY: {"running": {"pid": 60, "title": "Nightly scan"}}}
    found = lr.inflight(state, rows, {}, 999)
    check("sweep and job found, grep and editor ignored", len(found), 2)
    check("job named from the state", any('"Nightly scan", pid 60' in l for l in found), True)
    found = lr.inflight({}, rows, {}, 100)
    check("installer inside the sweep -> said so",
          any(l.startswith("a sweep") and "runs inside it" in l for l in found), True)
    check("LIMIT_RESUME_JOB set -> in flight",
          len(lr.inflight({}, [], {"LIMIT_RESUME_JOB": "1"}, 999)), 1)
    check("ps failed -> cannot tell, counts as in flight", len(lr.inflight({}, None, {}, 1)), 1)
    check("nothing running -> empty", lr.inflight({}, rows[:1] + rows[3:5], {}, 999), [])
    with contextlib.redirect_stdout(io.StringIO()):
        lr.ps_rows = lambda: rows[:1]
        check("reload-check exits 0 when idle", lr.cmd_reload_check([]), 0)
        lr.ps_rows = lambda: rows
        check("reload-check exits 1 when busy", lr.cmd_reload_check([]), 1)

    print("\n42. install.conf, atomic state writes")
    open(lr.INSTALL_CONF, "w").write("rc_dir=/a\nlabel=x.y\nrc_dir=/b\nbroken line\n")
    check("first rc_dir wins", lr.install_setting("rc_dir", None), "/a")
    check("label read", lr.install_setting("label", None), "x.y")
    check("missing key -> default", lr.install_setting("nope", "d"), "d")
    os.unlink(lr.INSTALL_CONF)
    check("no file -> default", lr.install_setting("label", "d"), "d")
    temps = lambda: [f for f in os.listdir(os.path.dirname(lr.STATE)) if ".tmp." in f]
    lr.save_state({"a": 1})
    check("state saved", lr.load_state(), {"a": 1})
    check("no temp file left beside it", temps(), [])
    try:
        lr.save_state({"bad": object()})
    except TypeError:
        pass
    check("a failed write keeps the old state", lr.load_state(), {"a": 1})
    check("and leaves no temp file", temps(), [])

    if sys.platform != "darwin":
        print("\n(not macOS - launchd scheduler_status tests skipped)")
        return
    print("\n43. doctor: a held agent is not healthy")
    lr.HOME = root
    os.makedirs(os.path.join(root, "Library", "LaunchAgents"))
    open(os.path.join(root, "Library", "LaunchAgents", lr.LABEL + ".plist"), "w").write("x")
    info = ("\tstate = not running\n\tprogram = %s\n\truns = 2\n\tlast exit code = 0\n"
            % sys.executable)
    lr.launchd_print = lambda: info + "\tpended nondemand spawn = speculative\n"
    desc, ok, pended = lr.scheduler_status()
    check("pended seen", (ok, pended, "pended (speculative)" in desc), (True, True, True))
    lr.launchd_print = lambda: info
    check("not pended", lr.scheduler_status()[2], False)
    lr.launchd_print = lambda: info.replace(sys.executable, "/gone/python3")
    desc, ok, _ = lr.scheduler_status()
    check("python gone -> not ok, says so", (ok, "MISSING" in desc), (False, True))
    lr.launchd_print = lambda: ""
    check("not loaded", lr.scheduler_status()[:2], ("launchd : NOT LOADED - run install.sh", False))

    def doctor(beat, pended):
        lr.scheduler_status = lambda: ("launchd : loaded", True, pended)
        lr.save_state({lr.CONFIG: {"last_sweep": beat}} if beat else {})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = lr.cmd_doctor([])
        return code, buf.getvalue()
    code, out = doctor(stale, True)
    check("held + stale -> NEEDS ATTENTION", (code, "NEEDS ATTENTION" in out), (1, True))
    check("says launchd holds it", "held       : launchd is holding the agent" in out, True)
    check("gives the kickstart", lr.kickstart_hint() in out, True)
    check("no longer says re-run install.sh", "re-run install.sh" in out, False)
    code, out = doctor(fresh, True)
    check("pended but sweeping -> not held, sweep OK", ("held" in out, "(2m ago) OK" in out),
          (False, True))
    check("and healthy", code, 0)
    code, out = doctor(None, True)
    check("never swept + pended -> held", ("held       :" in out, code), (True, 1))


# ---- sweep, resume, Remote Control watchdog (audit batch 2) -----------------

def append_records(path, recs):
    with open(path, "a") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def quota_rec(uuid, reset_delta=-600, window="five_hour"):
    import time as _t
    return {"type": "assistant", "error": "rate_limit", "apiErrorStatus": 429, "uuid": uuid,
            "quotaLimits": {"status": "rejected", "resetsAt": int(_t.time() + reset_delta),
                            "rateLimitType": window}}


def b2_scenario(name):
    root, argv_log, lr = scenario(name)
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "pushover"
    lr.ps_rows = lambda: []
    return root, argv_log, lr, alerts


def batch2_scenarios():
    import io, contextlib, time as _t
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())

    # ---- 60. B2-H2: one state lock for the sweep and every writing command --
    root, argv_log, lr, alerts = b2_scenario("60. state lock: a command waits, or gives up unchanged")
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    held = os.open(lr.STATE + ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)
    lr.STATE_LOCK_WAIT = 1
    with quiet():
        check("disarm while locked -> gives up", lr.cmd_disarm(["all"]), 1)
        check("sweep while locked -> gives up", lr.cmd_sweep([]), 1)
    check("nothing changed", lr.load_state(), {lr.CONFIG: {"auto_all": True}})
    check("says nothing changed", "nothing changed - try again" in open(lr.LOG).read(), True)
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".claude", "tools"))
    hstate = os.path.join(home, ".claude", "tools", "limit-resume.state.json")
    with open(hstate, "w") as fh:
        json.dump({lr.CONFIG: {"auto_all": True}}, fh)
    hfd = os.open(hstate + ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(hfd, fcntl.LOCK_EX)              # "a sweep" holds the lock
    proc = subprocess.Popen([sys.executable, "-I", "-B", TOOL, "auto", "off"],
                            env=dict(os.environ, HOME=home), stdout=subprocess.DEVNULL)
    _t.sleep(1.5)
    check("command waits while the sweep runs", proc.poll(), None)
    with open(hstate, "w") as fh:                # the sweep saves its own change
        json.dump({lr.CONFIG: {"auto_all": True, "last_sweep": "x"}}, fh)
    os.close(hfd)                                # sweep ends
    check("then runs", proc.wait(20), 0)
    cfg = json.load(open(hstate))[lr.CONFIG]
    check("both changes kept (no stale write-back)",
          (cfg.get("auto_all"), cfg.get("last_sweep")), (False, "x"))
    os.close(held)
    with quiet():
        check("lock free -> disarm runs", lr.cmd_disarm(["all"]), 0)

    # ---- 61. B2-H1 / B2-L3: detached resume, timeout stops its whole group --
    root, argv_log, lr, alerts = b2_scenario("61. a long resume runs detached; timeout stops it all")
    bindir = os.path.join(root, "bin")
    kid = os.path.join(root, "kid.pid")
    with open(os.path.join(bindir, "claude"), "w") as fh:
        fh.write("#!/bin/sh\nsleep 60 &\necho $! > %s\nwait\n" % kid)
    write_transcript(root, "sess-long", "quota")
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    t0 = _t.time()
    lr.cmd_sweep([])
    check("sweep returns at once", _t.time() - t0 < 5, True)
    wait_for(lambda: os.path.exists(kid) and open(kid).read().strip())
    cfg = lr.load_state()[lr.CONFIG]
    check("active task recorded with its pid",
          ((cfg.get("active_resume") or {}).get("how"), bool((cfg.get("active_resume") or {}).get("pid"))),
          ("headless", True))
    check("counts as busy (no job starts)", lr.resume_busy(cfg, int(_t.time())), True)
    check("reload-check sees it", any("headless resume of session sess-long" in l
                                      for l in lr.inflight(lr.load_state(), [], {}, 1)), True)
    lr.cmd_sweep([])
    check("still running on the next sweep", (lr.load_state()[lr.CONFIG].get("active_resume")
                                              or {}).get("sid"), "sess-long")
    check("heartbeat still written", bool(lr.load_state()[lr.CONFIG].get("last_sweep_at")), True)
    lr.RESUME_TIMEOUT = 0
    lr.RC_STOP_GRACE = 3
    _t.sleep(1.1)                                # the sweep's clock is whole seconds
    lr.cmd_sweep([])
    grandchild = int(open(kid).read().strip())
    check("timeout: logged", "resume ran past 0s, stopping it" in open(lr.LOG).read(), True)
    check("timeout: active task cleared", lr.load_state()[lr.CONFIG].get("active_resume"), None)
    check("timeout: its tool processes stopped too",
          wait_for(lambda: not lr.pid_alive(grandchild) or
                   subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)],
                                  capture_output=True, text=True).stdout.strip().startswith("Z")), True)
    settle(lr)

    proc_kid = os.path.join(root, "relay-kid.pid")
    try:
        lr.run_group(["/bin/sh", "-c", "sleep 60 & echo $! > %s; wait" % proc_kid], 1, root)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
    relay_kid = int(open(proc_kid).read().strip())
    check("relay timeout raises", timed_out, True)
    check("relay timeout kills its group", wait_for(lambda: not lr.pid_alive(relay_kid)), True)

    # ---- 62. B2-M1: a bad transcript is skipped; a crash is recorded --------
    root, argv_log, lr, alerts = b2_scenario("62. bad transcripts skipped, crashes alert once")
    bad = write_transcript(root, "sess-bad", "quota")
    append_records(bad, [])
    with open(bad, "a") as fh:
        fh.write("[1, 2]\n\"text\"\n")
        rec = quota_rec("q2")
        rec["quotaLimits"] = "rejected"
        rec["timestamp"] = 17
        rec.pop("type")                 # neither a stop nor a turn
        fh.write(json.dumps(rec) + "\n")
    check("non-object lines and a bad quotaLimits do not raise",
          lr.find_block(bad)["uuid"], "stop-sess-bad")
    write_transcript(root, "sess-good", "quota")
    real_find = lr.find_block
    lr.find_block = lambda path: (_ for _ in ()).throw(FileNotFoundError("gone")) \
        if "sess-bad" in path else real_find(path)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 2)
    log = open(lr.LOG).read()
    check("the good session is still resumed", any("--resume sess-good" in l for l in launches(argv_log)), True)
    check("the bad one logged once", log.count("skipping transcript of sess-bad"), 1)
    lr.find_block = real_find
    real_due = lr.due_resumes
    lr.due_resumes = lambda *a: 1 / 0
    with quiet():
        check("crash -> sweep exits 1", lr.cmd_sweep([]), 1)
        lr.cmd_sweep([])
    cfg = lr.load_state()[lr.CONFIG]
    check("crash recorded", "ZeroDivisionError" in (cfg.get("last_crash") or {}).get("what", ""), True)
    check("one alert for a run of crashes", sum("sweep crashed" in a for a in alerts), 1)
    lr.scheduler_status = lambda: ("launchd : loaded", True, False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lr.cmd_doctor([])
    check("doctor: last crash, NEEDS ATTENTION", ("last crash :" in buf.getvalue(), code), (True, 1))
    lr.due_resumes = real_due
    with quiet():
        check("clean sweep -> 0", lr.cmd_sweep([]), 0)
    check("crash mark cleared", lr.load_state()[lr.CONFIG].get("last_crash"), None)
    lr.rc_watchdog = lambda state: 1 / 0
    with quiet():
        check("watchdog crash does not stop the sweep", lr.cmd_sweep([]), 0)
    check("watchdog crash logged", "rc watchdog: CRASH" in open(lr.LOG).read(), True)

    # ---- 63. B2-M2: auto mode's own entries, disarm keeps what was handled --
    root, argv_log, lr, alerts = b2_scenario("63. auto entries ignored when auto is off; disarm")
    write_transcript(root, "sess-a", "quota")
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 2)
    st = lr.load_state()
    check("auto resume tagged origin auto", st["sess-a"].get("origin"), "auto")
    path = lr.session_file("sess-a")
    append_records(path, [{"type": "user", "message": {"role": "user", "content": "more"}},
                          quota_rec("q-a2")])
    st["sess-a"].pop("done", None)
    st[lr.CONFIG]["auto_all"] = False
    lr.save_state(st)
    check("auto off -> its entry is not a candidate", lr.candidates(lr.load_state()), [])
    st[lr.CONFIG]["auto_all"] = True
    lr.save_state(st)
    check("auto on -> it is", [c[0] for c in lr.candidates(lr.load_state())], ["sess-a"])
    old = quota_rec("q-old", reset_delta=-20 * 3600)
    append_records(path, [{"type": "user", "message": {"role": "user", "content": "x"}}, old])
    check("its age guard still applies", lr.candidates(lr.load_state()), [])
    st[lr.CONFIG]["active_resume"] = {"how": "relay", "sid": "z", "at": 1}
    st[lr.CONFIG]["rc_watch"] = True
    st["sess-x"] = {"prompt": "p", "resumes": 2, "handled": ["h1"]}
    lr.save_state(st)
    with quiet():
        lr.cmd_disarm(["all"])
    st = lr.load_state()
    check("disarm all: auto and rc watch off, heartbeat and task kept",
          (st[lr.CONFIG]["auto_all"], st[lr.CONFIG]["rc_watch"],
           bool(st[lr.CONFIG].get("last_sweep")), bool(st[lr.CONFIG].get("active_resume"))),
          (False, False, True, True))
    check("disarm all: entries become auto entries, handled kept",
          (st["sess-x"]["origin"], st["sess-x"]["handled"]), ("auto", ["h1"]))
    with quiet():
        lr.cmd_disarm(["sess-x"])
    check("disarm <sid>: retired, handled kept",
          {k: lr.load_state()["sess-x"].get(k) for k in ("done", "why", "handled")},
          {"done": True, "why": "disarmed", "handled": ["h1"]})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_status([])
    check("status lists auto entries apart",
          ("resumed by auto mode (ignored while it is off):" in buf.getvalue(),
           "done (disarmed)" in buf.getvalue()), (True, True))

    # ---- 64. B2-M3: the age guard counts from the reset ----------------------
    root, argv_log, lr, alerts = b2_scenario("64. age from the reset; a stall is reported")
    write_transcript(root, "sess-week", "quota", reset_delta=-600, age_hours=72)
    write_transcript(root, "sess-stall", "quota", reset_delta=-20 * 3600, age_hours=25)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 2)
    got = " ".join(launches(argv_log))
    check("weekly stop 72 h old, reset 10 min ago -> resumed", "--resume sess-week" in got, True)
    check("reset 20 h ago -> not resumed", "--resume sess-stall" in got, False)
    check("one alert naming it", [("sess-stall" in a, "age guard" in a) for a in alerts],
          [(True, True)])
    check("skip logged", "sess-stall: not resumed - its limit reset 20 h ago" in open(lr.LOG).read(), True)

    # ---- 65. B2-M4: retirement is not for life ------------------------------
    root, argv_log, lr, alerts = b2_scenario("65. MAX_RESUMES alerts; typing in it revives it")
    check("DONE must be the whole last line",
          [lr.said_done(t) for t in ("x\nDONE", "UNDONE", "it is NOT DONE", "**DONE**", "x\nDONE.\n")],
          [True, False, False, True, True])
    path = write_transcript(root, "sess-m", "quota")
    recs = [json.loads(l) for l in open(path)]
    prev = quota_rec("prev")
    ours = {"type": "user", "message": {"role": "user", "content": lr.DEFAULT_PROMPT}}
    with open(path, "w") as fh:
        for r in recs[:1] + [prev, ours] + recs[1:]:
            fh.write(json.dumps(r) + "\n")
    lr.save_state({lr.CONFIG: {"auto_all": True},
                   "sess-m": {"prompt": lr.DEFAULT_PROMPT, "resumes": 8, "handled": ["prev"]}})
    sweeps(lr, 1)
    st = lr.load_state()
    check("8 in a row -> retired, not launched", (st["sess-m"].get("why"), launches(argv_log)),
          ("max resumes", []))
    check("and you are told", sum("resumed 8 times" in a for a in alerts), 1)
    append_records(path, [{"type": "user", "message": {"role": "user", "content": "carry on"}},
                          quota_rec("new-stop")])
    sweeps(lr, 1)
    st = lr.load_state()
    check("typed in it, then a new stop -> watched and resumed again",
          (st["sess-m"].get("done"), st["sess-m"].get("resumes"),
           any("--resume sess-m" in l for l in launches(argv_log))), (None, 1, True))
    path = write_transcript(root, "sess-c", "quota")
    recs = [json.loads(l) for l in open(path)]
    typed = {"type": "user", "message": {"role": "user", "content": "my own turn"}}
    with open(path, "w") as fh:
        for r in recs[:1] + [quota_rec("c-prev"), ours, typed] + recs[1:]:
            fh.write(json.dumps(r) + "\n")
    st[lr.CONFIG].pop("active_resume", None)
    st["sess-c"] = {"prompt": lr.DEFAULT_PROMPT, "resumes": 8, "handled": ["c-prev"]}
    lr.save_state(st)
    settle(lr)
    sweeps(lr, 2)
    check("a typed turn since the last resume restarts the count",
          lr.load_state()["sess-c"].get("resumes"), 1)

    # ---- 66. B2-M5: a session open in another claude process ------------------
    root, argv_log, lr, alerts = b2_scenario("66. open in a terminal -> alert, no headless resume")
    rows = [(501, 1, "claude --resume sess-open"), (502, 1, "claude -r sess-r2"),
            (503, 1, "/x/versions/2.1 --print " + lr.RC_CHILD_MARK + "cse --session-id sess-rc2"),
            (504, 1, "claude --session-id=sess-eq --model opus")]
    check("owner by --resume / -r / --session-id=",
          [lr.local_owner(s, rows) for s in ("sess-open", "sess-r2", "sess-eq", "sess-open2")],
          [501, 502, 504, None])
    check("a Remote Control child is not an owner", lr.local_owner("sess-rc2", rows), None)
    write_transcript(root, "sess-open", "quota")
    lr.ps_rows = lambda: rows
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 1)
    check("not resumed headless", launches(argv_log), [])
    check("alert says where", [("pid 501" in a, "continue" in a) for a in alerts], [(True, True)])
    check("block handled, no second alert", (sweeps(lr, 1), len(alerts)), (None, 1))

    # ---- 67. B2-M6: a corrupt state file is kept and recovered ---------------
    root, argv_log, lr, alerts = b2_scenario("67. corrupt state -> kept aside, .bak used, doctor says so")
    lr.save_state({lr.CONFIG: {"auto_all": True, "rc_watch": False, "maint_last": 5}})
    open(lr.STATE, "w").write("{half")
    check("a reader gets the .bak, file untouched",
          (lr.load_state()[lr.CONFIG]["auto_all"], open(lr.STATE).read()), (True, "{half"))
    lr.scheduler_status = lambda: ("launchd : loaded", True, False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lr.cmd_doctor([])
    check("doctor: state UNREADABLE", ("state      : UNREADABLE" in buf.getvalue(), code), (True, 1))
    sweeps(lr, 1)
    kept = glob.glob(lr.STATE + ".corrupt-*")
    check("sweep keeps the corrupt copy", len(kept) == 1 and open(kept[0]).read() == "{half", True)
    check("and carries on from the .bak", lr.load_state()[lr.CONFIG].get("maint_last"), 5)
    check("alert sent", sum("could not be read" in a for a in alerts), 1)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lr.cmd_doctor([])
    check("doctor: names the kept copy", ("was unreadable, kept as" in buf.getvalue(), code), (True, 1))

    # ---- 68. B2-L2: this folder's server only --------------------------------
    root, argv_log, lr, alerts = b2_scenario("68. Remote Control server matched by --name")
    open(lr.INSTALL_CONF, "w").write("rc_dir=/Users/u/My Vault/\n")
    rows = [(10, 1, "claude rc"),
            (11, 1, "/u/.local/bin/claude remote-control --name auto-Other --debug-file /t/o.log"),
            (12, 1, "/u/.local/bin/claude remote-control --name auto-My Vault --debug-file "
                    "/Users/u s/.claude/tools/rc-debug/rc-1.log")]
    check("ours, with a debug path holding a space", lr.rc_server(rows, lr.rc_folders()[0]),
          (12, "/Users/u s/.claude/tools/rc-debug/rc-1.log"))
    check("another folder's or a hand-started server: not ours", lr.rc_server(rows[:2], lr.rc_folders()[0]), None)
    auto = [(13, 1, "/u/.local/bin/claude remote-control --name auto-My Vault --permission-mode auto "
                    "--debug-file /Users/u s/.claude/tools/rc-debug/rc-2.log")]
    check("with --permission-mode auto (2026-11-23.4 launcher): still ours, same log",
          lr.rc_server(auto, lr.rc_folders()[0]), (13, "/Users/u s/.claude/tools/rc-debug/rc-2.log"))
    os.unlink(lr.INSTALL_CONF)
    check("no install.conf -> the first server", lr.rc_server(rows, lr.rc_folders()[0])[0], 10)

    # ---- 69. B2-L5 / B2-L6: clock steps and stalls ---------------------------
    root, argv_log, lr, alerts = b2_scenario("69. epoch heartbeat; timers reset after a stall")
    check("heartbeat in the future -> unknown", lr.beat_age({"last_sweep_at": _t.time() + 600}), None)
    check("epoch heartbeat wins over local time",
          int(lr.beat_age({"last_sweep_at": _t.time() - 120, "last_sweep": "2000-01-01T00:00:00"})), 120)
    lr.rc_running = lambda _folder: False
    opened = []
    lr.subprocess.run = fake_run(calls=opened)
    lr.RC_LAUNCHER = os.path.join(root, "claude-rc.command")
    open(lr.RC_LAUNCHER, "w").write("#!/bin/sh\n")
    st = {lr.CONFIG: {"rc_watch": True, "rc_last_launch": _t.time() + 3600}}
    check("launch time ahead of the clock does not throttle", lr.rc_watchdog(st), True)
    subprocess.run = REAL_RUN
    lr.save_state({lr.CONFIG: {"last_sweep_at": _t.time() - 7200,
                               "rc_hung_seen": [1, 2, 3], "rc_stale_since": 4}})
    with quiet():
        lr.cmd_sweep([])
    cfg = lr.load_state()[lr.CONFIG]
    check("after a 2 h gap the hang and stale timers start afresh",
          (cfg.get("rc_hung_seen"), cfg.get("rc_stale_since")), (None, None))

    # ---- 70. B2-L8, B2-L9, B2-L1, B2-I1 ---------------------------------------
    root, argv_log, lr, alerts = b2_scenario("70. legacy jobs, uuid-less stops, log, tail")
    write_transcript(root, "sess-legacy", "quota")
    job = write_transcript(root, "sess-job", "quota")
    recs = [json.loads(l) for l in open(job)]
    recs[0]["message"]["content"] = lr.JOB_RULES % "/n.md" + "Do it."
    with open(job, "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in recs)
    lr.save_state({lr.CONFIG: {"auto_all": True},
                   "sess-legacy": {"prompt": "p", "resumes": 0, "handled": [], "job": "old.md"}})
    sweeps(lr, 2)
    st = lr.load_state()
    check("legacy job entry moved to the job list",
          ("sess-legacy" in st, "sess-legacy" in st.get(lr.JOBS_KEY, {}).get("sids", [])), (False, True))
    check("neither it nor a job transcript is resumed", launches(argv_log), [])
    rec = quota_rec(None)
    rec.pop("uuid")
    check("a stop without a uuid gets one", lr.rate_limit_stop(rec)["uuid"].startswith("noid-"), True)
    lr.LOG = os.path.join(root, "no-such-dir", "log.txt")
    err = io.StringIO()
    with quiet(), contextlib.redirect_stderr(err):
        lr.log("hello")
    check("log never raises; falls back to stderr", "hello" in err.getvalue(), True)
    big = os.path.join(root, "big.jsonl")
    with open(big, "w") as fh:
        for i in range(1000):
            fh.write(json.dumps({"i": i, "pad": "x" * (i % 7) * 20000}) + "\n")
        fh.write('{"i": "last"')
    check("tail read from the end", [json.loads(l)["i"] for l in lr.tail_lines(big, 400)[:-1]][-3:]
          + [lr.tail_lines(big, 400)[-1]], [997, 998, 999, '{"i": "last"'])
    check("tail keeps exactly n lines", len(lr.tail_lines(big, 400)), 400)
    check("tail of a short file", len(lr.tail_lines(lr.session_file("sess-job"), 400)), 4)

    # ---- 71. B2-L9: doctor shows a suspected hang; maintenance rotates out.log --
    root, argv_log, lr, alerts = b2_scenario("71. doctor shows a suspected hang; out.log rotated")
    lr.scheduler_status = lambda: ("launchd : loaded", True, False)
    lr.rc_serving = lambda rows, folder: ("launcher", 7)
    lr.keychain = lambda account: ""
    lr.save_state({lr.CONFIG: {"rc_watch": True, "last_sweep_at": _t.time(),
                               "last_sweep": "x", "rc_hung_seen": [7, _t.time() - 1200, 0]}})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lr.cmd_doctor([])
    check("suspected hang shown, NEEDS ATTENTION", ("SUSPECTED HUNG" in buf.getvalue(), code), (True, 1))
    os.makedirs(lr.TOOLS, exist_ok=True)
    out_log = os.path.join(lr.TOOLS, "limit-resume.out.log")
    open(out_log, "w").write("x" * 100)
    lr.LOG_MAX = 10
    with quiet():
        summary = lr.maintain({}, _t.time(), plan={})
    check("out.log rotated", (os.path.exists(out_log + ".1"), "out.log rotated" in summary), (True, True))

INSTALL_SH = os.path.join(os.path.dirname(HERE), "install.sh")
UNINSTALL_SH = os.path.join(os.path.dirname(HERE), "uninstall.sh")
KIT = os.path.dirname(INSTALL_SH)


def installer_box():
    """A throwaway HOME, with fake launchctl / osascript / claude / ps / security."""
    root = tempfile.mkdtemp(prefix="lrinst-")
    home, bindir = os.path.join(root, "home"), os.path.join(root, "bin")
    os.makedirs(home)
    os.makedirs(bindir)
    calls, loaded = os.path.join(root, "calls"), os.path.join(root, "loaded")
    bins = {
        "launchctl": 'echo "launchctl $*" >> "%s"\n' % calls
                     + 'case "$1" in\n'
                       'print) [ -f "%s" ] ;;\n' % loaded
                     + 'bootout) rm -f "%s" ;;\n' % loaded
                     + 'bootstrap)\n'
                       '  if [ -n "${FAKE_BOOTSTRAP_FAIL:-}" ]; then\n'
                       '    echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi\n'
                       '  grep -q maint_last "$HOME/.claude/tools/limit-resume.state.json" 2>/dev/null'
                       ' && echo "bootstrap after maintain" >> "%s"\n' % calls
                     + '  : > "%s" ;;\nesac\n' % loaded,
        "osascript": 'echo "osascript $*" >> "%s"\nexit 0\n' % calls,
        "claude": "exit 0\n",
        "ps": 'cat "%s/ps.out" 2>/dev/null\nexit 0\n' % root,
        "security": "exit 44\n",
    }
    for exe, body in bins.items():
        path = os.path.join(bindir, exe)
        open(path, "w").write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
    os.symlink(sys.executable, os.path.join(bindir, "python3"))
    box = {"root": root, "home": home, "calls": calls, "loaded": loaded,
           "bin": bindir, "tools": os.path.join(home, ".claude", "tools"),
           "label": "test.claude-limit-resume.%s" % uuidlib.uuid4().hex[:8]}
    box["plist"] = os.path.join(home, "Library", "LaunchAgents", box["label"] + ".plist")
    box["env"] = {"HOME": home, "PATH": bindir + ":/usr/bin:/bin:/usr/sbin:/sbin",
                  "TMPDIR": root, "LIMIT_RESUME_LABEL": box["label"],
                  "LIMIT_RESUME_NO_OBSIDIAN": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return box


def run_sh(box, script, *args, **env):
    open(box["calls"], "w").close()
    proc = subprocess.run(["/bin/sh", script] + list(args), capture_output=True, text=True,
                          env=dict(box["env"], **env), timeout=120, stdin=subprocess.DEVNULL)
    calls = [l for l in open(box["calls"]).read().splitlines() if l]
    return proc.returncode, proc.stdout + proc.stderr, calls


def installer_scenarios():
    if sys.platform != "darwin" or not os.path.exists(INSTALL_SH):
        print("\n(not macOS - install.sh / uninstall.sh tests skipped)")
        return
    import re
    read = lambda p: open(p).read() if os.path.exists(p) else None
    tools_files = lambda b: sorted(os.listdir(b["tools"])) if os.path.isdir(b["tools"]) else []
    version = re.search(r'^VERSION = "(.*)"$', open(TOOL).read(), re.M).group(1)

    box = installer_box()
    print("\n44. install.sh: refuses before changing anything")
    code, out, calls = run_sh(box, INSTALL_SH)
    check("default folder missing -> exit 1", code, 1)
    check("says which folder", "%s/_obsidian does not exist" % box["home"] in out, True)
    check("nothing written", (tools_files(box), os.path.exists(box["plist"]), calls),
          ([], False, []))
    code, out, calls = run_sh(box, INSTALL_SH, "--purge")
    check("unknown option -> exit 2, nothing written", (code, tools_files(box)), (2, []))
    odd = os.path.join(box["home"], "va$ult")
    os.makedirs(odd)
    code, out, _ = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIR=odd)
    check("a $ in the folder -> refused", (code, "cannot carry safely" in out, tools_files(box)),
          (1, True, []))

    print("\n45. install.sh: first install")
    vault = os.path.join(box["home"], "vault")
    os.makedirs(vault)
    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIR=vault)
    check("exit 0", code, 0)
    check("prints the version", "limit-resume.py %s (installed: none)" % version in out, True)
    check("tool installed byte for byte",
          read(os.path.join(box["tools"], "limit-resume.py")) == read(TOOL), True)
    check("folder and label saved", read(os.path.join(box["tools"], "install.conf")),
          "rc_dir=%s\nlabel=%s\nsrc=%s\n" % (vault, box["label"], KIT))
    launcher = read(os.path.join(box["tools"], "claude-rc.command")) or ""
    check("launcher filled in", ('cd "%s" || exit 1' % vault in launcher,
                                 'PYTHON="%s/python3"' % box["bin"] in launcher,
                                 re.findall(r"@[A-Z_]+@", launcher)), (True, True, []))
    check("spawned sessions start in auto mode, not manual",
          '--name "auto-vault" --permission-mode auto --debug-file' in launcher, True)
    settings = os.path.join(box["home"], ".claude", "settings.json")
    check("every session starts in auto: settings.json created with defaultMode auto",
          json.loads(read(settings) or "{}"), {"permissions": {"defaultMode": "auto"}})
    check("plist written", os.path.exists(box["plist"]), True)
    check("bootstrapped once, no bootout",
          ([c for c in calls if "launchctl boot" in c]),
          ["launchctl bootstrap gui/%d %s" % (os.getuid(), box["plist"])])
    check("maintenance ran before the bootstrap", "bootstrap after maintain" in calls, True)
    check("login item added", any("make login item" in c for c in calls), True)
    check("no temp files left", ([f for f in tools_files(box) if f.endswith(".new")],
                                 [f for f in os.listdir(box["root"])
                                  if f.startswith("limit-resume-plist")]), ([], []))

    print("\n46. install.sh: re-run")
    code, out, calls = run_sh(box, INSTALL_SH)
    check("no env needed: the saved folder is used", (code, "serves %s" % vault in out), (0, True))
    check("plist unchanged -> no reload", [c for c in calls if "launchctl boot" in c], [])
    check("says so", "not reloaded" in out, True)
    os.unlink(os.path.join(box["tools"], "install.conf"))
    code, out, _ = run_sh(box, INSTALL_SH)
    check("no install.conf yet -> the launcher's folder is kept",
          (code, (read(os.path.join(box["tools"], "install.conf")) or "").split("\n")[0]),
          (0, "rc_dir=%s" % vault))

    print("\n47. install.sh: a reload that would kill work in flight")
    open(box["plist"], "a").write("<!-- older -->\n")
    tool = os.path.join(box["tools"], "limit-resume.py")
    tool_before = os.stat(tool).st_mtime_ns
    open(os.path.join(box["root"], "ps.out"), "w").write(
        "  4242     1 %s %s/limit-resume.py sweep\n" % (sys.executable, box["tools"]))
    code, out, calls = run_sh(box, INSTALL_SH)
    check("refused", (code, "would kill" in out), (1, True))
    check("lists the sweep", "in flight: a sweep" in out, True)
    check("nothing changed", ([c for c in calls if "launchctl boot" in c],
                              read(box["plist"]).endswith("<!-- older -->\n"),
                              os.stat(tool).st_mtime_ns), ([], True, tool_before))
    os.unlink(os.path.join(box["root"], "ps.out"))
    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_JOB="1")
    check("run from inside a job session -> refused", (code, "LIMIT_RESUME_JOB" in out), (1, True))
    code, out, calls = run_sh(box, INSTALL_SH, "--force", LIMIT_RESUME_JOB="1")
    check("--force reloads", (code, [c.split()[1] for c in calls if "launchctl boot" in c]),
          (0, ["bootout", "bootstrap"]))
    check("plist rewritten", read(box["plist"]).endswith("</plist>\n"), True)

    print("\n48. install.sh: downgrade guard")
    open(tool, "w").write('VERSION = "2099-01-01"\n')
    code, out, _ = run_sh(box, INSTALL_SH)
    check("older kit refused", (code, "older than the installed 2099-01-01" in out), (1, True))
    check("installed copy untouched", read(tool), 'VERSION = "2099-01-01"\n')
    code, out, _ = run_sh(box, INSTALL_SH, "--force")
    check("--force downgrades", (code, read(tool) == read(TOOL)), (0, True))

    print("\n49. install.sh: bootstrap fails")
    os.unlink(box["loaded"])
    code, out, calls = run_sh(box, INSTALL_SH, FAKE_BOOTSTRAP_FAIL="1")
    check("exit 1", code, 1)
    check("tried three times", sum("launchctl bootstrap" in c for c in calls), 3)
    check("one clear line with the fix", "SCHEDULER NOT LOADED - no sweep will run. Fix: "
          "launchctl bootstrap" in out, True)
    check("the later steps still ran", os.path.exists(
        os.path.join(box["tools"], "weekly-check-setup.txt")), True)
    code, out, calls = run_sh(box, INSTALL_SH)
    check("next run loads it", (code, "loaded into launchd" in out), (0, True))

    print("\n50. install.sh --no-start")
    nbox = installer_box()
    os.makedirs(os.path.join(nbox["home"], "_obsidian"))
    code, out, calls = run_sh(nbox, INSTALL_SH, "--no-start")
    check("exit 0, nothing loaded, no maintenance", (code, [c for c in calls if " print " not in c],
                                                      "maint_last" in (
        read(os.path.join(nbox["tools"], "limit-resume.state.json")) or "")), (0, [], False))
    check("says launchd loads it at next login", "at the next login" in out, True)

    print("\n51. uninstall.sh")
    code, out, _ = run_sh(box, UNINSTALL_SH, "--purg")
    check("unknown option -> exit 2", code, 2)
    open(os.path.join(box["root"], "ps.out"), "w").write(
        "  4343     1 %s %s/limit-resume.py jobpty x.pty.log -- claude\n"
        % (sys.executable, box["tools"]))
    code, out, calls = run_sh(box, UNINSTALL_SH)
    check("a job session runs -> refused, nothing removed",
          (code, "in flight: a job" in out, os.path.exists(box["plist"]), calls),
          (1, True, True, []))
    os.unlink(os.path.join(box["root"], "ps.out"))
    state_path = os.path.join(box["tools"], "limit-resume.state.json")
    st = json.loads(read(state_path))
    st.setdefault("__jobs__", {})["on"] = True
    st.setdefault("__config__", {})["auto_all"] = True
    open(state_path, "w").write(json.dumps(st))
    code, out, calls = run_sh(box, UNINSTALL_SH)
    st = json.loads(read(state_path))
    check("exit 0", code, 0)
    check("scheduler gone", (os.path.exists(box["plist"]),
                             any("launchctl bootout" in c for c in calls)), (False, True))
    check("Remote Control Login Item removed",
          any('delete login item "claude-rc.command"' in c for c in calls), True)
    check("Obsidian left alone", any("Obsidian" in c for c in calls), False)
    check("auto off and the job queue off", (st["__config__"]["auto_all"], st["__jobs__"]["on"]),
          (False, False))
    check("tells you to delete the scheduled task", "auto-resume-weekly-maintenance" in out, True)
    check("tool, launcher and conf kept", all(os.path.exists(os.path.join(box["tools"], f)) for f in
          ("limit-resume.py", "claude-rc.command", "install.conf")), True)
    open(os.path.join(box["tools"], "limit-resume.py.bak-20261122"), "w").write("old")
    os.makedirs(os.path.join(box["tools"], "jobs"), exist_ok=True)
    code, out, calls = run_sh(box, UNINSTALL_SH, "--purge", "--obsidian")
    check("--purge: tool, launcher, conf, jobs/ gone; the backup kept", (code, tools_files(box)),
          (0, ["limit-resume.py.bak-20261122"]))
    check("says the backup was kept", "kept backup" in out, True)
    check("--obsidian removes that Login Item too",
          any('delete login item "Obsidian"' in c for c in calls), True)

    print("\n51d. install.sh: default permission mode merged into settings.json")
    box = installer_box()
    vault = os.path.join(box["home"], "vault")
    os.makedirs(vault)
    claude_dir = os.path.join(box["home"], ".claude")
    os.makedirs(claude_dir)
    settings = os.path.join(claude_dir, "settings.json")
    mine = {"model": "opus", "permissions": {"allow": ["Bash(ls)"], "defaultMode": "default"},
            "hooks": {"Stop": []}}
    open(settings, "w").write(json.dumps(mine))
    code, out, _ = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIR=vault)
    got = json.loads(read(settings))
    backups = [f for f in os.listdir(claude_dir) if f.startswith("settings.json.bak-")]
    check("manual -> auto, every other key kept, one backup of the old file",
          (code, got["permissions"]["defaultMode"], got["permissions"]["allow"], got["model"],
           got["hooks"], len(backups), json.loads(read(os.path.join(claude_dir, backups[0])))
           if backups else None, "manual -> auto" in out),
          (0, "auto", ["Bash(ls)"], "opus", {"Stop": []}, 1, mine, True))
    code, out, _ = run_sh(box, INSTALL_SH)
    check("already auto: no second backup",
          (code, "already auto" in out,
           len([f for f in os.listdir(claude_dir) if f.startswith("settings.json.bak-")])), (0, True, 1))
    open(settings, "w").write("{broken")
    code, out, _ = run_sh(box, INSTALL_SH)
    check("a settings.json that does not parse is left alone, install goes on",
          (code, read(settings), "does not parse" in out), (0, "{broken", True))

    print("\n51b. install.sh / uninstall.sh on Linux")
    box = installer_box()
    for exe, body in {"uname": "echo Linux\n",
                      "systemctl": 'echo "systemctl $*" >> "%s"\n' % box["calls"],
                      "loginctl": 'echo "Linger=yes"\n'}.items():
        path = os.path.join(box["bin"], exe)
        open(path, "w").write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
    vault = os.path.join(box["home"], "vault")
    os.makedirs(vault)
    units = os.path.join(box["home"], ".config", "systemd", "user")
    os.makedirs(units)
    for name in ("-maintenance.service", "-maintenance.timer"):      # left by 2026-11-23.2
        open(os.path.join(units, box["label"] + name), "w").write("old\n")
    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIR=vault)
    check("exit 0", code, 0)
    check("only the sweep timer; 2026-11-23.2's maintenance timer disabled and removed",
          (sorted(os.listdir(units)),
           [c for c in calls if c.startswith("systemctl --user enable")],
           any(c.startswith("systemctl --user disable") and "-maintenance.timer" in c
               for c in calls)),
          (sorted([box["label"] + ".service", box["label"] + ".timer"]),
           ["systemctl --user enable --now %s.timer" % box["label"]], True))
    setup = read(os.path.join(box["tools"], "weekly-check-setup.txt")) or ""
    check("prints the paste-in setup prompt, no `rc on` hint",
          ("LAST STEP" in out, setup.strip() in out, "create_trigger" in setup,
           "rc on       #" in out), (True, True, True, False))
    code, out, calls = run_sh(box, UNINSTALL_SH)
    check("uninstall removes the units", (code, sorted(os.listdir(units))), (0, []))

    print("\n51c. install.sh on Windows (Git Bash) refuses")
    box = installer_box()
    path = os.path.join(box["bin"], "uname")
    open(path, "w").write("#!/bin/sh\necho MINGW64_NT-10.0\n")
    os.chmod(path, 0o755)
    vault = os.path.join(box["home"], "vault")
    os.makedirs(vault)
    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIR=vault)
    check("exit 1, says Windows is not supported, nothing installed",
          (code, "Windows is not supported" in out, tools_files(box)), (1, True, []))


# ---- background jobs --------------------------------------------------------

FAKE_RC = r'''#!/usr/bin/env python3
# Stand-in for an interactive `claude --remote-control` job session.
import json, os, sys, time
ROOT = %r
args = sys.argv[1:]
with open(os.path.join(ROOT, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
with open(os.path.join(ROOT, "cwd.txt"), "a") as fh:
    fh.write(os.getcwd() + "\n")
with open(os.path.join(ROOT, "tty.txt"), "a") as fh:
    fh.write("%%s\n" %% os.isatty(0))
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
plan = json.load(open(os.path.join(ROOT, "plan.json")))
if plan.get("write", True):
    d = os.path.join(ROOT, "projects", "-fake")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "a") as fh:
        for r in plan.get("records", []):
            fh.write(json.dumps(r) + "\n")
if plan.get("stubborn"):
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
if plan.get("grandchild"):
    import subprocess
    kid = subprocess.Popen(["sleep", "60"], start_new_session=True)
    with open(os.path.join(ROOT, "grandchild.pid"), "w") as fh:
        fh.write(str(kid.pid))
sys.stdout.write("fake session screen\n")
sys.stdout.flush()
time.sleep(plan.get("sleep", 0))
sys.exit(plan.get("exit", 0))
'''


def said(text, tool=False):
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "name": "Bash", "input": {}})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


ASKED = {"type": "user", "message": {"role": "user", "content": "the job"}}
DONE_PLAN = {"records": [ASKED, said("Report written to /vault/report.md\n\nJOB DONE")],
             "sleep": 60}


def set_plan(root, plan):
    with open(os.path.join(root, "plan.json"), "w") as fh:
        json.dump(plan, fh)


def job_argvs(root):
    path = os.path.join(root, "argv.jsonl")
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def wait_for(cond, secs=10):
    import time as _t
    deadline = _t.time() + secs
    while not cond() and _t.time() < deadline:
        _t.sleep(0.05)
    return cond()


def write_job(folder, name, body="Do the job.", **meta):
    lines = ["---"] + ["%s: %s" % kv for kv in meta.items()] + ["---"]
    path = os.path.join(folder, name)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n# " + name[:-3] + "\n\n" + body + "\n")
    return path


def set_usage(lr, five=10, week=None, week_reset_h=48, five_reset_h=3, age=0):
    import time as _t
    now = _t.time()
    resets = now + week_reset_h * 3600
    if week is None:
        week = lr.weekly_cap(now, resets) - 40    # 40 points left -> gate opens ~63 h out
    data = {}
    if five is not None:
        data["five_hour"] = {"pct": five, "resets_at": now + five_reset_h * 3600,
                             "at": now - age, "source": "test"}
    if week is not None:
        data["seven_day"] = {"pct": week, "resets_at": resets, "at": now - age, "source": "test"}
    with open(lr.USAGE, "w") as fh:
        json.dump(data, fh)


SCENARIO_LRS = []


def job_scenario(name, plan=DONE_PLAN, trusted=True):
    print("\n%s" % name)
    root = tempfile.mkdtemp(prefix="lrjob-")
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "claude"), "w") as fh:
        fh.write(FAKE_RC % root)
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
    set_plan(root, plan)
    lr = load_tool(root, os.path.join(root, "argv.txt"))
    SCENARIO_LRS.append(lr)
    lr.CLAUDE_JSON = os.path.join(root, "claude.json")
    folder = os.path.join(root, "Background job")
    os.makedirs(folder)
    work = os.path.join(root, "work")
    os.makedirs(work)
    with open(lr.CLAUDE_JSON, "w") as fh:
        json.dump({"projects": {root if trusted else "/nowhere": {"hasTrustDialogAccepted": True}}}, fh)
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "pushover"
    probes = []
    lr.probe_usage = lambda: probes.append(1) or (None, "stub probe")
    lr.JOB_STOP_GRACE = 5
    lr.save_state({lr.CONFIG: {"auto_all": False},
                   lr.JOBS_KEY: {"on": True, "dir": folder, "cwd": work}})
    return root, lr, folder, work, alerts, probes


def running_run(lr):
    return lr.load_state()[lr.JOBS_KEY].get("running")


def wait_transcript(lr):
    run = running_run(lr)
    return wait_for(lambda: bool(run and lr.session_file(run["sid"])))


def kill_all():
    for lr in SCENARIO_LRS:
        for proc in lr._SPAWNED.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()


def note(path):
    return open(path).read()


def jobs_scenarios():
    try:
        _jobs_scenarios()
    finally:
        kill_all()


def _jobs_scenarios():
    import time as _t

    # ---- J1. gate arithmetic, per-account weekly reset ----------------------
    print("\nJ1. weekly gate and caps follow the account's own reset")
    root = tempfile.mkdtemp(prefix="lrjob-")
    lr = load_tool(root, os.path.join(root, "argv.txt"))
    check("gate 72 h with >= 50 left", lr.gate_hours(60), 72)
    check("gate 30 h with <= 5 left", lr.gate_hours(2), 30)
    check("gate linear in between", round(lr.gate_hours(27.5), 1), 51.0)
    at = lambda s: datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
    mon3 = at("2026-11-23 03:00")                 # an account resetting Monday 03:00
    check("reset Mon 03:00: Sunday noon is the last day, 90", lr.weekly_cap(at("2026-11-22 12:00"), mon3), 90)
    check("reset Mon 03:00: Monday 02:00 still 90", lr.weekly_cap(at("2026-11-23 02:00"), mon3), 90)
    check("reset Mon 03:00: Saturday 23:00 is 80", lr.weekly_cap(at("2026-11-21 23:00"), mon3), 80)
    thu = at("2026-11-19 14:00")                  # another account resetting Thursday 14:00
    check("reset Thu 14:00: Wednesday 01:00 is 90", lr.weekly_cap(at("2026-11-18 01:00"), thu), 90)
    check("reset Thu 14:00: Thursday 13:00 is 90", lr.weekly_cap(at("2026-11-19 13:00"), thu), 90)
    check("reset Thu 14:00: Tuesday 23:00 is 80", lr.weekly_cap(at("2026-11-17 23:00"), thu), 80)
    check("reset Thu 14:00: Sunday is an ordinary 80", lr.weekly_cap(at("2026-11-15 12:00"), thu), 80)
    check("unknown reset -> 80", lr.weekly_cap(at("2026-11-15 12:00"), None), 80)
    nxt = lr.next_weekly("Thu 14:00", at("2026-11-17 09:00"))
    check("--reset Thu 14:00 -> next Thursday 14:00", nxt, thu)
    check("--reset rolls to next week once passed",
          lr.next_weekly("thursday 14:00", at("2026-11-19 15:00")), at("2026-11-26 14:00"))
    check("--reset rejects junk", (lr.next_weekly("Funday 14:00", 0), lr.next_weekly("Mon 25:00", 0)),
          (None, None))
    now = _t.time()
    u = lambda five, week, wh: {
        "five_hour": {"pct": five, "resets_at": now + 3600, "at": now},
        "seven_day": {"pct": week, "resets_at": now + wh * 3600, "at": now}}
    cap = lambda wh: lr.weekly_cap(now, now + wh * 3600)
    ok, why = lr.job_gate(u(75, 10, 40), now)
    check("5-hour 75% closes it", (ok, why.startswith("5-hour 75% >= 60% until")), (False, True))
    ok, why = lr.job_gate(u(10, cap(40), 40), now)
    check("weekly at the cap closes it", (ok, "today's cap" in why), (False, True))
    check("100 h before reset: closed", lr.job_gate(u(10, cap(100) - 50, 100), now)[0], False)
    check("60 h before, 50 left: open", lr.job_gate(u(10, cap(60) - 50, 60), now)[0], True)
    check("60 h before, 10 left: closed", lr.job_gate(u(10, cap(60) - 10, 60), now)[0], False)
    check("30 h before, 10 left: open", lr.job_gate(u(10, cap(30) - 10, 30), now)[0], True)
    check("unknown usage: closed", lr.job_gate({}, now), (False, "usage unknown"))
    only_week = u(10, cap(40) - 40, 40)
    del only_week["five_hour"]
    check("probe gave only the weekly window: closed, unknown counts as over (B3-H2)",
          lr.job_gate(only_week, now), (False, "usage unknown: no 5-hour figure"))
    stale = u(10, cap(40) - 40, 40)
    stale["five_hour"]["at"] = now - lr.USAGE_MAX_AGE - 60
    check("a 5-hour figure older than USAGE_MAX_AGE: closed",
          lr.job_gate(stale, now), (False, "usage unknown: the 5-hour figure is 11 min old"))
    check("... and a running job pauses on it", lr.cap_hit(stale, now),
          "usage unknown: the 5-hour figure is 11 min old")
    expired = u(10, cap(40) - 40, 40)
    expired["five_hour"]["resets_at"] = now - 60
    check("a 5-hour figure from a window that has reset: closed",
          lr.job_gate(expired, now), (False, "usage unknown: the 5-hour window has reset since it was read"))
    check("fresh, both windows, under the caps: no hit", lr.cap_hit(u(10, cap(40) - 40, 40), now), None)
    far = u(10, cap(100) - 50, 100)
    far["five_hour"]["at"] = now - 3600
    check("a closed weekly gate is named before unknown usage",
          lr.job_gate(far, now)[1].startswith("weekly gate opens"), True)
    only_five = u(10, 10, 40)
    del only_five["seven_day"]
    check("no weekly figure: closed", lr.job_gate(only_five, now), (False, "usage unknown"))
    noreset = u(10, 30, 40)
    noreset["seven_day"]["resets_at"] = None
    check("no reset anywhere: closed, says how to set one",
          "jobs on --reset" in lr.job_gate(noreset, now)[1], True)
    spec = (datetime.datetime.fromtimestamp(now) + datetime.timedelta(hours=40)).strftime("%a %H:%M")
    check("no reset in the reading: --reset fills in",
          lr.job_gate(noreset, now, {"weekly_reset": spec})[0], True)
    old = u(10, 10, 100)
    for w in old.values():
        w["at"] = now - lr.JOB_PROBE_MAX_SKIP - 60
    check("an old reading can't keep the gate shut (a reset may have moved it)",
          lr.closed_anyway(old, now), False)
    check("same slot a week later", lr.same_schedule(thu, thu + 7 * 86400 + 30), True)
    check("a different day is a different slot", lr.same_schedule(thu, mon3), False)

    print("\nJ1b. a reset handed out by Claude moves the weekly reset")
    lr.notify = lambda m: alerted.append(m) or "pushover"
    alerted = []
    js = {}
    reading = lambda r: {"seven_day": {"pct": 5, "resets_at": r, "at": now}}
    nxt_thu = lr.next_weekly("Thu 14:00", now)
    lr.track_reset(js, reading(nxt_thu), now)
    check("first reset seen, no alert", (js.get("reset_seen"), alerted), (nxt_thu, []))
    lr.track_reset(js, reading(nxt_thu + 7 * 86400), now)
    check("the normal weekly roll is not a move", alerted, [])
    moved = nxt_thu + 2 * 86400 - 4 * 3600          # Saturday 10:00 instead
    lr.track_reset(js, reading(moved), now)
    check("moved slot -> one alert", len(alerted) == 1 and "Weekly reset moved" in alerted[0], True)
    check("move recorded", js.get("reset_moved", [None, None])[:2], [nxt_thu + 7 * 86400, moved])
    check("cap follows the new day", lr.weekly_cap(moved - 3600, moved), 90)
    nodata = {"seven_day": {"pct": 5, "resets_at": None, "at": now}}
    check("no reset in a later reading: the last one seen, rolled forward",
          lr.weekly_reset(nodata, js, moved + 60), moved + 7 * 86400)
    js2 = {"weekly_reset": "Thu 14:00"}
    lr.track_reset(js2, reading(moved), now)
    check("a reading supersedes --reset", (js2.get("weekly_reset"), js2.get("reset_seen")),
          (None, moved))
    check("superseded --reset logged", "no longer matches the account" in open(lr.LOG).read(), True)
    past = u(90, 10, 40)
    past["five_hour"]["resets_at"] = now - 60
    check("a 5-hour reading from a window that has reset counts as 0",
          lr.window_pct(past, "five_hour", now), 0.0)
    check("closed_anyway when reset is > 72 h out", lr.closed_anyway(u(10, 10, 100), now), True)
    check("not closed_anyway inside 72 h", lr.closed_anyway(u(10, 10, 60), now), False)

    # ---- J2. usage parsing -------------------------------------------------
    print("\nJ2. usage readings")
    sl = lr.reading_from_statusline({"rate_limits": {
        "five_hour": {"used_percentage": 42, "resets_at": 1790000000},
        "seven_day": {"used_percentage": 61.5, "resets_at": "2026-11-23T03:00:00+07:00"}}})
    check("status line 5-hour", sl["five_hour"]["pct"], 42.0)
    check("status line weekly with an ISO reset",
          (sl["seven_day"]["pct"], bool(sl["seven_day"]["resets_at"])), (61.5, True))
    st = lr.reading_from_stream([json.dumps(r) for r in [
        {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "five_hour",
                                                          "utilization": 0.25, "resetsAt": 1}},
        {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "seven_day",
                                                          "utilization": 0.5}},
        {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "seven_day_opus",
                                                          "status": "rejected"}},
        {"type": "assistant"}]])
    check("stream utilization fraction -> %", st["five_hour"]["pct"], 25.0)
    check("worst weekly variant binds; rejected = 100", st["seven_day"]["pct"], 100.0)
    lr.save_usage({"five_hour": {"pct": 30, "resets_at": 5}}, "probe", now=100)
    lr.save_usage({"seven_day": {"pct": 50, "resets_at": 6}}, "statusline", now=200)
    cached = lr.load_usage()
    check("cache keeps each window's own time",
          (cached["five_hour"]["at"], cached["seven_day"]["at"]), (100, 200))
    print("\nJ2b. usage cache merges monotonically; markers on the last line (B3-M8, L2, L5)")
    week = lambda pct, r: {"seven_day": {"pct": pct, "resets_at": r}}
    lr.save_usage(week(65, now + 9000), "job", now=now)
    lr.save_usage(week(20, now + 9000), "idle tab", now=now + 5)
    cached = lr.load_usage()["seven_day"]
    check("same window, lower figure: the higher is kept, seen anew",
          (cached["pct"], cached["at"]), (65, now + 5))
    lr.save_usage(week(5, now - 600), "idle tab", now=now + 6)
    check("a figure from an earlier window is ignored", lr.load_usage()["seven_day"]["pct"], 65)
    lr.save_usage(week(3, now + 9000 + 7 * 86400), "probe", now=now + 7)
    check("a later window replaces it", lr.load_usage()["seven_day"]["pct"], 3)
    check("no temp file left behind", glob.glob(lr.USAGE + ".tmp*"), [])
    r = {}
    lr.merge_reading(r, "seven_day", 30.0, 1000)
    lr.merge_reading(r, "seven_day", 10.0, 2000)
    check("variants: the reset of the highest figure binds", r["seven_day"], {"pct": 30.0, "resets_at": 1000})
    uw = lr.reading_from_stream([json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 5, "unifiedWindows": {
            "five_hour": {"utilization": 0.4, "resetsAt": 1790000000},
            "seven_day": {"utilization": 0.17, "resetsAt": 1790500000}}}})])
    check("unifiedWindows: both windows from one event",
          (uw["five_hour"]["pct"], round(uw["seven_day"]["pct"], 1), uw["seven_day"]["resets_at"]),
          (40.0, 17.0, 1790500000))
    check("usage card: \"Current session\" is the 5-hour window",
          lr.plan_reading({"windows": [{"label": "Current session", "percentUsed": 12}]}),
          {"five_hour": {"pct": 12.0}})
    for text in ("- JOB DONE", "JOB DONE \u2705", "> JOB DONE", "JOB DONE!", "Job done",
                 "Summary.\nJOB FAILED would only be used if X\nJOB DONE"):
        check("done: %r" % text, (lr.job_verdict(text) or ("?",))[0], "done")
    check("a marker that is not the last line does not count",
          lr.job_verdict("JOB DONE\nthen I kept going"), None)
    check("JOB FAILED on the last line, markdown stripped",
          lr.job_verdict("Tried.\n**JOB FAILED: no repo**")[:2], ("failed", "no repo"))
    check("JOB DONE verdict strips the marker",
          lr.job_verdict("All written.\n\n**JOB DONE**"), ("done", "", "All written."))
    check("JOB FAILED verdict keeps the reason",
          lr.job_verdict("Tried.\nJOB FAILED: repo missing")[:2], ("failed", "repo missing"))
    check("no marker, no verdict", lr.job_verdict("Should I use X or Y?"), None)

    # ---- J3. Remote Control session, queue order, JOB DONE -----------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J3. jobs start as Remote Control sessions, one at a time, by order")
    with open(os.path.join(folder, "_README.md"), "w") as fh:
        fh.write("---\nstatus: queued\n---\nignored\n")
    a = write_job(folder, "020 second.md", status="queued", order=20)
    b = write_job(folder, "010 first.md", "Audit the API.", status="queued", order=10,
                  tags="[work, job]")
    c = write_job(folder, "005 parked.md", status="hold", order=5)
    d = write_job(folder, "zzz no order.md", status="queued")
    set_usage(lr)
    lr.cmd_sweep([])
    check("session came up", wait_transcript(lr), True)
    argvs = job_argvs(root)
    check("one job started", len(argvs), 1)
    args = argvs[0] if argvs else []
    meta = lr.read_note(b)[0]
    check("lowest order first, hold skipped", meta.get("status"), "running")
    check("session_id + started recorded", bool(meta.get("session_id") and meta.get("started")), True)
    check("other frontmatter kept", meta.get("tags"), "[work, job]")
    flag = lambda f: args[args.index(f) + 1] if f in args else None
    check("interactive: the prompt is the first argument, no -p",
          (bool(args) and args[0].startswith("You are running unattended"), "-p" in args),
          (True, False))
    check("prompt has the rules, the JOB DONE contract and the body",
          bool(args) and "JOB DONE" in args[0] and "Audit the API." in args[0], True)
    check("--remote-control named after the job", flag("--remote-control"), "job: 010 first")
    check("--session-id is the note's", flag("--session-id"), meta.get("session_id"))
    check("Opus, bypassPermissions", (flag("--model"), flag("--permission-mode")),
          ("opus", "bypassPermissions"))
    check("runs on a terminal", open(os.path.join(root, "tty.txt")).read().split(), ["True"])
    check("runs in the default cwd",
          os.path.realpath(open(os.path.join(root, "cwd.txt")).read().strip()),
          os.path.realpath(work))
    settings = json.load(open(flag("--settings") or lr.JOB_SETTINGS))
    hook = settings["hooks"]["PreToolUse"][0]
    check("guard hook on every tool, failing closed",
          (hook["matcher"], hook["hooks"][0]["command"].endswith(" guard || exit 2")), ("*", True))
    check("job status line feeds the usage cache",
          settings.get("statusLine", {}).get("command", "").endswith(" statusline"), True)
    check("no bypass-mode dialog", settings.get("skipDangerousModePermissionPrompt"), True)
    run = running_run(lr)
    check("terminal output logged", wait_for(lambda: os.path.exists(run["pty"]) and
                                             "fake session screen" in open(run["pty"]).read()), True)
    check("start alert", alerts, ["Job started in Remote Control: 010 first"])
    first = dict(run)
    lr.cmd_sweep([])
    text = note(b)
    meta = lr.read_note(b)[0]
    check("JOB DONE -> done", (meta.get("status"), bool(meta.get("finished"))), ("done", True))
    check("Run section with the reply, marker stripped",
          "Report written to /vault/report.md" in text and "JOB DONE" not in text, True)
    check("body kept", "Audit the API." in text, True)
    check("finished session left open for the phone", lr.job_alive(first), True)
    st = lr.load_state()[lr.JOBS_KEY]
    check("tracked as open", [s["sid"] for s in st.get("open", [])], [first["sid"]])
    check("next job started in the same sweep", lr.read_note(a)[0].get("status"), "running")
    check("hold untouched", lr.read_note(c)[0].get("status"), "hold")
    check("unordered job still queued", lr.read_note(d)[0].get("status"), "queued")
    check("finish alert", "Job finished: 010 first" in alerts, True)
    wait_transcript(lr)
    lr.JOB_KEEP_OPEN = 0
    set_usage(lr)
    lr.cmd_sweep([])
    check("idle finished session closed", wait_for(lambda: not lr.job_alive(first)), True)
    wait_transcript(lr)
    set_usage(lr)
    lr.cmd_sweep([])
    check("unordered job runs last", lr.read_note(d)[0].get("status"), "done")
    check("no usage probe while the cache was fresh", probes, [])

    # ---- J4. paused job resumes before queued -------------------------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J4. paused job resumes first, in its own session",
        {"records": [ASKED, said("working on it", tool=True)], "sleep": 60})
    old = os.path.join(root, "projects", "-fake")
    os.makedirs(old)
    with open(os.path.join(old, "sess-job-p.jsonl"), "w") as fh:
        fh.write(json.dumps(ASKED) + "\n" + json.dumps(said("an older run\nJOB DONE")) + "\n")
    p = write_job(folder, "090 paused.md", status="paused", order=90, session_id="sess-job-p",
                  resumes=1, paused_why='"5-hour 75% >= 60% until Sat 19:10"')
    q = write_job(folder, "001 queued.md", status="queued", order=1)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_for(lambda: len(job_argvs(root)) == 1)
    args = (job_argvs(root) or [[]])[0]
    check("resumed with --resume and --remote-control",
          ("--resume" in args and args[args.index("--resume") + 1] == "sess-job-p",
           "--remote-control" in args), (True, True))
    check("resume prompt, not the full rules", args[0] if args else None, lr.JOB_RESUME_PROMPT)
    meta = lr.read_note(p)[0]
    check("resumes counted, paused_why cleared",
          (meta.get("status"), meta.get("resumes"), meta.get("paused_why")), ("running", "2", None))
    check("queued job waits", lr.read_note(q)[0].get("status"), "queued")
    _t.sleep(0.5)
    lr.cmd_sweep([])
    check("an older run's JOB DONE does not count", lr.read_note(p)[0].get("status"), "running")

    # ---- J5. cap reached mid-run -> session stopped, paused ------------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J5. 5-hour cap reached while running -> session stopped, paused",
        {"records": [ASKED, said("step 1", tool=True)], "sleep": 60})
    j = write_job(folder, "010 long.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    set_usage(lr, five=70)
    t0 = _t.time()
    lr.cmd_sweep([])
    meta = lr.read_note(j)[0]
    check("paused at the cap", meta.get("status"), "paused")
    check("paused_why names the cap",
          (meta.get("paused_why") or "").startswith("5-hour 70% >= 60%"), True)
    check("session stopped promptly", (lr.job_alive(run), _t.time() - t0 < 15), (False, True))
    child = int(open(run["pty"] + ".pid").read())
    check("the claude process itself is gone", wait_for(lambda: not lr.pid_alive(child)), True)
    check("no Run section for a pause", "## Run" in note(j), False)
    check("pause alert", any(x.startswith("Job paused: 010 long") for x in alerts), True)
    lr.cmd_sweep([])
    check("not resumed while the cap holds", len(job_argvs(root)), 1)

    # ---- J6. usage limit in the transcript -> paused ------------------------
    quota = {"type": "assistant", "error": "rate_limit", "apiErrorStatus": 429, "uuid": "q1",
             "timestamp": "2026-11-18T10:00:00Z",
             "quotaLimits": {"status": "rejected", "resetsAt": int(_t.time()) + 7200,
                             "rateLimitType": "five_hour"},
             "message": {"role": "assistant", "content": [{"type": "text", "text": "limit"}]}}
    root, lr, folder, work, alerts, probes = job_scenario(
        "J6. session hits the usage limit -> stopped and paused",
        {"records": [ASKED, said("step", tool=True), quota], "sleep": 60})
    j = write_job(folder, "010 x.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    set_usage(lr, five=65)                        # gate shut: nothing restarts this sweep
    lr.cmd_sweep([])
    meta = lr.read_note(j)[0]
    check("limit -> paused", meta.get("status"), "paused")
    check("paused_why says usage limit", (meta.get("paused_why") or "").startswith("usage limit until"),
          True)
    check("limited session stopped", wait_for(lambda: not lr.job_alive(run)), True)

    root, lr, folder, work, alerts, probes = job_scenario(
        "J6b. JOB FAILED -> failed with a Run section",
        {"records": [ASKED, said("Looked everywhere.\nJOB FAILED: repo not found")], "sleep": 60})
    j = write_job(folder, "010 y.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    set_usage(lr, five=65)
    lr.cmd_sweep([])
    check("JOB FAILED -> failed", lr.read_note(j)[0].get("status"), "failed")
    check("reply and reason in the note",
          "Looked everywhere." in note(j) and "repo not found" in note(j), True)
    check("failure alert", any(x.startswith("Job FAILED: 010 y") for x in alerts), True)

    root, lr, folder, work, alerts, probes = job_scenario(
        "J6c. session exits without JOB DONE -> paused, resumed later",
        {"records": [ASKED, said("half way", tool=True)], "sleep": 0})
    j = write_job(folder, "010 z.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    run = running_run(lr)
    wait_for(lambda: not lr.job_alive(run))
    set_usage(lr, five=65)
    lr.cmd_sweep([])
    meta = lr.read_note(j)[0]
    check("exit without marker -> paused", (meta.get("status"), meta.get("paused_why")),
          ("paused", "session exited before JOB DONE"))

    root, lr, folder, work, alerts, probes = job_scenario(
        "J6d. session waits on a question -> blocked, open; your answer finishes it",
        {"records": [ASKED, said("Which database, staging or UAT?")], "sleep": 60})
    j = write_job(folder, "010 q.md", status="queued", order=10)
    k = write_job(folder, "020 next.md", status="queued", order=20)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    lr.cmd_sweep([])
    check("not blocked before JOB_IDLE", lr.read_note(j)[0].get("status"), "running")
    lr.JOB_IDLE = 0
    set_usage(lr)
    lr.cmd_sweep([])
    check("idle on a question -> blocked", lr.read_note(j)[0].get("status"), "blocked")
    check("question in the note", "staging or UAT" in note(j), True)
    check("blocked session left open", lr.job_alive(run), True)
    check("alert asks for an answer", any("needs an answer" in x for x in alerts), True)
    check("queue moves on", lr.read_note(k)[0].get("status"), "running")
    path = lr.session_file(run["sid"])
    with open(path, "a") as fh:                    # you answer on the phone; it finishes
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "UAT"}}) + "\n")
        fh.write(json.dumps(said("Ran it on UAT.\nJOB DONE")) + "\n")
    lr.cmd_sweep([])
    check("blocked job finished after your answer", lr.read_note(j)[0].get("status"), "done")
    check("noted as after your answer", "after your answer" in note(j), True)

    root, lr, folder, work, alerts, probes = job_scenario(
        "J6e. session never writes a transcript -> failed, stopped", {"write": False, "sleep": 60})
    j = write_job(folder, "010 stuck.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    run = running_run(lr)
    lr.JOB_START_TIMEOUT = 0
    _t.sleep(0.2)
    lr.cmd_sweep([])
    check("no transcript -> failed", lr.read_note(j)[0].get("status"), "failed")
    check("says it did not start", "did not start" in note(j), True)
    check("stuck session stopped", wait_for(lambda: not lr.job_alive(run)), True)

    # ---- J7. gate closed / usage unknown / jobs off -------------------------
    root, lr, folder, work, alerts, probes = job_scenario("J7. nothing starts when the gate is shut")
    j = write_job(folder, "010 z.md", status="queued", order=10)
    set_usage(lr, week_reset_h=100)
    lr.cmd_sweep([])
    check("100 h before the weekly reset: not started", job_argvs(root), [])
    check("no probe when a reading can't open it", probes, [])
    os.remove(lr.USAGE)
    lr.cmd_sweep([])
    check("unknown usage: probed once", len(probes), 1)
    check("unknown usage: not started", job_argvs(root), [])
    lr.cmd_sweep([])
    check("probe throttled", len(probes), 1)
    check("gate reason logged", "usage unknown" in open(lr.LOG).read(), True)
    set_usage(lr)
    st = lr.load_state()
    st[lr.JOBS_KEY]["on"] = False
    lr.save_state(st)
    lr.cmd_sweep([])
    check("jobs off: not started", job_argvs(root), [])
    check("status command runs", lr.cmd_jobs(["status"]), 0)
    check("jobs on --reset stores the account's reset",
          (lr.cmd_jobs(["on", folder, "--reset", "Thu 14:00"]),
           lr.load_state()[lr.JOBS_KEY].get("weekly_reset")), (0, "Thu 14:00"))
    check("jobs on --reset rejects junk", lr.cmd_jobs(["on", "--reset", "someday"]), 2)
    check("jobs on --reset auto clears it",
          (lr.cmd_jobs(["on", "--reset", "auto"]), lr.load_state()[lr.JOBS_KEY].get("weekly_reset")),
          (0, None))

    # ---- J8. state lost while a job ran --------------------------------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J8. note says running but the runner lost it -> paused, then resumed")
    os.makedirs(os.path.join(root, "projects", "-fake"))
    with open(os.path.join(root, "projects", "-fake", "sess-lost.jsonl"), "w") as fh:
        fh.write(json.dumps(ASKED) + "\n")
    write_job(folder, "010 lost.md", status="running", order=10, session_id="sess-lost")
    set_usage(lr)
    lr.cmd_sweep([])
    wait_for(lambda: len(job_argvs(root)) == 1)
    args = (job_argvs(root) or [[]])[0]
    check("lost job resumed with its session",
          "--resume" in args and args[args.index("--resume") + 1] == "sess-lost", True)

    # ---- J8b. untrusted folder ------------------------------------------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J8b. untrusted folder -> failed before launch, says how to fix", trusted=False)
    j = write_job(folder, "010 t.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    check("nothing launched", job_argvs(root), [])
    check("failed with the trust hint",
          (lr.read_note(j)[0].get("status"), "accept the trust" in note(j)), ("failed", True))

    # ---- J9. job sessions are never auto-resumed ----------------------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J9. auto mode leaves job and probe transcripts alone")
    write_transcript(root, "sess-jobbed", "quota", reset_delta=-600)
    write_transcript(root, "sess-plain", "quota", reset_delta=-600)
    pd = os.path.join(root, "projects", lr.project_dir_name(lr.TOOLS))
    os.makedirs(pd)
    shutil.copy(os.path.join(root, "projects", "-test-project", "sess-plain.jsonl"),
                os.path.join(pd, "sess-probe.jsonl"))
    st = lr.load_state()
    st[lr.CONFIG]["auto_all"] = True
    st[lr.JOBS_KEY]["sids"] = ["sess-jobbed"]
    lr.save_state(st)
    ids = [sid for sid, _p, _b in lr.candidates(lr.load_state())]
    check("only the ordinary session is a candidate", ids, ["sess-plain"])
    lr.cmd_disarm(["all"])
    check("disarm all keeps the job queue", lr.load_state().get(lr.JOBS_KEY, {}).get("on"), True)

    # ---- J12. priority: top runs before the rest of the backlog ------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J12. `priority: top` notes run before paused and queued normal ones",
        {"records": [ASKED, said("working on it", tool=True)], "sleep": 60})
    n_paused = write_job(folder, "001 normal paused.md", status="paused", order=1)
    n_queued = write_job(folder, "002 normal queued.md", status="queued", order=2)
    t_queued = write_job(folder, "050 top queued.md", status="queued", order=50, priority="top")
    t_later = write_job(folder, "060 top later.md", status="queued", order=60, priority="Top")
    set_usage(lr)
    lr.cmd_sweep([])
    check("top note started first", lr.read_note(t_queued)[0].get("status"), "running")
    check("normal notes wait", (lr.read_note(n_paused)[0].get("status"),
                                lr.read_note(n_queued)[0].get("status"), lr.read_note(t_later)[0].get("status")),
          ("paused", "queued", "queued"))
    jobs = lr.job_notes(folder)
    for j in jobs:
        if j["path"] == t_queued:
            j["status"] = "done"
    check("then the next top note, before normal paused",
          lr.pick_job(jobs)["path"], t_later)
    for j in jobs:
        if j["path"] == t_later:
            j["status"] = "done"
    check("then normal: paused before queued", lr.pick_job(jobs)["path"], n_paused)

    # ---- J13. paused job over 400k tokens restarts in a new session ---------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J13. paused job with > 400k context restarts in a new session",
        {"records": [ASKED, said("working on it", tool=True)], "sleep": 60})
    old = os.path.join(root, "projects", "-fake")
    os.makedirs(old)
    big = said("halfway", tool=True)
    big["message"]["usage"] = {"input_tokens": 2, "cache_read_input_tokens": 440000,
                               "cache_creation_input_tokens": 5000, "output_tokens": 900}
    stub = {"type": "assistant", "error": "rate_limit",
            "message": {"role": "assistant", "content": "limit",
                        "usage": {"input_tokens": 0, "output_tokens": 0}}}
    with open(os.path.join(old, "sess-big.jsonl"), "w") as fh:
        for r in (ASKED, big, stub):
            fh.write(json.dumps(r) + "\n")
    check("context counts input + cache, skips the zero stub",
          lr.context_tokens(os.path.join(old, "sess-big.jsonl")), 445002)
    p = write_job(folder, "010 big.md", "Audit it.", status="paused", order=10,
                  session_id="sess-big", resumes=2)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_for(lambda: len(job_argvs(root)) == 1)
    args = (job_argvs(root) or [[]])[0]
    meta = lr.read_note(p)[0]
    check("no --resume, a new --session-id",
          ("--resume" in args, "--session-id" in args and
           args[args.index("--session-id") + 1] not in ("", "sess-big")), (False, True))
    check("full rules + body + the earlier session named",
          bool(args) and args[0].startswith("You are running unattended") and
          "Audit it." in args[0] and "sess-big" in args[0], True)
    check("note tracks the switch",
          (meta.get("status"), meta.get("previous_session"), meta.get("resumes"),
           meta.get("session_id") == args[args.index("--session-id") + 1] if args else None),
          ("running", "sess-big", "3", True))
    check("Run section records why", "(new session)" in note(p) and "445k tokens" in note(p), True)
    check("new sid kept off auto-resume",
          meta.get("session_id") in lr.load_state()[lr.JOBS_KEY]["sids"], True)
    check("restart alert", alerts[-1:], ["Job restarted in a new session in Remote Control: 010 big"])

    # ---- J14. one task at a time: resume waits for a running job ------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J14. a due chat resume waits for the running job; jobs wait for a due resume",
        {"records": [ASKED, said("working on it", tool=True)], "sleep": 60})
    resumed = []
    lr.run_resume = lambda sid, entry, path: resumed.append(sid) or False
    j1 = write_job(folder, "010 one.md", status="queued", order=10)
    j2 = write_job(folder, "020 two.md", status="queued", order=20)
    set_usage(lr)
    lr.cmd_sweep([])
    check("job running", lr.read_note(j1)[0].get("status"), "running")
    wait_transcript(lr)
    write_transcript(root, "sess-chat")
    st = lr.load_state()
    st[lr.CONFIG]["auto_all"] = True
    lr.save_state(st)
    set_usage(lr)
    lr.cmd_sweep([])
    check("resume not launched while the job runs", resumed, [])
    check("block not marked handled", lr.load_state().get("sess-chat"), None)
    check("waiting logged", "waiting for job 010 one" in open(lr.LOG).read(), True)
    run = running_run(lr)
    lr.job_stop(run)                                   # job ends (pauses) on its own
    lr.cmd_sweep([])
    check("job ended -> resume runs in this sweep", resumed, ["sess-chat"])
    check("next job did not start alongside the resume",
          (lr.read_note(j1)[0].get("status"), lr.read_note(j2)[0].get("status")),
          ("paused", "queued"))
    set_usage(lr)
    lr.cmd_sweep([])
    check("resume handled -> backlog continues (paused job first)",
          lr.read_note(j1)[0].get("status"), "running")

    # ---- J15. a resumed chat still working blocks the backlog ---------------
    root, lr, folder, work, alerts, probes = job_scenario("J15. busy resumed chat blocks job starts")
    j1 = write_job(folder, "010 one.md", status="queued", order=10)
    chat = write_transcript(root, "sess-busy", kind="normal")
    st = lr.load_state()
    st[lr.CONFIG]["active_resume"] = {"sid": "sess-busy", "at": 0}
    lr.save_state(st)
    set_usage(lr)
    lr.cmd_sweep([])
    check("chat transcript fresh -> job waits", lr.read_note(j1)[0].get("status"), "queued")
    old_t = os.path.getmtime(chat) - lr.RESUME_BUSY - 60
    os.utime(chat, (old_t, old_t))
    set_usage(lr)
    lr.cmd_sweep([])
    check("chat quiet for RESUME_BUSY -> job starts", lr.read_note(j1)[0].get("status"), "running")
    check("busy marker cleared", lr.load_state()[lr.CONFIG].get("active_resume"), None)

    # ---- J16. chat over 700k continues in a new Remote Control session -------
    root, lr, folder, work, alerts, probes = job_scenario("J16. chat > 700k -> new session")
    spawned = []
    lr.job_spawn = lambda argv, pty, cwd, job=True: spawned.append((argv, job)) or 4242
    resumed = []
    lr.run_resume = lambda sid, entry, path: resumed.append(sid) or False
    path = write_transcript(root, "sess-huge")
    lines = open(path).read().splitlines()
    huge = said("deep in it")
    huge["message"]["usage"] = {"input_tokens": 1, "cache_read_input_tokens": 720000,
                                "cache_creation_input_tokens": 1000}
    lines.insert(1, json.dumps(huge))
    open(path, "w").write("\n".join(lines) + "\n")
    small = write_transcript(root, "sess-small")
    os.utime(small, (os.path.getmtime(path) - 100,) * 2)   # older: resumed second
    st = lr.load_state()
    st[lr.CONFIG]["auto_all"] = True
    lr.save_state(st)
    j1 = write_job(folder, "010 one.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    argv, as_job = spawned[0] if spawned else ([], None)
    entry = lr.load_state().get("sess-huge", {})
    check("new session spawned, not resumed", (len(spawned), "sess-huge" in resumed), (1, False))
    check("new --session-id + Remote Control, not a job",
          ("--session-id" in argv, "--remote-control" in argv, "--resume" in argv, as_job),
          (True, True, False, False))
    check("handoff names the old session and the first request",
          bool(argv) and "sess-huge" in argv[1] and "do the thing" in argv[1], True)
    check("old session retired, points at the new one",
          (entry.get("done"), entry.get("continued_in") == argv[argv.index("--session-id") + 1]
           if argv else None), (True, True))
    check("only one task: the smaller chat waits", resumed, [])
    check("new chat is the running task",
          (lr.load_state()[lr.CONFIG].get("active_resume") or {}).get("sid"),
          entry.get("continued_in"))
    check("job waits too", lr.read_note(j1)[0].get("status"), "queued")
    check("alert names the size", any("721k tokens" in a for a in alerts), True)

    # ---- audit batch 3 ------------------------------------------------------
    import io, contextlib
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())

    def gone(pid):
        return wait_for(lambda: not lr.pid_alive(pid) or subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
            text=True).stdout.strip().startswith("Z"))

    def transcript(root, sid, recs):
        d = os.path.join(root, "projects", "-fake")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
        return os.path.join(d, sid + ".jsonl")
    WORKING = {"records": [ASKED, said("working on it", tool=True)], "sleep": 60}

    # B3-H2: a running job without a fresh reading is paused; one alert
    root, lr, folder, work, alerts, probes = job_scenario(
        "J17. running job, usage goes stale and the probe fails -> paused, alerted once", WORKING)
    j = write_job(folder, "010 a.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    set_usage(lr, age=lr.USAGE_MAX_AGE + 60)
    lr.cmd_sweep([])
    meta = lr.read_note(j)[0]
    check("paused, not run blind", (meta.get("status"), (meta.get("paused_why") or "")[:13]),
          ("paused", "usage unknown"))
    check("its session stopped", wait_for(lambda: not lr.job_alive(run)), True)
    lr.cmd_sweep([])
    check("not resumed on the stale reading", len(job_argvs(root)), 1)
    check("probe failure alerted once", sum("Usage could not be read" in a for a in alerts), 1)

    # B3-H3: a lost session is adopted, never resumed a second time
    root, lr, folder, work, alerts, probes = job_scenario(
        "J18. runner state lost while the session runs -> adopted, not a second session", WORKING)
    a = write_job(folder, "010 a.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    check("start saved with its pid", bool(run and run.get("pid")), True)
    st = lr.load_state()
    del st[lr.JOBS_KEY]["running"]
    lr.save_state(st)
    set_usage(lr)
    lr.cmd_sweep([])
    again = running_run(lr) or {}
    check("adopted: same session and helper, nothing relaunched",
          (again.get("sid"), again.get("pid"), len(job_argvs(root))), (run["sid"], run["pid"], 1))
    check("note still running", lr.read_note(a)[0].get("status"), "running")
    st = lr.load_state()
    st[lr.JOBS_KEY]["running"]["pid"] = None           # killed between launch and save
    lr.save_state(st)
    lr.cmd_sweep([])
    check("a run without a pid finds its helper", (running_run(lr) or {}).get("pid"), run["pid"])
    sid2 = "sess-second"
    b = write_job(folder, "020 b.md", status="running", order=20, session_id=sid2)
    pid2 = lr.job_spawn([os.path.join(root, "bin", "claude"), "x", "--session-id", sid2,
                         "--remote-control", "j"], os.path.join(lr.JOB_LOGS, sid2 + ".pty.log"), work)
    wait_for(lambda: lr.session_file(sid2))
    st = lr.load_state()
    del st[lr.JOBS_KEY]["running"]
    lr.save_state(st)
    lr.cmd_sweep([])
    check("two live sessions: the first adopted", (running_run(lr) or {}).get("sid"), run["sid"])
    check("the second stopped and paused",
          (wait_for(lambda: not lr.job_alive({"pid": pid2, "sid": sid2})),
           lr.read_note(b)[0].get("status"), "second session" in (lr.read_note(b)[0].get("paused_why") or "")),
          (True, "paused", True))
    lr.job_stop(running_run(lr))

    root, lr, folder, work, alerts, probes = job_scenario(
        "J19. a paused job whose session is open elsewhere is not resumed", WORKING)
    transcript(root, "sess-open", [ASKED, said("half", tool=True)])
    p = write_job(folder, "010 p.md", status="paused", order=10, session_id="sess-open")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--resume", "sess-open"])
    set_usage(lr)
    lr.cmd_sweep([])
    _t.sleep(0.5)
    check("not resumed while another process has it", job_argvs(root), [])
    check("logged once", open(lr.LOG).read().count("is open in pid %d" % holder.pid), 1)
    holder.kill()
    holder.wait()
    set_usage(lr)
    lr.cmd_sweep([])
    check("resumed once it is free", wait_for(lambda: len(job_argvs(root)) == 1), True)

    # B3-H4: answering a blocked job makes it the running job again
    root, lr, folder, work, alerts, probes = job_scenario(
        "J20. you answer a blocked job -> it runs again, the job meanwhile is paused, caps apply",
        {"records": [ASKED, said("Which database, staging or UAT?")], "sleep": 60})
    j = write_job(folder, "010 q.md", status="queued", order=10)
    k = write_job(folder, "020 next.md", status="queued", order=20)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    blocked = running_run(lr)
    lr.JOB_IDLE = 0
    set_usage(lr)
    lr.cmd_sweep([])
    check("blocked, the queue moved on",
          (lr.read_note(j)[0].get("status"), lr.read_note(k)[0].get("status")), ("blocked", "running"))
    wait_transcript(lr)
    other = running_run(lr)
    with open(lr.session_file(blocked["sid"]), "a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "UAT"}}) + "\n")
    set_usage(lr)
    lr.cmd_sweep([])
    check("answered job is the running job again",
          ((running_run(lr) or {}).get("sid"), lr.read_note(j)[0].get("status")),
          (blocked["sid"], "running"))
    check("the job running meanwhile is paused and stopped",
          (lr.read_note(k)[0].get("status"), wait_for(lambda: not lr.job_alive(other))), ("paused", True))
    check("no longer an open session", lr.load_state()[lr.JOBS_KEY].get("open"), [])
    set_usage(lr, five=70)
    lr.cmd_sweep([])
    check("caps apply to it", (lr.read_note(j)[0].get("paused_why") or "")[:10], "5-hour 70%")

    # B3-M1: a quota stop holds the job until its reset; an idle resume is not counted
    root, lr, folder, work, alerts, probes = job_scenario(
        "J21. quota stop -> paused_until; a resume that did no work is not counted",
        {"records": [quota], "sleep": 60})
    path = transcript(root, "sess-lim", [ASKED, said("half", tool=True)])
    p = write_job(folder, "010 lim.md", status="paused", order=10, session_id="sess-lim", resumes=2)
    set_usage(lr)
    lr.cmd_sweep([])
    check("resumed, counted", lr.read_note(p)[0].get("resumes"), "3")
    wait_for(lambda: "rate_limit" in open(path).read())
    set_usage(lr)
    lr.cmd_sweep([])
    meta = lr.read_note(p)[0]
    check("paused at the stop, until its reset, resume not counted",
          (meta.get("status"), bool(meta.get("paused_until")), meta.get("resumes")), ("paused", True, "2"))
    set_usage(lr)
    lr.cmd_sweep([])
    check("gate open, but it waits for the stop's reset",
          (len(job_argvs(root)), "waits for its usage limit to reset" in open(lr.LOG).read()), (1, True))
    lr.update_note(p, {"paused_until": lr.stamp(_t.time() - 120)})
    set_usage(lr)
    lr.cmd_sweep([])
    check("resumed after the reset", (wait_for(lambda: len(job_argvs(root)) == 2),
                                      lr.read_note(p)[0].get("paused_until")), (True, None))

    # B3-M2, L9: a fresh start resets the count and sends no old Run sections
    root, lr, folder, work, alerts, probes = job_scenario(
        "J22. re-queued failed job starts clean; paused without a session; transcript gone", WORKING)
    r = write_job(folder, "010 retry.md", "Do the job.\n\n## Run 2026-11-01T10:00 → 2026-11-01T11:00 (failed)\n\nold output",
                  status="queued", order=10, resumes=8, previous_session="sess-old")
    set_usage(lr)
    lr.cmd_sweep([])
    wait_for(lambda: len(job_argvs(root)) == 1)
    meta = lr.read_note(r)[0]
    args = (job_argvs(root) or [[""]])[0]
    check("resumes and previous_session cleared", (meta.get("resumes"), meta.get("previous_session")),
          (None, None))
    check("prompt has the body, not the old Run sections",
          ("Do the job." in args[0], "old output" in args[0]), (True, False))
    check("runner recorded", meta.get("runner"), lr.runner_name())
    lr.job_stop(running_run(lr))
    lr.update_note(r, {"status": "hold"})
    st = lr.load_state()
    st[lr.JOBS_KEY].pop("running", None)
    lr.save_state(st)
    g = write_job(folder, "020 gone.md", status="paused", order=20, session_id="sess-vanished", resumes=1)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_for(lambda: len(job_argvs(root)) == 2)
    meta = lr.read_note(g)[0]
    args = (job_argvs(root) or [[]])[-1]
    check("transcript gone -> a new session, not --resume",
          ("--resume" in args, meta.get("previous_session"), "its transcript is gone" in note(g)),
          (False, "sess-vanished", True))
    lr.job_stop(running_run(lr))
    lr.update_note(g, {"status": "hold"})
    st = lr.load_state()
    st[lr.JOBS_KEY].pop("running", None)
    lr.save_state(st)
    write_job(folder, "030 nosid.md", status="paused", order=30)
    set_usage(lr)
    lr.cmd_sweep([])
    check("paused without session_id: logged, started fresh",
          "030 nosid is paused but has no session_id" in open(lr.LOG).read(), True)
    lr.job_stop(running_run(lr))

    # B3-M3: a stop takes the session's whole tree, also when the helper died
    for helper_dead in (False, True):
        root, lr, folder, work, alerts, probes = job_scenario(
            "J23%s. stubborn session with a detached grandchild -> all stopped%s"
            % ("b" if helper_dead else "", ", helper already dead" if helper_dead else ""),
            {"records": [ASKED, said("x", tool=True)], "sleep": 60, "stubborn": True,
             "grandchild": True})
        write_job(folder, "010 s.md", status="queued", order=10)
        set_usage(lr)
        lr.cmd_sweep([])
        wait_transcript(lr)
        run = running_run(lr)
        kid_file = os.path.join(root, "grandchild.pid")
        wait_for(lambda: os.path.exists(kid_file) and open(kid_file).read().strip())
        wait_for(lambda: os.path.exists(run["pty"] + ".pid"))
        kid, child = int(open(kid_file).read()), int(open(run["pty"] + ".pid").read())
        if helper_dead:
            os.kill(run["pid"], signal.SIGKILL)
            lr._SPAWNED[run["pid"]].wait()
            check("claude outlives its helper (ignores SIGHUP)", lr.pid_alive(child), True)
        lr.job_stop(run)
        check("claude stopped", gone(child), True)
        check("its detached grandchild stopped", gone(kid), True)

    # B3-M4: jobs stop; a status you set while it runs is kept
    root, lr, folder, work, alerts, probes = job_scenario("J24. jobs stop, and hold set in the note", WORKING)
    j = write_job(folder, "010 a.md", status="queued", order=10)
    k = write_job(folder, "020 b.md", status="queued", order=20)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    with quiet():
        code = lr.cmd_jobs(["stop"])
    check("jobs stop: session stopped, note on hold, nothing running",
          (code, lr.job_alive(run), lr.read_note(j)[0].get("status"), running_run(lr)), (0, False, "hold", None))
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    run = running_run(lr)
    check("next job runs", lr.read_note(k)[0].get("status"), "running")
    lr.update_note(k, {"status": "hold"})
    set_usage(lr)
    lr.cmd_sweep([])
    check("hold set in the note: stopped, your status kept",
          (wait_for(lambda: not lr.job_alive(run)), lr.read_note(k)[0].get("status"),
           any(x.startswith("Job stopped: 020 b") for x in alerts)), (True, "hold", True))

    # B3-M9: notes of another runner are left alone; done -> queued is reported
    root, lr, folder, work, alerts, probes = job_scenario("J25. another machine's notes", WORKING)
    o1 = write_job(folder, "010 o1.md", status="paused", order=10, session_id="sess-x", runner="other-mac")
    o2 = write_job(folder, "020 o2.md", status="running", order=20, session_id="sess-y", runner="other-mac")
    d = write_job(folder, "030 d.md", status="done", order=30)
    set_usage(lr)
    lr.cmd_sweep([])
    check("their paused and running notes untouched, nothing started",
          (lr.read_note(o1)[0].get("status"), lr.read_note(o2)[0].get("status"), job_argvs(root)),
          ("paused", "running", []))
    lr.update_note(d, {"status": "queued"})
    set_usage(lr)
    lr.cmd_sweep([])
    check("done -> queued alerted, then run", (any("went from done back to queued" in x for x in alerts),
                                               lr.read_note(d)[0].get("status")), (True, "running"))
    lr.job_stop(running_run(lr))

    # B3-L1: frontmatter edge cases
    print("\nJ26. frontmatter edge cases (B3-L1)")
    with open(os.path.join(folder, "040 crlf.md"), "wb") as fh:
        fh.write("﻿---\r\nstatus: queued # later\r\norder: nan\r\n--- not the end\r\n---\r\nbody\r\n".encode())
    jobs = {j["name"]: j for j in lr.job_notes(folder)}
    check("BOM, CRLF, a comment and a --- line inside: read",
          (jobs["040 crlf.md"]["status"], jobs["040 crlf.md"]["body"].strip()), ("queued", "body"))
    check("order nan sorts with the unordered", lr.job_notes(folder)[-1]["name"], "040 crlf.md")
    write_job(folder, "050 typo.md", status="queud", order=50)
    st = lr.load_state()
    st[lr.JOBS_KEY].pop("running", None)
    lr.save_state(st)
    set_usage(lr, five=70)
    lr.cmd_sweep([])
    lr.cmd_sweep([])
    check("unknown status logged once", open(lr.LOG).read().count("status 'queud' is not one of"), 1)

    # B3-L4: the weekly reset passing pauses a running job
    root, lr, folder, work, alerts, probes = job_scenario("J27. the weekly reset passes while it runs", WORKING)
    j = write_job(folder, "010 a.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    check("reset recorded at start", bool(running_run(lr).get("week_resets")), True)
    st = lr.load_state()
    st[lr.JOBS_KEY]["running"]["week_resets"] = _t.time() - 1
    lr.save_state(st)
    set_usage(lr, week_reset_h=7 * 24 - 1)        # the new week: the gate is shut again
    lr.cmd_sweep([])
    check("paused at the reset", lr.read_note(j)[0].get("paused_why"), "the weekly reset passed while it ran")

    # B3-L6: an open session whose transcript vanished does not stop the runner
    root, lr, folder, work, alerts, probes = job_scenario("J28. open session's transcript vanishes")
    write_job(folder, "010 a.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    lr.cmd_sweep([])
    sess = lr.load_state()[lr.JOBS_KEY]["open"][0]
    os.remove(lr.session_file(sess["sid"]))
    lr.cmd_sweep([])
    check("no crash, the session closed",
          ("CRASH" in open(lr.LOG).read(), lr.load_state()[lr.JOBS_KEY]["open"],
           wait_for(lambda: not lr.job_alive(sess))), (False, [], True))

    # B3-L7: job-settings.json that cannot be written -> nothing starts
    root, lr, folder, work, alerts, probes = job_scenario("J29. job settings cannot be written")
    j = write_job(folder, "010 a.md", status="queued", order=10)
    os.makedirs(lr.JOB_SETTINGS)
    set_usage(lr)
    lr.cmd_sweep([])
    check("not started, note untouched, logged",
          (job_argvs(root), lr.read_note(j)[0].get("status"), "cannot write" in open(lr.LOG).read()),
          ([], "queued", True))

    # B3-M5, M6: doctor sees job health and a scheduled task that never ran
    root, lr, folder, work, alerts, probes = job_scenario("J30. doctor: job health, scheduled maintenance")
    write_job(folder, "010 a.md", status="queued", order=10)
    now = _t.time()
    js = {"on": True, "dir": folder, "probe_error": "probe failed: x",
          "crash": {"at": now, "what": "ZeroDivisionError"},
          "running": {"pid": 999999, "sid": "s", "title": "t", "started_at": now - 9 * 3600},
          "open": [{"pid": 1, "sid": "o", "title": "open one", "status": "blocked"}]}
    lines = dict(lr.job_health(js, now))
    check("crash, probe error, dead pid, past JOB_MAX_H, stale usage: attention",
          sorted(k.split(" ")[0] + " " + k.split(" ")[1] for k, bad in lines.items() if bad),
          ["job runner", "running job", "running job's", "usage probe", "usage reading"])
    check("open sessions listed", any("open session: open one" in k for k in lines), True)
    st = {lr.CONFIG: {"last_sweep": datetime.datetime.now().isoformat(timespec="seconds"),
                      "last_sweep_at": now}}
    lr.maintain(st, now - 9 * 86400, "install", {"windows": []})
    lr.maintain(st, now, "install", {"windows": []})
    lr.save_state(st)
    lr.scheduler_status = lambda: ("launchd : loaded", True, False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lr.cmd_doctor([])
    check("install runs do not hide a scheduled task that never ran",
          (code, "scheduled task: never ran STALE" in buf.getvalue()), (1, True))
    st = lr.load_state()
    lr.maintain(st, now, "scheduled", {"windows": []})
    lr.save_state(st)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_doctor([])
    check("a scheduled run clears it", ("STALE" in buf.getvalue(), "scheduled task: last ran" in buf.getvalue()),
          (False, True))
    lr.jobs_tick = lambda *a, **k: 1 / 0
    st = lr.load_state()
    st[lr.JOBS_KEY] = {"on": True, "dir": folder}
    lr.save_state(st)
    with quiet():
        lr.cmd_sweep([])
        lr.cmd_sweep([])
    check("jobs crash recorded, alerted once",
          (bool(lr.load_state()[lr.JOBS_KEY].get("crash")),
           sum("job runner crashed" in x for x in alerts)), (True, 1))

    # ---- J11. weekly maintenance, run by the Claude scheduled task ----------
    root, lr, folder, work, alerts, probes = job_scenario(
        "J11. weekly maintenance from the app's usage card; task-spec follows the reset")
    now = _t.time()
    thu = lr.next_weekly("Thu 13:00", now)
    iso = lambda t: datetime.datetime.fromtimestamp(t - 0.37, datetime.timezone.utc).isoformat()
    plan = {"status": "ok", "plan": "Pro", "windows": [
        {"label": "5-hour limit", "percentUsed": 70, "resetsAt": iso(now + 7200)},
        {"label": "Weekly \u00b7 all models", "percentUsed": 86, "resetsAt": iso(thu)},
        {"label": "Weekly \u00b7 Opus", "percentUsed": 40, "resetsAt": iso(thu)}]}
    r = lr.plan_reading(plan)
    check("usage card -> 5-hour and weekly (worst weekly binds)",
          (r["five_hour"]["pct"], r["seven_day"]["pct"]), (70.0, 86.0))
    check("cron: 10 min after Thu 13:00 (reset reported as 12:59:59.6)",
          lr.task_cron(r["seven_day"]["resets_at"]), "10 13 * * 4")
    check("cron: Sunday reset -> weekday 0", lr.task_cron(lr.next_weekly("Sun 23:55", now)), "5 0 * * 1")
    check("cron default before any reset is known", lr.task_cron(None), lr.TASK_DEFAULT_CRON)
    write_job(folder, "010 a.md", status="done", order=10, finished=lr.stamp(now - 3600))
    write_job(folder, "020 b.md", status="queued", order=20)
    write_job(folder, "030 c.md", status="failed", order=30)
    os.makedirs(lr.JOB_LOGS)
    oldlog = os.path.join(lr.JOB_LOGS, "sess-old.pty.log")
    newlog = os.path.join(lr.JOB_LOGS, "sess-new.pty.log")
    for f in (oldlog, newlog):
        open(f, "w").write("x")
    os.utime(oldlog, (now - 40 * 86400, now - 40 * 86400))
    write_transcript(root, "sess-gone-old", "normal")
    gone = os.path.join(root, "projects", "-test-project", "sess-gone-old.jsonl")
    os.utime(gone, (now - 40 * 86400, now - 40 * 86400))
    st = lr.load_state()
    st["sess-gone-old"] = {"prompt": "x", "resumes": 1, "handled": []}
    st["sess-missing"] = {"prompt": "x", "resumes": 1, "handled": []}
    lr.save_state(st)
    real_stdin = sys.stdin
    sys.stdin = __import__("io").StringIO(json.dumps({"plan": plan}))
    try:
        rc = lr.cmd_maintain(["--scheduled", "--usage-json", "-"])
    finally:
        sys.stdin = real_stdin
    check("scheduled run with the usage card on stdin", rc, 0)
    check("no probe when the app gave usage", probes, [])
    st = lr.load_state()
    js = st[lr.JOBS_KEY]
    check("weekly reset recorded, on the minute", js.get("reset_seen"), thu)
    check("usage cached from the app", lr.load_usage()["seven_day"]["source"], "app")
    summary = alerts[-1] if alerts else ""
    check("summary: run kind, reset day, weekly use, queue",
          ("(scheduled)" in summary, "weekly reset Thu" in summary and "13:00" in summary,
           "weekly 86% used" in summary, "1 done this week" in summary, "1 queued" in summary),
          (True, True, True, True, True))
    check("old job log removed, recent kept", (os.path.exists(oldlog), os.path.exists(newlog)),
          (False, True))
    check("dead and long-idle sessions forgotten",
          ("sess-gone-old" in st, "sess-missing" in st), (False, False))
    check("bad --usage-json refused", lr.cmd_maintain(["--usage-json", "{nope"]), 2)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_task_spec([])
    spec = json.loads(buf.getvalue())
    check("task-spec: id, cron after the recorded reset",
          (spec["taskId"], spec["cronExpression"]), (lr.TASK_ID, "10 13 * * 4"))
    check("task-spec prompt reads usage and reschedules itself",
          all(k in spec["prompt"] for k in ("get_usage", "maintain --scheduled --usage-json -",
                                             "task-spec", "update_scheduled_task")), True)
    check("task-spec: the same schedule with CRON_TZ, for a Claude Code scheduled task",
          spec["cron"], "CRON_TZ=%s 10 13 * * 4" % spec["timezone"] if spec["timezone"] else None)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    try:
        check("local_zone: TZ wins", lr.local_zone(), "Europe/Paris")
    finally:
        os.environ.pop("TZ") if old_tz is None else os.environ.__setitem__("TZ", old_tz)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_task_setup([])
    setup = buf.getvalue()
    check("task-setup: the prompt verbatim, both tools, this host, run once",
          (lr.TASK_PROMPT in setup, "create_trigger" in setup, "create_scheduled_task" in setup,
           lr.socket.gethostname() in setup, "fire_trigger" in setup), (True,) * 5)
    zone = lr.local_zone
    lr.local_zone = lambda: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_task_setup([])
    lr.local_zone = zone
    check("task-setup: unknown zone -> asks the human",
          "CRON_TZ=<ask the human for this machine's IANA time zone> 10 13 * * 4" in buf.getvalue(), True)
    check("check-up prompt: safe repairs only, report, never --force",
          all(k in lr.TASK_PROMPT for k in ("launchctl kickstart", "systemctl --user start",
                                             "rc on", "pull --ff-only", "Never --force",
                                             "lr notify", "Never commit, push")), True)
    # a reset handed out moves the day: the next run records it, flags it, moves the cron
    moved = thu + 2 * 86400 + 3 * 3600                      # Saturday 16:00
    plan["windows"][1]["resetsAt"] = iso(moved)
    plan["windows"][2]["resetsAt"] = iso(moved)
    st = lr.load_state()
    lr.maintain(st, now, "scheduled", plan)
    lr.save_state(st)
    check("moved reset flagged", "(MOVED)" in alerts[-1], True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_task_spec([])
    check("task-spec moves with it", json.loads(buf.getvalue())["cronExpression"], "10 16 * * 6")
    lr.probe_usage = lambda: (None, "probe failed: OAuth session expired")
    lr.maintain(st, now, "scheduled", None)
    check("no usage card and probe fails: reported, last reset kept",
          ("usage read failed" in alerts[-1], st[lr.JOBS_KEY].get("reset_seen")), (True, moved))
    check("the sweep never runs maintenance itself", "maintain(" in open(TOOL).read().split(
        "def cmd_sweep")[1].split("def scheduler_status")[0], False)

    # ---- J10. guard hook + status line feed ---------------------------------
    print("\nJ10. guard hook and status line")

    def run_tool(args, stdin, home):
        env = dict(os.environ, HOME=home)
        return subprocess.run([sys.executable, TOOL] + args, input=stdin, env=env,
                              capture_output=True, text=True, timeout=30)
    home = tempfile.mkdtemp(prefix="lrhome-")
    os.makedirs(os.path.join(home, ".claude", "tools"))
    bash = lambda c: json.dumps({"tool_name": "Bash", "tool_input": {"command": c}})
    for cmd, want in (("git commit -m x", 2), ("cd repo && git -C . push origin main", 2),
                      ("git status && git diff", 0), ("echo commit", 0),
                      ("ls; git add . && git commit -am wip", 2), ("git -c alias.c=commit c -m x", 2),
                      ("git log --oneline -3", 0)):
        check("guard %r -> %d" % (cmd, want), run_tool(["guard"], bash(cmd), home).returncode, want)
    check("guard ignores other tools",
          run_tool(["guard"], json.dumps({"tool_name": "Edit", "tool_input": {}}), home).returncode, 0)
    blocked = run_tool(["guard"], bash("git push"), home)
    check("guard explains itself", "must not git commit" in blocked.stderr, True)
    check("guard: bad JSON is blocked (fails closed)", run_tool(["guard"], "{nope", home).returncode, 2)
    check("guard: no tool_name is blocked", run_tool(["guard"], "{}", home).returncode, 2)
    hook = json.load(open(lr.job_settings()))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    ok_in = bash("git status")
    as_hook = lambda cmd: subprocess.run(["/bin/sh", "-c", cmd], input=ok_in, env=dict(os.environ, HOME=home),
                                         capture_output=True, text=True, timeout=30).returncode
    check("hook command allows read-only git", as_hook(hook), 0)
    check("hook command blocks when its python is gone",
          as_hook(hook.replace(shlex.quote(sys.executable), "/gone/python3", 1)), 2)

    print("\nJ10b. guard: read-only git allowed, every audited bypass blocked (B3-H5)")
    glr = load_tool(home, os.path.join(home, "argv.txt"))
    glr.TOOLS = os.path.join(glr.HOME, ".claude", "tools")
    repo = os.path.join(home, "repo")
    os.makedirs(repo)
    open(os.path.join(repo, "c.sh"), "w").write("#!/bin/sh\ngit commit -m x\n")
    open(os.path.join(repo, "ok.sh"), "w").write("#!/bin/bash\n# it's fine\ngit status\nls\n")
    open(os.path.join(repo, "c.py"), "w").write("import subprocess\nsubprocess.run(['git','commit','-m','x'])\n")
    call = lambda tool, inp: glr.guard_call({"tool_name": tool, "tool_input": inp, "cwd": repo})
    allow = [
        "git status", "git status --porcelain", "git log --oneline -5", "git log -p -- f.cs",
        "git diff HEAD~1 --stat", "git diff --cached", "git show HEAD:f.cs", "git rev-parse --show-toplevel",
        "git branch --list", "git branch -a", "git branch", "git branch --show-current",
        "git branch -r --contains abc", "git -C repo status", "git --no-pager log", "git log --grep=merge",
        "git log --author=x | head", "git ls-files | xargs grep foo", "git blame f", "git diff 2>/dev/null",
        "cd x && git status && git diff", "git log --format='%h %s' -n 3", "git stash list", "git stash show -p",
        "git config --get user.name", "git config user.name", "git remote -v", "git remote get-url origin",
        "git tag --list", "git tag", "git describe --tags", "git show-ref", "git for-each-ref",
        "git merge-base A B", "git cherry -v", "git log --merges", "GIT_PAGER=cat git log", "git grep -n foo",
        "git reflog", "git shortlog -sn", "git worktree list", "git rev-list --count HEAD", "git cat-file -p HEAD",
        "git ls-tree HEAD", "git check-ignore -v f", "timeout 10 git status", "env GIT_PAGER=cat git log",
        "python3 -c \"import subprocess; subprocess.run(['git','log'])\"",
        "find . -name '*.cs' -exec git log -1 --format=%h -- {} \;", "git log | grep commit",
        "echo \"remember to git commit\"", "git status; echo done", "git diff > /tmp/d.patch",
        "if [ -d x ]; then git status; fi", "for f in *.md; do echo \"$f\"; done",
        "xbuild \"UISP 3.0/X.csproj\" /p:Configuration=Debug /nologo 2>&1 | grep \"error CS\" | grep -v \"MSB3644\\|MSB4062\"",
        "python3 - <<'EOF'\nimport json\nprint(\"it's\")\nEOF", "cat > out.md <<'EOF'\nRun git commit later, it's fine\nEOF",
        "sed -i '' 's/a/b/' f.cs", "git log --since=\"$(date -v-1d +%F)\"", "# a comment, it's fine\ngit status",
        "bash ok.sh", "./ok.sh", "git -c color.ui=always log", "cat ~/.claude/tools/limit-resume.log",
        "git diff --name-only main...HEAD", "command -v git", "git --version", "echo $(git rev-parse HEAD)",
        "xargs -n1 git show < shas.txt", "python3 -m pytest -q", "git submodule status"]
    block = [
        "git commit -m x", "git push", "git -c alias.c=commit c -m x", "git -c alias.p=push p origin main",
        "git -c alias.p='!git push' p", "git merge --no-ff feat", "git revert HEAD", "git cherry-pick abc",
        "git pull", "git stash", "G=git; $G commit -m x", "gi''t commit -m x",
        "python3 -c \"import subprocess; subprocess.run(['git','commit','-m','x'])\"", "echo commit | xargs git",
        "git update-ref refs/heads/x HEAD", "bash ./c.sh", "./c.sh", "python3 c.py", "sh -c 'git commit -m x'",
        "bash -euo pipefail -c 'git push'", "eval \"git commit -m x\"", "env git commit", "timeout 5 git push",
        "find . -exec true \; -exec git commit -m x \;", "nohup git push &",
        "node -e \"require('child_process').execSync('git commit -m x')\"", "ruby -e 'system(\"git push\")'",
        "/usr/bin/git commit", "command git commit", "$(git --exec-path)/git-commit -m x", "f(){ git commit; }; f",
        "git status\ngit commit -m x", "git status # x\ngit commit", "echo `git commit -m x`",
        "echo \"$(git commit -m x)\"", "git -c core.pager='sh -c \"git commit\"' log",
        "git --exec-path=/tmp/x status", "git branch new", "git add .", "git config alias.c commit",
        "GIT_CONFIG_PARAMETERS=\"'alias.c=commit'\" git c", "bash -s <<'EOF'\ngit commit -m x\nEOF",
        "python3 - <<'EOF'\nimport os\nos.system('git commit -m x')\nEOF", "echo 'git commit' | bash",
        "cat > n.sh <<'EOF'\ngit commit\nEOF\nbash n.sh", "echo x > ~/.claude/tools/limit-resume.py",
        "cp evil ~/.claude/tools/limit-resume.py", "echo '{}' > .claude/settings.local.json",
        "echo x >> .git/config", "osascript -e 'do shell script \"git push\"'",
        "git diff --output=$HOME/.claude/tools/limit-resume.py", "${X}t commit -m x", "xargs -I{} git {} < cmds",
        "watch -n 5 'git push'", "echo 'unbalanced"]
    for cmd in allow:
        check("guard allows %r" % cmd, call("Bash", {"command": cmd}), None)
    for cmd in block:
        check("guard blocks %r" % cmd, bool(call("Bash", {"command": cmd})), True)
    check("guard: Monitor's command is checked too", bool(call("Monitor", {"command": "git push"})), True)
    check("guard: Write into the kit blocked",
          bool(call("Write", {"file_path": os.path.join(glr.TOOLS, "limit-resume.py")})), True)
    check("guard: Edit of project settings blocked", bool(call("Edit", {"file_path": ".claude/settings.json"})), True)
    check("guard: a dialog nobody can answer is blocked", "Write the question" in call("AskUserQuestion", {}), True)
    check("guard: Read of the kit log allowed",
          call("Read", {"file_path": os.path.join(glr.TOOLS, "limit-resume.log")}), None)
    check("guard: Write of a report that mentions git commit allowed",
          call("Write", {"file_path": "report.md", "content": "run git commit"}), None)
    out = run_tool(["statusline"], json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": 1999999999},
        "seven_day": {"used_percentage": 34, "resets_at": 1999999999}}}), home)
    check("status line prints usage", out.stdout.strip(), "5h 12% · wk 34%")
    cache = json.load(open(os.path.join(home, ".claude", "tools", "usage.json")))
    check("status line fills the cache",
          (cache["five_hour"]["pct"], cache["seven_day"]["source"]), (12, "statusline"))
    out = run_tool(["statusline", "--", "cat >/dev/null; echo mine"], "{}", home)
    check("status line chains to your own command", out.stdout.strip(), "mine")


# ---- J31. commit between jobs (2026-11-23) ---------------------------------

def git_repo(path):
    os.makedirs(path, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"],
                 ["config", "commit.gpgsign", "false"]):
        REAL_RUN(["git", "-C", path] + args, check=True)
    with open(os.path.join(path, "a.txt"), "w") as fh:
        fh.write("a\n")
    REAL_RUN(["git", "-C", path, "add", "-A"], check=True)
    REAL_RUN(["git", "-C", path, "commit", "-q", "-m", "init"], check=True)
    return path


def git_out(path, *args):
    return REAL_RUN(["git", "-C", path] + list(args), capture_output=True, text=True).stdout


def commit_scenarios():
    try:
        _commit_scenarios()
    finally:
        kill_all()


def _commit_scenarios():
    change = ("Built it.\n\n```change\nWP-9: the thing, with tests.\n```\n\nJOB DONE")
    plan = {"records": [ASKED, said(change)], "sleep": 60}
    root, lr, folder, work, alerts, probes = job_scenario(
        "J31. commit between jobs: the runner commits after JOB DONE", plan=plan)
    repo = git_repo(os.path.join(root, "repo"))
    check("commit_message: title, change block, trailers",
          lr.commit_message("T", "x\n```change\nbody\n```\nJOB DONE", "/f/100 a.md", "sid1"),
          "T\n\nbody\n\nJob: 100 a.md\nSession: sid1\n")
    check("commit_message: no block -> title only",
          lr.commit_message("T", "no block", "/f/n.md", "s"), "T\n\nJob: n.md\nSession: s\n")
    check("repo_problem: relative path", lr.repo_problem("repo").startswith("commit repo is not an absolute"), True)
    os.makedirs(os.path.join(repo, "sub"))
    check("repo_problem: a subfolder is refused",
          (lr.repo_problem(os.path.join(repo, "sub")) or "").startswith("not the top"), True)
    check("repo_problem: work tree top ok", lr.repo_problem(repo), None)
    check("repo_problem: not a repo", (lr.repo_problem(work) or "").startswith("not a git work tree"), True)

    a = write_job(folder, "100 first.md", status="queued", order=100, commit=repo)
    b = write_job(folder, "110 second.md", status="queued", order=110, commit=repo)
    set_usage(lr)
    lr.cmd_sweep([])
    check("first job started on a clean tree", lr.read_note(a)[0].get("status"), "running")
    wait_transcript(lr)
    with open(os.path.join(repo, "new.txt"), "w") as fh:       # what the job "wrote"
        fh.write("work\n")
    lr.cmd_sweep([])
    meta = lr.read_note(a)[0]
    log1 = git_out(repo, "log", "-1", "--format=%B")
    check("done and committed", (meta.get("status"), bool(meta.get("committed"))), ("done", True))
    check("commit message = title + change block + trailers",
          log1.startswith("100 first\n\nWP-9: the thing, with tests.\n\nJob: 100 first.md\nSession: "), True)
    check("note's committed sha is HEAD", meta.get("committed"), git_out(repo, "rev-parse", "--short=12", "HEAD").strip())
    check("Run section says so", "Committed `%s`" % meta.get("committed") in note(a), True)
    check("tree clean after the commit", git_out(repo, "status", "--porcelain"), "")
    check("next job started after the commit, same sweep", lr.read_note(b)[0].get("status"), "running")
    wait_transcript(lr)
    lr.cmd_sweep([])                                           # second job changed nothing
    meta = lr.read_note(b)[0]
    check("nothing to commit -> still done, no sha", (meta.get("status"), meta.get("committed")), ("done", None))
    check("Run section says nothing to commit", "Nothing to commit in %s." % repo in note(b), True)

    c = write_job(folder, "120 third.md", status="queued", order=120, commit=repo)
    with open(os.path.join(repo, "stray.txt"), "w") as fh:
        fh.write("someone's edit\n")
    del alerts[:]
    lr.cmd_sweep([])
    lr.cmd_sweep([])
    check("dirty tree: queued job does not start", lr.read_note(c)[0].get("status"), "queued")
    check("dirty tree: one alert, naming the path",
          [m for m in alerts if "uncommitted" in m and "stray.txt" in m and "120 third" in m].__len__(), 1)
    os.remove(os.path.join(repo, "stray.txt"))
    lr.cmd_sweep([])
    check("clean again: it starts", lr.read_note(c)[0].get("status"), "running")
    wait_transcript(lr)
    lr.JOB_COMMIT_MAX_BYTES = 10
    with open(os.path.join(repo, "big.bin"), "w") as fh:
        fh.write("x" * 11)
    lr.cmd_sweep([])
    meta = lr.read_note(c)[0]
    check("oversized file -> job failed, commit refused",
          (meta.get("status"), "staged file over" in note(c), meta.get("committed")), ("failed", True, None))
    check("refused commit leaves nothing staged", git_out(repo, "diff", "--cached", "--name-only"), "")
    check("refused commit keeps the file", os.path.exists(os.path.join(repo, "big.bin")), True)
    d = write_job(folder, "130 plain.md", status="queued", order=130)
    lr.cmd_sweep([])
    check("a note without commit: ignores the dirty repo", lr.read_note(d)[0].get("status"), "running")


# ---- the 2026-11-22 decisions: L10/B2-L2, B2-M5, B3-H4, B3-H5 ----------------

def lsof_run(cwd_of, fail=False, calls=None):
    """subprocess.run stand-in: lsof prints cwd_of[pid] for each pid it is asked
    about (fail: lsof is missing); `open` and anything else succeed silently."""
    def run(cmd, *a, **k):
        if calls is not None:
            calls.append(cmd)
        out = ""
        if cmd[0] == "lsof":
            if fail:
                raise FileNotFoundError("lsof")
            pids = [int(p) for p in cmd[cmd.index("-p") + 1].split(",")]
            out = "".join("p%d\nfcwd\nn%s\n" % (p, cwd_of[p]) for p in pids if p in cwd_of)
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()
    return run


def two_folders(root, lr):
    a, b = os.path.join(root, "vault"), os.path.join(root, "notes")
    os.makedirs(a, exist_ok=True)
    os.makedirs(b, exist_ok=True)
    open(lr.INSTALL_CONF, "w").write("rc_dir=%s\nrc_dir=%s\nlabel=x\n" % (a, b))
    return a, b


def decisions_scenarios():
    try:
        _decisions_scenarios()
    finally:
        subprocess.run = REAL_RUN
        kill_all()


def _decisions_scenarios():
    import io, contextlib, time as _t
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())
    ours_cmd = "/u/.local/bin/claude remote-control --name auto-vault --debug-file /t/a.log"

    # ---- D1. L10/B2-L2: the folder list ------------------------------------
    root, argv_log, lr, alerts = b2_scenario("D1. Remote Control folders from install.conf")
    check("none saved -> one record, any server counts",
          [(f["dir"], f["name"], f["key"]) for f in lr.rc_folders()], [(None, None, None)])
    open(lr.INSTALL_CONF, "w").write("rc_dir=/Users/u/vault\nlabel=x\n")   # the one-folder format
    f = lr.rc_folders()
    check("one-folder install.conf: one folder, the one-folder kit's files and state",
          (len(f), f[0]["name"], f[0]["key"], f[0]["launcher"], f[0]["status"], f[0]["lock"],
           f[0]["where"]),
          (1, "auto-vault", None, lr.RC_LAUNCHER, lr.RC_STATUS, lr.RC_LOCK, ""))
    open(lr.INSTALL_CONF, "w").write(
        "rc_dir=/Users/u/vault\nrc_dir=/Users/u/Code/notes/\nrc_dir=/x/notes\nlabel=x\n")
    f = lr.rc_folders()
    check("several, in order; a name already taken is skipped",
          [x["name"] for x in f], ["auto-vault", "auto-notes"])
    check("a later folder: its own launcher, status file, lock and state key",
          (os.path.basename(f[1]["launcher"]), os.path.basename(f[1]["status"]),
           os.path.basename(f[1]["lock"]), f[1]["key"]),
          ("claude-rc-notes.command", "rc-last-exit-notes", "rc-launch-notes.lock", "auto-notes"))
    check("messages name the folder once there are several", [x["where"] for x in f],
          [" for vault", " for notes"])
    cfg = {"rc_reset": 5}
    check("the first folder's state is the config itself", lr.rc_state(cfg, f[0]) is cfg, True)
    check("another's lives under rc_more, seeded with the last `rc on`",
          (lr.rc_state(cfg, f[1]), list(cfg["rc_more"])), ({"rc_reset": 5}, ["auto-notes"]))

    # ---- D2. which server serves which folder --------------------------------
    root, argv_log, lr, alerts = b2_scenario("D2. a server counts only for its own folder")
    a, b = two_folders(root, lr)
    fa, fb = lr.rc_folders()
    ours, hand = (11, 1, ours_cmd), (12, 1, "claude rc")
    lr.subprocess.run = lsof_run({11: a})
    check("ours, for its own folder", lr.rc_serving([ours], fa), ("launcher", 11))
    check("another folder's server is up: this folder's is down", lr.rc_serving([ours], fb),
          (None, None))
    lr.subprocess.run = lsof_run({11: a, 12: b})
    check("a server started by hand in the folder serves it (no launch, no 409 loop)",
          lr.rc_serving([ours, hand], fb), ("by hand", 12))
    check("but it is never taken for ours (stale and hung checks)",
          lr.rc_server([ours, hand], fb), None)
    calls = []
    lr.subprocess.run = lsof_run({}, fail=True, calls=calls)
    check("lsof missing -> cannot tell (up: never spawn blind)",
          lr.rc_serving([ours, hand], fb), ("unknown", None))
    check("ps failed -> cannot tell", lr.rc_serving(None, fb), ("unknown", None))
    del calls[:]
    check("the named server needs no lsof", (lr.rc_serving([ours, hand], fa), calls),
          (("launcher", 11), []))
    subprocess.run = REAL_RUN

    # ---- D3. per-folder watchdog state ----------------------------------------
    root, lr, calls, opens = rc_scenario("D3. watchdog: each folder on its own")
    a, b = two_folders(root, lr)
    fa, fb = lr.rc_folders()
    open(fb["launcher"], "w").write("#!/bin/sh\n")
    rows = [(11, 1, ours_cmd)]
    lr.ps_rows = lambda: list(rows)
    lr.subprocess.run = lsof_run({11: a}, calls=calls)
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "pushover"
    st = {lr.CONFIG: {"rc_watch": True}}
    check("only the folder that is down is started", (lr.rc_watchdog(st), [c[1] for c in opens()]),
          (True, [fb["launcher"]]))
    cfg = st[lr.CONFIG]
    check("the try is counted in that folder's own state",
          (cfg.get("rc_tries"), cfg["rc_more"]["auto-notes"].get("rc_tries")), (None, 1))
    lr.time.t += lr.RC_MIN_INTERVAL + 1
    del rows[:]
    cfg["rc_more"]["auto-notes"]["rc_tries"] = lr.RC_MAX_TRIES
    del calls[:]
    lr.rc_watchdog(st)
    check("one folder's tripped breaker does not hold the other back",
          [c[1] for c in opens()], [fa["launcher"]])
    check("its alert names the folder", [m for m in alerts if "restarts failed" in m][:1],
          ["Remote Control for notes is down and 3 restarts failed. Standing down; check the "
           "Terminal window, then `limit-resume.py rc on`."])
    open(fb["status"], "w").write("%d auth 1 13\n" % (lr.time.t - 5))
    lr.cred_mtime = lambda: 0
    check("each folder reads its own launcher's exit status",
          (lr.rc_login_dead({}, fb), lr.rc_login_dead({}, fa)), (True, False))
    rows[:] = [(12, 1, "claude remote-control")]
    lr.subprocess.run = lsof_run({12: b}, calls=calls)
    cfg["rc_more"]["auto-notes"] = {"rc_tries": 2}
    cfg["rc_last_launch"] = 0
    del calls[:]
    lr.rc_watchdog(st)
    check("a hand-started server in a folder: up, breaker reset, nothing opened for it",
          (cfg["rc_more"]["auto-notes"]["rc_tries"], fb["launcher"] in [c[1] for c in opens()]),
          (0, False))
    lr.save_state(st)
    with quiet():
        lr.cmd_rc(["on"])
    check("`rc on` resets every folder", "rc_more" in lr.load_state()[lr.CONFIG], False)
    orphans = [(21, 1, SDK % ("a", "a")), (22, 1, SDK % ("b", "b"))]
    lr.ps_rows = lambda: orphans
    lr.subprocess.run = lsof_run({21: a, 22: b})
    check("orphans are reaped per folder (a session works in its server's folder)",
          ([p for p, _ in lr.rc_orphans(fa)], [p for p, _ in lr.rc_orphans(fb)]), ([21], [22]))
    lr.subprocess.run = lsof_run({}, fail=True)
    check("lsof fails -> kill nothing", lr.rc_orphans(fb), [])
    subprocess.run = REAL_RUN

    root, argv_log, lr, alerts = b2_scenario("D3b. doctor: one line per folder; a stall resets all timers")
    a, b = two_folders(root, lr)
    lr.scheduler_status = lambda: ("launchd : loaded", True, False)
    lr.keychain = lambda account: ""
    lr.rc_serving = lambda rows, folder: ("launcher", 7) if folder["name"] == "auto-vault" \
        else ("by hand", 9)
    lr.save_state({lr.CONFIG: {"rc_watch": True, "last_sweep_at": _t.time(), "last_sweep": "x"}})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_doctor([])
    lines = [l for l in buf.getvalue().splitlines() if l.startswith("remote ctl")]
    check("doctor: a line per folder, with its name", lines,
          ["remote ctl : watched, running - %s (auto-vault)" % a,
           "remote ctl : watched, running (a server started by hand serves it, pid 9; not "
           "restarted) - %s (auto-notes)" % b])
    lr.save_state({lr.CONFIG: {"last_sweep_at": _t.time() - 7200,
                               "rc_more": {"auto-notes": {"rc_hung_seen": [1, 2, 3],
                                                          "rc_stale_since": 4}}}})
    with quiet():
        lr.cmd_sweep([])
    check("after a stall the other folders' timers start afresh too",
          lr.load_state()[lr.CONFIG]["rc_more"]["auto-notes"], {})

    # ---- D4. B2-M5: a claude working in the chat's folder owns it --------------
    root, argv_log, lr, alerts = b2_scenario("D4. lsof: cwds of several pids in one call")
    calls = []
    lr.subprocess.run = lsof_run({os.getpid(): "/a"}, calls=calls)
    check("one batched call, parsed", (lr.cwds([os.getpid(), os.getpid()]), len(calls)),
          ({os.getpid(): "/a"}, 1))
    gone = subprocess.Popen(["true"])
    gone.wait()
    lr.subprocess.run = lsof_run({})
    check("a live process of mine left out -> cannot tell", lr.cwds([os.getpid()]), None)
    check("an exited one, or another user's (pid 1), is just left out",
          lr.cwds([gone.pid, 1]), {})
    del calls[:]
    check("no pids -> no lsof", (lr.cwds([]), calls), ({}, []))
    subprocess.run = REAL_RUN

    def owned_scenario(name, rows, cwd_of, fail=False):
        root, argv_log, lr, alerts = b2_scenario(name)
        work = os.path.join(root, "work")
        os.makedirs(work)
        path = write_transcript(root, "sess-cwd", "quota")
        recs = [json.loads(l) for l in open(path)]
        recs[0]["cwd"] = work
        with open(path, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in recs)
        lr.ps_rows = lambda: rows
        lr.subprocess.run = lsof_run({p: (work if c == "WORK" else c) for p, c in cwd_of.items()},
                                     fail=fail)
        lr.save_state({lr.CONFIG: {"auto_all": True}})
        sweeps(lr, 1)
        return root, argv_log, lr, alerts, work

    root, argv_log, lr, alerts, work = owned_scenario(
        "D4b. a terminal claude in the chat's folder -> alert, no headless resume",
        [(601, 1, "claude"), (602, 1, "/x/claude --model opus")], {601: "/elsewhere", 602: "WORK"})
    check("not resumed headless", launches(argv_log), [])
    check("the alert names the pid and folder and says type continue",
          [("pid 602" in m, work in m, '"continue"' in m) for m in alerts], [(True, True, True)])
    sweeps(lr, 1)
    check("block handled as for the session-id owner: no second alert",
          (len(alerts), launches(argv_log)), (1, []))
    subprocess.run = REAL_RUN

    root, argv_log, lr, alerts, work = owned_scenario(
        "D4c. lsof missing -> fail safe: alert, no resume",
        [(603, 1, "/Users/x/.local/share/claude/versions/2.1.308 --model opus")], {}, fail=True)
    check("not resumed, alert says it cannot tell",
          (launches(argv_log), [("lsof failed" in m, '"continue"' in m) for m in alerts]),
          ([], [(True, True)]))
    subprocess.run = REAL_RUN

    tool = "/usr/bin/python3 /t/.claude/tools/limit-resume.py"
    root, argv_log, lr, alerts, work = owned_scenario(
        "D4d. Remote Control server and chats, job sessions, other folders: not owners",
        [(611, 1, "/u/.local/bin/claude remote-control --name auto-work"),
         (612, 611, SDK % ("1", "1")), (613, 1, SDK % ("2", "2")),
         (614, 1, tool + " jobpty /t/j.pty.log -- claude --session-id j1 --remote-control job"),
         (615, 614, "claude --session-id j1 --remote-control job"),
         (616, 1, "claude"), (617, 1, "/bin/zsh"), (618, 1, "claudette")],
        {611: "WORK", 612: "WORK", 613: "WORK", 615: "WORK", 616: "/other", 617: "WORK",
         618: "WORK"})
    check("resumed headless once", (len(launches(argv_log)), alerts), (1, []))
    subprocess.run = REAL_RUN

    # ---- D5. B3-H4: typing in a finished job session ---------------------------
    root, lr, folder, work, alerts, probes = job_scenario(
        "D5. typing in a finished job's open session -> it is the running task, uncapped")
    lr.headless_owner = lambda sid, path, rows: None
    j = write_job(folder, "010 done.md", status="queued", order=10)
    set_usage(lr)
    lr.cmd_sweep([])
    wait_transcript(lr)
    first = running_run(lr)
    set_usage(lr)
    lr.cmd_sweep([])
    js = lr.load_state()[lr.JOBS_KEY]
    check("finished and kept open", (lr.read_note(j)[0].get("status"),
                                     [s["sid"] for s in js.get("open", [])]), ("done", [first["sid"]]))
    check("not typed into yet: no running task", lr.typed_finished(js, _t.time()), None)
    with open(lr.session_file(first["sid"]), "a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user",
                                                         "content": "one more thing"}}) + "\n")
    k = write_job(folder, "020 next.md", status="queued", order=20)
    write_transcript(root, "sess-chat", "quota")
    st = lr.load_state()
    st[lr.CONFIG]["auto_all"] = True
    lr.save_state(st)
    lr.JOB_MAX_H = 0
    set_usage(lr, five=95, week=99)
    starts = len(job_argvs(root))
    lr.cmd_sweep([])
    log_text = open(lr.LOG).read()
    check("the next job waits", (lr.read_note(k)[0].get("status"),
                                 "020 next waits - you are working in finished job 010 done" in log_text),
          ("queued", True))
    check("the due resume waits too", (len(job_argvs(root)) == starts,
                                       "waiting for you to finish in job 010 done" in log_text),
          (True, True))
    check("no caps, no JOB_MAX_H: left running, note still done, not the running job",
          (lr.job_alive(first), lr.read_note(j)[0].get("status"),
           lr.load_state()[lr.JOBS_KEY].get("running")), (True, "done", None))
    lr.scheduler_status = lambda: ("launchd : loaded", True, False)
    lr.keychain = lambda account: ""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lr.cmd_doctor([])
    check("doctor says you are working in it", "you are working in it, the queue waits"
          in buf.getvalue(), True)
    quiet_since = _t.time() - lr.RESUME_BUSY - 60
    os.utime(lr.session_file(first["sid"]), (quiet_since, quiet_since))
    set_usage(lr)
    lr.cmd_sweep([])
    check("quiet for RESUME_BUSY: the due resume goes first",
          wait_for(lambda: any("--resume sess-chat" in " ".join(a) for a in job_argvs(root))), True)

    # ---- D6. B3-H5: the job git wrapper -----------------------------------------
    print("\nD6. job git wrapper: a second layer behind the guard")
    home = tempfile.mkdtemp(prefix="lrgit-")
    realbin, linkbin = os.path.join(home, "realbin"), os.path.join(home, "linkbin")
    os.makedirs(realbin)
    os.makedirs(linkbin)
    git_log = os.path.join(home, "git.argv")
    open(os.path.join(realbin, "git"), "w").write(
        '#!/bin/sh\necho "$*" >> "%s"\necho real-git\n' % git_log)
    os.chmod(os.path.join(realbin, "git"), 0o755)
    glr = load_tool(home, os.path.join(home, "argv.txt"))
    wrapper = os.path.join(glr.JOB_BIN, "git")
    os.makedirs(glr.JOB_BIN)
    open(wrapper, "w").write("#!/bin/sh\necho old wrapper\n")
    os.chmod(wrapper, 0o755)
    os.symlink(wrapper, os.path.join(linkbin, "git"))
    saved = os.environ["PATH"]
    os.environ["PATH"] = os.pathsep.join([glr.JOB_BIN, linkbin, realbin, "/usr/bin", "/bin"])
    try:
        check("written into JOB_BIN", glr.job_git_wrapper(), wrapper)
    finally:
        os.environ["PATH"] = saved
    text = open(wrapper).read()
    check("executable; names the tool version and the real git, never itself or a link to it",
          (os.access(wrapper, os.X_OK), glr.VERSION in text,
           'exec %s "$@"' % shlex.quote(os.path.join(realbin, "git")) in text, linkbin in text),
          (True, True, True, False))
    run_path = os.pathsep.join([glr.JOB_BIN, glr.JOB_BIN, realbin, "/usr/bin", "/bin"])
    read = lambda p: open(p).read() if os.path.exists(p) else ""

    def git(args, job=True, exe=wrapper):
        env = dict(os.environ, HOME=home, PATH=run_path)
        env.pop("LIMIT_RESUME_JOB", None)
        if job:
            env["LIMIT_RESUME_JOB"] = "1"
        open(git_log, "w").close()
        return subprocess.run([exe] + args, env=env, capture_output=True, text=True,
                              timeout=30, cwd=home)
    r = git(["status", "--short"])
    check("read-only git runs the real git, once (no recursion)",
          (r.returncode, r.stdout.strip(), read(git_log)), (0, "real-git", "status --short\n"))
    for args in (["log", "--oneline", "-3"], ["-C", home, "diff"], ["branch", "--list"]):
        check("allowed: git %s" % " ".join(args), git(args).returncode, 0)
    for args in (["commit", "-m", "x"], ["push", "origin", "main"], ["-c", "alias.c=commit", "c"],
                 ["merge", "feat"], ["stash"], ["-c", "core.pager=sh", "log"], ["update-ref", "a", "b"]):
        r = git(args)
        check("refused: git %s (real git never runs)" % " ".join(args),
              (r.returncode, "must not git commit" in r.stderr, read(git_log)), (1, True, ""))
    r = git(["commit", "-m", "x"], job=False)
    check("outside a job session: plain git", (r.returncode, read(git_log)), (0, "commit -m x\n"))
    broken = os.path.join(home, "broken-git")
    open(broken, "w").write(text.replace(shlex.quote(os.path.abspath(glr.__file__)),
                                         shlex.quote(os.path.join(home, "gone.py")), 1))
    os.chmod(broken, 0o755)
    r = git(["status"], exe=broken)
    check("tool gone -> refused (fails closed)", (r.returncode, read(git_log)), (1, ""))
    open(broken, "w").write(text.replace(shlex.quote(sys.executable), "/gone/python3", 1))
    r = git(["status"], exe=broken)
    check("python gone -> refused (fails closed)", (r.returncode, read(git_log)), (1, ""))
    os.environ["PATH"] = os.pathsep.join([glr.JOB_BIN, linkbin])
    try:
        glr.job_git_wrapper()
    finally:
        os.environ["PATH"] = saved
    r = git(["status"])
    check("no real git on PATH: says so, never loops", (r.returncode, "not found" in r.stderr),
          (127, True))
    spawned = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            spawned.update(kw)
            self.pid = 424242
    real_popen = subprocess.Popen
    subprocess.Popen = FakePopen
    try:
        glr.job_spawn(["claude"], os.path.join(home, "j.pty.log"), home)
        job_env = spawned["env"]
        glr.job_spawn(["claude"], os.path.join(home, "c.pty.log"), home, job=False)
        chat_env = spawned["env"]
    finally:
        subprocess.Popen = real_popen
        glr._SPAWNED.clear()
    check("job sessions get JOB_BIN first on PATH, once",
          (job_env["PATH"].split(os.pathsep)[0], job_env["PATH"].split(os.pathsep).count(glr.JOB_BIN),
           job_env.get("LIMIT_RESUME_JOB")), (glr.JOB_BIN, 1, "1"))
    check("a continued chat does not", glr.JOB_BIN in chat_env["PATH"].split(os.pathsep), False)
    os.unlink(wrapper)
    os.rmdir(glr.JOB_BIN)
    open(glr.JOB_BIN, "w").close()               # a file where the folder should be
    with quiet():
        check("wrapper cannot be written -> no job settings, so no job starts",
              glr.job_settings(), None)


def run_tty(box, script, answers, *args):
    """install.sh with its stdin on a terminal (a pty), typing `answers`."""
    import pty
    master, slave = pty.openpty()
    try:
        os.write(master, answers.encode())
        proc = subprocess.run(["/bin/sh", script] + list(args), stdin=slave, capture_output=True,
                              text=True, env=box["env"], timeout=120)
    finally:
        os.close(slave)
        os.close(master)
    return proc.returncode, proc.stdout + proc.stderr


def installer_decisions():
    if sys.platform != "darwin" or not os.path.exists(INSTALL_SH):
        print("\n(not macOS - multi-folder install tests skipped)")
        return
    read = lambda p: open(p).read() if os.path.exists(p) else None
    box = installer_box()
    tools = box["tools"]
    vault, notes = os.path.join(box["home"], "vault"), os.path.join(box["home"], "Code", "notes")
    for d in (vault, notes):
        os.makedirs(d)

    print("\nD7. install.sh: several Remote Control folders")
    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIRS="%s:%s" % (vault, notes))
    check("exit 0", code, 0)
    check("install.conf: one rc_dir line per folder, in order",
          read(os.path.join(tools, "install.conf")),
          "rc_dir=%s\nrc_dir=%s\nlabel=%s\nsrc=%s\n" % (vault, notes, box["label"], KIT))
    first, second = (read(os.path.join(tools, f)) or "" for f in
                     ("claude-rc.command", "claude-rc-notes.command"))
    check("first folder keeps the one-folder kit's launcher, status and lock",
          ('cd "%s" || exit 1' % vault in first, 'STATUS="$TOOLS/rc-last-exit"' in first,
           'LOCK="$TOOLS/rc-launch.lock"' in first, '--name "auto-vault"' in first),
          (True, True, True, True))
    check("second folder: its own launcher, name, status and lock",
          ('cd "%s" || exit 1' % notes in second, '--name "auto-notes"' in second,
           'STATUS="$TOOLS/rc-last-exit-notes"' in second,
           'LOCK="$TOOLS/rc-launch-notes.lock"' in second, "@" + "RC_" in second),
          (True, True, True, True, False))
    check("a Login Item for each",
          [os.path.basename(c.split('path:"')[1].split('"')[0]) for c in calls
           if "make login item" in c and "claude-rc" in c],
          ["claude-rc.command", "claude-rc-notes.command"])

    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIR=vault)
    check("LIMIT_RESUME_RC_DIR (one folder) still works; the dropped folder's launcher goes",
          (code, read(os.path.join(tools, "install.conf")).count("rc_dir="),
           os.path.exists(os.path.join(tools, "claude-rc-notes.command"))), (0, 1, False))
    check("and its Login Item", any('delete login item "claude-rc-notes.command"' in c
                                    for c in calls), True)

    other = os.path.join(box["home"], "Other", "notes")
    os.makedirs(other)
    before = sorted(os.listdir(tools))
    conf = read(os.path.join(tools, "install.conf"))
    code, out, calls = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIRS="%s:%s" % (notes, other))
    check("two folders with one name -> refused before any write",
          (code, 'named "notes"' in out, sorted(os.listdir(tools)),
           read(os.path.join(tools, "install.conf")), [c for c in calls if " print " not in c]),
          (1, True, before, conf, []))
    code, out, _ = run_sh(box, INSTALL_SH, LIMIT_RESUME_RC_DIRS="%s:%s:%s/" % (vault, notes, vault))
    check("the same folder twice is listed once",
          (code, read(os.path.join(tools, "install.conf")).count("rc_dir=")), (0, 2))

    open(os.path.join(tools, "install.conf"), "w").write("rc_dir=%s\n" % notes)  # one-folder kit
    code, out, _ = run_sh(box, INSTALL_SH)
    check("a one-folder install.conf is taken as a list of one (no env, no terminal)",
          (code, read(os.path.join(tools, "install.conf")),
           os.path.exists(os.path.join(tools, "claude-rc-notes.command"))),
          (0, "rc_dir=%s\nlabel=%s\nsrc=%s\n" % (notes, box["label"], KIT), False))

    code, out = run_tty(box, INSTALL_SH, "%s\n~/Code/notes:%s\n\n" % (vault, other), "--no-start")
    check("from a terminal it asks, showing the saved list",
          ("serves these folders now" in out, "  %s" % notes in out), (True, True))
    check("typed folders, ~ and ':' understood; collision still refused",
          (code, 'named "notes"' in out), (1, True))
    code, out = run_tty(box, INSTALL_SH, "%s\n~/Code/notes\n\n" % vault, "--no-start")
    check("typed list saved", (code, read(os.path.join(tools, "install.conf"))),
          (0, "rc_dir=%s\nrc_dir=%s\nlabel=%s\nsrc=%s\n" % (vault, notes, box["label"], KIT)))
    code, out = run_tty(box, INSTALL_SH, "\n", "--no-start")
    check("an empty first line keeps the saved list",
          (code, read(os.path.join(tools, "install.conf")).count("rc_dir=")), (0, 2))

    os.makedirs(os.path.join(tools, "jobbin"))
    open(os.path.join(tools, "jobbin", "git"), "w").write("#!/bin/sh\n")
    open(os.path.join(tools, "rc-last-exit-notes"), "w").write("1 exit 0 1\n")
    code, out, calls = run_sh(box, UNINSTALL_SH, "--purge")
    check("uninstall: every folder's Login Item removed",
          [c.split('login item "')[1].split('"')[0] for c in calls if "delete login item" in c],
          ["claude-rc.command", "claude-rc-notes.command"])
    check("--purge: every launcher, tagged file and the git wrapper gone",
          (code, sorted(os.listdir(tools))), (0, []))


def main():
    original_path = os.environ["PATH"]
    print("testing %s" % TOOL)

    # ---- 1. the happy path, end to end -----------------------------------
    root, argv_log, lr = scenario("1. quota stop, reset in the past -> resumes once")
    write_transcript(root, "sess-happy", "quota", reset_delta=-600)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    lr.cmd_sweep([])
    settle(lr)
    got = launches(argv_log)
    check("launched exactly one resume", len(got), 1)
    if got:
        check("passes --resume <sid>", "--resume sess-happy" in got[0], True)
        check("passes -p with the prompt", "-p" in got[0], True)
        check("passes --permission-mode", "--permission-mode acceptEdits" in got[0], True)
    st = json.load(open(lr.STATE))
    check("session recorded", "sess-happy" in st, True)
    check("block marked handled", st.get("sess-happy", {}).get("handled"), ["stop-sess-happy"])
    check("resume counted", st.get("sess-happy", {}).get("resumes"), 1)
    check("auto mode's entry is tagged", st.get("sess-happy", {}).get("origin"), "auto")
    act = st[lr.CONFIG].get("active_resume") or {}
    check("detached: tracked as the active task", (act.get("how"), act.get("sid")),
          ("headless", "sess-happy"))
    check("not retired before a sweep collects it", st.get("sess-happy", {}).get("done"), None)
    lr.cmd_sweep([])
    st = json.load(open(lr.STATE))
    check("retired after DONE", (st.get("sess-happy", {}).get("done"),
                                 st.get("sess-happy", {}).get("why")), (True, "DONE"))
    check("active task cleared once collected", st[lr.CONFIG].get("active_resume"), None)
    check("result logged", "sess-happy: resume ended, exit=0 out=working DONE"
          in open(lr.LOG).read(), True)
    sweeps(lr, 1)
    check("second sweep does not relaunch", len(launches(argv_log)), 1)

    # ---- 1b. Remote Control chats are continued through their own inbox --
    def rc_setup(root, lr, sids, child=True, relay_out="SENT"):
        fake_claude(root, stdout=relay_out)
        for sid in sids:
            path = write_transcript(root, sid, "quota", reset_delta=-600)
            recs = [json.loads(l) for l in open(path)]
            recs[0]["uuid"] = "first-" + sid
            with open(path, "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in recs)
        os.makedirs(lr.RC_DEBUG)
        with open(os.path.join(lr.RC_DEBUG, "bridge-transcript-cse_TEST.jsonl"), "w") as fh:
            fh.write('{"type":"control_request"}\n')
            fh.write(json.dumps({"type": "user", "uuid": "first-sess-rc",
                                 "message": {"role": "user", "content": "do the thing"}}) + "\n")
        lr.CC_SOCKS = os.path.join(root, "socks")
        os.makedirs(lr.CC_SOCKS)
        rows = []
        if child:
            open(os.path.join(lr.CC_SOCKS, "4242.sock"), "w").close()
            rows = [(4242, 77, "/x/versions/2.1.308 --print " + lr.RC_CHILD_MARK
                     + "cse_TEST --session-id cse_TEST --input-format stream-json")]
        lr.ps_rows = lambda: rows
        alerts = []
        lr.notify = lambda msg: alerts.append(msg) or "pushover"
        lr.save_state({lr.CONFIG: {"auto_all": True}})
        lr.MAX_PER_SWEEP = 5
        return alerts

    root, argv_log, lr = scenario("1b. Remote Control chat -> prompt sent to its inbox")
    alerts = rc_setup(root, lr, ("sess-rc", "sess-local"))
    check("rc_session finds the owner", lr.rc_session(lr.session_file("sess-rc")), "cse_TEST")
    check("plain session has no owner", lr.rc_session(lr.session_file("sess-local")), None)
    check("rc_child_pid finds the child", lr.rc_child_pid("cse_TEST"), 4242)
    check("rc_child_pid needs an exact id", lr.rc_child_pid("cse_TES"), None)
    sweeps(lr, 2)                   # one task at a time: the headless resume, then the relay
    got = launches(argv_log)
    relay = [g for g in got if "uds:" in g]
    check("rc chat never resumed headless", any("--resume sess-rc" in g for g in got), False)
    check("plain session still resumed", any("--resume sess-local" in g for g in got), True)
    check("one relay, to the child's socket",
          len(relay) == 1 and ("uds:%s" % os.path.join(lr.CC_SOCKS, "4242.sock")) in relay[0], True)
    raw = open(argv_log).read()     # the relay prompt spans lines in the argv log
    check("relay uses the cheap model and only SendMessage",
          "--model claude-haiku-5-5" in raw and "--allowedTools SendMessage" in raw, True)
    check("relay carries the resume prompt", lr.DEFAULT_PROMPT in raw, True)
    check("relay denies the tools it has no use for", "--disallowedTools " + lr.RELAY_DENY in raw, True)
    check("relay runs from TOOLS, which auto mode skips",
          os.path.realpath(open(os.path.join(root, "cwd.txt")).read().split()[-1]),
          os.path.realpath(lr.TOOLS))
    check("no alert when the relay succeeds", alerts, [])
    st = json.load(open(lr.STATE))
    check("rc block marked handled", st.get("sess-rc", {}).get("handled"), ["stop-sess-rc"])
    check("rc send counted as a resume", st.get("sess-rc", {}).get("resumes"), 1)
    lr.cmd_sweep([])
    check("second sweep sends nothing new", len([g for g in launches(argv_log) if "uds:" in g]), 1)

    root, argv_log, lr = scenario("1c. Remote Control chat with no live child -> alert only")
    alerts = rc_setup(root, lr, ("sess-rc",), child=False)
    lr.cmd_sweep([])
    check("nothing launched", launches(argv_log), [])
    check("one alert naming the chat", len(alerts) == 1 and "do the thing" in alerts[0], True)
    lr.cmd_sweep([])
    check("block handled, no second alert", len(alerts), 1)

    root, argv_log, lr = scenario("1d. relay reports FAILED -> alert, no headless resume")
    alerts = rc_setup(root, lr, ("sess-rc",), relay_out="FAILED")
    lr.cmd_sweep([])
    raw = open(argv_log).read()
    check("one relay attempt, no resume", (raw.count("--allowedTools SendMessage"),
                                           "--resume" in raw), (1, False))
    check("alert sent", len(alerts), 1)

    # ---- 2. stop causes that must be ignored -----------------------------
    root, argv_log, lr = scenario("2. non-quota endings are left alone")
    for kind in ("overloaded", "connection", "normal", "continued"):
        write_transcript(root, "sess-" + kind, kind, reset_delta=-600)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    lr.cmd_sweep([])
    check("no resume for 529/connection/normal/continued", launches(argv_log), [])

    # ---- 2b. permission mode follows the session --------------------------
    root, argv_log, lr = scenario("2b. resume uses the session's own permission mode")
    prompt = lr.DEFAULT_PROMPT
    write_transcript(root, "sess-auto", "quota", modes=[
        ("manual", "first turn"), ("auto", "do it"), (lr.PERM, prompt)])
    write_transcript(root, "sess-old", "quota", modes=[("default", "old cli")])
    write_transcript(root, "sess-odd", "quota", modes=[("bogus", "?")])
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    lr.MAX_PER_SWEEP = 5
    sweeps(lr, 4)
    got = " ".join(launches(argv_log))
    for sid, mode in (("sess-auto", "auto"), ("sess-old", "manual"), ("sess-odd", lr.PERM)):
        check("%s resumes in %s" % (sid, mode),
              "--resume %s -p %s --permission-mode %s" % (sid, prompt, mode) in got, True)
    lr.PERM_FORCED = "plan"
    check("LIMIT_RESUME_PERMISSION_MODE overrides the session",
          lr.session_perm(lr.session_file("sess-auto"), prompt), "plan")

    # ---- 2c. resume runs in the session's own directory -------------------
    root, argv_log, lr = scenario("2c. resume runs in the directory the session started in")
    proj = os.path.join(root, "vault")
    os.makedirs(os.path.join(proj, "sub"))
    path = write_transcript(root, "sess-cwd", "quota")
    recs = [json.loads(l) for l in open(path)]
    recs[0]["cwd"] = "/"                            # an earlier launchd resume
    for r in recs[1:]:
        r["cwd"] = os.path.join(proj, "sub")        # after a `cd`
    recs.insert(1, {"type": "user", "cwd": proj, "message": {"role": "user", "content": "hi"}})
    with open(path, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs[1:2] + recs[:1] + recs[2:]))
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    os.chdir("/")                                   # what launchd gives us
    sweeps(lr, 1)
    cwds = [l.strip() for l in open(os.path.join(root, "cwd.txt"))]
    check("claude launched from the session's start dir, not /",
          [os.path.realpath(c) for c in cwds], [os.path.realpath(proj)])
    os.makedirs(os.path.join(root, "gone"))
    gone = write_transcript(root, "sess-nodir", "quota")
    recs = [json.loads(l) for l in open(gone)]
    recs[0]["cwd"] = os.path.join(root, "gone", "deleted")
    with open(gone, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs))
    check("missing start dir falls back to HOME",
          lr.session_cwd(gone), os.path.expanduser("~"))

    # ---- 3. timing guard --------------------------------------------------
    root, argv_log, lr = scenario("3. reset still in the future -> waits")
    write_transcript(root, "sess-future", "quota", reset_delta=+3600)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    lr.cmd_sweep([])
    check("does not resume before resetsAt", launches(argv_log), [])

    # ---- 4. age guard -----------------------------------------------------
    root, argv_log, lr = scenario("4. stale block: skipped in auto, honoured when armed")
    write_transcript(root, "sess-stale", "quota", reset_delta=-300 * 3600, age_hours=305)
    alerts = []
    lr.notify = lambda msg: alerts.append(msg) or "pushover"
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 1)
    check("auto mode skips a block that reset 300 h ago", launches(argv_log), [])
    check("history older than a week: no alert", alerts, [])
    lr.save_state({lr.CONFIG: {"auto_all": True},
                   "sess-stale": {"prompt": "go", "resumes": 0, "handled": []}})
    sweeps(lr, 1)
    check("explicitly armed overrides the age guard", len(launches(argv_log)), 1)

    # ---- 5. pile-up throttle ---------------------------------------------
    root, argv_log, lr = scenario("5. many blocked sessions -> one at a time, a sweep apart")
    for i in range(5):
        write_transcript(root, "sess-pile%d" % i, "quota", reset_delta=-600)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 1)
    check("first sweep launches one resume", len(launches(argv_log)), 1)
    sweeps(lr, 1)
    check("next sweep collects it and takes the next", len(launches(argv_log)), 2)
    sweeps(lr, 4)
    check("all five in turn", len(launches(argv_log)), 5)

    # ---- 6. failure handling ---------------------------------------------
    root, argv_log, lr = scenario("6. a failing resume is not retried forever")
    fake_claude(root, stdout="boom", exit_code=1)      # overrides PATH again
    write_transcript(root, "sess-fail", "quota", reset_delta=-600)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    sweeps(lr, 2)
    st = json.load(open(lr.STATE))
    check("failed resume still marked handled", st["sess-fail"]["handled"], ["stop-sess-fail"])
    check("not retired on failure", st["sess-fail"].get("done"), None)
    check("exit status logged", "sess-fail: resume ended, exit=1" in open(lr.LOG).read(), True)
    before = len(launches(argv_log))
    sweeps(lr, 1)
    check("failed resume is not relaunched", len(launches(argv_log)), before)

    # ---- 7. missing binary ------------------------------------------------
    root, argv_log, lr = scenario("7. claude missing from PATH -> logged, not crashed")
    write_transcript(root, "sess-nobin", "quota", reset_delta=-600)
    lr.save_state({lr.CONFIG: {"auto_all": True}})
    os.environ["PATH"] = "/usr/bin:/bin"            # ps still runs, claude is gone
    lr.CLAUDE_PATH_HINT = ""                         # ~/.local/bin may hold the real one
    try:
        rc = lr.cmd_sweep([])
    finally:
        os.environ["PATH"] = original_path
    check("sweep survives a missing binary", rc, 0)
    check("logged the PATH problem", "not on PATH" in open(lr.LOG).read(), True)

    # ---- 8. heartbeat -----------------------------------------------------
    root, argv_log, lr = scenario("8. every sweep writes a heartbeat")
    lr.save_state({lr.CONFIG: {"auto_all": False}})
    lr.cmd_sweep([])
    beat = json.load(open(lr.STATE))[lr.CONFIG].get("last_sweep")
    check("heartbeat written even with nothing armed", bool(beat), True)

    # ---- 9. Remote Control watchdog ---------------------------------------
    root, argv_log, lr = scenario("9. Remote Control watchdog")
    launched = []
    real_run = subprocess.run
    lr.subprocess.run = lambda *a, **k: launched.append(a[0]) or type(
        "R", (), {"stdout": "", "stderr": "", "returncode": 0})()
    lr.RC_LAUNCHER = os.path.join(root, "claude-rc.command")
    open(lr.RC_LAUNCHER, "w").write("#!/bin/sh\ntrue\n")

    lr.rc_running = lambda _folder: False
    check("disabled -> never launches", lr.rc_watchdog({lr.CONFIG: {}}), False)

    lr.rc_running = lambda _folder: True
    st = {lr.CONFIG: {"rc_watch": True, "rc_tries": 2}}
    check("running -> no launch", lr.rc_watchdog(st), False)
    check("running -> breaker reset", st[lr.CONFIG]["rc_tries"], 0)

    lr.rc_running = lambda _folder: False
    st = {lr.CONFIG: {"rc_watch": True}}
    check("missing -> launches", lr.rc_watchdog(st), True)
    check("launched via open", launched[-1][0] if launched else None, "open")
    check("immediately after -> throttled", lr.rc_watchdog(st), False)

    for _ in range(4):
        st[lr.CONFIG]["rc_last_launch"] = 0
        lr.rc_watchdog(st)
    check("breaker caps tries", st[lr.CONFIG]["rc_tries"], lr.RC_MAX_TRIES)
    st[lr.CONFIG]["rc_last_launch"] = 0
    check("breaker blocks further tries", lr.rc_watchdog(st), False)

    lr.rc_running = lambda _folder: True
    lr.rc_watchdog(st)
    check("recovery resets breaker", st[lr.CONFIG]["rc_tries"], 0)

    # a missing launcher must not be retried forever either
    lr.rc_running = lambda _folder: False
    lr.RC_LAUNCHER = os.path.join(root, "gone.command")
    st = {lr.CONFIG: {"rc_watch": True}}
    check("missing launcher -> no launch", lr.rc_watchdog(st), False)
    check("missing launcher -> breaker tripped", st[lr.CONFIG]["rc_tries"], lr.RC_MAX_TRIES)
    subprocess.run = real_run         # lr.subprocess IS the global module

    rc_watchdog_scenarios()
    launcher_scenarios()        # after the real subprocess.run is restored
    deadman_scenarios()
    batch2_scenarios()
    installer_scenarios()
    installer_decisions()
    os.environ["PATH"] = original_path
    jobs_scenarios()
    commit_scenarios()
    decisions_scenarios()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
