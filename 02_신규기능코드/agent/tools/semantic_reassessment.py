"""Trusted local historical inspection / correction. No model or order calls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import semantic_layer as S

parser=argparse.ArgumentParser()
group=parser.add_mutually_exclusive_group(required=True)
group.add_argument('--judgment-id',help='Read one historical chain as JSON')
group.add_argument('--invalidate',metavar='DEPENDENCY_ID',help='Invalidate a corrected dependency within an explicit chain')
parser.add_argument('--chain-id')
parser.add_argument('--reason')
args=parser.parse_args()
if args.judgment_id:
    result=S.stored_trace(args.judgment_id)
else:
    if not args.chain_id or not args.reason:
        parser.error('--invalidate requires --chain-id and --reason; local claim IDs are not globally unique')
    result=S.invalidate(args.invalidate,args.reason,chain_id=args.chain_id)
print(json.dumps(result,ensure_ascii=False,indent=2))
