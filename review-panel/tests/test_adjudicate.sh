#!/bin/bash
# Test the review-panel adjudicator against a fixture repo + planted findings.
# Asserts the core invariants:
#   - a reproduced HIGH blocks
#   - a planted FALSE-POSITIVE HIGH (repro doesn't match) is refuted -> nit
#   - a model-text-only HIGH (no repro, static_proof) does NOT block
#   - a justified not_reproducible HIGH DOES block
#   - a MEDIUM is not gated
#   - node_modules is not mutated; overall decision = BLOCK (exit 1)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADJ="$HERE/../adjudicate.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/adj_test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# fixture git repo
REPO="$TMP/repo"; mkdir -p "$REPO"; ( cd "$REPO"
  git init -q; git config user.email t@t; git config user.name t
  echo "this line contains BUG" > file.txt
  git add -A; git commit -qm init )

cat > "$TMP/findings.json" <<'JSON'
{"findings":[
  {"id":"A-realbug","severity":"HIGH","file":"file.txt","claim":"BUG present","evidence_kind":"reproduced","repro_command":"grep -q BUG file.txt","expected_exit":0,"confidence":"high"},
  {"id":"B-falsepos","severity":"HIGH","file":"file.txt","claim":"NONEXISTENT present","evidence_kind":"reproduced","repro_command":"grep -q NONEXISTENT_TOKEN file.txt","expected_exit":0,"confidence":"high"},
  {"id":"C-modeltext","severity":"HIGH","file":"file.txt","claim":"vibes say bug","evidence_kind":"static_proof","repro_command":null,"expected_exit":null,"confidence":"low"},
  {"id":"D-justified","severity":"HIGH","file":"file.txt","claim":"race only in prod","evidence_kind":"not_reproducible","repro_command":null,"expected_exit":null,"confidence":"medium"},
  {"id":"E-medium","severity":"MEDIUM","file":"file.txt","claim":"style","evidence_kind":"static_proof","repro_command":null,"expected_exit":null,"confidence":"low"}
]}
JSON

bash "$ADJ" --findings "$TMP/findings.json" --repo "$REPO" --out "$TMP/out.json"
DECISION_EXIT=$?

node -e '
  const fs=require("fs");
  const o=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
  const all=[...o.blockers,...o.nits];
  const by={}; for(const f of all) by[f.id]=f.adjudication;
  const want={ "A-realbug":"reproduced","B-falsepos":"refuted","C-modeltext":"not_run","D-justified":"not_run_justified","E-medium":"not_gated" };
  let fail=0;
  for(const id in want){ if(by[id]!==want[id]){ console.error("FAIL "+id+": got "+by[id]+" want "+want[id]); fail=1;} else console.log("ok "+id+" -> "+by[id]); }
  const blk=o.blockers.map(f=>f.id).sort().join(",");
  if(blk!=="A-realbug,D-justified"){ console.error("FAIL blockers got ["+blk+"] want [A-realbug,D-justified]"); fail=1;} else console.log("ok blockers = A-realbug,D-justified");
  if(o.summary.decision!=="BLOCK"){ console.error("FAIL decision "+o.summary.decision); fail=1;} else console.log("ok decision=BLOCK");
  if(o.summary.node_modules_mutated!==false){ console.error("FAIL node_modules_mutated"); fail=1;} else console.log("ok node_modules_mutated=false");
  process.exit(fail);
' "$TMP/out.json"
NODE_EXIT=$?

echo "--- adjudicate decision exit=$DECISION_EXIT (expect 1=BLOCK) ---"
[ "$DECISION_EXIT" = "1" ] || { echo "FAIL: adjudicate exit $DECISION_EXIT want 1"; NODE_EXIT=1; }


# --- Relative path node_modules symlink resolution verification ---
(
  cd "$REPO"
  mkdir -p node_modules/some-pkg
  echo '{"name":"some-pkg"}' > node_modules/some-pkg/package.json
  echo '{"findings":[{"id":"R-rel","severity":"HIGH","file":"file.txt","claim":"BUG","evidence_kind":"reproduced","repro_command":"[ -d node_modules/some-pkg ]","expected_exit":0}]}' > "$TMP/rel.json"
  bash "$ADJ" --findings "$TMP/rel.json" --repo . --out "$TMP/rel_out.json" >/dev/null 2>&1
  rc=$?
  rm -rf node_modules
  [ "$rc" = "1" ]
) || { echo "FAIL: relative repo path node_modules resolution"; NODE_EXIT=1; }

# --- Panel-family preflight (audit 1a/1d) ---
FAIL_DIV=0
dec(){ node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1])).summary.decision)' "$1" 2>/dev/null; }
div(){ node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1])).summary.diversity_ok)' "$1" 2>/dev/null; }
cat > "$TMP/clean.json" <<'JSON'
{"findings":[
  {"id":"X-falsepos","severity":"HIGH","file":"file.txt","claim":"NONEXISTENT","evidence_kind":"reproduced","repro_command":"grep -q NOPE_TOKEN file.txt","expected_exit":0,"confidence":"high"},
  {"id":"Y-medium","severity":"MEDIUM","file":"file.txt","claim":"style","evidence_kind":"static_proof","repro_command":null,"expected_exit":null,"confidence":"low"}
]}
JSON

# 2 families, no blockers -> PROVISIONAL (exit 3)
bash "$ADJ" --findings "$TMP/clean.json" --repo "$REPO" --out "$TMP/prov.json" --families "codex,anthropic" >/dev/null 2>&1; PE=$?
if [ "$PE" = "3" ] && [ "$(dec "$TMP/prov.json")" = "PROVISIONAL" ] && [ "$(div "$TMP/prov.json")" = "false" ]; then echo "ok PROVISIONAL on 2 families (exit 3)"; else echo "FAIL provisional: exit=$PE dec=$(dec "$TMP/prov.json") div=$(div "$TMP/prov.json")"; FAIL_DIV=1; fi

# 3 families, no blockers -> PASS (exit 0)
bash "$ADJ" --findings "$TMP/clean.json" --repo "$REPO" --out "$TMP/pass.json" --families "codex,gemini,anthropic" >/dev/null 2>&1; PE=$?
if [ "$PE" = "0" ] && [ "$(dec "$TMP/pass.json")" = "PASS" ]; then echo "ok PASS on 3 families (exit 0)"; else echo "FAIL pass-3: exit=$PE dec=$(dec "$TMP/pass.json")"; FAIL_DIV=1; fi

# no --families -> legacy PASS (diversity check skipped)
bash "$ADJ" --findings "$TMP/clean.json" --repo "$REPO" --out "$TMP/legacy.json" >/dev/null 2>&1; PE=$?
if [ "$PE" = "0" ] && [ "$(dec "$TMP/legacy.json")" = "PASS" ] && [ "$(div "$TMP/legacy.json")" = "true" ]; then echo "ok legacy PASS without --families"; else echo "FAIL legacy: exit=$PE dec=$(dec "$TMP/legacy.json") div=$(div "$TMP/legacy.json")"; FAIL_DIV=1; fi

# explicit EMPTY --families (0 valid families = total panel failure) -> PROVISIONAL, not PASS (R4-1)
bash "$ADJ" --findings "$TMP/clean.json" --repo "$REPO" --out "$TMP/empty.json" --families "" >/dev/null 2>&1; PE=$?
if [ "$PE" = "3" ] && [ "$(dec "$TMP/empty.json")" = "PROVISIONAL" ] && [ "$(div "$TMP/empty.json")" = "false" ]; then echo "ok empty --families (0 families) -> PROVISIONAL (exit 3)"; else echo "FAIL empty-families: exit=$PE dec=$(dec "$TMP/empty.json") div=$(div "$TMP/empty.json")"; FAIL_DIV=1; fi

# blockers present + short families -> BLOCK still wins (exit 1)
bash "$ADJ" --findings "$TMP/findings.json" --repo "$REPO" --out "$TMP/blockwins.json" --families "codex" >/dev/null 2>&1; PE=$?
if [ "$PE" = "1" ] && [ "$(dec "$TMP/blockwins.json")" = "BLOCK" ]; then echo "ok BLOCK precedence over PROVISIONAL (exit 1)"; else echo "FAIL block-precedence: exit=$PE dec=$(dec "$TMP/blockwins.json")"; FAIL_DIV=1; fi

# ignored-file contamination between findings must NOT leak (clean -x) (R6-2)
( cd "$REPO" && printf ".cache/\n" >> .gitignore && git add .gitignore && git commit -qm ignore ) >/dev/null 2>&1
printf '{"findings":[{"id":"seed","severity":"HIGH","file":"file.txt","claim":"c","evidence_kind":"reproduced","repro_command":"mkdir -p .cache; touch .cache/proof; exit 1","expected_exit":0},{"id":"contam","severity":"HIGH","file":"file.txt","claim":"c","evidence_kind":"reproduced","repro_command":"test -f .cache/proof","expected_exit":0}]}\n' > "$TMP/contam.json"
bash "$ADJ" --findings "$TMP/contam.json" --repo "$REPO" --out "$TMP/contam_out.json" --families codex,gemini,anthropic >/dev/null 2>&1
cadj=$(node -e 'const o=JSON.parse(require("fs").readFileSync(process.argv[1]));const c=[...o.blockers,...o.nits].find(f=>f.id==="contam");console.log(c?c.adjudication:"missing")' "$TMP/contam_out.json" 2>/dev/null)
if [ "$cadj" = "refuted" ]; then echo "ok no ignored-file contamination between findings (clean -x)"; else echo "FAIL contamination: contam=$cadj (want refuted)"; FAIL_DIV=1; fi

[ "$FAIL_DIV" = "0" ] || NODE_EXIT=1
if [ "$NODE_EXIT" = "0" ]; then echo "PASS test_adjudicate"; else echo "FAIL test_adjudicate"; fi
exit "$NODE_EXIT"
