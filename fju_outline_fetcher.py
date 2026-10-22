"""
輔大課程大綱系統：大綱內容抓取（JSON 版，不需登入）
Fetches course outline details (JSON) for courses already in courses.db.

先執行 fju_list_scraper.py 建好 courses.db，再執行本程式。

用法 Usage:
    pip install requests
    python fju_outline_fetcher.py --dump 741436     # 先看一門課的原始回應長什麼樣子
    python fju_outline_fetcher.py --limit 5         # 抓 5 門測試
    python fju_outline_fetcher.py                   # 抓 courses.db 裡全部課程

資料表:
    outline_raw (jonCouSn, endpoint, json)   每支 API 的原始 JSON（之後抽評量比例用）
    outlines    (jonCouSn, text)             攤平後的大綱全文（RAG 用）
"""

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = "https://travellerlink.fju.edu.tw/Outline/api/OutlineMaintain/"
# (endpoint, 是否需要 tchNo)
ENDPOINTS = [
    ("CourseDetailsData", False),
    ("CourseRelations", True),
    ("CourseInfoAndBook", True),
    ("CourseCP", True),
    ("CourseMethods", True),
]
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Authorization": "Bearer null",
    "Origin": "https://outline.fju.edu.tw",
    "Referer": "https://outline.fju.edu.tw/",
    "User-Agent": "Mozilla/5.0 (FJU course project; educational use)",
}
SLEEP_SECONDS = 1.0  # 每次請求間隔（每門課 5 次請求），請勿調太低


def fetch_endpoint(endpoint, sn, tch_no, needs_tch):
    params = {"jonCouSn": sn, "lcid": 1028, "fromStu": "true"}
    if needs_tch:
        params["tchNo"] = tch_no
    resp = requests.get(BASE + endpoint, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


TAG_RE = re.compile(r"<[^>]+>")
SKIP_KEYS = {"statusCode", "message", "errorMessage"}


def clean(s: str) -> str:
    s = html.unescape(TAG_RE.sub(" ", s))  # 大綱內容常含 HTML 標籤
    return re.sub(r"\s+", " ", s).strip()


def flatten_text(obj, key=""):
    """把 JSON 裡所有有意義的文字攤平成 'key: value' 行。"""
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in SKIP_KEYS:
                lines += flatten_text(v, k)
    elif isinstance(obj, list):
        for v in obj:
            lines += flatten_text(v, key)
    elif isinstance(obj, str):
        t = clean(obj)
        # 跳過空值、單一代碼字元、純數字 ID
        if len(t) > 1 and not t.isdigit():
            lines.append(f"{key}: {t}")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        # 保留百分比、配分之類的數字欄位，ID 類欄位略過
        if not key.lower().endswith(("sn", "no", "id", "order", "group")):
            lines.append(f"{key}: {obj}")
    return lines


def init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outline_raw (
            jonCouSn INTEGER,
            endpoint TEXT,
            json     TEXT,
            PRIMARY KEY (jonCouSn, endpoint)
        );
        CREATE TABLE IF NOT EXISTS outlines (
            jonCouSn INTEGER PRIMARY KEY,
            text     TEXT
        );
    """)


def fetch_course(conn, sn, tch_no):
    texts = []
    for endpoint, needs_tch in ENDPOINTS:
        data = fetch_endpoint(endpoint, sn, tch_no, needs_tch)
        conn.execute("INSERT OR REPLACE INTO outline_raw VALUES (?,?,?)",
                     (sn, endpoint, json.dumps(data, ensure_ascii=False)))
        body = flatten_text(data)
        if body:
            texts.append(f"## {endpoint}\n" + "\n".join(body))
        time.sleep(SLEEP_SECONDS)
    conn.execute("INSERT OR REPLACE INTO outlines VALUES (?,?)", (sn, "\n\n".join(texts)))
    conn.commit()


def dump(conn, sn):
    row = conn.execute("SELECT tchNo, couCNa FROM courses WHERE jonCouSn=?", (sn,)).fetchone()
    if not row:
        sys.exit(f"courses.db 裡找不到 {sn}")
    print(f"=== {sn} {row[1]} ===")
    for endpoint, needs_tch in ENDPOINTS:
        data = fetch_endpoint(endpoint, sn, row[0], needs_tch)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        print(f"\n----- {endpoint} -----")
        print(text[:3000] + ("\n…（以下省略）" if len(text) > 3000 else ""))
        time.sleep(SLEEP_SECONDS)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="courses.db")
    p.add_argument("--limit", type=int, default=None, help="只抓前 N 門（測試用）")
    p.add_argument("--dump", type=int, default=None, help="印出一門課的原始回應")
    p.add_argument("--refetch", action="store_true", help="已抓過的課也重抓")
    args = p.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"找不到 {args.db}，請先執行 fju_list_scraper.py")
    conn = sqlite3.connect(args.db)
    init_tables(conn)

    if args.dump:
        dump(conn, args.dump)
        return

    done = set() if args.refetch else {r[0] for r in conn.execute("SELECT jonCouSn FROM outlines")}
    todo = [(sn, t) for sn, t in conn.execute("SELECT jonCouSn, tchNo FROM courses ORDER BY jonCouSn")
            if sn not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待抓 {len(todo)} 門（已完成 {len(done)} 門會跳過）")
    for i, (sn, tch_no) in enumerate(todo, 1):
        try:
            fetch_course(conn, sn, tch_no)
            print(f"[{i}/{len(todo)}] {sn} ok")
        except (requests.RequestException, ValueError) as e:
            print(f"[{i}/{len(todo)}] {sn} 失敗：{e}")
    conn.close()


if __name__ == "__main__":
    main()
