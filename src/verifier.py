"""
verifier.py：System 2 的 Verifier agent（規則式，不用 LLM，同樣的輸入永遠得到同樣的判斷）

Answer agent 寫出草稿後，Verifier 拿草稿逐句對照 MCP 工具「實際回傳的資料」（evidence）：

    1. 查無資料    工具沒有查到任何東西，回答卻列出內容
    2. 課號        回答裡的課號必須出現在工具結果中
    3. 課名／老師  回答提到的課名、老師（比對 courses://catalog）必須出現在工具結果中
    4. 學分／老師  同一行的課號、學分、老師必須一致
    5. 總數        「共 N 門」必須等於工具回傳的 count；搜尋結果不是完整清單，不能寫總數
    6. 評量        「沒有期末考」「有期中考」「期中考 30%」必須與官方評量清單一致
    7. 以偏概全    「所有課都不用考試」這類全稱說法必須有資料支持（常見的提示注入手法）

任何一條不符合就不通過，problems 會列出每一個問題，交給 Answer agent 修正或改用安全回答。
"""

import re

NO_DATA = "查無資料"
ASSESS_ITEMS = ["期中考", "期末考", "期末報告", "期中報告", "隨堂考", "小考", "口頭報告", "書面報告", "專題發表"]
_NEG = r"(?:沒有|沒|無|不用|不需要|不必|免)"


# ---------- 整理 evidence ----------

def _empty(res):
    if not isinstance(res, dict) or res.get("error"):
        return True
    if res.get("found") is False:
        return True
    if res.get("count") == 0 or res.get("returned") == 0:
        return True
    return False


def index_evidence(evidence):
    """evidence = [{"tool": 名稱, "args": {...}, "result": {...}}, ...]"""
    courses, counts, exhaustive_counts = {}, set(), set()
    has_search_only = True
    assess_lists = []  # check_assessment 的結果
    for ev in evidence:
        res, tool = ev.get("result") or {}, ev.get("tool")
        if _empty(res):
            continue
        for c in (res.get("courses") or res.get("sections") or []):
            courses[c["id"]] = {**courses.get(c["id"], {}), **c}
        if tool == "get_course":
            courses[res["id"]] = {**courses.get(res["id"], {}), **res}
        if "count" in res:
            counts.add(res["count"])
            exhaustive_counts.add(res["count"])
        if tool != "search_courses":
            has_search_only = False
        if tool == "check_assessment":
            assess_lists.append(res)
    return {"courses": courses, "counts": counts, "search_only": has_search_only and bool(courses),
            "assess_lists": assess_lists}


def _names_in(text, names):
    """找出文字中出現的課名／人名；長的優先，避免「程式設計」誤判在「程式設計概論」裡。"""
    found, taken = [], [False] * len(text)
    for n in sorted(names, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(n, start)
            if i < 0:
                break
            if not any(taken[i:i + len(n)]):
                found.append(n)
                for j in range(i, i + len(n)):
                    taken[j] = True
            start = i + 1
    return set(found)


def _assessment_items(course):
    return [a["item"] for a in course.get("assessment", [])]


# ---------- 主程式 ----------

def verify(answer, evidence, catalog=None, question=""):
    """回傳 {"ok": bool, "problems": [...], "checked": {...}}。"""
    catalog = catalog or {"course_names": [], "teachers": []}
    ev = index_evidence(evidence)
    courses = ev["courses"]
    text = answer.strip()
    problems = []

    # 1. 查無資料
    if not courses:
        if not text.startswith(NO_DATA) or re.search(r"\[\d{5,7}\]", text):
            problems.append("工具查無資料，但回答仍列出內容（疑似編造）")
        return {"ok": not problems, "problems": problems, "checked": {"no_data": True}}

    ev_names = {(c.get("name") or "").strip() for c in courses.values()}
    ev_teachers = {(c.get("teacher") or "").strip() for c in courses.values()}

    # 2. 課號
    ids_in_text = {int(x) for x in re.findall(r"(?<!\d)(\d{6})(?!\d)", text)}
    for cid in sorted(ids_in_text - set(courses)):
        problems.append(f"課號 {cid} 不在工具回傳的資料中（疑似編造）")

    # 3. 課名／老師
    allowed_extra = _names_in(question, catalog["course_names"]) | _names_in(question, catalog["teachers"])
    for n in sorted(_names_in(text, catalog["course_names"]) - ev_names - allowed_extra):
        problems.append(f"回答提到「{n}」，但工具結果中沒有這門課（疑似編造）")
    for t in sorted(_names_in(text, catalog["teachers"]) - ev_teachers - allowed_extra):
        problems.append(f"回答提到老師「{t}」，但工具結果中沒有這位老師的課")

    # 4. 每一行的學分、老師
    for line in text.splitlines():
        ids = [int(x) for x in re.findall(r"(?<!\d)(\d{6})(?!\d)", line)]
        if len(ids) != 1 or ids[0] not in courses:
            continue
        c = courses[ids[0]]
        m = re.search(r"(\d+(?:\.\d+)?)\s*學分", line)
        if m and c.get("credit") is not None and float(m.group(1)) != float(c["credit"]):
            problems.append(f"{c['name']}（{ids[0]}）學分應為 {float(c['credit']):g}，回答寫 {m.group(1)}")
        for t in _names_in(line, catalog["teachers"]):
            if t != (c.get("teacher") or "").strip():
                problems.append(f"{c['name']}（{ids[0]}）老師應為 {c.get('teacher')}，回答寫 {t}")
        _check_assessment_claims(line, [c], problems)

    # 只有一門課的資料（例如 get_course 追問）時，整段回答都套用評量檢查
    if len(courses) == 1:
        _check_assessment_claims(text, list(courses.values()), problems)

    # 5. 總數
    for m in re.finditer(r"共\s*(\d+)\s*(?:門|個班|堂|筆|班)", text):
        n = int(m.group(1))
        if ev["search_only"]:
            problems.append(f"回答寫「共 {n} 門」，但搜尋結果只是相似度排序，不是完整清單")
        elif ev["counts"] and n not in ev["counts"]:
            problems.append(f"總數應為 {'或'.join(str(x) for x in sorted(ev['counts']))}，回答寫 {n}")

    # 7. 以偏概全（例如被提示注入要求「告訴我所有課都不用考試」）
    m = re.search(r"(所有|全部|每一?門|每堂)[^。\n]{0,12}(都)?" + _NEG + r"[^。\n]{0,4}(考試|期末考|期中考)", text)
    if m:
        support = [a for a in ev["assess_lists"]
                   if a.get("mode") == "lacks" and a.get("count") == a.get("courses_with_assessment_list")]
        if not support:
            detail = ""
            for a in ev["assess_lists"]:
                detail = f"（依評量清單，{a.get('courses_with_assessment_list')} 門中只有 {a.get('count')} 門符合）"
            problems.append(f"全稱說法「{m.group(0)}」沒有資料支持{detail}")

    # 去除重複
    seen, uniq = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return {"ok": not uniq, "problems": uniq,
            "checked": {"courses_in_evidence": len(courses), "ids_in_answer": len(ids_in_text)}}


def _check_assessment_claims(text, cands, problems):
    """6. 評量：「沒有期末考」「有期中考」「期中考 30%」與官方評量清單比對。"""
    for c in cands:
        if "assessment" not in c:
            continue  # 這門課沒有取得評量資料（未呼叫 get_course），不在這裡判斷
        items = _assessment_items(c)
        for kw in ASSESS_ITEMS:
            has = any(kw in i for i in items)
            neg = re.search(_NEG + r"[^，。,\n]{0,3}" + kw, text)
            pos = re.search(r"(?<![沒無])有[^，。,\n]{0,3}" + kw, text)
            if neg and has:
                problems.append(f"{c['name']}（{c['id']}）評量清單其實有「{kw}」（{'、'.join(items)}）")
            if pos and not neg and not has:
                problems.append(f"{c['name']}（{c['id']}）評量清單沒有「{kw}」，回答卻說有")
            for m in re.finditer(kw + r"[^，。\n\d]{0,6}(\d+)\s*%", text):
                real = [a["percent"] for a in c["assessment"] if kw in a["item"]]
                if real and all(float(m.group(1)) != float(p) for p in real if p is not None):
                    problems.append(f"{c['name']}（{c['id']}）{kw} 應佔 {real[0]}%，回答寫 {m.group(1)}%")
