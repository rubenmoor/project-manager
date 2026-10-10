"""Tests for pm.py: `python3 -m unittest discover tests`."""
import datetime, importlib.util, io, os, subprocess, sys, tempfile, tomllib, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "pm.py"
spec = importlib.util.spec_from_file_location("pm", SCRIPT)
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

# A fake `claude`: works its task through `pm` commands like a real worker ($FAKE_MODE: done, block, ignore),
# commits, and prints a usage limit on its first run if $FAKE_LIMIT is set.
WORKER = r'''
import os, re, subprocess, sys
from pathlib import Path
prompt = sys.argv[sys.argv.index("-p") + 1]
qid = re.search(r"worker for task (M\d{3}-P\d{3}-T\d{3})", prompt)[1]
pm = [sys.executable, os.environ["PM_SCRIPT"], "--root", "."]
mode = os.environ.get("FAKE_MODE", "done")
marker = Path(".fake-limit-seen")
if os.environ.get("FAKE_LIMIT") and not marker.exists():
    marker.write_text("x")
    subprocess.run(["git", "add", ".fake-limit-seen"]); subprocess.run(["git", "commit", "-qm", "seen"])
    print("Claude error: You've hit your session limit · resets 7:10pm (America/Bogota)")
    sys.exit(1)
if mode == "done":
    subprocess.run(pm + ["task", "done", qid, "2026-10-10, fake"], check=True)
elif mode == "block":
    subprocess.run(pm + ["decision", "add", "Which color?", "--context", "needed",
                         "--option", "red", "--option", "blue", "--blocks", qid], check=True)
if mode != "ignore":
    subprocess.run(["git", "add", "-A"]); subprocess.run(["git", "commit", "-qm", f"{qid} fake"])
print("worked", qid)
'''


def milestone(mid, status="open"):
    return pm.dump_milestone({"id": mid, "title": f"title {mid}", "status": status, "goal": f"goal of {mid}"})


class PMTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "plans").mkdir()
        (self.root / "plans" / "M001.toml").write_text(milestone("M001", "implemented"))
        (self.root / "plans" / "M002.toml").write_text(milestone("M002"))
        worker = self.root / "fake-claude"
        worker.write_text(f"#!{sys.executable}" + WORKER)  # no /usr/bin/env in a Nix build
        worker.chmod(0o755)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.out = io.StringIO()
        self.pm = pm.Project(self.root, claude=str(worker), out=self.out)
        self.waits = []
        self.pm.wait_until = lambda when: self.waits.append(when)
        os.environ["PM_SCRIPT"] = str(SCRIPT)
        self.cwd = os.getcwd()
        os.chdir(self.root)  # the fake worker uses relative paths

    def tearDown(self):
        os.chdir(self.cwd)
        for k in ("FAKE_MODE", "FAKE_LIMIT", "PM_SCRIPT"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def toml(self, name):
        return tomllib.loads((self.root / "plans" / name).read_text())

    def ids(self):
        return [qid for _, qid, _ in self.pm.queue()]

    def synced(self):
        self.pm.sync()
        self.pm.commit("sync")

    # --- files ---

    def test_dump_round_trips(self):
        m = {"id": "M009", "title": 'a "quoted" title', "status": "open", "goal": "line 1\nline 2 with \\ backslash",
             "masterplan": {"status": "draft", "goal": "one line", "scope": "it's in", "exit_criteria": "x'''y"},
             "plans": [{"id": "P001", "title": "p", "priority": -2, "goal": "g"}],
             "tasks": [{"id": "T001", "plan": "P001", "subject": "s", "status": "open", "priority": 3,
                        "blocked_by": ["D0001"], "details": "tab\there\nand ünïcode"}]}
        (self.root / "plans" / "M009.toml").write_text(pm.dump_milestone(m))
        self.assertEqual(self.pm.load("M009"), m)  # prose is stored stripped
        self.assertEqual(pm.dump_milestone(self.pm.load("M009")), pm.dump_milestone(m))
        ds = [{"id": "D0001", "question": "Q?", "status": "answered", "options": ["a", "b"],
               "context": "c", "answer": "a", "closed": "2026-10-10"}]
        self.assertEqual([pm.normalize(d) for d in tomllib.loads(pm.dump_decisions(ds))["decisions"]], ds)

    # --- sync and queue ---

    def test_sync_stubs_open_milestones_only(self):
        self.assertEqual(self.pm.sync(), ["M002 masterplan stub"])
        self.assertNotIn("masterplan", self.toml("M001.toml"))
        m = self.toml("M002.toml")
        self.assertEqual(m["masterplan"]["status"], "stub")
        self.assertEqual([(t["plan"], t["id"]) for t in m["tasks"]], [("P001", "T001"), ("P001", "T002")])
        self.assertEqual(self.ids(), ["M002-P001-T001", "M002-P001-T002"])
        self.assertEqual(self.pm.validate(), [])
        self.assertEqual(self.pm.sync(), [])  # idempotent

    def test_queue_sorts_by_priority_then_id_and_tasks_inherit_the_plan_priority(self):
        self.synced()
        self.assertEqual(self.pm.plan_add("M002", "later", goal="g", prio=-1), "M002-P002")
        self.assertEqual(self.pm.plan_add("M002", "urgent", goal="g", prio=5), "M002-P003")
        self.pm.task_add("M002-P002", "low a", "d")
        self.pm.task_add("M002-P002", "low but pushed", "d", prio=9)
        self.pm.task_add("M002-P003", "urgent a", "d")
        self.pm.task_add("M002-P003", "urgent b", "d")
        self.assertEqual([(p, q) for p, q, _ in self.pm.queue()], [
            (9, "M002-P002-T002"), (5, "M002-P003-T001"), (5, "M002-P003-T002"),
            (0, "M002-P001-T001"), (0, "M002-P001-T002"), (-1, "M002-P002-T001")])
        self.pm.task_set("M002-P002-T002", priority=None)  # back to the plan's -1
        self.assertEqual(self.ids()[-2:], ["M002-P002-T001", "M002-P002-T002"])

    def test_task_ids_count_per_plan(self):
        self.synced()
        self.pm.plan_add("M002", "second", goal="g")
        self.assertEqual(self.pm.task_add("M002-P002", "first of P002", "d"), "M002-P002-T001")
        self.assertEqual(self.pm.task_add("M002-P001", "third of P001", "d"), "M002-P001-T003")

    def test_milestone_closes_when_all_tasks_are_closed_and_the_masterplan_is_not_a_stub(self):
        self.synced()
        self.pm.task_set("M002-P001-T001", status="done", note="x")
        self.pm.task_set("M002-P001-T002", status="dropped", note="y")
        self.assertEqual(self.pm.sync(), [])  # still a stub
        self.pm.masterplan_set("M002", status="draft")
        self.assertEqual(self.pm.sync(), ["M002 implemented"])
        self.assertEqual(self.toml("M002.toml")["status"], "implemented")
        self.assertEqual(self.ids(), [])

    # --- decisions ---

    def test_blocked_task_leaves_the_queue_until_the_decision_is_answered(self):
        self.synced()
        did = self.pm.decision_add("Which color?", "ctx", ["red", "blue"], ["M002-P001-T001"])
        self.assertEqual(did, "D0001")
        self.assertEqual(self.ids(), ["M002-P001-T002"])
        self.assertEqual(self.pm.decide(ask=lambda _: "2"), 1)
        self.assertEqual(self.ids(), ["M002-P001-T001", "M002-P001-T002"])
        d = self.toml("pm-decisions.toml")["decisions"][0]
        self.assertEqual((d["status"], d["answer"]), ("answered", "blue"))
        self.assertIn("Decision D0001 (answered): Which color? → blue", self.pm.show("M002-P001-T001"))
        self.assertEqual(self.pm.validate(), [])

    def test_irrelevant_decision_drops_its_open_tasks(self):
        self.synced()
        self.pm.decision_add("Which color?", blocks=["M002-P001-T001"])
        self.pm.decide(ask=lambda _: "x not needed")
        t = self.toml("M002.toml")["tasks"][0]
        self.assertEqual((t["status"], t["note"]), ("dropped", "D0001 irrelevant"))
        self.assertEqual(self.toml("pm-decisions.toml")["decisions"][0]["reason"], "not needed")

    def test_free_text_answer_and_skip(self):
        self.synced()
        self.pm.decision_add("A?", options=["yes", "no"])
        self.pm.decision_add("B?")
        replies = iter(["s", "green, please"])
        self.assertEqual(self.pm.decide(ask=lambda _: next(replies)), 1)
        ds = {d["id"]: d for d in self.toml("pm-decisions.toml")["decisions"]}
        self.assertEqual(ds["D0001"]["status"], "pending")
        self.assertEqual(ds["D0002"]["answer"], "green, please")

    # --- validation ---

    def test_validate_reports_format_errors(self):
        self.synced()
        path = self.root / "plans" / "M002.toml"
        text = path.read_text().replace('status = "open"\ndetails', 'status = "later"\ndetails', 1)
        text += '\n[[tasks]]\nid = "T009"\nplan = "P007"\nsubject = "x"\nstatus = "open"\ndetails = "d"\ncolour = 1\n'
        path.write_text(text)
        errors = "\n".join(self.pm.validate())
        self.assertIn("status must be one of open, done, dropped", errors)
        self.assertIn("plan 'P007' is not a plan of M002", errors)
        self.assertIn("unknown key 'colour'", errors)

    def test_validate_reports_unknown_decisions_and_syntax_errors(self):
        self.synced()
        path = self.root / "plans" / "M002.toml"
        path.write_text(path.read_text().replace('subject = "split', 'blocked_by = ["D0042"]\nsubject = "split'))
        self.assertIn("blocked_by names D0042", "\n".join(self.pm.validate()))
        path.write_text("id = \n")
        self.assertIn("M002.toml:", "\n".join(self.pm.validate()))

    def test_edit_commands_refuse_changes_that_break_the_files(self):
        self.synced()
        before = (self.root / "plans" / "M002.toml").read_text()
        with self.assertRaises(pm.FormatError):
            self.pm.task_add("M002-P001", "no details", "")
        with self.assertRaises(pm.FormatError):
            self.pm.task_block("M002-P001-T001", "D0099")
        with self.assertRaises(pm.FormatError):
            self.pm.task_add("M002-P009", "no such plan", "d")
        self.assertEqual((self.root / "plans" / "M002.toml").read_text(), before)

    # --- run ---

    def test_run_once_works_the_first_task(self):
        self.assertEqual(self.pm.run(once=True, interactive=False), 0)
        self.assertEqual(self.ids(), ["M002-P001-T002"])
        t = self.toml("M002.toml")["tasks"][0]
        self.assertEqual((t["status"], t["note"]), ("done", "2026-10-10, fake"))
        self.assertFalse(self.pm.dirty())
        log = self.git("log", "--format=%s")
        self.assertIn("M002-P001-T001 fake", log)

    def test_usage_limit_waits_for_the_reset_and_runs_the_same_task_again(self):
        os.environ["FAKE_LIMIT"] = "1"
        self.assertEqual(self.pm.run(once=True, interactive=False), 0)
        self.assertEqual(len(self.waits), 1)
        reset = pm.limit_reset("resets 7:10pm (America/Bogota)")
        self.assertGreaterEqual(self.waits[0], reset + pm.LIMIT_MARGIN)
        self.assertEqual(self.toml("M002.toml")["tasks"][0]["status"], "done")

    def test_worker_that_blocks_its_task(self):
        os.environ["FAKE_MODE"] = "block"
        self.assertEqual(self.pm.run(once=True, interactive=False), 0)
        self.assertIn("blocked", self.out.getvalue())
        self.assertEqual(self.ids(), ["M002-P001-T002"])

    def test_worker_that_neither_closes_nor_blocks_fails(self):
        os.environ["FAKE_MODE"] = "ignore"
        self.assertEqual(self.pm.run(once=True, interactive=False), 1)
        self.assertEqual(self.ids(), ["M002-P001-T001", "M002-P001-T002"])
        self.assertIn("still open and not blocked", self.out.getvalue())

    def test_dirty_tree_stops_the_run(self):
        self.synced()
        (self.root / "stray.txt").write_text("x")
        self.assertEqual(self.pm.run(once=True, interactive=False), 1)
        self.assertIn("uncommitted", self.out.getvalue())

    def test_dry_run_changes_nothing(self):
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.pm.run(dry_run=True, interactive=False), 0)
        self.assertIn("Queue empty", self.out.getvalue())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.pm.dirty())

    def test_dry_run_prints_the_worker_command_with_the_task(self):
        self.synced()
        self.assertEqual(self.pm.run(dry_run=True, interactive=False), 0)
        out = self.out.getvalue()
        self.assertIn("You are the worker for task M002-P001-T001", out)
        self.assertIn("Task M002-P001-T001: write goal, scope and exit criteria", out)
        self.assertIn("--permission-mode acceptEdits", out)

    def test_worker_log_lives_in_the_git_dir(self):
        self.pm.run(once=True, interactive=False)
        self.assertTrue((self.root / ".git" / "pm-logs" / "M002-P001-T001.log").exists())

    def test_text_ending_in_a_quote_stays_readable(self):
        self.assertTrue(pm.toml_value("it's 'x'", prose=True).startswith("'''"))
        self.assertEqual(tomllib.loads("x = " + pm.toml_value("it's 'x'", prose=True))["x"].strip(), "it's 'x'")

    def test_other_plans_folder(self):
        (self.root / "work").mkdir()
        (self.root / "work" / "M001.toml").write_text(milestone("M001"))
        other = pm.Project(self.root, plans_dir="work", out=self.out)
        self.assertEqual(other.sync(), ["M001 masterplan stub"])
        self.assertEqual([q for _, q, _ in other.queue()], ["M001-P001-T001", "M001-P001-T002"])
        self.assertEqual(self.ids(), [])  # plans/ is untouched: M002 has no masterplan yet

    def test_cli(self):
        self.synced()

        def run(*a, stdin=None):
            return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), *a],
                                  input=stdin, capture_output=True, text=True)
        self.assertEqual(run("plan", "add", "M002", "via cli", "--goal", "g", "--priority", "2").stdout.strip(),
                         "M002-P002")
        r = run("task", "add", "M002-P002", "from stdin", "--details", "-", stdin="long\ndetails\n")
        self.assertEqual(r.stdout.strip(), "M002-P002-T001")
        self.assertIn("+2 M002-P002-T001 from stdin", run("queue").stdout)
        self.assertEqual(self.pm.load("M002")["tasks"][-1]["details"], "long\ndetails")
        self.assertEqual(run("validate").returncode, 0)


class LimitTest(unittest.TestCase):
    def test_reset_time_with_zone(self):
        now = datetime.datetime(2026, 10, 9, 20, 0, tzinfo=datetime.timezone.utc)
        reset = pm.limit_reset("You've hit your session limit · resets 7:10pm (America/Bogota)", now)
        self.assertEqual(reset.isoformat(), "2026-10-09T19:10:00-05:00")

    def test_limit_line_needs_limit_words_and_a_reset(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "x.log"
            log.write_text("talking about the usage limit code\n")
            self.assertIsNone(pm.Project.limit_line(log))
            log.write_text("ok\nClaude error: You've hit your session limit · resets 3am\n")
            self.assertIn("resets 3am", pm.Project.limit_line(log))


if __name__ == "__main__":
    unittest.main()
