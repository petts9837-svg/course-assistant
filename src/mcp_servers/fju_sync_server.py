"""
fju-sync：System 2 的第三個 MCP Server（資料新鮮度：檢查、更新 courses.db）

為什麼要這個 Server？其他 Server 回答得再準，資料本身過期（老師改了評分比例、課程異動）
答案還是錯的。fju-sync 把「資料從哪來、多新、和官網是否一致」也變成可以呼叫、可以驗證的工具。

工具
    sync_status              資料庫筆數、學年期、最後更新時間、缺大綱的課、向量索引是否需要重建
    check_outline_updates    向輔大課程大綱 API 重新抓一門課，和資料庫比對（唯讀，不寫入）
    refresh_course           重新抓一門課並寫入資料庫；必須 confirm=true 才會寫入

規則
    - 只有 refresh_course(confirm=true) 會寫資料庫；其他都是唯讀。
    - 每次請求間隔 1 秒以上（沿用 fju_outline_fetcher.py 的設定），一門課約 5 次請求。
    - Router agent 看不到這些工具（學生問問題時不會觸發抓取），由介面的「資料同步」區塊或管理者呼叫。

啟動（stdio）：
    python src/mcp_servers/fju_sync_server.py
"""

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import fju_outline_fetcher as fetcher  # noqa: E402  沿用期中的爬蟲（同一套 API、同一套整理邏輯）

DB_PATH = Path(os.environ.get("COURSES_DB", ROOT / "courses.db"))
INDEX_PATH = Path(os.environ.get("VECTOR_INDEX", ROOT / "data" / "vector_index.npz"))
NO_DATA = "查無資料"

mcp = FastMCP("fju-sync")


def _ro():
    return sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)


def _digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _snapshot(conn, sn):
    """資料庫裡這門課目前的內容：大綱全文、評量清單、上課時間。"""
    row = conn.execute("SELECT text FROM outlines WHERE jonCouSn = ?", [sn]).fetchone()
    assess = [(r[0], r[1]) for r in conn.execute(
        "SELECT method_name, percent FROM methods WHERE jonCouSn = ? AND mName = '學習評量' ORDER BY method_name", [sn])]
    return {"text": row[0] if row else None, "assessment": assess}


def _fetch_live(sn, tch_no):
    raw = {}
    for endpoint, needs_tch in fetcher.ENDPOINTS:
        raw[endpoint] = fetcher.fetch_endpoint(endpoint, sn, tch_no, needs_tch)
        fetcher.time.sleep(fetcher.SLEEP_SECONDS)
    return raw


def _live_view(sn, raw):
    text, fields, methods = fetcher.build_records(sn, raw)
    assess = sorted((m[3], m[4]) for m in methods if m[2] == "學習評量")
    return {"text": text, "assessment": assess}


@mcp.tool()
def sync_status() -> dict:
    """回報資料的新鮮度：課程數、學年期、資料庫最後更新時間、沒有大綱的課、向量索引涵蓋幾門、是否需要重建索引。"""
    with _ro() as conn:
        n = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
        terms = [f"{a}-{b}" for a, b in conn.execute("SELECT DISTINCT hy, ht FROM courses")]
        missing = [r[0] for r in conn.execute(
            "SELECT jonCouSn FROM courses WHERE jonCouSn NOT IN "
            "(SELECT jonCouSn FROM outlines WHERE text IS NOT NULL AND text != '')")]
    indexed, index_time = 0, None
    if INDEX_PATH.exists():
        import numpy as np
        indexed = len(np.load(INDEX_PATH)["ids"])
        index_time = datetime.fromtimestamp(INDEX_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    db_time = datetime.fromtimestamp(DB_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    stale = indexed < n - len(missing) or (index_time is not None and index_time < db_time)
    return {"courses": n, "terms": terms, "db_updated": db_time,
            "missing_outlines": len(missing), "missing_ids": missing[:20],
            "vector_indexed": indexed, "index_updated": index_time,
            "index_needs_rebuild": stale,
            "hint": "執行 python rag_core.py build 重建向量索引" if stale else None}


@mcp.tool()
def check_outline_updates(course_id: int) -> dict:
    """向輔大課程大綱系統重新抓取一門課（約 5 秒），和資料庫裡的版本比對；只讀不寫。

    回傳 changed（是否有差異）、assessment_before / assessment_now（學習評量清單前後）與全文是否相同。
    要把新版本寫進資料庫，請再呼叫 refresh_course(course_id, confirm=true)。

    Args:
        course_id: 課號（jonCouSn）。
    """
    with _ro() as conn:
        row = conn.execute("SELECT tchNo, TRIM(couCNa) FROM courses WHERE jonCouSn = ?", [course_id]).fetchone()
        if not row:
            return {"found": False, "note": NO_DATA}
        before = _snapshot(conn, course_id)
    try:
        now = _live_view(course_id, _fetch_live(course_id, row[0]))
    except Exception as e:
        return {"found": True, "error": "無法連線輔大課程大綱系統，請確認網路", "detail": str(e)[:200]}
    text_same = _digest(before["text"]) == _digest(now["text"])
    assess_same = before["assessment"] == now["assessment"]
    return {"found": True, "id": course_id, "name": row[1], "changed": not (text_same and assess_same),
            "outline_text_same": text_same, "assessment_same": assess_same,
            "assessment_before": [f"{a} {p:g}%" for a, p in before["assessment"]],
            "assessment_now": [f"{a} {p:g}%" for a, p in now["assessment"]],
            "checked_at": datetime.now().isoformat(timespec="seconds")}


@mcp.tool()
def refresh_course(course_id: int, confirm: bool = False) -> dict:
    """重新抓取一門課的大綱並寫入資料庫（含原始 JSON、整理後全文、評量清單）。

    會修改資料庫，所以 confirm 必須是 true 才會寫入；confirm=false 只回報會做什麼。
    寫入後向量索引會過期，sync_status 會提示重建。

    Args:
        course_id: 課號（jonCouSn）。
        confirm: true 才真的寫入。
    """
    with _ro() as conn:
        row = conn.execute("SELECT tchNo, TRIM(couCNa) FROM courses WHERE jonCouSn = ?", [course_id]).fetchone()
    if not row:
        return {"found": False, "note": NO_DATA}
    if not confirm:
        return {"found": True, "id": course_id, "name": row[1], "written": False,
                "note": "尚未寫入：確認後請以 confirm=true 再呼叫一次"}
    try:
        raw = _fetch_live(course_id, row[0])
    except Exception as e:
        return {"found": True, "written": False, "error": "無法連線輔大課程大綱系統，請確認網路", "detail": str(e)[:200]}
    conn = sqlite3.connect(DB_PATH)
    try:
        fetcher.init_tables(conn)
        for endpoint, data in raw.items():
            conn.execute("INSERT OR REPLACE INTO outline_raw VALUES (?,?,?)",
                         (course_id, endpoint, json.dumps(data, ensure_ascii=False)))
        fetcher.save_records(conn, course_id, raw)
        conn.commit()
        after = _snapshot(conn, course_id)
    finally:
        conn.close()
    return {"found": True, "id": course_id, "name": row[1], "written": True,
            "assessment_now": [f"{a} {p:g}%" for a, p in after["assessment"]],
            "hint": "資料已更新；記得執行 python rag_core.py build 重建向量索引"}


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
