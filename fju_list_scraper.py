"""
輔大課程大綱系統：課程清單抓取程式
Fetches the course list from the FJU outline API and stores it in SQLite.

用法 Usage:
    pip install requests
    python fju_list_scraper.py --hy 115 --ht 1 --keyword 程式
    python fju_list_scraper.py --hy 115 --ht 1            # 不帶關鍵字（先測試 API 是否允許）

輸出 Output:
    data/list_<hy>_<ht>_<keyword>.json   原始 JSON
    courses.db                            SQLite 資料庫
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path

import requests

API_URL = "https://travellerlink.fju.edu.tw/Outline/api/OutlineQuery/OutlineStudentQuery"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Authorization": "Bearer null",
    "Origin": "https://outline.fju.edu.tw",
    "Referer": "https://outline.fju.edu.tw/",
    "User-Agent": "Mozilla/5.0 (FJU course project; educational use)",
}

SLEEP_SECONDS = 1.5  # 每次請求間隔，請勿調太低


def fetch_page(hy, ht, page, page_size, keyword=None, sco_typ="100"):
    params = {
        "hy": hy,
        "ht": ht,
        "scoTyp": sco_typ,
        "PageNumber": page,
        "PageSize": page_size,
        "lcid": 1028,
    }
    if keyword:
        params["couCNa"] = keyword
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("statusCode") != 200:
        raise RuntimeError(f"API error: {data.get('errorMessage')}")
    return data["result"]


def fetch_all(hy, ht, keyword=None, page_size=50, sco_typ="100"):
    first = fetch_page(hy, ht, 1, page_size, keyword, sco_typ)
    courses = list(first["result"])
    total_pages = first["totalPages"]
    print(f"共 {first['totalItemCount']} 筆，{total_pages} 頁")
    for page in range(2, total_pages + 1):
        time.sleep(SLEEP_SECONDS)
        print(f"  抓取第 {page}/{total_pages} 頁…")
        courses.extend(fetch_page(hy, ht, page, page_size, keyword, sco_typ)["result"])
    return courses


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    jonCouSn       INTEGER PRIMARY KEY,   -- 課程唯一編號（查大綱用）
    hy             INTEGER,               -- 學年
    ht             INTEGER,               -- 學期
    avaNO          TEXT,                  -- 開課代碼
    couCNa         TEXT,                  -- 中文課名
    couENa         TEXT,                  -- 英文課名
    tchNo          TEXT,
    tchCNa         TEXT,                  -- 教師
    credit         REAL,                  -- 學分
    reqSelCNa      TEXT,                  -- 必修 / 選修
    is_programming INTEGER                -- 官方分類是否為「程式設計類」(1/0)
);
CREATE TABLE IF NOT EXISTS schedules (
    jonCouSn INTEGER,
    couWek   TEXT,   -- 星期 1-7
    section  TEXT,   -- 節次，例如 D5,D6
    romNO    TEXT,   -- 教室
    FOREIGN KEY (jonCouSn) REFERENCES courses(jonCouSn)
);
CREATE TABLE IF NOT EXISTS classifies (
    jonCouSn        INTEGER,
    couClassifyNo   INTEGER,
    couClassifyCna  TEXT,
    FOREIGN KEY (jonCouSn) REFERENCES courses(jonCouSn)
);
"""


def save_to_db(courses, db_path="courses.db"):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for c in courses:
        sn = c["jonCouSn"]
        classifies = c.get("couClassifyList") or []
        is_prog = int(any(x.get("couClassifyCna") == "程式設計類" for x in classifies))
        conn.execute(
            "INSERT OR REPLACE INTO courses VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sn, c["hy"], c["ht"], c["avaNO"], c["couCNa"], c["couENa"],
             c["tchNo"], c["tchCNa"], c["credit"], c["reqSelCNa"], is_prog),
        )
        conn.execute("DELETE FROM schedules WHERE jonCouSn=?", (sn,))
        conn.execute("DELETE FROM classifies WHERE jonCouSn=?", (sn,))
        for s in c.get("seqList") or []:
            conn.execute("INSERT INTO schedules VALUES (?,?,?,?)",
                         (sn, s.get("couWek"), s.get("section"), s.get("romNO")))
        for x in classifies:
            conn.execute("INSERT INTO classifies VALUES (?,?,?)",
                         (sn, x.get("couClassifyNo"), x.get("couClassifyCna")))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
    conn.close()
    print(f"資料庫 {db_path} 目前共 {n} 門課")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hy", type=int, default=115, help="學年")
    p.add_argument("--ht", type=int, default=1, help="學期")
    p.add_argument("--keyword", default=None, help="課名關鍵字，例如 程式")
    p.add_argument("--scotyp", default="100", help="scoTyp 參數（意義待確認）")
    p.add_argument("--pagesize", type=int, default=50)
    p.add_argument("--db", default="courses.db")
    args = p.parse_args()

    courses = fetch_all(args.hy, args.ht, args.keyword, args.pagesize, args.scotyp)

    Path("data").mkdir(exist_ok=True)
    out = Path("data") / f"list_{args.hy}_{args.ht}_{args.keyword or 'all'}.json"
    out.write_text(json.dumps(courses, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"原始資料已存到 {out}")

    save_to_db(courses, args.db)


if __name__ == "__main__":
    main()
