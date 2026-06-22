---
name: review-panel
description: "Empirically-gated diverse code review. 2 Codex (Domain+Integration) + Gemini + Opus skeptic emit STRUCTURED findings on a diff; an adjudicator re-runs every HIGH+ finding in a throwaway worktree and blocks ONLY on reproduced findings, never on model text. Use before committing a substantive code wave (physics, multi-file refactor, architecture/correctness changes)."
user-invocable: true
argument-hint: "<branch-or-base..head> --repo <path> --gate '<cmd>'"
---

# review-panel Skill

A diverse review panel whose findings are **hypotheses, not verdicts**. Reviewers cannot block on model text; an adjudicator empirically gates every HIGH+ finding by re-running it. This is the operational form of the CLAUDE.md rule "a panel finding is a hypothesis, not a verdict."

**Sibling to `duel`.** `duel` = Claude+Codex design *debate*, no code execution. `review-panel` = empirically-gated *code-diff review* (adds Gemini, structured findings, and an executing adjudicator). Reuse duel's agent-spawn patterns; do not duplicate it.

Use for substantive code waves only (physics/correctness/multi-file). For comment-only or doc changes, a single Codex pass is enough.

## What does NOT change
This skill **only calls** the existing engines; it never edits them:
- Codex reviewers via `~/.claude/skills/codex/run_with_watchdog.sh` (unchanged).
- Gemini reviewer via `~/.claude/skills/gemini/run_with_watchdog.sh` (unchanged; its engine is now Antigravity `agy` since gemini-cli was retired 2026-06-18 — the call site is identical).
- Grok + Composer reviewers via `~/.claude/skills/grok/run_with_watchdog.sh` (xAI `grok` CLI; `GROK_MODEL=grok-build` for the grok seat, `GROK_MODEL=grok-composer-2.5-fast` for the composer seat).
- Opus skeptic via the `Agent` tool with `model: opus`.

The diversity invariant (≥3 distinct families, never one family self-checking) is load-bearing — the floor stays Codex + Gemini + Opus(anthropic). The Gemini seat is a Google-family model served through `agy`; do not swap it for agy's Claude/GPT-OSS models, which would collapse the invariant. Grok (xAI) and Composer (Cursor/Kimi) add review BREADTH as two seats, but they share one grok.com account/proxy (a single independence + outage domain), so they map to ONE family token (`grok`) for the diversity gate and cannot substitute for the codex+gemini+anthropic floor.

## Flow (the main session orchestrates)

### 1. Resolve inputs
- `REPO` (default: cwd), `RANGE` (e.g. `main..HEAD` or a branch), `GATE` (the project's full gate, e.g. `npm run typecheck && npx vitest run`).
- Build `DIFF=$(git -C $REPO diff $RANGE)` and the changed-file list. Put the diff in a file the reviewers read.

### 2. Auto-inject grounding (mandatory — mechanizes feedback_codex_grounding_mandatory)
Every reviewer prompt is prepended with: `~/.claude/CLAUDE.md`, the active plan doc, the relevant memory file(s), and the diff. No reviewer is launched without the rule docs; this is not optional.

### 3. Spawn the panel (background, one watchdog call per agent)
Allocate a run dir per reviewer. Each reviewer is told: **emit ONLY a JSON object matching `~/.claude/skills/review-panel/findings.schema.json`.** Per-agent model/effort via the existing env knobs.
- **codex-domain** — `CODEX_MODEL=gpt-5.5 CODEX_REASONING=xhigh run_with_watchdog.sh <dir> --output-schema ~/.claude/skills/review-panel/findings.schema.json -C $REPO --skip-git-repo-check` ; prompt = per-deliverable craft/physics correctness. (`--output-schema` is codex's flag; the raw watchdog forwards it verbatim. Prompt-instructed JSON also works without it.)
- **codex-integration** — same; prompt = cross-file consistency, build chain, would-it-work-on-this-machine. (A cheaper/`medium` pass is fine here.)
- **gemini** — `GEMINI_MODEL="Gemini 3.5 Flash (High)"` gemini `run_with_watchdog.sh` (agy-backed; use `"Gemini 3.1 Pro (High)"` for the heaviest waves); prompt = fresh-eye/alternative-mechanism; emit the same JSON shape in a fenced ```json block.
- **grok** — `GROK_MODEL=grok-build GROK_EFFORT=max GROK_WATCHDOG_CWD=$REPO ~/.claude/skills/grok/run_with_watchdog.sh <dir>` (xAI; read-only by default — reviewers don't execute, the adjudicator does); prompt = fresh-eye/alternative-mechanism, a distinct lens from gemini; emit the same JSON shape in a fenced ```json block.
- **composer** — same call with `GROK_MODEL=grok-composer-2.5-fast` (Cursor Composer 2.5); prompt = another independent lens; emit the same JSON shape.
- **opus-skeptic** — `Agent(subagent_type: "code-reviewer", model: "opus", ...)`; prompt = argue against "done", verify on machine, mandatory pushback; emit the same JSON shape.

**Every HIGH+ finding MUST carry a `repro_command` + `expected_exit`** (a shell command run from the repo root that exits with `expected_exit` iff the finding holds — for a fix proposal: `git apply PATCH && <gate>` asserting it now passes), OR set `evidence_kind: not_reproducible` with the reason in `mechanism`. A HIGH+ with neither is downgraded to a nit by the adjudicator (it will not block).

### 4. Collect + merge findings
Read each reviewer's `output.md` (or events), extract its findings JSON, and merge into one array. Give each finding a stable `id` (e.g. `codex-domain-1`). Write `merged.json` = `{"findings":[...]}`.

### 5. Adjudicate (empirical gate + family preflight)
First compute the distinct model **families** that produced VALID output — a seat counts only
if its run reached `status=done` with non-empty, parseable findings JSON (the watchdogs'
empty-output gates make "valid" honest). Map roles to families: `codex-domain` +
`codex-integration` → `codex`; `gemini` → `gemini`; `opus-skeptic` → `anthropic`; and BOTH
`grok` AND `composer` → `grok` (the SAME family token). They run as two seats for finding
breadth, but they share ONE grok.com account/proxy — a single independence domain — so they
count as ONE family for the diversity gate. This MECHANICALLY blocks a weak `codex,grok,composer`
panel (= 2 distinct families → PROVISIONAL) from clearing the 3-family floor without a real third
family (gemini or anthropic). Pass the DISTINCT family set; the adjudicator dedupes it.
```bash
~/.claude/skills/review-panel/adjudicate.sh \
  --findings merged.json --repo "$REPO" --ref "$(git -C "$REPO" rev-parse HEAD)" \
  --out results.json \
  --families "codex,gemini,anthropic,grok"   # DISTINCT families with VALID output; grok+composer both map to "grok" (one infra), so list it once
```
It re-runs each HIGH+ `repro_command` in ONE hardened worktree (installs banned, caches redirected, node_modules read-shared, per-finding reset) and classifies `reproduced | refuted | not_run | not_run_justified`. It writes `results.json` (`summary.decision = BLOCK|PROVISIONAL|PASS`, `blockers`, `nits`, `families_present`, `diversity_ok`) and exits **1 on BLOCK, 3 on PROVISIONAL, 0 on PASS**.

**The family preflight (audit 1a/1d) is automatic.** If fewer than 3 families produced valid output (e.g. the Gemini seat hit quota/timeout), the decision is `PROVISIONAL` — a clean bill of health from 2 families is NOT gate-eligible. This replaces the old advisory-only "don't ship without the third family" with an enforced gate.

### 6. Report
- **Blockers** = `reproduced` + `not_run_justified` only. Present these as what must be fixed before commit.
- **PROVISIONAL** (`decision=PROVISIONAL`, `diversity_ok=false`) = the panel ran with fewer than 3 families. Report it as NOT gate-eligible: the increment may commit as provisional, but it is barred from the terminal gate until a full 3-family panel re-clears it. State which families were missing.
- **Nits** = `refuted` (a reviewer's HIGH the machine could not reproduce — call it out as a refuted hypothesis), `not_run`, and all MEDIUM/LOW.
- State `node_modules_mutated` if true (a repro misbehaved). NEVER let a `refuted` or bare `not_run` finding block.

## Hardening notes
- Reviewers are output-only; only the adjudicator executes, in a throwaway worktree (`git reset --hard` + `clean` per finding). Source mutations cannot reach the user's tree; `node_modules` writes are guarded (offline installs, redirected caches, manifest-hash assertion).
- Use the explicit codex sandbox form `--sandbox workspace-write -c approval_policy=never` if a reviewer must execute (prefer it over the undocumented `--full-auto`); but by default reviewers do NOT execute — the adjudicator does.
- Gemini-via-agy can error on auth/quota (`RESOURCE_EXHAUSTED`, `IneligibleTier`, `UNSUPPORTED_CLIENT`) or time out. When it does, the family preflight in step 5 makes the result **PROVISIONAL automatically** — you no longer rely on remembering to check. To restore full diversity: retry; or note that Antigravity quota is **account-wide** (a Flash↔Pro tier switch will NOT help under quota — wait for reset); or substitute a second Opus pass labelled "standing in for Gemini" — but a substituted same-family pass does NOT restore the 3rd family, so the result stays PROVISIONAL until a real Gemini (or other third family) re-clears it. (The watchdog also self-heals any stale gemini-cli `GEMINI_MODEL` id by remapping it to the agy default.)

## Tests
`bash ~/.claude/skills/review-panel/tests/test_adjudicate.sh` — fixture proves a false-positive HIGH is refuted (nit), a model-text-only HIGH never blocks, a reproduced + a justified-non-reproducible HIGH block, MEDIUM is not gated, node_modules unmutated; plus the family preflight: < 3 families ⇒ PROVISIONAL (exit 3), 3 families ⇒ PASS, no `--families` ⇒ legacy PASS, and a real blocker outranks PROVISIONAL (BLOCK wins).
