#!/usr/bin/env python3
"""Status lights for running Claude Code sessions.

Claude Code keeps one record per running session in ~/.claude/sessions/<pid>.json
with a live "status" (busy | waiting | idle), a name, the cwd and the tmux pane.
This turns those into lights for the bar:

  busy    -> "busy"     (working)
  waiting -> "waiting"  (needs you)
  idle    -> "done"     until you have visited it, then hidden

A finished session counts as visited once you click its light ("jump") or a scan
finds its pane is the active pane of an attached tmux client. Visits are kept in
~/.local/state/agent-lights/seen.json, keyed "pid:statusUpdatedAt", so a session
that works and finishes again lights up again.

Sessions running a plan (lean-plan/lean-execute or superpowers) also get plan
progress (done/total per task), and running subagents are listed per session.

  agent_lights.py scan          print the lights as one JSON array
  agent_lights.py jump <pid>    switch tmux to that session's pane and focus its terminal

Python 3 stdlib only. Reads undocumented Claude Code internals (tested with
Claude Code 2.1.325), so an update can change the formats.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
SESSIONS_DIR = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude")), "sessions")
PROJECTS_DIR = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude")), "projects")
STATE_FILE = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local/state")), "agent-lights/seen.json")
IDLE_MAX_MS = 12 * 3600 * 1000
ORDER = {"waiting": 0, "done": 1, "busy": 2}
LIGHTS = {"busy": "busy", "waiting": "waiting", "idle": "done"}
CLIENT_FMT = "#{client_name}\t#{session_name}\t#{pane_id}\t#{client_activity}\t#{client_pid}"


def run(cmd):
    """Run a command, return stdout ('' on any failure). Replaced in tests."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# --- Platform: the only Linux-specific code -----------------------------------
# Everything else (Claude Code's files, tmux) is the same on macOS. To port,
# replace these four functions:
#   proc_start      /proc/<pid>/stat start time  -> e.g. `ps -o lstart= -p <pid>`
#                   (check what Claude Code writes as "procStart" there first)
#   is_alive        uses proc_start; kill(pid, 0) is a weaker fallback
#   focus_terminal  Hyprland (hyprctl)            -> activate the terminal app
#   notify          notify-send                    -> osascript "display notification"

def proc_start(pid):
    """Start time (field 22 of /proc/<pid>/stat) or None if the process is gone."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def is_alive(rec):
    """The session's process is running and is the same process (pid not reused)."""
    return rec.get("procStart") is not None and proc_start(rec.get("pid")) == str(rec.get("procStart"))


def window_pid_for(pid):
    """Walk parents of `pid` to the first one that owns a Hyprland window."""
    try:
        windows = {c.get("pid") for c in json.loads(run(["hyprctl", "clients", "-j"]) or "[]")}
    except ValueError:
        return None
    while pid and pid > 1:
        if pid in windows:
            return pid
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    return None


def focus_terminal(client_pid):
    """Raise the terminal window that hosts the tmux client with this pid."""
    wpid = window_pid_for(client_pid)
    if wpid:
        run(["hyprctl", "dispatch", "focuswindow", f"pid:{wpid}"])


def notify(title, body):
    run(["notify-send", title, body])


# --- Sessions ------------------------------------------------------------------

def load_sessions(sessions_dir, alive=None):
    alive = alive or is_alive
    out = []
    try:
        names = sorted(os.listdir(sessions_dir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, name)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("status") in LIGHTS and alive(rec):
            out.append(rec)
    return out


def pane_of(rec):
    """'17:@22.%39' -> ('17', '%39')."""
    t = rec.get("tmux") or ""
    if ":" not in t or "." not in t:
        return None, None
    return t.split(":", 1)[0], t.rsplit(".", 1)[1]


def tmux_clients():
    clients = []
    for line in run(["tmux", "list-clients", "-F", CLIENT_FMT]).splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            name, session, pane, activity, pid = parts
            clients.append({"name": name, "session": session, "pane": pane,
                            "activity": int(activity or 0), "pid": int(pid or 0)})
    return clients


def seen_key(rec):
    return f"{rec.get('pid')}:{rec.get('statusUpdatedAt')}"


def load_seen(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_seen(path, seen):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(seen), f)
        os.replace(tmp, path)
    except OSError:
        pass


def scan(sessions, seen, active_panes, now_ms):
    """Pure: sessions + seen keys + active tmux panes -> (lights, new seen set)."""
    lights, keep = [], set()
    live_pids = {str(r.get("pid")) for r in sessions}
    keep = {k for k in seen if k.split(":", 1)[0] in live_pids}
    for rec in sessions:
        light = LIGHTS[rec["status"]]
        since = int(rec.get("statusUpdatedAt") or rec.get("updatedAt") or now_ms)
        if light == "done":
            if pane_of(rec)[1] in active_panes:
                keep.add(seen_key(rec))
            if seen_key(rec) in keep or now_ms - since > IDLE_MAX_MS:
                continue
        lights.append({"pid": rec.get("pid"), "name": rec.get("name") or "", "cwd": rec.get("cwd") or "",
                       "tmux": rec.get("tmux") or "", "light": light, "since": since})
    lights.sort(key=lambda l: (ORDER[l["light"]], -l["since"]))
    return lights, keep


# --- Plan progress and subagents --------------------------------------------
# A session running a lean-execute plan gets a progress ring: the plan is the
# last existing "<YYYY-MM-DD-name>/log.md" named on a plan-log line of its
# transcript, total = "### N."
# headings in the plan.md beside that log, done = distinct "progress · Task N
# done" lines. Running subagents come from the session's subagents/*.meta.json.

PATH_TAIL = re.compile(r'[\w~./-]*$')  # the path token that ends right before a slug
SLUG = re.compile(r'(?<![\w-])\d{4}-\d{2}-\d{2}-[A-Za-z0-9][A-Za-z0-9_-]*')  # plan folder, e.g. 2026-11-15-agent-rings
TASK_HEADING = re.compile(r'^### (?:Task )?(\d+)[.:]', re.M)  # lean ("### 1.") or superpowers ("### Task 1:")
# Anchored to whole plan-log lines ("- 2026-11-15 16:17 · progress · ..."), so a
# decision that merely quotes these phrases never counts.
LOG_LINE = r'^- [\d-]+ [\d:]+ · progress · '
TASK_DONE = re.compile(LOG_LINE + r'Task (\d+) done', re.M)
PLAN_DONE = re.compile(LOG_LINE + r'Plan complete', re.M)
LEDGER_DONE = re.compile(r'^Task (\d+): complete', re.M)  # superpowers SDD progress.md
PLAN_STALE_S = 24 * 3600
SUBAGENT_STALE_S = 30 * 60


def find_transcript(sid, projects_dir, entry):
    path = entry.get("transcript")
    if path and os.path.exists(path):
        return path
    for d in sorted(glob.glob(os.path.join(glob.escape(projects_dir), "*", ""))):
        p = os.path.join(d, sid + ".jsonl")
        if os.path.exists(p):
            entry.update(transcript=p, offset=0, mention=None)
            return p
    return None


def plan_log_calls(line):
    """Inputs of the tool calls on one transcript line that run plan-log. Only
    the session's own calls count: tool output and chat that merely mention
    another plan (a cat of its log, a ledger saying "use plan-log") don't."""
    if "plan-log" not in line or '"tool_use"' not in line:
        return []
    try:
        rec = json.loads(line)
        content = rec["message"]["content"] if rec.get("type") == "assistant" else []
    except (ValueError, KeyError, TypeError):
        return []
    calls = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            text = json.dumps(item.get("input"), ensure_ascii=False)
            if "plan-log" in text:
                calls.append(text)
    return calls


def mentions(text):
    """(written path, slug) for each plan folder named in a plan-log tool call,
    oldest first. The slug is enough: commands often build the log path from a
    variable (D=docs/plans/<slug>; plan-log $D/log.md ...)."""
    out = []
    for line in text.splitlines():
        for call in plan_log_calls(line):
            for m in SLUG.finditer(call):
                prefix = PATH_TAIL.search(call, max(0, m.start() - 400), m.start()).group(0)
                out.append((prefix + m.group(0) + "/log.md", m.group(0)))
    return out


def last_mention(path, entry, cwd):
    """Read the transcript from the cached offset; remember the last mention of
    a log that exists (a stray path in a command must not hide the real plan)."""
    try:
        size = os.path.getsize(path)
        offset = entry.get("offset", 0)
        if size < offset:
            offset, entry["mention"] = 0, None
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return entry.get("mention")
    end = chunk.rfind(b"\n") + 1  # never split a line across reads
    for written, slug in reversed(list(mentions(chunk[:end].decode("utf-8", "replace")))):
        log_path = resolve_log(written, slug, cwd)
        if log_path:
            entry["mention"] = log_path
            break
    entry["offset"] = offset + end
    return entry.get("mention")


_WORKTREES = {}


def worktrees(cwd):
    """Git worktrees of the repo at cwd (plans often run in a sibling worktree)."""
    if cwd not in _WORKTREES:
        out = run(["git", "-C", cwd, "worktree", "list", "--porcelain"])
        _WORKTREES[cwd] = [l[9:] for l in out.splitlines() if l.startswith("worktree ")]
    return _WORKTREES[cwd]


def resolve_log(written, slug, cwd):
    rel = os.path.join("docs/plans", slug, "log.md")
    candidates = [os.path.expanduser(written), os.path.join(cwd, written), os.path.join(cwd, rel)]
    candidates += sorted(glob.glob(os.path.join(glob.escape(cwd), "*", rel)))
    candidates += [os.path.join(w, rel) for w in worktrees(cwd)]
    obsidian = os.environ.get("OBSIDIAN_PLANS_DIR")
    if obsidian:
        candidates += sorted(glob.glob(os.path.join(glob.escape(obsidian), "*", slug, "log.md")))
    for c in candidates:
        if os.path.isabs(c) and os.path.isfile(c):
            return c
    return None


def sdd_ledger(plan_path):
    """The superpowers subagent-driven-development ledger for this plan, if any:
    <repo>/.superpowers/sdd/*/progress.md whose first line names the plan. Those
    runs record "Task N: complete" there rather than in log.md."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(plan_path))))
    for p in sorted(glob.glob(os.path.join(glob.escape(repo), ".superpowers", "sdd", "*", "progress.md"))):
        try:
            with open(p) as f:
                if plan_path in f.readline():
                    return p
        except OSError:
            continue
    return None


def progress_of(log_path, now_s):
    """{"name", "total", "done", "states"} where states[i] is "done", "current"
    (the lowest unfinished task) or "todo"; None when there is nothing to show."""
    plan_path = os.path.join(os.path.dirname(log_path), "plan.md")
    ledger_path = sdd_ledger(plan_path)
    try:
        with open(log_path) as f:
            log = f.read()
        with open(plan_path) as f:
            numbers = sorted({int(n) for n in TASK_HEADING.findall(f.read())})
        ledger, mtime = "", os.path.getmtime(log_path)
        if ledger_path:
            with open(ledger_path) as f:
                ledger = f.read()
            mtime = max(mtime, os.path.getmtime(ledger_path))
    except OSError:
        return None
    if not numbers or PLAN_DONE.search(log) or now_s - mtime > PLAN_STALE_S:
        return None
    done = {int(n) for n in TASK_DONE.findall(log)} | {int(n) for n in LEDGER_DONE.findall(ledger)}
    current = next((n for n in numbers if n not in done), None)
    states = ["done" if n in done else "current" if n == current else "todo" for n in numbers]
    return {"name": os.path.basename(os.path.dirname(log_path))[11:], "total": len(numbers),
            "done": states.count("done"), "states": states}


def plan_progress(rec, cache, projects_dir, now_s):
    sid = rec.get("sessionId")
    if not sid:
        return None
    entry = cache.setdefault(sid, {})
    path = find_transcript(sid, projects_dir, entry)
    log_path = last_mention(path, entry, rec.get("cwd") or HOME) if path else None
    return progress_of(log_path, now_s) if log_path and os.path.isfile(log_path) else None


def running_subagents(rec, cache, now_s):
    path = cache.get(rec.get("sessionId"), {}).get("transcript")
    if not path:
        return []
    out = []
    for meta_path in sorted(glob.glob(os.path.join(glob.escape(path[:-6]), "subagents", "*.meta.json"))):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            log = meta_path[:-len(".meta.json")] + ".jsonl"
            mtime = os.path.getmtime(log if os.path.exists(log) else meta_path)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and not meta.get("finished") and now_s - mtime <= SUBAGENT_STALE_S:
            out.append(meta.get("description") or meta.get("agentType") or "subagent")
    return out


def enrich(lights, sessions, cache, projects_dir, now_s):
    """Attach plan progress and running subagents to each light (cache updated in place)."""
    by_pid = {r.get("pid"): r for r in sessions}
    for light in lights:
        rec = by_pid.get(light["pid"], {})
        light["plan"] = plan_progress(rec, cache, projects_dir, now_s)
        light["subagents"] = running_subagents(rec, cache, now_s)
    live = {r.get("sessionId") for r in sessions}
    for sid in [s for s in cache if s not in live]:
        del cache[sid]
    return lights


def load_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def cmd_scan(args):
    seen = load_seen(args.state)
    active = {c["pane"] for c in tmux_clients()}
    now_ms = args.now or int(time.time() * 1000)
    sessions = load_sessions(args.sessions)
    lights, new_seen = scan(sessions, seen, active, now_ms)
    if new_seen != seen:
        save_seen(args.state, new_seen)
    cache_path = os.path.join(os.path.dirname(args.state), "cache.json")
    cache = load_json(cache_path, {})
    before = json.dumps(cache, sort_keys=True)
    enrich(lights, sessions, cache, args.projects, now_ms / 1000)
    if json.dumps(cache, sort_keys=True) != before:
        save_json(cache_path, cache)
    print(json.dumps(lights))


def pick_client(clients, session):
    attached = [c for c in clients if c["session"] == session]
    pool = attached or clients
    return max(pool, key=lambda c: c["activity"]) if pool else None


def cmd_jump(args):
    rec = next((r for r in load_sessions(args.sessions) if str(r.get("pid")) == str(args.pid)), None)
    if rec is None:
        return 1
    session, pane = pane_of(rec)
    client = pick_client(tmux_clients(), session) if pane else None
    if client is None:
        target = f"tmux attach -t {session}" if session else rec.get("cwd", "")
        notify("Agent lights", f"{rec.get('name', 'Session')}: {target}")
        return 0
    run(["tmux", "switch-client", "-c", client["name"], "-t", pane])
    run(["tmux", "select-window", "-t", pane])
    run(["tmux", "select-pane", "-t", pane])
    focus_terminal(client["pid"])
    if rec.get("status") == "idle":
        seen = load_seen(args.state)
        seen.add(seen_key(rec))
        save_seen(args.state, seen)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent-lights")
    p.add_argument("--sessions", default=SESSIONS_DIR)
    p.add_argument("--state", default=STATE_FILE)
    p.add_argument("--projects", default=PROJECTS_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--now", type=int, default=0)
    j = sub.add_parser("jump")
    j.add_argument("pid")
    args = p.parse_args(argv)
    return cmd_scan(args) if args.cmd == "scan" else cmd_jump(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
