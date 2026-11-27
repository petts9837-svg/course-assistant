"""
System 1：課程大綱選課助理的 RAG 核心（Cloudflare Workers AI 版）

需要的環境變數（不要寫進程式碼）:
    CF_ACCOUNT_ID   Cloudflare 帳號 ID（後台右側欄可看到）
    CF_API_TOKEN    具備 Workers AI 權限的 API Token

用法 Usage:
    pip install requests numpy streamlit
    python rag_core.py build                      # 建立向量索引（課程有更新時重跑）
    python rag_core.py ask "有沒有不用寫程式的課"    # 命令列測試
    streamlit run app.py                          # 開網頁介面
"""

import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import requests


def _load_env(path=Path(__file__).resolve().parent / ".env"):
    """讀取 .env（KEY=VALUE）；已設定的環境變數優先。System 2 用同一份 .env。"""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

DB_PATH = "courses.db"
INDEX_PATH = Path("data") / "vector_index.npz"
LOG_PATH = Path("data") / "qa_log.jsonl"

EMBED_MODEL = os.environ.get("CF_EMBED_MODEL", "@cf/baai/bge-m3")
CHAT_MODEL = os.environ.get("CF_CHAT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")

SYSTEM_PROMPT = """你是輔仁大學的選課助理。請只根據下方「參考課程大綱」回答使用者的問題，用繁體中文回答。
回答時列出符合的課程名稱、授課教師、學分與上課時間。"""
# 注意：System 1 刻意只用簡單的提示詞，沒有加入查證或拒答規則，
# 這樣才能在缺陷診斷時觀察到 naive RAG 的錯誤。


# ---------- Cloudflare Workers AI ----------

class TooLargeError(RuntimeError):
    pass


def _cf_run(model, payload, retries=3):
    account = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_API_TOKEN")
    if not account or not token:
        raise RuntimeError("請先設定環境變數 CF_ACCOUNT_ID 和 CF_API_TOKEN")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
    for attempt in range(retries):
        resp = requests.post(url, json=payload, timeout=120,
                             headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 429 and attempt < retries - 1:  # 太快或額度用完
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code == 413:
            raise TooLargeError(resp.text[:300])
        if not resp.ok:
            raise RuntimeError(f"Cloudflare API 錯誤 {resp.status_code}: {resp.text[:300]}")
        return resp.json()["result"]


MAX_DOC_CHARS = 6000  # bge-m3 單篇上限約 8192 token，過長的大綱截斷


def _embed_batch(texts):
    """送一批；如果超過模型上限（413），拆成兩半各自重送。"""
    try:
        return _cf_run(EMBED_MODEL, {"text": texts})["data"]
    except TooLargeError:
        if len(texts) == 1:
            raise RuntimeError("單篇大綱仍然太長，請調小 MAX_DOC_CHARS")
        mid = len(texts) // 2
        return _embed_batch(texts[:mid]) + _embed_batch(texts[mid:])


def embed(texts, batch_size=10):
    texts = [t[:MAX_DOC_CHARS] for t in texts]
    vectors = []
    for i in range(0, len(texts), batch_size):
        vectors.extend(_embed_batch(texts[i:i + batch_size]))
        if len(texts) > batch_size:
            print(f"  已完成 {min(i + batch_size, len(texts))}/{len(texts)}")
    arr = np.array(vectors, dtype=np.float32)
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def chat(messages):
    result = _cf_run(CHAT_MODEL, {"messages": messages, "max_tokens": 1024})
    if isinstance(result, dict) and "response" in result:  # 一般 Workers AI 格式
        return result["response"]
    if isinstance(result, dict) and "choices" in result:  # OpenAI 相容格式的模型
        return result["choices"][0]["message"]["content"]
    return str(result)


# ---------- 資料 ----------

def load_documents(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT c.jonCouSn, c.couCNa, c.tchCNa, o.text
        FROM courses c JOIN outlines o ON c.jonCouSn = o.jonCouSn
        WHERE o.text IS NOT NULL AND o.text != ''
        ORDER BY c.jonCouSn""").fetchall()
    conn.close()
    if not rows:
        sys.exit("資料庫裡沒有大綱，請先執行 fju_outline_fetcher.py")
    return [{"id": r[0], "name": r[1], "teacher": r[2], "text": r[3]} for r in rows]


# ---------- 索引 ----------

def build_index(db_path=DB_PATH):
    docs = load_documents(db_path)
    print(f"為 {len(docs)} 門課建立向量（模型 {EMBED_MODEL}）…")
    vectors = embed([d["text"] for d in docs])
    INDEX_PATH.parent.mkdir(exist_ok=True)
    np.savez(INDEX_PATH, ids=np.array([d["id"] for d in docs]), vectors=vectors)
    print(f"完成，索引存到 {INDEX_PATH}，維度 {vectors.shape[1]}")


def load_index():
    if not INDEX_PATH.exists():
        raise RuntimeError("找不到向量索引，請先執行：python rag_core.py build")
    data = np.load(INDEX_PATH)
    return data["ids"], data["vectors"]


# ---------- 檢索 ----------

def _tokens(text):
    """中文用字元 bigram、英文數字用整個單字，不需要額外斷詞套件。"""
    text = text.lower()
    words = re.findall(r"[a-z0-9]+", text)
    han = re.sub(r"[^一-鿿]", " ", text)
    bigrams = []
    for seg in han.split():
        bigrams += [seg[i:i + 2] for i in range(len(seg) - 1)] or [seg]
    return words + bigrams


class BM25:
    def __init__(self, texts, k1=1.5, b=0.75):
        self.docs = [Counter(_tokens(t)) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / len(self.lens)
        df = Counter(tok for d in self.docs for tok in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query):
        q = _tokens(query)
        out = np.zeros(len(self.docs))
        for i, (d, L) in enumerate(zip(self.docs, self.lens)):
            for t in q:
                if t in d:
                    tf = d[t]
                    out[i] += self.idf[t] * tf * (self.k1 + 1) / (
                        tf + self.k1 * (1 - self.b + self.b * L / self.avg))
        return out


class Retriever:
    def __init__(self, db_path=DB_PATH):
        self.docs = {d["id"]: d for d in load_documents(db_path)}
        ids, vectors = load_index()
        keep = [i for i, sn in enumerate(ids) if int(sn) in self.docs]
        self.ids = [int(ids[i]) for i in keep]
        self.vectors = vectors[keep]
        self.bm25 = BM25([self.docs[sn]["text"] for sn in self.ids])

    def vector_search(self, query, k):
        q = embed([query])[0]
        sims = self.vectors @ q
        order = np.argsort(-sims)[:k]
        return [(self.ids[i], float(sims[i])) for i in order]

    def hybrid_search(self, query, k, rrf_k=60, pool=30):
        """關鍵字 BM25 + 向量，用 Reciprocal Rank Fusion 合併排名。"""
        vec = self.vector_search(query, pool)
        bm = self.bm25.scores(query)
        bm_rank = [self.ids[i] for i in np.argsort(-bm)[:pool] if bm[i] > 0]
        fused = Counter()
        for rank, (sn, _) in enumerate(vec):
            fused[sn] += 1 / (rrf_k + rank + 1)
        for rank, sn in enumerate(bm_rank):
            fused[sn] += 1 / (rrf_k + rank + 1)
        return [(sn, score) for sn, score in fused.most_common(k)]

    def search(self, query, k=5, mode="vector"):
        hits = self.hybrid_search(query, k) if mode == "hybrid" else self.vector_search(query, k)
        return [dict(self.docs[sn], score=score) for sn, score in hits]


# ---------- 問答 ----------

def answer(retriever, question, k=5, mode="vector"):
    hits = retriever.search(question, k, mode)
    context = "\n\n---\n\n".join(h["text"] for h in hits)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"參考課程大綱：\n\n{context}\n\n問題：{question}"},
    ]
    reply = chat(messages)
    log_qa(question, mode, hits, reply)
    return reply, hits


def log_qa(question, mode, hits, reply):
    """每次問答都記錄下來，缺陷診斷寫 Verification Log 時直接查這個檔案。"""
    LOG_PATH.parent.mkdir(exist_ok=True)
    rec = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "mode": mode,
        "model": CHAT_MODEL,
        "retrieved": [{"id": h["id"], "name": h["name"], "teacher": h["teacher"],
                       "score": round(h["score"], 4)} for h in hits],
        "answer": reply,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build_index()
    elif len(sys.argv) >= 3 and sys.argv[1] == "ask":
        mode = sys.argv[3] if len(sys.argv) > 3 else "vector"
        reply, hits = answer(Retriever(), sys.argv[2], mode=mode)
        print("檢索到：", [f"{h['name']}（{h['teacher']}）" for h in hits])
        print("\n" + reply)
    else:
        print(__doc__)
