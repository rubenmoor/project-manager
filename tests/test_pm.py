"""Tests for pm.py: `python3 -m unittest discover tests`."""
import datetime, importlib.util, io, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "pm.py"
spec = importlib.util.spec_from_file_location("pm", SCRIPT)
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

# A fake `claude`: works its task through `pm` commands like a real worker ($FAKE_MODE: done, block, ignore),
# commits a work file, and prints a usage limit on its first run if $FAKE_LIMIT is set.
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
    Path(f"{qid}.txt").write_text("work")
    subprocess.run(["git", "add", "-A"]); subprocess.run(["git", "commit", "-qm", f"{qid} fake"])
print("worked", qid)
'''


class PMTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        worker = self.root / "fake-claude"
        worker.write_text(f"#!{sys.executable}" + WORKER)  # no /usr/bin/env in a Nix build
        worker.chmod(0o755)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.out = io.StringIO()
        self.pm = pm.Project(self.root, claude=str(worker), out=self.out)
        self.assertEqual(self.pm.milestone_add("title M001", "goal of M001"), "M001")
        self.pm.milestone_set("M001", status="implemented")
        self.assertEqual(self.pm.milestone_add("title M002", "goal of M002\n"), "M002")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
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

    def ids(self):
        return [qid for _, qid, _ in self.pm.queue()]

    def task(self, qid):
        with self.pm.connect() as db:
            return self.pm.find(db, qid)

    def decision(self, did):
        return self.pm.rows("SELECT * FROM decision WHERE id = ?", did)[0]

    def raw(self, sql):
        """A hand edit, as with the sqlite3 shell: foreign keys off."""
        con = sqlite3.connect(self.pm.db_path)
        con.execute(sql)
        con.commit()
        con.close()

    # --- database ---

    def test_database_is_created_with_the_schema_and_kept_out_of_git(self):
        self.assertTrue((self.root / pm.DB_FILE).exists())
        self.assertIn(f"/{pm.DB_FILE}*", (self.root / ".git" / "info" / "exclude").read_text())
        self.assertNotIn(pm.DB_FILE, self.git("ls-files"))
        self.assertFalse(self.pm.dirty())
        self.assertEqual(self.pm.rows("PRAGMA user_version")[0]["user_version"], pm.SCHEMA_VERSION)
        self.assertEqual(self.pm.rows("SELECT description FROM milestone WHERE id = 'M002'")[0]["description"],
                         "goal of M002")

    def test_newer_schema_is_refused(self):
        self.raw("PRAGMA user_version = 99")
        self.assertIn("schema version 99", self.pm.validate()[0])

    # --- sync and queue ---

    def test_sync_stubs_open_milestones_only(self):
        self.assertEqual(self.pm.sync(), ["M002 masterplan stub"])
        mps = self.pm.rows("SELECT milestone, status, goal FROM masterplan")
        self.assertEqual(mps, [{"milestone": "M002", "status": "stub", "goal": "goal of M002"}])
        self.assertEqual(self.ids(), ["M002-P001-T001", "M002-P001-T002"])
        self.assertEqual(self.pm.validate(), [])
        self.assertEqual(self.pm.sync(), [])  # idempotent

    def test_queue_sorts_by_priority_then_id_and_tasks_inherit_the_plan_priority(self):
        self.pm.sync()
        self.assertEqual(self.pm.plan_add("M002", "later", "g", prio=-1), "M002-P002")
        self.assertEqual(self.pm.plan_add("M002", "urgent", "g", prio=5), "M002-P003")
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
        self.pm.sync()
        self.pm.plan_add("M002", "second", "g")
        self.assertEqual(self.pm.task_add("M002-P002", "first of P002", "d"), "M002-P002-T001")
        self.assertEqual(self.pm.task_add("M002-P001", "third of P001", "d"), "M002-P001-T003")

    def test_milestone_closes_when_all_tasks_are_closed_and_the_masterplan_is_not_a_stub(self):
        self.pm.sync()
        self.pm.task_set("M002-P001-T001", status="done", note="x")
        self.pm.task_set("M002-P001-T002", status="dropped", note="y")
        self.assertEqual(self.pm.sync(), [])  # still a stub
        self.pm.masterplan_set("M002", status="draft")
        self.assertEqual(self.pm.sync(), ["M002 implemented"])
        self.assertEqual(self.pm.rows("SELECT status FROM milestone WHERE id = 'M002'")[0]["status"], "implemented")
        self.assertEqual(self.ids(), [])

    # --- decisions ---

    def test_blocked_task_leaves_the_queue_until_the_decision_is_answered(self):
        self.pm.sync()
        did = self.pm.decision_add("Which color?", "ctx", ["red", "blue"], ["M002-P001-T001"])
        self.assertEqual(did, "D0001")
        self.assertEqual(self.ids(), ["M002-P001-T002"])
        self.assertEqual(self.pm.decide(ask=lambda _: "2"), 1)
        self.assertEqual(self.ids(), ["M002-P001-T001", "M002-P001-T002"])
        d = self.decision("D0001")
        self.assertEqual((d["status"], d["answer"]), ("answered", "blue"))
        self.assertIn("Decision D0001 (answered): Which color? → blue", self.pm.show("M002-P001-T001"))
        self.assertEqual(self.pm.validate(), [])

    def test_a_task_can_wait_for_several_decisions_and_a_decision_can_block_several_tasks(self):
        self.pm.sync()
        self.pm.decision_add("A?", blocks=["M002-P001-T001", "M002-P001-T002"])
        self.pm.decision_add("B?", blocks=["M002-P001-T001"])
        self.assertEqual(self.ids(), [])
        self.pm.resolve("D0001", answer="yes")
        self.assertEqual(self.ids(), ["M002-P001-T002"])
        self.pm.resolve("D0002", answer="yes")
        self.assertEqual(self.ids(), ["M002-P001-T001", "M002-P001-T002"])

    def test_irrelevant_decision_drops_its_open_tasks(self):
        self.pm.sync()
        self.pm.decision_add("Which color?", blocks=["M002-P001-T001"])
        self.pm.decide(ask=lambda _: "x not needed")
        t = self.task("M002-P001-T001")
        self.assertEqual((t["status"], t["note"]), ("dropped", "D0001 irrelevant"))
        self.assertEqual(self.decision("D0001")["reason"], "not needed")

    def test_free_text_answer_and_skip(self):
        self.pm.sync()
        self.pm.decision_add("A?", options=["yes", "no"])
        self.pm.decision_add("B?")
        replies = iter(["s", "green, please"])
        self.assertEqual(self.pm.decide(ask=lambda _: next(replies)), 1)
        self.assertEqual(self.decision("D0001")["status"], "pending")
        self.assertEqual(self.decision("D0002")["answer"], "green, please")

    # --- validation ---

    def test_validate_reports_dangling_references_from_hand_edits(self):
        self.pm.sync()
        self.raw("INSERT INTO task (milestone, plan, id, title, description) VALUES ('M002', 'P007', 'T001', 'x', 'd')")
        self.raw("INSERT INTO task_decision VALUES ('M002', 'P001', 'T001', 'D0042')")
        errors = "\n".join(self.pm.validate())
        self.assertIn("task (milestone M002, plan P007, id T001): no such plan", errors)
        self.assertIn("no such decision", errors)

    def test_validate_reports_a_broken_file(self):
        self.pm.db_path.write_bytes(b"not a database, just some bytes" * 100)
        self.assertIn(pm.DB_FILE, "\n".join(self.pm.validate()))

    def test_edit_commands_refuse_changes_that_break_the_schema(self):
        self.pm.sync()
        before = self.pm.db_path.read_bytes()
        with self.assertRaisesRegex(pm.FormatError, "an open task needs a description"):
            self.pm.task_add("M002-P001", "no description", "")
        with self.assertRaisesRegex(pm.FormatError, "no decision D0099"):
            self.pm.task_block("M002-P001-T001", "D0099")
        with self.assertRaisesRegex(pm.FormatError, "no plan M002-P009"):
            self.pm.task_add("M002-P009", "no such plan", "d")
        with self.assertRaisesRegex(pm.FormatError, "no task"):  # rolls back the decision it already added
            self.pm.decision_add("Q?", blocks=["M002-P001-T009"])
        self.assertEqual(self.pm.db_path.read_bytes(), before)

    # --- run ---

    def test_run_once_works_the_first_task(self):
        self.assertEqual(self.pm.run(once=True, interactive=False), 0)
        self.assertEqual(self.ids(), ["M002-P001-T002"])
        t = self.task("M002-P001-T001")
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
        self.assertEqual(self.task("M002-P001-T001")["status"], "done")

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
        self.pm.sync()
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
        self.pm.sync()
        self.assertEqual(self.pm.run(dry_run=True, interactive=False), 0)
        out = self.out.getvalue()
        self.assertIn("You are the worker for task M002-P001-T001", out)
        self.assertIn("Task M002-P001-T001: write goal, scope and exit criteria", out)
        self.assertIn("--permission-mode acceptEdits", out)

    def test_worker_log_lives_in_the_git_dir(self):
        self.pm.run(once=True, interactive=False)
        self.assertTrue((self.root / ".git" / "pm-logs" / "M002-P001-T001.log").exists())

    def test_other_database(self):
        other = pm.Project(self.root, db="other.dbsqlite", out=self.out)
        self.assertEqual(other.queue(), [])
        self.assertEqual(other.milestone_add("elsewhere"), "M001")
        self.assertEqual(other.sync(), ["M001 masterplan stub"])
        self.assertEqual([q for _, q, _ in other.queue()], ["M001-P001-T001", "M001-P001-T002"])
        self.assertEqual(self.ids(), [])  # tasks.dbsqlite is untouched: M002 has no masterplan yet
        self.assertFalse(self.pm.dirty())

    def test_cli(self):
        self.pm.sync()

        def run(*a, stdin=None):
            return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), *a],
                                  input=stdin, capture_output=True, text=True)
        self.assertEqual(run("milestone", "add", "via cli", "--description", "-", stdin="# Goal\n").stdout.strip(),
                         "M003")
        self.assertEqual(run("plan", "add", "M002", "via cli", "--description", "g", "--priority", "2")
                         .stdout.strip(), "M002-P002")
        r = run("task", "add", "M002-P002", "from stdin", "--description", "-", stdin="long\ndetails\n")
        self.assertEqual(r.stdout.strip(), "M002-P002-T001")
        self.assertIn("+2 M002-P002-T001 from stdin", run("queue").stdout)
        self.assertEqual(self.task("M002-P002-T001")["description"], "long\ndetails")
        self.assertEqual(run("validate").returncode, 0)
        r = run("task", "add", "M002-P002", "empty", "--description", " ")
        self.assertEqual(r.returncode, 1)
        self.assertIn("an open task needs a description", r.stdout)


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
