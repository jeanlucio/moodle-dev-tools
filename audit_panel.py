"""moodle-panel: a local web page to look at the plugins and to start audits.

    moodle-panel [--port 8765]

The server listens on 127.0.0.1 only: reach it from another machine through a port forward (the
VS Code Ports tab, or `ssh -L 8765:127.0.0.1:8765 host`). Three things keep a web page from
driving it by accident: the Host header must be this machine's own name (a DNS-rebinding page sends
another), every action needs the token that only the served page carries (a page from another
site cannot read it), and an action must be JSON (a plain form post cannot send that).
"""

import argparse
import json
import mimetypes
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import panel_data
import panel_runs

STATIC_DIR = panel_runs.TOOLS_DIR / 'panel'
PROGRESS_DIR = panel_runs.TOOLS_DIR / '.progress'
TOKEN_FILE = panel_runs.PANEL_DIR / 'token'
DEFAULT_PORT = 8765
ALLOWED_HOSTS = ('127.0.0.1', 'localhost')
TICK_SECONDS = 2
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')
LOG_CHUNK = 65536
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def load_token():
    """The panel token, created on first use and readable only by the owner."""
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text(encoding='utf-8').strip()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + '\n', encoding='utf-8')
    TOKEN_FILE.chmod(0o600)
    return token


class Handler(BaseHTTPRequestHandler):
    server_version = 'moodle-panel'
    token = ''
    port = DEFAULT_PORT
    public = None  # tests point this at a throwaway Moodle tree
    runs_dir = None

    # ---- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, content_type='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._send(status, {'error': message})

    def _host_ok(self):
        """Only this machine's own names. The port is not checked: a port forward may hand the
        browser another local port than the one the server listens on, while a DNS-rebinding page
        always arrives under a hostname that is not ours."""
        host = self.headers.get('Host', '')
        name = host.rsplit(':', 1)[0] if ':' in host else host
        return name in ALLOWED_HOSTS

    def _json_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 20000:
            raise panel_runs.ValidationError('corpo grande demais')
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise panel_runs.ValidationError('JSON inválido')

    def _plugin_or_404(self, path):
        plugin = panel_data.find_plugin(path or '', self.public)
        if plugin is None:
            self._error(404, 'plugin desconhecido')
        return plugin

    # ---- GET --------------------------------------------------------------- #

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, 'host não permitido')
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path
        try:
            if route == '/':
                return self._index()
            if route in ('/panel.js', '/panel.css'):
                return self._static(route.lstrip('/'))
            if route == '/api/plugins':
                return self._send(200, self._plugins())
            if route == '/api/plugin':
                return self._plugin_detail(query.get('path'))
            if route == '/api/estimate':
                return self._estimate(query)
            if route == '/api/static':
                return self._static_checks(query.get('plugin'))
            if route == '/api/static-all':
                return self._static_all()
            if route == '/api/runs':
                return self._send(200, [self._public_run(r)
                                        for r in panel_runs.list_runs(40, self.runs_dir)])
            match = re.fullmatch(r'/api/runs/([\w\-]+)(/log)?', route)
            if match:
                return self._run(match.group(1), bool(match.group(2)), query)
            if route == '/api/progress':
                return self._progress(query.get('component'))
            if route == '/api/report':
                return self._report(query.get('plugin'), query.get('file'))
        except panel_runs.ValidationError as exc:
            return self._error(400, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._error(404, 'não encontrado')

    def _index(self):
        page = (STATIC_DIR / 'index.html').read_text(encoding='utf-8')
        self._send(200, page.replace('__TOKEN__', self.token), 'text/html; charset=utf-8')

    def _static(self, name):
        path = STATIC_DIR / name
        kind = mimetypes.guess_type(name)[0] or 'text/plain'
        self._send(200, path.read_bytes(), f'{kind}; charset=utf-8')

    def _plugins(self):
        out = []
        for plugin in panel_data.discover_plugins(self.public):
            directory = panel_data.plugin_dir(plugin, self.public)
            out.append({**plugin, 'last_audit': panel_data.last_audit(directory)})
        return out

    def _plugin_detail(self, path):
        plugin = self._plugin_or_404(path)
        if plugin is None:
            return
        directory = panel_data.plugin_dir(plugin, self.public)
        self._send(200, {**plugin, 'groups': panel_data.groups_info(directory),
                         'tags': panel_data.tags(directory),
                         'reports': panel_data.reports(directory)})

    def _estimate(self, query):
        options = panel_runs.validate({
            'plugin': query.get('plugin', ''), 'focus': query.get('focus', 'all'),
            'only': [g for g in query.get('only', '').split(',') if g],
            'since': query.get('since') or None, 'full': query.get('full') == '1',
            'with_tests': query.get('with_tests') == '1'}, self.public)
        directory = panel_data.plugin_dir(panel_data.find_plugin(options['plugin'], self.public),
                                          self.public)
        self._send(200, panel_data.estimate(directory, options))

    def _static_checks(self, path):
        import static_checks
        import static_schema
        plugin = self._plugin_or_404(path)
        if plugin is None:
            return
        directory = panel_data.plugin_dir(plugin, self.public)
        results = static_checks.run(directory, list(static_schema.CHECKS))
        self._send(200, {'plugin': plugin['path'], 'results': {
            name: [{**v, 'text': static_schema.describe(name, v)} for v in violations]
            for name, violations in results.items()}})

    def _static_all(self):
        import static_checks
        import static_schema
        summary = {}
        for plugin in panel_data.discover_plugins(self.public):
            directory = panel_data.plugin_dir(plugin, self.public)
            try:
                results = static_checks.run(directory, list(static_schema.CHECKS))
            except ValueError as exc:
                summary[plugin['path']] = {'error': str(exc)}
                continue
            summary[plugin['path']] = {n: len(v) for n, v in results.items() if v}
        self._send(200, summary)

    def _public_run(self, run):
        return {k: run.get(k) for k in ('id', 'status', 'options', 'created', 'started',
                                        'finished', 'exit_code', 'report')}

    def _run(self, run_id, want_log, query):
        run = panel_runs.read_run(run_id, self.runs_dir)
        if run is None:
            return self._error(404, 'execução inexistente')
        if not want_log:
            return self._send(200, self._public_run(run))
        text, offset = panel_runs.read_log(run, int(query.get('offset') or 0), LOG_CHUNK)
        self._send(200, {'text': text, 'offset': offset, 'status': run['status']})

    def _progress(self, component):
        if not component or not SAFE_NAME_RE.match(component):
            return self._error(400, 'componente inválido')
        path = PROGRESS_DIR / f'{component}.json'
        if not path.is_file():
            return self._send(200, {})
        self._send(200, path.read_text(encoding='utf-8'))

    def _report(self, path, name):
        plugin = self._plugin_or_404(path)
        if plugin is None:
            return
        text = panel_data.read_report(panel_data.plugin_dir(plugin, self.public), name or '')
        if text is None:
            return self._error(404, 'relatório inexistente')
        self._send(200, text, 'text/markdown; charset=utf-8')

    # ---- POST -------------------------------------------------------------- #

    def do_POST(self):
        if not self._host_ok():
            return self._error(403, 'host não permitido')
        if self.headers.get('X-Panel-Token', '') != self.token:
            return self._error(403, 'token ausente ou inválido')
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self._error(415, 'use application/json')
        route = urlparse(self.path).path
        try:
            if route == '/api/runs':
                options = panel_runs.validate(self._json_body(), self.public)
                run = panel_runs.enqueue(options, self.runs_dir)
                return self._send(201, self._public_run(run))
            match = re.fullmatch(r'/api/runs/([\w\-]+)/cancel', route)
            if match:
                run = panel_runs.cancel(match.group(1), self.runs_dir)
                if run is None:
                    return self._error(404, 'execução inexistente')
                return self._send(200, self._public_run(run))
        except panel_runs.ValidationError as exc:
            return self._error(400, str(exc))
        return self._error(404, 'não encontrado')


def make_server(port=DEFAULT_PORT, token=None, public=None, runs_dir=None):
    """A server bound to 127.0.0.1 only; the handler class carries the settings of this server."""
    handler = type('PanelHandler', (Handler,), {
        'token': token or load_token(), 'port': port, 'public': public, 'runs_dir': runs_dir})
    server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    handler.port = server.server_address[1]  # the real port when 0 asked for any free one
    return server


def scheduler(runs_dir, stop):
    """Start the next queued run whenever nothing is running."""
    while not stop.wait(TICK_SECONDS):
        try:
            panel_runs.tick(runs_dir)
        except Exception as exc:  # the loop must outlive one bad tick
            print(f'aviso: agendador: {exc}', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='moodle-panel', description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port)
    except OSError as exc:
        print(f'erro: não consegui abrir 127.0.0.1:{args.port} ({exc})', file=sys.stderr)
        return 1
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(None, stop), daemon=True).start()
    print(f'Painel em http://localhost:{args.port}  (só 127.0.0.1)')
    print('De outra máquina: encaminhe a porta (aba Portas do VS Code, ou ssh -L).')
    print('Ctrl+C para parar. Execuções em andamento continuam depois que o painel para.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
