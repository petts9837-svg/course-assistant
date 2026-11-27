"""
System 2 agent：Router → MCP 工具 → Answer agent → Verifier（→ 修正 → 保底）

流程
    1. Router agent（LLM tool calling）：看問題與上一輪提到的課，選一個 course-sql MCP 工具與參數
    2. MCP Client：透過 stdio 呼叫 mcp_server/course_sql_server.py，拿回精確資料與「總門數」
    3. Answer agent（LLM, temperature 0）：只能根據工具資料寫回答，並附上結構化主張（claims）
    4. Verifier（mcp_server/verifier.py，不用 LLM）：逐條核對 claims 與內文
       - 通過 → 送出
       - 不通過 → 把問題回饋給 Answer agent 重寫一次
       - 還是不通過 → 改用「直接由工具資料產生」的保底回答（不經 LLM，不可能編造）

需要的環境變數：CF_ACCOUNT_ID、CF_API_TOKEN（與 System 1 相同）
命令列測試：python system2/agent.py "推薦 3 學分以上的選修課"
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))
from verifier import NO_DATA, verify  # noqa: E402

SERVER = str(ROOT / "mcp_server" / "course_sql_server.py")
CHAT_MODEL = os.environ.get("CF_CHAT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
LOG_PATH = ROOT / "data" / "qa_log_system2.jsonl"
MAX_LISTED = 10        # 回答最多列幾門，其餘以「共 N 門」交代
WEEKDAYS = "一二三四五六日"


# ---------------------------------------------------------------- Cloudflare

def _cf_run(payload, retries=3):
    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not account or not token:
        raise RuntimeError("請先設定環境變數 CF_ACCOUNT_ID 和 CF_API_TOKEN")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CHAT_MODEL}"
    for attempt in range(retries):
        resp = requests.post(url, json=payload, timeout=120, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 429 and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
            continue
        if not resp.ok:
            raise RuntimeError(f"Cloudflare API 錯誤 {resp.status_code}: {resp.text[:300]}")
        return resp.json()["result"]


def _text_of(result):
    if isinstance(result, dict):
        if isinstance(result.get("response"), str):
            return result["response"]
        if result.get("choices"):
            return result["choices"][0]["message"].get("content") or ""
    return str(result)


def _tool_calls_of(result):
    """相容兩種格式：Workers AI 的 result.tool_calls，與 OpenAI 相容的 choices[0].message.tool_calls。"""
    calls = []
    raw = (result or {}).get("tool_calls")
    if raw is None and (result or {}).get("choices"):
        raw = result["choices"][0]["message"].get("tool_calls")
    for c in raw or []:
        fn = c.get("function", c)
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        calls.append({"name": fn.get("name"), "arguments": args or {}})
    return calls


# ---------------------------------------------------------------- MCP client

def _data(result):
    d = getattr(result, "data", None)
    if isinstance(d, dict):
        return d
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


class MCPSession:
    """一個常駐的 MCP Client：Server 只啟動一次，之後的工具呼叫都重用同一個 stdio 連線。"""

    def __init__(self, server=SERVER):
        import threading
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server = server
        self.client = None
        self._run(self._open())

    def _run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        from fastmcp import Client
        self.client = Client(self.server)
        await self.client.__aenter__()

    async def _list(self):
        return await self.client.list_tools()

    async def _call(self, tool, args):
        return _data(await self.client.call_tool(tool, args or {}))

    def list_tools(self):
        return self._run(self._list())

    def call_tool(self, tool, args):
        return self._run(self._call(tool, args))


_session = None


def session():
    global _session
    if _session is None:
        _session = MCPSession()
    return _session


def list_tools():
    return session().list_tools()


def call_tool(tool, args):
    return session().call_tool(tool, args)


HELPER_TOOLS = {"match_terms"}       # 給防護層用，不讓 Router 選


def tool_schemas():
    """把 MCP 工具描述轉成 LLM tool calling 的格式（MCP 的動態能力探索）。"""
    out = []
    for t in list_tools():
        if t.name in HELPER_TOOLS:
            continue
        schema = dict(getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                      or {"type": "object", "properties": {}})
        schema.pop("$schema", None)
        out.append({"name": t.name, "description": (t.description or "").strip()[:900], "parameters": schema})
    return out


# ---------------------------------------------------------------- Router agent

ROUTER_PROMPT = """你是輔仁大學選課助理的 Router。你的唯一工作是：為使用者的問題選「一個」最合適的工具並填好參數。
規則：
- 結構化條件（學分、必選修、系所、老師、星期、晚上、語言、是否程式課）→ filter_courses
- 問某門課「幾學分」「有哪幾班」→ list_sections
- 「有／沒有期末考、期中考、報告」→ check_assessment
- 追問上一輪的課（那門課、它、這堂）→ get_course，用下方「上一輪提到的課」的課號
- 「不用寫程式」→ filter_courses(programming=false)
- 星期用數字：一=1 … 六=6
- 一定要呼叫工具，不要直接回答。"""


def route(question, memory, tools, terms):
    mem = "（無）"
    if memory:
        mem = "\n".join(f"[{m['id']}] {m['name']}｜{m.get('teacher', '')}" for m in memory[:5])
    known = "；".join(f"{k}={'、'.join(v)}" for k, v in
                     (("課名", terms["courses"]), ("老師", terms["teachers"]), ("系所", terms["depts"])) if v) or "（無）"
    messages = [
        {"role": "system", "content": ROUTER_PROMPT},
        {"role": "user", "content": (f"上一輪提到的課：\n{mem}\n\n問題中出現的已知名稱（參數請照抄）：{known}"
                                     f"\n\n使用者問題：{question}")},
    ]
    result = _cf_run({"messages": messages, "tools": tools, "temperature": 0, "max_tokens": 300})
    calls = _tool_calls_of(result)
    if calls and calls[0]["name"]:
        return {"tool": calls[0]["name"], "args": calls[0]["arguments"], "by": "llm"}
    return fallback_route(question, memory, terms)


STRING_ARGS = {"name_contains": "courses", "name": "courses", "teacher": "teachers", "dept": "depts"}


def guard_args(r, question, memory, terms, tools):
    """Router 防護：檢查並修正工具參數。回傳修正後的 route 與修正紀錄。"""
    fixes = []
    schema = {t["name"]: t["parameters"].get("properties", {}) for t in tools}
    if r["tool"] not in schema:
        fixes.append(f"不存在的工具 {r['tool']} → 改用規則路由")
        return fallback_route(question, memory, terms), fixes
    args = {}
    for k, v in (r["args"] or {}).items():
        if k not in schema[r["tool"]]:
            fixes.append(f"刪除不認得的參數 {k}")
        elif isinstance(v, (dict, list)):
            fixes.append(f"刪除格式錯誤的參數 {k}")
        else:
            args[k] = v
    for k, kind in STRING_ARGS.items():
        if k in args and str(args[k]) not in question:
            good = terms[kind][0] if terms[kind] else None
            fixes.append(f"{k}「{args[k]}」不在問題裡 → " + (f"改成「{good}」" if good else "刪除"))
            if good:
                args[k] = good
            else:
                args.pop(k)
    if "keyword" in args and str(args["keyword"]) not in question:
        fixes.append(f"keyword「{args['keyword']}」不在問題裡 → 刪除")
        args.pop("keyword")
    tool = r["tool"]
    if tool == "get_course":
        valid = {m["id"] for m in memory}
        try:
            cid = int(args.get("course_id"))
        except (TypeError, ValueError):
            cid = None
        if cid not in valid:
            fixes.append(f"課號 {args.get('course_id')} 不是上一輪提到的課（疑似編造）→ 改用規則路由")
            return fallback_route(question, memory, terms), fixes
        args["course_id"] = cid
    if tool == "check_assessment" and (terms["teachers"] or terms["courses"]):
        # 問的是「某一門課」有沒有某項評量，不是全校清單：先鎖定那門課，再自動查完整資料
        fixes.append("問題指定了課名或老師 → 改用 filter_courses 鎖定那門課，而不是列出全校清單")
        tool, args = "filter_courses", {**({"teacher": terms["teachers"][0]} if terms["teachers"] else {}),
                                        **({"name_contains": terms["courses"][0]} if terms["courses"] else {})}
    if tool == "list_sections" and terms["teachers"]:
        fixes.append("問題指定了老師 → 改用 filter_courses 找出那一班")
        tool, args = "filter_courses", {"teacher": terms["teachers"][0],
                                        **({"name_contains": terms["courses"][0]} if terms["courses"] else {})}
    if tool == "list_sections" and "name" not in args:
        fixes.append("list_sections 缺少課名 → 改用規則路由")
        return fallback_route(question, memory, terms), fixes
    for k, kind in (("teacher", "teachers"), ("dept", "depts")):
        if tool == "filter_courses" and terms[kind] and k not in args:
            args[k] = terms[kind][0]
            fixes.append(f"補上問題中的{'老師' if k == 'teacher' else '系所'}「{terms[kind][0]}」")
    return {**r, "tool": tool, "args": args}, fixes


def fallback_route(question, memory, terms=None):
    """LLM 沒有呼叫工具或參數無法修正時的規則式備援（仍然走 MCP）。"""
    terms = terms or {"courses": [], "teachers": [], "depts": []}
    q = question
    if memory and re.search(r"那門|那堂|它|這門|這堂|那它", q):
        return {"tool": "get_course", "args": {"course_id": memory[0]["id"]}, "by": "rule"}
    m = re.search(r"(期末考|期中考|期末報告|口頭報告)", q)
    if m and not (terms["courses"] or terms["teachers"]):
        mode = "lacks" if re.search(r"沒有|不用|免", q) else "has"
        return {"tool": "check_assessment", "args": {"keyword": m.group(1), "mode": mode}, "by": "rule"}
    if terms["courses"] and not terms["teachers"] and not terms["depts"] and re.search(r"幾學分|哪幾班|幾個班|有哪些班", q):
        return {"tool": "list_sections", "args": {"name": terms["courses"][0]}, "by": "rule"}
    if re.search(r"不用寫程式|不寫程式|非程式", q):
        return {"tool": "filter_courses", "args": {"programming": False}, "by": "rule"}
    args = {}
    if terms["courses"]:
        args["name_contains"] = terms["courses"][0]
    if terms["teachers"]:
        args["teacher"] = terms["teachers"][0]
    if terms["depts"]:
        args["dept"] = terms["depts"][0]
    m = re.search(r"(\d+(?:\.\d+)?)\s*學分以上", q)
    if m:
        args["credit_min"] = float(m.group(1))
    for t in ("必修", "選修", "通識"):
        if t in q:
            args["req_type"] = t
    m = re.search(r"星期([一二三四五六])", q)
    if m:
        args["weekday"] = WEEKDAYS.index(m.group(1)) + 1
    if "晚上" in q:
        args["evening"] = True
    return {"tool": "filter_courses", "args": args, "by": "rule"}


# ---------------------------------------------------------------- Answer agent

ANSWER_PROMPT = """你是輔仁大學選課助理的 Answer agent。只能根據「工具資料」回答，用繁體中文。
課程清單會由系統依工具資料自動列出，你只需要寫一段簡短摘要，並挑出要列出的課號。
嚴格規則：
1. 工具資料 count=0 或 found=false → summary 只能是「查無資料」，show_ids 為空。不要推薦其他課。
2. 有 count 時，summary 第一句必須是「共 N 門…」，N 就是工具的 count。
3. 有資料時絕對不能說「沒有」「查無資料」。
4. 只使用工具資料裡的事實，不要加入補考、推薦理由等工具沒有的資訊。
5. 若工具資料有 basis 欄位，summary 最後一句照實說明判斷依據。
6. 單一課程（found=true）：直接回答使用者問的事（例如評量、上課時間），數字照抄資料。
7. show_ids 最多 {max_listed} 個，挑最符合問題的課號；單一課程時放那一門的課號。

只輸出一個 JSON 物件，不要其他文字：
{{"summary": "1 到 3 句話", "show_ids": [課號, ...], "total": 工具的 count 或 null}}"""

EVIDENCE_FIELDS = ("id", "name", "teacher", "credit", "type", "dept", "schedule", "language")


def compact_evidence(ev):
    """只給 Answer agent 需要的欄位，控制上下文長度。"""
    out = {k: v for k, v in ev.items() if k not in ("courses", "sections")}
    rows = ev.get("courses") or ev.get("sections")
    if rows is not None:
        out["courses"] = [{k: c.get(k) for k in EVIDENCE_FIELDS} for c in rows[:30]]
    if "objective" in out:
        out["objective"] = (out["objective"] or "")[:200]
    return out


def _parse_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("沒有 JSON")
    return json.loads(m.group(0))


def draft_answer(question, evidence, feedback=None):
    """Answer agent：回傳 (摘要, 要列出的課號, 主張, 原始輸出)。格式錯誤時摘要為 None。"""
    content = (f"使用者問題：{question}\n\n工具資料：\n"
               + json.dumps(compact_evidence(evidence), ensure_ascii=False))
    if feedback:
        content += ("\n\n你上一版的回答沒有通過驗證，問題如下，請修正後重新輸出 JSON：\n- "
                    + "\n- ".join(feedback))
    messages = [{"role": "system", "content": ANSWER_PROMPT.format(max_listed=MAX_LISTED)},
                {"role": "user", "content": content}]
    raw = _text_of(_cf_run({"messages": messages, "temperature": 0, "max_tokens": 400}))
    try:
        d = _parse_json(raw)
        summary = str(d.get("summary", "")).strip()
        ids = [int(x) for x in (d.get("show_ids") or [])][:MAX_LISTED]
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, [], [], raw
    claims = [{"type": "course", "id": i} for i in ids]
    if d.get("total") is not None:
        claims.append({"type": "total", "n": int(d["total"]),
                       "complete": len(ids) >= int(d["total"])})
    if evidence.get("mode") == "lacks":
        claims += [{"type": "lacks_assessment", "id": i, "keyword": evidence["keyword"]} for i in ids]
    return summary, ids, claims, raw


def compose(summary, ids, evidence):
    """最終回答＝LLM 摘要＋由工具資料產生的課程清單（清單內容不經 LLM）。"""
    if summary.strip() == NO_DATA:
        return NO_DATA
    if evidence.get("found") is True:
        return summary + "\n\n" + render_from_data(evidence)
    rows = {c["id"]: c for c in (evidence.get("courses") or evidence.get("sections") or [])}
    shown = [rows[i] for i in ids if i in rows] or list(rows.values())[:MAX_LISTED]
    lines = [summary, ""] + [_fmt_course(c) for c in shown]
    n = evidence.get("count")
    if n and n > len(shown):
        lines.append(f"（另有 {n - len(shown)} 門未列出）")
    if evidence.get("credits"):
        lines.append("學分分布：" + "、".join(f"{k} 學分 {v} 班" for k, v in evidence["credits"].items()))
    return "\n".join(lines)


def _fmt_course(c):
    return f"- [{c['id']}] {c['name']}｜{c.get('teacher') or '—'}｜{c['credit']:g} 學分｜{c.get('type') or '—'}"


def render_from_data(evidence):
    """保底回答：完全由工具資料產生，不經過 LLM。"""
    if evidence.get("found") is False or evidence.get("count") == 0:
        return NO_DATA
    if evidence.get("found") is True:                       # get_course
        c = evidence
        sched = "、".join(f"星期{WEEKDAYS[int(s['weekday']) - 1] if s['weekday'].isdigit() else s['weekday']} "
                         f"{s['section']}（{s.get('room') or '教室未定'}）" for s in c.get("schedules", []))
        assess = "、".join(f"{a['item']} {a['percent']:g}%" for a in c.get("assessment", [])) or "大綱未列"
        return (f"[{c['id']}] {c['name']}｜{c.get('teacher')}｜{c['credit']:g} 學分｜{c.get('type')}\n"
                f"上課時間：{sched or '未列'}\n學習評量：{assess}")
    rows = evidence.get("courses") or evidence.get("sections") or []
    n = evidence.get("count", len(rows))
    shown = rows[:MAX_LISTED]
    head = f"共 {n} 門" + (f"，以下列出 {len(shown)} 門：" if n > len(shown) else "：")
    lines = [head] + [_fmt_course(c) for c in shown]
    if evidence.get("credits"):
        lines.append("學分分布：" + "、".join(f"{k} 學分 {v} 班" for k, v in evidence["credits"].items()))
    if evidence.get("basis"):
        lines.append(f"依據：{evidence['basis']}")
    return "\n".join(lines)


def data_claims(evidence):
    """保底回答對應的主張（同樣送 Verifier 檢查，證明保底回答也正確）。"""
    if evidence.get("found") is False or evidence.get("count") == 0:
        return []
    if evidence.get("found") is True:
        return [{"type": "course", "id": evidence["id"], "credit": evidence["credit"], "teacher": evidence.get("teacher")}]
    rows = (evidence.get("courses") or evidence.get("sections") or [])[:MAX_LISTED]
    claims = [{"type": "course", "id": c["id"], "credit": c["credit"], "teacher": c.get("teacher")} for c in rows]
    n = evidence.get("count", len(rows))
    claims.append({"type": "total", "n": n, "complete": n <= len(rows)})
    if evidence.get("mode") == "lacks":
        claims += [{"type": "lacks_assessment", "id": c["id"], "keyword": evidence["keyword"]} for c in rows]
    return claims


# ---------------------------------------------------------------- pipeline

def _mentioned(answer, evidence):
    """把這一輪回答中提到的課記下來，給下一輪追問用。"""
    rows = {c["id"]: c for c in (evidence.get("courses") or evidence.get("sections") or [])}
    if evidence.get("found") is True:
        rows[evidence["id"]] = evidence
    ids = [int(x) for x in re.findall(r"\[(\d{6})\]", answer)]
    return [{"id": i, "name": rows[i]["name"], "teacher": rows[i].get("teacher")} for i in ids if i in rows]


def answer(question, memory=None, tools=None):
    """回傳完整追蹤紀錄 trace（介面與紀錄檔都用它）。"""
    memory = memory or []
    t0 = time.time()
    trace = {"question": question, "steps": []}
    tools = tools or tool_schemas()

    terms = call_tool("match_terms", {"text": question})
    trace["terms"] = terms
    r = route(question, memory, tools, terms)
    trace["router_raw"] = {"tool": r["tool"], "args": r["args"]}
    r, fixes = guard_args(r, question, memory, terms, tools)
    try:
        evidence = call_tool(r["tool"], r["args"])
    except Exception as e:                     # 參數仍然無效：改走規則路由
        fixes.append(f"工具呼叫失敗（{str(e)[:80]}）→ 改用規則路由")
        r = fallback_route(question, memory, terms)
        evidence = call_tool(r["tool"], r["args"])
    r["fixes"] = fixes
    trace["route"] = r
    rows = evidence.get("courses") or evidence.get("sections") or []
    if r["tool"] != "get_course" and evidence.get("count") == 1 and rows:
        # 只找到一門課：自動接著查完整資料（評量、上課時間），才能回答「有期中考嗎」這類問題
        trace["chained"] = {"tool": "get_course", "args": {"course_id": rows[0]["id"]}}
        evidence = call_tool("get_course", {"course_id": rows[0]["id"]})
    trace["evidence"] = evidence

    status, text = None, None
    feedback = None
    for stage in ("draft", "repair"):
        summary, ids, claims, raw = draft_answer(question, evidence, feedback)
        if summary is None:
            v = {"ok": False, "answer": "", "problems": ["回答格式錯誤（不是有效的 JSON），無法驗證"]}
        else:
            v = verify(summary, claims, [evidence])
        trace["steps"].append({"stage": stage, "answer": summary if summary is not None else raw,
                               "claims": claims, "verdict": v})
        if v["ok"]:
            text = compose(summary, ids, evidence)
            status = "verified" if stage == "draft" else "repaired"
            break
        feedback = v["problems"]
    if status is None:
        text = render_from_data(evidence)
        v3 = verify(text, data_claims(evidence), [evidence])
        trace["steps"].append({"stage": "fallback", "answer": text, "verdict": v3})
        status = "fallback"

    trace.update(final=text, status=status, seconds=round(time.time() - t0, 2),
                 memory=_mentioned(text, evidence) or memory)
    _log(trace)
    return trace


def _log(trace):
    LOG_PATH.parent.mkdir(exist_ok=True)
    rec = {"time": datetime.now().isoformat(timespec="seconds"), "model": CHAT_MODEL,
           **{k: trace[k] for k in ("question", "route", "status", "final", "seconds")},
           "count": trace["evidence"].get("count"),
           "problems": [p for s in trace["steps"] for p in s["verdict"]["problems"]]}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=list) + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    tr = answer(sys.argv[1])
    print(f"工具：{tr['route']['tool']}({json.dumps(tr['route']['args'], ensure_ascii=False)})  [{tr['route']['by']}]")
    print(f"總數：{tr['evidence'].get('count')}  狀態：{tr['status']}  耗時：{tr['seconds']}s")
    for s in tr["steps"]:
        if s["verdict"]["problems"]:
            print(f"  {s['stage']} 被攔截：", "；".join(s["verdict"]["problems"]))
    print("\n" + tr["final"])
