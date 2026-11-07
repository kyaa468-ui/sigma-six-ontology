"""Bounded reader for workbench local-ontology-ir v1. No eval, network or runtime design updates."""
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'config' / 'ontology'
BINARY = {'gt','gte','lt','lte','eq','ne','add','sub','mul','div','and','or'}
OPS = BINARY | {'literal','property','if','function'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_bindings(ir, bindings):
    if ir.get('format') != 'local-ontology-ir' or ir.get('formatVersion') != 1:
        raise ValueError('Unsupported ontology export')
    model = ir['ontology']
    nodes = {n['id']: n for n in model['nodes']}
    funcs = {f['id']: f for f in model['functions']}
    if len(funcs) != len(model['functions']) or len(nodes) != len(model['nodes']):
        raise ValueError('Duplicate definition ID')
    def visit(expr, node, stack, depth=0):
        if depth > 24 or not isinstance(expr, dict) or expr.get('op') not in OPS:
            raise ValueError('Unsupported expression')
        op = expr['op']
        if op == 'literal':
            v = expr['value']
            if not (v is None or type(v) in (int,float,bool,str)) or isinstance(v,float) and not math.isfinite(v):
                raise ValueError('Invalid literal')
        elif op == 'property':
            if expr['property'] not in {p['id'] for p in nodes[node]['properties']}:
                raise ValueError('Unknown property')
        elif op == 'function':
            fid = expr['function']
            if fid in stack or fid not in funcs or funcs[fid]['node'] != node:
                raise ValueError('Invalid function reference')
            visit(funcs[fid]['expression'], node, stack + [fid], depth+1)
        else:
            for key in (('condition','then','else') if op == 'if' else ('left','right')):
                visit(expr[key], node, stack, depth+1)
    for fid, fn in funcs.items():
        if fn['node'] not in nodes:
            raise ValueError('Unknown function node')
        visit(fn['expression'], fn['node'], [fid])
    seen, versions = set(), set()
    for item in bindings['criteria']:
        cid, vid = item['criterion_id'], item['criterion_version_id']
        if cid in seen or vid in versions or item['function_id'] not in funcs:
            raise ValueError('Invalid criterion binding')
        seen.add(cid); versions.add(vid)
    rule_ids = set()
    for rule in bindings['rules']:
        if rule['rule_version_id'] in rule_ids or not set(rule['criteria']) <= seen:
            raise ValueError('Invalid rule binding')
        rule_ids.add(rule['rule_version_id'])
    return funcs


def load_definition_bundle(directory=None):
    root = Path(directory or ROOT)
    ir = json.loads((root/'sigma-six.ir.json').read_text(encoding='utf-8'))
    bindings = json.loads((root/'bindings.json').read_text(encoding='utf-8'))
    validate_bindings(ir, bindings)
    value = {'ir': ir, 'bindings': bindings}
    return {**value, 'hash': digest(value)}


def evaluate(bundle, criterion, values, reads=None):
    funcs = {f['id']: f for f in bundle['ir']['ontology']['functions']}
    item = next(c for c in bundle['bindings']['criteria'] if c['criterion_id'] == criterion)
    def run(e):
        op=e['op']
        if op == 'literal': return e['value']
        if op == 'property':
            v=values.get(e['property'])
            if reads is not None:
                reads[e['property']] = v
            if v is None or isinstance(v,float) and not math.isfinite(v):
                raise ValueError('Missing input: '+e['property'])
            return v
        if op == 'function': return run(funcs[e['function']]['expression'])
        if op == 'if': return run(e['then'] if run(e['condition']) else e['else'])
        a=run(e['left'])
        if op == 'and': return bool(a and run(e['right']))
        if op == 'or': return bool(a or run(e['right']))
        b=run(e['right'])
        if op == 'div' and b == 0: raise ValueError('Zero denominator')
        return {'gt':lambda:a>b,'gte':lambda:a>=b,'lt':lambda:a<b,'lte':lambda:a<=b,
                'eq':lambda:a==b,'ne':lambda:a!=b,'add':lambda:a+b,'sub':lambda:a-b,
                'mul':lambda:a*b,'div':lambda:a/b}[op]()
    return run(funcs[item['function_id']]['expression'])
