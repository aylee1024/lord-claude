<div align="center">

# `Lord` **Claude** 👑

### *Claude Code cracks the whip. Codex, Gemini, Grok & Composer do the work.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![codex-cli ≥ 0.125](https://img.shields.io/badge/codex--cli-%E2%89%A50.125-1f6feb)](https://github.com/openai/codex)
[![Antigravity agy ≥ 1.0](https://img.shields.io/badge/Antigravity%20agy-%E2%89%A51.0-4285f4)](https://antigravity.google)
[![xAI grok CLI](https://img.shields.io/badge/xAI%20grok-CLI-111111)](https://x.ai)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-d97706)](https://docs.claude.com/en/docs/claude-code/skills)
[![Shell: bash](https://img.shields.io/badge/shell-bash-4eaa25)](https://www.gnu.org/software/bash/)

</div>

```
          ♛  L O R D   C L A U D E  ♛
           ( o_o )
          /(  |  )╮                                   ┌──────────────────────────────────┐
            /  \  ╰═≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈ »S N A P!« ═▶│  CODEX    · gpt-5.5 · xhigh      │  ...grind...
                                                      │  GEMINI   · agy · Gemini Flash   │  ...grind...
                                                      │  GROK     · grok-build · max     │  ...grind...
                                                      │  COMPOSER · Composer 2.5 · max   │  ...grind...
                                                      └──────────────────────────────────┘
       …all five vassals run under run_with_watchdog.sh — no hangs, no orphans, no excuses.
```

Five Claude Code skills that make OpenAI **Codex**, Google **Gemini**, xAI **Grok**, and Cursor **Composer** behave like obedient Claude subagents. You type `/codex`, `/gemini`, `/grok`, or `/composer`; the lord delegates the sub-task, a watchdog stands over the CLI with a whip, and the result comes back clean. `/review-panel` summons a diverse set at once to review your diff — then an adjudicator **re-runs every finding** and blocks the commit only on the ones it can reproduce.

> Bare `codex exec`, `agy --print`, and `grok -p` have sharp edges: startup auth hangs, silent model-drift across versions, `cmd &` orphans the harness never hears back from, parallel runs clobbering one output file. The whip is `run_with_watchdog.sh` — per-run dir, a status file you can poll, stderr fast-fail, one retry. It turns *"the CLI usually works"* into *"the CLI always works the same way."*

## The five vassals

| Command | What it does | Engine under the whip |
|---|---|---|
| `/codex <prompt>` | one delegated sub-task, supervised | Codex CLI · `gpt-5.5` · `xhigh` |
| `/gemini <prompt>` | one delegated sub-task, supervised | Antigravity `agy --print` · `Gemini 3.5 Flash` |
| `/grok <prompt>` | one delegated sub-task, supervised | xAI `grok` · `grok-build` · `--effort max` |
| `/composer <prompt>` | one delegated sub-task, supervised | Cursor **Composer 2.5** via the `grok` CLI · `grok-composer-2.5-fast` |
| `/review-panel <diff>` | Codex + Gemini + Grok + Composer + an Opus skeptic review in parallel, then an adjudicator re-runs every HIGH+ finding in a throwaway `git worktree` and **blocks only on reproductions, never on prose** | the whole court at once |

> 🪶 **The Gemini vassal serves through [Antigravity](https://antigravity.google) `agy`.** Google retired Gemini CLI's free OAuth tier (2026-06-18). The `/gemini` skill keeps its name, run-dir contract, and `GEMINI_MODEL` knob — only the engine swapped to `agy --print`, serving the same Google Gemini models. Callers, `/review-panel` included, need no changes.

> ⚡ **Grok and Composer share one CLI.** The installed xAI `grok` binary serves *both* `grok-build` (xAI's coding model) and `grok-composer-2.5-fast` (Cursor's Composer 2.5). So `/grok` and `/composer` ride **one** model-parameterized watchdog (`composer` is `grok` with `GROK_MODEL=grok-composer-2.5-fast`). Both run **read-only by default** under an OS-enforced sandbox (macOS Seatbelt) — a delegated review can't write your tree; pass `--full-auto` to allow writes, `--isolate` to confine them to a throwaway worktree. Because they share one grok.com account, `/review-panel` counts them as **one** family for its diversity gate (breadth, not a substitute for the Codex+Gemini+Opus floor).

## Swear in the vassals (install)

Clone once, symlink all five:

```bash
git clone https://github.com/aylee1024/lord-claude.git
mkdir -p ~/.claude/skills
for s in codex gemini grok composer review-panel; do ln -s "$PWD/lord-claude/$s" ~/.claude/skills/$s; done
```

<details><summary>…or <code>curl</code> a single skill into place</summary>

```bash
# Codex (swap "codex"→"gemini" or "grok" for those skills — same file set)
mkdir -p ~/.claude/skills/codex && cd ~/.claude/skills/codex
for f in SKILL.md run_with_watchdog.sh status.sh prune_old_runs.sh; do
  curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/codex/$f" -o "$f"
done && chmod +x *.sh

# Composer is SKILL.md only — it reuses the grok watchdog, so install grok too.
mkdir -p ~/.claude/skills/composer && cd ~/.claude/skills/composer
curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/composer/SKILL.md" -o SKILL.md

# review-panel (orchestrates the others)
mkdir -p ~/.claude/skills/review-panel/tests && cd ~/.claude/skills/review-panel
for f in SKILL.md adjudicate.sh findings.schema.json; do
  curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/review-panel/$f" -o "$f"
done
curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/review-panel/tests/test_adjudicate.sh" -o tests/test_adjudicate.sh
chmod +x adjudicate.sh tests/test_adjudicate.sh
```

</details>

**Prereqs:** Claude Code · macOS/Linux with `bash` · **Codex CLI ≥ 0.125** (`codex login`) for `/codex` · **Antigravity `agy` ≥ 1.0** (`agy --print "ok"`) for `/gemini` · **xAI `grok` CLI** (`grok login`, or `XAI_API_KEY`) for `/grok` and `/composer` · `git` for `/review-panel` and `--isolate`.

## Crack the whip (use)

```
/codex     Find race conditions in ./src/server.py.
/gemini    Summarize the debate in ./DESIGN.md in five bullets.
/grok      Audit ./auth for logic bugs (read-only).
/composer  Refactor ./ui/cart.tsx; --full-auto --isolate.
/review-panel main..HEAD --repo . --gate 'npm run typecheck && npx vitest run'
```

**Parallel review-wave?** Fire **one Bash call per agent** with `run_in_background: true` — each gets its own `/tmp/<skill>_runs/<id>/`, and Claude Code gets one completion notification per finisher, so you can read the first while the rest grind:

```bash
~/.claude/skills/grok/run_with_watchdog.sh /tmp/grok_runs/grok_subjectA
```

Poll a run with `status.sh`, continue one with `/grok --resume …`, and reap old runs with `prune_old_runs.sh`. The anti-pattern to avoid — a single Bash call backgrounding N `nohup` processes — is documented in the SKILL files (the harness sees only one notification and the whip can't supervise them).

> **Worktree isolation.** For risky `--full-auto` builds, add `--isolate`: the vassal runs inside a throwaway `git worktree` so it physically can't touch your live tree (the lord protects the realm). Fails *closed* — if it can't make a clean worktree, it refuses rather than run loose. For `/grok` and `/composer` the confinement is OS-enforced (Seatbelt), and caller flags that would rebind the workspace (`--cwd`/`--sandbox`/`--worktree`) are refused.

## Why the vassals never break loose

| The CLI might… | The whip… |
|---|---|
| hang at startup on expired auth / MCP `tools/list` timeout / a wedged backend | fast-fails on stderr patterns and kills within one poll cycle, then retries |
| drift to a different default model across versions | plumbs the model explicitly (and validates it — Gemini against `agy models`, Grok against `~/.grok/models_cache.json`) |
| detach via `cmd &` so the harness never hears back | runs the CLI as a foreground child it supervises, so completion notifications actually fire |
| collide when run in parallel on one output file | gives every run its own `/tmp/<skill>_runs/<id>/` |

Each run leaves a full paper trail (`output.md`, `stderr.log`, `watchdog.log`, `status`, …). Full per-skill design — hang thresholds, fast-fail patterns, sandbox modes, each CLI's quirks — lives in [`codex/SKILL.md`](./codex/SKILL.md), [`gemini/SKILL.md`](./gemini/SKILL.md), [`grok/SKILL.md`](./grok/SKILL.md), [`composer/SKILL.md`](./composer/SKILL.md), and [`review-panel/SKILL.md`](./review-panel/SKILL.md).

## Compatibility

Verified on macOS (Apple Silicon): codex-cli 0.125+, Antigravity `agy` 1.0.9+, xAI `grok` 0.2.60+. Should run on Linux — POSIX `bash` 3.2+ and BSD-compatible `ps` (the Grok/Composer sandbox uses Seatbelt on macOS / Landlock on Linux). If a CLI ships breaking flag/output changes, the watchdog reports via `watchdog.log` / `stderr.log` instead of failing silently.

## License

[MIT](./LICENSE) — use it freely. Long live the Lord. 👑
