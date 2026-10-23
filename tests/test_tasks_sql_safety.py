"""tasks.sql never loses rows while git removes and restores it (autostash): `python -m unittest tests.test_tasks_sql_safety`."""
import os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rows(text):
    return sum(l.startswith("INSERT INTO") for l in text.splitlines())


class TasksSqlSafety(unittest.TestCase):
    def pm(self, *args):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        env.pop("PM_WORKER", None)
        return subprocess.run([sys.executable, "-m", "pm", "--root", self.dir, *args], cwd=self.dir, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True)

    def test_flapping_file(self):
        with tempfile.TemporaryDirectory() as self.dir:
            db = Path(self.dir) / "tasks.sql"
            self.git("init", "-q")
            self.git("config", "user.email", "t@t")
            self.git("config", "user.name", "t")
            self.assertEqual(self.pm("init").returncode, 0)
            with open(Path(self.dir) / "project-manager.toml", "a") as f:
                f.write('main = "everything"\n')
            for i in range(3):
                r = self.pm("milestone", "add", f"milestone {i}")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            before = rows(db.read_text())
            self.assertGreater(before, 0)

            stop, seen = threading.Event(), []

            def flap():
                while not stop.is_set():
                    text = db.read_text()
                    seen.append(rows(text))
                    db.unlink()
                    time.sleep(0.05)
                    if db.exists():
                        seen.append(-rows(db.read_text()))   # pm wrote while git had it removed
                    db.write_text(text)
                    time.sleep(0.05)

            t = threading.Thread(target=flap)
            t.start()
            try:
                for i in range(15):
                    self.pm("status")
                    self.pm("milestone", "add", f"during flapping {i}")
            finally:
                stop.set()
                t.join()
            self.assertFalse([n for n in seen if n < 0], f"pm wrote tasks.sql while it was missing: {before} {seen}")
            self.assertEqual(seen, sorted(seen), "tasks.sql lost rows")
            self.assertGreaterEqual(rows(db.read_text()), before)
            self.assertEqual(self.pm("validate").returncode, 0)
            self.git("cat-file", "-e", "HEAD:tasks.sql")


if __name__ == "__main__":
    unittest.main()
