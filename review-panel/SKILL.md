---
name: review-panel
description: "Empirically-gated diverse code review. 2 Codex (Domain+Integration) + Gemini + Opus skeptic emit STRUCTURED findings on a diff; an adjudicator re-runs every HIGH+ finding in a throwaway worktree and blocks ONLY on reproduced findings, never on model text. Use before committing a substantive code wave (physics, multi-file refactor, architecture/correctness changes)."
user-invocable: true
argument-hint: "<branch-or-base..head> --repo <path> --gate '<cmd>'"
---

# review-panel Skill

A diverse review panel whose findings are **hypotheses, not verdicts**. Reviewers cannot block on model text; an adjudicator empirically gates every HIGH+ finding by re-running it. This is the operational form of the principle "a panel finding is a hypothesis, not a verdict."

**Sibling to `duel`.** `duel` = Claude+Codex design *debate*, no code execution. `review-panel` = empirically-gated *code-diff review* (adds Gemini, structured findings, and an executing adjudicator). Reuse duel's agent-spawn patterns; do not duplicate it.

Use for substantive code waves only (physics/correctness/multi-file). For comment-only or doc changes, a single Codex pass is enough.

## What does NOT change
This skill **only calls** the existing engines; it never edits them:
- Codex reviewers via `~/.claude/skills/codex/run_with_watchdog.sh` (unchanged).
- Gemini reviewer via `~/.claude/skills/gemini/run_with_watchdog.sh` (unchanged).
- Opus skeptic via the `Agent` tool with `model: opus`.

The diversity invariant (Codex + Gemini + Opus, not one family) is load-bearing — keep all three families.

## Flow (the main session orchestrates)

### 1. Resolve inputs
- `REPO` (default: cwd), `RANGE` (e.g. `main..HEAD` or a branch), `GATE` (the project's full gate, e.g. `npm run typecheck && npx vitest run`).
- Build `DIFF=$(git -C $REPO diff $RANGE)` and the changed-file list. Put the diff in a file the reviewers read.

### 2. Auto-inject grounding (mandatory)
Every reviewer prompt is prepended with your project's rule docs (e.g. `CLAUDE.md` or equivalent), the active plan/design doc, any relevant design notes, and the diff. No reviewer is launched without the rule docs; this is not optional.

### 3. Spawn the panel (background, one watchdog call per agent)
Allocate a run dir per reviewer. Each reviewer is told: **emit ONLY a JSON object matching `~/.claude/skills/review-panel/findings.schema.json`.** Per-agent model/effort via the existing env knobs.
- **codex-domain** — `CODEX_MODEL=gpt-5.5 CODEX_REASONING=xhigh run_with_watchdog.sh <dir> --schema ~/.claude/skills/review-panel/findings.schema.json -C $REPO --skip-git-repo-check` ; prompt = per-deliverable craft/physics correctness.
- **codex-integration** — same; prompt = cross-file consistency, build chain, would-it-work-on-this-machine. (A cheaper/`medium` pass is fine here.)
- **gemini** — `GEMINI_MODEL=gemini-2.5-pro` gemini `run_with_watchdog.sh`; prompt = fresh-eye/alternative-mechanism; emit the same JSON shape in a fenced ```json block.
- **opus-skeptic** — `Agent(subagent_type: "code-reviewer", model: "opus", ...)`; prompt = argue against "done", verify on machine, mandatory pushback; emit the same JSON shape.

**Every HIGH+ finding MUST carry a `repro_command` + `expected_exit`** (a shell command run from the repo root that exits with `expected_exit` iff the finding holds — for a fix proposal: `git apply PATCH && <gate>` asserting it now passes), OR set `evidence_kind: not_reproducible` with the reason in `mechanism`. A HIGH+ with neither is downgraded to a nit by the adjudicator (it will not block).

### 4. Collect + merge findings
Read each reviewer's `output.md` (or events), extract its findings JSON, and merge into one array. Give each finding a stable `id` (e.g. `codex-domain-1`). Write `merged.json` = `{"findings":[...]}`.

### 5. Adjudicate (empirical gate)
```bash
~/.claude/skills/review-panel/adjudicate.sh \
  --findings merged.json --repo "$REPO" --ref "$(git -C "$REPO" rev-parse HEAD)" \
  --out results.json
```
It re-runs each HIGH+ `repro_command` in ONE hardened worktree (installs banned, caches redirected, node_modules read-shared, per-finding reset) and classifies `reproduced | refuted | not_run | not_run_justified`. It writes `results.json` (`summary.decision = BLOCK|PASS`, `blockers`, `nits`) and exits 1 on BLOCK.

### 6. Report
- **Blockers** = `reproduced` + `not_run_justified` only. Present these as what must be fixed before commit.
- **Nits** = `refuted` (a reviewer's HIGH the machine could not reproduce — call it out as a refuted hypothesis), `not_run`, and all MEDIUM/LOW.
- State `node_modules_mutated` if true (a repro misbehaved). NEVER let a `refuted` or bare `not_run` finding block.

## Hardening notes
- Reviewers are output-only; only the adjudicator executes, in a throwaway worktree (`git reset --hard` + `clean` per finding). Source mutations cannot reach the user's tree; `node_modules` writes are guarded (offline installs, redirected caches, manifest-hash assertion).
- Use the explicit codex sandbox form `--sandbox workspace-write -c approval_policy=never` if a reviewer must execute (prefer it over the undocumented `--full-auto`); but by default reviewers do NOT execute — the adjudicator does.
- Gemini may rate-limit (RESOURCE_EXHAUSTED 429). For high-stakes waves, do not ship without the third family: retry, switch Gemini model, or substitute a second Opus pass labelled "standing in for Gemini," and note the gap.

## Tests
`bash ~/.claude/skills/review-panel/tests/test_adjudicate.sh` — fixture proves a false-positive HIGH is refuted (nit), a model-text-only HIGH never blocks, a reproduced + a justified-non-reproducible HIGH block, MEDIUM is not gated, node_modules unmutated.
