"""
檢索準確率 benchmark：33 題，標準答案由 SQL 對 courses.db 即時計算
（之後擴充資料庫，標準答案會自動更新，題目不用改）

比較三種檢索：
    bm25    純關鍵字（BM25 + 中文 bigram）— 不需要 API 憑證，可離線跑
    vector  純向量（Cloudflare bge-m3）    — 需要 CF_ACCOUNT_ID / CF_API_TOKEN 和向量索引
    hybrid  BM25 + 向量（RRF 合併）         — 同上

用法：
    python benchmark.py --dry                 # 只印每題的標準答案數量
    python benchmark.py                       # 跑全部能跑的模式（沒憑證就只跑 bm25）
    python benchmark.py --modes vector,hybrid --k 5

指標（只計「有標準答案」的題目；查無資料型的題目另外用 run_edge_tests.py 測 LLM 行為）：
    Hit@k     top-k 裡至少有 1 門正確課程的題目比例
    Recall@k  |top-k ∩ 正確課程| / min(k, |正確課程|)   （正確課程多於 k 時，滿分仍是 1）
    MRR       第一門正確課程排名的倒數平均

輸出：data/benchmark_report.md
"""

import argparse
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

import rag_core

OUT = Path("data") / "benchmark_report.md"

BASE = ("SELECT c.jonCouSn FROM courses c "
        "LEFT JOIN outline_fields f ON f.jonCouSn = c.jonCouSn "
        "LEFT JOIN outlines o ON o.jonCouSn = c.jonCouSn WHERE ")

SCHED = "c.jonCouSn IN (SELECT jonCouSn FROM schedules WHERE {})"

# (編號, 類型, 問題, 標準答案的 SQL WHERE 條件)
QUESTIONS = [
    # --- 查詢型：問某老師／某課的資訊 ---
    ("L1", "lookup", "王國華老師教哪些課？", "c.tchCNa = '王國華'"),
    ("L2", "lookup", "石佳惠老師開的課是幾學分？", "c.tchCNa = '石佳惠'"),
    ("L3", "lookup", "李鍾斌老師教什麼課？", "c.tchCNa = '李鍾斌'"),
    ("L4", "lookup", "WEB程式設計在星期幾上課？", "TRIM(c.couCNa) = 'WEB程式設計'"),
    ("L5", "lookup", "Python程式設計實務的授課老師是誰？", "TRIM(c.couCNa) = 'Python程式設計實務'"),
    ("L6", "lookup", "行動裝置程式設計是必修還是選修？", "TRIM(c.couCNa) = '行動裝置程式設計'"),
    ("L7", "lookup", "物件導向程式設計的評分方式是什麼？", "TRIM(c.couCNa) = '物件導向程式設計'"),
    ("L8", "lookup", "3D程式設計在哪間教室上課？", "TRIM(c.couCNa) = '3D程式設計'"),
    # --- 條件篩選型：學分、必選修、系所、時段 ---
    ("F1", "filter", "推薦 3 學分以上的選修課", "c.credit >= 3 AND c.reqSelCNa = '選修'"),
    ("F2", "filter", "1 學分的課有哪些？", "c.credit = 1"),
    ("F3", "filter", "有哪些通識課程？", "c.reqSelCNa = '通識'"),
    ("F4", "filter", "星期六有什麼課？", SCHED.format("couWek = '6'")),
    ("F5", "filter", "資管系的必修課有哪些？", "f.dept_class LIKE '資管%' AND c.reqSelCNa = '必修'"),
    ("F6", "filter", "統資系開了哪些課？", "f.dept_class LIKE '統資%'"),
    ("F7", "filter", "電機系有什麼課？", "f.dept_class LIKE '電機%'"),
    ("F8", "filter", "有哪些全英文授課的課程？", "f.tea_lang = '全英文'"),
    ("F9", "filter", "有哪些遠距教學的課程？",
     "c.jonCouSn IN (SELECT jonCouSn FROM classifies WHERE couClassifyCna = '遠距教學類')"),
    ("F10", "filter", "星期五晚上有什麼課？", SCHED.format("couWek = '5' AND section LIKE '%E%'")),
    # --- 主題語意型：問大綱內容 ---
    ("T1", "topic", "我想學 Python，有什麼課？", "c.couCNa LIKE '%Python%' OR f.objective LIKE '%Python%'"),
    ("T2", "topic", "有哪些課會教 C 語言？", "f.objective LIKE '%C語言%' OR f.content LIKE '%C語言%'"),
    ("T3", "topic", "我想學網頁開發", "c.couCNa LIKE '%WEB%' OR o.text LIKE '%HTML%'"),
    ("T4", "topic", "我想學手機 App 開發",
     "c.couCNa LIKE '%行動裝置%' OR f.objective LIKE '%App%' OR f.objective LIKE '%行動%'"),
    ("T5", "topic", "有沒有跟資料分析有關的課？",
     "c.couCNa LIKE '%資料%' OR f.objective LIKE '%資料分析%' OR f.content LIKE '%資料分析%'"),
    ("T6", "topic", "哪些課有期末報告？", "o.text LIKE '%期末報告%'"),
    ("T7", "topic", "哪些課需要做專題？", "o.text LIKE '%專題%'"),
    ("T8", "topic", "有沒有教演算法的課？", "o.text LIKE '%演算法%'"),
    # --- 歧義／聚合型：同名課多個班級 ---
    ("A1", "ambiguous", "程式設計概論是幾學分？", "TRIM(c.couCNa) = '程式設計概論'"),
    ("A2", "ambiguous", "程式設計(一)有哪幾個班？", "TRIM(c.couCNa) = '程式設計(一)'"),
    # --- 查無資料／錯誤前提型：標準答案為空，正確行為是「說查無」 ---
    ("N1", "no-answer", "有沒有不用寫程式的課？", "c.is_programming = 0"),
    ("N2", "no-answer", "輔大有開量子計算的課嗎？", "c.couCNa LIKE '%量子%' OR o.text LIKE '%量子%'"),
    ("N3", "no-answer", "資工系的程式設計概論在星期幾上課？",
     "TRIM(c.couCNa) = '程式設計概論' AND f.dept_class LIKE '資工%'"),
    ("N4", "no-answer", "哪些課沒有期末考？", "0"),  # 資訊缺失，無法由資料推得
    ("N5", "no-answer", "我想修藝術史", "c.couCNa LIKE '%藝術%' OR o.text LIKE '%藝術史%'"),
]


def ground_truth(conn):
    gt = {}
    for qid, typ, question, where in QUESTIONS:
        gt[qid] = {r[0] for r in conn.execute(BASE + where)}
    return gt


def have_credentials():
    return bool(os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"))


class Searchers:
    """把三種檢索包成同一個介面：search(mode, question, k) -> [課號, ...]"""

    def __init__(self, modes):
        docs = rag_core.load_documents()
        self.ids = [d["id"] for d in docs]
        self.bm25 = rag_core.BM25([d["text"] for d in docs])
        self.retriever = None
        if any(m in ("vector", "hybrid") for m in modes):
            self.retriever = rag_core.Retriever()

    def search(self, mode, question, k):
        if mode == "bm25":
            sc = self.bm25.scores(question)
            order = np.argsort(-sc)[:k]
            return [self.ids[i] for i in order if sc[i] > 0]
        return [h["id"] for h in self.retriever.search(question, k, mode)]


def score(ranked, rel, k):
    top = ranked[:k]
    inter = [x for x in top if x in rel]
    hit = 1.0 if inter else 0.0
    recall = len(inter) / min(k, len(rel))
    mrr = 0.0
    for rank, x in enumerate(top, 1):
        if x in rel:
            mrr = 1 / rank
            break
    return hit, recall, mrr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="bm25,vector,hybrid")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--dry", action="store_true", help="只印標準答案數量")
    a = ap.parse_args()

    conn = sqlite3.connect(rag_core.DB_PATH)
    gt = ground_truth(conn)
    conn.close()

    if a.dry:
        for qid, typ, question, _ in QUESTIONS:
            print(f"{qid:<4}{typ:<10}{len(gt[qid]):>3} 門  {question}")
        n_ans = sum(1 for v in gt.values() if v)
        print(f"\n共 {len(QUESTIONS)} 題，有標準答案 {n_ans} 題，查無型 {len(QUESTIONS) - n_ans} 題")
        return

    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    skipped = []
    if not have_credentials():
        skipped = [m for m in modes if m in ("vector", "hybrid")]
        modes = [m for m in modes if m == "bm25"]
        print("⚠ 沒有設定 CF_ACCOUNT_ID / CF_API_TOKEN，只跑 bm25；略過：", skipped)

    s = Searchers(modes)
    answerable = [(qid, typ, q) for qid, typ, q, _ in QUESTIONS if gt[qid]]

    per_q = defaultdict(dict)                       # per_q[qid][mode] = (hit, recall, mrr)
    by_type = defaultdict(lambda: defaultdict(list))  # by_type[mode][type] -> [(hit, recall, mrr)]
    for mode in modes:
        for qid, typ, q in answerable:
            r = score(s.search(mode, q, a.k), gt[qid], a.k)
            per_q[qid][mode] = r
            by_type[mode][typ].append(r)
            by_type[mode]["ALL"].append(r)

    def avg(rows, i):
        return sum(r[i] for r in rows) / len(rows)

    lines = [f"# 檢索 Benchmark 報告\n",
             f"- top-k = {a.k}　題數：{len(QUESTIONS)}（有標準答案 {len(answerable)}、查無型 {len(QUESTIONS) - len(answerable)}）\n"
             f"- 模式：{', '.join(modes)}" + (f"　（略過：{', '.join(skipped)}，缺少憑證）" if skipped else "") + "\n",
             "## 總表\n", "| 類型 | 題數 | " + " | ".join(f"{m} Hit@k / Recall@k / MRR" for m in modes) + " |",
             "|---|---|" + "---|" * len(modes)]
    for typ in ["lookup", "filter", "topic", "ambiguous", "ALL"]:
        n = sum(1 for _, t, _ in answerable if t == typ) if typ != "ALL" else len(answerable)
        if n == 0:
            continue
        cells = [f"{avg(by_type[m][typ], 0):.2f} / {avg(by_type[m][typ], 1):.2f} / {avg(by_type[m][typ], 2):.2f}"
                 for m in modes]
        lines.append(f"| {typ} | {n} | " + " | ".join(cells) + " |")

    lines += ["\n## 逐題結果（Hit / Recall）\n",
              "| 題號 | 類型 | 問題 | 正確課數 | " + " | ".join(modes) + " |",
              "|---|---|---|---|" + "---|" * len(modes)]
    for qid, typ, q in answerable:
        cells = [f"{'✅' if per_q[qid][m][0] else '❌'} {per_q[qid][m][1]:.2f}" for m in modes]
        lines.append(f"| {qid} | {typ} | {q} | {len(gt[qid])} | " + " | ".join(cells) + " |")

    lines += ["\n## 查無型題目（不算檢索分數，用 run_edge_tests.py 觀察 LLM 是否誠實說查無）\n"]
    for qid, typ, q, _ in QUESTIONS:
        if not gt[qid]:
            lines.append(f"- {qid}：{q}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:12]))
    print(f"\n完整報告：{OUT}")


if __name__ == "__main__":
    main()
