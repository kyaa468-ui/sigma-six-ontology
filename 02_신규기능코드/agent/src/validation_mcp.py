"""Only frozen, pre-authorized experiment inputs. No production MCP imports."""
import json
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from prompt_validation import FrozenTools
import record_store
import storage_objects
import common as C


def main():
    path=Path(sys.argv[1])
    # The explicit manifest path is supplied by the authorized experiment runner.
    C.WORK=str(record_store.base_for(path).parent)
    request=record_store.load(path)
    def sink(event):
        with (path.parent/'calls.jsonl').open('a',encoding='utf-8') as out:
            for part in storage_objects.chunks(event):out.write(part.decode('utf-8'))
            out.write('\n');out.flush()
    frozen=FrozenTools(request,sink)
    server=FastMCP('frozen')
    @server.tool()
    def get_briefing() -> dict:
        return frozen.call('get_briefing')
    @server.tool()
    def get_account() -> dict:
        return frozen.call('get_account')
    @server.tool()
    def record_ai_judgments(items:list[dict]) -> list[dict]:
        return frozen.call('record_ai_judgments',{'items':items})
    @server.tool()
    def get_evidence_detail(ref:str, fields:list[str]) -> dict:
        return frozen.call('get_evidence_detail', {'ref': ref, 'fields': fields})
    server.run()

if __name__=='__main__':main()
