"""
course-search：System 2 的第二個 MCP Server（唯讀，語意／關鍵字搜尋課程大綱）

course-sql 負責「有明確條件」的問題（學分、星期、系所、有沒有期末考）；
course-search 負責「描述型」的問題（例如「想學資料分析」「跟 AI 有關的課」），
用 BM25 關鍵字 + bge-m3 向量，以 Reciprocal Rank Fusion（RRF）合併排名。

規則
- 搜尋結果是「相似度排序」，不是完整清單：回傳 exhaustive=false，回答不可以寫「共 N 門」。
- 沒有任何關鍵字命中、向量也不可用時，回傳 {"returned": 0, "note": "查無資料"}。
- 向量索引只涵蓋已建立的課程（data/vector_index.npz）；沒涵蓋的課只靠 BM25，
  回傳的 index_coverage 會寫清楚涵蓋幾門，避免把部分結果當成全部。
- 查詢向量用 Cloudflare Workers AI（與建索引相同的 @cf/baai/bge-m3）；
  沒有設定 CF_ACCOUNT_ID / CF_API_TOKEN 時自動退回純 BM25，回傳的 mode_used 會標示。

啟動（stdio）：
    python src/mcp_servers/course_search_server.py
通常由 Client（src/mcp_hub.py）自動啟動。
"""

import math
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Literal

import numpy as np
import requests
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("COURSES_DB", ROOT / "courses.db"))
INDEX_PATH = Path(os.environ.get("VECTOR_INDEX", ROOT / "data" / "vector_index.npz"))
EMBED_MODEL = os.environ.get("CF_EMBED_MODEL", "@cf/baai/bge-m3")
NO_DATA = "查無資料"
SNIPPET_CHARS = 160

mcp = FastMCP("course-search")


# ---------- 資料 ----------

def _load_docs():
    conn = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT c.jonCouSn, TRIM(c.couCNa), c.tchCNa, c.credit, c.reqSelCNa, f.dept_class,
               f.objective, o.text,
               (SELECT group_concat(TRIM(s.couWek) || ':' || s.section, ' ')
                  FROM schedules s WHERE s.jonCouSn = c.jonCouSn)
        FROM courses c
        JOIN outlines o ON o.jonCouSn = c.jonCouSn
        LEFT JOIN outline_fields f ON f.jonCouSn = c.jonCouSn
        WHERE o.text IS NOT NULL AND o.text != ''
        ORDER BY c.jonCouSn""").fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "teacher": r[2], "credit": r[3], "type": r[4],
             "dept": r[5], "objective": r[6] or "", "text": r[7], "schedule": r[8]} for r in rows]


def _tokens(text):
    """中文用字元 bigram、英文數字用整個單字（與 System 1 的 rag_core 相同）。"""
    text = text.lower()
    words = re.findall(r"[a-z0-9]+", text)
    han = re.sub(r"[^一-鿿]", " ", text)
    bigrams = []
    for seg in han.split():
        bigrams += [seg[i:i + 2] for i in range(len(seg) - 1)] or [seg]
    return words + bigrams


# 問句裡沒有內容意義的詞：比對前先拿掉，避免「今天天氣如何」因為「如何」命中一堆大綱，
# 也避免「跟人工智慧有關的課」切出「慧有」「關的」這種跨詞的 bigram
STOP_PHRASES = sorted("""有沒有 有哪些 哪些 什麼 如何 怎麼 可以 推薦 一下 我想 想要 想學 想修 請問 課程 的課 課 相關
有關 關於 學習 一些 幫我 給我 告訴我 嗎 呢 吧 今天 明天 現在 時候 輔大 輔仁 有開 開的 開設 跟 和 與 的 我 你
是 有 要 學 上""".split(), key=len, reverse=True)
MIN_COVERAGE = float(os.environ.get("SEARCH_MIN_COVERAGE", 0.5))   # 關鍵字至少命中一半才算相關
MIN_VECTOR_SIM = float(os.environ.get("SEARCH_MIN_SIM", 0.55))     # 向量相似度門檻（bge-m3 cosine）


def content_tokens(query):
    q = query.lower()
    for w in STOP_PHRASES:
        q = q.replace(w, " ")
    return set(_tokens(q))


class BM25:
    def __init__(self, texts, k1=1.5, b=0.75):
        self.docs = [Counter(_tokens(t)) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / max(len(self.lens), 1)
        df = Counter(tok for d in self.docs for tok in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query):
        q = content_tokens(query)
        out = np.zeros(len(self.docs))
        for i, (d, L) in enumerate(zip(self.docs, self.lens)):
            for t in q:
                if t in d:
                    tf = d[t]
                    out[i] += self.idf[t] * tf * (self.k1 + 1) / (
                        tf + self.k1 * (1 - self.b + self.b * L / self.avg))
        return out


DOCS = _load_docs()
BM = BM25([d["name"] + " " + d["text"] for d in DOCS])
POS = {d["id"]: i for i, d in enumerate(DOCS)}

if INDEX_PATH.exists():
    _idx = np.load(INDEX_PATH)
    _keep = [i for i, sn in enumerate(_idx["ids"]) if int(sn) in POS]
    VEC_IDS = [int(_idx["ids"][i]) for i in _keep]
    VECTORS = _idx["vectors"][_keep]
else:
    VEC_IDS, VECTORS = [], np.zeros((0, 1024), dtype=np.float32)


def _embed_query(text):
    """用 Cloudflare bge-m3 取得查詢向量；沒有憑證或失敗時回傳 None（改用純 BM25）。"""
    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not (account and token) or not VEC_IDS:
        return None
    try:
        resp = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{EMBED_MODEL}",
            json={"text": [text]}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        resp.raise_for_status()
        v = np.array(resp.json()["result"]["data"][0], dtype=np.float32)
        return v / np.linalg.norm(v)
    except Exception:
        return None


def _snippet(doc, query):
    """取大綱中最先出現查詢關鍵字的一小段，讓回答有出處可以引用。"""
    text = re.sub(r"\s+", " ", doc["text"])
    for tok in sorted(content_tokens(query), key=len, reverse=True):
        i = text.lower().find(tok)
        if i >= 0:
            start = max(0, i - 40)
            return ("…" if start else "") + text[start:start + SNIPPET_CHARS] + "…"
    return (doc["objective"] or text)[:SNIPPET_CHARS] + "…"


# ---------- 工具 ----------

@mcp.tool()
def search_courses(query: str, k: int = 5,
                   mode: Literal["hybrid", "bm25", "vector"] = "hybrid") -> dict:
    """用自然語言描述搜尋課程大綱（例如「想學資料分析」「跟人工智慧有關的課」）。

    適合沒有明確條件、需要看大綱內容的問題。結果依相似度排序，不是完整清單
    （exhaustive=false），回答時不可以說「共 N 門」或「只有這些」。
    有明確條件（學分、星期、系所、評量方式）時應該改用 course-sql 的 filter_courses。

    Args:
        query: 搜尋描述，用使用者的原話即可。
        k: 回傳幾門（1–10），預設 5。
        mode: hybrid（預設，BM25 + 向量）、bm25（只用關鍵字）、vector（只用向量）。
    """
    k = max(1, min(int(k), 10))
    pool = 30
    terms = content_tokens(query)
    if not terms:
        return {"query": query, "returned": 0, "courses": [], "exhaustive": False, "mode_used": "none",
                "note": NO_DATA, "basis": "問題中沒有可以搜尋的關鍵字"}
    bm = BM.scores(query)

    def coverage(i):
        return sum(1 for t in terms if t in BM.docs[i]) / len(terms)

    # 只保留關鍵字命中夠多的課，避免「量子計算」因為「計算」一詞撈到不相關的課
    bm_rank = [DOCS[i]["id"] for i in np.argsort(-bm)[:pool] if bm[i] > 0 and coverage(i) >= MIN_COVERAGE]

    vec_rank, mode_used = [], "bm25"
    if mode in ("hybrid", "vector"):
        q = _embed_query(query)
        if q is not None:
            sims = VECTORS @ q
            vec_rank = [VEC_IDS[i] for i in np.argsort(-sims)[:pool] if sims[i] >= MIN_VECTOR_SIM]
            mode_used = "hybrid" if mode == "hybrid" else "vector"
    if mode == "vector" and not vec_rank:
        mode_used = "bm25"  # 向量不可用時退回關鍵字，並在回傳中標示

    fused = Counter()
    if mode_used in ("hybrid", "bm25"):
        for rank, sn in enumerate(bm_rank):
            fused[sn] += 1 / (60 + rank + 1)
    if mode_used in ("hybrid", "vector"):
        for rank, sn in enumerate(vec_rank):
            fused[sn] += 1 / (60 + rank + 1)

    hits = []
    for sn, score in fused.most_common(k):
        d = DOCS[POS[sn]]
        hits.append({"id": d["id"], "name": d["name"], "teacher": d["teacher"], "credit": d["credit"],
                     "type": d["type"], "dept": d["dept"], "schedule": d["schedule"], "score": round(score, 5),
                     "keyword_hit": sn in bm_rank,
                     "keyword_coverage": round(coverage(POS[sn]), 2), "snippet": _snippet(d, query)})

    out = {"query": query, "mode_used": mode_used, "returned": len(hits), "exhaustive": False,
           "courses": hits,
           "index_coverage": {"vector": len(VEC_IDS), "keyword": len(DOCS)},
           "thresholds": {"keyword_coverage": MIN_COVERAGE, "vector_sim": MIN_VECTOR_SIM},
           "basis": "依相似度排序的搜尋結果，不是完整清單"}
    if not hits:
        out["note"] = NO_DATA
    return out


@mcp.tool()
def index_status() -> dict:
    """回報搜尋索引的狀態：向量索引涵蓋幾門課、關鍵字索引涵蓋幾門課、是否能用向量搜尋。"""
    can_vec = bool(VEC_IDS) and bool(os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"))
    return {"vector_indexed": len(VEC_IDS), "keyword_indexed": len(DOCS),
            "vector_available": can_vec,
            "hint": None if len(VEC_IDS) >= len(DOCS)
            else "向量索引尚未涵蓋全部課程，請執行 python rag_core.py build"}


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
