"""Verify loop: phases, verify runs, accept/reject, max_tries, models, `pm report tries`.
`python -m unittest tests.test_verify_loop`; a fake worker named claude acts out each run from a script."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pm  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
state = Path(os.environ["FAKE_STATE"])
steps = json.loads(state.read_text())
step = steps.pop(0)
state.write_text(json.dumps(steps))
args = sys.argv[1:]
model = args[args.index("--model") + 1] if "--model" in args else ""
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"task": os.environ["PM_TASK"], "verify": bool(os.environ.get("PM_VERIFY")), "model": model,
                        "step": step, "prompt": prompt}) + "\n")
tid = os.environ["PM_TASK"]
pm = [sys.executable, "-m", "pm"]
def run(*a, stdin=None):
    subprocess.run([*pm, *a], input=stdin, text=True, check=True)
do = step["do"]
if do == "work":
    Path(step["file"]).write_text(step.get("text", "x") + "\n")
    subprocess.run(["git", "add", step["file"]], check=True)
    run("task", "done", tid, "note", "--evidence", "-", stdin="criterion: it exists; ran ls; it showed the file")
elif do == "work-plain":
    Path(step["file"]).write_text("x\n")
    subprocess.run(["git", "add", step["file"]], check=True)
    run("task", "done", tid, "note")
elif do == "accept":
    run("task", "accept", tid, "checked")
elif do == "reject":
    run("task", "reject", tid, stdin=step.get("findings", "it fails"))
elif do == "change-accept":
    Path("README.md").write_text("changed\n")
    run("task", "accept", tid, "checked")
'''


class VerifyLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "repo"
        self.dir.mkdir()
        bin_ = Path(self.tmp.name) / "bin"
        bin_.mkdir()
        (bin_ / "claude").write_text(FAKE)
        (bin_ / "claude").chmod(0o755)
        self.state, self.log = Path(self.tmp.name) / "state.json", Path(self.tmp.name) / "log.jsonl"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        (self.dir / "README.md").write_text("readme\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        self.ok("init")
        cfg = self.dir / "project-manager.toml"
        text = cfg.read_text().replace('worker = ["claude", "-p"', f'worker = ["{bin_ / "claude"}", "-p"')
        text = text.replace("max_tries = 3", "max_tries = 2").replace("push_minutes = 60", "push_minutes = 0")
        cfg.write_text(text + 'main = "everything"\n')
        self.git("commit", "-qam", "config")
        self.ok("milestone", "add", "m1")

    def tearDown(self):
        self.tmp.cleanup()

    def pm(self, *args, stdin=""):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "FAKE_STATE": str(self.state), "FAKE_LOG": str(self.log)}
        for k in ("PM_WORKER", "PM_TASK", "PM_VERIFY", "PM_ROOT"):
            env.pop(k, None)
        return subprocess.run([sys.executable, "-m", "pm", "--root", str(self.dir), *args], cwd=self.dir, env=env,
                              input=stdin, capture_output=True, text=True)

    def ok(self, *args, stdin=""):
        r = self.pm(*args, stdin=stdin)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True, text=True).stdout

    def task(self, subject, *extra):
        return int(self.ok("task", "add", "1", subject, "--worktree", "main", *extra, stdin="do it").strip())

    def start(self):
        self.ok("task", "done", "1", "masterplan written")    # the human closes it, so it holds nothing back

    def steps(self, *steps):
        self.state.write_text(json.dumps(list(steps)))

    def run_once(self):
        r = self.pm("run", "--once")
        return r

    def row(self, tid):
        p = pm.Project(self.dir, pm.load_config(self.dir))
        return p.rows("SELECT * FROM task WHERE id = ?", tid)[0]

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def in_trunk(self, name):
        return name in self.git("ls-tree", "--name-only", "HEAD").split()

    def test_accept_merges_only_after_verify(self):
        a = self.task("a")
        b = self.task("b", "--requires", str(a))
        self.start()
        self.steps({"do": "work", "file": "a.txt"}, {"do": "accept"}, {"do": "work", "file": "b.txt"}, {"do": "accept"})
        self.run_once()
        self.assertEqual((self.row(a)["status"], self.row(a)["phase"]), ("open", "verifying"))
        self.assertFalse(self.in_trunk("a.txt"), "merged before accept")
        self.assertIn("verifying, try 1", self.ok("queue"))
        self.run_once()
        self.assertEqual(self.row(a)["status"], "done")
        self.assertTrue(self.in_trunk("a.txt"))
        calls = self.calls()
        self.assertEqual([c["verify"] for c in calls], [False, True])
        self.assertEqual([c["task"] for c in calls], [str(a), str(a)], "the dependent ran before its prerequisite was done")
        self.assertEqual([c["model"] for c in calls], ["sonnet", "opus"])
        verify_prompt = calls[1]["prompt"]
        self.assertIn("a.txt", verify_prompt)
        self.assertLess(verify_prompt.index("infer the task's acceptance criteria"), verify_prompt.index("criterion: it exists"))
        self.run_once()
        self.assertEqual(self.calls()[2]["task"], str(b))

    def test_reject_rework_escalates_then_decision(self):
        a = self.task("a")
        self.start()
        self.steps({"do": "work", "file": "a.txt"}, {"do": "reject", "findings": "F1: broken"},
                   {"do": "work", "file": "a.txt", "text": "y"}, {"do": "reject", "findings": "F2: still broken"},
                   {"do": "work", "file": "a.txt", "text": "z"}, {"do": "accept"})
        self.run_once()
        self.run_once()
        self.assertEqual(self.row(a)["phase"], "reworking")
        self.assertFalse(self.in_trunk("a.txt"), "merged while reworking")
        self.run_once()
        calls = self.calls()
        self.assertEqual(calls[2]["model"], "opus", "no escalation after a rejection")
        self.assertIn("F1: broken", calls[2]["prompt"])
        self.run_once()
        self.assertEqual(self.row(a)["phase"], "reworking")
        d = [x for x in pm.Project(self.dir, pm.load_config(self.dir)).pending()
             if x["subject"].startswith(f"task {a}: rejected after 2 tries")]
        self.assertTrue(d, "no decision after max_tries")
        self.assertNotIn(f"task {a}:", self.ok("queue"))
        self.ok("decision", "answer", str(d[0]["id"]), "1")
        self.run_once()
        self.run_once()
        self.assertEqual(self.row(a)["status"], "done")
        report = self.ok("report", "tries")
        self.assertIn("sonnet→opus/opus (1 task", report)
        g = pm.Project(self.dir, pm.load_config(self.dir)).tries_data()[0]
        self.assertEqual([b["tasks"] for b in g["buckets"]], [0, 0, 1, 0, 0, 0])

    def test_drop_after_max_tries_reverts(self):
        a = self.task("a", "--tries", "sonnet")
        self.start()
        self.steps({"do": "work", "file": "a.txt"}, {"do": "reject"}, {"do": "work", "file": "a.txt", "text": "y"},
                   {"do": "reject"})
        for _ in range(4):
            self.run_once()
        self.assertEqual([c["model"] for c in self.calls() if not c["verify"]], ["sonnet", "sonnet"])
        did = pm.Project(self.dir, pm.load_config(self.dir)).pending()[0]["id"]
        self.ok("decision", "answer", str(did), "3")
        self.assertEqual(self.row(a)["status"], "dropped")
        self.run_once()
        self.assertFalse(self.in_trunk("a.txt"))
        self.assertNotEqual(self.row(a)["revert"], "pending")
        g = pm.Project(self.dir, pm.load_config(self.dir)).tries_data()[0]
        self.assertEqual(g["buckets"][5]["tasks"], 1)

    def test_verify_run_that_changes_files_fails(self):
        a = self.task("a")
        self.start()
        self.steps({"do": "work", "file": "a.txt"}, {"do": "change-accept"}, {"do": "accept"})
        self.run_once()
        self.run_once()
        self.assertEqual((self.row(a)["status"], self.row(a)["phase"]), ("open", "verifying"))
        self.assertNotIn("changed", (self.dir.parent / "repo-worktrees/main/README.md").read_text())
        self.run_once()
        self.assertEqual(self.row(a)["status"], "done", "the verify run wasn't run again after its first failure")

    def test_no_verify(self):
        a = self.task("a", "--no-verify")
        self.start()
        self.steps({"do": "work-plain", "file": "a.txt"})
        self.run_once()
        self.assertEqual(self.row(a)["status"], "done")
        self.assertTrue(self.in_trunk("a.txt"))
        self.assertIn("not verified", self.ok("show", str(a)))

    def test_evidence_required(self):
        self.task("a")
        self.start()
        self.steps({"do": "work-plain", "file": "a.txt"})
        self.run_once()
        self.assertEqual(self.row(2)["phase"], "implementing")


class Models(unittest.TestCase):
    def project(self, models, **task):
        p = pm.Project.__new__(pm.Project)
        p.config = {"models": models}
        t = {"kind": "work", "model": None, "tries": None, "verify": None, "verify_model": None} | task
        return p, t

    def test_short_form(self):
        p, t = self.project({"default": "sonnet", "work": "haiku"})
        self.assertEqual(p.models_of(t), {"tries": ["haiku"], "verify": None, "why": "kind"})

    def test_old_escalate(self):
        p, t = self.project({"default": "sonnet", "escalate": "opus", "split": "fable"}, kind="split")
        self.assertEqual(p.models_of(t)["tries"], ["fable", "opus"])

    def test_table_and_verify_list(self):
        p, t = self.project({"default": "sonnet", "verify": "opus",
                             "work": {"tries": ["sonnet", "opus"], "verify": ["sonnet", "opus"]}})
        self.assertEqual(p.models_of(t), {"tries": ["sonnet", "opus"], "verify": ["sonnet", "opus"], "why": "kind"})
        p, t = self.project({"default": "sonnet", "verify": "opus", "work": {"tries": "sonnet", "verify": False}})
        self.assertIsNone(p.models_of(t)["verify"])

    def test_task_overrides(self):
        m = {"default": "sonnet", "verify": "opus", "work": {"tries": ["sonnet", "opus"], "verify": "opus"}}
        p, t = self.project(m, model="haiku")
        self.assertEqual(p.models_of(t)["tries"], ["haiku"])
        p, t = self.project(m, tries='["opus", "fable"]', verify_model='["fable"]')
        self.assertEqual(p.models_of(t), {"tries": ["opus", "fable"], "verify": ["fable"], "why": "set"})
        p, t = self.project(m, verify=0)
        self.assertIsNone(p.models_of(t)["verify"])
        p, t = self.project({"default": "sonnet", "verify": "opus"}, verify=1)
        self.assertEqual(p.models_of(t)["verify"], ["opus"])

    def test_ranking_warning(self):
        p, _ = self.project({"default": "sonnet", "work": {"tries": ["sonnet", "opus"], "verify": "sonnet"}})
        p.rows = lambda *a: []
        self.assertTrue(any("try 2's verifier sonnet is weaker than its worker opus" in w for w in p.model_warnings()))
        p, _ = self.project({"default": "sonnet", "work": {"tries": ["x"], "verify": "y"}})
        p.rows = lambda *a: []
        self.assertEqual(p.model_warnings(), [])

    def test_config_errors(self):
        self.assertTrue(pm.models_errors({"split": {"verify": "opus"}}))
        self.assertTrue(pm.models_errors({"work": {"tries": []}}))
        self.assertFalse(pm.models_errors({"work": {"tries": "sonnet", "verify": ["sonnet", "opus"]}, "ranking": ["a"]}))


if __name__ == "__main__":
    unittest.main()
