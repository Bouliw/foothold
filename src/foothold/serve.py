"""The local web page: research a company, read the briefs, export them. Bound to 127.0.0.1 only; every request
must carry this machine's host name, and every action the token printed into the page."""
import hmac
import html
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from . import cli, fetch, llm, render, sources

MAX_BODY = 10_000


def make_handler(offer, cfg, out_dir, port, token):
    page = files('foothold').joinpath('web.html').read_text(encoding='utf-8')
    busy = threading.Lock()  # one research at a time: the sources are rate limited anyway
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'foothold'

        def log_message(self, format, *args):
            pass

        def _send(self, status, body, ctype='application/json', extra=()):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            for name, value in (('Content-Type', ctype), ('Content-Length', str(len(data))), ('Cache-Control', 'no-store'),
                                ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'), *extra):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _local(self):
            # A page on another site that points its own name at 127.0.0.1 still sends its own Host
            if self.headers.get('Host') in hosts:
                return True
            self._send(403, {'error': 'unknown host'})
            return False

        def _authorised(self):
            if hmac.compare_digest(self.headers.get('X-Foothold-Token', ''), token):
                return True
            self._send(403, {'error': 'missing or wrong token: reload the page'})
            return False

        def do_GET(self):
            if not self._local():
                return
            if self.path == '/':
                nonce = secrets.token_urlsafe(16)
                body = page.replace('__TOKEN__', token).replace('__NONCE__', nonce).replace(
                    '__OFFER__', html.escape(offer['product']['name']))
                policy = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                          f"connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self._send(200, body.encode(), 'text/html; charset=utf-8', [('Content-Security-Policy', policy)])
            elif self.path == '/api/briefs':
                if not self._authorised():
                    return
                briefs = []
                for path in sorted(out_dir.glob('*.json')) if out_dir.is_dir() else []:
                    try:
                        brief = json.loads(path.read_text(encoding='utf-8'))
                        briefs.append(_bundle(brief))
                    except (ValueError, KeyError, TypeError, OSError):
                        continue
                self._send(200, {'briefs': briefs})
            else:
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            if not self._local() or not self._authorised():
                return
            if self.path != '/api/research':
                return self._send(404, {'error': 'not found'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= MAX_BODY:
                    raise ValueError
                company = str(json.loads(self.rfile.read(length))['company']).strip()
                if not company or len(company) > 200:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._send(400, {'error': 'send {"company": "a name or a domain"}'})
            with busy:
                try:
                    brief = cli.research(cli.parse_company(company), offer, cfg)
                    cli.save(brief, out_dir)
                except (sources.Unresolved, fetch.FetchError, llm.LLMError) as e:
                    return self._send(502, {'error': str(e)})
            self._send(200, _bundle(brief))

    return Handler


def _bundle(brief):
    return {'brief': brief, 'markdown': render.to_markdown(brief),
            'csv': {crm: render.to_csv([brief], crm) for crm in render.CRM_COLUMNS}}


def run(offer, cfg, out_dir, port, open_browser=True):
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(offer, cfg, out_dir, port, token))
    url = f'http://127.0.0.1:{port}/'
    print(f'foothold is running at {url} (Ctrl+C to stop)')
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
