#!/usr/bin/env python3
"""Stop hook: the hard quality gate for /autobuild.

- Does nothing unless <project>/.autobuild/active exists.
- Only acts when the grader has written a NEW grade.json since the last verdict.
  Between grades (planning, waiting on background builders) turns may end;
  /goal is what keeps the session moving across those turns.
- On a new grade: scores the build (or reuses a fresh score.json), commits a git
  checkpoint, tracks the best score, and blocks the stop while failing. Feeds
  back what failed, and tells Claude to change strategy if it stalls or regresses.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}

root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
ab = os.path.join(root, ".autobuild")
grade_path, score_path, state_path = (os.path.join(ab, f) for f in ("grade.json", "score.json", "state.json"))


def allow():
    sys.exit(0)


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def sh(cmd):
    r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


if not os.path.exists(os.path.join(ab, "active")):
    allow()
if not os.path.exists(grade_path):
    allow()  # planning/building; nothing to gate yet

try:
    with open(os.path.join(ab, "gates.json")) as fh:
        cfg = json.load(fh)
except Exception:
    block("Autobuild is active but .autobuild/gates.json is missing or invalid. Re-run init.py.")

try:
    with open(state_path) as fh:
        state = json.load(fh)
except Exception:
    state = {}
state.setdefault("round", 0)
state.setdefault("last_grade_mtime", 0)
state.setdefault("no_improve", 0)

grade_mtime = os.path.getmtime(grade_path)
if grade_mtime <= float(state["last_grade_mtime"]):
    allow()  # no new grade since last verdict

scorer = os.path.join(ab, "score.py")
if not os.path.exists(scorer):
    scorer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score.py")

os.environ["CLAUDE_PROJECT_DIR"] = root
spec = importlib.util.spec_from_file_location("ab_score", scorer)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
fresh = (os.path.exists(score_path)
         and os.path.getmtime(score_path) >= grade_mtime
         and os.path.getmtime(score_path) >= mod.newest_source_mtime()
         and not json.load(open(score_path)).get("gates_only"))
if not fresh:
    subprocess.run([sys.executable, scorer, "--quiet"], cwd=root, capture_output=True, text=True)
try:
    with open(score_path) as fh:
        score = json.load(fh)
except Exception:
    block("Scorer failed to write .autobuild/score.json. Run `python3 .autobuild/score.py` and fix the error.")

overall = float(score.get("overall", 0))
sha = None
if shutil.which("git") and sh("git rev-parse --is-inside-work-tree")[0] == 0:
    sh("git add -A && git -c user.email=autobuild@local -c user.name=autobuild commit -q "
       f"--allow-empty -m 'autobuild checkpoint: score {overall}'")
    sha = sh("git rev-parse --short HEAD")[1] or None

best = state.get("best")
improved = best is None or overall > float(best) + 0.5
if improved:
    state["best"], state["best_sha"], state["no_improve"] = overall, sha, 0
else:
    state["no_improve"] += 1
state["last_grade_mtime"] = grade_mtime
passing = bool(score.get("passing"))
if not passing:
    state["round"] += 1
with open(state_path, "w") as fh:
    json.dump(state, fh)

if passing:
    block(f"Score PASSES ({overall} >= {score['threshold']}, checkpoint {sha}). Final step: present "
          "every item in .autobuild/QUESTIONS.md with AskUserQuestion (skip if none), apply the answers "
          "(another build+grade round if code changes), then `rm .autobuild/active` and give the final summary.")

max_rounds = int(cfg.get("max_rounds", 6))
if state["round"] >= max_rounds:
    block(f"Max fix rounds ({max_rounds}) reached. Final {overall}, best {state.get('best')} at commit "
          f"{state.get('best_sha')}. If the best commit beats the current code, restore it "
          f"(`git checkout {state.get('best_sha')} -- .`). Then present QUESTIONS.md with AskUserQuestion, "
          "list what still fails honestly in the final summary, and `rm .autobuild/active`.")

lines = [f"Score {overall} < {score['threshold']} (fix round {state['round']}/{max_rounds}, "
         f"best {state.get('best')}). Not done."]
if best is not None and overall < float(best) - 5:
    lines.append(f"REGRESSION: score fell from {best} (commit {state.get('best_sha')}). Run "
                 f"`git diff {state.get('best_sha')} --stat` and undo whatever made it worse before adding more.")
if state["no_improve"] >= 2:
    lines.append("STALLED: no improvement for 2 graded rounds. Change approach instead of repeating: "
                 "diagnose the top failure yourself (Opus), rewrite those tasks with a different design, "
                 "and dispatch them with model \"opus\".")
for prob in score.get("problems", []):
    lines.append(f"! {prob}")
if score.get("hard_failures"):
    lines.append("Hard gate failures: " + ", ".join(score["hard_failures"]))
for g in [g for g in score.get("gates", []) if not g["pass"]][:4]:
    lines.append(f"- {g['name']} (`{g['cmd']}`) exit {g['exit']}: {g['tail'][-350:]}")
if score.get("rubric_status") != "ok":
    lines.append(f"Rubric: {score.get('rubric_status')}.")
lines.append("Next: turn grade.json top_fixes + these failures into a fix wave, dispatch builders "
             "(escalate repeat failures one tier), then re-run the grader and score.py.")
block("\n".join(lines))
