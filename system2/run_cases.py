"""
用同一批 System 1 失敗案例測試 System 2，輸出修正前後對照（給 VERIFICATION_LOG.md 用）。

執行（在專案根目錄）：python system2/run_cases.py
輸出：data/system2_cases.md（Markdown 表格與每題詳細結果）
標準答案一律用獨立的 SQL 計算，不使用 MCP 工具，避免「自己驗證自己」。
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402

DB = agent.ROOT / "courses.db"
OUT = agent.ROOT / "data" / "system2_cases.md"


def sql(q, *p):
    with sqlite3.connect(DB) as c:
        return c.execute(q, p).fetchone()[0]


# (編號, 問題, System 1 的錯誤（取自 VERIFICATION_LOG.md）, 標準答案的總數, 追問用的前一題)
CASES = [
    ("T5", "推薦 3 學分以上的選修課",
     "列 2 門，其中程式設計(三)其實是必修（11/2）",
     sql("SELECT COUNT(*) FROM courses WHERE credit >= 3 AND reqSelCNa = '選修'"), None),
    ("T4", "哪些課沒有期末考？",
     "192 門只點名 1 門，漏掉檢索結果中的計量經濟學（11/2）；另一次列 5 門其中 3 門有期末考（10/30）",
     sql("""SELECT COUNT(*) FROM courses WHERE jonCouSn IN (SELECT jonCouSn FROM methods WHERE mName='學習評量')
            AND jonCouSn NOT IN (SELECT jonCouSn FROM methods WHERE mName='學習評量' AND method_name LIKE '%期末考%')"""), None),
    ("Case 5", "有沒有不用寫程式的課？",
     "回答「沒有」，找到的 5 門全是程式課（11/2，共 6 次）",
     sql("SELECT COUNT(*) FROM courses WHERE is_programming = 0"), None),
    ("T3", "資工系的程式設計概論在星期幾上課？",
     "把 5 個其他系的班級說成「資工系（或相關）」（10/23）",
     sql("""SELECT COUNT(*) FROM courses c JOIN outline_fields f USING(jonCouSn)
            WHERE c.couCNa LIKE '%程式設計概論%' AND f.dept_class LIKE '%資工%'"""), None),
    ("T2", "程式設計概論是幾學分？",
     "只答一種學分，實際 2、3 學分並存",
     sql("SELECT COUNT(*) FROM courses WHERE TRIM(couCNa) = '程式設計概論'"), None),
    ("Case 4", "吳周駿老師的程式設計概論有期中考嗎？",
     "完整貼上課程資訊仍找不到，且把蔡明志的課誤列為有期中考（10/30）",
     None, None),
    ("T8", "那它在星期幾上課？",
     "追問失去上下文，檢索到 5 門無關的課",
     None, "吳周駿老師的程式設計概論有期中考嗎？"),
    ("N2", "輔大有開量子計算的課嗎？",
     "（應回答查無資料）",
     sql("SELECT COUNT(*) FROM courses WHERE couCNa LIKE '%量子%'"), None),
]


def main():
    tools = agent.tool_schemas()
    memory, last_q, rows, details = [], None, [], []
    for cid, q, s1, truth, prev in CASES:
        if prev and last_q != prev:          # 追問題：先問前一題建立上下文
            memory = agent.answer(prev, tools=tools)["memory"]
        if not prev:
            memory = []
        tr = agent.answer(q, memory=memory, tools=tools)
        memory, last_q = tr["memory"], q
        got = tr["evidence"].get("count")
        match = "—" if truth is None else ("✅" if got == truth else f"❌（應為 {truth}）")
        r = tr["route"]
        rows.append(f"| {cid} | {q} | {s1} | `{r['tool']}` | {got if got is not None else '—'} | {match} | {tr['status']} | {tr['seconds']}s |")
        problems = [p for s in tr["steps"] for p in s["verdict"]["problems"]]
        details.append(
            f"### {cid}：{q}\n\n"
            f"- **System 1**：{s1}\n"
            f"- **Router**（{r['by']}）：`{r['tool']}({json.dumps(r['args'], ensure_ascii=False)})`\n"
            + (f"- **Router 防護修正**：{'；'.join(r['fixes'])}\n" if r.get('fixes') else "")
            + (f"- **自動追查**：`get_course({tr['chained']['args']['course_id']})`\n" if tr.get('chained') else "")
            +
            f"- **工具總數**：{got}；**SQL 標準答案**：{truth if truth is not None else '—'}\n"
            f"- **Verifier**：{tr['status']}" + (f"；攔截原因：{'；'.join(problems)}" if problems else "") + "\n\n"
            f"```\n{tr['final']}\n```\n")
        print(f"{cid}: {r['tool']} count={got} truth={truth} {tr['status']} {tr['seconds']}s")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        f"# System 2 修正前後對照（{datetime.now():%Y-%m-%d %H:%M}，模型 {agent.CHAT_MODEL}）\n\n"
        "| 編號 | 問題 | System 1（修正前） | System 2 工具 | 工具總數 | 對照 SQL | 驗證結果 | 耗時 |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "驗證結果：verified＝初稿直接通過；repaired＝初稿被攔截、修正後通過；fallback＝改用工具資料直出。\n\n"
        + "\n".join(details), encoding="utf-8")
    print(f"\n已輸出 {OUT}")


if __name__ == "__main__":
    main()
