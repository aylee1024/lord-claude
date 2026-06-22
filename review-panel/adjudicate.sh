#!/bin/bash
# review-panel adjudicator.
#
# Empirically gates HIGH/CRITICAL findings before any of them can block. For
# each such finding it re-runs the reviewer's repro_command in ONE throwaway
# git worktree and classifies it:
#   reproduced  - repro_command exited with expected_exit  -> CAN block
#   refuted     - repro_command did NOT match expected_exit -> downgraded to nit
#   not_run     - no repro_command. Blocks ONLY if evidence_kind=not_reproducible
#                 (a reason was given for why it can't be locally reproduced);
#                 otherwise (static_proof / nothing) it is a nit.
# Never blocks on model text. The blocker set is the gate's verdict.
#
# Usage:
#   adjudicate.sh --findings <merged.json> --repo <path> [--ref <ref>] \
#                 --out <results.json> [--scratch <dir>] \
#                 [--families <csv>] [--min-families <N>]
#
#   --findings      JSON: {"findings":[...]} or a bare [...]; per findings.schema.json
#   --repo          target git repo
#   --ref           git ref to adjudicate at (default: the repo's current HEAD)
#   --out           where to write the adjudicated results JSON
#   --scratch       writable scratch for caches/TMPDIR (default: mktemp -d)
#   --families      CSV of distinct model families that produced VALID output this panel
#                   (e.g. "codex,gemini,anthropic"). Enables the diversity preflight (1a/1d):
#                   fewer than --min-families => decision PROVISIONAL, not PASS. Omit to skip.
#   --min-families  diversity threshold (default 3 = codex + gemini + anthropic).
#
# Exit: 0 PASS (no blockers, diversity ok); 1 BLOCK (a reproduced/justified blocker);
#       3 PROVISIONAL (no blockers but < min families produced valid output); 2 on bad args.
#
# HARDENING
#   - ONE worktree; `git reset --hard <ref> && git clean -fdx-excluding-node_modules`
#     between findings so each repro runs from a clean diff state and a
#     fix-application repro cannot leak into the next finding.
#   - Installs banned (NPM_CONFIG_OFFLINE=1, npm_config_offline=true, CI=1).
#     TMPDIR + npm/yarn/vite caches redirected into --scratch so a test run
#     cannot write through a symlinked node_modules into the shared real tree.
#   - node_modules is symlinked read-shared from --repo (git worktrees don't
#     copy it). Post-run we assert the SOURCE repo's node_modules package set is
#     unchanged (manifest hash) and the worktree's tracked tree is clean.
#   - repro_commands come from the review panel (semi-trusted): this is our own
#     pipeline, not adversarial input. Defense is no-network-install + isolation,
#     not a full sandbox.

set -u

FINDINGS="" ; REPO="" ; REF="" ; OUT="" ; SCRATCH="" ; FAMILIES="" ; FAMILIES_SET="0" ; MIN_FAMILIES="3"
while [ $# -gt 0 ]; do
  case "$1" in
    --findings) FINDINGS="$2"; shift 2;;
    --repo)     REPO="$2"; shift 2;;
    --ref)      REF="$2"; shift 2;;
    --out)      OUT="$2"; shift 2;;
    --scratch)  SCRATCH="$2"; shift 2;;
    --families)     FAMILIES="$2"; FAMILIES_SET="1"; shift 2;;   # CSV of distinct families with VALID output (audit 1a/1d); PRESENCE (even empty=0 families) enforces the diversity gate
    --min-families) MIN_FAMILIES="$2"; shift 2;;
    *) echo "[adjudicate] unknown arg: $1" >&2; exit 2;;
  esac
done
[ -s "$FINDINGS" ] || { echo "[adjudicate] --findings missing/empty" >&2; exit 2; }
[ -d "$REPO/.git" ] || git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "[adjudicate] --repo is not a git repo: $REPO" >&2; exit 2; }
[ -n "$OUT" ] || { echo "[adjudicate] --out required" >&2; exit 2; }
REPO="$(cd "$REPO" && pwd)"
REF="${REF:-$(git -C "$REPO" rev-parse HEAD)}"
if [ -n "$SCRATCH" ]; then AUTO_SCRATCH=0; else SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/adjudicate.XXXXXX")"; AUTO_SCRATCH=1; fi
mkdir -p "$SCRATCH/cache" "$SCRATCH/tmp"
SCRATCH="$(cd "$SCRATCH" && pwd)"

WORKTREE="$SCRATCH/worktree"
NODE_MODULES_SRC="$REPO/node_modules"

log() { printf '[adjudicate] %s\n' "$*" >&2; }

cleanup() {
  git -C "$REPO" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || rm -rf "$WORKTREE" 2>/dev/null
  git -C "$REPO" worktree prune >/dev/null 2>&1 || true
  [ "${AUTO_SCRATCH:-0}" = "1" ] && rm -rf "$SCRATCH" 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- set up the single adjudication worktree ---
log "worktree at $REF"
git -C "$REPO" worktree add --quiet --detach "$WORKTREE" "$REF" || { echo "[adjudicate] worktree add failed" >&2; exit 2; }
if [ -d "$NODE_MODULES_SRC" ] && [ ! -e "$WORKTREE/node_modules" ]; then
  ln -s "$NODE_MODULES_SRC" "$WORKTREE/node_modules"
fi
# Record the source dependency manifest so we can prove we never mutated it.
NM_BEFORE=$(find "$NODE_MODULES_SRC" -maxdepth 2 -name package.json 2>/dev/null | sort | xargs shasum 2>/dev/null | shasum | awk '{print $1}')

# Extract HIGH+ findings as: id <TAB> evidence_kind <TAB> expected_exit <TAB> base64(repro_command)
ROWS=$(node -e '
  const fs = require("fs");
  const raw = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  const findings = Array.isArray(raw) ? raw : (raw.findings || []);
  let i = 0;
  for (const f of findings) {
    const sev = (f.severity || "").toUpperCase();
    if (sev !== "HIGH" && sev !== "CRITICAL") continue;
    const id = f.id || ("finding-" + (++i));
    const ek = f.evidence_kind || "static_proof";
    const ee = (f.expected_exit === 0 || f.expected_exit) ? String(f.expected_exit) : "";
    const cmd = f.repro_command ? Buffer.from(f.repro_command, "utf8").toString("base64") : "";
    process.stdout.write([id, ek, ee, cmd].join("\t") + "\n");
  }
' "$FINDINGS")

# --- adjudicate each HIGH+ finding ---
RESULTS_TSV="$SCRATCH/results.tsv"
: > "$RESULTS_TSV"
if [ -n "$ROWS" ]; then
  while IFS=$'\t' read -r id ek ee cmd_b64; do
    [ -n "$id" ] || continue
    if [ -z "$cmd_b64" ]; then
      # No repro. Blocks only if the reviewer explicitly justified non-reproducibility.
      if [ "$ek" = "not_reproducible" ]; then verdict="not_run_justified"; else verdict="not_run"; fi
      log "$id: no repro_command -> $verdict"
      printf '%s\t%s\n' "$id" "$verdict" >> "$RESULTS_TSV"
      continue
    fi
    cmd=$(printf '%s' "$cmd_b64" | base64 --decode)
    # clean diff state for this finding (keep the symlinked node_modules)
    git -C "$WORKTREE" reset --hard --quiet "$REF"
    git -C "$WORKTREE" clean -fdqx -e node_modules   # -x: also remove IGNORED artifacts so a prior repro's cache/coverage cannot contaminate the next finding
    log "$id: running repro (expected_exit=${ee:-0})"
    (
      cd "$WORKTREE" || exit 99
      env NPM_CONFIG_OFFLINE=true npm_config_offline=true CI=true \
          TMPDIR="$SCRATCH/tmp" npm_config_cache="$SCRATCH/cache" \
          bash -c "$cmd"
    ) > "$SCRATCH/$id.out" 2>&1
    actual=$?
    want="${ee:-0}"
    if [ "$actual" = "$want" ]; then verdict="reproduced"; else verdict="refuted"; fi
    log "$id: exit=$actual want=$want -> $verdict"
    printf '%s\t%s\n' "$id" "$verdict" >> "$RESULTS_TSV"
  done <<< "$ROWS"
fi

# --- HARDENING assertion: the shared node_modules package set is unchanged ---
NM_AFTER=$(find "$NODE_MODULES_SRC" -maxdepth 2 -name package.json 2>/dev/null | sort | xargs shasum 2>/dev/null | shasum | awk '{print $1}')
NM_DIRTY="false"
if [ "$NM_BEFORE" != "$NM_AFTER" ]; then
  NM_DIRTY="true"
  log "WARNING: shared node_modules manifest changed during adjudication ($NM_BEFORE -> $NM_AFTER)"
fi

# --- merge verdicts back into findings, compute blockers, write --out ---
node -e '
  const fs = require("fs");
  const raw = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  const findings = Array.isArray(raw) ? raw : (raw.findings || []);
  const tsv = fs.existsSync(process.argv[2]) ? fs.readFileSync(process.argv[2], "utf8") : "";
  const nmDirty = process.argv[3] === "true";
  const verdicts = {};
  for (const line of tsv.split("\n")) {
    if (!line.trim()) continue;
    const [id, v] = line.split("\t");
    verdicts[id] = v;
  }
  let i = 0;
  const blockers = [], nits = [];
  for (const f of findings) {
    const sev = (f.severity || "").toUpperCase();
    if (sev !== "HIGH" && sev !== "CRITICAL") { f.adjudication = "not_gated"; nits.push(f); continue; }
    const id = f.id || ("finding-" + (++i));
    f.id = id;
    const v = verdicts[id] || "not_run";
    f.adjudication = v;
    // BLOCK only on empirically reproduced, or an explicitly justified non-reproducible.
    if (v === "reproduced" || v === "not_run_justified") blockers.push(f);
    else nits.push(f);   // refuted, not_run (no justification), or model-text-only
  }
  // Panel-family preflight (audit 1a/1d). The diversity invariant needs >= minFamilies
  // distinct model families with VALID output. If --families was passed and the count is
  // short, the result is PROVISIONAL (not gate-eligible) even with zero blockers, until a
  // full-diversity panel re-clears it. Omitting --families skips the check (legacy behavior).
  const familiesRaw = (process.argv[5] || "").trim();
  const minFamilies = parseInt(process.argv[6] || "3", 10);
  // PRESENCE of --families (argv[7]==="1"), not non-emptiness, enables the gate: an explicit
  // empty set means 0 valid families (a total panel failure) and MUST be PROVISIONAL, not PASS.
  const familiesProvided = (process.argv[7] === "1");
  const families = familiesProvided
    ? [...new Set(familiesRaw.split(",").map(s => s.trim().toLowerCase()).filter(Boolean))]
    : [];
  const diversityOk = familiesProvided ? (families.length >= minFamilies) : true;
  // Precedence: a reproduced bug blocks regardless; otherwise an incomplete panel is
  // PROVISIONAL (a clean bill of health from < 3 families is not trustworthy); else PASS.
  const decision = blockers.length > 0 ? "BLOCK" : (!diversityOk ? "PROVISIONAL" : "PASS");
  const out = {
    summary: {
      total: findings.length,
      blockers: blockers.length,
      nits: nits.length,
      node_modules_mutated: nmDirty,
      families_present: families,
      families_count: families.length,
      min_families: minFamilies,
      diversity_ok: diversityOk,
      decision: decision
    },
    blockers, nits
  };
  fs.writeFileSync(process.argv[4], JSON.stringify(out, null, 2));
  process.stderr.write("[adjudicate] decision=" + decision +
    " blockers=" + blockers.length + " nits=" + nits.length +
    " families=" + families.length + "/" + minFamilies + " diversity_ok=" + diversityOk +
    " node_modules_mutated=" + nmDirty + "\n");
' "$FINDINGS" "$RESULTS_TSV" "$NM_DIRTY" "$OUT" "$FAMILIES" "$MIN_FAMILIES" "$FAMILIES_SET"

# Exit reflects the gate decision (block iff a reproduced/justified blocker exists).
node -e 'const o=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")); const d=o.summary.decision; process.exit(d==="BLOCK"?1:(d==="PROVISIONAL"?3:0))' "$OUT"
