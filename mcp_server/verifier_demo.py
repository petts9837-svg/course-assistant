"""
verifier_demo.py：用真實 MCP 工具的回傳，示範 Verifier 如何攔下 System 1 式的錯誤回答。

流程：Client 呼叫 MCP 工具 → 取得 evidence → 把 Answer agent 的「草稿 + 主張」交給 Verifier。
草稿是我們模擬的（重現 System 1 實際出現過的錯誤類型），用來證明 Verifier 攔得住。

執行：python3 mcp_server/verifier_demo.py
"""
import asyncio
from pathlib import Path

from fastmcp import Client

from demo_client import SERVER, data
from verifier import verify


async def main():
    async with Client(Path(SERVER)) as client:
        async def call(tool, **a):
            return data(await client.call_tool(tool, a))

        # 取一門真的有期末考的課，用來測「錯誤宣稱沒有期末考」
        has_final = (await call("check_assessment", keyword="期末考", mode="has", limit=1))["courses"][0]
        has_final_detail = await call("get_course", course_id=has_final["id"])

        ev_elective = await call("filter_courses", credit_min=3, req_type="選修", limit=5)
        ev_none = await call("filter_courses", dept="資工", name_contains="程式設計概論")
        ev_sections = await call("list_sections", name="程式設計概論")
        first = ev_elective["courses"][0]

        scenarios = [
            ("A. 正確回答（應通過）",
             f"{first['name']}（{first['credit']:g} 學分，{first['teacher']}）",
             [{"type": "course", "id": first["id"], "credit": first["credit"], "teacher": first["teacher"]}],
             [ev_elective]),
            ("B. 沒資料卻編造課程（System 1 的 hallucination）",
             "資工系有開「程式設計概論」，3 學分，由王大明老師授課。",
             [{"type": "course", "id": 999999, "credit": 3}],
             [ev_none]),
            ("C. 學分說錯（同名課 2/3 學分並存，只答 3）",
             "程式設計概論都是 3 學分。",
             [{"type": "course", "id": ev_sections["sections"][0]["id"], "credit": 3}],
             [ev_sections]),
            ("D. 只列 5 門卻說是全部（incomplete answer）",
             "3 學分以上的選修課共 5 門，如上。",
             [{"type": "total", "n": 5, "complete": True}],
             [ev_elective]),
            ("E. 宣稱沒有期末考，其實有（T4）",
             f"{has_final['name']} 沒有期末考。",
             [{"type": "lacks_assessment", "id": has_final["id"], "keyword": "期末考"}],
             [has_final_detail]),
        ]
        for title, answer, claims, evidence in scenarios:
            r = verify(answer, claims, evidence)
            print(f"\n{'=' * 64}\n{title}\n草稿：{answer}")
            print(f"Verifier：{'通過 ✅' if r['ok'] else '攔截 ⛔'}")
            for p in r["problems"]:
                print(f"   - {p}")
            print(f"最終回答：{r['answer']}")


if __name__ == "__main__":
    asyncio.run(main())
