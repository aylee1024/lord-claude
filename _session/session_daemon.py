#!/usr/bin/env python3
"""
session_daemon.py — one long-lived process per LIVE agent session.

Holds a backend warm and supervises it the way run_with_watchdog.sh supervises a one-shot child:
atomic status writes, a wd_pid heartbeat to daemon.log (so the stale-sweep can spot a zombie),
process-group kill on exit, idle + per-turn-hang backstops. A control channel (unix socket, with an
inbox/ file-drop fallback) lets the `agents` dispatcher send turns and read replies.

Backends (same session dir contract for all):
  - AcpBackend  : grok / composer via `grok agent stdio` (ACP JSON-RPC).
  - McpBackend  : codex via `codex mcp-server` (MCP JSON-RPC).
  - PtyDbBackend: gemini via `agy -i` under a pty + SQLite steps-table signal.

ADDITIVE: never touches run_with_watchdog.sh, the /tmp/<skill>_runs one-shot dirs, or review-panel.
Live sessions live under their own namespace (default /tmp/agent_sessions/<handle>/).

Session dir files: status, session.txt, pid, wd_pid, daemon.log, stderr.log, meta.json,
turns.jsonl, outbox/<n>.md, inbox/, control.sock.
"""
import argparse, glob, json, os, queue, shutil, signal, socket, sqlite3, subprocess, sys, threading, time, traceback

# ---- timing knobs (mirror run_with_watchdog.sh) ----
HEARTBEAT_SEC = int(os.environ.get("SESSION_HEARTBEAT_SEC", "30"))
IDLE_SEC      = int(os.environ.get("SESSION_IDLE_SEC", "1800"))
HANG_SEC      = int(os.environ.get("SESSION_HANG_SEC", "900"))
POLL_SEC      = 0.5
MAX_REQ_BYTES = 8 * 1024 * 1024   # cap a single control request

LIVE_STATES = ("starting", "idle", "busy")
TERMINAL_STATES = ("done", "failed", "hung_killed", "aborted", "stopped")


# --------------------------------------------------------------------------- utils
class _Box:
    __slots__ = ("event", "result", "error")
    def __init__(self):
        self.event = threading.Event(); self.result = None; self.error = None
    def set_result(self, r): self.result = r; self.event.set()
    def set_error(self, e): self.error = e; self.event.set()


class RpcError(Exception):
    def __init__(self, err): self.err = err; super().__init__(str(err))


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _kill_tree(pid, log=lambda *_: None):
    """TERM then KILL the child's whole process group, reaping as we go. A killed child becomes a
    zombie until reaped, and os.kill(pid,0) reports a zombie as alive — so we waitpid(WNOHANG) inside
    the wait loop, otherwise shutdown would block the full TERM+KILL timeout on every exit."""
    if not pid:
        return
    try:
        pgid = os.getpgid(pid)
    except Exception:
        pgid = None

    def dead():
        try:
            r, _ = os.waitpid(pid, os.WNOHANG)   # reap if it's our child
            if r == pid:
                return True
        except ChildProcessError:
            pass                                  # not our child / already reaped
        except Exception:
            pass
        return not _pid_alive(pid)

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if dead():
            return
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            return
        except Exception as e:
            log(f"kill_tree {sig}: {e}")
        for _ in range(20):
            if dead():
                return
            time.sleep(0.1)


# --------------------------------------------------------------------------- JSON-RPC client
class JsonRpcStdioClient:
    """Newline-delimited JSON-RPC 2.0 over a child's stdio. Shared by ACP (grok) and MCP (codex).
    Server->client requests are handled OFF the reader thread so a slow fs/terminal handler can never
    stall the reader (and thus the in-flight turn)."""
    def __init__(self, argv, cwd, env, stderr_path, on_notification, on_server_request, log):
        self.log = log
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._stderr_fh = open(stderr_path, "ab")
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_fh,
            cwd=cwd, env=env, text=True, bufsize=1, start_new_session=True,
        )
        self.alive = True
        self._id = 0
        self._wlock = threading.Lock()    # serialises writes to stdin
        self._plock = threading.Lock()    # guards _id + _pending
        self._pending = {}
        self._reader = threading.Thread(target=self._read_loop, name="jsonrpc-reader", daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    self.log(f"[non-json] {line[:200]}")
                    continue
                self._dispatch(msg)
        except Exception as e:
            self.log(f"[reader err] {e}")
        finally:
            self.alive = False
            with self._plock:
                pend = list(self._pending.values())
                self._pending.clear()
            for box in pend:
                box.set_error({"code": -1, "message": "backend stdout closed"})

    def _dispatch(self, msg):
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._plock:
                box = self._pending.pop(msg["id"], None)
            if box is not None:
                box.set_error(msg["error"]) if "error" in msg else box.set_result(msg.get("result", {}))
            return
        if "method" in msg and "id" in msg:
            # off-thread so the reader keeps draining stdout while a handler runs
            threading.Thread(target=self._serve_server_request, args=(msg,), daemon=True).start()
            return
        if "method" in msg:
            try:
                self._on_notification(msg["method"], msg.get("params", {}))
            except Exception as e:
                self.log(f"[notif-handler err] {e}")

    def _serve_server_request(self, msg):
        try:
            result = self._on_server_request(msg["method"], msg.get("params", {}))
            self._send_raw({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except RpcError as re:
            self._send_raw_safe({"jsonrpc": "2.0", "id": msg["id"], "error": re.err})
        except Exception as e:
            self._send_raw_safe({"jsonrpc": "2.0", "id": msg["id"],
                                 "error": {"code": -32000, "message": str(e)}})

    def _send_raw(self, obj):
        with self._wlock:
            if self.proc.poll() is not None:
                raise RuntimeError("backend process exited")
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def _send_raw_safe(self, obj):
        try:
            self._send_raw(obj)
        except Exception as e:
            self.log(f"[send err] {e}")

    def request(self, method, params, timeout):
        box = _Box()
        with self._plock:
            self._id += 1
            mid = self._id
            self._pending[mid] = box
        try:
            self._send_raw({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except Exception:
            with self._plock:
                self._pending.pop(mid, None)
            raise
        if not box.event.wait(timeout):
            with self._plock:
                self._pending.pop(mid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s")
        if box.error is not None:
            raise RpcError(box.error)
        return box.result

    def notify(self, method, params):
        self._send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    def stop(self):
        _kill_tree(self.proc.pid, self.log)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            self._reader.join(timeout=3)
        except Exception:
            pass
        try:
            self._stderr_fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- backend base
class Backend:
    kind = "base"
    def __init__(self):
        self._send_lock = threading.Lock()
    def start(self): raise NotImplementedError
    def _send(self, text): raise NotImplementedError
    def send(self, text):
        # the daemon worker is single-threaded, but this guard makes concurrent send impossible by
        # construction (no cross-turn collector bleed)
        with self._send_lock:
            return self._send(text)
    def stop(self): pass
    def child_pid(self): return None
    def is_alive(self): return True


def _confine(path, base, what):
    """Resolve `path` and require it to live under `base` (the session cwd)."""
    rp = os.path.realpath(path)
    rbase = os.path.realpath(base)
    if rp != rbase and not rp.startswith(rbase + os.sep):
        raise RpcError({"code": -32001, "message": f"{what} denied: '{path}' is outside the session cwd"})
    return rp


# --------------------------------------------------------------------------- grok / composer (ACP)
class AcpBackend(Backend):
    """grok + composer via `grok agent stdio`. Tools run CLIENT-SIDE; read-only = no write channel.
    File access (read and, under --full-auto, write/terminal) is confined to the session cwd."""
    kind = "acp"

    def __init__(self, model, cwd, full_auto, sdir, log):
        super().__init__()
        self.model = model
        self.cwd = cwd
        self.full_auto = full_auto
        self.sdir = sdir
        self.log = log
        self.client = None
        self.session_id = ""
        self._collector = None
        self._terminals = {}
        self._term_seq = 0
        self._tlock = threading.Lock()

    def _client_capabilities(self):
        if self.full_auto:
            return {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True}
        return {"fs": {"readTextFile": True, "writeTextFile": False}, "terminal": False}

    def start(self):
        argv = ["grok", "agent", "--model", self.model]
        if self.full_auto:
            argv.append("--always-approve")
        argv.append("stdio")
        env = dict(os.environ)
        env.pop("GROK_SANDBOX", None)
        self.client = JsonRpcStdioClient(
            argv, self.cwd, env, os.path.join(self.sdir, "stderr.log"),
            self._on_notification, self._on_server_request, self.log,
        )
        self.client.request("initialize",
                            {"protocolVersion": 1, "clientCapabilities": self._client_capabilities()},
                            timeout=60)
        r = self.client.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=60)
        self.session_id = (r or {}).get("sessionId", "")
        return self.session_id

    def child_pid(self):
        return self.client.proc.pid if self.client else None

    def is_alive(self):
        return self.client is not None and self.client.alive

    def _on_notification(self, method, params):
        if method == "session/update":
            u = params.get("update", {})
            if u.get("sessionUpdate") == "agent_message_chunk" and self._collector is not None:
                self._collector.append((u.get("content", {}) or {}).get("text", ""))

    def _on_server_request(self, method, params):
        if method == "fs/read_text_file":
            path = _confine(params.get("path", ""), self.cwd, "read")
            with open(path, "r", errors="replace") as f:
                data = f.read()
            line, limit = params.get("line"), params.get("limit")
            if line is not None or limit is not None:
                lines = data.splitlines(keepends=True)
                start = max(0, int(line) - 1) if line else 0
                end = start + int(limit) if limit else len(lines)
                data = "".join(lines[start:end])
            return {"content": data}
        if method == "fs/write_text_file":
            if not self.full_auto:
                raise RpcError({"code": -32001, "message": "read-only session: file write denied"})
            path = _confine(params.get("path", ""), self.cwd, "write")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(params.get("content", ""))
            return {}
        if method == "terminal/create":
            if not self.full_auto:
                raise RpcError({"code": -32001, "message": "read-only session: terminal denied"})
            return self._term_create(params)
        if method == "terminal/output":
            return self._term_output(params)
        if method == "terminal/wait_for_exit":
            return self._term_wait(params)
        if method in ("terminal/release", "terminal/kill"):
            return self._term_kill(params)
        return {}

    def _term_create(self, params):
        cmd = params.get("command", "")
        cwd = self.cwd  # confine command cwd to the session cwd (ignore caller override)
        env = dict(os.environ)
        for kv in params.get("env", []) or []:
            if isinstance(kv, dict) and "name" in kv:
                env[kv["name"]] = kv.get("value", "")
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with self._tlock:
            self._term_seq += 1
            tid = "t%d" % self._term_seq
            entry = {"proc": proc, "chunks": []}
            self._terminals[tid] = entry

        def drain():
            try:
                for line in proc.stdout:
                    entry["chunks"].append(line)
            except Exception:
                pass
        threading.Thread(target=drain, daemon=True).start()
        return {"terminalId": tid}

    def _term_wait(self, params):
        t = self._terminals.get(params.get("terminalId"))
        if not t:
            return {"exitStatus": {"exitCode": 127}}
        try:
            t["proc"].wait(timeout=HANG_SEC)
        except Exception:
            _kill_tree(t["proc"].pid, self.log)
        rc = t["proc"].returncode
        return {"exitStatus": {"exitCode": rc if rc is not None else 124}}

    def _term_output(self, params):
        t = self._terminals.get(params.get("terminalId"))
        if not t:
            return {"output": "", "exitStatus": {"exitCode": 127}}
        rc = t["proc"].poll()
        return {"output": "".join(t["chunks"]),
                "exitStatus": ({"exitCode": rc} if rc is not None else None)}

    def _term_kill(self, params):
        tid = params.get("terminalId")
        t = self._terminals.get(tid)
        if t:
            _kill_tree(t["proc"].pid, self.log)
            with self._tlock:
                self._terminals.pop(tid, None)
        return {}

    def _send(self, text):
        if not self.client or not self.client.alive:
            raise RuntimeError("backend not alive")
        self._collector = []
        try:
            self.client.request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=HANG_SEC,
            )
        finally:
            reply = "".join(self._collector or [])
            self._collector = None
        return reply

    def stop(self):
        with self._tlock:
            terms = list(self._terminals.values())
            self._terminals.clear()
        for t in terms:
            _kill_tree(t["proc"].pid, self.log)
        if self.client:
            self.client.stop()


# --------------------------------------------------------------------------- codex (MCP)
class McpBackend(Backend):
    """codex via `codex mcp-server`. tools/call codex (start) then codex-reply (continue) = warm session.
    threadId arrives in streamed codex/event notifications. read-only is OS-enforced via the sandbox arg."""
    kind = "mcp"

    def __init__(self, model, cwd, full_auto, sdir, log):
        super().__init__()
        self.model = model or "gpt-5.5"
        self.cwd = cwd
        self.full_auto = full_auto
        self.sdir = sdir
        self.log = log
        self.client = None
        self.thread_id = ""
        self._reasoning = os.environ.get("CODEX_REASONING", "xhigh")

    def start(self):
        env = dict(os.environ)
        self.client = JsonRpcStdioClient(
            ["codex", "mcp-server"], self.cwd, env, os.path.join(self.sdir, "stderr.log"),
            self._on_notification, self._on_server_request, self.log,
        )
        self.client.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "agents-session", "version": "1"},
        }, timeout=60)
        self.client.notify("notifications/initialized", {})
        return ""

    def child_pid(self):
        return self.client.proc.pid if self.client else None

    def is_alive(self):
        return self.client is not None and self.client.alive

    def _on_notification(self, method, params):
        if not self.thread_id:
            tid = self._scan_thread_id(params)
            if tid:
                self._set_thread_id(tid)

    def _on_server_request(self, method, params):
        return {}

    def _set_thread_id(self, tid):
        self.thread_id = tid
        try:
            with open(os.path.join(self.sdir, "session.txt"), "w") as f:
                f.write(tid + "\n")
        except Exception:
            pass

    @staticmethod
    def _scan_thread_id(obj):
        keys = ("threadId", "thread_id", "conversationId", "conversation_id", "session_id")
        found = [None]
        def rec(o):
            if found[0]:
                return
            if isinstance(o, dict):
                for k in keys:                          # prefer the most specific key at this level
                    v = o.get(k)
                    if isinstance(v, str) and len(v) >= 8:
                        found[0] = v
                        return
                for v in o.values():
                    rec(v)
            elif isinstance(o, list):
                for x in o:
                    rec(x)
        rec(obj)
        return found[0]

    def _sandbox(self):
        return "workspace-write" if self.full_auto else "read-only"

    @staticmethod
    def _text_from_result(result):
        try:
            return " ".join(c.get("text", "") for c in (result or {}).get("content", [])
                            if c.get("type") == "text").strip()
        except Exception:
            return ""

    def _send(self, text):
        if not self.client or not self.client.alive:
            raise RuntimeError("backend not alive")
        if not self.thread_id:
            args = {"prompt": text, "model": self.model, "cwd": self.cwd,
                    "approval-policy": "never", "sandbox": self._sandbox(),
                    "config": {"model_reasoning_effort": self._reasoning}}
            result = self.client.request("tools/call", {"name": "codex", "arguments": args}, timeout=HANG_SEC)
            if not self.thread_id:
                tid = self._scan_thread_id(result)
                if tid:
                    self._set_thread_id(tid)
        else:
            result = self.client.request(
                "tools/call",
                {"name": "codex-reply", "arguments": {"threadId": self.thread_id, "prompt": text}},
                timeout=HANG_SEC,
            )
        return self._text_from_result(result)

    def stop(self):
        if self.client:
            self.client.stop()


# --------------------------------------------------------------------------- gemini (PTY + SQLite)
def _read_varint(b, i):
    shift = 0; val = 0
    while i < len(b):
        byte = b[i]; i += 1
        val |= (byte & 0x7f) << shift
        if not (byte & 0x80):
            return val, i
        shift += 7
        if shift > 70:
            return None, i
    return None, i


def _pb_scan(blob, depth=0, out=None, path=()):
    """Minimal protobuf wire scanner. For every length-delimited field it records a UTF-8 string leaf
    AND (always, depth-guarded) attempts to recurse — a nested message whose bytes happen to be printable
    must still be descended, else field (20,1) is missed for longer replies."""
    if out is None:
        out = []
    i = 0; n = len(blob)
    while i < n:
        tag, i = _read_varint(blob, i)
        if tag is None:
            break
        field = tag >> 3; wt = tag & 7
        if wt == 0:
            _, i = _read_varint(blob, i)
        elif wt == 2:
            ln, i = _read_varint(blob, i)
            if ln is None or i + ln > n:
                break
            data = blob[i:i + ln]; i += ln
            try:
                s = data.decode("utf-8")
                if len(s) > 0 and sum(1 for c in s if c.isprintable() or c in "\n\t") / len(s) > 0.9:
                    out.append((path + (field,), s))
            except Exception:
                pass
            if depth < 8 and len(data) >= 2:
                _pb_scan(data, depth + 1, out, path + (field,))
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return out


def _extract_gemini_reply(payload):
    """Assistant reply text is at protobuf path (20,1) of a type-15 step (verified on-machine);
    (20,8) duplicates it, (20,6) is the bot id."""
    strings = _pb_scan(bytes(payload))
    texts = [s for p, s in strings if p == (20, 1)]
    if not texts:
        texts = [s for p, s in strings if p == (20, 8)]
    return "\n".join(t.strip() for t in texts).strip()


class PtyDbBackend(Backend):
    """gemini via `agy -i` under a pty. Turn-end = a new type-15 step at terminal status=3 that is the
    LATEST row and stays stable, after a fresh user (type-14) row for this turn. Reply text comes from
    the db payload. Antigravity has NO write sandbox, so default sessions are not write-confined."""
    kind = "ptydb"

    def __init__(self, model, cwd, full_auto, sdir, log):
        super().__init__()
        self.model = model or "Gemini 3.5 Flash (High)"
        self.cwd = cwd
        self.full_auto = full_auto
        self.sdir = sdir
        self.log = log
        self._master = None
        self._pid = None
        self._started = False
        self._screen = bytearray()
        self._slock = threading.Lock()
        self._conv_db = None
        self._pre_snapshot = {}
        self.CONV_DIR = os.environ.get(
            "AGY_CONV_DIR", os.path.expanduser("~/.gemini/antigravity-cli/conversations"))

    def start(self):
        self._pre_snapshot = self._db_mtimes()
        return ""  # spawn lazily on the first send (agy -i needs the first prompt)

    def _db_mtimes(self):
        out = {}
        try:
            for p in glob.glob(self.CONV_DIR + "/*.db"):
                try:
                    out[p] = os.path.getmtime(p)
                except OSError:
                    pass
        except Exception:
            pass
        return out

    def _spawn(self, first_text):
        import pty, fcntl, termios, struct
        # build argv + env in the PARENT; the child does only async-signal-safe syscalls + execve
        agy_bin = shutil.which("agy") or "agy"
        argv = ["agy", "--model", self.model]
        if self.full_auto:
            argv.append("--dangerously-skip-permissions")
        argv += ["-i", first_text]
        child_env = dict(os.environ, TERM="xterm-256color")
        cwd = self.cwd
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        except Exception:
            pass
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
                try:
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                except Exception:
                    pass
                try:
                    os.chdir(cwd)
                except Exception:
                    pass
                os.execve(agy_bin, argv, child_env)
            except Exception:
                pass
            os._exit(127)
        os.close(slave)
        self._master = master
        self._pid = pid
        self._started = True
        try:
            with open(os.path.join(self.sdir, "pid"), "w") as f:
                f.write(str(pid))
        except Exception:
            pass
        threading.Thread(target=self._drain, name="pty-drain", daemon=True).start()

    def _drain(self):
        import select as _sel
        while True:
            try:
                r, _, _ = _sel.select([self._master], [], [], 0.5)
                if self._master in r:
                    data = os.read(self._master, 65536)
                    if not data:
                        break
                    with self._slock:
                        self._screen.extend(data)
            except OSError:
                break

    def is_alive(self):
        if not self._started:
            return True
        return self._pid is not None and _pid_alive(self._pid)

    def child_pid(self):
        return self._pid

    def _query(self, sql, args=()):
        con = sqlite3.connect(f"file:{self._conv_db}?mode=ro", uri=True, timeout=3)
        try:
            con.execute("PRAGMA busy_timeout=2000")
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _max_idx(self):
        if not self._conv_db:
            return -1
        try:
            rows = self._query("SELECT COALESCE(MAX(idx),-1) FROM steps")
            return rows[0][0] if rows else -1
        except Exception:
            return -1

    def _wait_new_db(self, timeout=45):
        """Prefer a brand-new db path (almost certainly this run's); fall back to an mtime-advanced one."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self._db_mtimes()
            fresh = [p for p in cur if p not in self._pre_snapshot]
            if fresh:
                return max(fresh, key=lambda p: cur[p])
            advanced = [(m, p) for p, m in cur.items() if self._pre_snapshot.get(p, 0) < m]
            if advanced:
                return max(advanced)[1]
            time.sleep(0.5)
        return None

    def _wait_turn_end(self, pre_max, timeout):
        """Done when (a) a fresh user row (type-14) beyond pre_max proves this turn's prompt registered,
        (b) a terminal assistant row (type-15, status-3) exists AFTER that user row, and (c) the rows
        stop changing for a settle window. agy writes trailing trajectory/metadata rows (type 23/98)
        AFTER the assistant message, so we must NOT require the assistant row to be the latest one."""
        deadline = time.time() + timeout
        stable_sig = None
        stable_since = None
        while time.time() < deadline:
            time.sleep(0.8)
            try:
                rows = self._query(
                    "SELECT idx, step_type, status FROM steps WHERE idx>? ORDER BY idx", (pre_max,))
            except Exception:
                continue
            if not rows:
                continue
            user_idx = max((r[0] for r in rows if r[1] == 14), default=None)
            answered = (user_idx is not None
                        and any(r[0] > user_idx and r[1] == 15 and r[2] == 3 for r in rows))
            if answered:
                sig = (rows[-1][0], len(rows))      # settle: max idx + row count must stop changing
                if sig == stable_sig:
                    if stable_since and time.time() - stable_since >= 1.5:
                        payloads = self._query(
                            "SELECT step_payload FROM steps WHERE idx>? AND step_type=15 AND status=3 "
                            "ORDER BY idx", (user_idx,))
                        return [r[0] for r in payloads]
                else:
                    stable_sig = sig
                    stable_since = time.time()
            else:
                stable_sig = None
                stable_since = None
        raise TimeoutError("gemini turn-end not detected via DB signal")

    def _screen_tail_text(self):
        import re as _re
        with self._slock:
            raw = bytes(self._screen).decode("utf-8", "replace")
        ansi = _re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r')
        return ansi.sub('', raw)[-400:].strip()

    def _inject(self, text):
        """Write a follow-up prompt into the pty. Bracketed paste keeps a multi-line prompt as ONE
        submission (the line discipline would otherwise submit each embedded newline early)."""
        if "\n" in text:
            payload = b"\x1b[200~" + text.encode() + b"\x1b[201~\r"
        else:
            payload = text.encode() + b"\r"
        os.write(self._master, payload)

    def _send(self, text):
        if not self._started:
            self._pre_snapshot = self._db_mtimes()
            self._spawn(text)
            self._conv_db = self._wait_new_db()
            if not self._conv_db:
                raise RuntimeError("gemini conversation db did not appear")
            uuid = os.path.splitext(os.path.basename(self._conv_db))[0]
            try:
                with open(os.path.join(self.sdir, "session.txt"), "w") as f:
                    f.write(uuid + "\n")
            except Exception:
                pass
            payloads = self._wait_turn_end(-1, HANG_SEC)
        else:
            if self._master is None or not self.is_alive():
                raise RuntimeError("gemini pty not alive")
            pre = self._max_idx()
            self._inject(text)
            payloads = self._wait_turn_end(pre, HANG_SEC)
        reply = "\n".join(_extract_gemini_reply(p) for p in payloads).strip()
        if not reply:
            self.log("gemini: empty DB extraction, falling back to screen text")
            reply = self._screen_tail_text()
        return reply

    def stop(self):
        try:
            if self._master is not None:
                try:
                    os.write(self._master, b"\x03"); time.sleep(0.2); os.write(self._master, b"\x04")
                except Exception:
                    pass
        finally:
            _kill_tree(self._pid, self.log)
            if self._master is not None:
                try:
                    os.close(self._master)
                except Exception:
                    pass
                self._master = None


def make_backend(engine, model, cwd, full_auto, sdir, log):
    if engine in ("grok", "composer"):
        return AcpBackend(model, cwd, full_auto, sdir, log)
    if engine == "codex":
        return McpBackend(model, cwd, full_auto, sdir, log)
    if engine == "gemini":
        return PtyDbBackend(model, cwd, full_auto, sdir, log)
    raise NotImplementedError(f"engine '{engine}' backend not built yet")


# --------------------------------------------------------------------------- daemon
class Daemon:
    def __init__(self, sdir, engine, model, cwd, full_auto):
        self.sdir = sdir
        self.engine = engine
        self.model = model
        self.cwd = cwd
        self.full_auto = full_auto
        self.status = "starting"
        self.turn_q = queue.Queue()
        self.turn_counter = 0
        self._counter_lock = threading.Lock()
        self.last_activity = time.time()
        self.active_turn_started = None
        self._stop = threading.Event()
        self._terminal_reason = "done"
        self.sock = None
        self.backend = make_backend(engine, model, cwd, full_auto, sdir, self.log)

    def _p(self, *a): return os.path.join(self.sdir, *a)

    def log(self, msg):
        try:
            with open(self._p("daemon.log"), "a") as f:
                f.write(f"[{int(time.time())}] {msg}\n")
        except Exception:
            pass

    def write_status(self, s):
        self.status = s
        tmp = self._p("status.tmp")
        with open(tmp, "w") as f:
            f.write(s + "\n")
        os.replace(tmp, self._p("status"))

    def _write(self, name, content):
        tmp = self._p(name + ".tmp")
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, self._p(name))

    def _append_turn(self, n, prompt, reply, status):
        rec = {"n": n, "ts": int(time.time()), "prompt_chars": len(prompt or ""),
               "reply_chars": len(reply or ""), "status": status}
        with open(self._p("turns.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def run(self):
        os.makedirs(self.sdir, exist_ok=True)
        os.makedirs(self._p("outbox"), exist_ok=True)
        os.makedirs(self._p("inbox"), exist_ok=True)
        self._write("wd_pid", str(os.getpid()))
        self._write("meta.json", json.dumps({
            "engine": self.engine, "model": self.model, "kind": self.backend.kind,
            "cwd": self.cwd, "full_auto": self.full_auto, "started": int(time.time()),
        }))
        self.write_status("starting")
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.log(f"daemon start engine={self.engine} model={self.model} cwd={self.cwd} full_auto={self.full_auto}")
        # bind the control socket BEFORE announcing idle, so a fast send never races a missing listener
        try:
            self._bind_socket()
        except Exception as e:
            self.log(f"socket bind FAILED: {repr(e)}")
            self.write_status("failed")
            return 1
        try:
            sid = self.backend.start()
            self._write("session.txt", (sid or "") + "\n")
            cp = self.backend.child_pid()
            if cp:
                self._write("pid", str(cp))
        except Exception as e:
            self.log(f"backend start FAILED: {repr(e)}\n{traceback.format_exc()}")
            self.write_status("failed")
            return 1
        self.write_status("idle")
        self.log(f"backend ready session_id={sid}")
        threading.Thread(target=self._worker, name="turn-worker", daemon=True).start()
        threading.Thread(target=self._heartbeat, name="heartbeat", daemon=True).start()
        self._serve()
        self._shutdown()
        return 0

    def _on_signal(self, *_):
        self._terminal_reason = "aborted"
        self._stop.set()

    def _bind_socket(self):
        sockpath = self._p("control.sock")
        try:
            os.unlink(sockpath)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(sockpath)
        self.sock.listen(8)
        self.sock.settimeout(POLL_SEC)

    def _serve(self):
        last_inbox = 0
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
            except socket.timeout:
                pass
            except OSError:
                break
            now = time.time()
            if now - last_inbox >= POLL_SEC:
                self._poll_inbox()
                last_inbox = now

    def _poll_inbox(self):
        d = self._p("inbox")
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        except FileNotFoundError:
            return
        for name in names:
            path = os.path.join(d, name)
            try:
                with open(path) as f:
                    req = json.load(f)
                os.unlink(path)
            except Exception:
                continue
            # handle off the accept loop so a long turn doesn't block accept()/further polling
            threading.Thread(target=self._serve_inbox, args=(name, req), daemon=True).start()

    def _serve_inbox(self, name, req):
        resp = self._handle_request(req)
        d = self._p("inbox")
        try:
            tmp = os.path.join(d, name[:-5] + ".reply.tmp")
            with open(tmp, "w") as f:
                json.dump(resp, f)
            os.replace(tmp, os.path.join(d, name[:-5] + ".reply"))
        except Exception:
            pass

    def _handle_conn(self, conn):
        try:
            conn.settimeout(HANG_SEC + 30)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_REQ_BYTES:
                    conn.sendall((json.dumps({"ok": False, "error": "request too large"}) + "\n").encode())
                    return
            if not data:
                return
            req = json.loads(data.decode().splitlines()[0])
            resp = self._handle_request(req)
            conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _read_session_id(self):
        try:
            return open(self._p("session.txt")).read().strip()
        except Exception:
            return ""

    def _handle_request(self, req):
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "status": self.status, "session_id": self._read_session_id(),
                    "engine": self.engine, "model": self.model, "kind": self.backend.kind,
                    "turns": self.turn_counter}
        if op == "stop":
            self._terminal_reason = "stopped"
            self._stop.set()
            return {"ok": True, "status": "stopping"}
        if op == "read":
            n = int(req.get("turn") or self.turn_counter)
            f = self._p("outbox", f"{n}.md")
            return {"ok": True, "turn": n, "reply": (open(f).read() if os.path.exists(f) else "")}
        if op == "send":
            with self._counter_lock:
                self.turn_counter += 1
                n = self.turn_counter
            text = req.get("text", "")
            self.last_activity = time.time()
            if req.get("bg"):
                self.turn_q.put((n, text, None))
                return {"ok": True, "turn": n, "status": "queued"}
            box = _Box()
            self.turn_q.put((n, text, box))
            if not box.event.wait(HANG_SEC + 20):
                return {"ok": False, "turn": n, "error": "turn wait timed out"}
            if box.error is not None:
                return {"ok": False, "turn": n, "error": str(box.error)}
            return {"ok": True, "turn": n, "status": "done",
                    "reply": box.result.get("reply"), "reply_file": box.result.get("reply_file")}
        return {"ok": False, "error": f"unknown op '{op}'"}

    def _worker(self):
        while not self._stop.is_set():
            try:
                n, text, box = self.turn_q.get(timeout=POLL_SEC)
            except queue.Empty:
                continue
            self.active_turn_started = time.time()
            self.write_status("busy")
            self.log(f"turn {n} start ({len(text)} chars)")
            result = None
            err = None
            try:
                reply = self.backend.send(text)
                rf = self._p("outbox", f"{n}.md")
                with open(rf, "w") as f:
                    f.write(reply or "")
                self._append_turn(n, text, reply, "done")
                self.log(f"turn {n} done ({len(reply or '')} chars)")
                result = {"reply": reply, "reply_file": rf}
            except Exception as e:
                self.log(f"turn {n} FAILED: {repr(e)}")
                self._append_turn(n, text, "", "failed")
                err = str(e)
            finally:
                self.active_turn_started = None
                self.last_activity = time.time()
                if not self._stop.is_set():
                    self.write_status("idle")
                # settle the status write BEFORE unblocking the caller, so a stop/idle the caller
                # triggers next can't race this idle write past the terminal status
                if box:
                    box.set_error(err) if err is not None else box.set_result(result)

    def _heartbeat(self):
        last_hb = 0
        while not self._stop.is_set():
            time.sleep(POLL_SEC)
            now = time.time()
            if now - last_hb >= HEARTBEAT_SEC:
                self.log(f"heartbeat status={self.status} turns={self.turn_counter}")
                last_hb = now
            if not self.backend.is_alive() and not self._stop.is_set():
                self.log("backend process exited unexpectedly -> aborted")
                self._terminal_reason = "aborted"
                self._stop.set()
                break
            if self.active_turn_started and now - self.active_turn_started > HANG_SEC:
                self.log("turn exceeded HANG_SEC -> hung_killed")
                self._terminal_reason = "hung_killed"
                # actively cancel the in-flight turn so the worker (and any waiting caller) unblocks
                try:
                    self.backend.stop()
                except Exception:
                    pass
                self._stop.set()
                break
            if (not self.active_turn_started and self.turn_q.empty()
                    and now - self.last_activity > IDLE_SEC):
                self.log("idle timeout -> shutting down")
                self._terminal_reason = "done"
                self._stop.set()
                break

    def _shutdown(self):
        self.log(f"shutdown reason={self._terminal_reason}")
        try:
            self.backend.stop()
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        try:
            os.unlink(self._p("control.sock"))
        except Exception:
            pass
        self.write_status(self._terminal_reason if self._terminal_reason in TERMINAL_STATES else "done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sdir")
    ap.add_argument("--engine", required=True, choices=["grok", "composer", "codex", "gemini"])
    ap.add_argument("--model", default="")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--full-auto", action="store_true")
    args = ap.parse_args()
    d = Daemon(args.sdir, args.engine, args.model, os.path.abspath(args.cwd), args.full_auto)
    return d.run()


if __name__ == "__main__":
    sys.exit(main())
