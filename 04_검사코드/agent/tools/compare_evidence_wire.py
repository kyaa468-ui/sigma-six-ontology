"""Offline logical-I/O comparison; no model, network, or operational orders.

Uses the same synthetic sources in both formats. Byte counts exclude provider
prompts, MCP framing, hidden subagent traffic and tokenization; they are not
token/cost/account-limit estimates. Saved public responses permit human review.
"""
from __future__ import annotations

import argparse
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
import pytest
import common as C
import evidence_wire as W
import record_store
import storage_objects
import mcp_server as M
import ontology
import data_prep
import judgment_coverage as JC
import learning_proposals as LP
from prompt_validation import FrozenTools
from test_ontology import isolated, row, brief, FakeBroker
from test_learning_proposals import source as research_source, item as proposal_item


def encoded(value):
    return value.encode('utf-8') if isinstance(value, str) else b''.join(storage_objects.chunks(value))


def plain(value):
    return json.loads(b''.join(storage_objects.chunks(value)))


class Capture:
    def __init__(self): self.calls = []
    def call(self, name, **arguments):
        result = getattr(M, name)(**arguments)
        self.calls.append({'tool': name, 'arguments': plain(arguments), 'result': plain(result)})
        return result


def judgment(text, key='correct'):
    if text.startswith('# 판단 자료\n'):
        payload = json.loads(text.split('\n', 1)[1])
        evidence = next(r for r in W.public_rows(payload) if r['field'] == 'close')
        declaration = {'wire_format': W.FORMAT, 'bundle_refs': [payload['bundle_alias']]}
        ref = evidence['ref']
    else:
        did = text.split('브리핑 전달 ID: ')[-1].splitlines()[0]
        receipt = record_store.load(C.ontology_root() / 'deliveries' / (did + '.json'))
        evidence = next(r for r in receipt['evidence_catalog'] if r['field'] == 'close')
        declaration = {'delivery_ids': [did]}
        ref = {'delivery_id': did, 'evidence_id': evidence['evidence_id']}
    return {'request_key': key, 'code': evidence['subject'], 'recommended_action': '관망',
        'confidence_for': '매수', 'confidence_raw': .5, 'decision': '동일 원본의 종가를 인용하는 검증용 판단',
        **declaration, 'arguments': [{'id': 'close', 'kind': 'fact_claim', 'proposer': 'lead',
            'stance': 'neutral', 'adoption': 'reference', 'evidence': ref,
            'measurement_kind': evidence['kind'], 'assertion': {'op': 'value', 'value': evidence['value'], 'unit': evidence['unit']}}]}


def run_case(name, wire, directory):
    capture = Capture()
    with pytest.MonkeyPatch.context() as mp:
        fixture = isolated.__wrapped__(directory, mp)
        next(fixture)
        try:
            mp.setenv(W.ENV, wire)
            mp.setenv('RUN_ID', 'benchmark-' + name)
            mp.setenv('RUN_ATTEMPT_ID', 'benchmark-' + name)
            mp.setenv('RUN_MODE', 'decide')
            mp.setattr(data_prep, 'live_market', lambda *_: None)
            broker = FakeBroker()
            mp.setattr(broker, 'account', lambda: {'total': 1000000, 'cash': 800000, 'stock_value': 200000,
                'holdings': [{'code': '005930', 'name': '모의 보유', 'qty': 10, 'avg_price': 19000,
                              'price': 20000, 'value': 200000, 'pnl_pct': .0526316}]}, raising=False)
            mp.setattr(M, '_broker', broker)
            if name in ('regular', 'holdings', 'empty', 'frozen'):
                b = brief([row()] if name in ('regular', 'frozen') else [])
                b['holdings'] = [row(1)] if name in ('regular', 'holdings') else []
                for r in b['candidates'] + b['holdings']:
                    r['news'] = [{'title': '검증용 뉴스: 수급 방향은 엇갈리고 전망은 미확인', 'date': '11-05', 'risk': False}]
                C.write_json(C.BRIEFING, b)
                attempt = JC.start_attempt('benchmark-' + name, 'decide', b, broker.account())
                mp.setenv('RUN_ATTEMPT_ID', attempt['run_attempt_id'])
                text = capture.call('get_briefing')
                capture.call('get_account')
                if name != 'empty':
                    good = judgment(text)
                    bad = copy.deepcopy(good); bad['request_key'] = 'wrong'
                    bad['arguments'][0]['assertion']['value'] += 100
                    assert capture.call('record_ai_judgments', items=[bad])[0]['status'] == 'rejected'
                    assert capture.call('record_ai_judgments', items=[good])[0]['status'] == 'accepted'
                    assert capture.call('record_ai_judgments', items=[good])[0]['idempotent'] is True
                    capture.call('get_judgment_coverage')
                    if name == 'frozen':
                        receipts = [record_store.load(p) for p in (C.ontology_root() / 'deliveries').glob('*.json')]
                        receipts = [W.freeze_receipt(r, C.now()) for r in receipts]
                        target = {'code': good['code'], 'confidence_for': '매수', 'run_id': 'benchmark-frozen', 'run_attempt_id': attempt['run_attempt_id']}
                        frozen = FrozenTools({'receipts': receipts, 'target': target, 'as_of': C.now().isoformat()})
                        for tool, args in [('get_briefing', {}), ('record_ai_judgments', {'items': [good]})]:
                            result = frozen.call(tool, args)
                            capture.calls.append({'tool': 'frozen.' + tool, 'arguments': plain(args), 'result': plain(result)})
                        assert result[0]['status'] == 'accepted', result
            elif name == 'external':
                material = {'request_key': 'news', 'subject': '005930', 'title': '미검증 기사',
                            'url': 'https://fixture.invalid/news', 'text': '기관은 순매도했으며 외국인은 순매수했다. 향후 주가 영향은 미확인이다.'}
                first = capture.call('record_external_evidence', items=[material])[0]
                second = capture.call('record_external_evidence', items=[material])[0]
                assert first['status'] == second['status'] == 'recorded_unverified'
                assert second['idempotent'] is True
                if wire == W.FORMAT:
                    capture.call('get_evidence_detail', ref=first['evidence_ref'], fields=['source_url'])
            else:
                mp.setenv(W.ENV, 'legacy')
                mp.setenv('RUN_ATTEMPT_ID', 'fixture-setup')
                run, old_context, old_page = research_source(mp)
                mp.setenv(W.ENV, wire)
                mp.setenv('RUN_ID', 'benchmark-' + name)
                if name == 'review':
                    mp.setenv('RUN_MODE', 'case_review'); mp.setenv('CASE_REVIEW_ID', 'research-one')
                    mp.setenv('CASE_REVIEW_ATTEMPT_ID', 'benchmark-review')
                    context = capture.call('get_case_review', review_id='research-one')
                    page = capture.call('get_review_cases', review_id='research-one')
                    document = W.public_document(context)
                    metric = next(m for m in document['metric_catalog'] if m['path'] == '/comparison/summary/refutation_rate')
                    declaration = {'wire_format': W.FORMAT, 'bundle_refs': [context['bundle_alias'], page['bundle_alias']]} if wire == W.FORMAT else {}
                    fact = {'id': 'rate', 'metric_id': metric['metric_id'], 'op': 'value', 'value': .5, 'unit': 'ratio'}
                    result = capture.call('record_case_review', review_id='research-one', request_key='review',
                        findings=[fact], hypotheses=[{'id': 'h', 'text': '사후 해석, 미검증', 'finding_ids': ['rate'], 'case_ids': ['a']}], **declaration)
                    assert result['status'] == 'accepted', result
                else:
                    rid = run['proposal_run_id']
                    mp.setenv('RUN_MODE', 'proposal_review'); mp.setenv('PROPOSAL_RUN_ID', rid)
                    mp.setenv('PROPOSAL_ATTEMPT_ID', 'benchmark-proposal')
                    context = capture.call('get_proposal_context', proposal_run_id=rid)
                    page = capture.call('get_proposal_cases', proposal_run_id=rid)
                    value = proposal_item(old_context, old_page)
                    if wire == W.FORMAT:
                        value.pop('delivery_ids'); value.update(wire_format=W.FORMAT, bundle_refs=[context['bundle_alias'], page['bundle_alias']])
                        value['fact_claims'][0]['reference'] = W.public_rows(context)[0]['ref']
                    else:
                        value['delivery_ids'] = [context['delivery_id'], page['delivery_id']]
                    result = capture.call('record_learning_proposals', proposal_run_id=rid, request_key='proposal', items=[value])
                    assert result['status'] == 'accepted', result
                    capture.call('get_learning_proposals', proposal_run_id=rid)
            incoming = sum(len(encoded(c['arguments'])) for c in capture.calls)
            outgoing = sum(len(encoded(c['result'])) for c in capture.calls)
            return {'scenario': name, 'wire_format': wire, 'calls': capture.calls,
                    'call_count': len(capture.calls), 'tool_arguments_utf8_bytes': incoming,
                    'tool_responses_utf8_bytes': outgoing, 'total_utf8_bytes': incoming + outgoing}
        finally:
            try: next(fixture)
            except StopIteration: pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results, log = [], io.StringIO()
    with tempfile.TemporaryDirectory(prefix='sion-wire-comparison-') as temporary, contextlib.redirect_stdout(log):
        for name in ('regular', 'holdings', 'empty', 'external', 'review', 'proposal', 'frozen'):
            pair = {}
            for label, wire in [('legacy', 'legacy'), ('compact', W.FORMAT)]:
                work = Path(temporary) / name / label; work.mkdir(parents=True)
                result = run_case(name, wire, work)
                (args.output / (name + '-' + label + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                pair[label] = {k: v for k, v in result.items() if k != 'calls'}
            pair['scenario'] = name
            pair['reduction_percent'] = round(100 * (1 - pair['compact']['total_utf8_bytes'] / pair['legacy']['total_utf8_bytes']), 2)
            results.append(pair)
    total = {label: sum(r[label]['total_utf8_bytes'] for r in results) for label in ('legacy', 'compact')}
    summary = {'status': 'passed', 'measurement': 'logical_tool_io_utf8_bytes_excluding_MCP_envelope_and_model_prompts',
               'actual_model_tokens': None, 'actual_model_cost': None, 'account_limit_usage': None,
               'model_calls': 0, 'network_calls': 0, 'operational_orders': 0,
               'results': results, 'total_utf8_bytes': total,
               'total_reduction_percent': round(100 * (1 - total['compact'] / total['legacy']), 2)}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'fixture-log.txt').write_text(log.getvalue())
    print(json.dumps({k: v for k, v in summary.items() if k != 'results'}, ensure_ascii=False))


if __name__ == '__main__': main()
