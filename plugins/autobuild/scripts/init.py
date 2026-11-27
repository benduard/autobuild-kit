#!/usr/bin/env python3
"""Set up (or resume) an autobuild run in the current project.

- Resume: if .autobuild/active exists, prints where the run left off and exits.
- Otherwise: creates .autobuild/, copies score.py in, auto-detects build/test/
  lint/typecheck gates for Node, Python, Rust, Go, or Make projects, runs them
  once as a baseline, puts the work on a fresh git branch (or inits git), and
  writes SPEC/DECISIONS/QUESTIONS templates.

Usage: python3 <skill-dir>/scripts/init.py ["task description"]
"""
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
AB = os.path.join(ROOT, ".autobuild")
HERE = os.path.dirname(os.path.abspath(__file__))
TASK = " ".join(a for a in sys.argv[1:] if not a.startswith("--")).strip()

PASS_RE, FAIL_RE = r"(\d+) passed", r"(\d+) failed"


def exists(*parts):
    return os.path.exists(os.path.join(ROOT, *parts))


def read(*parts):
    try:
        with open(os.path.join(ROOT, *parts)) as fh:
            return fh.read()
    except OSError:
        return ""


def sh(cmd):
    r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def gate(name, cmd, weight, hard, counts=False):
    g = {"name": name, "cmd": cmd, "weight": weight, "hard": hard}
    if counts:
        g.update(passed_regex=PASS_RE, failed_regex=FAIL_RE)
    return g


def detect():
    gates, stack = [], []
    if exists("package.json"):
        stack.append("node")
        try:
            scripts = json.loads(read("package.json")).get("scripts", {})
        except ValueError:
            scripts = {}
        pm = ("pnpm" if exists("pnpm-lock.yaml") else "yarn" if exists("yarn.lock")
              else "bun" if exists("bun.lockb") or exists("bun.lock") else "npm")
        run = f"{pm} run" if pm != "yarn" else "yarn"
        if "build" in scripts:
            gates.append(gate("build", f"{run} build", 2, True))
        t = scripts.get("test", "")
        if t and "no test specified" not in t:
            gates.append(gate("tests", f"{run} test", 3, True, counts=True))
        if "lint" in scripts:
            gates.append(gate("lint", f"{run} lint", 1, False))
        tc = next((k for k in ("typecheck", "type-check", "check-types", "tsc") if k in scripts), None)
        if tc:
            gates.append(gate("typecheck", f"{run} {tc}", 1, False))
        elif exists("tsconfig.json"):
            gates.append(gate("typecheck", "npx --no-install tsc --noEmit", 1, False))
    py_markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini")
    if any(exists(m) for m in py_markers) or exists("tests") and not stack:
        stack.append("python")
        pyproj = read("pyproject.toml") + read("setup.cfg")
        gates.append(gate("compile", "python3 -m compileall -q -x '/(\\.?venv|node_modules|\\.git)/' .", 1, True))
        if exists("tests") or exists("test") or "pytest" in pyproj or exists("pytest.ini"):
            gates.append(gate("tests", "python3 -m pytest -q", 3, True, counts=True))
        if "ruff" in pyproj or exists("ruff.toml") or exists(".ruff.toml"):
            gates.append(gate("lint", "ruff check .", 1, False))
        if "mypy" in pyproj or exists("mypy.ini"):
            gates.append(gate("typecheck", "mypy .", 1, False))
    if exists("Cargo.toml"):
        stack.append("rust")
        gates += [gate("build", "cargo build --quiet", 2, True),
                  gate("tests", "cargo test --quiet", 3, True, counts=True),
                  gate("lint", "cargo clippy --quiet -- -D warnings", 1, False)]
    if exists("go.mod"):
        stack.append("go")
        gates += [gate("build", "go build ./...", 2, True),
                  gate("tests", "go test ./...", 3, True),
                  gate("vet", "go vet ./...", 1, False)]
    if not gates and exists("Makefile"):
        mk = read("Makefile")
        stack.append("make")
        if "\nbuild:" in "\n" + mk:
            gates.append(gate("build", "make build", 2, True))
        if "\ntest:" in "\n" + mk:
            gates.append(gate("tests", "make test", 3, True))
    # de-dupe by name (first wins)
    seen, out = set(), []
    for g in gates:
        if g["name"] not in seen:
            seen.add(g["name"])
            out.append(g)
    return out, stack


def resume():
    state = {}
    try:
        with open(os.path.join(AB, "state.json")) as fh:
            state = json.load(fh)
    except Exception:
        pass
    score = {}
    try:
        with open(os.path.join(AB, "score.json")) as fh:
            score = json.load(fh)
    except Exception:
        pass
    print("RESUME: an autobuild run is already in progress here. Do not re-init.")
    print(f"  fix round: {state.get('round', 0)}  best score: {state.get('best', 'n/a')}  "
          f"last score: {score.get('overall', 'not graded yet')}")
    print(f"  gates locked: {os.path.exists(os.path.join(AB, 'gates.lock'))}")
    for f in ("SPEC.md", "PLAN.md", "DECISIONS.md", "QUESTIONS.md"):
        print(f"  read .autobuild/{f}" + ("" if os.path.exists(os.path.join(AB, f)) else " (missing)"))
    print("  then continue from the first unfinished task in PLAN.md.")


def main():
    if os.path.exists(os.path.join(AB, "active")):
        resume()
        return
    os.makedirs(AB, exist_ok=True)
    shutil.copy(os.path.join(HERE, "score.py"), os.path.join(AB, "score.py"))

    gates, stack = detect()
    cfg = {"threshold": 90, "max_rounds": 6, "rubric_weight": 0.4, "min_criterion": 70,
           "gate_timeout_sec": 600, "acceptance_dir": "", "gates": gates}
    with open(os.path.join(AB, "gates.json"), "w") as fh:
        json.dump(cfg, fh, indent=2)

    # git: work on its own branch so the user's branch is untouched
    git_note = ""
    if shutil.which("git"):
        code, _ = sh("git rev-parse --is-inside-work-tree")
        if code != 0:
            sh("git init -q")
            git_note = "initialized a new git repo"
        branch = "autobuild/" + time.strftime("%Y%m%d-%H%M%S")
        sh(f"git switch -q -c {branch} 2>/dev/null || git checkout -q -b {branch}")
        sh("git add -A && git -c user.email=autobuild@local -c user.name=autobuild "
           "commit -q -m 'autobuild: baseline' --allow-empty")
        _, sha = sh("git rev-parse --short HEAD")
        git_note = (git_note + "; " if git_note else "") + f"working on branch {branch} (baseline {sha})"
        gi = os.path.join(ROOT, ".gitignore")
        if ".autobuild/" not in read(".gitignore"):
            with open(gi, "a") as fh:
                fh.write("\n.autobuild/\n")

    templates = {
        "SPEC.md": f"# Goal\n\n{TASK or '<2-3 lines: what is being built and for whom>'}\n\n"
                   "## Acceptance criteria\n\n"
                   "<!-- Numbered. Each one must be provable by running something. -->\n"
                   "1. <criterion>\n\n## Out of scope\n\n- <thing>\n",
        "PLAN.md": "# Plan\n\n<!-- waves of tasks: id | owner agent | model | files | criteria | status -->\n",
        "DECISIONS.md": "# Decisions made autonomously\n\n<!-- - [area] chose X over Y because Z -->\n",
        "QUESTIONS.md": "# Saved for the user (critical only)\n\n"
                        "<!-- ## Q1: question\n- Options: A / B\n- Recommended: A, because ...\n"
                        "- Built meanwhile: stub/flag/env var ... -->\n",
    }
    for name, body in templates.items():
        path = os.path.join(AB, name)
        if not os.path.exists(path):
            with open(path, "w") as fh:
                fh.write(body)
    with open(os.path.join(AB, "state.json"), "w") as fh:
        json.dump({"round": 0, "last_grade_mtime": 0, "best": None, "best_sha": None,
                   "no_improve": 0, "started": time.time()}, fh)
    open(os.path.join(AB, "active"), "w").close()

    print(f"INIT OK. stack: {', '.join(stack) or 'none detected (new project)'}")
    if git_note:
        print(f"  git: {git_note}")
    if gates:
        print("  baseline gate run:")
        os.environ["CLAUDE_PROJECT_DIR"] = ROOT
        sys.argv = ["score.py", "--gates-only"]
        sys.path.insert(0, AB)
        import importlib.util
        spec = importlib.util.spec_from_file_location("ab_score", os.path.join(AB, "score.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
        print("  Gates that already fail at baseline and are unrelated to the task may be removed "
              "now (log why in DECISIONS.md). Then run: python3 .autobuild/score.py --lock")
    else:
        print("  No gates detected. Once the stack is chosen in planning, add build/test gates "
              "to .autobuild/gates.json, then run: python3 .autobuild/score.py --lock")


if __name__ == "__main__":
    main()
