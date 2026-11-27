# autobuild-kit

A Claude Code plugin for autonomous builds. Opus plans, parallel Sonnet/Haiku
subagents build, an Opus grader audits, and hooks block finishing until the
objective score passes. Critical decisions are batched for you at the end.

## Install on any computer (once per computer)

```bash
claude plugin marketplace add benduard/autobuild-kit
claude plugin install autobuild@autobuild-kit
```

Needs Claude Code, python3, and git. If the repo is private, run
`gh auth login` and then `gh auth setup-git` on that computer first.

## Get updates

```bash
claude plugin marketplace update autobuild-kit && claude plugin update autobuild@autobuild-kit
```

Or turn on auto-update once: `/plugin` > Marketplaces > autobuild-kit > Enable auto-update.

## Use it

```bash
cd your-project
claude --model opus --permission-mode auto
```

```
/goal Use the autobuild:build skill to build: <WHAT YOU WANT>. Done when python3 .autobuild/score.py prints PASS, every item in .autobuild/QUESTIONS.md has been asked with AskUserQuestion, and .autobuild/active is deleted. Or stop after 100 turns.
```

Or run it without the auto-continue loop: `/autobuild:build <what you want>`.

## What's inside

| Part | Job |
|---|---|
| `skills/build` | Opus playbook: spec, test-first, plan, routing, decisions, end-of-run questions |
| `agents/scout` (Haiku) | Read-only recon and research |
| `agents/builder-lite` (Haiku) | Boilerplate and small tasks |
| `agents/builder` (Sonnet) | Most build work (Opus when a task fails twice) |
| `agents/grader` (Opus) | Independent audit, writes the grade |
| `scripts/score.py` | Runs your real build/test/lint checks plus the grader's rubric and gives the official % |
| `scripts/gate.py` (Stop hook) | Won't let it finish below the pass mark. Saves git checkpoints, catches stalls and regressions |
| `scripts/guard.py` (PreToolUse hook) | Blocks push, publish, deploy, infra changes, deleting outside the project, and tampering with the score |

The hooks only do anything while a run is active (`.autobuild/active` exists),
so normal sessions aren't affected.

Tune per project in `.autobuild/gates.json`: `threshold` 90, `max_rounds` 6,
`rubric_weight` 0.4, `min_criterion` 70. You can raise these but not lower them once locked.
