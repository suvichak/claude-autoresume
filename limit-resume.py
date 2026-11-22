#!/usr/bin/env python3
"""Resume Claude Code sessions cut off by the 5-hour usage limit.

Zero tokens while waiting: this is plain Python run by launchd. The only spend
is the `claude --resume` call itself, once the limit has actually reset.

  limit-resume.py arm [session-id] [-- prompt...]   watch one session
  limit-resume.py auto on|off                       watch every session
  limit-resume.py sweep                             launchd entry point
  limit-resume.py rc on|off                         keep Remote Control running
  limit-resume.py status | disarm <id> | disarm all
  limit-resume.py doctor | notify [text]            health check | test alert
  limit-resume.py deadman                           alert once if the sweep has stopped
                                                    (run by the Remote Control launcher)
  limit-resume.py reload-check                      what a scheduler reload would kill
                                                    (install.sh / uninstall.sh; exit 1 if any)
  limit-resume.py jobs on [dir] [--cwd path] [--reset "Mon 03:00"] | off | stop | status | probe
                                                    background job queue (Remote Control)
  limit-resume.py statusline [-- command...]        status line feed (usage cache)
  limit-resume.py guard                             PreToolUse hook for job sessions
  limit-resume.py git-check <git args>              the job git wrapper's check (exit 2: refused)
  limit-resume.py maintain [--install|--scheduled] [--usage-json <json>|-]
                                                    weekly maintenance (reset date, tidy)
  limit-resume.py task-spec                         the Claude scheduled task that runs it

In auto mode any session whose transcript ends on an unresolved quota 429 (the
5-hour or the weekly window) is eligible, subject to AUTO_MAX_AGE_H counted from
the reset (don't resurrect dead conversations) and MAX_PER_SWEEP (don't re-burn
the fresh window on a pile-up).

Background jobs: a folder of job notes (one Markdown note per job), run one at
a time as Remote Control sessions on quota that would otherwise go unused. See JOB_* below
and the jobs section of README.md.
"""
import datetime
import fcntl
import glob
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid as uuidlib

VERSION = "2026-11-23.1"

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
TOOLS = os.path.join(HOME, ".claude", "tools")
STATE = os.path.join(TOOLS, "limit-resume.state.json")
LOG = os.path.join(TOOLS, "limit-resume.log")

BUFFER = 90          # seconds to wait past resetsAt before retrying
MAX_RESUMES = 8      # per session, then give up rather than burn quota forever
TAIL = 400           # transcript lines to inspect
AUTO_MAX_AGE_H = 12  # auto mode ignores blocks whose limit reset longer ago than this
AGE_ALERT_H = 7 * 24  # a block skipped for age alerts once if it reset within this
MAX_PER_SWEEP = 2    # resumes launched per sweep, newest session first
RESUME_TIMEOUT = 3600  # seconds before an unattended resume is stopped (whole process group)
STATE_LOCK_WAIT = 300  # s a sweep or command waits for the state lock, then gives up unchanged
CHAT_NEW_AT = 700000   # context tokens above which a chat continues in a new session
RESUME_BUSY = 900      # s a resumed chat counts as running after its last transcript write
RC_LAUNCHER = os.path.join(HOME, ".claude", "tools", "claude-rc.command")
RC_PATTERN = r"^(/[^ ]*/)?claude (rc|remote-control)([ ]|$)"
RC_MIN_INTERVAL = 600   # never relaunch Remote Control more than once per 10 min
RC_MAX_TRIES = 3        # consecutive failed starts before standing down
# A session Remote Control spawned. Only reaped once its server is gone
# (reparented to launchd, ppid 1), so live servers' sessions and the desktop
# app's own sessions are never touched.
RC_CHILD_MARK = "--sdk-url https://api.anthropic.com/v1/code/sessions/"
REAP_GRACE = 5          # seconds between SIGTERM and SIGKILL
# A Claude Code update leaves the running server on its old binary. It is only
# replaced once its sessions have been quiet this long, so no turn is cut off.
RC_IDLE = 600           # seconds without session traffic in the server's debug log
RC_STOP_GRACE = 20      # seconds a stale server gets to exit before SIGKILL
RC_STALE_MAX = 7200     # busy this long after an update -> restarted anyway
RC_ACTIVITY = re.compile(r"^(\S+) \[DEBUG\] \[bridge:(ws|activity)\]")
# A live server writes its debug log at least every ~9.5 min even with no
# sessions (work polls; heartbeats every ~3 min with any open). On 2026-10-27
# one froze for 23 h while still running, so the "is it running" check never
# fired. Silent this long, and still silent RC_HUNG_CONFIRM later, it is
# killed and relaunched. The second look keeps a VM just woken from suspend
# from being taken for hung.
RC_HUNG = 900
RC_HUNG_CONFIRM = 600
# Written by the launcher when it ends: "<end epoch> <auth|network|exit> ...".
RC_STATUS = os.path.join(HOME, ".claude", "tools", "rc-last-exit")
RC_LOCK = os.path.join(HOME, ".claude", "tools", "rc-launch.lock")   # held by a live launcher
RC_DEBUG = os.path.join(TOOLS, "rc-debug")   # holds bridge-transcript-cse_*.jsonl
RC_HEAD = 65536         # bytes of a bridge transcript searched for the session's first uuid
CC_SOCKS = "/tmp/cc-socks"   # <pid>.sock: each local Claude session's cross-session inbox
SEND_MODEL = "claude-haiku-5-5"   # the throwaway session that only relays the prompt
SEND_TIMEOUT = 300
RELAY_DENY = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"   # the relay only needs SendMessage
NET_PROBE = "https://api.anthropic.com/"
NET_TIMEOUT = 3         # seconds; a slower API than this is treated as down
# Pushover keys live in the login Keychain, never in a file:
#   security add-generic-password -U -s limit-resume-pushover -a token -w
#   security add-generic-password -U -s limit-resume-pushover -a user -w
PUSHOVER_SERVICE = "limit-resume-pushover"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
CRED_SERVICE = "Claude Code-credentials"
CRED_FILE = os.path.join(HOME, ".claude", ".credentials.json")
CONFIG = "__config__"
INSTALL_CONF = os.path.join(TOOLS, "install.conf")   # what install.sh used: rc_dir= (one
                                                     # line per folder), label=


def install_settings(key):
    """Every value install.sh saved for `key` (`key=value` lines), in order."""
    values = []
    try:
        with open(INSTALL_CONF) as fh:
            for line in fh:
                name, sep, value = line.rstrip("\n").partition("=")
                if sep and name == key and value:
                    values.append(value)
    except OSError:
        pass
    return values


def install_setting(key, default):
    """The first value install.sh saved for `key`, else `default`."""
    values = install_settings(key)
    return values[0] if values else default


LABEL = (os.environ.get("LIMIT_RESUME_LABEL")
         or install_setting("label", "com.suvichak.claude-limit-resume"))
SWEEP_STALE = 900       # s without a sweep heartbeat before doctor and `deadman` complain
DEADMAN = os.path.join(TOOLS, "sweep-stale")   # `deadman`'s note: "<beat>" or "<beat> alerted"
# A sweep, or a job / continued chat under its pty helper, as ps shows it.
AGENT_PROC = re.compile(r"^\S*[Pp]ython\S* .*limit-resume\.py (sweep|jobpty)( |$)")
CLAUDE_PATH_HINT = os.path.join(HOME, ".local", "bin")
# A resume runs in the mode the user last ran that session in, so it gets the
# same rights it had: no more, no less. PERM is only the fallback when the
# transcript names no mode; LIMIT_RESUME_PERMISSION_MODE forces one for all.
PERM_FORCED = os.environ.get("LIMIT_RESUME_PERMISSION_MODE")
PERM = PERM_FORCED or "acceptEdits"
PERM_MODES = {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
PERM_ALIASES = {"default": "manual"}      # older CLIs recorded "default"
DEFAULT_PROMPT = (
    "Continue the work that the usage limit interrupted. Do not re-explore what "
    "you already know. Nobody is at the keyboard, so do not ask questions and do "
    "not wait for confirmation. If there is no unfinished work, or you cannot "
    "proceed without an answer from the user, reply with exactly DONE and stop."
)


def log(msg):
    line = "%s %s" % (datetime.datetime.now().isoformat(timespec="seconds"), msg)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # A full disk or a missing folder must not turn an alert or a crash
        # report into a second crash.
        sys.stderr.write("%s  (log not written: %s)\n" % (line, exc))
    print(line)


# The state lock: an flock on STATE + ".lock", held by a sweep for its whole
# run and by every command that writes the state, so neither saves back a copy
# the other has changed meanwhile. The kernel drops it however the holder dies.
_LOCK_FD = None


def lock_state(wait):
    """Take the state lock, waiting up to `wait` s. True once held."""
    global _LOCK_FD
    fd = os.open(STATE + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.time() + wait
    told = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _LOCK_FD = fd
            return True
        except OSError:
            pass
        if time.time() >= deadline:
            os.close(fd)
            return False
        if not told:
            print("waiting for a running sweep or command to let go of the state file ...")
            told = True
        time.sleep(0.5)


def unlock_state():
    global _LOCK_FD
    if _LOCK_FD is not None:
        os.close(_LOCK_FD)
        _LOCK_FD = None


def with_state_lock(cmd):
    """cmd(argv), run holding the state lock. Gives up, changing nothing, when
    another holder keeps it for STATE_LOCK_WAIT s."""
    def run(argv):
        if _LOCK_FD is not None:
            return cmd(argv)               # already held by this process
        if not lock_state(STATE_LOCK_WAIT):
            log("%s: the state file stayed locked for %d s (a sweep or another command); "
                "nothing changed - try again" % (cmd.__name__[4:], STATE_LOCK_WAIT))
            return 1
        try:
            return cmd(argv)
        finally:
            unlock_state()
    run.__name__ = cmd.__name__
    return run


def read_json(path):
    """The dict in `path`. FileNotFoundError if there is none, ValueError if it
    holds anything else."""
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def load_state():
    """The state, {} before the first save.

    A state file that cannot be read is never saved over. The lock holder
    moves it aside as .corrupt-<time>, alerts, and carries on from the copy of
    the last good save (.bak). A reader without the lock only uses the .bak."""
    try:
        return read_json(STATE)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        problem = exc
    try:
        backup = read_json(STATE + ".bak")
    except (OSError, ValueError):
        backup = {}
    if _LOCK_FD is not None:
        kept = "%s.corrupt-%s" % (STATE, datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
        try:
            os.replace(STATE, kept)
        except OSError as exc:
            kept = "nowhere (%s)" % exc
        notify("The auto-resume state file could not be read (%s). It is kept as %s; "
               "carrying on from %s. Run `limit-resume.py doctor`."
               % (problem, kept, "the last good copy" if backup else "an empty state"))
    return backup


def write_json(path, data):
    # Written beside it and renamed into place: a reader never sees half a
    # file, and a crash mid-write leaves the previous one, not an empty one.
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_state(state):
    write_json(STATE, state)
    write_json(STATE + ".bak", state)      # what load_state falls back on


def auto_on(state):
    return state.get(CONFIG, {}).get("auto_all", False)


def sessions(state):
    """Armed session ids, skipping the pseudo-entries (__config__, __jobs__)."""
    return [k for k in state if not k.startswith("__")]


def session_file(sid):
    hits = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
    return hits[0] if hits else None


def latest_session():
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    if not files:
        return None
    return os.path.basename(max(files, key=os.path.getmtime))[:-6]


def tail_lines(path, n):
    """The last `n` lines of a file, read from its end: a sweep looks at every
    transcript, and the large ones run to many MB."""
    with open(path, "rb") as fh:
        pos = fh.seek(0, os.SEEK_END)
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    return data.decode("utf-8", "replace").splitlines()[-n:]


def tail_records(path):
    """The JSON objects among the last TAIL lines; other lines are skipped."""
    out = []
    for line in tail_lines(path, TAIL):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def rate_limit_stop(rec):
    """Quota rejection, or None.

    Deliberately strict: a session is only resumable if it stopped because the
    plan's quota window was exhausted. Every other way a turn dies — 529
    overloaded, `Connection error.`, `server_error`, connection lost
    mid-response — is a transient or unknown failure that must NOT be retried
    unattended, and none of them carry these fields.
    """
    if rec.get("error") != "rate_limit" or rec.get("apiErrorStatus") != 429:
        return None
    quota = rec.get("quotaLimits")
    if not isinstance(quota, dict) or quota.get("status") != "rejected":
        return None            # a warning/allowed quota record is not a stop
    window = quota.get("rateLimitType")
    resets_at = quota.get("resetsAt")
    if not window or not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        return None            # no window or no reset time = nothing to wait for
    # A stop without a uuid gets one from its reset time, so `handled` never
    # holds a None that would match every later uuid-less stop.
    return {"uuid": rec.get("uuid") or "noid-%d" % int(resets_at), "resets_at": int(resets_at),
            "when": rec.get("timestamp"), "window": window}


def find_block(path):
    """The quota stop a transcript ends on, or None if it ended any other way."""
    block = None
    for rec in tail_records(path):
        stop = rate_limit_stop(rec)
        if stop:
            block = stop
        elif rec.get("type") in ("assistant", "user"):
            # Real work after the stop means it already continued. Bookkeeping
            # records (last-prompt, atis-latch, attachment) do follow a 429 and
            # must not count as continuation.
            block = None
    return block


def context_tokens(path):
    """Context size of the session's last real turn (input + cache tokens, as
    /context counts it), or 0 if unknown. Error stubs carry zero usage and
    subagent turns their own context, so both are skipped."""
    try:
        recs = tail_records(path) if path else []
    except OSError:
        return 0
    for rec in reversed(recs):
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        usage = (rec.get("message") or {}).get("usage") or {}
        total = sum(usage.get(k) or 0 for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            if isinstance(usage.get(k) or 0, int))
        if total:
            return total
    return 0


def first_prompt(path):
    """(uuid, text) of the session's first real user message, or (None, "")."""
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "user" or rec.get("isMeta"):
                    continue
                text = text_of((rec.get("message") or {}).get("content"))
                if text:
                    return rec.get("uuid"), text
    except OSError:
        pass
    return None, ""


def rc_session(path):
    """The Remote Control session (cse_...) that owns this transcript, or None.

    A Remote Control session runs as a child of the rc server and streams to
    claude.ai; the phone shows that stream. A headless `claude --resume -p`
    only appends to the local transcript, so the phone never sees the turn and
    the live child never learns of it (2026-10-26: a headless turn submitted
    a pending form while the chat still asked whether to submit it). The first
    user message's uuid appears in both the transcript and the server's
    bridge-transcript-cse_<id>.jsonl, which is how ownership is detected.
    """
    uuid, _ = first_prompt(path)
    if not uuid:
        return None
    for bridge in glob.glob(os.path.join(RC_DEBUG, "bridge-transcript-cse_*.jsonl")):
        try:
            with open(bridge, errors="replace") as fh:
                head = fh.read(RC_HEAD)
        except OSError:
            continue
        if '"uuid":"%s"' % uuid in head or '"uuid": "%s"' % uuid in head:
            return os.path.basename(bridge)[len("bridge-transcript-"):-len(".jsonl")]
    return None


def block_age_hours(when):
    """Hours since the stop itself (its timestamp), 1e9 if it has none."""
    try:
        stamped = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return 1e9
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - stamped).total_seconds() / 3600.0


def hours_since_reset(block, now):
    """How long the block has been resumable: hours since its limit reset
    (negative while it has not)."""
    return (now - block["resets_at"]) / 3600.0


def is_job_transcript(path):
    """Was this session started as a background job (its first prompt is the
    job rules)? Jobs pause and resume themselves, never as an ordinary chat."""
    return first_prompt(path)[1].startswith(JOB_RULES[:80])


def note_unreadable(state, bad):
    """Log each transcript that could not be read once, not every sweep."""
    cfg = state.setdefault(CONFIG, {})
    seen = cfg.get("unreadable") or {}
    for sid, why in sorted(bad.items()):
        if seen.get(sid) != why:
            log("skipping transcript of %s, it could not be read: %s" % (sid, why))
    if bad:
        cfg["unreadable"] = bad
    else:
        cfg.pop("unreadable", None)


def candidates(state, aged=None):
    """[(sid, path, block)] eligible for resume now, newest transcript first.

    Auto mode's own entries (origin auto) are ignored while it is off, and they
    keep its age guard. Blocks skipped for age are appended to `aged`, if given.
    One transcript that cannot be read is skipped, never fatal for the rest."""
    auto = auto_on(state)
    paths = {}
    for sid in sessions(state):
        entry = state[sid]
        if entry.get("done") or (entry.get("origin") == "auto" and not auto):
            continue
        path = session_file(sid)
        if path:
            paths[sid] = path
    if auto:
        # Background jobs pause and resume themselves; the usage probe and the
        # Remote Control relay (both run in TOOLS) are throwaways. None is ever
        # resumed as an ordinary session.
        skip = set(job_sids(state))
        probe_dir = project_dir_name(TOOLS)
        for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
            sid = os.path.basename(path)[:-6]
            if sid in paths or sid in skip or state.get(sid, {}).get("done"):
                continue
            if os.path.basename(os.path.dirname(path)) == probe_dir:
                continue
            paths[sid] = path

    out, bad = [], {}
    now = time.time()
    for sid, path in paths.items():
        try:
            block = find_block(path)
            if not block:
                continue
            found_by_auto = sid not in state or state[sid].get("origin") == "auto"
            if found_by_auto and is_job_transcript(path):
                continue
            # Age guard: auto mode only, counted from the reset, i.e. how long
            # the block has been resumable. An explicitly armed session is
            # watched however long it takes.
            if found_by_auto and hours_since_reset(block, now) > AUTO_MAX_AGE_H:
                if aged is not None:
                    aged.append((sid, path, block))
                continue
            out.append((os.path.getmtime(path), sid, path, block))
        except Exception as exc:          # a vanished or malformed transcript
            bad[sid] = "%s: %s" % (type(exc).__name__, exc)
    note_unreadable(state, bad)
    out.sort(key=lambda row: row[0], reverse=True)
    return [(sid, path, block) for _mtime, sid, path, block in out]


@with_state_lock
def cmd_arm(argv):
    prompt = DEFAULT_PROMPT
    if "--" in argv:
        cut = argv.index("--")
        prompt = " ".join(argv[cut + 1:]) or DEFAULT_PROMPT
        argv = argv[:cut]
    sid = argv[0] if argv else os.environ.get("CLAUDE_SESSION_ID") or latest_session()
    if not sid:
        log("arm: no session to arm")
        return 1
    if not session_file(sid):
        log("arm: no transcript for %s" % sid)
        return 1
    state = load_state()
    state[sid] = {"prompt": prompt, "resumes": 0, "handled": []}
    save_state(state)
    log("armed %s" % sid)
    return 0


@with_state_lock
def cmd_auto(argv):
    if not argv or argv[0] not in ("on", "off"):
        print("usage: limit-resume.py auto on|off")
        return 2
    state = load_state()
    state.setdefault(CONFIG, {})["auto_all"] = (argv[0] == "on")
    save_state(state)
    log("auto mode %s (max age %dh, max %d resumes per sweep)"
        % (argv[0], AUTO_MAX_AGE_H, MAX_PER_SWEEP))
    return 0


@with_state_lock
def cmd_rc(argv):
    if not argv or argv[0] not in ("on", "off"):
        print("usage: limit-resume.py rc on|off")
        return 2
    state = load_state()
    cfg = state.setdefault(CONFIG, {})
    cfg["rc_watch"] = (argv[0] == "on")
    cfg["rc_tries"] = 0                   # a fresh `rc on` resets the breaker
    cfg["rc_reset"] = time.time()         # and forgets an earlier login failure
    cfg.pop("rc_last_launch", None)       # no throttle: the next sweep may launch
    for flag in RC_FLAGS:
        cfg.pop(flag, None)
    cfg.pop("rc_more", None)              # the other folders' breakers and throttles too
    save_state(state)
    log("Remote Control watchdog %s (running now: %s)" % (argv[0], ", ".join(
        "%s %s" % (folder["name"] or "any", rc_running(folder)) for folder in rc_folders())))
    return 0


@with_state_lock
def cmd_disarm(argv):
    """disarm <sid>: never resume that session again (`arm` undoes it).
    disarm all: auto mode and the Remote Control watch off, and every armed
    session becomes an auto entry, ignored while auto mode is off. Both keep the
    blocks already handled, so a later `auto on` never resumes one twice. The
    job queue has its own on/off."""
    state = load_state()
    if argv and argv[0] == "all":
        cfg = state.setdefault(CONFIG, {})
        cfg["auto_all"] = False
        cfg["rc_watch"] = False           # uninstall.sh relies on this
        for sid in sessions(state):
            state[sid]["origin"] = "auto"
    elif argv:
        old = state.get(argv[0]) or {}
        state[argv[0]] = {"done": True, "why": "disarmed", "origin": old.get("origin"),
                          "resumes": old.get("resumes", 0), "handled": old.get("handled", [])}
    save_state(state)
    log("disarmed %s" % (argv[0] if argv else ""))
    return 0


def cmd_status(_argv):
    state = load_state()
    print("auto mode: %s" % ("ON — every session" if auto_on(state) else "off"))
    by_auto = [sid for sid in sessions(state) if state[sid].get("origin") == "auto"]
    for title, group in (
            ("armed explicitly:", [s for s in sessions(state) if s not in by_auto]),
            ("resumed by auto mode%s:" % ("" if auto_on(state) else " (ignored while it is off)"),
             by_auto)):
        if group:
            print(title)
        for sid in group:
            entry = state[sid]
            flag = " done (%s)" % entry.get("why", "retired") if entry.get("done") else ""
            print("  %s  resumes=%d%s" % (sid, entry.get("resumes", 0), flag))
    ready = candidates(state)
    if not ready:
        print("no session is currently blocked")
        return 0
    now = int(datetime.datetime.now().timestamp())
    print("blocked now:")
    for sid, _path, block in ready:
        when = datetime.datetime.fromtimestamp(block["resets_at"]).strftime("%m-%d %H:%M")
        wait = max(0, block["resets_at"] + BUFFER - now) // 60
        kind = "armed" if sid in state and state[sid].get("origin") != "auto" else "auto"
        print("  %s (%s)  %s quota, resumes at %s  ~%dm"
              % (sid, kind, block["window"], when, wait))
    return 0


def rc_folders():
    """One record per folder Remote Control serves: install.conf's rc_dir= lines,
    in order. Each has its dir, the --name its launcher gives the server
    (auto-<basename>), its launcher, the launcher's exit status file and lock,
    `key` (where its watchdog state lives, None: in the config itself) and
    `where` (" for <basename>" in messages once there are several).

    The first folder keeps the file names of the one-folder kit, so a server
    its launcher started before the upgrade stays watched. A later folder
    whose name is taken is skipped; install.sh refuses one. With no folder
    saved there is one record without dir or name, and any server counts, as
    before install.conf existed."""
    out = []
    for folder in install_settings("rc_dir"):
        base = os.path.basename(folder.rstrip("/"))
        if base and not any(f["name"] == "auto-" + base for f in out):
            out.append(rc_folder(folder, base, "-" + base if out else ""))
    if len(out) > 1:
        for f in out:
            f["where"] = " for " + os.path.basename(f["dir"].rstrip("/"))
    return out or [rc_folder(None, None, "")]


def rc_folder(folder, base, tag):
    """The record for `folder` (see rc_folders); tag is "" for the first one."""
    launcher, ext = os.path.splitext(RC_LAUNCHER)
    lock, lock_ext = os.path.splitext(RC_LOCK)
    name = "auto-" + base if base else None
    return {"dir": folder, "name": name, "key": name if tag else None, "where": "",
            "launcher": launcher + tag + ext, "status": RC_STATUS + tag,
            "lock": lock + tag + lock_ext}


def rc_state(cfg, folder):
    """The watchdog's keys for `folder`: the config itself for the first folder,
    where the one-folder kit kept them, else a dict of its own under rc_more
    (`rc on` drops rc_more, which resets every folder)."""
    if folder["key"] is None:
        return cfg
    return cfg.setdefault("rc_more", {}).setdefault(
        folder["key"], {"rc_reset": cfg.get("rc_reset", 0)})


def same_dir(a, b):
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def signalable(pid):
    """"mine", "other user" (a process this user may not signal) or "gone"."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "other user"
    return "mine"


def cwds(pids):
    """{pid: working directory} of `pids`, from one lsof call, or None when lsof
    cannot tell: it is missing, times out, or leaves out a process of this user
    that is still running. A pid that has exited since, or that is another
    user's, is left out."""
    pids = sorted(set(pids))
    if not pids:
        return {}
    try:
        out = subprocess.run(["lsof", "-a", "-p", ",".join(str(p) for p in pids), "-d", "cwd",
                              "-Fn"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    found, pid = {}, None
    for line in (out or "").splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            pid = int(line[1:])
        elif line[:1] == "n" and pid is not None:
            found[pid] = line[1:]
    if any(p not in found and signalable(p) == "mine" for p in pids):
        return None
    return found


def rc_servers(rows):
    """[(pid, command)] of the Remote Control servers among ps `rows`.

    pgrep does not see these on macOS - `pgrep -f "claude rc"` reports nothing
    while one is plainly running - so match ps output. The pattern is anchored
    to the start of the command line, or shell wrappers that merely mention
    "claude rc" count as a false positive."""
    return [(pid, cmd) for pid, _ppid, cmd in rows if re.match(RC_PATTERN, cmd)]


def rc_serving(rows, folder):
    """Who serves `folder`: ("launcher", pid) for the server its launcher named,
    ("by hand", pid) for another server whose working directory is the folder
    (one started by hand there; a second server for a folder gets a 409, so
    its launcher must not try), ("unknown", None) when ps or lsof cannot tell,
    or (None, None). A server for another folder does not count."""
    if rows is None:
        return "unknown", None
    server = rc_server(rows, folder)
    if server:
        return "launcher", server[0]
    others = [pid for pid, _cmd in rc_servers(rows)]
    if not others:
        return None, None
    where = cwds(others)
    if where is None:
        return "unknown", None
    for pid in others:
        if same_dir(where.get(pid), folder["dir"]):
            return "by hand", pid
    return None, None


def rc_running(folder):
    """Is a Remote Control server serving `folder` up? When ps or lsof cannot
    tell, yes: never spawn blind."""
    return rc_serving(ps_rows(), folder)[0] is not None


def net_ok():
    """Does the API answer quickly? Any HTTP reply counts.

    Uses curl, the same probe as the launcher. If the probe itself cannot run,
    assume the network is fine rather than block every relaunch on it.
    """
    try:
        proc = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time",
                               str(NET_TIMEOUT), NET_PROBE],
                              capture_output=True, timeout=NET_TIMEOUT + 10)
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return True
    return proc.returncode == 0


def launcher_busy(folder):
    """Is this folder's launcher alive right now - waiting for the network, or serving?"""
    try:
        fd = os.open(folder["lock"], os.O_RDWR)
    except OSError:
        return False                       # no lock file, or an old directory lock
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)                       # also drops the lock if we took it
    return False


def rc_last_exit(folder):
    """(end epoch, reason) this folder's launcher recorded for its last run."""
    try:
        with open(folder["status"]) as fh:
            parts = fh.read().split()
        return float(parts[0]), parts[1]
    except (IOError, ValueError, IndexError):
        return 0, None


def cred_mtime():
    """When the stored login last changed (epoch), or 0 if that is unknown.

    Reads Keychain attributes only, never the secret. A change after a login
    failure means someone ran `claude /login`, so a relaunch is worth a try.
    """
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", CRED_SERVICE],
                             capture_output=True, text=True, timeout=20).stdout
        stamp = re.search(r'"mdat"<timedate>=\S+\s+"(\d{14})Z', out or "")
        if stamp:
            when = datetime.datetime.strptime(stamp.group(1), "%Y%m%d%H%M%S")
            return when.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        pass
    try:
        return os.path.getmtime(CRED_FILE)
    except OSError:
        return 0


def keychain(account):
    try:
        proc = subprocess.run(["security", "find-generic-password", "-s", PUSHOVER_SERVICE,
                               "-a", account, "-w"],
                              capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def notify(msg):
    """Tell a human. Returns the channel used: pushover, local, log or retry.

    Pushover when its keys are in the Keychain, otherwise a notification on this
    machine's own screen. `retry` means Pushover is set up but the post failed,
    so the caller should send it again later. Never raises - an alert must not
    break the sweep.
    """
    log("ALERT: %s" % msg)
    token, user = keychain("token"), keychain("user")
    wanted = bool(token and user)
    if wanted:
        try:
            body = urllib.parse.urlencode({
                "token": token, "user": user, "message": msg,
                "title": "Claude on %s" % os.uname().nodename.split(".")[0]}).encode()
            urllib.request.urlopen(PUSHOVER_URL, body, timeout=20)
            return "pushover"
        except Exception as exc:
            log("  pushover failed: %s" % exc)
    try:
        proc = subprocess.run(["osascript", "-e", "display notification %s with title %s"
                               % (json.dumps(msg), json.dumps("Claude watchdog"))],
                              capture_output=True, timeout=20)
        shown = proc.returncode == 0
    except Exception:
        shown = False
    if wanted:
        return "retry"
    return "local" if shown else "log"


def cmd_notify(argv):
    how = notify(" ".join(argv) or "Test alert from limit-resume.py")
    print("sent via %s" % how)
    if how == "retry":
        print("Pushover keys found but the post failed - see the log")
    elif how != "pushover":
        print("No Pushover keys - add them to the Keychain:")
        for account in ("token", "user"):
            print("  security add-generic-password -U -s %s -a %s -w"
                  % (PUSHOVER_SERVICE, account))
    return 0


def rc_orphans(folder):
    """[(pid, command)] of Remote Control sessions whose server has died, and
    that ran in `folder` (a session works in its server's folder): another
    folder's are left to that folder. Every one when the folder has no dir."""
    rows = ps_rows()
    if rows is None:
        return []            # can't tell -> kill nothing
    found = [(pid, cmd) for pid, ppid, cmd in rows
             if ppid == 1 and RC_CHILD_MARK in cmd and pid != os.getpid()]
    if not found or folder["dir"] is None:
        return found
    where = cwds([pid for pid, _cmd in found])
    if where is None:
        return []            # can't tell -> kill nothing
    return [(pid, cmd) for pid, cmd in found if same_dir(where.get(pid), folder["dir"])]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def rc_reap_orphans(folder):
    """Kill this folder's orphaned Remote Control sessions before a new server
    is spawned for it.

    When the server dies without taking its sessions down, they stay attached to
    their claude.ai sessions and pile up with every relaunch. SIGTERM first,
    SIGKILL whatever is still there after REAP_GRACE seconds.
    """
    orphans = rc_orphans(folder)
    for pid, cmd in orphans:
        log("rc watchdog: killing orphaned session pid %d: %s" % (pid, cmd[:120]))
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log("  could not signal pid %d: %s" % (pid, exc))
    deadline = time.time() + REAP_GRACE
    left = [pid for pid, _ in orphans]
    while left and time.time() < deadline:
        time.sleep(0.5)
        left = [pid for pid in left if pid_alive(pid)]
    for pid in left:
        log("  pid %d ignored SIGTERM, sending SIGKILL" % pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return len(orphans)


def ps_rows():
    """[(pid, ppid, command)] for every process, or None if ps fails."""
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def version_of(path):
    """'2.1.292' for a native install's .../claude/versions/2.1.292, else None."""
    if path and os.path.basename(os.path.dirname(path)) == "versions":
        return os.path.basename(path)
    return None


def installed_version():
    """The version an update has put behind `claude` on PATH, or None."""
    claude = shutil.which("claude", path=os.environ.get("PATH", "") + ":" + CLAUDE_PATH_HINT)
    return version_of(os.path.realpath(claude)) if claude else None


def proc_version(pid):
    """The version of the binary process `pid` is running, or None."""
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return version_of(line[1:])      # the first txt entry is the executable
    return None


def rc_server(rows, folder):
    """(pid, debug log or None) of the server this folder's launcher started, or None.

    Only the server named folder["name"] counts (any server when the folder
    has no name), so the stale and hung checks never stop another folder's
    server or a `claude rc` started by hand. ps does not quote arguments, so
    each one runs up to the next ` --` (a path may hold spaces)."""
    name = folder["name"]
    for pid, cmd in rc_servers(rows):
        if name and not re.search(r" --name %s(?= --|$)" % re.escape(name), cmd):
            continue
        m = re.search(r" --debug-file (.+?)(?= --|$)", cmd)
        return pid, m.group(1) if m else None
    return None


def last_activity(debug_log):
    """Epoch of the server's last session traffic, 0 if none in the tail, or
    None if the log cannot be read. Heartbeats do not count."""
    try:
        with open(debug_log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 256 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (IOError, OSError, TypeError):
        return None
    for line in reversed(lines):
        m = RC_ACTIVITY.match(line)
        if not m:
            continue
        try:
            when = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            continue
        return when.replace(tzinfo=datetime.timezone.utc).timestamp()
    return 0


def rc_busy(rows, pid, debug_log):
    """Is any session of server `pid` mid-turn? When unsure, yes.

    An idle session process has no children. During a turn it has its tool's
    shell, and the CLI's `caffeinate`. A turn that is only streaming from the
    model shows up as recent bridge traffic in the server's debug log."""
    sessions = {p for p, pp, _ in rows if pp == pid}
    if any(pp in sessions for _, pp, _ in rows):
        return True
    last = last_activity(debug_log) if debug_log else None
    return last is None or time.time() - last < RC_IDLE


def rc_restart_stale(cfg, folder):
    """Stop a Remote Control server that an update has left on an old binary.

    A running server never updates itself, and the sessions it spawns run its
    own binary with the updater off. Remote Control has a minimum version, so
    an old server can stay up without serving, and the watchdog, seeing it
    run, would never replace it. It waits for idle sessions, but no longer
    than RC_STALE_MAX. True once the old server is gone. cfg: the folder's
    watchdog state (rc_state)."""
    rows = ps_rows()
    server = rc_server(rows or [], folder)
    if not server:
        return False
    pid, debug_log = server
    running, installed = proc_version(pid), installed_version()
    if not running or not installed or running == installed:
        cfg.pop("rc_stale_wait", None)
        cfg.pop("rc_stale_since", None)
        return False
    since = cfg.setdefault("rc_stale_since", time.time())
    overdue = time.time() - since >= RC_STALE_MAX
    if not overdue and rc_busy(rows, pid, debug_log):
        if cfg.get("rc_stale_wait") != installed:
            cfg["rc_stale_wait"] = installed
            log("rc watchdog: Remote Control%s runs %s but %s is installed; restarting "
                "it once no session is mid-turn, or in %d min at the latest"
                % (folder["where"], running, installed,
                   (RC_STALE_MAX - (time.time() - since)) // 60))
        return False
    cfg.pop("rc_stale_wait", None)
    cfg.pop("rc_stale_since", None)
    log("rc watchdog: restarting Remote Control%s on %s (was %s), %s" % (
        folder["where"], installed, running,
        "still busy after %d min" % (RC_STALE_MAX // 60) if overdue else "its sessions are idle"))
    return rc_stop(pid, folder)


def rc_restart_hung(cfg, folder):
    """Stop a Remote Control server that is running but no longer working.

    Its debug log is the heartbeat: silent for RC_HUNG, and unchanged when
    looked at again RC_HUNG_CONFIRM later, means it is hung. Left alone, the
    server drops it and every message sent to the machine is lost until the
    process happens to die. Never during an outage, when a quiet server may
    only be waiting for the network. True once the hung server is gone."""
    rows = ps_rows()
    server = rc_server(rows or [], folder)
    try:
        mtime = os.path.getmtime(server[1]) if server else None
    except (OSError, TypeError):
        mtime = None
    now = time.time()
    if mtime is None or now - mtime < RC_HUNG:
        cfg.pop("rc_hung_seen", None)
        return False
    pid = server[0]
    seen = cfg.get("rc_hung_seen")
    if not seen or seen[0] != pid or seen[1] != mtime:
        cfg["rc_hung_seen"] = [pid, mtime, now]
        log("rc watchdog: Remote Control%s (pid %d) has logged nothing for %d min; "
            "restarting it if still silent in %d min"
            % (folder["where"], pid, (now - mtime) // 60, RC_HUNG_CONFIRM // 60))
        return False
    if now - seen[2] < RC_HUNG_CONFIRM or not net_ok():
        return False
    cfg.pop("rc_hung_seen", None)
    since = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    notify("Remote Control%s froze: running, but silent since %s (%d min). "
           "Restarting it." % (folder["where"], since, (now - mtime) // 60))
    return rc_stop(pid, folder)


def rc_stop(pid, folder):
    """SIGTERM server `pid`, SIGKILL it after RC_STOP_GRACE, and wait for its
    launcher (this folder's) to record the exit. True once it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        log("  could not signal pid %d: %s" % (pid, exc))
        return False
    deadline = time.time() + RC_STOP_GRACE
    while pid_alive(pid) and time.time() < deadline:
        time.sleep(0.5)
    if pid_alive(pid):
        log("  pid %d ignored SIGTERM, sending SIGKILL" % pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    while launcher_busy(folder) and time.time() < deadline + RC_STOP_GRACE:
        time.sleep(0.5)                   # its launcher records the exit, then lets go
    return True


# One-shot markers: each is set when its condition is first seen, so the log
# line or alert goes out once, and dropped when the condition clears.
RC_FLAGS = ("rc_auth_alerted", "rc_auth_block", "rc_net_down", "rc_trip_alerted")


def rc_login_dead(cfg, folder):
    """Did this folder's launcher last stop on a login failure that nobody has
    fixed? cfg: the folder's watchdog state (rc_state)."""
    ended, reason = rc_last_exit(folder)
    if reason != "auth" or ended <= cfg.get("rc_reset", 0):
        return False
    return cred_mtime() <= ended          # login unchanged since it failed


def rc_watchdog(state):
    """Restart each folder's Remote Control server if it has gone missing.
    Opt-in via `rc on`. True if a launcher was opened."""
    cfg = state.setdefault(CONFIG, {})
    if not cfg.get("rc_watch"):
        return False
    opened = False
    for folder in rc_folders():
        opened = rc_watch_folder(rc_state(cfg, folder), folder) or opened
    return opened


def rc_watch_folder(cfg, folder):
    """The watchdog for one folder; cfg is its state (rc_state). True if its
    launcher was opened."""
    if rc_running(folder):
        if cfg.get("rc_tries"):
            cfg["rc_tries"] = 0           # back up, reset the circuit breaker
        for flag in RC_FLAGS:
            cfg.pop(flag, None)
        if not rc_restart_stale(cfg, folder) and not rc_restart_hung(cfg, folder):
            return False
        # The old or hung server is gone: start a new one straight away.
    if launcher_busy(folder):
        return False                      # one is already starting it; not a try
    ended, reason = rc_last_exit(folder)
    if rc_login_dead(cfg, folder):
        # Retrying cannot fix a dead login and no try is spent on it. It lifts
        # by itself once the stored login changes (`claude /login`, or another
        # session renewing it), or on `rc on`.
        if not cfg.get("rc_auth_alerted"):
            cfg["rc_auth_alerted"] = notify(
                "Remote Control%s is down: the Claude login expired. Run "
                "`claude /login` on this machine; it restarts by itself afterwards."
                % folder["where"]) != "retry"
            cfg["rc_auth_block"] = True
        return False
    if cfg.pop("rc_auth_block", None):
        # The login changed since it failed. Start from a clean breaker, or the
        # promised restart would be refused when the failure was the last try.
        log("rc watchdog: the stored login changed, trying Remote Control%s again"
            % folder["where"])
        cfg["rc_tries"] = 0
        for flag in RC_FLAGS:
            cfg.pop(flag, None)
    if (reason == "network" and ended > cfg.get("rc_last_launch", 0)
            and cfg.get("rc_refunded") != ended and cfg.get("rc_tries")):
        # The launcher asks for three good probes, the check below for one. A
        # launch it abandoned at its own network gate was not a failed start.
        cfg["rc_tries"] -= 1
        cfg["rc_refunded"] = ended
        cfg.pop("rc_trip_alerted", None)
    now = time.time()
    if 0 <= now - cfg.get("rc_last_launch", 0) < RC_MIN_INTERVAL:
        # Already tried recently, give it time. A launch time ahead of the clock
        # (the clock stepped back) does not hold relaunches off until it catches up.
        return False
    if cfg.get("rc_tries", 0) >= RC_MAX_TRIES:
        if not cfg.get("rc_trip_alerted"):
            cfg["rc_trip_alerted"] = notify(
                "Remote Control%s is down and %d restarts failed. Standing down; "
                "check the Terminal window, then `limit-resume.py rc on`."
                % (folder["where"], RC_MAX_TRIES)) != "retry"
        return False                      # standing down; `rc on` resets it
    if not os.path.exists(folder["launcher"]):
        log("rc watchdog: launcher missing at %s" % folder["launcher"])
        cfg["rc_tries"] = RC_MAX_TRIES
        return False
    if not net_ok():
        # An outage is not a failed start: wait it out without spending a try,
        # however long it lasts. Starting now would also risk the login token.
        if not cfg.get("rc_net_down"):
            cfg["rc_net_down"] = True
            log("rc watchdog: Remote Control%s is not running and the API is "
                "unreachable, waiting for the network" % folder["where"])
        return False
    if cfg.pop("rc_net_down", None):
        log("rc watchdog: network is back")
    cfg["rc_last_launch"] = now
    cfg["rc_tries"] = cfg.get("rc_tries", 0) + 1
    log("rc watchdog: Remote Control%s is not running, starting it (try %d/%d)"
        % (folder["where"], cfg["rc_tries"], RC_MAX_TRIES))
    rc_reap_orphans(folder)               # old sessions die before a new server
    try:
        # `open` hands it to Terminal, so it comes back as a visible window
        # rather than an invisible background process.
        subprocess.run(["open", folder["launcher"]], capture_output=True, timeout=30)
    except Exception as exc:
        log("  failed to launch: %s" % exc)
    return True


def text_of(content):
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content if isinstance(content, str) else ""


def session_perm(path, prompt):
    """The permission mode of the user's last turn in this transcript, or PERM.

    Our own resume prompts are skipped: they carry the mode the resume ran in,
    and counting them would let one fallback resume pin every later one."""
    if PERM_FORCED:
        return PERM_FORCED
    mode = None
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"permissionMode"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "user":
                    continue
                if text_of((rec.get("message") or {}).get("content")) == prompt:
                    continue
                mode = rec.get("permissionMode") or mode
    except IOError:
        return PERM
    mode = PERM_ALIASES.get(mode, mode)
    return mode if mode in PERM_MODES else PERM


def session_cwd(path):
    """The directory the session was started in, or HOME.

    launchd runs us from /, and `claude --resume` from / finds the transcript
    but loads none of the project's CLAUDE.md, memory or settings. The first
    record carrying a cwd is the launch directory; later ones follow `cd`."""
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd and cwd != "/" and os.path.isdir(cwd):
                    return cwd
    except IOError:
        pass
    return os.path.expanduser("~")


def local_owner(sid, rows):
    """Pid of another process that has session `sid` open, by its command line
    (`claude --resume <sid>`, `-r <sid>`, `--session-id <sid>`), or None.
    A Remote Control child does not count: its chat is relayed to instead."""
    flag = re.compile(r"(^|\s)(--resume|-r|--session-id)[ =]%s(\s|$)" % re.escape(sid))
    for pid, _ppid, cmd in rows:
        if pid != os.getpid() and RC_CHILD_MARK not in cmd and flag.search(cmd):
            return pid
    return None


# A Claude Code process as ps shows it: `claude ...` by any path, or the
# versioned binary of a native install (.../claude/versions/2.1.308 ...).
CLAUDE_PROC = re.compile(r"^(/[^ ]*/)?claude( |$)|^/[^ ]*/claude/versions/[^ /]+( |$)")


def cwd_owner(cwd, rows, mine):
    """(pid, None) of a live claude process working in folder `cwd`, (None, None)
    if there is none, or (None, why) when lsof cannot tell.

    A chat open in a terminal or the desktop app since before it stopped has
    no --resume on its command line when it was started fresh (local_owner
    misses it), but it works in the chat's folder. Not counted: Remote Control
    servers and their sessions (a Remote Control chat is relayed to), and the
    tool's own processes, everything below a pid in `mine` (this sweep and
    its resume, the pty helpers of jobs and continued chats)."""
    servers = {pid for pid, _cmd in rc_servers(rows)}
    own = set(mine)
    for pid in mine:
        own.update(descendants(pid, rows))
    claudes = [pid for pid, ppid, cmd in rows
               if pid not in own and pid not in servers and CLAUDE_PROC.match(cmd)
               and not (RC_CHILD_MARK in cmd and (ppid in servers or ppid == 1))]
    if not claudes:
        return None, None
    where = cwds(claudes)
    if where is None:
        return None, "lsof failed, cannot tell which folder each claude process works in"
    for pid in claudes:
        if same_dir(where.get(pid), cwd):
            return pid, None
    return None, None


def headless_owner(sid, path, rows):
    """None when session `sid` may be resumed headless, else (log line, alert).

    Never behind a window that still has the session open: that window never
    learns of the turn, and two writers would share one transcript. When ps or
    lsof cannot tell, it counts as open (fail safe)."""
    title = chat_title(path)[:60]
    if rows is None:
        return ("ps failed, cannot tell whether it is open elsewhere",
                'Limit reset, but chat "%s" is open in another claude process (ps failed) - '
                'type "continue" there.' % title)
    owner = local_owner(sid, rows)
    if owner:
        return ("open in another claude process (pid %d)" % owner,
                'Limit reset, but chat "%s" is open in another claude process (pid %d) - '
                'type "continue" there.' % (title, owner))
    cwd = session_cwd(path)
    mine = [os.getpid()] + [pid for pid, kind in agent_procs(rows) if kind == "jobpty"]
    owner, unknown = cwd_owner(cwd, rows, mine)
    if owner or unknown:
        return ("%s, may be open there" % (
                    "a claude process (pid %d) works in its folder %s" % (owner, cwd) if owner
                    else unknown),
                'Limit reset, but chat "%s" may still be open: %s - type "continue" in it.'
                % (title, "a claude process (pid %d) works in %s" % (owner, cwd) if owner
                   else "lsof failed, so the tool cannot tell"))
    return None


def resume_files(sid):
    """(stdout, stderr, exit status) files of a detached headless resume."""
    base = os.path.join(JOB_LOGS, sid + ".resume")
    return base + ".out", base + ".err", base + ".exit"


RESUME_DONE_RE = re.compile(r"^[ \t*`]*DONE[ \t*`.]*$")


def said_done(out):
    """Did the resume end on a line that is exactly DONE (markdown aside)?"""
    lines = out.strip().splitlines()
    return bool(lines) and bool(RESUME_DONE_RE.match(lines[-1]))


def run_resume(sid, entry, path):
    """Start one headless resume, detached in a process group of its own, and
    return the active_resume record for it, or None. The sweep does not wait
    for it: a later sweep collects the result (resume_tick). The block is
    already recorded as attempted, so every failure here loses the attempt
    rather than repeating it."""
    claude = shutil.which("claude", path=os.environ.get("PATH", "") + ":" + CLAUDE_PATH_HINT)
    if not claude:
        log("  FAILED: `claude` not on PATH (check the LaunchAgent PATH key)")
        return None
    perm = session_perm(path, entry["prompt"])
    cwd = session_cwd(path)
    out_path, err_path, exit_path = resume_files(sid)
    log("  permission mode %s, cwd %s, output in %s" % (perm, cwd, out_path))
    try:
        os.makedirs(JOB_LOGS, exist_ok=True)
        if os.path.exists(exit_path):
            os.unlink(exit_path)
        with open(out_path, "w") as out, open(err_path, "w") as err:
            # The shell stays to write claude's exit status, which no later
            # sweep could read from a process it did not start.
            proc = subprocess.Popen(
                ["/bin/sh", "-c", '"$@"; echo $? > "$LIMIT_RESUME_EXIT"', "sh",
                 claude, "--resume", sid, "-p", entry["prompt"], "--permission-mode", perm],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=cwd,
                start_new_session=True, env=dict(os.environ, LIMIT_RESUME_EXIT=exit_path))
    except OSError as exc:
        log("  FAILED: could not launch resume: %s" % exc)
        return None
    _SPAWNED[proc.pid] = proc
    return {"how": "headless", "sid": sid, "pid": proc.pid, "at": time.time()}


def stop_group(pid, alive):
    """SIGTERM process group `pid`, then SIGKILL whatever of it is left after
    RC_STOP_GRACE s, or as soon as alive() says its leader is gone."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + RC_STOP_GRACE
    while alive() and time.time() < deadline:
        time.sleep(0.5)
    try:
        os.killpg(pid, signal.SIGKILL)    # its tool shells and builds too
    except OSError:
        pass


def resume_tick(state, now):
    """Check on the headless resume an earlier sweep started: still running,
    run past RESUME_TIMEOUT (its whole process group is stopped), or ended (its
    result is logged; a reply of DONE retires the session)."""
    cfg = state.setdefault(CONFIG, {})
    act = cfg.get("active_resume")
    if not act or act.get("how") != "headless":
        return
    sid = act.get("sid", "")
    if job_alive(act):
        if now - act.get("at", now) < RESUME_TIMEOUT:
            return
        log("%s: FAILED: resume ran past %ds, stopping it and everything it started"
            % (sid, RESUME_TIMEOUT))
        stop_group(act["pid"], lambda: job_alive(act))
        cfg.pop("active_resume", None)
        return
    out_path, err_path, exit_path = resume_files(sid)
    out = read_from(out_path, 0).strip()
    code = read_from(exit_path, 0).strip() or "none (stopped or killed)"
    log("%s: resume ended, exit=%s out=%s" % (sid, code, out[-300:].replace("\n", " ")))
    err = read_from(err_path, 0).strip()
    if code != "0" and err:
        log("  stderr=%s" % err[-300:].replace("\n", " "))
    entry = state.get(sid)
    if code == "0" and said_done(out) and entry is not None:
        log("  session reported DONE, retiring %s" % sid)
        entry["done"], entry["why"] = True, "DONE"
    cfg.pop("active_resume", None)


def run_group(argv, timeout, cwd):
    """subprocess.run with captured text output, in a process group of its own:
    on timeout the whole group is killed, not just its leader, and
    TimeoutExpired is raised."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def rc_child_pid(cse):
    """Pid of the live Remote Control child serving cse_..., or None."""
    for pid, _ppid, cmd in ps_rows() or []:
        if RC_CHILD_MARK in cmd and re.search(r"--session-id %s( |$)" % re.escape(cse), cmd):
            return pid
    return None


def send_continue(sid, entry, path, cse):
    """Deliver the resume prompt into a Remote Control chat. True if delivered.

    A throwaway headless session calls SendMessage with the chat's own socket
    as `to`. The message lands in the live child's inbox and wakes it even
    when idle (tested 2026-10-26, PONG2 busy / PONG3 idle), so the chat runs
    the turn itself and the phone shows it. The sender runs in the chat's
    permission mode, because a receiver holds messages from a different mode
    for approval.
    """
    pid = rc_child_pid(cse)
    sock = os.path.join(CC_SOCKS, "%d.sock" % pid) if pid else None
    if not sock or not os.path.exists(sock):
        log("  %s: Remote Control session %s has no live child/inbox (pid %s)"
            % (sid, cse, pid))
        return False
    perm = session_perm(path, entry["prompt"])
    message = ("limit-resume: the usage limit that stopped this chat has reset. "
               + entry["prompt"])
    relay = ("Call the SendMessage tool exactly once, with `to` set to "
             '"uds:%s" and `message` set to the text between <<< and >>>, '
             "verbatim. Call no other tool. Then reply with exactly SENT if the "
             'tool result contains "success":true, otherwise with exactly FAILED.'
             "\n<<<\n%s\n>>>" % (sock, message))
    log("  %s is Remote Control session %s (pid %d): sending via its inbox, mode %s"
        % (sid, cse, pid, perm))
    try:
        # Run from TOOLS, which auto mode never resumes, so the relay's own
        # transcript is not taken for a chat and HOME's project memory is not
        # loaded. The tools it has no use for are denied outright.
        os.makedirs(TOOLS, exist_ok=True)
        proc = run_group(
            ["claude", "-p", relay, "--model", SEND_MODEL, "--permission-mode", perm,
             "--allowedTools", "SendMessage", "--disallowedTools", RELAY_DENY,
             "--output-format", "text"], SEND_TIMEOUT, TOOLS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("  FAILED: relay did not run: %s" % exc)
        return False
    out = (proc.stdout or "").strip()
    log("  relay exit=%d out=%s" % (proc.returncode, out[-200:].replace("\n", " ")))
    return proc.returncode == 0 and out.endswith("SENT")


CHAT_HANDOFF = (
    "This chat continues session %(old)s, which stopped at the usage limit with "
    "%(k)dk tokens of context, too large to resume, so it was restarted here. "
    "Its transcript is %(path)s; read the end of it for where it stopped. "
    "Continue the work it was doing; do not redo finished work. Nobody is at the "
    "keyboard, so do not ask questions and do not wait for confirmation.\n\n"
    "Its first request was:\n\n%(first)s"
)


def start_new_chat(sid, entry, path, tokens):
    """Continue an over-large chat in a new Remote Control session. New sid or None."""
    claude = shutil.which("claude", path=os.environ.get("PATH", "") + ":" + CLAUDE_PATH_HINT)
    if not claude:
        log("  FAILED: `claude` not on PATH (check the LaunchAgent PATH key)")
        return None
    _, first = first_prompt(path)
    new = str(uuidlib.uuid4())
    title = (first.strip().splitlines() or [sid[:8]])[0]
    prompt = CHAT_HANDOFF % {"old": sid, "k": tokens // 1000, "path": path,
                             "first": first.strip()[:4000] or "(not found)"}
    argv = [claude, prompt, "--session-id", new, "--remote-control",
            ("continued: %s" % title)[:80],
            "--permission-mode", session_perm(path, entry["prompt"])]
    cwd = session_cwd(path)
    log("  %s has %dk tokens of context (> %dk): continuing in new session %s, cwd %s"
        % (sid, tokens // 1000, CHAT_NEW_AT // 1000, new, cwd))
    if not job_spawn(argv, os.path.join(JOB_LOGS, new + ".pty.log"), cwd, job=False):
        return None
    notify('Chat "%s" was too large to resume (%dk tokens); continued in a new '
           'Remote Control session.' % (title[:60], tokens // 1000))
    return new


# ---------------------------------------------------------------------------
# Background jobs
#
# A folder of job notes, one Markdown note per job, run one at a time as
# Remote Control sessions on quota that would otherwise go unused. The note's frontmatter
# is the job's status, so the queue is managed from Obsidian (or any editor):
#
#   status: queued | hold | running | paused | limit | blocked | done | failed
#   order:  lower runs first; notes without it run last, by name
#   cwd:    optional working directory (default: `jobs on --cwd`, else HOME)
#
#   priority: top runs before the rest of the backlog
#
# The runner adds session_id, started, finished, resumes, paused_why,
# paused_until and runner (this machine; other machines leave the note
# alone), and appends a `## Run ...` section with the final reply. Notes whose
# names start with `_` are ignored, as are notes in subfolders. A job runs
# detached and each sweep checks on it; the sweep only waits while it stops
# one (up to JOB_STOP_GRACE) or probes usage (up to PROBE_TIMEOUT). Waiting
# costs no tokens; reading usage costs at most one Haiku turn per
# JOB_PROBE_EVERY (or per sweep while a job runs), and none at all when a
# status line keeps the usage cache fresh.

JOBS_KEY = "__jobs__"
JOB_LOGS = os.path.join(TOOLS, "jobs")            # <session_id>.pty.log terminal output
JOB_SETTINGS = os.path.join(TOOLS, "job-settings.json")   # guard hook + status line feed
JOB_BIN = os.path.join(TOOLS, "jobbin")           # first on a job session's PATH: the git wrapper
USAGE = os.path.join(TOOLS, "usage.json")         # last known usage, per window
CLAUDE_JSON = os.path.join(HOME, ".claude.json")  # holds each folder's trust decision
JOB_MODEL = "opus"
JOB_PERM = "bypassPermissions"
JOB_FIVE_HOUR_MAX = 60      # % of the 5-hour window
JOB_NEW_AT = 400000         # context tokens above which a paused job restarts in a new session: no start at or above; a running job is paused
# Weekly caps follow the account's own weekly reset (accounts reset on
# different days), read from the usage figures or set with `jobs on --reset`.
WEEKLY_CAP_DEFAULT = 80     # % of the weekly window a job may run up to
WEEKLY_CAP_LAST_DAY = 90    # from local midnight the day before the reset (e.g. reset
                            # Mon 03:00 -> all of Sunday and Monday until 03:00)
# The weekly gate: jobs only start in the run-up to the weekly reset, earlier
# the more quota is left under today's cap. 72 h before with >= 50 points left,
# 30 h before with <= 5 left, linear in between.
GATE_EARLY_H, GATE_LATE_H = 72, 30
GATE_RICH, GATE_POOR = 50, 5
USAGE_FRESH_RUNNING = 240   # s: a running job wants a reading from this sweep (probes if older)
USAGE_MAX_AGE = 600         # s: an older figure counts as unknown, and unknown counts as
                            # over a cap: no job starts, and a running one is paused
USAGE_FRESH_IDLE = USAGE_MAX_AGE   # s: with no job running, a reading this old is refreshed
JOB_PROBE_EVERY = 900       # s between probes while no job runs
JOB_PROBE_MAX_SKIP = 6 * 3600   # s a reading may be trusted to keep the gate shut; a
                                # reset Claude hands out can change usage and the reset day
WEEK = 7 * 86400
RESET_SLACK = 3600          # s two reset times may differ and still be the same weekly slot
PROBE_TIMEOUT = 180
JOB_MAX_H = 8               # a single run longer than this is stopped and failed
JOB_STOP_GRACE = 30         # s between SIGTERM and SIGKILL when stopping a job session
JOB_START_TIMEOUT = 300     # s for a new session to write its transcript (else a dialog blocks it)
JOB_IDLE = 1200             # s on a reply without JOB DONE -> the job is waiting for you (blocked)
JOB_KEEP_OPEN = 1800        # s a finished job's session stays open on the phone once idle
JOB_BLOCKED_KEEP = 86400    # s a blocked job's session stays open for your answer
JOB_COLS, JOB_ROWS = 200, 50
JOB_PTY_LOG_MAX = 2 * 1024 * 1024
JOB_MAX_RESUMES = MAX_RESUMES
JOB_STATUSES = ("queued", "hold", "running", "paused", "limit", "blocked", "done", "failed")
# Matched on the last non-blank line of the final message only, any case, with
# bullets, quotes, markdown or an emoji around the marker.
JOB_DONE_RE = re.compile(r"^\W*JOB\s+DONE\W*$", re.I)
JOB_FAILED_RE = re.compile(r"^\W*JOB\s+FAILED\b\W*(.*?)[\s*_`]*$", re.I)
JOB_END = (
    "When the job is complete, end your final message with a line containing "
    "exactly JOB DONE. If it cannot be completed, end with a line "
    "JOB FAILED: <one-line reason>."
)
JOB_RULES = (
    "You are running unattended as a background job started by limit-resume.py, "
    "on quota that would otherwise go unused. The session is visible in Remote "
    "Control, but nobody is watching: do not ask questions and do not wait for "
    "confirmation; where a choice is needed, take the safe one and record it in "
    "your output. Do not take destructive actions: no deleting files or data, no "
    "dropping tables, no schema changes, no force pushes, no history rewrites. "
    "Use git only to read (status, log, diff, show and the like): a hook blocks "
    "`git commit`, `git push`, every other git command that writes, and edits to "
    "~/.claude/tools and to Claude settings. Leave changes uncommitted and list "
    "them. Do not edit the job note itself (%s); the runner "
    "records the job's status there. If you cannot continue without an answer "
    "from the user, write the question and stop without JOB DONE. End with a "
    "short summary of what you did and where the output is. " + JOB_END
    + "\n\nThe job:\n\n"
)
JOB_RESUME_PROMPT = (
    "Continue this background job where it stopped. The runner paused it at a "
    "usage cap or limit; the user did not stop it. Do not redo finished work. "
    "The same rules apply: unattended, no questions, no destructive actions, no "
    "git commit or push. End with a short summary of what you did and where the "
    "output is. " + JOB_END
)
JOB_FRESH_NOTE = (
    "\n\nThis job already ran in an earlier session (%s) that the runner paused; "
    "its context grew too large to resume, so this is a new session. Before you "
    "start, check the outputs it already wrote and the Run sections of the job "
    "note, and do not redo finished work."
)
_SPAWNED = {}               # pid -> Popen, for jobs this process started


def project_dir_name(path):
    """The ~/.claude/projects folder name Claude Code uses for cwd `path`."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def job_sids(state):
    js = state.get(JOBS_KEY) or {}
    out = list(js.get("sids", []))
    run = js.get("running")
    if run and run.get("sid"):
        out.append(run["sid"])
    return out


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M")


def fmt_when(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime("%a %H:%M")


# -- job notes ---------------------------------------------------------------

def yaml_value(raw):
    raw = raw.strip()
    if raw[:1] not in ("'", '"'):
        raw = re.split(r"\s#", raw, maxsplit=1)[0].rstrip()   # `status: queued # parked till Monday`
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw.startswith("'") and raw.endswith("'") and len(raw) > 1:
        return raw[1:-1].replace("''", "'")
    return raw


def yaml_scalar(value):
    text = str(value)
    if (not text or text != text.strip() or ": " in text or " #" in text
            or text[0] in "-?:,[]{}#&*!|>'\"%@`"):
        return json.dumps(text, ensure_ascii=False)   # a JSON string is valid YAML
    return text


def split_note(text):
    """(frontmatter lines, body) — no frontmatter gives ([], text). A byte-order
    mark and Windows line endings are dropped first; the frontmatter ends at the
    first line that is exactly `---`."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = re.search(r"\n---[ \t]*(\n|$)", text[3:])
        if end:
            return text[4:3 + end.start()].split("\n"), text[3 + end.end():]
    return [], text


def read_note(path):
    """(meta dict, body). Only flat `key: value` lines are read; others are kept as-is."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        head, body = split_note(fh.read())
    meta = {}
    for line in head:
        m = re.match(r"^([A-Za-z_][\w-]*):(.*)$", line)
        if m:
            meta[m.group(1)] = yaml_value(m.group(2))
    return meta, body


def update_note(path, changes, append=None):
    """Set (or, with None, remove) frontmatter keys and optionally append text.

    Re-reads the note first, so an edit made since the sweep read it is kept,
    and writes through a temp file, so a crash never leaves half a note."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        log("jobs: cannot update %s: %s" % (path, exc))
        return False
    head, body = split_note(text)
    done = set()
    out = []
    for line in head:
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        key = m.group(1) if m else None
        if key in changes:
            done.add(key)
            if changes[key] is not None:
                out.append("%s: %s" % (key, yaml_scalar(changes[key])))
            continue
        out.append(line)
    for key, value in changes.items():
        if key not in done and value is not None:
            out.append("%s: %s" % (key, yaml_scalar(value)))
    if append:
        body = body.rstrip("\n") + "\n\n" + append.strip("\n") + "\n"
    new = "---\n%s\n---\n%s" % ("\n".join(out), body)
    tmp = path + ".lr-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except OSError as exc:
        log("jobs: cannot write %s: %s" % (path, exc))
        return False
    return True


def note_title(path, body):
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return os.path.basename(path)[:-3]


def job_notes(folder):
    """Every job note in `folder`, as dicts, in run order."""
    out = []
    for path in glob.glob(os.path.join(folder, "*.md")):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        try:
            meta, body = read_note(path)
        except OSError:
            continue
        out.append({"path": path, "name": name, "meta": meta,
                    "title": note_title(path, body), "body": body,
                    "status": (meta.get("status") or "").strip().lower()})

    def order(job):
        try:
            value = float(job["meta"].get("order"))
        except (TypeError, ValueError):
            value = float("nan")
        if not math.isfinite(value):
            return (1, 0.0, job["name"])   # no usable order: last, by name
        return (0, value, job["name"])
    return sorted(out, key=order)


RUN_HEADER = re.compile(r"^## Run \d{4}-\d\d-\d\dT\d\d:\d\d", re.M)


def job_prompt_body(body):
    """The note body a fresh start sends: without the `## Run …` sections the
    runner appended (always at the end), which are its own past output."""
    found = RUN_HEADER.search(body)
    return (body[:found.start()] if found else body).strip()


def job_top(job):
    """`priority: top` notes run before the rest of the backlog."""
    return str(job["meta"].get("priority") or "").strip().lower() == "top"


def pick_job(jobs):
    """The next job to run: `priority: top` notes before the rest; within each,
    a paused one first, then the first queued one."""
    for tier in ([j for j in jobs if job_top(j)], [j for j in jobs if not job_top(j)]):
        for wanted in (("paused", "limit"), ("queued",)):
            for job in tier:
                if job["status"] in wanted:
                    return job
    return None


# -- usage -------------------------------------------------------------------

def to_epoch(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def to_pct(value, fraction=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if fraction and value <= 1.5:
        return float(value) * 100.0
    return float(value)


def window_of(kind):
    if kind == "five_hour":
        return "five_hour"
    if isinstance(kind, str) and kind.startswith("seven_day"):
        return "seven_day"       # seven_day, seven_day_opus, ...: the worst one binds
    return None


def merge_reading(reading, window, pct, resets_at):
    """Fold one figure into `reading`. Of several variants of a window
    (seven_day, seven_day_opus, ...) the highest binds, and its reset time
    comes with it, so a variant that resets at another time cannot move it."""
    if pct is None and resets_at is None:
        return
    cur = reading.setdefault(window, {})
    if pct is not None and (cur.get("pct") is None or pct > cur["pct"]):
        cur["pct"] = pct
        if resets_at is not None:
            cur["resets_at"] = resets_at
    elif resets_at is not None and cur.get("resets_at") is None:
        cur["resets_at"] = resets_at


def reading_from_statusline(data):
    """Usage from the JSON a status line command gets on stdin."""
    reading = {}
    limits = (data or {}).get("rate_limits") or {}
    if isinstance(limits, dict):
        for kind, win in limits.items():
            if not isinstance(win, dict) or not window_of(kind):
                continue
            pct = to_pct(win.get("used_percentage"))
            if pct is None:
                pct = to_pct(win.get("utilization"), fraction=True)
            merge_reading(reading, window_of(kind), pct,
                          to_epoch(win.get("resets_at") or win.get("resetsAt")))
    return reading


def reading_from_stream(lines):
    """Usage from `claude --output-format stream-json` output (rate_limit_event)."""
    reading = {}
    for line in lines:
        if '"rate_limit' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "rate_limit_event":
            continue
        info = rec.get("rate_limit_info") or {}
        # Since 2026-11 the figures are per window under unifiedWindows; the
        # top level keeps only status/resetsAt of the binding window.
        unified = info.get("unifiedWindows") or {}
        if isinstance(unified, dict):
            for kind, uwin in unified.items():
                if isinstance(uwin, dict) and window_of(kind):
                    merge_reading(reading, window_of(kind),
                                  to_pct(uwin.get("utilization"), fraction=True),
                                  to_epoch(uwin.get("resetsAt")))
        win = window_of(info.get("rateLimitType"))
        if not win:
            continue
        pct = to_pct(info.get("utilization"), fraction=True)
        if info.get("status") == "rejected":
            pct = 100.0
        merge_reading(reading, win, pct, to_epoch(info.get("resetsAt")))
    return reading


def load_usage():
    try:
        with open(USAGE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (IOError, ValueError):
        return {}


def merged_window(old, new, now):
    """The cached figure for one window after reading `new` (pct, resets_at).

    Usage only grows within a window, so within the same window (reset times
    within RESET_SLACK) the higher figure is kept. A figure from an older
    window than the cached one, e.g. an idle session re-rendering its last
    reply, is ignored. A later window, or a cached one that has reset,
    replaces it. Either way the window was seen now."""
    pct, resets = new["pct"], new.get("resets_at")
    if old and old.get("pct") is not None:
        old_resets = old.get("resets_at")
        ended = bool(old_resets) and now >= old_resets
        if resets and old_resets and resets < old_resets - RESET_SLACK and not ended:
            return old                     # from an earlier window: stale
        later = bool(resets and old_resets and resets > old_resets + RESET_SLACK)
        if not later and not ended:
            pct = max(pct, old["pct"])
            resets = resets or old_resets
    return {"pct": round(pct, 1), "resets_at": resets, "at": now}


def save_usage(reading, source, now=None):
    """Merge a reading into the cache, each window stamped with when it was read.

    Every session's status line writes here at every render, so the merge is
    done under an flock and written through a temp file of this process's own."""
    if not reading:
        return
    now = now or time.time()
    try:
        os.makedirs(os.path.dirname(USAGE), exist_ok=True)
        with open(USAGE + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            usage = load_usage()
            for win, val in reading.items():
                if val.get("pct") is None:
                    if (win in usage and (val.get("resets_at") or 0)
                            > (usage[win].get("resets_at") or 0)):
                        usage[win]["resets_at"] = val["resets_at"]
                    continue
                cur = merged_window(usage.get(win), val, now)
                if cur is not usage.get(win):
                    cur["source"] = source
                    usage[win] = cur
            write_json(USAGE, usage)
    except OSError:
        pass


def window_pct(usage, win, now):
    """Current % of a window, 0 once its reset has passed, None if unknown."""
    val = (usage or {}).get(win) or {}
    if val.get("pct") is None:
        return None
    resets = val.get("resets_at")
    if resets and now >= resets:
        return 0.0
    return float(val["pct"])


def usage_age(usage, now):
    ages = [now - (usage.get(w) or {}).get("at", 0) for w in ("five_hour", "seven_day")]
    return max(ages) if usage else float("inf")


def usage_unknown(usage, now):
    """Why the reading cannot be trusted to run a job on, or None.

    Both windows are needed, each read in the last USAGE_MAX_AGE s and from a
    window that has not reset since. A probe that reports only one window, a
    probe that keeps failing, or a figure from an earlier window all leave
    usage unknown, and unknown counts as over a cap."""
    for win, label in (("five_hour", "5-hour"), ("seven_day", "weekly")):
        val = (usage or {}).get(win) or {}
        if val.get("pct") is None:
            return "usage unknown: no %s figure" % label
        age = now - val.get("at", 0)
        if age > USAGE_MAX_AGE:
            return "usage unknown: the %s figure is %d min old" % (label, age // 60)
        if val.get("resets_at") and now >= val["resets_at"]:
            return "usage unknown: the %s window has reset since it was read" % label
    return None


def probe_usage():
    """(reading, error): one cheap Haiku turn, read for its rate_limit_event records."""
    try:
        os.makedirs(TOOLS, exist_ok=True)
        proc = subprocess.run(
            ["claude", "-p", "Reply with exactly OK.", "--model", SEND_MODEL,
             "--output-format", "stream-json", "--verbose"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, cwd=TOOLS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "probe did not run: %s" % exc
    lines = (proc.stdout or "").splitlines()
    reading = reading_from_stream(lines)
    if reading and any(v.get("pct") is not None for v in reading.values()):
        return reading, None
    err = "no usage figures in the probe output"
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "result" and rec.get("is_error"):
            err = "probe failed: %s" % str(rec.get("result"))[:160]
            break
    return reading or None, err


def ensure_usage(js, running, now):
    """A usage reading fresh enough for the decision at hand, probing if needed."""
    usage = load_usage()
    track_reset(js, usage, now)           # status line readings land here too
    fresh = USAGE_FRESH_RUNNING if running else USAGE_FRESH_IDLE
    if usage_age(usage, now) <= fresh:
        return usage
    if not running:
        if closed_anyway(usage, now, js):
            return usage                  # no reading can open the gate yet: save the probe
        if now - js.get("last_probe", 0) < JOB_PROBE_EVERY:
            return usage
    js["last_probe"] = now
    reading, err = probe_usage()
    if reading:
        save_usage(reading, "probe", now)
    if err:
        if js.get("probe_error") != err:
            log("jobs: usage probe: %s" % err)
        js["probe_error"] = err
        if not js.get("probe_alerted"):
            js["probe_alerted"] = True     # once, until a probe works again
            notify("Usage could not be read (%s). No job starts, and a running one is "
                   "paused, until it can; `limit-resume.py jobs probe` tries again." % err)
    else:
        js.pop("probe_error", None)
        js.pop("probe_alerted", None)
    usage = load_usage()
    track_reset(js, usage, now)
    return usage


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def next_weekly(spec, now):
    """Next occurrence of a weekly time like "Mon 03:00" or "thursday 14:00"."""
    m = re.match(r"^\s*([a-z]{3})[a-z]*\s+(\d{1,2}):(\d{2})\s*$", str(spec or ""), re.I)
    if not m or m.group(1).lower() not in WEEKDAYS:
        return None
    day, hour, minute = WEEKDAYS.index(m.group(1).lower()), int(m.group(2)), int(m.group(3))
    if hour > 23 or minute > 59:
        return None
    base = datetime.datetime.fromtimestamp(now)
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + datetime.timedelta(
        days=(day - base.weekday()) % 7)
    if when.timestamp() <= now:
        when += datetime.timedelta(days=7)
    return when.timestamp()


def weekly_reset(usage, js, now):
    """This account's next weekly reset, newest knowledge first:

    1. the usage figures (seven_day.resets_at);
    2. the last reset seen in them, rolled forward a week at a time;
    3. `jobs on --reset "Thu 14:00"`, only until a real one has been seen.

    The reset is not fixed: a reset Claude hands out moves it to another day,
    which track_reset notices."""
    resets = ((usage or {}).get("seven_day") or {}).get("resets_at")
    if resets and resets > now:
        return resets
    seen = (js or {}).get("reset_seen")
    if seen:
        while seen <= now:
            seen += WEEK
        return seen
    return next_weekly((js or {}).get("weekly_reset"), now)


def same_schedule(a, b):
    """Do two reset times fall on the same weekly slot (weeks apart, give or take)?"""
    off = abs(a - b) % WEEK
    return min(off, WEEK - off) <= RESET_SLACK


def track_reset(js, usage, now):
    """Remember the account's weekly reset; say so when it moves to a new slot."""
    resets = ((usage or {}).get("seven_day") or {}).get("resets_at")
    if not resets or resets <= now:
        return
    prev = js.get("reset_seen")
    js["reset_seen"] = resets
    manual = js.get("weekly_reset")
    if manual:
        spec = next_weekly(manual, now)
        if not spec or not same_schedule(spec, resets):
            log('jobs: --reset "%s" no longer matches the account (resets %s); following '
                "the account" % (manual, fmt_when(resets)))
        js.pop("weekly_reset", None)      # a real reading supersedes the hand-set one
    if prev and not same_schedule(prev, resets):
        js["reset_moved"] = [prev, resets, now]
        msg = ("Weekly reset moved: was %s, now %s. Caps and the job gate follow the "
               "new day." % (fmt_when(prev), datetime.datetime.fromtimestamp(resets)
                             .strftime("%a %d %b %H:%M")))
        log("jobs: " + msg)
        notify(msg)


def weekly_cap(now, resets):
    """Today's weekly cap: WEEKLY_CAP_LAST_DAY from local midnight the day before
    the account's reset until the reset, WEEKLY_CAP_DEFAULT otherwise."""
    if not resets:
        return WEEKLY_CAP_DEFAULT
    reset_day = datetime.datetime.fromtimestamp(resets).replace(
        hour=0, minute=0, second=0, microsecond=0)
    last_day = (reset_day - datetime.timedelta(days=1)).timestamp()
    return WEEKLY_CAP_LAST_DAY if last_day <= now < resets else WEEKLY_CAP_DEFAULT


def gate_hours(left):
    """How long before the weekly reset the gate opens, given points left under the cap."""
    left = max(GATE_POOR, min(GATE_RICH, left))
    return GATE_LATE_H + (GATE_EARLY_H - GATE_LATE_H) * (left - GATE_POOR) / float(
        GATE_RICH - GATE_POOR)


def cap_hit(usage, now, js=None):
    """Why a job must not run right now because of a cap, or None. A figure at
    or over its cap is a hit; so is unknown usage (usage_unknown)."""
    five = window_pct(usage, "five_hour", now)
    if five is not None and five >= JOB_FIVE_HOUR_MAX:
        resets = (usage.get("five_hour") or {}).get("resets_at")
        return "5-hour %d%% >= %d%%%s" % (five, JOB_FIVE_HOUR_MAX,
                                           " until %s" % fmt_when(resets) if resets else "")
    week = window_pct(usage, "seven_day", now)
    resets = weekly_reset(usage, js, now)
    cap = weekly_cap(now, resets)
    if week is not None and week >= cap:
        return "weekly %d%% >= today's cap %d%%%s" % (week, cap,
                                                      " until %s" % fmt_when(resets) if resets else "")
    return usage_unknown(usage, now)


def job_gate(usage, now, js=None):
    """(open, why) for starting or resuming a job.

    Both windows must be known and fresh (usage_unknown). A known figure over
    a cap, or the weekly gate, is given as the reason in preference to
    "unknown": either keeps the gate shut whatever a fresh reading says."""
    if window_pct(usage, "seven_day", now) is None:
        return False, "usage unknown"
    hit = cap_hit(usage, now, js)
    if hit and not hit.startswith("usage unknown"):
        return False, hit
    resets = weekly_reset(usage, js, now)
    if not resets:
        return False, "weekly reset time unknown (set one: jobs on --reset \"Mon 03:00\")"
    left = weekly_cap(now, resets) - window_pct(usage, "seven_day", now)
    need = gate_hours(left)
    hours = (resets - now) / 3600.0
    if hours > need:
        return False, "weekly gate opens %s (%.0f h before the %s reset, %d points left)" % (
            fmt_when(resets - need * 3600), need, fmt_when(resets), left)
    if hit:
        return False, hit
    return True, "open: %d points left, weekly reset %s" % (left, fmt_when(resets))


def closed_anyway(usage, now, js=None):
    """Would the gate stay shut whatever a fresh reading said?

    Usage only grows and resets only come on schedule, except when Claude hands
    out a reset: usage drops and the weekly reset moves to a new day. So this
    only saves probes while the reading is younger than JOB_PROBE_MAX_SKIP."""
    if usage_age(usage, now) > JOB_PROBE_MAX_SKIP:
        return False
    five = window_pct(usage, "five_hour", now)
    if five is not None and five >= JOB_FIVE_HOUR_MAX:
        return True
    resets = weekly_reset(usage, js, now)
    return bool(resets) and resets - now > GATE_EARLY_H * 3600


# -- running a job as a Remote Control session ----------------------------------
#
# A job is an interactive `claude --remote-control` session, so it shows up in
# the Remote Control list on the phone and claude.ai like any other chat, and
# can be watched or answered there. Interactive Claude needs a terminal, so it
# runs under `limit-resume.py jobpty`, a small detached helper that gives it a
# pseudo-terminal and logs the screen. The session does not exit when the work
# is done, so the transcript is the signal: the job ends its final message
# with JOB DONE (or JOB FAILED: why). Pausing stops the session; resuming starts
# `claude --resume <id> --remote-control` again.

def job_settings():
    """Settings passed to job sessions only (--settings): the commit/push guard
    on every tool, the status line feeding the usage cache, and no bypass-mode
    confirmation dialog (nobody is at the terminal to accept it).

    The guard fails closed: `|| exit 2` turns any other exit, including a
    python that is gone, into a block (exit 1 would only be a hook error).
    Written atomically and read back; None if that fails."""
    me = "%s %s" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))
    data = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": me + " guard || exit 2"}]}]},
        "statusLine": {"type": "command", "command": me + " statusline"},
        "skipDangerousModePermissionPrompt": True}
    try:
        os.makedirs(os.path.dirname(JOB_SETTINGS), exist_ok=True)
        write_json(JOB_SETTINGS, data)
        if read_json(JOB_SETTINGS) != data:
            raise ValueError("read back differently")
    except (OSError, ValueError) as exc:
        log("jobs: cannot write %s: %s" % (JOB_SETTINGS, exc))
        return None
    if not job_git_wrapper():
        return None                        # no job without its second layer
    return JOB_SETTINGS


GIT_WRAPPER = """#!/bin/sh
# git for background job sessions. Written by limit-resume.py %(version)s each
# time it writes the job settings; an edit here is overwritten.
#
# The second layer behind the PreToolUse guard: a job session has this folder
# first on its PATH, so a `git` run by name - however its command line was
# built, from a script, make or npm - comes here first. The tool decides, with
# the guard's own rules (git-check); when the tool cannot run, git does not
# either. Outside a job session (LIMIT_RESUME_JOB unset) it is plain git.
if [ "${LIMIT_RESUME_JOB:-}" = 1 ]; then
  %(check)s git-check "$@" || exit 1
fi
%(run)s
"""


def real_git(wrapper):
    """The first git on PATH that is not `wrapper` (by any link to it), or None."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        exe = os.path.join(d, "git") if d else None
        if exe and os.path.isfile(exe) and os.access(exe, os.X_OK) and not same_dir(exe, wrapper):
            return exe
    return None


def job_git_wrapper():
    """Write JOB_BIN/git (GIT_WRAPPER) for this tool and version. The real git
    is looked up now and baked in, so the wrapper never runs itself. Path of
    the wrapper, or None if it cannot be written."""
    wrapper = os.path.join(JOB_BIN, "git")
    real = real_git(wrapper)
    text = GIT_WRAPPER % {
        "version": VERSION,
        "check": "%s -I -B %s" % (shlex.quote(sys.executable),
                                  shlex.quote(os.path.abspath(__file__))),
        "run": 'exec %s "$@"' % shlex.quote(real) if real
               else 'echo "git: not found on PATH (limit-resume job wrapper)" >&2\nexit 127'}
    tmp = "%s.tmp.%d" % (wrapper, os.getpid())
    try:
        os.makedirs(JOB_BIN, exist_ok=True)
        with open(tmp, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, wrapper)
        with open(wrapper) as fh:
            if fh.read() != text:
                raise ValueError("read back differently")
    except (OSError, ValueError) as exc:
        log("jobs: cannot write %s: %s" % (wrapper, exc))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return wrapper


def folder_trusted(cwd):
    """True/False from ~/.claude.json (a trusted parent counts), None if unreadable.

    An interactive session in an untrusted folder stops at the trust dialog,
    and nobody is there to answer it."""
    try:
        with open(CLAUDE_JSON) as fh:
            projects = json.load(fh).get("projects") or {}
    except (IOError, ValueError, AttributeError):
        return None
    for start in {os.path.abspath(cwd), os.path.realpath(cwd)}:
        d = start
        while True:
            if (projects.get(d) or {}).get("hasTrustDialogAccepted"):
                return True
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return False


def job_spawn(claude_argv, pty_log, cwd, job=True):
    """Start a job session (or, job=False, a continued chat) under the pty
    helper, detached from the sweep. pid or None."""
    os.makedirs(os.path.dirname(pty_log), exist_ok=True)
    argv = [sys.executable, os.path.abspath(__file__), "jobpty", pty_log, "--"] + claude_argv
    env = dict(os.environ, TERM="xterm-256color")
    if job:
        env["LIMIT_RESUME_JOB"] = "1"
        # JOB_BIN first, so its git wrapper (job_git_wrapper) is the git it finds.
        env["PATH"] = os.pathsep.join([JOB_BIN] + [
            d for d in env.get("PATH", "").split(os.pathsep) if d and not same_dir(d, JOB_BIN)])
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=cwd, start_new_session=True,
                                env=env)
    except OSError as exc:
        log("  FAILED: could not launch job: %s" % exc)
        return None
    _SPAWNED[proc.pid] = proc
    return proc.pid


def cmd_jobpty(argv):
    """jobpty <log> -- <command...>: run command on a pseudo-terminal, log its output.

    Never writes to the terminal, so the session only takes input from Remote
    Control. SIGTERM/SIGHUP are passed on; if this helper dies, the terminal
    closes and the session gets SIGHUP, so it never outlives its helper."""
    import pty
    import struct
    import termios
    if len(argv) < 3 or argv[1] != "--":
        print("usage: limit-resume.py jobpty <log> -- <command...>")
        return 2
    log_path, cmd = argv[0], argv[2:]
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", JOB_ROWS, JOB_COLS, 0, 0))
        except OSError:
            pass
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            os.write(2, ("jobpty: cannot run %s: %s\n" % (cmd[0], exc)).encode())
        os._exit(127)
    try:
        with open(log_path + ".pid", "w") as fh:
            fh.write(str(pid))
    except OSError:
        pass

    def forward(sig, _frame):
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, forward)
    written = 0
    with open(log_path, "ab") as out:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            if written < JOB_PTY_LOG_MAX:
                out.write(chunk)
                out.flush()
                written += len(chunk)
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1


def job_alive(run):
    pid = run.get("pid")
    if not pid or pid < 0:
        return False
    proc = _SPAWNED.get(pid)
    if proc is not None:
        return proc.poll() is None
    if not pid_alive(pid):
        return False
    rows = ps_rows()
    if rows is None:
        return True                      # can't tell: never treat a live job as gone
    return any(p == pid and run.get("sid", "") in cmd for p, _pp, cmd in rows)


def job_child(run, rows):
    """The claude pid under the run's pty helper (its .pid file), if ps shows
    that pid still running this session: a pid file outlives its process, and
    the pid may since belong to another."""
    try:
        with open(run.get("pty", "") + ".pid") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if pid <= 1 or rows is None:
        return None
    return pid if any(p == pid and run.get("sid", "") in cmd for p, _pp, cmd in rows) else None


def descendants(pid, rows):
    """Every pid below `pid` in the process tree, as ps shows it."""
    kids = {}
    for p, pp, _cmd in rows or []:
        kids.setdefault(pp, []).append(p)
    out, todo = [], [pid]
    while todo:
        for kid in kids.get(todo.pop(), []):
            if kid not in out and kid != pid:
                out.append(kid)
                todo.append(kid)
    return out


def job_stop(run):
    """Stop a job session and everything it started.

    SIGTERM the helper (it passes it on to claude), wait up to JOB_STOP_GRACE,
    then SIGKILL what is left: claude's process group (pty.fork made claude a
    group leader, so its tool shells are in it), every process that was below
    claude when the stop began (builds and shells that left the group), and
    the helper. Works when the helper has already died, too."""
    pid = run.get("pid")
    rows = ps_rows()
    child = job_child(run, rows)
    tree = descendants(child, rows) if child else []
    helper = bool(pid and pid > 0 and job_alive(run))
    if not helper and not child and not tree:
        return
    if helper:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    elif child:
        try:
            os.kill(child, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + JOB_STOP_GRACE
    while ((helper and job_alive(run)) or (child and pid_alive(child))) and time.time() < deadline:
        time.sleep(0.5)
    left = [p for p in tree + ([child] if child else []) if pid_alive(p)]
    if (helper and job_alive(run)) or left:
        log("  job %s: %s still running after SIGTERM, sending SIGKILL"
            % (run.get("sid"), ", ".join(str(p) for p in left) or "helper %d" % pid))
    if child:
        try:
            os.killpg(child, signal.SIGKILL)
        except OSError:
            pass
    for p in left:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    if helper:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    proc = _SPAWNED.get(pid)
    if proc is not None:
        try:
            proc.wait(5)
        except Exception:
            pass


def size_of(path):
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def transcript_since(path, offset):
    """(end-of-turn text or None, quota stop or None) in a transcript after `offset`.

    The text is the last assistant message when nothing has followed it, i.e.
    the session is sitting on a reply. The quota stop is one the session ended
    on (the same strict test as for ordinary resumes)."""
    text, block = None, None
    for line in read_from(path, offset).splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        stop = rate_limit_stop(rec)
        if stop:
            block, text = stop, None
            continue
        kind = rec.get("type")
        if kind not in ("assistant", "user") or rec.get("isMeta"):
            continue
        block = None
        content = (rec.get("message") or {}).get("content")
        if kind == "assistant" and not (isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_use" for c in content)):
            said = text_of(content).strip()
            text = said or text
        else:
            text = None
    return text, block


def job_verdict(text):
    """(status, why, reply) when the last non-blank line of the end-of-turn
    text is a JOB DONE or JOB FAILED marker (JOB_DONE_RE, JOB_FAILED_RE)."""
    lines = (text or "").rstrip().splitlines()
    if not lines:
        return None
    last, reply = lines[-1], "\n".join(lines[:-1]).strip()
    if JOB_DONE_RE.match(last):
        return "done", "", reply
    failed = JOB_FAILED_RE.match(last)
    if failed:
        return "failed", (failed.group(1).strip() or "the job reported failure")[:200], reply
    return None


def read_from(path, offset):
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""


def mtime(path):
    """A file's mtime, 0.0 when it is gone (a transcript can vanish between a
    glob and the stat): a missing file counts as long idle."""
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def run_records(path, offset):
    """The transcript records after `offset`, as dicts."""
    out = []
    for line in read_from(path, offset).splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def did_work(path, offset):
    """Did the session reply at all after `offset`, other than with a quota stop?"""
    return any(rec.get("type") == "assistant" and not rate_limit_stop(rec)
               for rec in run_records(path, offset))


def typed_after(path, offset):
    """Was a turn typed into the session after `offset` (an answer on the phone)?"""
    return any(typed_by_user(rec, JOB_RESUME_PROMPT) for rec in run_records(path, offset))


def job_check(js, run, now):
    """None while the job should carry on, else (status, why, reply, keep_open)."""
    path = session_file(run["sid"])
    text, block = transcript_since(path, run.get("offset", 0)) if path else (None, None)
    verdict = job_verdict(text)
    if verdict:
        return verdict + (True,)
    if block:
        # `until` keeps the job from resuming before this limit resets, whatever
        # the readings say; `idle` marks a resume that got no work done.
        return ("paused", "usage limit until %s" % fmt_when(block["resets_at"]), "", False,
                {"until": block["resets_at"], "idle": not did_work(path, run.get("offset", 0))})
    if not job_alive(run):
        if not path:
            return ("failed", "session exited before it started; see %s" % run.get("pty"),
                    "", False)
        return ("paused", "session exited before JOB DONE", "", False)
    hit = cap_hit(ensure_usage(js, True, now), now, js)
    if hit:
        return ("paused", hit, "", False)
    if run.get("week_resets") and now >= run["week_resets"]:
        # A new week's quota: the gate decides again, as for any start.
        return ("paused", "the weekly reset passed while it ran", "", False)
    started = run.get("started_at", now)
    if not path and now - started > JOB_START_TIMEOUT:
        return ("failed", "session did not start within %d min (a dialog waiting? see %s)"
                % (JOB_START_TIMEOUT // 60, run.get("pty")), "", False)
    if now - started > JOB_MAX_H * 3600:
        return ("failed", "ran past %d h" % JOB_MAX_H, "", False)
    if text and now - mtime(path) > JOB_IDLE:
        first = text.strip().splitlines()[-1][:120]
        return ("blocked", "stopped without JOB DONE: %s" % first, text, True)
    return None


def record_end(path, status, why, reply, started, now, resumes=0, later=False, changes=None):
    changes = dict({"status": status, "paused_until": None}, **(changes or {}))
    append = None
    if status == "paused":
        changes["paused_why"] = why
    else:
        changes["paused_why"] = None
        if status in ("done", "failed"):
            changes["finished"] = stamp(now)
        append = "## Run %s → %s (%s%s%s)\n\n%s" % (
            stamp(started), stamp(now), status,
            ", resume %d" % resumes if resumes else "",
            ", after your answer" if later else "",
            reply or ("Failed: %s" % why))
        if status == "blocked":
            append += "\n\nWaiting for an answer in Remote Control: %s" % why
        elif status == "failed" and reply and why not in reply:
            append += "\n\nFailed: %s" % why
    update_note(path, changes, append)


# ---- commit between jobs (2026-11-23) ------------------------------------
# A note with `commit: <repo>` has what the job changed in that repo committed
# by the runner itself (never by the job session, whose git stays read-only),
# right after JOB DONE and before the next job can start. The message is the
# job's last ```change block. A queued job whose repo has uncommitted changes
# does not start, so every job begins on a committed tree.

JOB_COMMIT_MAX_BYTES = 10 * 1024 * 1024   # a staged file this big is refused (a stray download)
CHANGE_BLOCK = re.compile(r"```change[ \t]*\n(.*?)```", re.S)


def commit_repo(meta):
    """The note's `commit:` repo as written (expanded), or None."""
    raw = str((meta or {}).get("commit") or "").strip()
    return os.path.expanduser(raw) if raw else None


def run_git(repo, args, stdin=None):
    """(returncode, stdout, stderr) of the real git in `repo`; (None, "", why) if it can't run."""
    exe = real_git(os.path.join(JOB_BIN, "git"))
    if not exe:
        return None, "", "git not found on PATH"
    env = {k: v for k, v in os.environ.items() if k != "LIMIT_RESUME_JOB"}
    try:
        r = subprocess.run([exe, "-C", repo] + list(args), input=stdin, capture_output=True,
                           text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", str(exc)
    return r.returncode, r.stdout, r.stderr.strip()


def repo_problem(repo):
    """None when `repo` is an absolute path to the top of a git work tree, else why not."""
    if not os.path.isabs(repo):
        return "commit repo is not an absolute path: %s" % repo
    code, out, err = run_git(repo, ["rev-parse", "--show-toplevel"])
    if code != 0:
        return "not a git work tree: %s (%s)" % (repo, err or "git failed")
    if os.path.realpath(out.strip()) != os.path.realpath(repo):
        return "not the top of its work tree: %s" % repo
    return None


def repo_dirty(repo):
    """Uncommitted paths in `repo` (git status --porcelain), or None if git failed."""
    code, out, _ = run_git(repo, ["status", "--porcelain", "--untracked-files=all"])
    return None if code != 0 else [l for l in out.splitlines() if l.strip()]


def commit_message(title, reply, note, sid):
    blocks = CHANGE_BLOCK.findall(reply or "")
    body = blocks[-1].strip() if blocks else ""
    head = title if not body else "%s\n\n%s" % (title, body)
    return "%s\n\nJob: %s\nSession: %s\n" % (head, os.path.basename(note), sid)


def job_commit(repo, title, reply, note, sid):
    """Stage everything in `repo` and commit it. (sha, None), (None, None) when
    there was nothing to commit, or (None, why) - nothing is left staged then."""
    problem = repo_problem(repo)
    if problem:
        return None, problem
    code, _, err = run_git(repo, ["add", "-A"])
    if code != 0:
        return None, "git add failed: %s" % err[:200]
    code, out, err = run_git(repo, ["diff", "--cached", "--name-only", "-z"])
    if code != 0:
        run_git(repo, ["reset", "-q"])
        return None, "git diff failed: %s" % err[:200]
    staged = [f for f in out.split("\0") if f]
    if not staged:
        return None, None
    big = [f for f in staged if os.path.isfile(os.path.join(repo, f))
           and os.path.getsize(os.path.join(repo, f)) > JOB_COMMIT_MAX_BYTES]
    if big:
        run_git(repo, ["reset", "-q"])
        return None, "refused: staged file over %d bytes: %s" % (JOB_COMMIT_MAX_BYTES, ", ".join(big[:5]))
    code, _, err = run_git(repo, ["commit", "-q", "-F", "-"],
                           stdin=commit_message(title, reply, note, sid))
    if code != 0:
        run_git(repo, ["reset", "-q"])
        return None, "git commit failed: %s" % err[:200]
    code, out, _ = run_git(repo, ["rev-parse", "--short=12", "HEAD"])
    return (out.strip() if code == 0 else "unknown"), None


def commit_on_done(note, title, sid, status, why, reply):
    """For a job ending `done` whose note names a commit repo: commit, and turn
    a failed commit into a failed job. Returns (status, why, reply, changes)."""
    if status != "done":
        return status, why, reply, {}
    try:
        meta = read_note(note)[0]
    except OSError:
        meta = {}
    repo = commit_repo(meta)
    if not repo:
        return status, why, reply, {}
    sha, err = job_commit(repo, title, reply, note, sid)
    if err:
        log("jobs: %s commit failed - %s" % (title, err))
        return "failed", "commit failed: %s" % err, reply, {}
    if not sha:
        log("jobs: %s done, nothing to commit in %s" % (title, repo))
        return status, why, (reply or "") + "\n\nNothing to commit in %s." % repo, {}
    log("jobs: %s committed %s in %s" % (title, sha, repo))
    return status, why, (reply or "") + "\n\nCommitted `%s` in %s." % (sha, repo), {"committed": sha}


def job_finish(js, run, now, status, why, reply, keep_open, extra=None):
    """End the running job: keep its session open or stop it, record the end
    in the note, alert. extra (a quota stop): `until` the stop's reset, kept in
    the note as paused_until; `idle`, the resume did no work, so it is not
    counted against JOB_MAX_RESUMES."""
    title = run.get("title", "")
    if keep_open and job_alive(run):
        js.setdefault("open", []).append(dict(run, status=status, offset=size_of(
            session_file(run["sid"]))))
    else:
        job_stop(run)
    changes = {}
    if (extra or {}).get("until"):
        changes["paused_until"] = stamp(math.ceil((extra["until"] + BUFFER) / 60.0) * 60)
    if (extra or {}).get("idle") and run.get("resumes"):
        changes["resumes"] = str(run["resumes"] - 1) if run["resumes"] > 1 else None
    status, why, reply, committed = commit_on_done(run["note"], title, run.get("sid", ""),
                                                   status, why, reply)
    changes.update(committed)
    record_end(run["note"], status, why, reply, run.get("started_at", now), now,
               run.get("resumes", 0), later=run.get("answered", False), changes=changes)
    log("jobs: %s %s%s" % (title, status, " - " + why if why else ""))
    word = {"done": "finished", "paused": "paused", "failed": "FAILED",
            "blocked": "needs an answer in Remote Control"}[status]
    notify("Job %s: %s%s" % (word, title, " (%s)" % why if why else ""))
    js.pop("running", None)


def open_tick(js, now):
    """Sessions left open after their job ended: a blocked one may still finish
    once you answer it in Remote Control; each closes after it has been idle.

    Returns the first blocked session you have typed into since it was blocked
    and that has not finished yet: it is working again, so it becomes the
    running job (one task at a time, under the caps). It leaves `open`."""
    keep, woke = [], None
    for sess in js.get("open", []):
        if not job_alive(sess):
            continue
        path = session_file(sess["sid"])
        if sess.get("status") == "blocked" and path:
            verdict = job_verdict(transcript_since(path, sess.get("offset", 0))[0])
            if verdict:
                status, why, reply = verdict
                status, why, reply, committed = commit_on_done(
                    sess["note"], sess.get("title", ""), sess.get("sid", ""), status, why, reply)
                record_end(sess["note"], status, why, reply, sess.get("started_at", now), now,
                           later=True, changes=committed)
                notify("Job %s: %s" % ("finished" if status == "done" else "FAILED",
                                       sess.get("title", "")))
                # Only what you type after this end counts (typed_finished).
                sess["status"], sess["offset"] = status, size_of(path)
            elif woke is None and typed_after(path, sess.get("offset", 0)):
                woke = sess
                continue
        idle = now - mtime(path)
        limit = JOB_BLOCKED_KEEP if sess.get("status") == "blocked" else JOB_KEEP_OPEN
        if idle > limit:
            log("jobs: closing the idle session of %s" % sess.get("title"))
            job_stop(sess)
            continue
        keep.append(sess)
    js["open"] = keep
    return woke


def typed_finished(js, now):
    """The finished (done or failed) job session, kept open, that you have typed
    into since it ended and whose transcript was written in the last
    RESUME_BUSY s, or None. It is the running task for the one-task-at-a-time
    rule, like a resumed chat: the next job and due resumes wait until it has
    been quiet for RESUME_BUSY. It has no caps and no JOB_MAX_H, and its note
    keeps its status."""
    for sess in (js or {}).get("open", []):
        if sess.get("status") not in ("done", "failed"):
            continue
        path = session_file(sess["sid"])
        if (path and now - mtime(path) < RESUME_BUSY
                and typed_after(path, sess.get("offset", 0)) and job_alive(sess)):
            return sess
    return None


def wake_answered(js, sess, now):
    """A blocked session you answered becomes the running job again; a job
    running meanwhile is paused first (one task at a time). The caps, the
    weekly reset and JOB_MAX_H apply to it from now on, as to any run."""
    run = js.get("running")
    if run:
        job_finish(js, run, now, "paused", "you answered %s, which runs first"
                   % sess.get("title"), "", False)
    js["running"] = dict({k: v for k, v in sess.items() if k != "status"},
                         started_at=now, answered=True,
                         week_resets=weekly_reset(load_usage(), js, now))
    update_note(sess["note"], {"status": "running", "paused_why": None})
    log("jobs: %s: answered in Remote Control, it is the running job again" % sess.get("title"))


def runner_name():
    """This machine, as written into the `runner:` of the notes it runs: on
    macOS the LocalHostName, which does not change with the network the way
    the DHCP-given host name can; else the short host name."""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
            if name:
                return name
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().split(".")[0] or "unknown"


def other_runner(job, me):
    """Is this note another machine's? A paused, running or blocked note stays
    with the machine that has its session; a queued one is anyone's."""
    owner = str(job["meta"].get("runner") or "").strip()
    return bool(owner) and owner != me and job["status"] != "queued"


def note_status(path):
    """The status the note says now (you may have changed it), None if unreadable."""
    try:
        return (read_note(path)[0].get("status") or "").strip().lower()
    except OSError:
        return None


def job_helper(sid, rows):
    """Pid of a live `jobpty` helper running session `sid`, or None."""
    for pid, _ppid, cmd in rows:
        m = AGENT_PROC.match(cmd)
        if m and m.group(1) == "jobpty" and sid in cmd:
            return pid
    return None


def adopt(job, pid, now):
    """The run record for a job session found still running under its helper."""
    sid = job["meta"]["session_id"]
    pty_log = os.path.join(JOB_LOGS, sid + ".pty.log")
    try:
        resumes = int(job["meta"].get("resumes") or 0)
    except ValueError:
        resumes = 0
    return {"note": job["path"], "sid": sid, "pid": pid, "pty": pty_log, "title": job["title"],
            "started_at": mtime(pty_log + ".pid") or now, "offset": 0, "resumes": resumes,
            "adopted": True}


def job_start(state, js, job, now):
    meta, path, title = job["meta"], job["path"], job["title"]
    resume = job["status"] in ("paused", "limit") and bool(meta.get("session_id"))
    if job["status"] in ("paused", "limit") and not resume:
        log("jobs: %s is paused but has no session_id: starting it from scratch" % title)
    sid = meta.get("session_id") if resume else str(uuidlib.uuid4())
    try:
        resumes = int(meta.get("resumes") or 0)
    except ValueError:
        resumes = 0

    def fail(why):
        update_note(path, {"status": "failed", "finished": stamp(now)},
                    "## Run %s (failed)\n\n%s" % (stamp(now), why))
        log("jobs: %s failed - %s" % (title, why))
        notify("Job FAILED: %s (%s)" % (title, why))
        return False
    if resume and resumes >= JOB_MAX_RESUMES:
        return fail("Gave up after %d resumes." % resumes)
    repo = commit_repo(meta) if job["status"] == "queued" else None
    if repo:
        # Commit between jobs: a fresh job starts only on a committed tree.
        problem = repo_problem(repo)
        dirty = None if problem else repo_dirty(repo)
        if problem or dirty is None or dirty:
            why = "%s waits - %s" % (title, problem or (
                "git status failed in %s" % repo if dirty is None else
                "%s has %d uncommitted path(s), e.g. %s; commit or clean them"
                % (repo, len(dirty), dirty[0].strip())))
            if js.get("commit_refused") != why:
                js["commit_refused"] = why
                log("jobs: " + why)
                notify("Job " + why)
            return False
        js.pop("commit_refused", None)
    # An earlier session of this note, still open (finished or blocked), goes first.
    for sess in [s for s in js.get("open", []) if s.get("note") == path]:
        job_stop(sess)
    js["open"] = [s for s in js.get("open", []) if s.get("note") != path]
    if resume:
        # Never a second session on one transcript: a lost helper may still run it.
        rows = ps_rows()
        owner = local_owner(sid, rows) if rows is not None else None
        if rows is None or owner:
            why = "%s not resumed - %s" % (title, "session %s is open in pid %d" % (sid, owner)
                                           if owner else "ps failed, cannot tell if it runs")
            if js.get("resume_refused") != why:
                js["resume_refused"] = why
                log("jobs: " + why)
            return False
        js.pop("resume_refused", None)
    # Too large to resume, or its transcript is gone: the job restarts in a new session.
    fresh, tokens = None, context_tokens(session_file(sid)) if resume else 0
    gone = resume and not session_file(sid)
    if tokens > JOB_NEW_AT or gone:
        fresh, resume, sid = sid, False, str(uuidlib.uuid4())
    cwd = os.path.expanduser(meta.get("cwd") or js.get("cwd") or HOME)
    if not os.path.isdir(cwd):
        return fail("Working directory does not exist: %s" % cwd)
    if folder_trusted(cwd) is False:
        return fail("Folder not trusted: %s. Run `claude` there once and accept the trust "
                    "prompt, then set status: queued again." % cwd)
    claude = shutil.which("claude", path=os.environ.get("PATH", "") + ":" + CLAUDE_PATH_HINT)
    if not claude:
        log("jobs: `claude` not on PATH, not starting %s" % title)
        return False
    settings = job_settings()
    if not settings:
        return False                       # logged; nothing changed, tried again next sweep
    common = ["--remote-control", ("job: %s" % title)[:80], "--model", JOB_MODEL,
              "--permission-mode", JOB_PERM, "--settings", settings]
    append = None
    body = job_prompt_body(job["body"])
    if resume:
        argv = [claude, JOB_RESUME_PROMPT, "--resume", sid] + common
        changes = {"status": "running", "resumes": str(resumes + 1), "paused_why": None}
    elif fresh:
        argv = [claude, JOB_RULES % path + body + JOB_FRESH_NOTE % fresh,
                "--session-id", sid] + common
        changes = {"status": "running", "session_id": sid, "previous_session": fresh,
                   "resumes": str(resumes + 1), "finished": None, "paused_why": None}
        why = ("its transcript is gone" if gone else "it had %dk tokens of context (> %dk), "
               "too large to resume" % (tokens // 1000, JOB_NEW_AT // 1000))
        append = ("## Run %s (new session)\n\nSession `%s`: %s; continuing in new session `%s`."
                  % (stamp(now), fresh, why, sid))
    else:
        argv = [claude, JOB_RULES % path + body, "--session-id", sid] + common
        changes = {"status": "running", "session_id": sid, "started": stamp(now),
                   "finished": None, "paused_why": None, "resumes": None,
                   "previous_session": None}
    changes.update(runner=runner_name(), paused_until=None)
    # Record the start BEFORE launching, in the note and in the state: a sweep
    # killed after the launch leaves a run with no pid, whose helper the next
    # sweep finds and adopts (or, if there is none, sets back to paused).
    update_note(path, changes, append)
    sids = [s for s in js.get("sids", []) if s != sid] + [sid]
    js["sids"] = sids[-200:]
    pty_log = os.path.join(JOB_LOGS, sid + ".pty.log")
    js["running"] = run = {"note": path, "sid": sid, "pid": None, "pty": pty_log,
                           "title": title, "started_at": now,
                           "offset": size_of(session_file(sid)) if resume else 0,
                           "resumes": resumes + 1 if resume or fresh else 0,
                           "week_resets": weekly_reset(load_usage(), js, now)}
    save_state(state)
    log("jobs: %s %s in Remote Control (session %s, cwd %s)"
        % ("resuming" if resume else "restarting (new session)" if fresh else "starting",
           title, sid, cwd))
    pid = job_spawn(argv, pty_log, cwd)
    if not pid:
        js.pop("running", None)
        return fail("Could not launch the session.")
    run["pid"] = pid
    save_state(state)
    notify("Job %s in Remote Control: %s" % (
        "resumed" if resume else "restarted in a new session" if fresh else "started", title))
    return True


def watch_running(js, run, now):
    """None while the running job carries on, else the outcome for job_finish.
    Also stops it, keeping your status, when you have changed its note."""
    if not run.get("pid"):
        # The launch was recorded but its pid was not: the sweep was killed
        # in between. The helper, if it started, is still there.
        rows = ps_rows()
        helper = job_helper(run["sid"], rows) if rows is not None else None
        if rows is None:
            return None                    # cannot tell yet; look again next sweep
        if not helper:
            return ("paused", "launch interrupted", "", False)
        run["pid"] = helper
        log("jobs: %s: found its session running (helper pid %d), watching it" % (
            run.get("title"), helper))
    said = note_status(run["note"])
    if said not in (None, "running"):
        job_stop(run)
        log("jobs: %s: its note says %s, session stopped and the status kept" % (
            run.get("title"), said))
        notify("Job stopped: %s (its note was set to %s)" % (run.get("title"), said))
        js.pop("running", None)
        return None
    return job_check(js, run, now)


def note_changes(js, jobs):
    """Log, once, notes the runner cannot act on: a status it does not know,
    and a done note set back to queued (by you, or by a sync revert)."""
    warned = js.get("status_warned") or {}
    for job in jobs:
        if job["status"] not in JOB_STATUSES and warned.get(job["name"]) != job["status"]:
            log("jobs: %s: status %r is not one of %s; the note is left alone"
                % (job["name"], job["status"], ", ".join(JOB_STATUSES)))
    js["status_warned"] = {j["name"]: j["status"] for j in jobs if j["status"] not in JOB_STATUSES}
    done = set(js.get("done_seen") or [])
    for job in jobs:
        if job["name"] in done and job["status"] == "queued":
            log("jobs: %s went from done back to queued; it runs again" % job["name"])
            notify("Job note %s went from done back to queued, so it will run again. If you "
                   "did not do that (a sync revert?), set it to done." % job["name"])
    js["done_seen"] = sorted(j["name"] for j in jobs if j["status"] == "done")


def lost_jobs(js, jobs, now):
    """Notes marked running that this runner is not running: a session still
    alive under its helper is adopted as the running job (a second one is
    stopped), any other is set back to paused. False when ps failed: nothing
    may start, because a lost session might still be running."""
    rows = None
    for job in jobs:
        if job["status"] != "running":
            continue
        sid = job["meta"].get("session_id") or ""
        if rows is None:
            rows = ps_rows()
            if rows is None:
                log("jobs: ps failed, cannot tell whether %s still runs; starting nothing"
                    % job["title"])
                return False
        helper = job_helper(sid, rows) if sid else None
        if helper and not js.get("running"):
            js["running"] = adopt(job, helper, now)
            log("jobs: %s: its session is still running (helper pid %d), watching it again"
                % (job["title"], helper))
            continue
        if helper:
            job_stop({"pid": helper, "sid": sid, "pty": os.path.join(JOB_LOGS, sid + ".pty.log")})
            why = "a second session of it was running; stopped"
        else:
            why = "interrupted, runner lost track of it"
        update_note(job["path"], {"status": "paused", "paused_why": why})
        job["status"] = "paused"
    return True


def paused_until(job):
    """Epoch of the note's paused_until (a quota stop's reset), or None."""
    try:
        return datetime.datetime.strptime(str(job["meta"].get("paused_until")),
                                          "%Y-%m-%dT%H:%M").timestamp()
    except ValueError:
        return None


def jobs_tick(state, now=None, may_start=True):
    """One sweep's worth of job handling: watch the running job, start the next.
    may_start=False (a chat resume is due or still running) only watches."""
    js = state.get(JOBS_KEY)
    if not js or (not js.get("on") and not js.get("running") and not js.get("open")):
        return
    now = now or time.time()
    woke = open_tick(js, now)
    if woke:
        wake_answered(js, woke, now)
    run = js.get("running")
    if run:
        outcome = watch_running(js, run, now)
        if js.get("running") and outcome is None:
            return                        # still working
        if outcome is not None:
            job_finish(js, run, now, *outcome)
        now = time.time()
    if not js.get("on"):
        return
    folder = js.get("dir")
    if not folder or not os.path.isdir(folder):
        if js.get("dir_missing") != folder:
            js["dir_missing"] = folder
            log("jobs: job folder missing: %s" % folder)
        return
    js.pop("dir_missing", None)
    me = runner_name()
    jobs = [j for j in job_notes(folder) if not other_runner(j, me)]
    note_changes(js, jobs)
    if not lost_jobs(js, jobs, now) or js.get("running"):
        return
    job = pick_job(jobs)
    if not job:
        js.pop("gate_why", None)
        return
    typed = typed_finished(js, now)
    if typed:
        why = "%s waits - you are working in finished job %s" % (job["title"], typed.get("title"))
        if js.get("gate_why") != why:
            js["gate_why"] = why
            log("jobs: " + why)
        return
    if not may_start:
        if js.get("gate_why") != "resume first":
            js["gate_why"] = "resume first"
            log("jobs: %s waits - a chat resume goes first" % job["title"])
        return
    until = paused_until(job)
    if until and now < until:
        why = "%s waits for its usage limit to reset (%s)" % (job["title"], fmt_when(until))
        if js.get("gate_why") != why:
            js["gate_why"] = why
            log("jobs: " + why)
        return
    usage = ensure_usage(js, False, now)
    ok, why = job_gate(usage, now, js)
    if not ok:
        if js.get("gate_why") != why:
            js["gate_why"] = why
            log("jobs: %d waiting, not starting - %s"
                % (sum(j["status"] in ("queued", "paused", "limit") for j in jobs), why))
        return
    js.pop("gate_why", None)
    job_start(state, js, job, now)


@with_state_lock
def cmd_jobs(argv):
    sub = argv[0] if argv else "status"
    state = load_state()
    js = state.setdefault(JOBS_KEY, {})
    if sub == "on":
        rest = argv[1:]
        if "--cwd" in rest:
            i = rest.index("--cwd")
            if i + 1 >= len(rest):
                print("usage: limit-resume.py jobs on [dir] [--cwd path] [--reset \"Mon 03:00\"]")
                return 2
            js["cwd"] = os.path.abspath(os.path.expanduser(rest[i + 1]))
            rest = rest[:i] + rest[i + 2:]
        if "--reset" in rest:
            i = rest.index("--reset")
            spec = rest[i + 1] if i + 1 < len(rest) else ""
            if spec.lower() in ("auto", "none", ""):
                js.pop("weekly_reset", None)
            elif next_weekly(spec, time.time()) is None:
                print('--reset wants a weekday and time, e.g. --reset "Thu 14:00"')
                return 2
            else:
                js["weekly_reset"] = spec
            rest = rest[:i] + rest[i + 2:]
        folder = rest[0] if rest else js.get("dir") or os.environ.get("LIMIT_RESUME_JOBS_DIR")
        if not folder:
            print("usage: limit-resume.py jobs on <job folder> [--cwd default-dir]")
            return 2
        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            print("no such folder: %s" % folder)
            return 1
        js.update(on=True, dir=folder)
        js.pop("gate_why", None)
        save_state(state)
        log("jobs on: %s (default cwd %s, weekly reset %s)" % (
            folder, js.get("cwd") or HOME, js.get("weekly_reset") or "from usage"))
        return 0
    if sub == "off":
        js["on"] = False
        save_state(state)
        log("jobs off: no new jobs will start%s"
            % ("; %s keeps running" % js["running"].get("title") if js.get("running") else ""))
        return 0
    if sub == "stop":
        # The running job only; its note is set to hold, so nothing resumes it.
        run = js.get("running")
        if not run:
            print("no job is running")
            return 0
        job_stop(run)
        update_note(run["note"], {"status": "hold", "paused_why": "stopped with `jobs stop`"})
        js.pop("running", None)
        save_state(state)
        log("jobs stop: %s stopped, its note set to hold" % run.get("title"))
        return 0
    if sub == "probe":
        reading, err = probe_usage()
        if reading:
            save_usage(reading, "probe")
        print(err or "usage read")
        js["last_probe"] = time.time()
        save_state(state)
        sub = "status"
    if sub != "status":
        print("usage: limit-resume.py jobs on [dir] [--cwd path] | off | stop | status | probe")
        return 2
    now = time.time()
    print("jobs       : %s" % ("ON" if js.get("on") else "off"))
    print("folder     : %s" % (js.get("dir") or "(none - `jobs on <folder>`)"))
    print("default cwd: %s" % (js.get("cwd") or HOME))
    usage = load_usage()
    for win, label in (("five_hour", "5-hour"), ("seven_day", "weekly")):
        val = usage.get(win) or {}
        pct = window_pct(usage, win, now)
        if pct is None:
            print("%-11s: unknown" % label)
            continue
        print("%-11s: %d%%%s  (%s, %d min ago)" % (
            label, pct, ", resets %s" % fmt_when(val["resets_at"]) if val.get("resets_at") else "",
            val.get("source", "?"), (now - val.get("at", now)) // 60))
    if js.get("probe_error"):
        print("last probe : %s" % js["probe_error"])
    resets = weekly_reset(usage, js, now)
    src = ("from usage" if ((usage.get("seven_day") or {}).get("resets_at") or 0) > now
           else "last seen, rolled forward" if js.get("reset_seen")
           else "--reset %s" % js["weekly_reset"] if js.get("weekly_reset") else "")
    print("weekly reset: %s%s" % (fmt_when(resets) if resets else "unknown",
                                  " (%s)" % src if src and resets else ""))
    moved = js.get("reset_moved")
    if moved:
        print("reset moved: %s, from %s to %s" % (stamp(moved[2]), fmt_when(moved[0]),
                                                  fmt_when(moved[1])))
    print("limits     : 5-hour pause trigger %d%% (a job lands ~70-80%%), weekly cap < %d%%"
          % (JOB_FIVE_HOUR_MAX, weekly_cap(now, resets)))
    ok, why = job_gate(usage, now, js)
    print("gate       : %s" % ("OPEN - " + why[6:] if ok else "closed - " + why))
    run = js.get("running")
    if run:
        print("running    : %s (pid %s, since %s, session %s)" % (
            run.get("title"), run.get("pid"), stamp(run.get("started_at", now)), run.get("sid")))
    for sess in js.get("open", []):
        print("open       : %s (%s, session %s)" % (sess.get("title"), sess.get("status"),
                                                   sess.get("sid")))
    if js.get("dir") and os.path.isdir(js["dir"]):
        jobs = job_notes(js["dir"])
        print("queue      :" + ("" if jobs else " empty"))
        me = runner_name()
        for job in jobs:
            extra = job["meta"].get("paused_why") if job["status"] in ("paused", "limit") else ""
            if job["status"] in ("paused", "limit") and job["meta"].get("paused_until"):
                extra += ", not before %s" % job["meta"]["paused_until"]
            if other_runner(job, me):
                extra = "%s's, left alone%s" % (job["meta"]["runner"], ": " + extra if extra else "")
            print("  %-8s %5s  %s%s" % (job["status"] or "-", job["meta"].get("order", ""),
                                       job["title"], "  [%s]" % extra if extra else ""))
    return 0


def cmd_statusline(argv):
    """Status line entry point: caches the usage figures Claude Code hands every
    status line, so the job runner rarely needs to probe. With `-- command`, the
    same stdin is passed on to your own status line and its output printed."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    reading = reading_from_statusline(data)
    save_usage(reading, "statusline")
    if argv and argv[0] == "--":
        argv = argv[1:]
    if argv:
        cmd = argv if len(argv) > 1 else ["/bin/sh", "-c", argv[0]]
        try:
            proc = subprocess.run(cmd, input=raw, capture_output=True, text=True, timeout=10)
            sys.stdout.write(proc.stdout)
            return 0
        except Exception:
            pass
    parts = []
    for win, label in (("five_hour", "5h"), ("seven_day", "wk")):
        pct = (reading.get(win) or {}).get("pct")
        if pct is not None:
            parts.append("%s %d%%" % (label, pct))
    print(" · ".join(parts))
    return 0


# -- the commit/push guard ------------------------------------------------------
#
# A PreToolUse hook on every tool of a job session (job-settings.json). It
# fails closed: input it cannot read, a command it cannot parse, a script it
# cannot read and any error of its own block the call. Shell commands are
# parsed, not matched: every simple command, through `;` `&&` `|` `$( )`
# backticks, heredocs, `sh -c`, `eval`, launchers (env, xargs, timeout,
# find -exec, ...), script files and python/node/ruby/perl code. git may run
# only read-only subcommands; `-c alias.*` is followed, any other `-c` refused.
# Writes to the kit, to Claude settings (they can turn hooks off) and into a
# .git directory (aliases, hooks) are refused too. It is a static check: code
# that builds the word "git" at run time is beyond it.

class Blocked(Exception):
    """Why the guard refuses a tool call."""


GUARD_PUNCT = ";&|()<>\n"
GUARD_DEPTH = 8                 # nested sh -c / eval / scripts / aliases
GUARD_FILE_MAX = 1024 * 1024    # bytes of a script read to check it
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "rev-list", "ls-files", "ls-tree",
            "cat-file", "blame", "annotate", "describe", "shortlog", "grep", "merge-base",
            "name-rev", "show-ref", "show-branch", "for-each-ref", "cherry", "count-objects",
            "diff-tree", "diff-files", "diff-index", "range-diff", "whatchanged",
            "check-ignore", "check-attr", "check-ref-format", "check-mailmap", "var",
            "version", "help", "verify-commit", "verify-tag", "branch", "tag", "remote",
            "config", "stash", "worktree", "reflog", "notes", "submodule"}
# git subcommands that create commits or push; code and dynamic commands that
# mention one of these and git are blocked.
GIT_WRITES = {"commit", "push", "merge", "rebase", "revert", "cherry-pick", "pull", "stash",
              "am", "update-ref", "commit-tree", "filter-branch", "fast-import"}
GIT_GLOBAL_FLAGS = {"--no-pager", "-p", "-P", "--paginate", "--bare", "--no-replace-objects",
                    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
                    "--icase-pathspecs", "--no-optional-locks", "--exec-path", "--html-path",
                    "--man-path", "--info-path", "--version", "--help", "-h", "--no-advice"}
GIT_GLOBAL_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                    "--attr-source"}
GIT_ENV_OK = {"GIT_DIR", "GIT_WORK_TREE", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS",
              "GIT_CEILING_DIRECTORIES", "GIT_FLUSH", "GIT_LITERAL_PATHSPECS",
              "GIT_NO_REPLACE_OBJECTS"}
PAGERS_OK = {"", "cat", "less", "more"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
# interpreter -> (flags whose value is code, flags that take a value)
INTERPRETERS = {"node": (("-e", "--eval", "-p", "--print"), ("-r", "--require", "--import")),
                "nodejs": (("-e", "--eval", "-p", "--print"), ("-r", "--require", "--import")),
                "bun": (("-e", "--eval", "-p", "--print"), ()),
                "ruby": (("-e",), ("-I", "-r", "-E", "-C")),
                "perl": (("-e", "-E"), ("-I", "-M", "-m")),
                "php": (("-r",), ("-c", "-d")),
                "osascript": (("-e",), ())}
PYTHON = re.compile(r"^(python|pypy)[0-9.]*$")
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{",
                  "}", "time", "esac", "coproc"}
WRITE_CMDS = {"cp", "mv", "ln", "install", "rsync", "tee", "touch", "rm", "rmdir", "unlink",
              "chmod", "chown", "truncate", "dd", "mkdir"}
GUARD_READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
GUARD_NO = ("Blocked by limit-resume: background jobs must not git commit or git push, and "
            "may run git only to read (status, log, diff, show, rev-parse, branch --list, "
            "...). Leave the changes uncommitted and list them in your summary. ")


def kit_path(path, cwd):
    """Is `path` one a job must not write: the kit's own files (~/.claude/tools),
    a Claude settings file (it can turn the hooks off), or anything inside a
    .git directory (config aliases, hooks)?"""
    p = os.path.expanduser(path.replace("${HOME}", HOME).replace("$HOME", HOME))
    for full in {os.path.normpath(os.path.join(cwd, p)),
                 os.path.realpath(os.path.join(cwd, p))}:
        parts = full.split(os.sep)
        tools = os.path.normpath(TOOLS)
        if (full == tools or full.startswith(tools + os.sep) or ".git" in parts[:-1]
                or (len(parts) > 1 and parts[-2] == ".claude"
                    and parts[-1].startswith("settings"))):
            return True
    return False


def shell_prepass(text):
    """(text, heredoc bodies) for the tokenizer: comments dropped, backticks
    turned into $( ), and each heredoc's body taken out, in order."""
    out, bodies, pending = [], [], []
    i, n, quote, tick, start = 0, len(text), None, False, True
    while i < n:
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i, start = i + 2, False
            continue
        if c == "`":
            out.append(")" if tick else "$(")
            tick = not tick
            i += 1
            continue
        if quote == '"':
            quote = None if c == '"' else quote
            out.append(c)
            i += 1
            continue
        if c in "'\"":
            quote, start = c, False
        elif c == "#" and start:
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|<>()'\"]+))",
                         text[i:])
            if m:
                pending.append((m.group(2) or m.group(3) or m.group(4) or "", m.group(1)))
                out.append(text[i:i + m.end()])
                i, start = i + m.end(), False
                continue
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for delim, strip in pending:
                body = []
                while i < n:
                    j = text.find("\n", i)
                    j = n if j == -1 else j
                    line, i = text[i:j], j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    body.append(line)
                bodies.append("\n".join(body))
            pending, start = [], True
            continue
        out.append(c)
        start = c in " \t\n;&|()<>"
        i += 1
    bodies += ["" for _ in pending]
    return "".join(out), bodies


def substitutions(word):
    """The commands inside each $( ) of a word (also inside double quotes)."""
    out, i = [], word.find("$(")
    while i != -1:
        depth, j = 0, i + 1
        while j < len(word):
            depth += {"(": 1, ")": -1}.get(word[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(word[i + 2:j])
        i = word.find("$(", j)
    return out


def expand(word, ctx):
    """`word` with $NAME / ${NAME} set earlier in the same command filled in."""
    return re.sub(r"\$\{?([A-Za-z_]\w*)\}?",
                  lambda m: ctx["vars"].get(m.group(1), m.group(0)), word)


def guard_text(text, ctx, depth):
    """Check every command in shell text. Raises Blocked."""
    if depth > GUARD_DEPTH:
        raise Blocked("the command nests too deeply to check")
    clean, bodies = shell_prepass(text)
    lexer = shlex.shlex(clean, posix=True, punctuation_chars=GUARD_PUNCT)
    lexer.whitespace_split, lexer.whitespace, lexer.commenters = True, " \t\r", ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise Blocked("cannot parse the command (%s)" % exc)
    words, heredoc, i = [], None, 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok and all(c in GUARD_PUNCT for c in tok):
            redirect = tok[0] in "<>" or tok.startswith("&>")
            if redirect and "(" not in tok and i < len(tokens) and not all(
                    c in GUARD_PUNCT for c in tokens[i]):
                target = tokens[i]
                i += 1
                if words and words[-1].isdigit():
                    words.pop()            # the fd of `2>`
                if tok.startswith("<<") and tok != "<<<":
                    heredoc = bodies.pop(0) if bodies else ""
                elif ">" in tok and not target.isdigit() and target != "-" and kit_path(
                        expand(target, ctx), ctx["cwd"]):
                    raise Blocked("writing to %s is not allowed in a job" % target)
                continue
            guard_simple(words, ctx, depth, heredoc)
            words, heredoc = [], None
            continue
        for sub in substitutions(tok):
            guard_text(sub, ctx, depth + 1)
        words.append(tok)
    guard_simple(words, ctx, depth, heredoc)
    for body in bodies:                    # a heredoc no command took: check it as commands
        guard_text(body, ctx, depth + 1)


def guard_assign(name, value):
    if name in ("GIT_PAGER", "PAGER"):
        if (value.split() or [""])[0].rsplit("/", 1)[-1] not in PAGERS_OK:
            raise Blocked("%s=%s can run a command" % (name, value))
    elif name.startswith("GIT_") and name not in GIT_ENV_OK:
        raise Blocked("setting %s is not allowed in a job (it can make git run commands "
                      "or use other config)" % name)


def take_assignments(words, ctx):
    """Leading NAME=value words, recorded and checked; the rest of the words."""
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
        name, value = words[0].split("=", 1)
        guard_assign(name, value)
        ctx["vars"][name] = expand(value, ctx)
        words = words[1:]
    return words


def launched(base, args):
    """(the command a launcher runs, its arguments come from stdin) for env,
    xargs, timeout, nice, nohup, find -exec and the like; None when `base`
    is no launcher; ([], False) when it runs nothing."""
    def after(i, value_flags=(), stop=None):
        while i < len(args) and args[i].startswith("-") and args[i] != stop:
            i += 2 if args[i] in value_flags else 1
        return args[i:]
    if base == "env":
        rest, i = [], 0
        while i < len(args):
            a = args[i]
            if a in ("-S", "--split-string"):
                return shlex.split(args[i + 1] if i + 1 < len(args) else "") + args[i + 2:], False
            if a.startswith("--split-string="):
                return shlex.split(a.split("=", 1)[1]) + args[i + 1:], False
            if a in ("-u", "--unset", "-C", "--chdir", "-P"):
                i += 2
            elif a.startswith("-"):
                i += 1
            elif re.match(r"^[A-Za-z_]\w*=", a):
                guard_assign(*a.split("=", 1))
                i += 1
            else:
                return args[i:], False
        return rest, False
    if base == "xargs":
        return after(0, {"-I", "-L", "-n", "-P", "-s", "-E", "-e", "-d", "-a", "-J", "-R",
                         "--arg-file", "--delimiter", "--max-args", "--max-lines",
                         "--max-procs", "--max-chars", "--eof", "--replace"}), True
    if base in ("timeout", "gtimeout"):
        rest = after(0, {"-s", "-k", "--signal", "--kill-after"})
        return rest[1:], False             # the duration, then the command
    if base in ("nice", "stdbuf", "caffeinate", "arch", "sudo", "doas"):
        return after(0, {"-n", "-t", "-w", "-u", "-g", "-C", "-p", "-r", "-U", "-arch"}), False
    if base in ("nohup", "builtin", "chronic", "unbuffer", "time"):
        return after(0), False
    if base == "command":
        return ([], False) if args[:1] in (["-v"], ["-V"]) else (after(0), False)
    if base == "exec":
        return after(0, {"-a"}), False
    if base == "xcrun":
        return ([], False) if args[:1] in (["-f"], ["--find"]) else (
            after(0, {"--sdk", "--toolchain"}), False)
    if base == "flock":
        rest = after(0, {"-w", "-E", "--timeout", "--conflict-exit-code"})[1:]
        return (shlex.split(rest[1]) if rest[:1] in (["-c"], ["--command"]) and len(rest) > 1
                else rest), False
    if base == "watch":
        return shlex.split(" ".join(after(0, {"-n", "--interval"}))), False
    if base == "script":
        return after(0, {"-F", "-T", "-t"})[1:], False
    return None


def find_execs(words):
    """The commands of each -exec / -execdir / -ok / -okdir in find's words."""
    out, i = [], 0
    while i < len(words):
        if words[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(words) and words[j] not in (";", "+"):
                j += 1
            out.append(words[i + 1:j])
            i = j
        i += 1
    return out


def guard_simple(words, ctx, depth, heredoc=None, from_stdin=False):
    """Check one simple command (its words, redirections removed)."""
    if depth > GUARD_DEPTH:
        raise Blocked("the command nests too deeply to check")
    words = take_assignments(words, ctx)
    while words and words[0] in SHELL_KEYWORDS:
        words = words[1:]
    if not words or words[0] in ("for", "case", "select", "function", "in"):
        return
    if words[0].startswith("-"):           # find's -exec after an escaped `\;`
        for cmd in find_execs(words):
            guard_simple(cmd, ctx, depth + 1)
        return
    cmd, args = expand(words[0], ctx).lstrip("\\"), [expand(w, ctx) for w in words[1:]]
    if cmd in ctx["aliases"]:
        return guard_simple(shlex.split(ctx["aliases"][cmd]) + args, ctx, depth + 1, heredoc)
    base = os.path.basename(cmd)
    if "$" in cmd and base != "git" and not base.startswith("git-"):
        # A command word only known at run time.
        if set(args) & (GIT_WRITES | {"git"}):
            raise Blocked("a command run through a variable with git words in it")
        return
    if base == "git":
        return guard_git(args, ctx, depth, from_stdin)
    if base.startswith("git-"):
        return guard_git([base[4:]] + args, ctx, depth, from_stdin)
    if base in ("export", "declare", "typeset", "local", "readonly"):
        take_assignments([a for a in args if not a.startswith("-")], ctx)
        return
    if base == "alias":
        for a in args:
            if "=" in a:
                name, value = a.split("=", 1)
                ctx["aliases"][name] = value
        return
    if base == "eval":
        return guard_text(" ".join(args), ctx, depth + 1)
    if base in ("source", "."):
        return guard_file(args[0], "shell", ctx, depth) if args else None
    if base == "find":
        for sub in find_execs(args):
            guard_simple(sub, ctx, depth + 1)
        return
    if base == "parallel":
        return guard_simple(args[:args.index(":::")] if ":::" in args else args,
                            ctx, depth + 1, from_stdin=":::" not in args)
    run = launched(base, args)
    if run is not None:
        return guard_simple(run[0], ctx, depth + 1, heredoc, run[1]) if run[0] else None
    if base in WRITE_CMDS or (base in ("sed", "perl") and any(
            a.startswith(("-i", "--in-place")) or (
                a.startswith("-") and not a.startswith("--") and "i" in a) for a in args)):
        for a in args:
            target = a.split("=", 1)[1] if base == "dd" and a.startswith("of=") else a
            if not target.startswith("-") and kit_path(target, ctx["cwd"]):
                raise Blocked("%s on %s is not allowed in a job" % (base, a))
    if base in SHELLS:
        return guard_shell(args, ctx, depth, heredoc)
    if PYTHON.match(base) or base in INTERPRETERS:
        return guard_interpreter(base, args, ctx, depth, heredoc)
    if "/" in cmd:
        guard_file(cmd, "exec", ctx, depth)


def guard_shell(args, ctx, depth, heredoc):
    i = 0
    while i < len(args) and args[i][:1] in "-+" and args[i] != "--":
        a = args[i]
        if a in ("-s", "--"):
            break
        if not a.startswith("--") and "c" in a[1:]:
            return guard_text(args[i + 1] if i + 1 < len(args) else "", ctx, depth + 1)
        i += 2 if (a in ("--rcfile", "--init-file") or (
            not a.startswith("--") and a[1:].strip("abefhkmnptuvxBCEHPT") == "o")) else 1
    if i < len(args) and args[i] not in ("-s", "--"):
        return guard_file(args[i], "shell", ctx, depth)
    if heredoc is not None:
        return guard_text(heredoc, ctx, depth + 1)
    raise Blocked("a shell reading its commands from stdin cannot be checked; use "
                  "sh -c '...' or a script file")


def guard_interpreter(base, args, ctx, depth, heredoc):
    code_flags, value_flags = INTERPRETERS.get(base, (("-c",), ("-W", "-X", "-Q")))
    i = 0
    while i < len(args):
        a = args[i]
        if a in code_flags:
            return guard_code(args[i + 1] if i + 1 < len(args) else "")
        short = [f for f in code_flags if len(f) == 2 and a.startswith(f) and len(a) > 2]
        if short:
            return guard_code(a[2:])
        if a == "-m" or a in ("--version", "-V", "--help", "-h"):
            return                         # a module, or nothing run: the PATH layer's job
        if a == "-":
            break
        if not a.startswith("-"):
            return guard_file(a, "code", ctx, depth)
        i += 2 if a in value_flags else 1
    if heredoc is not None:
        return guard_code(heredoc)
    raise Blocked("%s reading its code from stdin cannot be checked; use a script file "
                  "or a heredoc" % base)


def guard_code(code):
    """Code for an interpreter: blocked if it names git and a git write, or the kit."""
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9_-]*", code))
    if "git" in words and words & GIT_WRITES:
        raise Blocked("the code runs git %s" % sorted(words & GIT_WRITES)[0])
    for mark in (".claude/tools", ".claude/settings", "limit-resume"):
        if mark in code:
            raise Blocked("the code touches %s" % mark)


def guard_file(path, kind, ctx, depth):
    """Check a script before it runs: kind shell (sh file, source), code (an
    interpreter's file) or exec (run directly: by its #! line). A script
    named by the command itself must be readable; one named inside a script
    that cannot be found is skipped (it may be relative to another folder)."""
    full = os.path.realpath(os.path.join(ctx["cwd"], os.path.expanduser(expand(path, ctx))))
    if full in ctx["seen"]:
        return
    try:
        with open(full, "rb") as fh:
            data = fh.read(GUARD_FILE_MAX)
    except OSError as exc:
        if ctx["in_file"]:
            return
        raise Blocked("cannot read %s to check it (%s); write the script first, then run it "
                      "in a separate command" % (path, exc.strerror or exc))
    if b"\0" in data[:8192]:
        return                             # a compiled program: the PATH layer's job
    ctx["seen"].add(full)
    text = data.decode("utf-8", "replace")
    if kind == "exec":
        first = text.split("\n", 1)[0]
        words = first[2:].split() if first.startswith("#!") else ["sh"]
        if words and os.path.basename(words[0]) == "env":
            words = [w for w in words[1:] if not w.startswith("-")] or ["sh"]
        interp = os.path.basename(words[0]) if words else "sh"
        kind = "shell" if interp in SHELLS else "code"
    saved, ctx["in_file"] = ctx["in_file"], True
    try:
        if kind == "shell":
            guard_text(text, ctx, depth + 1)
        else:
            guard_code(text)
    finally:
        ctx["in_file"] = saved


def git_read_only(sub, args):
    """None if `git <sub> <args>` only reads, else why not."""
    pos = [a for a in args if not a.startswith("-")]
    flags = set(a.split("=", 1)[0] for a in args if a.startswith("-"))
    if sub not in GIT_READ:
        return "git %s is not a read-only command" % sub
    if sub == "branch":
        if flags & {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f",
                    "--force", "-u", "--set-upstream-to", "--unset-upstream",
                    "--edit-description", "-t", "--track", "--no-track", "--create-reflog"}:
            return "git branch changing a branch"
        values = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                  "--sort", "--format"}
        free = [a for k, a in enumerate(args) if not a.startswith("-")
                and not (k and args[k - 1] in values)]
        if free and not flags & {"-l", "--list"}:
            return "git branch %s creates a branch" % free[0]
    elif sub == "tag":
        if flags & {"-a", "-s", "-u", "-f", "-d", "-m", "-F", "-e", "--delete", "--annotate",
                    "--sign", "--local-user", "--force", "--message", "--file", "--edit"}:
            return "git tag changing a tag"
        values = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                  "--sort", "--format"}
        free = [a for k, a in enumerate(args) if not a.startswith("-")
                and not (k and args[k - 1] in values)]
        if free and not flags & {"-l", "--list", "-v", "--verify"}:
            return "git tag %s creates a tag" % free[0]
    elif sub == "remote":
        if pos and pos[0] not in ("show", "get-url"):
            return "git remote %s" % pos[0]
    elif sub == "config":
        reads = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color",
                 "--get-colorbool", "--list", "-l"}
        writes = {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section",
                  "--remove-section", "-e", "--edit"}
        file_values = {"-f", "--file", "--blob"}
        free = [a for k, a in enumerate(args) if not a.startswith("-")
                and not (k and args[k - 1] in file_values)]
        if flags & writes:
            return "git config changing config"
        if not (flags & reads or (free and free[0] in ("get", "list")) or len(free) == 1):
            return "git config changing config"
    elif sub == "stash":
        if not pos or pos[0] not in ("list", "show"):
            return "git stash creates a commit"
    elif sub == "worktree":
        if pos[:1] != ["list"]:
            return "git worktree %s" % (pos[0] if pos else "")
    elif sub == "reflog":
        if pos and pos[0] in ("expire", "delete"):
            return "git reflog %s" % pos[0]
    elif sub == "notes":
        if pos and pos[0] not in ("list", "show"):
            return "git notes %s creates a commit" % pos[0]
    elif sub == "submodule":
        if pos and pos[0] not in ("status", "summary"):
            return "git submodule %s" % pos[0]
    elif sub == "grep":
        if any(a.startswith(("-O", "--open-files-in-pager")) for a in args):
            return "git grep -O runs a command"
    return None


def guard_git(args, ctx, depth, from_stdin=False):
    aliases, i = {}, 0
    while i < len(args):
        a = args[i]
        if a in GIT_GLOBAL_VALUE:
            i += 2
        elif a == "-c":
            key, _sep, value = (args[i + 1] if i + 1 < len(args) else "").partition("=")
            key = key.lower()
            if key.startswith("alias."):
                aliases[key[6:]] = value
            elif not (key.startswith("color.") or key == "core.quotepath"):
                raise Blocked("git -c %s can make git run commands" % key)
            i += 2
        elif a.startswith(("--config-env", "--exec-path=")):
            raise Blocked("git %s is not allowed in a job" % a.split("=", 1)[0])
        elif a.split("=", 1)[0] in GIT_GLOBAL_VALUE or a in GIT_GLOBAL_FLAGS:
            i += 1
        elif a.startswith("-"):
            raise Blocked("git option %s is not known to the guard" % a)
        else:
            break
    if i >= len(args):
        if from_stdin:
            raise Blocked("git with its subcommand from stdin (xargs)")
        return
    sub, rest = args[i], args[i + 1:]
    if sub in aliases and sub not in GIT_READ | GIT_WRITES:   # git ignores those
        if depth >= GUARD_DEPTH:
            raise Blocked("git aliases nest too deeply to check")
        value = aliases[sub]
        if value.startswith("!"):
            return guard_text(value[1:] + " " + " ".join(shlex.quote(a) for a in rest),
                              ctx, depth + 1)
        return guard_git(args[:i] + shlex.split(value) + rest, ctx, depth + 1)
    why = git_read_only(sub, rest)
    if why:
        raise Blocked(why)
    for k, a in enumerate(rest):
        target = (a.split("=", 1)[1] if a.startswith("--output=")
                  else rest[k + 1] if a == "--output" and k + 1 < len(rest) else None)
        if target and kit_path(target, ctx["cwd"]):
            raise Blocked("git --output to %s is not allowed in a job" % target)


def guard_call(data):
    """None to let the tool call run, else why it is blocked. Raises on input
    it does not understand (the caller blocks then, too)."""
    if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str):
        raise ValueError("no tool_name in the hook input")
    tool, inp = data["tool_name"], data.get("tool_input")
    if inp is None:
        inp = {}
    if not isinstance(inp, dict):
        raise ValueError("tool_input is not an object")
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
    if tool == "AskUserQuestion":
        return ("nobody is watching this job to answer a dialog. Write the question in your "
                "reply and stop without JOB DONE; it is sent to the user.")
    if tool not in GUARD_READ_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            if isinstance(inp.get(key), str) and kit_path(inp[key], cwd):
                return "writing to %s is not allowed in a job" % inp[key]
    command = inp.get("command")
    if isinstance(command, str):
        try:
            guard_text(command, {"cwd": cwd, "vars": {}, "aliases": {}, "seen": set(),
                                 "in_file": False}, 0)
        except Blocked as exc:
            return str(exc)
    return None


def cmd_guard(_argv):
    """PreToolUse hook for job sessions: exit 2 (block) on a git write, an edit
    to the kit, a dialog, or anything it cannot check; 0 otherwise."""
    try:
        why = guard_call(json.load(sys.stdin))
    except Exception as exc:
        sys.stderr.write(GUARD_NO + "(The guard could not check this call: %s: %s.)\n"
                         % (type(exc).__name__, exc))
        return 2
    if why:
        sys.stderr.write(GUARD_NO + "(%s.)\n" % why)
        return 2
    return 0


def cmd_git_check(argv):
    """git-check <git arguments>: run by the job git wrapper (GIT_WRAPPER) before
    the real git. Exit 0 if git may run with these arguments - the rules the
    guard applies to a `git` in a command line (guard_git) - else 2, saying why.
    The environment is not checked: Claude Code sets GIT_EDITOR in every job
    shell, git ignores aliases that shadow its own commands, and any command a
    variable makes git run that calls git by name meets this check again."""
    try:
        guard_git(list(argv), {"cwd": os.getcwd(), "vars": {}, "aliases": {}, "seen": set(),
                               "in_file": False}, 0)
    except Blocked as exc:
        sys.stderr.write(GUARD_NO + "(%s.)\n" % exc)
        return 2
    except Exception as exc:
        sys.stderr.write(GUARD_NO + "(The guard could not check this git call: %s: %s.)\n"
                         % (type(exc).__name__, exc))
        return 2
    return 0


# ---------------------------------------------------------------------------
# Weekly maintenance
#
# Run by a Claude scheduled task (the desktop app's own scheduler), installed
# with the kit: `auto-resume-weekly-maintenance`, MAINT_AFTER_RESET after the
# account's weekly reset. Each run reads the plan usage from the app (the same
# figures as its usage card: no probe, no CLI login needed), passes them to
# `maintain --usage-json -`, which records the weekly reset, tidies the
# auto-resume and job-queue state and sends a weekly summary, and then moves
# the task to `task-spec`'s schedule when a reset from Claude moved the day.

MAINT_AFTER_RESET = 600         # s after the weekly reset
MAINT_KEEP_DAYS = 30            # state entries and job logs older than this are pruned
MAINT_STALE = WEEK + 86400      # doctor complains when maintenance is older than this
LOG_MAX = 5 * 1024 * 1024       # limit-resume.log is rotated to .log.1 past this
TASK_ID = "auto-resume-weekly-maintenance"
TASK_TITLE = "Auto-resume weekly maintenance"
TASK_DEFAULT_CRON = "10 13 * * 4"   # Thu 13:10, until this machine has seen its reset
TASK_PROMPT = """Weekly maintenance for the Claude auto-resume kit on this machine. Unattended: do not ask questions, change nothing beyond the steps below.

1. If ~/.claude/tools/limit-resume.py does not exist, reply "auto-resume kit not installed on this machine" and stop.
2. Call the mcp__ccd_session_mgmt__get_usage tool (load it with ToolSearch first if it is deferred) and take its `plan` object.
3. If plan.status is "ok", run this, with the plan object as JSON on stdin:
   python3 ~/.claude/tools/limit-resume.py maintain --scheduled --usage-json - <<'JSON'
   <the plan object>
   JSON
   Otherwise run: python3 ~/.claude/tools/limit-resume.py maintain --scheduled
   (it then reads usage itself with one Haiku turn).
4. Run: python3 ~/.claude/tools/limit-resume.py task-spec
   It prints JSON with "taskId" and "cronExpression". Call mcp__scheduled-tasks__list_scheduled_tasks; if the task with that taskId has a different cronExpression, call mcp__scheduled-tasks__update_scheduled_task with taskId and the new cronExpression (nothing else). This keeps the task 10 minutes after the account's weekly reset when a reset from Claude moves it.
5. Reply with the "Weekly maintenance" line from step 3, and whether the schedule changed (old -> new)."""


def plan_reading(plan):
    """Usage from the app's plan limits (get_usage `plan`, the usage card)."""
    reading = {}
    for win in (plan or {}).get("windows") or []:
        label = str(win.get("label") or "").lower()
        if label.startswith("5-hour") or "five" in label or "session" in label:
            name = "five_hour"           # "Current session" is the 5-hour window too
        elif label.startswith("weekly") or "7-day" in label or "seven" in label:
            name = "seven_day"           # all models and per-model: the worst one binds
        else:
            continue
        resets = to_epoch(win.get("resetsAt"))
        if resets:
            resets = round(resets / 60.0) * 60   # reported as 12:59:59.6 for a 13:00 reset
        merge_reading(reading, name, to_pct(win.get("percentUsed")), resets)
    return reading


def task_cron(resets):
    """Cron (local time) for MAINT_AFTER_RESET after a weekly reset at `resets`."""
    if not resets:
        return TASK_DEFAULT_CRON
    resets = round(resets / 60.0) * 60       # the app reports 12:59:59.6 for a 13:00 reset
    when = datetime.datetime.fromtimestamp(resets + MAINT_AFTER_RESET)
    return "%d %d * * %d" % (when.minute, when.hour, (when.weekday() + 1) % 7)


def maintain(state, now, why="weekly", plan=None):
    """Run the weekly maintenance. Returns the summary line.

    `plan` is the app's plan usage (from the scheduled task); without it, one
    Haiku probe reads usage instead."""
    cfg = state.setdefault(CONFIG, {})
    js = state.setdefault(JOBS_KEY, {})
    cfg["maint_last"] = now
    cfg["maint_why"] = why
    cfg.setdefault("maint_first", now)
    if why == "scheduled":
        cfg["maint_scheduled"] = now       # install runs must not hide a task that never runs
    notes = []

    # 1. Usage and this account's weekly reset.
    before = js.get("reset_seen")
    if plan is not None:
        reading = plan_reading(plan)
        err = None if reading else "the app's usage had no plan windows"
        source = "app"
    else:
        reading, err = probe_usage()
        js["last_probe"] = now
        source = "probe"
    if reading:
        save_usage(reading, source, now)
    usage = load_usage()
    track_reset(js, usage, now)
    resets = weekly_reset(usage, js, now)
    week = window_pct(usage, "seven_day", now)
    if err and not reading:
        js["probe_error"] = err
        notes.append("usage read failed (%s)" % err)
    else:
        js.pop("probe_error", None)
    if resets:
        moved = before and not same_schedule(before, resets)
        notes.append("weekly reset %s%s" % (
            datetime.datetime.fromtimestamp(resets).strftime("%a %d %b %H:%M"),
            " (MOVED)" if moved else ""))
    else:
        notes.append("weekly reset unknown")
    if week is not None:
        notes.append("weekly %d%% used" % week)

    # 2. Auto-resume state: drop sessions whose transcript is gone or long idle.
    cutoff = now - MAINT_KEEP_DAYS * 86400
    pruned = 0
    for sid in sessions(state):
        path = session_file(sid)
        if not path or mtime(path) < cutoff:
            state.pop(sid, None)
            pruned += 1
    if pruned:
        notes.append("%d old sessions forgotten" % pruned)

    # 3. Job queue: dead open sessions, old terminal logs, queue counts.
    js["open"] = [s for s in js.get("open", []) if job_alive(s)]
    live = {s.get("sid") for s in js.get("open", [])}
    if js.get("running"):
        live.add(js["running"].get("sid"))
    removed = 0
    for path in glob.glob(os.path.join(JOB_LOGS, "*")):
        sid = os.path.basename(path).split(".")[0]
        try:
            if sid not in live and mtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        notes.append("%d old job logs removed" % removed)
    if js.get("dir") and os.path.isdir(js["dir"]):
        counts = {}
        since = stamp(now - WEEK)
        finished = 0
        for job in job_notes(js["dir"]):
            counts[job["status"] or "-"] = counts.get(job["status"] or "-", 0) + 1
            if job["status"] == "done" and (job["meta"].get("finished") or "") >= since:
                finished += 1
        notes.append("jobs: %d done this week, %s" % (finished, ", ".join(
            "%d %s" % (n, s) for s, n in sorted(counts.items()) if s != "done") or "queue empty"))
    elif js.get("on"):
        notes.append("job folder missing: %s" % js.get("dir"))

    # 4. Log rotation. launchd's own stdout/stderr files grow too: log()
    # echoes every line, and launchd opens them afresh for each sweep.
    for path, what in ((LOG, "log"),
                       (os.path.join(TOOLS, "limit-resume.out.log"), "out.log"),
                       (os.path.join(TOOLS, "limit-resume.err.log"), "err.log")):
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
                notes.append("%s rotated" % what)
        except OSError:
            pass

    summary = "Weekly maintenance (%s): %s" % (why, "; ".join(notes))
    log(summary)
    notify(summary)
    return summary


@with_state_lock
def cmd_maintain(argv):
    """maintain [--install|--scheduled] [--usage-json <json>|-]"""
    plan = None
    if "--usage-json" in argv:
        i = argv.index("--usage-json")
        raw = argv[i + 1] if i + 1 < len(argv) else "-"
        try:
            plan = json.loads(sys.stdin.read() if raw == "-" else raw)
        except ValueError as exc:
            print("--usage-json is not JSON: %s" % exc)
            return 2
        if isinstance(plan, dict) and "plan" in plan:
            plan = plan["plan"]          # the whole get_usage result works too
    why = "install" if "--install" in argv else "scheduled" if "--scheduled" in argv else "manual"
    state = load_state()
    summary = maintain(state, time.time(), why, plan)
    save_state(state)
    print(summary)
    return 0


def cmd_task_spec(_argv):
    """The Claude scheduled task that runs the weekly maintenance, as JSON for
    mcp__scheduled-tasks__create_scheduled_task / update_scheduled_task."""
    state = load_state()
    now = time.time()
    resets = weekly_reset(load_usage(), state.get(JOBS_KEY) or {}, now)
    print(json.dumps({
        "taskId": TASK_ID, "title": TASK_TITLE, "cronExpression": task_cron(resets),
        "description": "Weekly auto-resume/backlog maintenance, 10 min after the weekly reset: "
                       "record the reset date, tidy state, weekly summary",
        "prompt": TASK_PROMPT,
        "weeklyReset": stamp(resets) if resets else None}, indent=1, ensure_ascii=False))
    return 0


def due_resumes(state, now, aged=None):
    """[(sid, path, block)] whose quota stop has reset and is not handled yet."""
    if not sessions(state) and not auto_on(state):
        return []
    out = []
    for sid, path, block in candidates(state, aged):
        if block["uuid"] in (state.get(sid) or {}).get("handled", []):
            continue
        if now < block["resets_at"] + BUFFER:
            continue
        out.append((sid, path, block))
    return out


def resume_busy(cfg, now):
    """True while the task resumed last is still working: a headless resume
    until a sweep has collected it (resume_tick); a chat continued in Remote
    Control while its transcript was written in the last RESUME_BUSY seconds."""
    act = cfg.get("active_resume")
    if not act:
        return False
    if act.get("how") == "headless":
        return True
    last = act.get("at", 0)
    path = session_file(act.get("sid", ""))
    if path:
        try:
            last = max(last, os.path.getmtime(path))
        except OSError:
            pass
    if now - last < RESUME_BUSY:
        return True
    cfg.pop("active_resume", None)
    return False


def chat_title(path):
    """The first line of the session's first request, "" if none."""
    return (first_prompt(path)[1].strip().splitlines() or [""])[0]


def typed_by_user(rec, prompt):
    """Is transcript record `rec` a turn someone typed: not a tool result, a
    meta record, or one of this tool's own prompts?"""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and not any(
            isinstance(c, dict) and c.get("type") == "text" for c in content):
        return False                       # tool results only
    text = text_of(content).strip()
    return (bool(text) and prompt not in text
            and not text.startswith(CHAT_HANDOFF.split("%")[0]))


def user_typed_since(path, uuid, prompt):
    """Was a turn typed in the transcript after record `uuid` (a stop this tool
    handled)? False when that record is not found."""
    seen = False
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if not seen:
                    seen = bool(uuid) and rec.get("uuid") == uuid
                elif typed_by_user(rec, prompt):
                    return True
    except OSError:
        pass
    return False


def revive(state):
    """Watch a retired session again once you have worked in it: it ends on a
    new quota stop, after a turn typed since the last stop it was resumed from.
    A disarmed session, or one continued in a new chat, stays retired."""
    for sid in sessions(state):
        entry = state[sid]
        if not entry.get("done") or entry.get("why") == "disarmed" or entry.get("continued_in"):
            continue
        handled = entry.get("handled") or []
        path = session_file(sid)
        if not path or not handled:
            continue
        try:
            block = find_block(path)
            back = (block is not None and block["uuid"] not in handled and user_typed_since(
                path, handled[-1], entry.get("prompt") or DEFAULT_PROMPT))
        except Exception:                  # unreadable: candidates reports it
            continue
        if back:
            entry.pop("done", None)
            entry.pop("why", None)
            entry["resumes"] = 0
            log("%s: worked in again since it was retired; watched again" % sid)


def migrate_legacy_jobs(state):
    """Entries from the pre-2026-11-18 job runner carry a `job` key. They are
    job sessions, not chats: they move to the job list, which is never resumed."""
    legacy = [sid for sid in sessions(state) if "job" in state[sid]]
    if not legacy:
        return
    js = state.setdefault(JOBS_KEY, {})
    js["sids"] = ([s for s in js.get("sids", []) if s not in legacy] + legacy)[-200:]
    for sid in legacy:
        state.pop(sid)
    log("moved %d legacy job entries to the job list: %s" % (len(legacy), ", ".join(legacy)))


def alert_aged(state, aged, now):
    """Log and alert, once per block, the blocks auto mode skipped because their
    limit reset over AUTO_MAX_AGE_H ago (a stalled sweep, a Mac asleep). Blocks
    that reset over AGE_ALERT_H ago are old history and stay quiet."""
    cfg = state.setdefault(CONFIG, {})
    told = cfg.get("age_skipped") or []
    new = [(sid, path, block) for sid, path, block in aged
           if block["uuid"] not in told and hours_since_reset(block, now) <= AGE_ALERT_H]
    if not new:
        return
    for sid, _path, block in new:
        log("%s: not resumed - its limit reset %.0f h ago, past the %d h age guard"
            % (sid, hours_since_reset(block, now), AUTO_MAX_AGE_H))
    names = ['"%s" (%s)' % (chat_title(path)[:40], sid) for sid, path, _block in new[:5]]
    notify("%d blocked chat(s) not resumed: the limit reset over %d h ago, past auto "
           "mode's age guard. `limit-resume.py arm <session-id>` resumes one: %s"
           % (len(new), AUTO_MAX_AGE_H, ", ".join(names)))
    cfg["age_skipped"] = (told + [block["uuid"] for _s, _p, block in new])[-200:]


def note_crash(state, what):
    """Record a sweep crash in the state; alert on the first of a run of them."""
    cfg = state.setdefault(CONFIG, {})
    first = not cfg.get("last_crash")
    cfg["last_crash"] = {"at": time.time(),
                         "what": (what.strip().splitlines() or ["?"])[-1][:200]}
    if first:
        notify("The 5-minute sweep crashed (%s). Quota stops may not be resumed until "
               "it is fixed; see limit-resume.log." % cfg["last_crash"]["what"])


def note_jobs_crash(state, what):
    """Record a crash of the job runner; alert on the first of a run of them."""
    js = state.setdefault(JOBS_KEY, {})
    first = not js.get("crash")
    js["crash"] = {"at": time.time(), "what": (what.strip().splitlines() or ["?"])[-1][:200]}
    if first:
        notify("The background job runner crashed (%s). A running job is not watched or "
               "capped until it is fixed; see limit-resume.log." % js["crash"]["what"])


@with_state_lock
def cmd_sweep(_argv):
    """The launchd entry point. Holds the state lock for its whole run."""
    state = load_state()
    try:
        sweep(state)
    except Exception:
        import traceback
        what = traceback.format_exc()
        log("CRASH in sweep:\n%s" % what)
        note_crash(state, what)
        save_state(state)
        return 1
    if state.get(CONFIG, {}).pop("last_crash", None):
        log("sweep ran cleanly again")
        save_state(state)
    return 0


def sweep(state):
    cfg = state.setdefault(CONFIG, {})
    gap = beat_age(cfg)
    if gap is not None and gap > SWEEP_STALE:
        # After a stall or a sleep these timers would fire at once: a busy
        # server killed as overdue, a hang "confirmed" by the gap alone.
        dropped = [k for fcfg in [cfg] + list((cfg.get("rc_more") or {}).values())
                   for k in ("rc_stale_since", "rc_hung_seen") if fcfg.pop(k, None)]
        if dropped:
            log("no sweep for %d min: Remote Control timers start afresh (%s)"
                % (gap // 60, ", ".join(dropped)))
    cfg["last_sweep"] = datetime.datetime.now().isoformat(timespec="seconds")
    cfg["last_sweep_at"] = time.time()
    try:
        rc_watchdog(state)                 # keep Remote Control alive
    except Exception:
        # A watchdog fault must not stop the heartbeat, jobs or resumes.
        import traceback
        log("rc watchdog: CRASH:\n%s" % traceback.format_exc())
    save_state(state)                      # heartbeat, proves the agent is alive
    migrate_legacy_jobs(state)
    # One task at a time: a chat resume first, then `priority: top` jobs, then
    # the rest of the backlog. A due resume waits for a running job to end.
    now = int(datetime.datetime.now().timestamp())
    resume_tick(state, now)                # the headless resume an earlier sweep started
    revive(state)
    aged = []
    due = due_resumes(state, now, aged)
    alert_aged(state, aged, now)
    busy = resume_busy(cfg, now)
    try:
        jobs_tick(state, may_start=not due and not busy)   # background job queue
        if (state.get(JOBS_KEY) or {}).pop("crash", None):
            log("jobs: ran cleanly again")
    except Exception:
        # A broken job note or log must never stop quota-stop resumes.
        import traceback
        what = traceback.format_exc()
        log("jobs: CRASH:\n%s" % what)
        note_jobs_crash(state, what)
    save_state(state)
    if not due:
        return
    run = (state.get(JOBS_KEY) or {}).get("running")
    typed = None if run else typed_finished(state.get(JOBS_KEY), now)
    if run or typed:
        if cfg.get("resume_wait") != (run or typed).get("sid"):
            cfg["resume_wait"] = (run or typed).get("sid")
            log("%d resume(s) due, waiting for %s" % (len(due), (
                "job %s to end" % run.get("title") if run else
                "you to finish in job %s (quiet for %d min)" % (typed.get("title"),
                                                                RESUME_BUSY // 60))))
            save_state(state)
        return
    cfg.pop("resume_wait", None)
    launched = 0
    for sid, path, block in due:
        if launched >= MAX_PER_SWEEP or resume_busy(cfg, now):
            break
        uuid, resets_at = block["uuid"], block["resets_at"]
        entry = state.get(sid) or {"prompt": DEFAULT_PROMPT, "resumes": 0, "handled": [],
                                   "origin": "auto"}
        handled = entry.get("handled") or []
        if (entry.get("resumes", 0) and handled
                and user_typed_since(path, handled[-1], entry["prompt"])):
            entry["resumes"] = 0           # you worked in it: a new run of resumes
        if entry.get("resumes", 0) >= MAX_RESUMES:
            log("%s hit MAX_RESUMES, marking done" % sid)
            entry["done"], entry["why"] = True, "max resumes"
            state[sid] = entry
            save_state(state)
            notify('Chat "%s" was resumed %d times in a row without you typing in it, so '
                   'it is no longer resumed automatically. Type in it and it is watched again.'
                   % (chat_title(path)[:60], MAX_RESUMES))
            continue
        # Record the attempt BEFORE launching. If this process is killed, or the
        # machine loses power mid-launch, the block stays handled and the next
        # sweep will not launch it a second time.
        entry.setdefault("handled", []).append(uuid)
        entry["resumes"] = entry.get("resumes", 0) + 1
        state[sid] = entry
        save_state(state)
        launched += 1
        log("resuming %s - stopped by %s quota, reset %s"
            % (sid, block["window"],
               datetime.datetime.fromtimestamp(resets_at).strftime("%H:%M")))
        tokens = context_tokens(path)
        if tokens > CHAT_NEW_AT:
            new = start_new_chat(sid, entry, path, tokens)
            if new:
                # The old session is retired; the new one is the running task.
                entry["done"], entry["why"], entry["continued_in"] = True, "continued", new
                cfg["active_resume"] = {"how": "new chat", "sid": new, "at": time.time()}
            else:
                notify('Limit reset, but chat "%s" is too large to resume (%dk tokens) and '
                       'a new session could not be started - open a new chat for it.'
                       % (chat_title(path)[:60], tokens // 1000))
            state[sid] = entry
            save_state(state)
            continue
        cse = rc_session(path)
        if cse:
            # Never resume a Remote Control chat headless: the turn would be
            # invisible on the phone and unknown to the live session. Post the
            # prompt into the chat's own inbox instead, so the chat runs it.
            if send_continue(sid, entry, path, cse):
                # The chat now runs the turn on its own; nothing else starts
                # until it goes quiet (resume_busy).
                cfg["active_resume"] = {"how": "relay", "sid": sid, "at": time.time()}
                save_state(state)
            else:
                notify('Limit reset, but Remote Control chat "%s" could not be continued '
                       'automatically - open it and type "continue".' % chat_title(path)[:60])
            continue
        # Nor behind a window that still has the session open (headless_owner).
        owned = headless_owner(sid, path, ps_rows())
        if owned:
            log("  %s: %s, not resumed headless" % (sid, owned[0]))
            notify(owned[1])
            continue
        act = run_resume(sid, entry, path)
        if act:
            cfg["active_resume"] = act     # collected by a later sweep
            save_state(state)


def launchd_print():
    """`launchctl print` for this agent; no `state = ` line when it is not loaded."""
    return subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)],
                          capture_output=True, text=True, timeout=20).stdout


def scheduler_status():
    """(description, ok, pended) for whichever scheduler this platform uses.

    pended: launchd is holding the agent back ("pended nondemand spawn"). That
    is how it looked, loaded all along, through the ~35 h without a sweep on
    2026-11-20/21. Only a stale sweep makes it a fault (doctor)."""
    if sys.platform == "darwin":
        unit = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
        if not os.path.exists(unit):
            return "launchd : plist MISSING - run install.sh", False, False
        try:
            info = launchd_print()
        except Exception as exc:
            return "launchd : could not query (%s)" % exc, False, False
        if "state = " not in info:
            return "launchd : NOT LOADED - run install.sh", False, False
        program = re.search(r"^\s*program = (.+)$", info, re.M)
        if program and not os.path.exists(program.group(1).strip()):
            # The python path is fixed at install time; launchd fails silently without it.
            return ("launchd : program %s MISSING - re-run install.sh"
                    % program.group(1).strip()), False, False
        runs = re.search(r"runs = (\d+)", info)
        code = re.search(r"last exit code = (.+)", info)
        pended = re.search(r"pended nondemand spawn = (.+)", info)
        return ("launchd : loaded, runs=%s last exit=%s%s"
                % (runs.group(1) if runs else "?", code.group(1) if code else "?",
                   ", pended (%s)" % pended.group(1).strip() if pended else "")), True, bool(pended)
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", LABEL + ".timer"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except FileNotFoundError:
        return "systemd : systemctl not found - install the timer manually", False, False
    except Exception as exc:
        return "systemd : could not query (%s)" % exc, False, False
    return "systemd : timer is %s" % (out or "inactive"), out == "active", False


def sweep_age(beat, now=None):
    """Seconds since the heartbeat `beat` (local ISO time), or None if there is none."""
    try:
        when = datetime.datetime.fromisoformat(beat)
    except (TypeError, ValueError):
        return None
    return ((now or datetime.datetime.now()) - when).total_seconds()


def beat_age(cfg):
    """Seconds since the last sweep heartbeat, or None if unknown. Taken from
    epoch seconds (last_sweep_at) when there are some, so a time-zone change
    cannot skew it. A heartbeat in the future (the clock stepped back) is
    unknown, never fresh."""
    at = cfg.get("last_sweep_at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        age = time.time() - at
    else:
        age = sweep_age(cfg.get("last_sweep"))
    return age if age is not None and age >= 0 else None


def kickstart_hint():
    """The command that runs one sweep now, outside the schedule."""
    if sys.platform == "darwin":
        return "launchctl kickstart gui/%d/%s" % (os.getuid(), LABEL)
    return "systemctl --user start %s.service" % LABEL


def agent_procs(rows):
    """[(pid, "sweep" | "jobpty")] for the tool's own processes among ps `rows`."""
    found = []
    for pid, _ppid, cmd in rows:
        m = AGENT_PROC.match(cmd)
        if m and pid != os.getpid():
            found.append((pid, m.group(1)))
    return found


def deadman_step(beat, age, sweeping, mark):
    """(alert text or None, new mark) for one dead-man check.

    beat: the last heartbeat, "never" if none; age: its age in s, None if
    unknown; sweeping: a sweep is running right now (a long resume, not a
    stall); mark: what the previous check left in DEADMAN. A stall must be seen
    on two checks in a row, so a sweep that is merely late after the Mac wakes
    never alerts, and it alerts once: the mark resets only with a fresh sweep."""
    if sweeping or (age is not None and age < SWEEP_STALE):
        return None, ""
    if mark == beat + " alerted":
        return None, mark
    if mark != beat:
        return None, beat
    since = ("install (no heartbeat)" if beat == "never"
             else "%s (%d min)" % (beat, age // 60) if age is not None else beat)
    return ("The 5-minute sweep has not run since %s: no resumes, jobs or Remote Control "
            "restarts until it does. Run `%s`, then `limit-resume.py doctor`."
            % (since, kickstart_hint())), beat + " alerted"


def cmd_deadman(_argv):
    """Dead-man check for the sweep, run from outside launchd (the Remote Control
    launcher, every 10 min): a sweep that never runs cannot report it itself."""
    cfg = load_state().get(CONFIG, {})
    beat = cfg.get("last_sweep") or "never"
    age = beat_age(cfg)
    sweeping = any(kind == "sweep" for _pid, kind in agent_procs(ps_rows() or []))
    try:
        with open(DEADMAN) as fh:
            mark = fh.read().strip()
    except OSError:
        mark = ""
    alert, new = deadman_step(beat, age, sweeping, mark)
    if alert and notify(alert) == "retry":
        new = mark                       # Pushover failed: alert again at the next check
    if new != mark:
        with open(DEADMAN, "w") as fh:
            fh.write(new + "\n")
    print("deadman: last sweep %s%s" % (beat, ", alerted" if alert else ""))
    return 0


def inflight(state, rows, env, me):
    """What a scheduler reload (launchctl bootout) would kill: [line for a human].

    rows: ps_rows(), None if ps failed; me: this process's pid. Headless
    resumes, job sessions and continued chats run detached, in a session of
    their own; whether bootout reaches them is unverified, so they count too."""
    if rows is None:
        return ["cannot list processes (ps failed), so cannot tell what is running"]
    found = []
    if env.get("LIMIT_RESUME_JOB"):
        found.append("this shell runs inside a background job session (LIMIT_RESUME_JOB is set)")
    parent = {pid: ppid for pid, ppid, _cmd in rows}
    mine, pid = set(), me
    while pid in parent and pid not in mine and pid > 1:
        mine.add(pid)
        pid = parent[pid]
    act = (state.get(CONFIG) or {}).get("active_resume") or {}
    if act.get("how") == "headless" and job_alive(act):
        found.append("a headless resume of session %s, pid %d" % (act.get("sid"), act["pid"]))
    js = state.get(JOBS_KEY) or {}
    titles = {run.get("pid"): run.get("title")
              for run in [js.get("running") or {}] + js.get("open", [])}
    for pid, kind in agent_procs(rows):
        what = ("a sweep (and any resume it runs)" if kind == "sweep"
                else 'job session "%s"' % titles[pid] if titles.get(pid)
                else "a job or continued-chat session")
        found.append("%s, pid %d%s" % (what, pid, " - this command runs inside it"
                                       if pid in mine else ""))
    return found


def cmd_reload_check(_argv):
    """Exit 1, listing it, if a scheduler reload would kill work in flight."""
    found = inflight(load_state(), ps_rows(), os.environ, os.getpid())
    for line in found:
        print("in flight: %s" % line)
    if not found:
        print("nothing in flight")
    return 1 if found else 0


def job_health(js, now):
    """[(line, needs attention)] about the job runner, for doctor."""
    out = []
    crash = js.get("crash")
    if crash:
        out.append(("job runner CRASHED %s: %s - see limit-resume.log"
                    % (stamp(crash.get("at", 0)), crash.get("what", "?")), True))
    if js.get("probe_error"):
        out.append(("usage probe failing: %s" % js["probe_error"], True))
    run = js.get("running")
    if run:
        if run.get("pid") and not job_alive(run):
            out.append(("running job's session is gone (pid %s); the next sweep pauses it"
                        % run.get("pid"), True))
        if now - run.get("started_at", now) > JOB_MAX_H * 3600 + SWEEP_STALE:
            out.append(("running job started %s, past JOB_MAX_H (%d h): is the sweep running?"
                        % (stamp(run.get("started_at", now)), JOB_MAX_H), True))
    folder = js.get("dir")
    waiting = 0
    if js.get("on") and folder and os.path.isdir(folder):
        waiting = sum(j["status"] in ("queued", "paused", "limit") for j in job_notes(folder))
    usage = load_usage()
    age = usage_age(usage, now)
    if waiting and age > 1800 and not closed_anyway(usage, now, js):
        out.append(("usage reading %s while %d job(s) wait; jobs stay shut until it is "
                    "fresh (`jobs probe`)" % ("missing" if age > WEEK
                                              else "%d min old" % (age // 60), waiting), True))
    typed = typed_finished(js, now)
    for sess in js.get("open", []):
        out.append(("open session: %s (%s, session %s)%s" % (
            sess.get("title"), sess.get("status"), sess.get("sid"),
            " - you are working in it, the queue waits" if sess is typed else ""), False))
    return out


def rc_health(cfg, folder, serving):
    """(doctor's line, healthy) for one Remote Control folder. cfg: its watchdog
    state (rc_state); serving: rc_serving() for it."""
    who, pid = serving
    up = who is not None
    hung = cfg.get("rc_hung_seen")
    if who == "by hand":
        why = "running (a server started by hand serves it, pid %d; not restarted)" % pid
    elif who == "unknown":
        why = "running? (ps or lsof cannot tell; never started blind)"
    elif up and hung:
        why = "SUSPECTED HUNG - silent since %s, restarted if still silent" % stamp(hung[1])
    elif up and cfg.get("rc_stale_wait"):
        why = "running, restarts on %s once its sessions are idle" % cfg["rc_stale_wait"]
    elif up:
        why = "running"
    elif rc_login_dead(cfg, folder):
        why = "DOWN - login expired, run `claude /login`"
    elif cfg.get("rc_tries", 0) >= RC_MAX_TRIES:
        why = "DOWN - %d restarts failed, standing down (`rc on` to retry)" % RC_MAX_TRIES
    elif cfg.get("rc_net_down"):
        why = "DOWN - waiting for the network"
    else:
        why = "MISSING - will restart"
    if folder["dir"]:
        why += " - %s (%s)" % (folder["dir"], folder["name"])
    return why, up and not hung and who != "unknown"


def cmd_doctor(_argv):
    """Health check: is the watchdog actually installed, loaded and running?"""
    ok = os.path.exists(__file__)
    print("script     : %s" % (__file__ if ok else "MISSING"))
    print("version    : %s" % VERSION)
    claude = shutil.which("claude", path=os.environ.get("PATH", "") + ":" + CLAUDE_PATH_HINT)
    print("claude bin : %s" % (claude or "NOT FOUND - resumes will fail"))
    ok = ok and bool(claude)
    desc, sched_ok, pended = scheduler_status()
    print("%s" % desc)
    ok = ok and sched_ok
    try:
        read_json(STATE)
        unreadable = False
    except FileNotFoundError:
        unreadable = False
    except (OSError, ValueError):
        unreadable = True
    corrupt = sorted(glob.glob(STATE + ".corrupt-*"))
    if unreadable:
        print("state      : UNREADABLE - shown from the last good copy (.bak); the next "
              "sweep moves it aside")
    elif corrupt:
        print("state      : was unreadable, kept as %s - check it, then delete it" % corrupt[-1])
    ok = ok and not unreadable and not corrupt
    state = load_state()
    beat = state.get(CONFIG, {}).get("last_sweep")
    age = beat_age(state.get(CONFIG, {}))
    stale = age is None or age >= SWEEP_STALE
    if age is not None:
        print("last sweep : %s (%dm ago) %s"
              % (beat, age // 60, "STALE - agent may be stopped" if stale else "OK"))
    elif beat:
        print("last sweep : %s UNREADABLE or in the future (clock moved back?)" % beat)
    else:
        print("last sweep : NEVER - agent has not swept since install")
    ok = ok and not stale
    if stale and pended:
        # Loaded, yet launchd starts nothing: re-running install.sh did not help on 2026-11-21.
        print("held       : launchd is holding the agent (pended nondemand spawn)")
    if stale:
        print("             run now: %s ; if it stays stale, see the runbook, "
              '"last sweep : NEVER or stale"' % kickstart_hint())
    cfg = state.get(CONFIG, {})
    crash = cfg.get("last_crash")
    if crash:
        print("last crash : %s %s - no clean sweep since; see limit-resume.log"
              % (stamp(crash.get("at", 0)), crash.get("what", "?")))
        ok = False
    act = cfg.get("active_resume")
    if act:
        print("active task: %s of %s, %d min%s" % (
            {"headless": "headless resume", "relay": "Remote Control chat continuing",
             "new chat": "new Remote Control chat"}.get(act.get("how"), "chat continuing"),
            act.get("sid"), (time.time() - act.get("at", time.time())) // 60,
            ", pid %s" % act.get("pid") if act.get("pid") else ""))
    if cfg.get("rc_watch"):
        rows = ps_rows()
        for folder in rc_folders():
            line, healthy = rc_health(rc_state(cfg, folder), folder, rc_serving(rows, folder))
            print("remote ctl : watched, %s" % line)
            ok = ok and healthy
        print("alerts     : %s" % ("Pushover" if keychain("token") and keychain("user")
                                   else "this screen only (no Pushover keys in the Keychain)"))
    else:
        print("remote ctl : not watched (`rc on` to enable)")
    by_auto = sum(state[sid].get("origin") == "auto" for sid in sessions(state))
    print("auto mode  : %s / armed: %d explicit, %d by auto mode"
          % (auto_on(state), len(sessions(state)) - by_auto, by_auto))
    last = cfg.get("maint_last")
    if last:
        # Install runs count as maintenance too, so staleness follows the
        # scheduled task alone (or the first run, until it has run once).
        sched = cfg.get("maint_scheduled")
        stale = time.time() - (sched or cfg.get("maint_first") or last) > MAINT_STALE
        print("maintenance: %s (%s, %dd ago); scheduled task: %s%s" % (
            stamp(last), cfg.get("maint_why", "?"), (time.time() - last) // 86400,
            "last ran %s" % stamp(sched) if sched else "never ran",
            " STALE - is the '%s' scheduled task installed and the app open?" % TASK_ID
            if stale else ""))
        ok = ok and not stale
    else:
        print("maintenance: NEVER - create the Claude scheduled task from `task-spec`")
    js = state.get(JOBS_KEY) or {}
    if js.get("on") or js.get("running") or js.get("open"):
        run = js.get("running")
        folder_ok = bool(js.get("dir")) and os.path.isdir(js["dir"])
        print("jobs       : %s, %s%s" % (
            "on" if js.get("on") else "off",
            js.get("dir") if folder_ok else "FOLDER MISSING %s" % js.get("dir"),
            ", running: %s" % run.get("title") if run else ""))
        ok = ok and (folder_ok or not js.get("on"))
        for line, bad in job_health(js, time.time()):
            print("             %s" % line)
            ok = ok and not bad
    else:
        print("jobs       : off (`jobs on <folder>` to enable)")
    print("\nhealthy" if ok else "\nNEEDS ATTENTION")
    return 0 if ok else 1


COMMANDS = {"arm": cmd_arm, "auto": cmd_auto, "sweep": cmd_sweep,
            "status": cmd_status, "disarm": cmd_disarm, "doctor": cmd_doctor,
            "rc": cmd_rc, "notify": cmd_notify, "jobs": cmd_jobs,
            "statusline": cmd_statusline, "guard": cmd_guard, "jobpty": cmd_jobpty,
            "maintain": cmd_maintain, "task-spec": cmd_task_spec,
            "deadman": cmd_deadman, "reload-check": cmd_reload_check,
            "git-check": cmd_git_check}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(COMMANDS[cmd](sys.argv[2:]))
    except SystemExit:
        raise
    except Exception:
        # Never die silently under launchd: the traceback goes to the log so
        # `doctor` and the log tail can show what broke.
        import traceback
        log("CRASH in %s:\n%s" % (cmd, traceback.format_exc()))
        sys.exit(1)
