#!/usr/bin/env python3
"""Isolated OpenCode 2 HTTP contract server; fixtures persist native sessions across process restarts."""
import base64
import copy
import http.server
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

SESSIONS = Path(os.environ['FAKE_OPENCODE_SESSIONS'])
LOG = Path(os.environ['FAKE_OPENCODE_LOG'])
SESSIONS.mkdir(parents=True, exist_ok=True)
CONFIG = json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT') or '{}')
SKILL_FILE = 'frameworks/.claude/skills/sediment-framework/SKILL.md'
REJECTED = 'The user rejected permission to use this specific tool call.'
RULE_DENIED = 'The user has specified a rule which prevents you from using this specific tool call. [rules]'
LOCK = threading.RLock()
EVENTS = []
ANSWERS = {}
ACTIVE = set()
CANCELLED = threading.Event()
TOKENS = {'input': 1224, 'output': 10, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}}
MODEL = {'providerID': '0g', 'id': 'deepseek-v4.1-flash'}
STARTED = time.monotonic()


def identity(prefix):
    return prefix + uuid.uuid4().hex


def record(data):
    with LOCK, LOG.open('a') as out:
        out.write(json.dumps(data, ensure_ascii=False) + '\n')


def load(sid):
    with (SESSIONS / (sid + '.json')).open() as src:
        return json.load(src)


def save(sid, data):
    (SESSIONS / (sid + '.json')).write_text(json.dumps(data, ensure_ascii=False))


def info(sid, data):
    return {'id': sid, 'title': data.get('title', 'New session'), 'agent': data.get('agent', 'orchestrator'),
            'model': data.get('model', MODEL), 'cost': data.get('cost', 0), 'tokens': TOKENS,
            'projectID': 'proj_test', 'time': {'created': 1, 'updated': 1}, 'location': {'directory': os.getcwd()}}


def event(kind, sid, **data):
    with LOCK:
        EVENTS.append({'id': identity('evt_'), 'created': time.time() * 1000, 'type': kind,
                       'data': {'sessionID': sid, **data}})
        if kind not in ('session.text.delta', 'session.reasoning.delta', 'session.tool.input.delta',
                        'session.usage.updated', 'session.idle', 'permission.asked'):
            EVENTS[-1]['durable'] = {'aggregateID': sid, 'seq': len(EVENTS),
                                     'version': 2 if kind in ('session.tool.success', 'session.tool.failed') else 1}


def add_message(sid, message):
    with LOCK:
        data = load(sid)
        data.setdefault('messages', []).append(message)
        save(sid, data)


def say(sid, *chunks):
    mid = identity('msg_')
    event('session.reasoning.delta', sid, assistantMessageID=mid, ordinal=0, delta='private thought')
    event('session.text.started', sid, assistantMessageID=mid, ordinal=1)
    for chunk in chunks:
        event('session.text.delta', sid, assistantMessageID=mid, ordinal=1, delta=chunk)
    event('session.text.ended', sid, assistantMessageID=mid, ordinal=1, text=''.join(chunks))
    add_message(sid, {'id': mid, 'type': 'assistant', 'agent': 'orchestrator', 'model': MODEL,
                      'time': {'created': 1, 'completed': 2}, 'content': [{'type': 'text', 'text': ''.join(chunks)}],
                      'tokens': TOKENS, 'cost': 0.01})
    with LOCK:
        data = load(sid)
        data['title'] = '原生自动标题'
        save(sid, data)


def tool_start(sid, call_id, name, raw):
    mid = identity('msg_')
    event('session.tool.input.started', sid, assistantMessageID=mid, id=call_id, name=name)
    event('session.tool.input.delta', sid, assistantMessageID=mid, id=call_id, delta=json.dumps(raw))
    event('session.tool.input.ended', sid, assistantMessageID=mid, id=call_id, text=json.dumps(raw))
    event('session.tool.called', sid, assistantMessageID=mid, id=call_id, input=raw, executed=False)
    return mid


def tool_end(sid, mid, call_id, output='', error=None):
    if error:
        event('session.tool.failed', sid, assistantMessageID=mid, id=call_id,
              error={'type': 'APIError', 'message': error}, executed=False)
    else:
        event('session.tool.success', sid, assistantMessageID=mid, id=call_id,
              content=[{'type': 'text', 'text': output}], executed=False)


def ask(sid, mid, call_id, action, resources):
    rid = identity('per_')
    ANSWERS[rid] = answers = queue.Queue()
    event('permission.asked', sid, id=rid, action=action, resources=resources,
          source={'type': 'tool', 'messageID': mid, 'id': call_id}, metadata={})
    while not CANCELLED.is_set():
        try:
            return answers.get(timeout=.03)
        except queue.Empty:
            pass
    return None


def finish(sid, cancelled=False, error=None):
    ACTIVE.discard(sid)
    if cancelled:
        event('session.execution.interrupted', sid, reason='user')
    elif error:
        event('session.execution.failed', sid, error={'type': 'APIError', 'message': error})
    else:
        with LOCK:
            data = load(sid)
            data['cost'] = data.get('cost', 0) + .01
            save(sid, data)
        event('session.step.ended', sid, assistantMessageID=identity('msg_'), finish='stop', cost=.01, tokens=TOKENS)
        event('session.usage.updated', sid, cost=data['cost'], tokens=TOKENS)
        event('session.execution.succeeded', sid)
    event('session.idle', sid)


def turn(sid, text):
    ACTIVE.add(sid)
    call_id = identity('call_')
    event('session.execution.started', sid)
    if text == 'crash':
        print('boom', file=sys.stderr, flush=True)
        os._exit(3)
    if text == 'fail':
        finish(sid, error='Internal error: Cannot connect to API')
        return
    if text == 'slow':
        tool_start(sid, call_id, 'shell', {'command': 'sleep 99', 'workdir': os.getcwd()})
        CANCELLED.wait()
        finish(sid, cancelled=True)
        return
    if text in ('tool', 'denied', 'writefw'):
        if text == 'writefw':
            raw = {'path': str(Path.cwd() / 'frameworks/ZZZ.US.md'), 'content': '# ZZZ'}
            mid = tool_start(sid, call_id, 'write', raw)
            tool_end(sid, mid, call_id, 'Wrote file successfully.')
            say(sid, 'written')
        elif text == 'denied':
            mid = tool_start(sid, call_id, 'shell', {'command': 'git push'})
            tool_end(sid, mid, call_id, error=RULE_DENIED)
            say(sid, 'blocked')
        else:
            mid = tool_start(sid, call_id, 'shell', {'command': 'echo hi'})
            tool_end(sid, mid, call_id, 'hi\n')
            say(sid, 'done')
    elif text in ('skill', 'other'):
        if text == 'skill':
            path = str(Path.cwd() / SKILL_FILE)
            mid = tool_start(sid, call_id, 'edit', {'path': path, 'oldString': 'a', 'newString': 'b'})
            answer = ask(sid, mid, call_id, 'edit', [path])
        else:
            mid = tool_start(sid, call_id, 'shell', {'command': 'chown me x'})
            answer = ask(sid, mid, call_id, 'shell', ['chown me x'])
        if answer is None:
            finish(sid, cancelled=True)
            return
        if answer == 'once':
            tool_end(sid, mid, call_id, 'Edit applied successfully.')
            say(sid, 'edited')
        else:
            tool_end(sid, mid, call_id, error=REJECTED)
    else:
        say(sid, 'echo: ', text)
    finish(sid)


def compact(sid, mid):
    ACTIVE.add(sid)
    event('session.compaction.started', sid, reason='manual', inputID=mid)
    if os.environ.get('FAKE_OPENCODE_COMPACT_SLOW'):
        CANCELLED.wait(2)
        if CANCELLED.is_set():
            finish(sid, cancelled=True)
            return
    hints = list(Path(os.environ.get('FAKE_OPENCODE_HINT_DIR', str(LOG.parent))).rglob('*.compact-hint*'))
    hint = '\n'.join(json.loads(p.read_text())['text'] for p in hints if p.is_file())
    for path in hints:
        path.unlink(missing_ok=True)
    with LOCK:
        data = load(sid)
        data['compaction_prompt'] = hint
        save(sid, data)
    add_message(sid, {'id': mid, 'type': 'compaction', 'status': 'completed', 'reason': 'manual',
                      'summary': 'Native summary', 'time': {'created': 1}})
    event('session.compaction.ended', sid, reason='manual', text='Native summary')
    finish(sid)


class NativeHttp(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_native('GET')

    def do_POST(self):
        self.handle_native('POST')

    def do_DELETE(self):
        self.handle_native('DELETE')

    def handle_native(self, method):
        wanted = 'Basic ' + base64.b64encode(('opencode:' + os.environ['OPENCODE_PASSWORD']).encode()).decode()
        if self.headers.get('Authorization') != wanted:
            self.send_json(401, {})
            return
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path != '/api/info' and urllib.parse.parse_qs(url.query).get('location[directory]') != [os.getcwd()]:
            self.send_json(400, {'message': 'location[directory] is required'})
            return
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        record({'http': path, 'method': method, 'body': body})
        if path == '/api/info':
            if time.monotonic() - STARTED < float(os.environ.get('FAKE_OPENCODE_READY_DELAY', '0')):
                self.send_json(503, {'message': 'server starting'})
                return
            if os.environ.get('FAKE_OPENCODE_HANG'):
                time.sleep(60)
                return
            self.send_json(200, {'version': '2.2.5', 'pid': os.getpid(), 'urls': [], 'paths': {'tmp': str(LOG.parent)}})
            return
        if path == '/api/event':
            self.stream_events()
            return
        if path == '/api/plugin':
            if os.environ.get('FAKE_OPENCODE_PLUGIN_NEVER') != '1':
                def mark_plugins_ready():
                    for plugin in CONFIG.get('plugins', []):
                        path = Path(plugin)
                        if path.is_dir() and path.name.endswith('.compaction-plugin'):
                            (path / 'ready').touch()
                threading.Timer(float(os.environ.get('FAKE_OPENCODE_PLUGIN_DELAY', '0')), mark_plugins_ready).start()
            self.send_json(200, {'data': []})
            return
        if path == '/api/model':
            self.send_json(200, {'data': [{'id': 'deepseek-v4.1-flash', 'providerID': '0g',
                                        'limit': {'context': 1000000}}]})
            return
        if path == '/api/session' and method == 'POST':
            sid = identity('ses_')
            data = {'messages': [], 'agent': body.get('agent', 'orchestrator'), 'model': body.get('model', MODEL)}
            with LOCK:
                save(sid, data)
            self.send_json(200, {'data': info(sid, data)})
            return
        pieces = path.strip('/').split('/')
        if len(pieces) < 3 or pieces[:2] != ['api', 'session']:
            self.send_json(404, {'message': 'unknown API'})
            return
        sid = pieces[2]
        try:
            with LOCK:
                data = load(sid)
        except OSError:
            self.send_json(404, {'message': 'session not found'})
            return
        action = pieces[3] if len(pieces) > 3 else ''
        if method == 'GET':
            if action == 'inbox':
                self.send_json(200, {'data': data.get('inbox', [])})
            elif action == 'message':
                self.send_json(200, {'data': data.get('messages', []), 'cursor': {'next': None, 'previous': None}})
            else:
                self.send_json(200, {'data': info(sid, data)})
        elif action == 'fork':
            if os.environ.get('FAKE_OPENCODE_FORK_FAIL'):
                self.send_json(500, {'message': 'fork failed'})
                return
            messages = data.get('messages', [])
            index = next((i for i, m in enumerate(messages) if m['id'] == body.get('before')), len(messages))
            new = identity('ses_')
            copied = copy.deepcopy(data)
            copied['messages'] = copied['messages'][:index]
            with LOCK:
                save(new, copied)
            self.send_json(200, {'data': info(new, copied)})
        elif action == 'inbox' and method == 'DELETE':
            with LOCK:
                existed = any(item['id'] == pieces[4] for item in data.get('inbox', []))
                data['inbox'] = [item for item in data.get('inbox', []) if item['id'] != pieces[4]]
                save(sid, data)
            if existed:
                event('session.inbox.cancelled', sid, inboxID=pieces[4])
            self.send_empty()
        elif action in ('prompt', 'compact'):
            CANCELLED.clear()
            mid = body.get('id') or identity('msg_')
            payload = {'text': body['text']} if action == 'prompt' else {}
            inbox = {'id': mid, 'sessionID': sid, 'type': 'user' if action == 'prompt' else 'compaction',
                     'time': {'created': 1}, 'payload': payload, 'delivery': body.get('delivery', 'queue')}
            if action == 'prompt' and os.environ.get('FAKE_OPENCODE_ACK_INBOX_ONLY') != '1':
                add_message(sid, {'id': mid, 'type': 'user', 'text': body['text'], 'time': {'created': 1}})
            if action == 'prompt' and os.environ.get('FAKE_OPENCODE_PROMPT_DROP_ACK') == '1':
                if os.environ.get('FAKE_OPENCODE_ACK_INBOX_ONLY') == '1':
                    with LOCK:
                        data = load(sid)
                        data.setdefault('inbox', []).append(inbox)
                        save(sid, data)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if action == 'prompt' and body['text'] == 'pending':
                with LOCK:
                    data = load(sid)
                    data.setdefault('inbox', []).append(inbox)
                    save(sid, data)
                self.send_json(200, {'data': inbox})
                return
            self.send_json(200, {'data': inbox})
            threading.Thread(target=turn if action == 'prompt' else compact,
                             args=(sid, body['text'] if action == 'prompt' else mid), daemon=True).start()
        elif action == 'interrupt':
            CANCELLED.set()
            self.send_json(200, {'interrupted': sid in ACTIVE})
        elif action == 'permission' and pieces[-1] == 'reply':
            answers = ANSWERS.pop(pieces[4], None)
            if answers:
                answers.put(body['decision'])
            self.send_empty()
        elif action in ('agent', 'model'):
            data[action] = body.get(action, body)
            with LOCK:
                save(sid, data)
            self.send_empty()
        else:
            self.send_json(404, {'message': 'unknown API'})

    def send_empty(self):
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    def stream_events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        index = len(EVENTS)
        try:
            self.wfile.write(b'data: {"id":"evt_connected","type":"server.connected","data":{}}\n\n')
            self.wfile.flush()
            while True:
                with LOCK:
                    pending = EVENTS[index:]
                for item in pending:
                    self.wfile.write(('data: ' + json.dumps(item) + '\n\n').encode())
                    index += 1
                self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()
                time.sleep(.01)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    record({'argv': sys.argv[1:], 'env': sorted(os.environ), 'cwd': os.getcwd(), 'config': CONFIG})
    if os.environ.get('FAKE_OPENCODE_EXIT'):
        print('no provider', file=sys.stderr, flush=True)
        sys.exit(2)
    if sys.argv[1] != 'serve':
        print('V2 requires serve', file=sys.stderr, flush=True)
        sys.exit(2)
    port = int(sys.argv[sys.argv.index('--port') + 1])
    http.server.ThreadingHTTPServer(('127.0.0.1', port), NativeHttp).serve_forever()


if __name__ == '__main__':
    main()
