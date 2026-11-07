"""Offline local-server and public-writer boundaries. No market, AI or publishing."""
import copy
import gzip
import hashlib
import http.client
import io
from types import SimpleNamespace
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import logger
import trace_view as V

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def snapshot(monkeypatch):
    value = {'status':'complete','view_id':'view-fixed','manifest':{'created_at':'2026-11-07T10:00:00+09:00'},
             'projection':{'schema_version':'TRACE_VIEW@v1','targets':[],'traces':{},'cases':[],
                           'reviews':[],'proposals':[],'validations':[],'policies':[],
                           'source_manifest':[],'errors':[],'protected_count':0}}
    monkeypatch.setattr(V.trace_snapshot, 'load_view_snapshot', lambda identifier: copy.deepcopy(value))
    return value


@pytest.fixture
def server(snapshot, monkeypatch):
    # Exercise the actual HTTP handler/parser without requiring a network port.
    # The root integration check separately opens this server in a real browser.
    def transport(address, handler):
        return SimpleNamespace(server_address=address, server_port=8765,
                               RequestHandlerClass=handler)
    monkeypatch.setattr(V, 'ThreadingHTTPServer', transport)
    return V.create_server('view-fixed', port=8765)


def request(server, path, method='GET', headers=None):
    class Connection:
        def __init__(self, payload): self.incoming=io.BytesIO(payload); self.output=bytearray()
        def makefile(self, *_): return self.incoming
        def sendall(self, payload): self.output.extend(payload)
    fields={'Host':f'127.0.0.1:{server.server_port}', **(headers or {})}
    payload=f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{key}: {value}\r\n' for key,value in fields.items()) + '\r\n'
    connection=Connection(payload.encode())
    server.RequestHandlerClass(connection, ('127.0.0.1',12345), server)
    head,body=bytes(connection.output).split(b'\r\n\r\n',1)
    lines=head.decode().split('\r\n')
    return int(lines[0].split()[1]), dict(line.split(': ',1) for line in lines[1:]), body


def test_loopback_pinned_view_and_offline_assets(server, snapshot):
    assert server.server_address[0] == '127.0.0.1'
    before = copy.deepcopy(snapshot)
    for path, mime in [('/', 'text/html'),('/trace.js','text/javascript'),('/trace.css','text/css'),('/api/view','application/json')]:
        status, headers, body = request(server, path)
        assert status == 200
        assert mime in headers['Content-Type']
        assert headers['Cache-Control'] == 'no-store'
        assert headers['X-Frame-Options'] == 'DENY'
        assert 'Access-Control-Allow-Origin' not in headers
        assert body
    assert snapshot == before
    code = (ROOT/'review/trace.js').read_text()
    assert "fetch('/api/view'" in code
    assert 'innerHTML' not in code
    html = (ROOT/'review/index.html').read_text()
    assert 'http://' not in html and 'https://' not in html


@pytest.mark.parametrize('encoding,compressed', [('gzip', True), ('gzip;q=0.5', True),
                                               ('gzip;q=0', False), ('identity', False)])
def test_large_view_transport_preserves_exact_logical_payload(server, snapshot, encoding, compressed):
    snapshot['projection']['test_repeated_observations'] = [{'source': 'synthetic', 'value': 20400}] * 200
    before = copy.deepcopy(snapshot)
    status, headers, body = request(server, '/api/view', headers={'Accept-Encoding': encoding})
    assert status == 200
    assert (headers.get('Content-Encoding') == 'gzip') is compressed
    decoded = gzip.decompress(body) if compressed else body
    assert json.loads(decoded) == before
    if compressed:
        assert len(body) < len(decoded) / 4
    assert snapshot == before


@pytest.mark.parametrize('path', ['/../config/params.json','/%2e%2e/config/params.json','/api/view?view_id=other','/api/trace/private','/assets/','/config/params.json'])
def test_no_arbitrary_files_or_other_views(server, path):
    assert request(server, path)[0] == 404


@pytest.mark.parametrize('headers', [{'Host':'attacker.invalid'},{'Origin':'https://attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}])
def test_cross_origin_and_rebinding_rejected(server, headers):
    assert request(server, '/api/view', headers=headers)[0] == 403


@pytest.mark.parametrize('method', ['POST','PUT','PATCH','DELETE','OPTIONS'])
def test_no_mutation_endpoints(server, method):
    status, _, body = request(server, '/api/view', method=method)
    assert status == 405 and json.loads(body)['status'] == 'read_only'


def test_corruption_after_start_never_serves_previous_payload(server, monkeypatch):
    monkeypatch.setattr(V.trace_snapshot,'load_view_snapshot',lambda _: {'status':'recording_error','view_id':'view-fixed'})
    status, _, body = request(server, '/api/view')
    assert status == 409 and 'projection' not in json.loads(body)


def test_cannot_start_unavailable_snapshot(monkeypatch):
    monkeypatch.setattr(V.trace_snapshot,'load_view_snapshot',lambda _: {'status':'protected'})
    with pytest.raises(ValueError, match='VIEW_NOT_AVAILABLE'):
        V.create_server('view-missing',0)


def test_javascript_parses_and_brand_matches_existing_dashboard():
    for path in [ROOT/'review/trace.js',ROOT.parent/'docs/trace-summary.js']:
        result = subprocess.run(['node','--check',str(path)],capture_output=True,text=True)
        assert result.returncode == 0, result.stderr
    css = (ROOT/'review/trace.css').read_text()
    for token in ['#da291c','#ff5a4a','#4aa3d4','AppleMyungjo','Batang','rgba(218,41,28,.26)']:
        assert token in css


def private_row(**extra):
    return {'time':'2026-11-07 10:00:00','code':'000001','name':'시험종목','side':'매수','qty':40,'price':20400,
            'amount':816000,'confidence':0.0,'bull':'PRIVATE_SENTINEL','bear':'PRIVATE_SENTINEL',
            'decision':'PRIVATE_SENTINEL','order_no':'PRIVATE_ORDER_NUMBER','judgment_id':'judgment-one',
            'account':{'secret':'PRIVATE_SENTINEL'},'source_path':'/PRIVATE_SENTINEL','raw':{'text':'PRIVATE_SENTINEL'}, **extra}


def test_final_public_writer_strips_prose_private_ids_and_keeps_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'DOCS_DATA', str(tmp_path))
    row=private_row()
    logger.out('trades',[row])
    value=json.loads((tmp_path/'trades.json').read_text())[0]
    assert (value['confidence'],value['qty'],value['price'])==(0.0,40,20400)
    encoded=json.dumps(value)
    assert 'PRIVATE_' not in encoded and 'order_no' not in value and 'judgment_id' not in value
    expected='public-ref-'+hashlib.sha256(b'judgment_id:judgment-one').hexdigest()
    assert value['origin_refs']==[{'kind':'judgment_id','ref':expected}]
    assert row['bull']=='PRIVATE_SENTINEL'


@pytest.mark.parametrize('name,value',[
    ('decision',{'time':'2026-11-07','reason':'PRIVATE_SENTINEL','items':[private_row()]}),
    ('debates',[{'reason':'PRIVATE_SENTINEL','items':[private_row()]}]),
    ('risk',[{'detail':'PRIVATE_SENTINEL','rule':'PRIVATE_SENTINEL'}]),
    ('briefing',{'market':{'raw':'PRIVATE_SENTINEL'},'intraday':{'error':'PRIVATE_SENTINEL'},'candidates':[{'code':'000001','news':[{'title':'PRIVATE_SENTINEL'}]}]}),
    ('reviews',[{'suggestion':'PRIVATE_SENTINEL'}]),
    ('tuning',[{'summary':'PRIVATE_SENTINEL','applied':[{'reason':'PRIVATE_SENTINEL'}],'rejected':[{'rejected':'PRIVATE_SENTINEL'}]}]),
    ('status',{'note':'PRIVATE_SENTINEL','sync':{'ok':False,'error':'PRIVATE_SENTINEL'}}),
])
def test_legacy_public_routes_do_not_bypass_allowlist(name,value):
    assert 'PRIVATE_SENTINEL' not in json.dumps(logger.public_payload(name,value))


def test_public_migration_prepares_separate_bundle_and_leaves_source(tmp_path):
    source=tmp_path/'source';source.mkdir()
    raw=json.dumps([private_row()]);(source/'trades.json').write_text(raw)
    (source/'private.json').write_text('{"secret":"PRIVATE_SENTINEL"}')
    result=logger.sanitize_public_directory(source,tmp_path/'staging')
    assert result['published'] is False
    assert (source/'trades.json').read_text()==raw
    assert not (tmp_path/'staging/private.json').exists()
    assert 'PRIVATE_' not in (tmp_path/'staging/trades.json').read_text()
    with pytest.raises(ValueError):logger.sanitize_public_directory(source,source)
    with pytest.raises(ValueError):logger.sanitize_public_directory(source,tmp_path/'staging')
