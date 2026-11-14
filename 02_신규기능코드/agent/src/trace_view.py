"""Loopback-only, read-only presentation of a prepared step-8 snapshot.

Serving never builds a snapshot, records an AI delivery, repairs a ledger or
imports the trading MCP server. Research exposure is reserved by the separate
snapshot preparation command before any payload can be served here.
"""
from __future__ import annotations

import json
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

import trace_snapshot
import trace_pages

ASSETS = Path(__file__).resolve().parents[1] / 'review'
_ROUTES = {'/': ('index.html', 'text/html'), '/trace.js': ('trace.js', 'text/javascript'),
           '/trace.css': ('trace.css', 'text/css')}


def _accepts_gzip(header):
    for item in header.split(','):
        coding, *parameters = [part.strip().lower() for part in item.split(';')]
        if coding != 'gzip':
            continue
        quality = next((p.split('=', 1)[1].strip() for p in parameters if p.startswith('q=')), '1')
        try:
            return 0 < float(quality) <= 1
        except ValueError:
            return False
    return False


def create_server(view_id, port=8765):
    """Only a pinned immutable view can be served; callers can use port=0 in tests."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('INVALID_PORT')
    initial = trace_snapshot.load_view_snapshot(view_id)
    if not isinstance(initial.get('projection'), dict):
        raise ValueError('VIEW_NOT_AVAILABLE: ' + initial.get('status', 'unknown'))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # A GET does not create an access ledger or disclose URL contents.

        def send_payload(self, status, data, content_type='application/json'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False,separators=(',',':')).encode()
            # The stored logical view is unchanged. Compress only transport to
            # browsers that explicitly accept it; no extra copy is saved.
            compressed = content_type == 'application/json' and len(body) > 1024 and _accepts_gzip(self.headers.get('Accept-Encoding', ''))
            if compressed:
                body = gzip.compress(body, compresslevel=1, mtime=0)
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Vary', 'Accept-Encoding')
            if compressed:
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def safe_request(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed:
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + host for host in allowed}:
                return False
            return self.headers.get('Sec-Fetch-Site') not in {'cross-site'}

        def do_GET(self):
            if not self.safe_request():
                return self.send_payload(403, {'status': 'forbidden'})
            parsed = urlsplit(self.path)
            if parsed.fragment:
                return self.send_payload(404, {'status': 'not_found'})
            if parsed.path in _ROUTES:
                if parsed.query:return self.send_payload(404,{'status':'not_found'})
                filename, content_type = _ROUTES[parsed.path]
                try:
                    return self.send_payload(200, (ASSETS / filename).read_bytes(), content_type)
                except OSError:
                    return self.send_payload(503, {'status': 'asset_unavailable'})
            if parsed.path.startswith('/api/'):
                try:
                    query=parse_qs(parsed.query,strict_parsing=True)
                    allowed={'cursor','limit','search','field','offset'}
                    if set(query)-allowed or any(len(v)!=1 for v in query.values()):
                        return self.send_payload(404, {'status':'invalid_query'})
                    args={k:v[0] for k,v in query.items()}
                    value=trace_snapshot.load_view_snapshot(view_id)
                    if not isinstance(value.get('projection'),dict):return self.send_payload(409,value)
                    route=parsed.path[len('/api/'):].split('/')
                    if route==['view']:
                        if query:return self.send_payload(400,{'status':'invalid_query'})
                        result=trace_pages.summary(value)
                    elif len(route)==1 and route[0] in trace_pages.LISTS:
                        result=trace_pages.list_page(value,route[0],cursor=args.get('cursor'),
                            limit=int(args.get('limit',50)),search=args.get('search',''))
                    elif len(route)==2 and route[0] in ('trace','case','proposal','validation','policy'):
                        result=trace_pages.detail(value,route[0],unquote(route[1]),field=args.get('field'),
                            cursor=args.get('cursor'),limit=int(args.get('limit',50)))
                    elif len(route)==3 and route[0]=='chunk':
                        result=trace_pages.chunk(value,route[1],unquote(route[2]),args.get('field'),int(args.get('offset',0)))
                    elif len(route)==4 and route[0]=='relations':
                        result=trace_pages.relations(value,route[1],unquote(route[2]),route[3],cursor=args.get('cursor'),limit=int(args.get('limit',50)))
                    else:return self.send_payload(404,{'status':'not_found'})
                    return self.send_payload(404 if result.get('status')=='not_found' else 200,result)
                except (ValueError,TypeError):return self.send_payload(400,{'status':'invalid_request'})
                except Exception:return self.send_payload(503,{'status':'recording_error','view_id':view_id})
            return self.send_payload(404, {'status': 'not_found'})

        def do_POST(self):
            self.send_payload(405, {'status': 'read_only'})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def serve_view(view_id, port=8765):
    server = create_server(view_id, port)
    print(f'로컬 근거 조회: http://127.0.0.1:{server.server_port}/ · {view_id}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
