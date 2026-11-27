"""
mcp_hub.py：System 2 的 MCP Client

啟動兩個 MCP Server（stdio 子程序）並保持連線，提供同步介面給 Streamlit 與 pipeline 使用：
    hub = MCPHub()
    hub.call("filter_courses", {"credit_min": 3})
    hub.read_resource("courses://catalog")

工具名稱會自動對應到提供它的 Server（course-sql 或 course-search）。
"""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from fastmcp import Client

import llm  # noqa: F401  載入 .env，讓 MCP Server 子程序也拿得到憑證

SERVERS_DIR = Path(__file__).resolve().parent / "mcp_servers"
SERVERS = {
    "course-sql": SERVERS_DIR / "course_sql_server.py",
    "course-search": SERVERS_DIR / "course_search_server.py",
    "fju-sync": SERVERS_DIR / "fju_sync_server.py",
}


def _data(result):
    """相容不同 fastmcp 版本的工具回傳格式，統一轉成 dict。"""
    d = getattr(result, "data", None)
    if isinstance(d, dict):
        return d
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


class MCPHub:
    def __init__(self, servers=None):
        self.servers = servers or SERVERS
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.clients = {}        # server 名稱 → Client
        self.tool_owner = {}     # 工具名稱 → server 名稱
        self.tools = {}          # 工具名稱 → 說明（給 Router 看）
        self._run(self._connect())

    def _run(self, coro, timeout=120):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self):
        from fastmcp.client.transports import PythonStdioTransport
        for name, path in self.servers.items():
            transport = PythonStdioTransport(script_path=str(path), python_cmd=sys.executable,
                                             env=dict(os.environ))
            client = Client(transport)
            await client.__aenter__()
            self.clients[name] = client
            for t in await client.list_tools():
                self.tool_owner[t.name] = name
                self.tools[t.name] = {"server": name, "description": (t.description or "").strip(),
                                      "schema": getattr(t, "input_schema", None) or t.inputSchema}

    def call(self, tool, args):
        """呼叫工具；工具不存在時回傳錯誤 dict（不拋例外，讓 pipeline 記錄在 trace 裡）。"""
        server = self.tool_owner.get(tool)
        if not server:
            return {"error": f"unknown tool {tool}"}

        async def _go():
            return _data(await self.clients[server].call_tool(tool, args))
        try:
            return self._run(_go())
        except Exception as e:  # 參數錯誤等
            return {"error": str(e)[:300]}

    def read_resource(self, uri, server="course-sql"):
        async def _go():
            res = await self.clients[server].read_resource(uri)
            return json.loads(res[0].text)
        return self._run(_go())

    def close(self):
        async def _close():
            for c in self.clients.values():
                await c.__aexit__(None, None, None)
        try:
            self._run(_close(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


if __name__ == "__main__":
    hub = MCPHub()
    for name, info in hub.tools.items():
        print(f"{info['server']:14s} {name}")
    print(hub.read_resource("courses://stats"))
    print(hub.call("filter_courses", {"credit_min": 3, "req_type": "選修", "limit": 2})["count"])
    print(hub.call("search_courses", {"query": "資料分析", "k": 3})["mode_used"])
    hub.close()
