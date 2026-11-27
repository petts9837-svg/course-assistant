"""
course-sql：System 2 的第一個 MCP Server（唯讀、只查 courses.db）

為什麼要這個 Server？System 1 的向量檢索不懂「學分 ≥ 3」「星期五晚上」「沒有期末考」這種
結構化條件，會找不全或編造。這些問題交給 SQL 精準回答，並回報「總共幾門」，讓 Verifier
agent 可以核對清單有沒有漏。

規則：沒有符合的資料就回傳 {"count": 0, "note": "查無資料"}，不猜測、不推薦替代課程。

工具
    filter_courses     依學分、必選修、系所、老師、星期、晚上、語言、分類等條件篩選
    list_sections      列出同名課的所有班級（解決同名課學分不同的問題）
    get_course         用課號取得完整資訊（時間、評量項目、分類）
    check_assessment   依官方「學習評量」清單，找出有／沒有某項評量（例如期末考）的課
    match_terms        從一句話裡找出資料庫中真實存在的課名、老師、系所（給 Router 照抄參數用）

啟動（stdio）：
    pip install fastmcp
    python src/mcp_servers/course_sql_server.py
通常不需要手動啟動，由 Client（src/mcp_hub.py）自動啟動。

Resource
    courses://catalog  所有課名與教師名單（Verifier 用來偵測回答中「不在工具結果裡」的課名）
    courses://stats    資料庫筆數與學年期
資料庫路徑預設為專案根目錄的 courses.db，可用環境變數 COURSES_DB 覆蓋。
"""

import os
import re
import sqlite3
from pathlib import Path
from typing import Literal, Optional

from fastmcp import FastMCP

DB_PATH = Path(os.environ.get("COURSES_DB", Path(__file__).resolve().parents[2] / "courses.db"))
NO_DATA = "查無資料"
MAX_LIMIT = 100

mcp = FastMCP("course-sql")


def _conn():
    """唯讀連線：就算被要求，也無法修改資料庫。"""
    conn = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_SUMMARY_SQL = """
SELECT c.jonCouSn AS id, TRIM(c.couCNa) AS name, c.tchCNa AS teacher, c.credit,
       c.reqSelCNa AS type, f.dept_class AS dept, f.tea_lang AS language, f.day_type AS division,
       (SELECT group_concat(TRIM(s.couWek) || ':' || s.section, ' ')
          FROM schedules s WHERE s.jonCouSn = c.jonCouSn) AS schedule
FROM courses c
LEFT JOIN outline_fields f ON f.jonCouSn = c.jonCouSn
"""


def _rows(conn, where, params, limit):
    sql = _SUMMARY_SQL + (f" WHERE {where}" if where else "") + " ORDER BY name, c.jonCouSn LIMIT ?"
    return [dict(r) for r in conn.execute(sql, [*params, limit])]


def _count(conn, where, params):
    sql = ("SELECT COUNT(*) FROM courses c LEFT JOIN outline_fields f ON f.jonCouSn = c.jonCouSn"
           + (f" WHERE {where}" if where else ""))
    return conn.execute(sql, params).fetchone()[0]


def _result(conn, where, params, limit):
    limit = max(1, min(int(limit), MAX_LIMIT))
    total = _count(conn, where, params)
    courses = _rows(conn, where, params, limit)
    out = {"count": total, "returned": len(courses), "truncated": total > len(courses), "courses": courses}
    if total == 0:
        out["note"] = NO_DATA
    return out


@mcp.tool()
def filter_courses(
    name_contains: Optional[str] = None,
    teacher: Optional[str] = None,
    dept: Optional[str] = None,
    credit_min: Optional[float] = None,
    credit_max: Optional[float] = None,
    req_type: Optional[Literal["必修", "選修", "通識", "輔系"]] = None,
    weekday: Optional[int] = None,
    evening: Optional[bool] = None,
    language: Optional[str] = None,
    classify: Optional[str] = None,
    programming: Optional[bool] = None,
    limit: int = 30,
) -> dict:
    """依結構化條件篩選課程。所有條件都是選填，會以 AND 組合。

    回傳 count＝符合條件的「總門數」（不受 limit 影響），可用來核對清單是否完整；
    沒有符合時 count=0 且 note="查無資料"。

    Args:
        name_contains: 課名包含的字，例如「程式設計概論」。
        teacher: 老師姓名（包含比對）。
        dept: 開課班級／系所（包含比對），例如「資工」「企管二」。
        credit_min: 最低學分（含）。
        credit_max: 最高學分（含）。「1 學分的課」＝ credit_min=1, credit_max=1。
        req_type: 必修／選修／通識／輔系。
        weekday: 星期，1=週一 … 6=週六。
        evening: True 表示有晚間節次（節次含 E）；False 表示沒有。
        language: 授課語言，例如「全英文」「中英文」「中文」。
        classify: 官方分類，例如「程式設計類」「遠距教學類」「全英-專業學科類」。
        programming: 是否為官方「程式設計類」課程；False＝非程式課。
        limit: 最多回傳幾門（1–100），預設 30。
    """
    where, params = [], []
    if name_contains:
        where.append("c.couCNa LIKE ?"); params.append(f"%{name_contains}%")
    if teacher:
        where.append("c.tchCNa LIKE ?"); params.append(f"%{teacher}%")
    if dept:
        where.append("f.dept_class LIKE ?"); params.append(f"%{dept}%")
    if credit_min is not None:
        where.append("c.credit >= ?"); params.append(credit_min)
    if credit_max is not None:
        where.append("c.credit <= ?"); params.append(credit_max)
    if req_type:
        where.append("c.reqSelCNa = ?"); params.append(req_type)
    if language:
        where.append("f.tea_lang = ?"); params.append(language)
    if classify:
        where.append("c.jonCouSn IN (SELECT jonCouSn FROM classifies WHERE couClassifyCna = ?)")
        params.append(classify)
    if programming is not None:
        where.append("c.is_programming = ?"); params.append(1 if programming else 0)
    if weekday is not None or evening is not None:
        cond = ["s.jonCouSn = c.jonCouSn"]
        if weekday is not None:
            cond.append("TRIM(s.couWek) = ?"); params.append(str(weekday))
        if evening is True:
            cond.append("s.section LIKE '%E%'")
        # 同一個時段同時滿足「星期」與「晚上」，所以放在同一個 EXISTS 裡
        if evening is False:
            where.append("NOT EXISTS (SELECT 1 FROM schedules s2 WHERE s2.jonCouSn = c.jonCouSn AND s2.section LIKE '%E%')")
        if len(cond) > 1:
            where.append("EXISTS (SELECT 1 FROM schedules s WHERE " + " AND ".join(cond) + ")")
    with _conn() as conn:
        return _result(conn, " AND ".join(where), params, limit)


@mcp.tool()
def list_sections(name: str) -> dict:
    """列出同名課程的所有班級，並統計各種學分的班數。

    同名課常有多個班級、不同老師與學分（例如「程式設計概論」有 9 個班，2 學分與 3 學分並存），
    回答「這門課幾學分」之前應先呼叫這個工具。先精確比對課名，查不到再用包含比對。

    Args:
        name: 課名，例如「程式設計概論」。
    """
    with _conn() as conn:
        where, params = "TRIM(c.couCNa) = ?", [name.strip()]
        if _count(conn, where, params) == 0:
            where, params = "c.couCNa LIKE ?", [f"%{name.strip()}%"]
        out = _result(conn, where, params, MAX_LIMIT)
        credits = {}
        for c in out["courses"]:
            key = f"{c['credit']:g}"
            credits[key] = credits.get(key, 0) + 1
        out["credits"] = credits
        out["sections"] = out.pop("courses")
        return out


@mcp.tool()
def get_course(course_id: int) -> dict:
    """用課號取得單一課程的完整資訊：基本資料、上課時間與教室、評量項目與比例、分類。

    課號就是其他工具回傳的 id。追問（例如「那它星期幾上課？」）時，請用上一輪的課號呼叫，
    不要重新用文字搜尋。

    Args:
        course_id: 課號（jonCouSn）。
    """
    with _conn() as conn:
        rows = _rows(conn, "c.jonCouSn = ?", [course_id], 1)
        if not rows:
            return {"found": False, "note": NO_DATA}
        course = rows[0]
        course["schedules"] = [
            {"weekday": r["couWek"].strip(), "section": r["section"], "room": r["romNO"]}
            for r in conn.execute("SELECT couWek, section, romNO FROM schedules WHERE jonCouSn = ?", [course_id])
        ]
        course["assessment"] = [
            {"item": r["method_name"], "percent": r["percent"]}
            for r in conn.execute(
                "SELECT method_name, percent FROM methods WHERE jonCouSn = ? AND mName = '學習評量'", [course_id])
        ]
        course["teaching_methods"] = [
            r["method_name"] for r in conn.execute(
                "SELECT method_name FROM methods WHERE jonCouSn = ? AND mName = '教學方法'", [course_id])
        ]
        course["classifications"] = [
            r["couClassifyCna"] for r in conn.execute(
                "SELECT couClassifyCna FROM classifies WHERE jonCouSn = ?", [course_id])
        ]
        obj = conn.execute("SELECT objective FROM outline_fields WHERE jonCouSn = ?", [course_id]).fetchone()
        course["objective"] = (obj["objective"] or "")[:300] if obj else ""
        course["found"] = True
        return course


@mcp.tool()
def check_assessment(
    keyword: str = "期末考",
    mode: Literal["lacks", "has"] = "lacks",
    limit: int = 30,
) -> dict:
    """依官方結構化的「學習評量」清單，找出「有」或「沒有」某項評量的課程。

    例如 keyword="期末考", mode="lacks"：評量清單裡沒有列出期末考的課。
    注意：這是依評量清單判斷，「沒列出」不代表老師一定不會考，回答時請照實說明依據。

    Args:
        keyword: 評量項目關鍵字，例如「期末考」「期中考」「期末報告」「口頭報告」。
        mode: "lacks"＝清單裡沒有；"has"＝清單裡有。
        limit: 最多回傳幾門（1–100）。
    """
    has_sql = ("c.jonCouSn IN (SELECT jonCouSn FROM methods WHERE mName = '學習評量' AND method_name LIKE ?)")
    # 只比較「有評量清單」的課，避免把沒資料的課誤判成沒有期末考
    listed = "c.jonCouSn IN (SELECT jonCouSn FROM methods WHERE mName = '學習評量')"
    where = f"{listed} AND " + (has_sql if mode == "has" else "NOT " + has_sql)
    with _conn() as conn:
        out = _result(conn, where, [f"%{keyword}%"], limit)
        out["keyword"], out["mode"] = keyword, mode
        out["courses_with_assessment_list"] = _count(conn, listed, [])
        out["basis"] = "官方『學習評量』清單；清單沒列出不代表一定沒有該項評量"
        return out


_DEPT_SUFFIX = re.compile(r"(學士後系|學程系|學程|系)?[一二三四五六七八九甲乙丙丁戊A-Za-z0-9]*$")


def _longest_only(names):
    """只保留最長的比對結果：同時比對到「程式設計」和「程式設計概論」時只留後者。"""
    names = sorted(set(names), key=len, reverse=True)
    return [n for i, n in enumerate(names) if not any(n in m for m in names[:i])]


@mcp.tool()
def match_terms(text: str) -> dict:
    """從一句話中找出資料庫裡「真實存在」的課名、老師姓名與系所名稱。

    Router 填工具參數前先呼叫它，參數就能直接照抄正確名稱，避免模型把中文名稱寫錯或編造。

    Args:
        text: 使用者的問題原文。
    """
    with _conn() as conn:
        courses = [r[0] for r in conn.execute(
            "SELECT DISTINCT TRIM(couCNa) FROM courses WHERE length(TRIM(couCNa)) >= 2 AND instr(?, TRIM(couCNa)) > 0",
            [text])]
        teachers = [r[0] for r in conn.execute(
            "SELECT DISTINCT tchCNa FROM courses WHERE length(tchCNa) >= 2 AND instr(?, tchCNa) > 0", [text])]
        depts = set()
        for (d,) in conn.execute("SELECT DISTINCT dept_class FROM outline_fields WHERE dept_class IS NOT NULL"):
            full, stem = d.strip(), _DEPT_SUFFIX.sub("", d.strip())
            if len(full) >= 3 and full in text:
                depts.add(full)               # 完整班級，例如「資工一乙」
            elif len(stem) >= 2 and stem in text:
                depts.add(stem)               # 系所簡稱，例如「資工」
    return {"courses": _longest_only(courses), "teachers": _longest_only(teachers), "depts": _longest_only(depts)}


@mcp.resource("courses://catalog", mime_type="application/json")
def catalog() -> dict:
    """所有課名、教師與開課班級名單。Verifier 用它辨認回答中提到的課名／老師是否出現在工具結果裡。"""
    with _conn() as conn:
        names = sorted({r[0] for r in conn.execute("SELECT DISTINCT TRIM(couCNa) FROM courses") if r[0]})
        teachers = sorted({r[0] for r in conn.execute("SELECT DISTINCT TRIM(tchCNa) FROM courses") if r[0]})
        depts = sorted({r[0] for r in conn.execute("SELECT DISTINCT TRIM(dept_class) FROM outline_fields") if r[0]})
    return {"course_names": names, "teachers": teachers, "dept_classes": depts}


@mcp.resource("courses://stats", mime_type="application/json")
def stats() -> dict:
    """資料庫概況：課程數、有大綱的課程數、學年期。"""
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
        outlines = conn.execute("SELECT COUNT(*) FROM outlines WHERE text IS NOT NULL AND text != ''").fetchone()[0]
        terms = [f"{r[0]}-{r[1]}" for r in conn.execute("SELECT DISTINCT hy, ht FROM courses")]
    return {"courses": n, "outlines": outlines, "terms": terms}


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
