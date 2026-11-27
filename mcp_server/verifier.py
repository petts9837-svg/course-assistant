"""
verifier.py：System 2 的 Verifier agent（最小版，不用 LLM，純程式比對）

Answer agent 產生回答時，要同時附上「結構化主張（claims）」；
Verifier 拿這些主張去對照 MCP 工具實際回傳的資料（evidence）。
任何一條對不上，整份回答就被擋下，改回「查無資料」或附上更正。

支援的主張：
    {"type": "course", "id": 741436, "credit": 2, "teacher": "許經典"}   課程資料是否屬實
    {"type": "total", "n": 90, "complete": true}                         宣稱「共 n 門、已列全部」是否屬實
    {"type": "lacks_assessment", "id": 123, "keyword": "期末考"}         宣稱該課沒有某評量是否屬實

另外會直接檢查回答「內文」（System 2 加上）：
    - 內文出現的課號 [123456] 都必須在工具回傳的資料裡（不能在文字裡偷加課）
    - 內文寫「共 N 門」時，N 必須等於工具回傳的總數；清單有 2 門以上時必須寫出總數
    - 工具有資料，內文卻說「沒有／查無資料」→ 攔截（否定條件失效）
    - 單一課程（get_course）：內文提到的星期、評量項目與比例必須和資料一致
"""
import re

NO_DATA = "查無資料"
_ID_RE = re.compile(r"\[(\d{6})\]")
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*(?:門|個班|班)")
_DENIAL_RE = re.compile(r"查無資料|沒有(?:任何)?(?:符合|相關|這類|這樣|不用寫程式|不需要寫程式)")
_WEEKDAY_RE = re.compile(r"(?:星期|週|禮拜)([一二三四五六日])")
_PCT_RE = re.compile(r"([\u4e00-\u9fff]{2,8})\s*(?:佔|占|為|：|:)?\s*(\d{1,3})\s*%")
_NOT_HAVE_RE = re.compile(r"沒有([\u4e00-\u9fff]{2,4}考)")
_WD = "一二三四五六日"


def _check_single_course(answer, c):
    """get_course 的回答：星期、評量比例、「沒有某考試」都要和資料一致。"""
    problems = []
    days = {s.get("weekday", "").strip() for s in c.get("schedules", [])}
    for w in _WEEKDAY_RE.findall(answer):
        if str(_WD.index(w) + 1) not in days:
            problems.append(f"內文說星期{w}上課，但資料的上課日是 {sorted(days)}")
    items = {a["item"]: a["percent"] for a in c.get("assessment", [])}
    for name, pct in _PCT_RE.findall(answer):
        hit = [k for k in items if name in k or k in name]
        if hit and all(float(items[k]) != float(pct) for k in hit):
            problems.append(f"內文寫{name} {pct}%，資料是 " + "、".join(f"{k} {items[k]:g}%" for k in hit))
    for exam in _NOT_HAVE_RE.findall(answer):
        if any(exam in k for k in items):
            problems.append(f"內文說沒有{exam}，但評量清單有「{exam}」")
    return problems


def _index(evidence):
    """把所有工具回傳整理成 {課號: 課程資料}，並記下最大的 count 與是否被截斷。"""
    courses, count, truncated = {}, None, False
    for ev in evidence:
        for c in (ev.get("courses") or ev.get("sections") or []):
            courses[c["id"]] = {**courses.get(c["id"], {}), **c}
        if ev.get("found") is not False and "id" in ev:        # get_course
            courses[ev["id"]] = {**courses.get(ev["id"], {}), **ev}
        if ev.get("mode") == "lacks" and ev.get("keyword"):    # check_assessment：清單本身就是證據
            for c in ev.get("courses") or []:
                courses[c["id"]].setdefault("lacks", set()).add(ev["keyword"])
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
            if c is not None and cl["keyword"] in c.get("lacks", set()):
                continue                                       # check_assessment 已證明
            if c is None or "assessment" not in c:
                problems.append(f"課號 {cl['id']} 沒有評量資料可佐證")
            elif any(cl["keyword"] in i for i in items):
                problems.append(f"{c['name']}（{cl['id']}）其實有「{cl['keyword']}」（評量：{'、'.join(items)}）")

    # 內文檢查：課號必須來自工具資料；「共 N 門」必須等於工具總數
    for cid in _ID_RE.findall(answer):
        if int(cid) not in courses:
            problems.append(f"內文提到課號 {cid}，但工具資料裡沒有（疑似編造）")
    m = _TOTAL_RE.search(answer)
    if m and count is not None and int(m.group(1)) != count:
        problems.append(f"內文寫共 {m.group(1)} 門，工具回傳總數是 {count}")
    if count is not None and count > 1 and not m:
        problems.append(f"工具找到 {count} 門，回答沒有交代總數（可能被當成完整清單）")
    if count and _DENIAL_RE.search(answer):
        problems.append(f"工具找到 {count} 門，回答卻說沒有／查無資料（否定條件失效）")
    singles = [ev for ev in evidence if ev.get("found") is True]
    for c in singles:
        problems += _check_single_course(answer, c)

    if problems:
        return {"ok": False, "answer": f"{NO_DATA}（回答未通過驗證，已攔截）", "problems": problems}
    return {"ok": True, "answer": answer, "problems": []}
