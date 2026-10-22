"""
輔大課程大綱系統：大綱內容抓取（JSON 版，不需登入）
Fetches course outline details (JSON) for courses already in courses.db.

先執行 fju_list_scraper.py 建好 courses.db，再執行本程式。

用法 Usage:
    pip install requests
    python fju_outline_fetcher.py --dump 741436     # 先看一門課的原始回應長什麼樣子
    python fju_outline_fetcher.py --limit 5         # 抓 5 門測試
    python fju_outline_fetcher.py                   # 抓 courses.db 裡全部課程
    python fju_outline_fetcher.py --show 741436     # 看一門課整理後的全文
    python fju_outline_fetcher.py --rebuild         # 不連網，重建整理後的資料表

資料表:
    outline_raw    每支 API 的原始 JSON
    outlines       整理過的大綱全文（RAG 用）
    outline_fields 評分方式、課程目標、班級等單一欄位
    methods        教學 / 評量方法與百分比（只存 > 0%）
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


def clean(s) -> str:
    if not s:
        return ""
    s = html.unescape(TAG_RE.sub(" ", str(s)))  # 大綱內容可能含 HTML 標籤
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outline_raw (
            jonCouSn INTEGER,
            endpoint TEXT,
            json     TEXT,
            PRIMARY KEY (jonCouSn, endpoint)
        );
        -- 整理過的大綱全文（RAG 用）
        CREATE TABLE IF NOT EXISTS outlines (
            jonCouSn INTEGER PRIMARY KEY,
            text     TEXT
        );
        -- 重要的單一欄位（SQL 篩選、驗證 agent 查證用）
        CREATE TABLE IF NOT EXISTS outline_fields (
            jonCouSn   INTEGER PRIMARY KEY,
            day_type   TEXT,   -- 日間部 / 進修部
            dept_class TEXT,   -- 開課班級，例如 大傳學程二甲
            tea_lang   TEXT,   -- 授課語言
            objective  TEXT,   -- 課程目標
            content    TEXT,   -- 課程內容
            norms      TEXT,   -- 評分方式（自由文字，例如 期中考25% 期末考25%…）
            book       TEXT,
            ref_book   TEXT,
            pre_course TEXT
        );
        -- 教學 / 評量方法，只存 percent > 0 的項目
        CREATE TABLE IF NOT EXISTS methods (
            jonCouSn    INTEGER,
            mType       INTEGER,
            mName       TEXT,     -- 方法類別，例如 教學方法
            method_name TEXT,     -- 例如 講述、實作教學
            percent     REAL
        );
    """)


def _result(raw, endpoint):
    data = raw.get(endpoint) or {}
    return data.get("result")


def build_records(sn, raw):
    """raw: {endpoint: json}。回傳 (全文, fields tuple, methods list)。"""
    d = _result(raw, "CourseDetailsData") or {}
    ib = _result(raw, "CourseInfoAndBook") or {}
    cp = _result(raw, "CourseCP") or {}
    rel = _result(raw, "CourseRelations") or []
    mth = _result(raw, "CourseMethods") or []

    methods = []
    for group in mth:
        for m in group.get("methodsDetails") or []:
            if (m.get("percent") or 0) > 0:  # 0% 的項目是雜訊，丟掉
                methods.append((sn, group.get("mType"), group.get("mName"),
                                m.get("methodName"), m.get("percent")))

    fields = (sn, d.get("dayCNa"), d.get("dptGrdCN"), d.get("teaLangCNa"),
              clean(ib.get("obj")), clean(ib.get("cm")), clean(ib.get("norms")),
              clean(ib.get("book")), clean(ib.get("refBook")), clean(ib.get("preCourse")))

    lines = [
        f"課程名稱：{d.get('couCNa', '')}（{d.get('couENa', '')}）",
        f"授課教師：{d.get('tchCNa', '')}",
        f"學分：{d.get('credit', '')}　{d.get('reqSelCNa', '')}",
        f"開課班級：{d.get('dayCNa', '')} {d.get('dptGrdCN', '')}",
        f"上課時間：{'；'.join(d.get('seqList') or [])}",
        f"授課語言：{d.get('teaLangCNa', '')}",
    ]
    for label, value in [("課程目標", fields[4]), ("先修課程", fields[9]),
                         ("課程內容", fields[5]), ("評分方式", fields[6]),
                         ("教科書", fields[7]), ("參考書", fields[8])]:
        if value:
            lines.append(f"{label}：\n{value}")

    by_type = {}
    for _, _, mname, name, pct in methods:
        by_type.setdefault(mname, []).append(f"{name} {pct:g}%")
    for mname, items in by_type.items():
        lines.append(f"{mname}：{'、'.join(items)}")

    weeks = []
    for w in cp.get("weeklyCP") or []:
        theme = clean(w.get("theme")).replace("\n", " ")
        if theme:
            weeks.append(f"第{w.get('cweek')}週 {theme}")
    if weeks:
        lines.append("每週進度：\n" + "\n".join(weeks))

    cores = [r.get("itemName") for r in rel if (r.get("relation") or 0) > 0]
    if cores:
        lines.append(f"對應核心能力：{'、'.join(cores)}")

    return "\n".join(lines), fields, methods


def save_records(conn, sn, raw):
    text, fields, methods = build_records(sn, raw)
    conn.execute("INSERT OR REPLACE INTO outlines VALUES (?,?)", (sn, text))
    conn.execute("INSERT OR REPLACE INTO outline_fields VALUES (?,?,?,?,?,?,?,?,?,?)", fields)
    conn.execute("DELETE FROM methods WHERE jonCouSn=?", (sn,))
    conn.executemany("INSERT INTO methods VALUES (?,?,?,?,?)", methods)


def fetch_course(conn, sn, tch_no):
    raw = {}
    for endpoint, needs_tch in ENDPOINTS:
        data = fetch_endpoint(endpoint, sn, tch_no, needs_tch)
        raw[endpoint] = data
        conn.execute("INSERT OR REPLACE INTO outline_raw VALUES (?,?,?)",
                     (sn, endpoint, json.dumps(data, ensure_ascii=False)))
        time.sleep(SLEEP_SECONDS)
    save_records(conn, sn, raw)
    conn.commit()


def rebuild(conn):
    """不重新連線，直接用 outline_raw 重建整理後的資料表（改了整理邏輯時用）。"""
    raws = {}
    for sn, endpoint, js in conn.execute("SELECT jonCouSn, endpoint, json FROM outline_raw"):
        raws.setdefault(sn, {})[endpoint] = json.loads(js)
    for sn, raw in raws.items():
        save_records(conn, sn, raw)
    conn.commit()
    print(f"已重建 {len(raws)} 門課的整理資料")


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
    p.add_argument("--rebuild", action="store_true", help="不連網，用已抓的原始資料重建整理後的資料表")
    p.add_argument("--show", type=int, default=None, help="印出一門課整理後的全文")
    args = p.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"找不到 {args.db}，請先執行 fju_list_scraper.py")
    conn = sqlite3.connect(args.db)
    init_tables(conn)

    if args.dump:
        dump(conn, args.dump)
        return
    if args.rebuild:
        rebuild(conn)
        return
    if args.show:
        row = conn.execute("SELECT text FROM outlines WHERE jonCouSn=?", (args.show,)).fetchone()
        print(row[0] if row else f"{args.show} 還沒抓")
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
