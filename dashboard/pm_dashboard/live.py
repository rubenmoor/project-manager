"""pm-dashboard-live: runs the newest pm-dashboard of a GitHub branch, and restarts it on every push there.

    pm-dashboard-live [--repo rubenmoor/project-manager] [--branch main] [--interval 60] [pm-dashboard args…]

Polls the branch head with `git ls-remote`; on a new commit builds that commit's `#dashboard` with nix first (the old
server keeps running if the build fails), then stops the old server and starts the new one on the same port. Open
browser tabs reload themselves when the new server's pm version differs.
"""
import argparse, signal, subprocess, sys, time


def head(repo, branch):
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", f"refs/heads/{branch}"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode or not out.stdout.strip():
        raise RuntimeError(out.stderr.strip() or f"no branch {branch}")
    return out.stdout.split()[0]


def build(repo, sha):
    out = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", f"github:{repo}/{sha}#dashboard"],
                         stdout=subprocess.PIPE, text=True)
    if out.returncode:
        raise RuntimeError(f"nix build failed ({out.returncode})")
    return out.stdout.split()[0] + "/bin/pm-dashboard"


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def say(msg):
    print(f"pm-dashboard-live: {msg}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pm-dashboard-live", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="rubenmoor/project-manager", help="GitHub owner/name")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--interval", type=float, default=60, help="seconds between checks")
    ap.add_argument("--port", type=int, default=8642)
    args, rest = ap.parse_known_args(argv)
    signal.signal(signal.SIGTERM, signal.default_int_handler)   # stop the server too
    proc, running = None, None
    try:
        while True:
            try:
                sha = head(args.repo, args.branch)
                if sha != running:
                    say(f"building {args.branch} at {sha[:7]}")
                    exe = build(args.repo, sha)
                    stop(proc)
                    say(f"{'restarting' if running else 'starting'} at {sha[:7]}")
                    proc, running = subprocess.Popen([exe, "--port", str(args.port), *rest]), sha
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                say(f"{e}; keeping {running[:7] if running else 'nothing'} running")
            if proc and proc.poll() is not None:
                say(f"pm-dashboard exited ({proc.returncode}); waiting for the next push")
                proc = None
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
