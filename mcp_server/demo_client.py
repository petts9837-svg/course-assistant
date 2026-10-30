"""
demo_client.py：System 2 的 MCP Client 示範

用同一批 System 1 答錯的問題，改由 MCP Server（course-sql）回答，
展示：工具清單 → 呼叫工具 → 回傳「總門數」可核對 → 沒資料時回「查無資料」。

執行（在專案根目錄）：
    pip install fastmcp
    python mcp_server/demo_client.py
"""
import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client

SERVER = str(Path(__file__).resolve().parent / "course_sql_server.py")


def data(result):
    """相容不同 fastmcp 版本的回傳格式。"""
    d = getattr(result, "data", None)
    if isinstance(d, dict):
        return d
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def show(title, system1, args_tool, res, n=5):
    print(f"\n{'=' * 64}\n問題：{title}\nSystem 1 的問題：{system1}")
    tool, args = args_tool
    print(f"→ 呼叫 {tool}({json.dumps(args, ensure_ascii=False)})")
    if res.get("count") == 0 or res.get("found") is False:
        print("→ 回傳：查無資料")
        return
    if "count" in res:
        print(f"→ 符合總門數 = {res['count']}（顯示 {res.get('returned', '?')} 門）")
    rows = res.get("courses") or res.get("sections") or []
    for c in rows[:n]:
        print(f"   - [{c['id']}] {c['name']} | {c['teacher']} | {c['credit']:g} 學分 | {c['type']} | {c.get('dept')}")
    if len(rows) > n:
        print(f"   …（其餘 {len(rows) - n} 門省略）")
    for k in ("credits", "courses_with_assessment_list", "basis"):
        if k in res:
            print(f"   {k}: {res[k]}")


async def main():
    async with Client(SERVER) as client:
        tools = await client.list_tools()
        print("MCP Server 提供的工具：", ", ".join(t.name for t in tools))

        async def call(tool, **args):
            return tool, args, data(await client.call_tool(tool, args))

        cases = [
            ("3 學分以上的選修課有哪些？",
             "向量檢索只撈 top-5，漏掉大部分",
             "filter_courses", dict(credit_min=3, req_type="選修", limit=5)),
            ("1 學分的課有哪些？",
             "只列出幾門卻當成全部",
             "filter_courses", dict(credit_min=1, credit_max=1, limit=5)),
            ("星期五晚上有課的有哪些？",
             "無法判斷星期與晚間節次",
             "filter_courses", dict(weekday=5, evening=True, limit=5)),
            ("資工系有開「程式設計概論」嗎？",
             "System 1 傾向編造",
             "filter_courses", dict(dept="資工", name_contains="程式設計概論")),
            ("量子運算相關課程？",
             "沒資料應回「查無資料」",
             "filter_courses", dict(name_contains="量子")),
            ("「程式設計概論」幾學分？",
             "同名課 2/3 學分並存，只答一種",
             "list_sections", dict(name="程式設計概論")),
            ("沒有期末考的課？",
             "T4：4 門裡 3 門其實有期末考",
             "check_assessment", dict(keyword="期末考", mode="lacks", limit=5)),
        ]
        for title, s1, tool, args in cases:
            t, a, res = await call(tool, **args)
            show(title, s1, (t, a), res)

        # 追問：用上一輪的課號，而不是重新文字搜尋（解決 context loss）
        t, a, res = await call("filter_courses", name_contains="程式設計概論", limit=1)
        cid = res["courses"][0]["id"]
        t, a, detail = await call("get_course", course_id=cid)
        print(f"\n{'=' * 64}\n追問：「那它什麼時候上課、怎麼評分？」（用課號 {cid}，不重新搜尋）")
        print(f"→ 呼叫 get_course(course_id={cid})")
        print(f"   {detail['name']}｜{detail['teacher']}｜{detail['credit']:g} 學分")
        print("   時間：", [(s['weekday'], s['section']) for s in detail['schedules']])
        print("   評量：", [(x['item'], x['percent']) for x in detail['assessment']])


if __name__ == "__main__":
    asyncio.run(main())
