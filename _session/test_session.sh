#!/usr/bin/env bash
# test_session.sh — exercises the live-session daemon + agents dispatcher with a MOCK `grok agent stdio`.
# Mirrors the stub-binary harness style of skills/grok/test_watchdog.sh: a fake `grok` on a prepended PATH
# speaks canned ACP JSON-RPC. Proves: handshake, multi-turn context on ONE warm process, the read-only
# client tool bridge, list/status/stop, and the dead-daemon stale sweep.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(mktemp -d /tmp/sess_test.XXXXXX)"
BIN="$ROOT/bin"; mkdir -p "$BIN"
export AGENT_SESSIONS_DIR="$ROOT/sessions"
export PATH="$BIN:$PATH"
PASS=0; FAIL=0
chk(){ if [ "$2" = "$3" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); echo "FAIL: $1 — expected [$3] got [$2]"; fi; }
has(){ case "$2" in *"$3"*) PASS=$((PASS+1));; *) FAIL=$((FAIL+1)); echo "FAIL: $1 — [$2] missing [$3]";; esac; }

cleanup(){ pkill -f "session_daemon.py $AGENT_SESSIONS_DIR" 2>/dev/null; rm -rf "$ROOT"; }
trap cleanup EXIT

# ---- mock `grok`: a minimal ACP server that stays warm across prompts ----
cat > "$BIN/grok" <<'PYEOF'
#!/usr/bin/env python3
import sys, json, re, os
args = sys.argv[1:]
if "agent" not in args or "stdio" not in args:
    sys.exit(0)
SID = "mock-sess-123"
state = {"number": None}
def emit(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
def reply_turn(mid, text):
    emit({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":SID,
        "update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":text}}}})
    emit({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
def read_one():
    line = sys.stdin.readline()
    return json.loads(line) if line.strip() else None
for raw in sys.stdin:
    raw = raw.strip()
    if not raw: continue
    msg = json.loads(raw); mid = msg.get("id"); method = msg.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1,"agentCapabilities":{}}})
    elif method == "session/new":
        emit({"jsonrpc":"2.0","id":mid,"result":{"sessionId":SID}})
    elif method == "session/load":
        # reattach: record the loaded id (proves --resume drove session/load), keep the session usable
        try:
            with open(os.environ.get("GROK_MOCK_LOAD_FILE","/dev/null"),"w") as f:
                f.write((msg.get("params") or {}).get("sessionId",""))
        except Exception:
            pass
        emit({"jsonrpc":"2.0","id":mid,"result":{}})
    elif method == "session/prompt":
        text = "".join(b.get("text","") for b in msg["params"]["prompt"])
        if text.startswith("readtest:"):
            # exercise the client tool bridge: ask the client to read a file
            path = text.split(":",1)[1].strip()
            emit({"jsonrpc":"2.0","id":9001,"method":"fs/read_text_file",
                  "params":{"sessionId":SID,"path":path}})
            resp = read_one()
            if resp and resp.get("error"):
                reply_turn(mid, "read denied")
            else:
                content = (resp or {}).get("result",{}).get("content","")
                reply_turn(mid, f"read {len(content)} bytes")
        elif text.startswith("writetest:"):
            path = text.split(":",1)[1].strip()
            emit({"jsonrpc":"2.0","id":9002,"method":"fs/write_text_file",
                  "params":{"sessionId":SID,"path":path,"content":"hi"}})
            resp = read_one()
            err = (resp or {}).get("error") if resp else None
            reply_turn(mid, "write denied" if err else "write ok")
        elif text.startswith("termtest"):
            emit({"jsonrpc":"2.0","id":9003,"method":"terminal/create",
                  "params":{"sessionId":SID,"command":"echo hi","env":[]}})
            resp = read_one()
            err = (resp or {}).get("error") if resp else None
            reply_turn(mid, "terminal denied" if err else "terminal ok")
        elif "streamtest" in text:
            # emit chunks WITH GAPS so outbox/<n>.partial is observably mid-stream (alpha before gamma)
            for piece in ["alpha ", "beta ", "gamma"]:
                emit({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":SID,
                    "update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":piece}}}})
                __import__("time").sleep(0.5)
            emit({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
        elif "schematest" in text:
            # first attempt returns NON-json (forces a schema retry), second returns valid JSON
            state["sc"] = state.get("sc", 0) + 1
            reply_turn(mid, "sorry, here is no json" if state["sc"] == 1 else '{"n": 7, "ok": true}')
        elif "schemafail" in text:
            reply_turn(mid, "this is never valid json")   # always invalid -> schema-unmet after retries
        elif "cancelnow" in text:
            # emit slowly while watching stdin for the ACP session/cancel notification (graceful cancel)
            sel = __import__("select"); cancelled = False
            for k in range(40):
                emit({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":SID,
                    "update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":f"tick{k} "}}}})
                r,_,_ = sel.select([sys.stdin],[],[],0.2)
                if r:
                    l = sys.stdin.readline()
                    if l and "session/cancel" in l:
                        cancelled = True; break
            emit({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"cancelled" if cancelled else "end_turn"}})
        elif "HANGNOW" in text:
            __import__("time").sleep(30)        # never reply within HANG_SEC -> a hung turn
            reply_turn(mid, "late")
        else:
            m = re.search(r'number\s+(\d+)', text, re.I)
            if m and "remember" in text.lower():
                state["number"] = m.group(1); reply_turn(mid, "OK")
            elif "what number" in text.lower():
                reply_turn(mid, state["number"] or "unknown")
            else:
                reply_turn(mid, "ack")
    else:
        if mid is not None:
            emit({"jsonrpc":"2.0","id":mid,"result":{}})
PYEOF
chmod +x "$BIN/grok"

# ---- mock `codex`: a minimal MCP server (codex + codex-reply tools), stays warm across calls ----
cat > "$BIN/codex" <<'PYEOF'
#!/usr/bin/env python3
import sys, json, re, os
if "mcp-server" not in sys.argv[1:]:
    sys.exit(0)
TID = "mock-thread-abc12345"
state = {"number": None}
def emit(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
def result_text(mid, text):
    emit({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":text}]}})
for raw in sys.stdin:
    raw = raw.strip()
    if not raw: continue
    msg = json.loads(raw); mid = msg.get("id"); method = msg.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
            "capabilities":{"tools":{"listChanged":True}},"serverInfo":{"name":"mock-codex","version":"0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/call":
        name = msg["params"]["name"]; argd = msg["params"].get("arguments", {})
        text = argd.get("prompt", "")
        ff = os.environ.get("CODEX_MOCK_FIRSTTOOL_FILE")
        if ff and not os.path.exists(ff):
            try:
                with open(ff, "w") as f: f.write(name)   # first tool seen (reattach -> codex-reply)
            except Exception: pass
        if "cancelnow" in text:
            __import__("time").sleep(30)    # long turn; cancel() tears down the backend (S4)
            result_text(mid, "late"); continue
        if "streamtest" in text:
            if name == "codex":
                emit({"jsonrpc":"2.0","method":"codex/event",
                      "params":{"msg":{"type":"thread.started","thread_id":TID},"threadId":TID}})
            for piece in ["alpha ", "beta ", "gamma"]:   # incremental agent_message_content_delta (S1 shape)
                emit({"jsonrpc":"2.0","method":"codex/event",
                      "params":{"msg":{"type":"agent_message_content_delta","delta":piece}}})
                __import__("time").sleep(0.5)
            result_text(mid, "alpha beta gamma")
            continue
        if name == "codex":
            try:
                with open(os.environ.get("CODEX_MOCK_SANDBOX_FILE","/dev/null"),"w") as f:
                    f.write(str(argd.get("sandbox","")))
            except Exception: pass
            emit({"jsonrpc":"2.0","method":"codex/event",
                  "params":{"msg":{"type":"thread.started","thread_id":TID},"threadId":TID}})
            m = re.search(r'number\s+(\d+)', text, re.I)
            if m and "remember" in text.lower():
                state["number"] = m.group(1); result_text(mid, "OK")
            else:
                result_text(mid, "ack")
        elif name == "codex-reply":
            result_text(mid, state["number"] if "what number" in text.lower() and state["number"] else "ack")
        else:
            result_text(mid, "ack")
    else:
        if mid is not None:
            emit({"jsonrpc":"2.0","id":mid,"result":{}})
PYEOF
chmod +x "$BIN/codex"

# ---- mock `agy`: runs under the daemon's pty; writes a conversation .db with steps rows whose
#      assistant (type-15, status-3) payloads encode the reply at protobuf path (20,1), like Antigravity ----
cat > "$BIN/agy" <<'PYEOF'
#!/usr/bin/env python3
import sys, os, re, sqlite3, uuid, signal
# real agy catches the pty INTR (\x03 -> SIGINT) to cancel a turn and STAYS ALIVE (spike S5); mirror that
# so the mock survives `agents cancel` instead of being killed by the default SIGINT disposition.
_sigint = {"v": False}
signal.signal(signal.SIGINT, lambda *a: _sigint.__setitem__("v", True))
argv = sys.argv[1:]
prompt = None
conv_id = None
for i, a in enumerate(argv):
    if a in ("-i", "--prompt-interactive") and i + 1 < len(argv):
        prompt = argv[i + 1]
    if a == "--conversation" and i + 1 < len(argv):
        conv_id = argv[i + 1]            # resume: append to the existing conversation db
CONV = os.environ.get("AGY_CONV_DIR", os.path.expanduser("~/.gemini/antigravity-cli/conversations"))
os.makedirs(CONV, exist_ok=True)
con = sqlite3.connect(os.path.join(CONV, (conv_id or str(uuid.uuid4())) + ".db"))
con.execute("CREATE TABLE IF NOT EXISTS steps (idx integer, step_type integer, status integer, step_payload blob, PRIMARY KEY(idx))")
con.commit()
_mx = con.execute("SELECT COALESCE(MAX(idx),-1) FROM steps").fetchone()[0]
state = {"number": None, "idx": _mx + 1}   # resume continues idx beyond the existing rows
def varint(n):
    o = bytearray()
    while True:
        b = n & 0x7f; n >>= 7
        o.append(b | 0x80 if n else b)
        if not n: break
    return bytes(o)
def tag(field, wt): return varint((field << 3) | wt)   # proper protobuf tag varint (field 20 -> b'\xa2\x01')
def encode_reply(text):
    tb = text.encode()
    inner = tag(1, 2) + varint(len(tb)) + tb            # field 1 = message text
    return tag(20, 2) + varint(len(inner)) + inner      # field 20 = message
def reply_for(text):
    if text.startswith("longtest:"):
        return text.split(":", 1)[1]
    m = re.search(r'number\s+(\d+)', text, re.I)
    if m and "remember" in text.lower():
        state["number"] = m.group(1); return "OK"
    if "what number" in text.lower():
        return state["number"] or "unknown"
    return "ack"
def do_turn(text):
    if "cancelnow" in text:
        # write an in-progress (status 8) row, then wait for the cancel SIGINT (\x03 on the pty). The
        # daemon's _cancel event independently unblocks its _wait_turn_end; we just stay alive + return.
        import time as _t
        con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 14, 3, b"user")); state["idx"] += 1
        con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 15, 8, encode_reply("partial so far"))); state["idx"] += 1
        con.commit()
        sys.stdout.write("...gen...\n"); sys.stdout.flush()
        _sigint["v"] = False
        deadline = _t.time() + 15
        while _t.time() < deadline and not _sigint["v"]:
            _t.sleep(0.2)
        return
    if "streamtest" in text:
        # mirror S2: assistant row created at status=8 (generating), its (20,1) payload grows, then
        # flips to status=3 (final). The daemon streams (20,1) to outbox/<n>.partial each poll.
        import time as _t
        con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 14, 3, b"user")); state["idx"] += 1
        aidx = state["idx"]; state["idx"] += 1
        con.execute("INSERT INTO steps VALUES (?,?,?,?)", (aidx, 15, 8, encode_reply("alpha "))); con.commit()
        sys.stdout.write("...gen...\n"); sys.stdout.flush(); _t.sleep(0.6)
        con.execute("UPDATE steps SET step_payload=? WHERE idx=?", (encode_reply("alpha beta "), aidx)); con.commit(); _t.sleep(0.6)
        con.execute("UPDATE steps SET step_payload=?, status=3 WHERE idx=?", (encode_reply("alpha beta gamma"), aidx)); con.commit()
        con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 23, 3, b"trajectory")); state["idx"] += 1
        con.commit()
        return
    r = reply_for(text)
    con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 14, 3, b"user")); state["idx"] += 1
    con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 15, 3, encode_reply(r))); state["idx"] += 1
    # trailing trajectory/metadata row AFTER the assistant message — real agy does this, so turn-end
    # detection must NOT assume the assistant row is the latest.
    con.execute("INSERT INTO steps VALUES (?,?,?,?)", (state["idx"], 23, 3, b"trajectory")); state["idx"] += 1
    con.commit()
    # screen NOISE (deliberately NOT the reply) so the test proves the reply came from the DB (20,1),
    # not the screen-scrape fallback.
    sys.stdout.write("...generating...\n"); sys.stdout.flush()
if prompt is not None:
    do_turn(prompt)
while True:
    line = sys.stdin.readline()
    if not line: break
    line = line.strip()
    if line: do_turn(line)
PYEOF
chmod +x "$BIN/agy"
AG="$HERE/agents"

# observer: did outbox/<n>.partial ever show a mid-stream state (alpha present, gamma not yet)?
cat > "$ROOT/observe_stream.py" <<'PYEOF'
import sys, os, time, glob
outdir = os.path.join(sys.argv[1], "outbox")
saw_mid = False
deadline = time.time() + 25
while time.time() < deadline:
    for p in glob.glob(outdir + "/*.partial"):
        try:
            data = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            data = ""
        if "alpha" in data and "gamma" not in data:
            saw_mid = True
    done = False
    for m in glob.glob(outdir + "/*.md"):
        try:
            if "gamma" in open(m, encoding="utf-8", errors="replace").read():
                done = True
        except OSError:
            pass
    if done:
        break
    time.sleep(0.05)
# sentinel must NOT contain "MID" as a substring (the test asserts with EXACT match, not `has`, so a
# broken stream that prints the negative sentinel cannot false-green via substring containment).
print("MID" if saw_mid else "NONE")
PYEOF

# stream_check <handle>: launch a streaming turn (--bg), prove the partial is live mid-stream, then
# prove --follow returns the full finalized reply.
stream_check(){
  local H="$1"
  local SB STURN OBS FR
  SB="$(python3 "$AG" send --to "$H" "streamtest please" --bg 2>/dev/null)"
  STURN="$(printf '%s' "$SB" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("turn",""))' 2>/dev/null)"
  OBS="$(python3 "$ROOT/observe_stream.py" "$AGENT_SESSIONS_DIR/$H")"
  chk "stream[$H]: partial is live mid-stream (alpha before gamma)" "$OBS" "MID"   # EXACT (NONE must fail)
  FR="$(python3 "$AG" read --to "$H" --turn "$STURN" --follow 2>/dev/null)"
  has "stream[$H]: --follow yields the full finalized reply" "$FR" "alpha beta gamma"
}

# wait_for <handle> <status>: poll the status file until it matches (or give up after ~6s)
wait_for(){ local i; for i in $(seq 1 30); do [ "$(cat "$AGENT_SESSIONS_DIR/$1/status" 2>/dev/null)" = "$2" ] && return 0; sleep 0.2; done; return 1; }
# db_maxidx <db path>: MAX(idx) of the steps table (or -99 on error)
db_maxidx(){ python3 -c "import sqlite3,sys
try: print(sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True).execute('SELECT COALESCE(MAX(idx),-1) FROM steps').fetchone()[0])
except Exception: print(-99)" "$1" 2>/dev/null; }

echo "== start =="
OUT="$(python3 "$AG" start grok --handle t1 --cwd "$ROOT" 2>"$ROOT/start.err")"
has "start returns idle" "$OUT" '"status": "idle"'
has "start captures sessionId" "$OUT" 'mock-sess-123'
chk "status file idle" "$(cat "$AGENT_SESSIONS_DIR/t1/status" 2>/dev/null)" "idle"

echo "== multi-turn context on ONE warm process =="
R1="$(python3 "$AG" send --to t1 "Remember the number 7. Reply with exactly: OK" 2>/dev/null)"
has "turn1 reply OK" "$R1" "OK"
R2="$(python3 "$AG" send --to t1 "what number did I say" 2>/dev/null)"
has "turn2 remembers 7 (warm session)" "$R2" "7"

echo "== read-only tool bridge =="
echo "hello-world-payload" > "$ROOT/readme.txt"
R3="$(python3 "$AG" send --to t1 "readtest:$ROOT/readme.txt" 2>/dev/null)"
has "read-only allows fs read" "$R3" "read 20 bytes"
R4="$(python3 "$AG" send --to t1 "writetest:$ROOT/blocked.txt" 2>/dev/null)"
has "read-only DENIES fs write" "$R4" "write denied"
chk "denied write did NOT create file" "$([ -e "$ROOT/blocked.txt" ] && echo yes || echo no)" "no"
R3b="$(python3 "$AG" send --to t1 "readtest:/etc/hosts" 2>/dev/null)"
has "reads confined to cwd (outside path denied)" "$R3b" "read denied"
R6="$(python3 "$AG" send --to t1 "termtest" 2>/dev/null)"
has "read-only DENIES terminal" "$R6" "terminal denied"

echo "== list / status =="
LST="$(python3 "$AG" list 2>/dev/null)"
has "list shows t1" "$LST" "t1"
has "list shows idle" "$LST" "idle"
STS="$(python3 "$AG" status --to t1 2>/dev/null)"
has "status shows engine grok" "$STS" '"engine": "grok"'

echo "== read by turn =="
RD="$(python3 "$AG" read --to t1 --turn 1 2>/dev/null)"
has "read turn1 == OK" "$RD" "OK"

echo "== Feature 3: structured output (--schema) =="
SU="$(python3 "$HERE/schema_util.py" 2>&1)"
has "schema_util pure-function self-check OK" "$SU" "self-check OK"
echo '{"type":"object","required":["n","ok"],"properties":{"n":{"type":"integer"},"ok":{"type":"boolean"}}}' > "$ROOT/sc.json"
SCR1="$(python3 "$AG" send --to t1 "schematest" --schema "$ROOT/sc.json" 2>/dev/null)"
has "schema: invalid-then-valid retry surfaces the valid JSON (n)" "$SCR1" '"n": 7'
has "schema: validated reply has ok=true" "$SCR1" '"ok": true'
SCR2="$(python3 "$AG" send --to t1 "schemafail" --schema "$ROOT/sc.json" 2>&1)"
has "schema: unmet after retries -> clear error" "$SCR2" "schema not satisfied"
SCR3="$(python3 "$AG" send --to t1 "schematest" --schema "$ROOT/nope.json" 2>&1)"
has "schema: missing schema file -> clear error" "$SCR3" "cannot read/parse"

echo "== Feature 1: live streaming (grok native chunks) =="
stream_check t1

echo "== codex (MCP) multi-turn + threadId capture + read-only sandbox =="
export CODEX_MOCK_SANDBOX_FILE="$ROOT/codex_sandbox.txt"
python3 "$AG" start codex --handle tc1 --cwd "$ROOT" >/dev/null 2>&1
CR1="$(python3 "$AG" send --to tc1 "Remember the number 7. Reply OK" 2>/dev/null)"
has "codex turn1 OK" "$CR1" "OK"
CR2="$(python3 "$AG" send --to tc1 "what number did I say" 2>/dev/null)"
has "codex turn2 remembers 7 (codex-reply warm)" "$CR2" "7"
chk "codex threadId -> session.txt" "$(cat "$AGENT_SESSIONS_DIR/tc1/session.txt" 2>/dev/null)" "mock-thread-abc12345"
chk "codex default sandbox read-only" "$(cat "$ROOT/codex_sandbox.txt" 2>/dev/null)" "read-only"
echo "== Feature 1: live streaming (codex agent_message_content_delta) =="
stream_check tc1
python3 "$AG" stop --to tc1 >/dev/null 2>&1

echo "== gemini (PTY+DB) multi-turn + protobuf reply extraction =="
export AGY_CONV_DIR="$ROOT/gem_conv"; mkdir -p "$AGY_CONV_DIR"
python3 "$AG" start gemini --handle tg1 --cwd "$ROOT" >/dev/null 2>&1
GR1="$(python3 "$AG" send --to tg1 "Remember the number 7. Reply with exactly OK" 2>/dev/null)"
has "gemini turn1 reply from DB payload" "$GR1" "OK"
GR2="$(python3 "$AG" send --to tg1 "what number did I say" 2>/dev/null)"
has "gemini turn2 remembers 7 (warm pty session)" "$GR2" "7"
GLONG="$(python3 "$AG" send --to tg1 "longtest:The quick brown fox jumps over the lazy dog" 2>/dev/null)"
has "gemini long reply extracted from DB (20,1) not screen" "$GLONG" "quick brown fox jumps over the lazy dog"
GSID="$(cat "$AGENT_SESSIONS_DIR/tg1/session.txt" 2>/dev/null)"
chk "gemini conv-id captured" "$([ -n "$GSID" ] && echo yes || echo no)" "yes"
echo "== Feature 1: live streaming (gemini DB (20,1) growth) =="
stream_check tg1
python3 "$AG" stop --to tg1 >/dev/null 2>&1

echo "== Feature 2: cancel (grok graceful) + reattach (session/load) =="
export GROK_MOCK_LOAD_FILE="$ROOT/grok_load.txt"; rm -f "$GROK_MOCK_LOAD_FILE"
python3 "$AG" start grok --handle gcan --cwd "$ROOT" >/dev/null 2>&1
python3 "$AG" send --to gcan "cancelnow please" --bg >/dev/null 2>&1
wait_for gcan busy; chk "grok cancel: busy before cancel" "$(cat "$AGENT_SESSIONS_DIR/gcan/status" 2>/dev/null)" "busy"
CN="$(python3 "$AG" cancel --to gcan 2>/dev/null)"
has "grok cancel: graceful" "$CN" '"graceful": true'
wait_for gcan idle; chk "grok cancel: session warm (idle) after graceful cancel" "$(cat "$AGENT_SESSIONS_DIR/gcan/status" 2>/dev/null)" "idle"
RW="$(python3 "$AG" send --to gcan "Remember the number 7. Reply OK" 2>/dev/null)"
has "grok cancel: follow-up after cancel works (warm)" "$RW" "OK"
GSID0="$(cat "$AGENT_SESSIONS_DIR/gcan/session.txt" 2>/dev/null)"
python3 "$AG" stop --to gcan >/dev/null 2>&1; sleep 1
RES="$(python3 "$AG" start grok --handle gcan --cwd "$ROOT" --resume 2>/dev/null)"
has "grok reattach: start --resume reports resumed" "$RES" '"resumed": true'
chk "grok reattach: session/load called with the saved id" "$(cat "$ROOT/grok_load.txt" 2>/dev/null)" "$GSID0"
RR="$(python3 "$AG" send --to gcan "hello there" 2>/dev/null)"
has "grok reattach: session answers after reattach" "$RR" "ack"
python3 "$AG" stop --to gcan >/dev/null 2>&1
unset GROK_MOCK_LOAD_FILE

echo "== Feature 2: cancel (codex killed->terminal) + reattach (codex-reply) =="
export CODEX_MOCK_FIRSTTOOL_FILE="$ROOT/codex_firsttool.txt"; rm -f "$CODEX_MOCK_FIRSTTOOL_FILE"
python3 "$AG" start codex --handle ccan --cwd "$ROOT" >/dev/null 2>&1
python3 "$AG" send --to ccan "hello" >/dev/null 2>&1     # a normal turn first so a threadId is saved
python3 "$AG" send --to ccan "cancelnow please" --bg >/dev/null 2>&1
wait_for ccan busy; chk "codex cancel: busy before cancel" "$(cat "$AGENT_SESSIONS_DIR/ccan/status" 2>/dev/null)" "busy"
CNC="$(python3 "$AG" cancel --to ccan 2>/dev/null)"
has "codex cancel: NOT graceful (backend killed, S4)" "$CNC" '"graceful": false'
sleep 1; chk "codex cancel: session terminal (cancelled)" "$(cat "$AGENT_SESSIONS_DIR/ccan/status" 2>/dev/null)" "cancelled"
CTID="$(cat "$AGENT_SESSIONS_DIR/ccan/session.txt" 2>/dev/null)"
rm -f "$ROOT/codex_firsttool.txt"
RESC="$(python3 "$AG" start codex --handle ccan --cwd "$ROOT" --resume 2>/dev/null)"
has "codex reattach: start --resume reports resumed" "$RESC" '"resumed": true'
chk "codex reattach: thread id preserved across reattach" "$(cat "$AGENT_SESSIONS_DIR/ccan/session.txt" 2>/dev/null)" "$CTID"
RC="$(python3 "$AG" send --to ccan "continue please" 2>/dev/null)"
chk "codex reattach: first tool after resume is codex-reply" "$(cat "$ROOT/codex_firsttool.txt" 2>/dev/null)" "codex-reply"
python3 "$AG" stop --to ccan >/dev/null 2>&1
unset CODEX_MOCK_FIRSTTOOL_FILE

echo "== Feature 2: cancel (gemini graceful) + reattach (--conversation) =="
python3 "$AG" start gemini --handle gemcan --cwd "$ROOT" >/dev/null 2>&1
python3 "$AG" send --to gemcan "hello" >/dev/null 2>&1    # first turn creates the conv db + saves uuid
GEMID="$(cat "$AGENT_SESSIONS_DIR/gemcan/session.txt" 2>/dev/null)"
GEMDB="$AGY_CONV_DIR/$GEMID.db"
python3 "$AG" send --to gemcan "cancelnow please" --bg >/dev/null 2>&1
wait_for gemcan busy; chk "gemini cancel: busy before cancel" "$(cat "$AGENT_SESSIONS_DIR/gemcan/status" 2>/dev/null)" "busy"
CNG="$(python3 "$AG" cancel --to gemcan 2>/dev/null)"
has "gemini cancel: graceful (single \\x03)" "$CNG" '"graceful": true'
wait_for gemcan idle; chk "gemini cancel: session warm (idle) after cancel" "$(cat "$AGENT_SESSIONS_DIR/gemcan/status" 2>/dev/null)" "idle"
# no test-side sleep: the daemon's post-cancel _wait_pty_quiesce settles agy before the follow-up injects
RWG="$(python3 "$AG" send --to gemcan "Remember the number 7. Reply OK" 2>/dev/null)"
has "gemini cancel: follow-up after cancel works (warm, daemon-settled)" "$RWG" "OK"
IDX0="$(db_maxidx "$GEMDB")"
python3 "$AG" stop --to gemcan >/dev/null 2>&1; sleep 1
RESG="$(python3 "$AG" start gemini --handle gemcan --cwd "$ROOT" --resume 2>/dev/null)"
has "gemini reattach: start --resume reports resumed" "$RESG" '"resumed": true'
chk "gemini reattach: same conv id preserved" "$(cat "$AGENT_SESSIONS_DIR/gemcan/session.txt" 2>/dev/null)" "$GEMID"
RRG="$(python3 "$AG" send --to gemcan "hello again" 2>/dev/null)"
has "gemini reattach: session answers after reattach" "$RRG" "ack"
IDX1="$(db_maxidx "$GEMDB")"
chk "gemini reattach: appended to the SAME conv db (idx grew)" "$([ "$IDX1" -gt "$IDX0" ] && echo grew || echo same)" "grew"
python3 "$AG" stop --to gemcan >/dev/null 2>&1

echo "== Feature 2+3: cancel STOPS a --schema retry loop (panel HIGH: _cancel must survive retries) =="
echo '{"type":"object","required":["x"],"properties":{"x":{"type":"integer"}}}' > "$ROOT/sc2.json"
python3 "$AG" start grok --handle scancel --cwd "$ROOT" >/dev/null 2>&1
# cancelnow emits ticks (never valid JSON); with --schema it would retry to exhaustion, but a mid-turn
# cancel must abort the loop promptly (pre-fix: each retry's send cleared _cancel -> cancel ignored).
python3 "$AG" send --to scancel --schema "$ROOT/sc2.json" --bg "cancelnow please" >/dev/null 2>&1
wait_for scancel busy
CSC="$(python3 "$AG" cancel --to scancel 2>/dev/null)"
has "schema-cancel: cancel reports graceful" "$CSC" '"graceful": true'
wait_for scancel idle
chk "schema-cancel: session warm (idle), not hung" "$(cat "$AGENT_SESSIONS_DIR/scancel/status" 2>/dev/null)" "idle"
has "schema-cancel: turn recorded cancelled (retry loop aborted, not exhausted)" "$(tail -1 "$AGENT_SESSIONS_DIR/scancel/turns.jsonl" 2>/dev/null)" '"status": "cancelled"'
python3 "$AG" stop --to scancel >/dev/null 2>&1

echo "== Feature 6: broadcast (--to-all / --engines, parallel, failure-isolated) =="
python3 "$AG" start grok  --handle bc1 --cwd "$ROOT" >/dev/null 2>&1
python3 "$AG" start codex --handle bc2 --cwd "$ROOT" >/dev/null 2>&1
BCA="$(python3 "$AG" send --to-all "hello all" 2>/dev/null)"
has "broadcast --to-all reaches bc1 (grok)" "$BCA" '"bc1"'
has "broadcast --to-all reaches bc2 (codex)" "$BCA" '"bc2"'
has "broadcast collects replies" "$BCA" "ack"
BCE="$(python3 "$AG" send --engines codex "hello codex only" 2>/dev/null)"
has "broadcast --engines codex reaches bc2" "$BCE" '"bc2"'
chk "broadcast --engines codex EXCLUDES grok bc1" "$(printf '%s' "$BCE" | grep -c '\"bc1\"')" "0"
python3 "$AG" stop --to bc1 >/dev/null 2>&1
python3 "$AG" stop --to bc2 >/dev/null 2>&1

echo "== Feature 5: a2a blackboard (post/read, from-tag, --since, topic isolation) =="
python3 "$AG" board post --topic chan --from alice "first message" >/dev/null 2>&1
python3 "$AG" board post --topic chan --from bob "second message" >/dev/null 2>&1
BR="$(python3 "$AG" board read --topic chan 2>/dev/null)"
has "board read shows posted msg" "$BR" "first message"
has "board read carries the from-tag" "$BR" '"from": "bob"'
BRS="$(python3 "$AG" board read --topic chan --since 1 2>/dev/null)"
has "board --since 1 shows message 2" "$BRS" "second message"
chk "board --since 1 EXCLUDES message 1" "$(printf '%s' "$BRS" | grep -c 'first message')" "0"
python3 "$AG" board post --from carol "default-topic msg" >/dev/null 2>&1
BRD="$(python3 "$AG" board read 2>/dev/null)"
has "board default topic isolated from chan" "$BRD" "default-topic msg"
chk "board default topic has no chan msgs" "$(printf '%s' "$BRD" | grep -c 'first message')" "0"
python3 "$AG" gc --days 0 >/dev/null 2>&1
chk "gc preserves the .board dir (not swept as a dead session)" "$([ -d "$AGENT_SESSIONS_DIR/.board" ] && echo yes || echo no)" "yes"
F1="$(python3 "$AG" send --to t1 --from peerX "tagged turn" 2>/dev/null)"
has "send --from tags the turn record in turns.jsonl" "$(tail -1 "$AGENT_SESSIONS_DIR/t1/turns.jsonl" 2>/dev/null)" '"from": "peerX"'

echo "== full-auto allows writes =="
python3 "$AG" start grok --handle t2 --cwd "$ROOT" --full-auto >/dev/null 2>&1
R5="$(python3 "$AG" send --to t2 "writetest:$ROOT/allowed.txt" 2>/dev/null)"
has "full-auto allows fs write" "$R5" "write ok"
chk "full-auto write created file" "$([ -e "$ROOT/allowed.txt" ] && echo yes || echo no)" "yes"
python3 "$AG" stop --to t2 >/dev/null 2>&1

echo "== stop =="
python3 "$AG" stop --to t1 >/dev/null 2>&1
sleep 1
chk "stopped status terminal" "$(cat "$AGENT_SESSIONS_DIR/t1/status" 2>/dev/null)" "stopped"

echo "== dead-daemon stale sweep =="
SESSION_STALE_SEC=1 python3 "$AG" start grok --handle t3 --cwd "$ROOT" >/dev/null 2>&1
WD="$(cat "$AGENT_SESSIONS_DIR/t3/wd_pid" 2>/dev/null)"
kill -9 "$WD" 2>/dev/null
sleep 2
LST3="$(SESSION_STALE_SEC=1 python3 "$AG" list 2>/dev/null)"
has "killed daemon shows stale" "$LST3" "stale"

echo "== security: handle traversal cannot escape / delete arbitrary dirs =="
SENTINEL="$ROOT/SENTINEL_KEEP"; mkdir -p "$SENTINEL"
python3 "$AG" start grok --handle "../SENTINEL_KEEP" --cwd "$ROOT" >/dev/null 2>&1; rc=$?
chk "traversal handle rejected (exit 2)" "$rc" 2
chk "sentinel dir survived (no rmtree escape)" "$([ -d "$SENTINEL" ] && echo yes || echo no)" "yes"
python3 "$AG" start grok --handle "bad/name" --cwd "$ROOT" >/dev/null 2>&1; chk "slash handle rejected" "$?" 2

echo "== hang surfacing: a hung turn -> error to caller + hung_killed + fast-fail follow-ups =="
SESSION_HANG_SEC=2 python3 "$AG" start grok --handle thang --cwd "$ROOT" >/dev/null 2>&1
HOUT="$(python3 "$AG" send --to thang "HANGNOW please" 2>&1)"
has "hung turn returns an error to the caller (not silence)" "$HOUT" "timed out"
sleep 1
chk "hung session marked hung_killed" "$(cat "$AGENT_SESSIONS_DIR/thang/status" 2>/dev/null)" "hung_killed"
FF="$(python3 "$AG" send --to thang "again?" 2>&1)"
has "follow-up to hung session fast-fails (no infinite wait)" "$FF" "not live"

echo "== gc sweeps non-live (unknown/corrupt) session dirs =="
Z="$AGENT_SESSIONS_DIR/zombie"; mkdir -p "$Z"; echo unknown > "$Z/status"; echo 999999 > "$Z/wd_pid"
touch -t 200001010000 "$Z/daemon.log" 2>/dev/null
python3 "$AG" gc --days 0 >/dev/null 2>&1
chk "gc removed unknown-status dir" "$([ -d "$Z" ] && echo yes || echo no)" "no"

echo "== hardening: concurrent same-handle start -> exactly one live, no rmtree race =="
python3 "$AG" start grok --handle dup --cwd "$ROOT" >"$ROOT/dupA.out" 2>&1 &
PA=$!
python3 "$AG" start grok --handle dup --cwd "$ROOT" >"$ROOT/dupB.out" 2>&1 &
PB=$!
wait $PA; RA=$?
wait $PB; RB=$?
OKN=$(grep -l '"status": "idle"' "$ROOT/dupA.out" "$ROOT/dupB.out" 2>/dev/null | wc -l | tr -d ' ')
ALN=$(grep -l 'already live' "$ROOT/dupA.out" "$ROOT/dupB.out" 2>/dev/null | wc -l | tr -d ' ')
chk "concurrent start: exactly ONE became idle" "$OKN" "1"
chk "concurrent start: the loser reported 'already live' (not rmtree+relaunch)" "$ALN" "1"
chk "concurrent start: the session is live + usable" "$(python3 "$AG" send --to dup "hi" 2>/dev/null)" "ack"
python3 "$AG" stop --to dup >/dev/null 2>&1

echo "== hardening: sessions dir is private 0700 (loose perms get tightened) =="
chmod 0777 "$AGENT_SESSIONS_DIR" 2>/dev/null
python3 "$AG" list >/dev/null 2>&1
PERM="$(stat -f '%Lp' "$AGENT_SESSIONS_DIR" 2>/dev/null || stat -c '%a' "$AGENT_SESSIONS_DIR" 2>/dev/null)"
chk "base sessions dir tightened to 0700" "$PERM" "700"

echo
echo "==================================="
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL GREEN" || echo "FAILURES PRESENT"
exit "$FAIL"
