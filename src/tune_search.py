"""
tune_search.py：校正 course-search 的向量相似度門檻（需要 Cloudflare 憑證，會呼叫 bge-m3 約 20 次）

做法：拿「有相關課」和「資料庫裡沒有」兩組問題，各自算出問題向量與 479 門課大綱向量的 cosine：
    有相關課的問題 → 相關課（大綱含人工列出的關鍵字）的最高相似度
    沒有相關課的問題 → 所有課裡的最高相似度（＝向量搜尋會撈到的「最像的不相關課」）
建議門檻＝兩組之間的分界。結果寫進 data/search_calibration.md，並印出建議的 .env 設定。

執行（專案根目錄）：python src/tune_search.py
"""

import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(ROOT))

import rag_core  # noqa: E402  與建索引相同的 embed()（會讀 .env）

RELEVANT = {  # 問題 → 大綱裡代表「相關」的關鍵字（人工列出）
    "我想學 Python": ["python"],
    "有哪些課會教 C 語言": ["c語言", "c++"],
    "我想學網頁開發": ["html", "網頁", "web"],
    "我想學手機 App 開發": ["行動裝置", "android", "ios"],
    "跟資料分析有關的課": ["資料分析", "數據分析"],
    "有沒有教演算法的課": ["演算法"],
    "想了解人工智慧": ["人工智慧"],
    "想修藝術史": ["藝術史"],
    "心理學相關的課": ["心理學"],
    "行銷管理的課": ["行銷"],
}
UNRELATED = ["我想學天文學", "有沒有烹飪課", "有教區塊鏈的課嗎", "我想學韓文", "有籃球課嗎",
             "輔大有開量子計算的課嗎", "今天天氣如何", "推薦好吃的餐廳", "我想學鋼琴", "有教游泳的課嗎"]


def main():
    data = np.load(ROOT / "data" / "vector_index.npz")
    ids, vecs = [int(x) for x in data["ids"]], data["vectors"]
    conn = sqlite3.connect(f"{(ROOT / 'courses.db').resolve().as_uri()}?mode=ro", uri=True)
    texts = {r[0]: (r[1] + re.sub(r"\s+", "", r[2] or "")).lower() for r in conn.execute(
        "SELECT c.jonCouSn, c.couCNa, o.text FROM courses c JOIN outlines o ON o.jonCouSn = c.jonCouSn")}
    conn.close()

    qs = list(RELEVANT) + UNRELATED
    qv = rag_core.embed(qs)
    sims = qv @ vecs.T

    rows, rel_best, unrel_best = [], [], []
    for qi, q in enumerate(qs):
        s = sims[qi]
        top = int(np.argmax(s))
        if q in RELEVANT:
            good = [j for j, cid in enumerate(ids) if any(k in texts.get(cid, "") for k in RELEVANT[q])]
            best = float(max(s[j] for j in good)) if good else float("nan")
            rel_best.append(best)
            rows.append(("有相關課", q, len(good), best, float(s[top])))
        else:
            unrel_best.append(float(s[top]))
            rows.append(("資料庫沒有", q, 0, float("nan"), float(s[top])))

    hi_unrel = max(unrel_best)
    lo_rel = float(np.nanmin(rel_best))
    rec = round((hi_unrel + lo_rel) / 2, 3) if lo_rel > hi_unrel else round(hi_unrel + 0.02, 3)
    lines = ["# course-search 向量門檻校正", "",
             f"- 不相關問題的最高相似度：**{hi_unrel:.3f}**（向量搜尋撈到的「最像的不相關課」）",
             f"- 有相關課的問題，相關課的最高相似度裡最低的：**{lo_rel:.3f}**",
             f"- 建議 `SEARCH_MIN_SIM_VECTOR_ONLY={rec}`"
             + ("" if lo_rel > hi_unrel else "（兩組重疊：只靠向量分不開，建議維持關鍵字優先）"),
             "", "| 類型 | 問題 | 相關課數 | 相關課最高相似度 | 全部課最高相似度 |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {t} | {q} | {n} | {b:.3f} | {m:.3f} |" for t, q, n, b, m in rows]
    out = ROOT / "data" / "search_calibration.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:5]))
    print(f"\n完整結果：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
