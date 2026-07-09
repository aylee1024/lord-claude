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
            /  \  ╰═≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈ »S N A P!« ═▶│  CODEX    · gpt-5.6-sol · ultra      │  ...grind...
                                                      │  GEMINI   · agy · Gemini Flash   │  ...grind...
                                                      │  GROK     · grok-build · max     │  ...grind...
                                                      │  COMPOSER · Composer 2.5 · max   │  ...grind...
                                                      └──────────────────────────────────┘
       …all five vassals run under run_with_watchdog.sh — no hangs, no orphans, no excuses.
```

Five Claude Code skills that make OpenAI **Codex**, Google **Gemini**, xAI **Grok**, and Cursor **Composer** behave like obedient Claude subagents. You type `/codex`, `/gemini`, `/grok`, or `/composer`; the lord delegates the sub-task, a watchdog stands over the CLI with a whip, and the result comes back clean. `/review-panel` summons a diverse set at once to review your diff — then an adjudicator **re-runs every finding** and blocks the commit only on the ones it can reproduce. And when one task isn't enough, [`agents`](#keep-a-vassal-at-your-side-live-sessions) keeps a vassal **warm at your side** — a standing conversation it remembers turn to turn, just like messaging Claude's own subagents.

> Bare `codex exec`, `agy --print`, and `grok -p` have sharp edges: startup auth hangs, silent model-drift across versions, `cmd &` orphans the harness never hears back from, parallel runs clobbering one output file. The whip is `run_with_watchdog.sh` — per-run dir, a status file you can poll, stderr fast-fail, one retry. It turns *"the CLI usually works"* into *"the CLI always works the same way."*

## ✦ What the court can do

- **🗡 Five rival engines, one harness.** Codex, Gemini, Grok, and Composer (plus an Opus skeptic in panels) all answer to the same supervised contract — so each one *always works the same way*, not "usually."
- **💬 Message them like Claude's own subagents.** `agents` holds a **warm, multi-turn session** with any engine: address it by handle, it stays alive in memory and remembers the whole thread. One verb-set (`start · send · read · cancel · list · stop · board`) for every model — with **live streaming, mid-turn cancel, schema-validated JSON, broadcast, an agent-to-agent blackboard, and reattach-with-memory** all on tap.
- **⚖ A review court that checks itself.** `/review-panel` runs a diverse set in parallel, then an adjudicator **re-runs every HIGH+ finding in a throwaway `git worktree`** and blocks the commit *only on what it can reproduce* — never on confident prose.
- **🛡 Read-only by default; real isolation on demand.** A delegated review can't touch your tree — OS-enforced Seatbelt for grok/composer one-shots, capability-gated + cwd-confined for live sessions, `sandbox=read-only` for codex. `--full-auto` to let it act, `--isolate` for a throwaway worktree that **fails closed**.
- **🐶 No hangs, no orphans, no silent drift.** The watchdog kills a startup-auth hang within one poll cycle, pins the model explicitly so it can't drift across CLI versions, supervises the CLI as a foreground child so completion notifications actually fire, and hands every run its own directory.
- **🔀 Real model diversity, counted honestly.** Grok + Composer ride one CLI; Gemini rides Antigravity `agy`; the panel's gate counts distinct model *lineages*, not seats — so "diverse" means diverse.
- **🧪 Hardened in the open.** Every skill ships its own regression suite; the live-session daemon was forged through **two adversarial multi-model panels** and live-verified on all four engines. No fake green.

## The five vassals

| Command | What it does | Engine under the whip |
|---|---|---|
| `/codex <prompt>` | one delegated sub-task, supervised | Codex CLI · `gpt-5.6-sol` · `ultra` |
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
for s in codex gemini grok composer review-panel _session; do ln -s "$PWD/lord-claude/$s" ~/.claude/skills/$s; done
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

## Keep a vassal at your side (live sessions)

The five commands above each send a vassal off to do **one** task and report back. Sometimes you want one to *stay* — a standing conversation it remembers turn to turn, exactly like messaging one of Claude's own subagents. That's `agents`:

```bash
agents start grok --handle rev1 --cwd .          # summon a warm vassal, give it a name
agents send  --to rev1 "Review ./auth for bypasses."
agents send  --to rev1 "Now the token-refresh path — anything there?"   # it remembers the review
agents list                                      # who's at court
agents stop  --to rev1
```

One unified command addresses **any** engine by handle; each stays warm in memory and keeps the whole thread:

| Engine | How it stays warm |
|---|---|
| **grok / composer** | `grok agent stdio` — a real ACP session, streaming, its tools run client-side |
| **codex** | `codex mcp-server` — `codex-reply` continues the same thread |
| **gemini** | `agy -i` held under a pseudo-terminal; each turn's end **and reply** are read from Antigravity's own SQLite store |

**Read-only by default** — grok/composer get *no write channel at all* (and reads are confined to the session's `--cwd`); codex runs `sandbox=read-only`. `--full-auto` hands over a file-write + terminal bridge (grok/composer) or `workspace-write` (codex). One honest caveat: **gemini has no enforceable read-only mode** — Antigravity simply doesn't offer one — so a gemini session chats and reasons but isn't write-sandboxed; point it at a throwaway `--cwd` if that matters. Live sessions live in their own `/tmp/agent_sessions/<handle>/` and never touch the one-shot machinery or `/review-panel`.

> Each engine speaks a different dialect under the hood — ACP JSON-RPC, MCP, or a terminal plus a database tail — but `agents` makes them one court you address the same way. The daemon supervises each session like the watchdog supervises a one-shot run: atomic status, a heartbeat, zombie-reaping process-group kill. Full docs in [`_session/SKILL.md`](./_session/SKILL.md).

### What a standing vassal can do

A warm session isn't just "remembers the thread." The same court now grants six more powers — every one of them spiked against the real CLIs, then hammered by a diverse review panel:

```bash
agents send  --to rev1 --follow "Audit ./auth and stream your thinking."   # watch the reply as it writes
agents cancel --to rev1                       # change your mind mid-turn — it stops, stays warm
agents send  --to rev1 --schema bug.json "Report the worst bug as JSON."   # reply guaranteed to fit your schema
agents send  --to-all "Status?"               # one decree, every live vassal answers (in parallel)
agents board post --topic plan --from rev1 "auth.ts line 88 looks off"     # a shared notice-board…
agents board read --topic plan                # …any vassal (or you) can read
agents start grok --handle rev1 --resume      # bring a dismissed vassal back — full memory intact
```

- **👁 Watch it think (live streaming).** `read --follow` tails the reply as the model writes it — grok's native chunks, codex's token deltas, even gemini's answer pulled live out of Antigravity's database mid-generation. No more staring at a blank prompt.
- **✋ Cancel mid-turn.** Told it the wrong thing? `agents cancel` stops the turn in flight. grok/composer and gemini stay **warm** for the next message; codex (which won't honor a soft cancel) is cut and reattached on demand. No waiting out a runaway essay.
- **📐 Structured output on any engine.** `--schema FILE` makes *any* vassal return JSON that validates against your JSON Schema — it re-asks itself with the validator's complaint until it fits. None of them support this natively; the court adds it uniformly.
- **📣 Broadcast to the whole court.** `--to-all` (or `--engines grok,codex`) sends one prompt to every live vassal at once, in parallel, and hands you `{handle: reply}` — one slow or failed vassal never holds up the rest.
- **📜 A shared blackboard (agent-to-agent).** `agents board` is an append-only notice-board the vassals post to and read from — so a `--full-auto` vassal can leave findings for the next one. Coordination without a babysitter.
- **🔁 Reattach with memory intact.** Stop a session, reboot the machine, come back tomorrow — `--resume` rebinds the engine's own conversation id (grok `session/load`, codex's thread, gemini's `--conversation`) and the model picks up exactly where it left off.

> Verified the boring way: 81 self-tests (the streaming ones *fail* if you break a stream — no false greens), six live spikes against the real binaries, and a full end-to-end run on real Grok where a reattached vassal still recalled what it was first asked. The diverse panel (2 Codex + Gemini + an Opus skeptic) earned its keep — it caught a test that was passing for the wrong reason, and it got fixed.

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
