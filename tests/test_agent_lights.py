# Unit tests for core/agent_lights.py. Run: python3 -m unittest tests/test_agent_lights.py
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "../core/agent_lights.py")
FIX = os.path.join(HERE, "fixtures/sessions")
_loader = importlib.machinery.SourceFileLoader("agent_lights", SRC)
_spec = importlib.util.spec_from_loader("agent_lights", _loader)
al = importlib.util.module_from_spec(_spec)
_loader.exec_module(al)

NOW = 1794775200000  # fixtures' statusUpdatedAt values sit a few minutes before this
LIVE = {"100", "200", "300", "400"}  # pid 500 is "dead", pid 600 has a reused pid


def alive(rec):
    return str(rec.get("pid")) in LIVE and rec.get("procStart") == "1"


def sessions():
    return al.load_sessions(FIX, alive=alive)


class AgentLights(unittest.TestCase):
    def test_status_maps_to_light(self):
        lights, _ = al.scan(sessions(), set(), set(), NOW)
        by_pid = {l["pid"]: l["light"] for l in lights}
        self.assertEqual(by_pid, {100: "busy", 200: "waiting", 300: "done"})

    def test_waiting_first_then_done_then_busy(self):
        lights, _ = al.scan(sessions(), set(), set(), NOW)
        self.assertEqual([l["light"] for l in lights], ["waiting", "done", "busy"])

    def test_dead_and_reused_pids_dropped_broken_json_ignored(self):
        pids = {r["pid"] for r in sessions()}
        self.assertNotIn(500, pids)
        self.assertNotIn(600, pids)
        self.assertEqual(pids, {100, 200, 300, 400})

    def test_idle_with_active_pane_becomes_seen_and_hidden(self):
        lights, seen = al.scan(sessions(), set(), {"%3"}, NOW)
        self.assertNotIn(300, [l["pid"] for l in lights])
        self.assertIn("300:1794775000000", seen)
        lights, _ = al.scan(sessions(), seen, set(), NOW)
        self.assertNotIn(300, [l["pid"] for l in lights])

    def test_active_pane_does_not_hide_busy_or_waiting(self):
        lights, seen = al.scan(sessions(), set(), {"%1", "%2"}, NOW)
        self.assertEqual({l["pid"] for l in lights}, {100, 200, 300})
        self.assertEqual(seen, set())

    def test_idle_older_than_12h_hidden(self):
        # pid 400 went idle 13 h before NOW
        lights, _ = al.scan(sessions(), set(), set(), NOW)
        self.assertNotIn(400, [l["pid"] for l in lights])

    def test_new_status_time_relights_after_seen(self):
        seen = {"300:1794775000000"}
        recs = sessions()
        for r in recs:
            if r["pid"] == 300:
                r["statusUpdatedAt"] = 1794775100000  # worked and finished again
        lights, _ = al.scan(recs, seen, set(), NOW)
        self.assertIn(300, [l["pid"] for l in lights])

    def test_idle_with_running_shell_is_busy(self):
        # pid 300 is idle but has a background shell; pid 400 too, despite going idle 13 h ago
        lights, seen = al.scan(sessions(), set(), {"%3"}, NOW, shells={300, 400})
        by_pid = {l["pid"]: l["light"] for l in lights}
        self.assertEqual(by_pid, {100: "busy", 200: "waiting", 300: "busy", 400: "busy"})
        self.assertEqual(seen, set())  # a yellow light is never marked visited

    def test_running_shell_does_not_hide_waiting(self):
        lights, _ = al.scan(sessions(), set(), set(), NOW, shells={200})
        self.assertEqual({l["pid"]: l["light"] for l in lights}[200], "waiting")

    def test_shell_pids_finds_claude_shells_only(self):
        with tempfile.TemporaryDirectory() as proc:
            def mk(pid, cmd, children=()):
                os.makedirs(os.path.join(proc, str(pid), "task", str(pid)))
                with open(os.path.join(proc, str(pid), "cmdline"), "w") as f:
                    f.write("\0".join(cmd))
                with open(os.path.join(proc, str(pid), "task", str(pid), "children"), "w") as f:
                    f.write(" ".join(map(str, children)) + " ")
            mk(10, ["claude"], [11, 12])
            mk(11, ["/usr/bin/zsh", "-c", "source /home/u/.claude/shell-snapshots/snapshot-zsh-1.sh && sleep 600"])
            mk(12, ["npm", "exec", "some-mcp"])
            mk(20, ["claude"], [21])
            mk(21, ["sh", "-c", "statusline.sh"])
            self.assertEqual(al.shell_pids([10, 20, 30], proc=proc), {10})

    def test_shell_and_unknown_statuses_are_busy(self):
        with tempfile.TemporaryDirectory() as d:
            for pid, status in ((100, "shell"), (200, "some_new_state"), (300, None)):
                with open(os.path.join(d, f"{pid}.json"), "w") as f:
                    json.dump({"pid": pid, "procStart": "1", "status": status, "statusUpdatedAt": NOW}, f)
            lights, _ = al.scan(al.load_sessions(d, alive=alive), set(), set(), NOW)
        self.assertEqual({l["pid"]: l["light"] for l in lights}, {100: "busy", 200: "busy"})

    def test_seen_pruned_for_dead_pids(self):
        _, seen = al.scan(sessions(), {"999:1", "300:1794775000000"}, set(), NOW)
        self.assertEqual(seen, {"300:1794775000000"})

    def test_missing_dir_and_bad_state_are_empty(self):
        self.assertEqual(al.load_sessions("/nonexistent/agent-lights"), [])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        try:
            self.assertEqual(al.load_seen(f.name), set())
        finally:
            os.unlink(f.name)

    def test_pane_of(self):
        self.assertEqual(al.pane_of({"tmux": "17:@22.%39"}), ("17", "%39"))
        self.assertEqual(al.pane_of({}), (None, None))

    def test_pick_client_prefers_attached_session_then_most_recent(self):
        clients = [{"name": "a", "session": "1", "activity": 5}, {"name": "b", "session": "2", "activity": 9}]
        self.assertEqual(al.pick_client(clients, "1")["name"], "a")
        self.assertEqual(al.pick_client(clients, "7")["name"], "b")
        self.assertIsNone(al.pick_client([], "1"))

    def test_jump_switches_client_and_marks_idle_seen(self):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            if cmd[:2] == ["tmux", "list-clients"]:
                return "/dev/pts/0\t4\t%9\t10\t1\n"
            return ""
        orig_run, orig_alive = al.run, al.is_alive
        al.run, al.is_alive = fake_run, alive
        try:
            with tempfile.TemporaryDirectory() as d:
                state = os.path.join(d, "seen.json")
                al.main(["--sessions", FIX, "--state", state, "jump", "300"])
                with open(state) as f:
                    self.assertEqual(json.load(f), ["300:1794775000000"])
        finally:
            al.run, al.is_alive = orig_run, orig_alive
        self.assertIn(["tmux", "switch-client", "-c", "/dev/pts/0", "-t", "%3"], calls)


def tool_use(command):
    """One assistant transcript line running a Bash command."""
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}) + "\n"


def tool_result(text):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": text}]}}) + "\n"


class PlanProgress(unittest.TestCase):
    """Plan rings and subagents, against a temp HOME-like tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.now = 2_000_000_000.0
        self.cwd = os.path.join(self.root, "ws")
        self.repo = os.path.join(self.cwd, "repo")  # repo in a subdir of the session cwd
        self.projects = os.path.join(self.root, "projects")
        os.makedirs(os.path.join(self.projects, "-ws"))
        self.transcript = os.path.join(self.projects, "-ws", "sid1.jsonl")
        self.rec = {"pid": 1, "sessionId": "sid1", "cwd": self.cwd}
        self.cache = {}
        al._WORKTREES.clear()
        self.orig_run = al.run
        al.run = lambda cmd: ""  # no git worktrees unless a test says so

    def tearDown(self):
        al.run = self.orig_run
        self.tmp.cleanup()

    def plan(self, slug, headings, log_lines=(), base=None, age_s=60):
        d = os.path.join(base or self.repo, "docs/plans", slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "plan.md"), "w") as f:
            f.write("# Plan\n\n" + "\n".join(headings) + "\n")
        log = os.path.join(d, "log.md")
        with open(log, "w") as f:
            f.write("# Log\n\n" + "".join(l + "\n" for l in log_lines))
        os.utime(log, (self.now - age_s, self.now - age_s))
        return log

    def say(self, *lines):
        with open(self.transcript, "a") as f:
            f.write("".join(lines))

    def progress(self):
        return al.plan_progress(self.rec, self.cache, self.projects, self.now)

    def test_counts_lean_plan_via_variable_path_in_repo_subdir(self):
        self.plan("2026-01-02-alpha", ["### 1. a", "### 2. b", "### 3. c"],
                  ["- 2026-01-02 10:00 · progress · Task 1 done · x", "- 2026-01-02 10:00 · progress · Task 1 done · again",
                   "- 2026-01-02 10:00 · progress · Task 3 done (Live) · y"])
        self.say(tool_use("cd repo; D=docs/plans/2026-01-02-alpha; scripts/plan-log $D/log.md progress x"))
        p = self.progress()
        self.assertEqual((p["name"], p["done"], p["total"]), ("alpha", 2, 3))
        self.assertEqual(p["states"], ["done", "current", "done"])

    def test_last_existing_plan_wins_and_output_mentions_are_ignored(self):
        self.plan("2026-01-02-alpha", ["### 1. a"])
        absolute = self.plan("2026-01-03-beta", ["### 1. a", "### 2. b"])
        self.say(tool_use("plan-log docs/plans/2026-01-02-alpha/log.md note x"),
                 tool_use("plan-log %s note y" % absolute),
                 tool_use("plan-log docs/plans/2026-01-04-missing/log.md note z"),
                 tool_result("plan-log docs/plans/2026-01-02-alpha/log.md"),
                 tool_use("cat docs/plans/2026-01-02-alpha/log.md"))
        self.assertEqual(self.progress()["name"], "beta")

    def test_incremental_scan_picks_up_new_mention_and_truncation_resets(self):
        self.plan("2026-01-02-alpha", ["### 1. a"])
        self.plan("2026-01-03-beta", ["### 1. a", "### 2. b"])
        self.say(tool_use("plan-log docs/plans/2026-01-02-alpha/log.md note x"))
        self.assertEqual(self.progress()["name"], "alpha")
        offset = self.cache["sid1"]["offset"]
        self.say(tool_use("plan-log docs/plans/2026-01-03-beta/log.md note y"))
        self.assertEqual(self.progress()["name"], "beta")
        self.assertGreater(self.cache["sid1"]["offset"], offset)
        with open(self.transcript, "w") as f:  # transcript rewritten shorter
            f.write(tool_use("plan-log docs/plans/2026-01-02-alpha/log.md note z"))
        self.assertEqual(self.progress()["name"], "alpha")

    def test_partial_last_line_is_left_for_the_next_scan(self):
        self.plan("2026-01-02-alpha", ["### 1. a"])
        line = tool_use("plan-log docs/plans/2026-01-02-alpha/log.md note x")
        self.say(line[:20])
        self.assertIsNone(self.progress())
        self.say(line[20:])
        self.assertEqual(self.progress()["name"], "alpha")

    def test_plan_complete_and_stale_log_hide_the_ring(self):
        self.plan("2026-01-02-alpha", ["### 1. a"], ["- 2026-01-02 10:00 · decision · D3 hidden when the log says Plan complete",
                   "- 2026-01-02 10:01 · decision · D9 only a '· progress · Plan complete' line counts",
                   "- 2026-01-02 10:02 · decision · quoting '· progress · Task 1 done' is not done"])
        self.say(tool_use("plan-log docs/plans/2026-01-02-alpha/log.md note x"))
        p = self.progress()  # phrases quoted inside decisions don't count
        self.assertEqual((p["done"], p["states"]), (0, ["current"]))
        self.plan("2026-01-02-alpha", ["### 1. a"], ["- 2026-01-02 10:00 · progress · Plan complete"])
        self.assertIsNone(self.progress())
        self.plan("2026-01-02-alpha", ["### 1. a"], age_s=25 * 3600)
        self.assertIsNone(self.progress())

    def test_superpowers_plan_in_worktree_reads_sdd_ledger(self):
        tree = os.path.join(self.root, "repo-feature")
        al.run = lambda cmd: "worktree %s\n\nworktree %s\n" % (self.cwd, tree) if "worktree" in cmd else ""
        log = self.plan("2026-01-05-repairs", ["### Task 1: a", "### Task 2: b", "### Task 3: c"], base=tree)
        ledger = os.path.join(tree, ".superpowers/sdd/plan/progress.md")
        os.makedirs(os.path.dirname(ledger))
        with open(ledger, "w") as f:
            f.write("# SDD ledger — plan: %s\n\nTask 1: dispatched\nTask 1: complete (x)\nTask 2: dispatched\n"
                    % log.replace("log.md", "plan.md"))
        self.say(tool_use("plan-log docs/plans/2026-01-05-repairs/log.md decision x"))
        p = self.progress()
        self.assertEqual((p["name"], p["done"], p["total"]), ("repairs", 1, 3))
        self.assertEqual(p["states"], ["done", "current", "todo"])

    def test_no_transcript_or_session_id_is_none(self):
        self.assertIsNone(self.progress())
        self.assertIsNone(al.plan_progress({"pid": 1}, {}, self.projects, self.now))

    def test_running_subagents_only_unfinished_and_fresh(self):
        self.say("\n")
        self.progress()  # locates the transcript
        sub = os.path.join(self.projects, "-ws", "sid1", "subagents")
        os.makedirs(sub)
        for name, finished, age in (("a", True, 10), ("b", False, 10), ("c", False, 3600)):
            with open(os.path.join(sub, "agent-%s.meta.json" % name), "w") as f:
                json.dump({"description": "job " + name, "finished": finished}, f)
            path = os.path.join(sub, "agent-%s.jsonl" % name)
            open(path, "w").close()
            os.utime(path, (self.now - age, self.now - age))
        self.assertEqual(al.running_subagents(self.rec, self.cache, self.now), ["job b"])

    def test_enrich_attaches_fields_and_drops_dead_sessions_from_cache(self):
        self.cache["gone"] = {"offset": 5}
        lights = al.enrich([{"pid": 1}], [self.rec], self.cache, self.projects, self.now)
        self.assertEqual((lights[0]["plan"], lights[0]["subagents"]), (None, []))
        self.assertNotIn("gone", self.cache)


class RepoColors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.colors = os.path.join(self.tmp.name, "agent-lights", "colors.json")
        self.orig_run = al.run

    def tearDown(self):
        al.run = self.orig_run
        self.tmp.cleanup()

    def git_says(self, common_dir):
        al.run = lambda cmd: (common_dir + "\n") if cmd[:1] == ["git"] else ""

    def test_worktree_and_subdir_resolve_to_main_repo(self):
        self.git_says("/src/music-sorter/.git")
        self.assertEqual(al.repo_of("/src/music-sorter-repairs"), "music-sorter")
        self.assertEqual(al.repo_of("/src/music-sorter/packages/core"), "music-sorter")

    def test_bare_repo_and_non_git_fallback(self):
        self.git_says("/srv/tools.git")
        self.assertEqual(al.repo_of("/srv/tools-wt"), "tools")
        self.git_says("")
        self.assertEqual(al.repo_of("/home/me/notes/"), "notes")

    def test_auto_colour_is_stable_and_from_palette(self):
        hexes = set(al.PALETTE.values())
        for repo in ("music-sorter", "config-workspace", "a", "omarchy-style"):
            self.assertIn(al.auto_color(repo), hexes)
            self.assertEqual(al.auto_color(repo), al.auto_color(repo))
        self.assertEqual(al.auto_color("music-sorter"), "#89dceb")  # crc32-based: same on every machine

    def test_palette_avoids_state_colours(self):
        self.assertEqual(len(al.PALETTE), 16)
        self.assertEqual(len(set(al.PALETTE.values())), 16)
        self.assertFalse({"#f38ba8", "#f9e2af", "#a6e3a1"} & set(al.PALETTE.values()))

    def test_overrides_by_name_or_hex_win_and_bad_values_are_ignored(self):
        colors = {"a": "Teal", "b": "#ABCDEF", "c": "chartreuse", "d": 7}
        self.assertEqual(al.color_for("a", colors), "#94e2d5")
        self.assertEqual(al.color_for("b", colors), "#abcdef")
        self.assertEqual(al.color_for("c", colors), al.auto_color("c"))
        self.assertEqual(al.color_for("d", colors), al.auto_color("d"))

    def test_cli_set_auto_and_refuse(self):
        self.assertEqual(al.main(["--colors", self.colors, "color", "music-sorter", "teal"]), 0)
        self.assertEqual(al.main(["--colors", self.colors, "color", "other", "#112233"]), 0)
        self.assertEqual(al.load_colors(self.colors), {"music-sorter": "teal", "other": "#112233"})
        self.assertEqual(al.main(["--colors", self.colors, "color", "music-sorter", "auto"]), 0)
        self.assertEqual(al.load_colors(self.colors), {"other": "#112233"})
        self.assertEqual(al.main(["--colors", self.colors, "color", "x", "red"]), 2)  # not in the palette
        self.assertEqual(al.load_colors(self.colors), {"other": "#112233"})

    def test_corrupt_colors_file_means_auto(self):
        os.makedirs(os.path.dirname(self.colors))
        with open(self.colors, "w") as f:
            f.write("{oops")
        self.assertEqual(al.load_colors(self.colors), {})

    def test_palette_command(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            al.main(["palette"])
        names = [c["name"] for c in json.loads(out.getvalue())]
        self.assertEqual(names, list(al.PALETTE))

    def test_enrich_adds_repo_and_colour_and_caches_repo(self):
        calls = []
        al.run = lambda cmd: calls.append(cmd) or "/src/proj/.git\n"
        rec = {"pid": 1, "sessionId": "s1", "cwd": "/src/proj"}
        cache = {}
        light = al.enrich([{"pid": 1}], [rec], cache, self.tmp.name, 0, {"proj": "mauve"})[0]
        self.assertEqual((light["repo"], light["repoColor"]), ("proj", "#cba6f7"))
        n = len(calls)
        al.enrich([{"pid": 1}], [rec], cache, self.tmp.name, 0, {})
        self.assertEqual(len(calls), n)  # repo came from the cache, no second git call


if __name__ == "__main__":
    unittest.main()
