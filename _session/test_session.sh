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
import sys, json, re
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
import sys, os, re, sqlite3, uuid
argv = sys.argv[1:]
prompt = None
for i, a in enumerate(argv):
    if a in ("-i", "--prompt-interactive") and i + 1 < len(argv):
        prompt = argv[i + 1]; break
CONV = os.environ.get("AGY_CONV_DIR", os.path.expanduser("~/.gemini/antigravity-cli/conversations"))
os.makedirs(CONV, exist_ok=True)
con = sqlite3.connect(os.path.join(CONV, str(uuid.uuid4()) + ".db"))
con.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob, PRIMARY KEY(idx))")
con.commit()
state = {"number": None, "idx": 0}
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

echo "== codex (MCP) multi-turn + threadId capture + read-only sandbox =="
export CODEX_MOCK_SANDBOX_FILE="$ROOT/codex_sandbox.txt"
python3 "$AG" start codex --handle tc1 --cwd "$ROOT" >/dev/null 2>&1
CR1="$(python3 "$AG" send --to tc1 "Remember the number 7. Reply OK" 2>/dev/null)"
has "codex turn1 OK" "$CR1" "OK"
CR2="$(python3 "$AG" send --to tc1 "what number did I say" 2>/dev/null)"
has "codex turn2 remembers 7 (codex-reply warm)" "$CR2" "7"
chk "codex threadId -> session.txt" "$(cat "$AGENT_SESSIONS_DIR/tc1/session.txt" 2>/dev/null)" "mock-thread-abc12345"
chk "codex default sandbox read-only" "$(cat "$ROOT/codex_sandbox.txt" 2>/dev/null)" "read-only"
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
python3 "$AG" stop --to tg1 >/dev/null 2>&1

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

echo
echo "==================================="
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL GREEN" || echo "FAILURES PRESENT"
exit "$FAIL"
