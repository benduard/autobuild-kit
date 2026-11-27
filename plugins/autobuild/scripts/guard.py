#!/usr/bin/env python3
"""PreToolUse guard for /autobuild. Active only while <project>/.autobuild/active exists.

Makes unattended runs safe and the score hard to game:
  1. Blocks irreversible / outward-facing actions (push, publish, deploy, PR/release,
     infra apply, deleting outside the project). Claude is told to log it in
     QUESTIONS.md and build around it.
  2. Blocks tampering with the scoring machinery (lock, scorer, score, history,
     state, hooks, settings).
  3. Only the grader may write grade.json.
  4. After gates are locked, builder subagents can't edit acceptance tests.
"""
import json
import os
import re
import shlex
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

root = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
ab = os.path.join(root, ".autobuild")
if not os.path.exists(os.path.join(ab, "active")):
    sys.exit(0)

tool = data.get("tool_name", "")
inp = data.get("tool_input", {}) or {}
agent = (data.get("agent_type") or "").lower().split(":")[-1]  # plugin agents arrive as autobuild:grader


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "[autobuild guard] " + reason}}))
    sys.exit(0)


CRITICAL = "This is a critical action reserved for the user. Add it to .autobuild/QUESTIONS.md " \
           "(question, options, your recommendation, what you built meanwhile), build around it, and keep going."

DANGEROUS = [
    (r"\bgit\s+push\b", "git push"),
    (r"\b(npm|pnpm|yarn|bun)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b", "publishing a package"),
    (r"\bvercel\b[^|;&]*--prod\b|\bnetlify\s+deploy\b[^|;&]*--prod\b|\bfly(ctl)?\s+deploy\b|"
     r"\bfirebase\s+deploy\b|\bwrangler\s+(deploy|publish)\b|\brailway\s+up\b|\bheroku\s+\S*(deploy|release)",
     "deploying"),
    (r"\bterraform\s+(apply|destroy)\b|\bpulumi\s+(up|destroy)\b|\bkubectl\s+(apply|delete|create)\b|"
     r"\bhelm\s+(install|upgrade|uninstall)\b|\baws\s+\S+\s+(create|delete|put|run|terminate)", "changing cloud infrastructure"),
    (r"\bgh\s+(pr\s+(create|merge)|release\s+create|repo\s+(create|delete))\b", "creating PRs/releases/repos"),
    (r"\bgit\s+(branch\s+-D|reset\s+--hard\s+\S*origin)|\bgit\s+clean\s+-\S*f", "destructive git operation"),
    (r"\b(stripe|twilio|sendgrid)\b[^|;&]*\b(create|send|charge|post)\b", "a paid/outbound API call"),
]

PROTECTED = [".autobuild/gates.lock", ".autobuild/score.py", ".autobuild/score.json",
             ".autobuild/history.jsonl", ".autobuild/state.json", ".claude/hooks/",
             ".claude/settings.json", ".claude/settings.local.json"]
PLUGIN_ROOT = os.path.realpath(os.environ.get("CLAUDE_PLUGIN_ROOT", "")) if os.environ.get("CLAUDE_PLUGIN_ROOT") else None
READONLY_CMD = re.compile(r"^\s*(cat|less|head|tail|ls|wc|jq|grep|rg|git\s+(diff|log|show|status))\b")
SCORER_CMD = re.compile(r"^\s*python3?\s+\.autobuild/score\.py(\s+--(gates-only|lock|quiet))*\s*$")
INIT_CMD = re.compile(r"^\s*python3?\s+\"?[^\s\";&|<>]*/scripts/init\.py\"?(\s+\"[^\"]*\"|\s+[^\s;&|<>]+)*\s*$")


def rel(path):
    ap = os.path.realpath(os.path.join(root, os.path.expanduser(path)))
    return os.path.relpath(ap, root).replace(os.sep, "/"), ap


def is_protected(r, ap):
    home_claude = os.path.realpath(os.path.expanduser("~/.claude"))
    if PLUGIN_ROOT and (ap == PLUGIN_ROOT or ap.startswith(PLUGIN_ROOT + os.sep)):
        return True
    if ap.startswith(os.path.join(home_claude, "plugins")) or \
       ap.startswith(os.path.join(home_claude, "hooks")) or \
       ap == os.path.join(home_claude, "settings.json"):
        return True
    return any(r == pth or r.startswith(pth) or ("/" + pth) in ("/" + r) for pth in PROTECTED)


def acceptance_dir():
    try:
        lock = json.load(open(os.path.join(ab, "gates.lock")))
    except Exception:
        return None  # not locked yet -> tests may still be written
    d = (lock.get("acceptance_dir") or "").strip().strip("/")
    return d or None


def check_path(path):
    r, ap = rel(path)
    if is_protected(r, ap):
        deny(f"{r} is part of the scoring machinery and can't be modified during a run.")
    if r == ".autobuild/grade.json" and agent != "grader":
        deny("Only the grader subagent writes .autobuild/grade.json.")
    acc = acceptance_dir()
    if acc and (r == acc or r.startswith(acc + "/")) and agent in ("builder", "builder-lite"):
        deny(f"Acceptance tests in {acc}/ are locked for builders. If a test is genuinely wrong, "
             "report it; the orchestrator decides and logs it in DECISIONS.md.")


if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if path:
        check_path(path)
    sys.exit(0)

if tool == "Bash":
    cmd = inp.get("command", "") or ""
    for pat, what in DANGEROUS:
        if re.search(pat, cmd, re.I):
            deny(f"Blocked {what}. {CRITICAL}")
    if SCORER_CMD.match(cmd) or INIT_CMD.match(cmd):
        sys.exit(0)
    touches = [pth for pth in PROTECTED + [".autobuild/grade.json"] if pth.rstrip("/") in cmd]
    if PLUGIN_ROOT and PLUGIN_ROOT in cmd:
        touches.append("plugin files")
    if touches and not READONLY_CMD.match(cmd):
        if touches == [".autobuild/grade.json"] and agent == "grader":
            pass
        else:
            deny(f"Command touches protected autobuild file(s) {touches}. Read-only commands "
                 "(cat, jq, grep, git diff) and `python3 .autobuild/score.py [--gates-only|--lock]` are allowed.")
    # rm -r / mv / deletes aimed outside the project
    for segment in re.split(r"[;&|]+", cmd):
        try:
            toks = shlex.split(segment)
        except ValueError:
            continue
        if not toks:
            continue
        base = os.path.basename(toks[0])
        if base in ("sudo",) and len(toks) > 1:
            toks, base = toks[1:], os.path.basename(toks[1])
        if base in ("rm", "rmdir", "shred", "unlink"):
            for t in toks[1:]:
                if t.startswith("-"):
                    continue
                _, ap = rel(t)
                if ap != root and not ap.startswith(root + os.sep):
                    deny(f"Deleting {t} is outside the project. {CRITICAL}")
                if ap == root:
                    deny("Deleting the whole project directory is blocked.")
sys.exit(0)
