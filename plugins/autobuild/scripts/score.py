#!/usr/bin/env python3
"""Official autobuild score. Copied into <project>/.autobuild/score.py by init.py.

overall = (1 - rubric_weight) * gate_score + rubric_weight * rubric_score

Passing needs ALL of:
  - gates are locked (score.py --lock) and none were weakened since
  - at least one gate exists, and every hard gate exits 0
  - a fresh grade.json (newer than the newest source file)
  - every acceptance criterion in SPEC.md was graded, none below min_criterion
  - overall >= threshold

The rubric is recomputed here from the grader's per-criterion scores, so the
grader's own arithmetic (or a hand-edited total) doesn't count.

Usage:
  python3 .autobuild/score.py              full score, writes score.json + history
  python3 .autobuild/score.py --gates-only quick check between waves
  python3 .autobuild/score.py --lock       freeze current gates (additions allowed later)
  add --quiet to suppress output
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
AB = os.path.join(ROOT, ".autobuild")
SKIP_DIRS = {".git", ".autobuild", ".claude", "node_modules", ".venv", "venv", "env",
             "__pycache__", "dist", "build", ".next", ".nuxt", "target", "coverage",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".turbo", ".cache"}
UNVERIFIED = re.compile(r"\b(not verified|unverified|could not|couldn't|unable to|did not run|didn't run)\b", re.I)


def p(path):
    return os.path.join(AB, path)


def load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def newest_source_mtime():
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                pass
    return newest


def spec_criteria_ids():
    """Numbered items under the first heading containing 'acceptance' (or anywhere if none)."""
    try:
        with open(p("SPEC.md")) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    start = next((i for i, l in enumerate(lines)
                  if l.lstrip().startswith("#") and "acceptance" in l.lower()), None)
    body = lines if start is None else lines[start + 1:]
    ids = []
    for l in body:
        if start is not None and l.lstrip().startswith("#"):
            break
        m = re.match(r"\s*(\d+)[.)]\s+\S", l)
        if m:
            ids.append(int(m.group(1)))
    return ids


def weakened_vs_lock(cfg, lock):
    probs = []
    if float(cfg.get("threshold", 0)) < float(lock.get("threshold", 0)):
        probs.append(f"threshold lowered {lock['threshold']} -> {cfg.get('threshold')}")
    if float(cfg.get("rubric_weight", 0.4)) != float(lock.get("rubric_weight", 0.4)):
        probs.append("rubric_weight changed")
    if float(cfg.get("min_criterion", 70)) < float(lock.get("min_criterion", 70)):
        probs.append("min_criterion lowered")
    cur = {g["name"]: g for g in cfg.get("gates", [])}
    for name, lg in lock.get("gates", {}).items():
        g = cur.get(name)
        if g is None:
            probs.append(f"gate '{name}' removed")
            continue
        if g.get("cmd") != lg["cmd"]:
            probs.append(f"gate '{name}' command changed")
        if float(g.get("weight", 1)) < float(lg["weight"]):
            probs.append(f"gate '{name}' weight lowered")
        if lg["hard"] and not g.get("hard"):
            probs.append(f"gate '{name}' no longer hard")
    return probs


def do_lock(cfg, quiet):
    lock = load_json(p("gates.lock"))
    if lock:
        probs = weakened_vs_lock(cfg, lock)
        if probs:
            print("REFUSED: gates weaker than the existing lock: " + "; ".join(probs))
            sys.exit(1)
    if not cfg.get("gates"):
        print("REFUSED: no gates defined. Add at least a build or test gate first.")
        sys.exit(1)
    new = {
        "threshold": cfg.get("threshold", 90),
        "rubric_weight": cfg.get("rubric_weight", 0.4),
        "min_criterion": cfg.get("min_criterion", 70),
        "acceptance_dir": cfg.get("acceptance_dir", ""),
        "gates": {g["name"]: {"cmd": g["cmd"], "weight": float(g.get("weight", 1)),
                              "hard": bool(g.get("hard"))} for g in cfg["gates"]},
        "at": time.time(),
    }
    with open(p("gates.lock"), "w") as fh:
        json.dump(new, fh, indent=2)
    if not quiet:
        print(f"LOCKED {len(new['gates'])} gates: {', '.join(new['gates'])}")


def last_int(pattern, text):
    if not pattern:
        return None
    hits = re.findall(pattern, text)
    if not hits:
        return None
    h = hits[-1]
    return int(h[0] if isinstance(h, tuple) else h)


def run_gate(g, timeout):
    start = time.time()
    try:
        proc = subprocess.run(g["cmd"], shell=True, cwd=ROOT, capture_output=True,
                              text=True, timeout=timeout)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        out, code = f"TIMEOUT after {timeout}s", 124
    ok = code == 0
    score = 1.0 if ok else 0.0
    passed = last_int(g.get("passed_regex"), out)
    failed = last_int(g.get("failed_regex"), out)
    if passed is not None or failed is not None:
        pa, fa = passed or 0, failed or 0
        if pa + fa > 0:
            score = pa / (pa + fa)
            if not ok:
                score = min(score, 0.99)  # nonzero exit can never be full marks
    return {"name": g["name"], "cmd": g["cmd"], "hard": bool(g.get("hard")),
            "weight": float(g.get("weight", 1)), "exit": code, "pass": ok,
            "score": round(score, 4), "secs": round(time.time() - start, 1),
            "tail": "\n".join(out.strip().splitlines()[-15:])}


def rubric_from_grade(grade, spec_ids):
    crit = {}
    for c in grade.get("criteria", []):
        try:
            cid, s = int(c.get("id")), float(c.get("score", 0))
        except (TypeError, ValueError):
            continue
        ev = str(c.get("evidence", "")).strip()
        if not ev or UNVERIFIED.search(ev):
            s = min(s, 50.0)
        crit[cid] = max(0.0, min(100.0, s))
    ids = spec_ids or sorted(crit)
    missing = [i for i in ids if i not in crit]
    scores = [crit.get(i, 0.0) for i in ids]
    avg = sum(scores) / len(scores) if scores else 0.0
    issues = grade.get("issues", [])
    high = sum(1 for i in issues if str(i.get("severity", "")).lower() == "high")
    med = sum(1 for i in issues if str(i.get("severity", "")).lower() == "medium")
    rubric = max(0.0, avg - 5 * high - 2 * med)
    per = {i: crit.get(i, 0.0) for i in ids}
    return round(rubric, 1), per, missing, high, med


def main():
    quiet = "--quiet" in sys.argv
    gates_only = "--gates-only" in sys.argv
    cfg = load_json(p("gates.json"))
    if cfg is None:
        print("ERROR: .autobuild/gates.json missing or invalid")
        sys.exit(2)
    if "--lock" in sys.argv:
        do_lock(cfg, quiet)
        return

    problems = []
    lock = load_json(p("gates.lock"))
    if not lock:
        problems.append("gates not locked (run score.py --lock)")
    else:
        problems += weakened_vs_lock(cfg, lock)
    if not cfg.get("gates"):
        problems.append("no gates defined")

    results = [run_gate(g, int(cfg.get("gate_timeout_sec", 600))) for g in cfg.get("gates", [])]
    total_w = sum(r["weight"] for r in results) or 1.0
    gate_score = 100.0 * sum(r["score"] * r["weight"] for r in results) / total_w if results else 0.0
    hard_fail = [r["name"] for r in results if r["hard"] and not r["pass"]]

    spec_ids = spec_criteria_ids()
    rubric, per, missing, high, med, status = 0.0, {}, [], 0, 0, "missing"
    grade = load_json(p("grade.json"))
    if grade is not None and not gates_only:
        rubric, per, missing, high, med = rubric_from_grade(grade, spec_ids)
        status = "ok"
        if os.path.getmtime(p("grade.json")) + 1 < newest_source_mtime():
            rubric, status = 0.0, "stale (code changed after grading - re-run grader)"
    elif grade is None and not gates_only:
        status = "missing (run the grader)"
    min_c = float(cfg.get("min_criterion", 70))
    low = [i for i, s in per.items() if s < min_c] if status == "ok" else []

    w = float(cfg.get("rubric_weight", 0.4))
    overall = gate_score if gates_only else (1 - w) * gate_score + w * rubric
    threshold = float(cfg.get("threshold", 90))
    if gates_only:
        passing = not hard_fail and bool(results) and gate_score >= threshold
    else:
        if not spec_ids:
            problems.append("SPEC.md has no numbered acceptance criteria")
        if missing:
            problems.append(f"criteria not graded: {missing}")
        if low:
            problems.append(f"criteria below {min_c:g}: {low}")
        passing = (not problems and not hard_fail and status == "ok" and overall >= threshold)

    report = {"overall": round(overall, 1), "threshold": threshold, "passing": passing,
              "gate_score": round(gate_score, 1), "rubric_score": rubric,
              "rubric_status": status, "criteria": per, "high_issues": high,
              "medium_issues": med, "hard_failures": hard_fail, "problems": problems,
              "gates": results, "gates_only": gates_only, "at": time.time()}
    if not gates_only:
        with open(p("score.json"), "w") as fh:
            json.dump(report, fh, indent=2)
        with open(p("history.jsonl"), "a") as fh:
            fh.write(json.dumps({k: report[k] for k in
                                 ("at", "overall", "gate_score", "rubric_score", "passing")}) + "\n")

    if not quiet:
        tag = "GATES-ONLY " if gates_only else ""
        print(f"{tag}OVERALL {report['overall']} / threshold {threshold:g} -> {'PASS' if passing else 'FAIL'}")
        if not gates_only:
            print(f"  gates {report['gate_score']} | rubric {rubric} ({status}) | "
                  f"high issues {high}, medium {med}")
            if per:
                print("  criteria: " + ", ".join(f"#{i}={s:g}" for i, s in per.items()))
        for prob in problems:
            print(f"  ! {prob}")
        for r in results:
            print(f"  - {r['name']}{' [hard]' if r['hard'] else ''}: "
                  f"{'PASS' if r['pass'] else 'FAIL'} score={r['score']} ({r['secs']}s)")
            if not r["pass"]:
                print("    " + r["tail"].replace("\n", "\n    "))
    return report


if __name__ == "__main__":
    main()
