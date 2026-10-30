"""
verifier.py：System 2 的 Verifier agent（最小版，不用 LLM，純程式比對）

Answer agent 產生回答時，要同時附上「結構化主張（claims）」；
Verifier 拿這些主張去對照 MCP 工具實際回傳的資料（evidence）。
任何一條對不上，整份回答就被擋下，改回「查無資料」或附上更正。

支援的主張：
    {"type": "course", "id": 741436, "credit": 2, "teacher": "許經典"}   課程資料是否屬實
    {"type": "total", "n": 90, "complete": true}                         宣稱「共 n 門、已列全部」是否屬實
    {"type": "lacks_assessment", "id": 123, "keyword": "期末考"}         宣稱該課沒有某評量是否屬實
"""
NO_DATA = "查無資料"


def _index(evidence):
    """把所有工具回傳整理成 {課號: 課程資料}，並記下最大的 count 與是否被截斷。"""
    courses, count, truncated = {}, None, False
    for ev in evidence:
        for c in (ev.get("courses") or ev.get("sections") or []):
            courses[c["id"]] = {**courses.get(c["id"], {}), **c}
        if ev.get("found") is not False and "id" in ev:        # get_course
            courses[ev["id"]] = {**courses.get(ev["id"], {}), **ev}
        if "count" in ev:
            count, truncated = ev["count"], ev.get("truncated", False)
    return courses, count, truncated


def verify(answer, claims, evidence):
    """回傳 {"ok": bool, "answer": 最終要顯示的文字, "problems": [...]}。"""
    courses, count, truncated = _index(evidence)
    problems = []

    if count == 0 or not courses:
        # 工具沒查到任何東西：只允許回「查無資料」
        if answer.strip() != NO_DATA:
            problems.append("工具查無資料，但回答仍列出內容（疑似編造）")
        return {"ok": not problems, "answer": NO_DATA, "problems": problems}

    for cl in claims:
        t = cl["type"]
        if t == "course":
            c = courses.get(cl["id"])
            if not c:
                problems.append(f"課號 {cl['id']} 不在工具回傳的資料中（疑似編造）")
                continue
            if "credit" in cl and float(cl["credit"]) != float(c["credit"]):
                problems.append(f"{c['name']}（{cl['id']}）學分應為 {c['credit']:g}，回答寫 {cl['credit']}")
            if "teacher" in cl and cl["teacher"] != c.get("teacher"):
                problems.append(f"{c['name']}（{cl['id']}）老師應為 {c.get('teacher')}，回答寫 {cl['teacher']}")
        elif t == "total":
            if count is not None and cl["n"] != count:
                problems.append(f"總數應為 {count}，回答寫 {cl['n']}")
            elif cl.get("complete") and truncated:
                problems.append(f"工具只回傳部分結果（共 {count} 門），回答卻當成完整清單")
        elif t == "lacks_assessment":
            c = courses.get(cl["id"])
            items = [a["item"] for a in (c or {}).get("assessment", [])]
            if c is None or "assessment" not in c:
                problems.append(f"課號 {cl['id']} 沒有評量資料可佐證")
            elif any(cl["keyword"] in i for i in items):
                problems.append(f"{c['name']}（{cl['id']}）其實有「{cl['keyword']}」（評量：{'、'.join(items)}）")

    if problems:
        return {"ok": False, "answer": f"{NO_DATA}（回答未通過驗證，已攔截）", "problems": problems}
    return {"ok": True, "answer": answer, "problems": []}
