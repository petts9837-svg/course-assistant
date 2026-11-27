"""
benchmark_system2.py：用期中 benchmark 的同一批題目（benchmark.py 的 33 題）＋ 5 題冷門主題，
測 System 2 的「整條流程」（Router → MCP 工具 → Answer → Verifier）的最終回答。

標準答案：benchmark.py 的 SQL 條件，對 courses.db（479 門）即時計算，不經過 MCP Server。

每題的判定
    查詢／篩選／同名課（lookup、filter、ambiguous）
        precision  回答列出的課號有多少在標準答案裡（列錯課＝扣分）
        complete   標準答案 ≤ 10 門：全部都要列出；> 10 門：回答寫的「共 N 門」要等於標準答案數
        正確 = precision 100% 且 complete
    主題（topic）：相關＝期中標準答案 SQL ∪ 大綱含人工列出的關鍵字（TOPIC_KEYWORDS）；
        列出的課都要相關、至少 1 門（搜尋結果不是完整清單，不要求列全）
    查無資料（標準答案 0 門）：回答必須是「查無資料」
    N4「哪些課沒有期末考」：依官方評量清單，回答的總數要等於清單算出的門數，並說明判斷依據

執行（專案根目錄）：
    python src/benchmark_system2.py           # 有 Cloudflare 憑證用 LLM，沒有就用離線規則模式
輸出：docs/benchmark_system2.md、data/benchmark_system2.json
"""

import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

from pipeline import Assistant  # noqa: E402

import benchmark as s1_bench    # noqa: E402  期中 System 1 的題目與標準答案 SQL

DB = ROOT / "courses.db"
OUT = ROOT / "docs" / "benchmark_system2.md"

# 期中 47 門資料的「查無資料」題，到了 479 門有些變成有答案：
#   N1「不用寫程式的課」→ 一般篩選題；N5「藝術史」→ 主題題
#   N2「量子計算」：479 門裡只有 2 份大綱在參考書目提到《量子經濟時代》，沒有任何課在教量子計算 → 仍是查無資料
OVERRIDE = {"N1": ("filter", None), "N5": ("topic", None),
            "N2": ("no-answer", "c.couCNa LIKE '%量子%' OR f.objective LIKE '%量子計算%' OR f.content LIKE '%量子計算%'")}

# 主題題的「相關」判定：期中標準答案的 SQL ∪ 大綱（去空白、小寫）含下列任一關鍵字的課。
# 關鍵字由我們人工列出，和 course-search 的程式無關，避免「自己驗自己」。
TOPIC_KEYWORDS = {"T1": ["python"], "T2": ["c語言", "c++"], "T3": ["html", "網頁", "web"],
                  "T4": ["app", "行動裝置", "android", "ios"], "T5": ["資料分析", "數據分析"],
                  "T6": ["期末報告"], "T7": ["專題"], "T8": ["演算法"], "N5": ["藝術史"]}

EXTRA = [  # 冷門主題：資料庫裡沒有 → 正確行為是「查無資料」（檢查 course-search 的相關度門檻）
    ("N6", "no-answer", "我想學天文學", "c.couCNa LIKE '%天文%' OR o.text LIKE '%天文%'"),
    ("N7", "no-answer", "有沒有烹飪課？", "c.couCNa LIKE '%烹飪%' OR o.text LIKE '%烹飪%'"),
    ("N8", "no-answer", "有教區塊鏈的課嗎？", "c.couCNa LIKE '%區塊鏈%' OR o.text LIKE '%區塊鏈%'"),
    ("N9", "no-answer", "我想學韓文", "c.couCNa LIKE '%韓文%' OR o.text LIKE '%韓文%'"),
    ("N10", "no-answer", "有籃球課嗎？", "c.couCNa LIKE '%籃球%' OR o.text LIKE '%籃球%'"),
]
NO_FINAL_SQL = """SELECT c.jonCouSn FROM courses c
    WHERE EXISTS (SELECT 1 FROM methods m WHERE m.jonCouSn=c.jonCouSn AND m.mName='學習評量')
      AND NOT EXISTS (SELECT 1 FROM methods m WHERE m.jonCouSn=c.jonCouSn AND m.mName='學習評量'
                      AND m.method_name LIKE '%期末考%')"""


def gold_sets(questions):
    conn = sqlite3.connect(f"{DB.resolve().as_uri()}?mode=ro", uri=True)
    try:
        gold = {qid: {r[0] for r in conn.execute(s1_bench.BASE + where)} for qid, _, _, where in questions}
        no_final = {r[0] for r in conn.execute(NO_FINAL_SQL)}
        texts = {r[0]: (r[1] + re.sub(r"\s+", "", r[2] or "")).lower()
                 for r in conn.execute("SELECT c.jonCouSn, c.couCNa, o.text FROM courses c "
                                       "JOIN outlines o ON o.jonCouSn = c.jonCouSn")}
    finally:
        conn.close()
    for qid, kws in TOPIC_KEYWORDS.items():
        gold[qid] = gold[qid] | {i for i, t in texts.items() if any(k in t for k in kws)}
    return gold, no_final


def judge(qid, typ, gold, no_final, answer):
    cited = list(dict.fromkeys(int(x) for x in re.findall(r"\[(\d{6})\]", answer)))
    m = re.search(r"共\s*(\d+)\s*(?:門|個班|班|堂)", answer)
    stated = int(m.group(1)) if m else None
    wrong = [i for i in cited if i not in gold]
    precision = (len(cited) - len(wrong)) / len(cited) if cited else None

    if qid == "N4":
        ok = stated == len(no_final) and "學習評量" in answer
        return ok, {"stated": stated, "expected": len(no_final), "cited": len(cited)}
    if not gold:
        return answer.strip().endswith("查無資料") and not cited, {"cited": len(cited)}
    if typ == "topic":
        ok = bool(cited) and not wrong
        return ok, {"cited": len(cited), "wrong": wrong[:5], "precision": precision}
    complete = set(gold) <= set(cited) if len(gold) <= 10 else stated == len(gold)
    ok = bool(cited) and not wrong and complete
    return ok, {"cited": len(cited), "wrong": wrong[:5], "stated": stated, "gold": len(gold),
                "precision": precision, "complete": complete}


def main():
    questions = [(qid, OVERRIDE.get(qid, (typ, where))[0], q, OVERRIDE.get(qid, (typ, where))[1] or where)
                 for qid, typ, q, where in s1_bench.QUESTIONS] + EXTRA
    gold, no_final = gold_sets(questions)
    bot = Assistant(log=False)
    rows, latencies = [], []
    for qid, typ, q, _ in questions:
        bot.reset()
        tr = bot.ask(q)
        ok, detail = judge(qid, typ, gold[qid], no_final, tr["answer"])
        eff_type = typ
        latencies.append(tr["latency_s"])
        rows.append({"id": qid, "type": eff_type, "question": q, "gold": len(gold[qid]), "pass": ok,
                     "verified": tr["verified"], "router": tr["router_by"],
                     "blocked": sum(1 for a in tr["attempts"] if not a["verdict"]["ok"]),
                     "tools": [f"{s['tool']}({', '.join(f'{k}={v}' for k, v in s['args'].items())})"
                               for s in tr["steps"] if s["step"] == "tool"],
                     "latency_s": tr["latency_s"], "detail": detail, "answer": tr["answer"],
                     "llm_error": tr.get("llm_error")})
        print(f"  {'✅' if ok else '❌'} {qid:<4} {q[:28]}")

    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    passed = sum(r["pass"] for r in rows)
    cited_total = sum(r["detail"].get("cited", 0) for r in rows)
    wrong_total = sum(len(r["detail"].get("wrong", [])) for r in rows)
    lines = [
        "# System 2 benchmark（期中 33 題 ＋ 5 題冷門主題）",
        "",
        f"- 產生時間：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模式：{bot.mode}",
        f"- 資料：courses.db 479 門；標準答案用 benchmark.py 的 SQL 即時計算",
        "",
        f"**正確 {passed} / {len(rows)} 題**　·　回答列出的課號 {cited_total} 個，其中不在標準答案的 {wrong_total} 個"
        f"　·　回應時間中位數 {statistics.median(latencies):.2f} 秒（最慢 {max(latencies):.2f} 秒）"
        f"　·　Verifier 攔截草稿 {sum(r['blocked'] for r in rows)} 份",
        "",
        (f"> ⚠️ 其中 {sum(1 for r in rows if r['llm_error'])} 題因 LLM 無法使用（{next(r['llm_error'] for r in rows if r['llm_error'])}）"
         "改用離線規則回答，這次結果不能代表 LLM 模式。" if any(r["llm_error"] for r in rows) else ""),
        "",
        "| 類型 | 題數 | 正確 |",
        "| --- | --- | --- |",
    ]
    for t, rs in by_type.items():
        lines.append(f"| {t} | {len(rs)} | {sum(r['pass'] for r in rs)} |")
    lines += ["", "| ID | 類型 | 問題 | 標準答案 | 結果 | 工具 | 細節 | 秒 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        d = {k: v for k, v in r["detail"].items() if v not in (None, [], "")}
        lines.append(f"| {r['id']} | {r['type']} | {r['question']} | {r['gold']} 門 | {'✅' if r['pass'] else '❌'} | "
                     f"{'<br>'.join(r['tools'])} | {json.dumps(d, ensure_ascii=False)} | {r['latency_s']} |")
    lines += ["", "## 判定規則", "", __doc__.split("每題的判定")[1].split("執行（")[0].strip()]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "benchmark_system2.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    print(f"\n{passed}/{len(rows)} 題正確，報告：{OUT.relative_to(ROOT)}")
    bot.hub.close()


if __name__ == "__main__":
    main()
