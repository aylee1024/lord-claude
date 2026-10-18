#!/opt/homebrew/bin/python3.12
"""A deterministic Lord protocol peer for headless engine checks."""
import argparse
import http.server
import json
import os
import socketserver
import threading
import time


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.record({})
        if self.path == '/health':
            self.reply({'ok': True, 'pid': os.getpid(), 'version': 'fake-1'})
        elif self.path == '/agents':
            self.reply(list(self.server.agents.values()))
        elif self.path == '/usage':
            self.reply({'chatgpt': {'usedPercent': 47, 'resetsAt': None, 'credits': None}})
        else:
            self.send_error(404)

    def record(self, body):
        with self.server.lock:
            with open(self.server.log_path, 'a') as out:
                out.write(json.dumps({'path': self.path, 'body': body, 'time': time.time()}) + '\n')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        self.record(body)
        aid = body.get('agentId', '')
        if self.path == '/open':
            self.server.agents.setdefault(aid, {**body, 'model': body.get('model', 'fake-model'), 'effort': body.get('effort', 'high'), 'state': 'idle', 'activity': ''})
            self.reply({'vendorSession': 'fake-' + aid, 'model': self.server.agents[aid]['model'], 'effort': self.server.agents[aid]['effort']})
        elif self.path == '/interrupt':
            self.server.stops.setdefault(aid, threading.Event()).set()
            self.reply({'ok': True})
        elif self.path == '/steer':
            self.reply({'ok': True, 'steered': self.server.agents.get(aid, {}).get('state') == 'running'})
        elif self.path == '/tool_result':
            self.server.results[aid] = body
            self.reply({'ok': True})
        elif self.path == '/close':
            self.server.agents.pop(aid, None)
            self.reply({'ok': True})
        elif self.path in ('/turn', '/continue'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.end_headers()
            agent = self.server.agents[aid]
            agent['state'] = 'running'
            text = body.get('text', '')
            try:
                self.event({'type': 'activity', 'text': 'fake working'})
                if 'SLEEP' in text:
                    self.server.stops.setdefault(aid, threading.Event()).wait(60)
                if 'SPAWNCHILD' in text:
                    self.event({'type': 'tool', 'callId': 'fake-call-' + aid, 'name': 'Agent', 'input': {'subagent_type': 'lord:gemini', 'description': 'fake child', 'prompt': 'CHILD', 'run_in_background': False}})
                    return
                if 'FAIL' in text:
                    self.event({'type': 'error', 'message': 'fake vendor failed', 'retryable': False})
                    return
                answer = 'FAKE continued ' + self.server.results.get(aid, {}).get('text', '') if self.path == '/continue' else 'FAKE ' + agent['family'] + ': ' + text
                self.event({'type': 'text', 'text': answer})
                self.event({'type': 'done', 'text': answer, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0}, 'model': 'fake-' + agent['family']})
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                agent['state'] = 'idle'
        else:
            self.send_error(404)

    def event(self, data):
        self.wfile.write((json.dumps(data) + '\n').encode())
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--log', required=True)
    args = parser.parse_args()
    server = Server(args.socket, Handler)
    os.chmod(args.socket, 0o600)
    server.agents, server.results, server.stops = {}, {}, {}
    server.lock = threading.Lock()
    server.log_path = args.log
    try:
        server.serve_forever()
    finally:
        server.server_close()
        os.unlink(args.socket)


if __name__ == '__main__':
    main()
