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

if [ "$NODE_EXIT" = "0" ]; then echo "PASS test_adjudicate"; else echo "FAIL test_adjudicate"; fi
exit "$NODE_EXIT"
