"""Only frozen, pre-authorized experiment inputs. No production MCP imports."""
import json
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from prompt_validation import FrozenTools


def main():
    path=Path(sys.argv[1])
    request=json.loads(path.read_text(encoding='utf-8'))
    def sink(event):
        with (path.parent/'calls.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n');out.flush()
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
    server.run()

if __name__=='__main__':main()
