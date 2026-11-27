"""
pipeline.py：System 2 的完整流程（Multi-Agent + MCP）

    問題 → Guard（提示注入偵測）→ Router agent → MCP 工具（course-sql / course-search）
         → Answer agent → Verifier agent ─ 通過 → 回答
                                         └ 不通過 → 把問題回饋給 Answer agent 重寫一次
                                                    → 仍不通過 → 改用工具資料直接產生的安全回答

每一步都記錄在 trace（工具、參數、耗時、Verifier 的判斷），Streamlit 介面與評估腳本都用它。

命令列測試：
    python src/pipeline.py "推薦 3 學分以上的選修課"
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import answer_agent
import router_agent
import verifier
from llm import get_llm
from mcp_hub import MCPHub

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "data" / "qa_log_system2.jsonl"
DETAIL_WORDS = ["考", "評分", "評量", "成績", "時間", "星期幾", "幾點", "教室", "上課", "報告", "作業"]


class Assistant:
    def __init__(self, hub=None, llm="auto", log=True):
        self.hub = hub or MCPHub()
        self.llm = get_llm() if llm == "auto" else llm
        self.catalog = self.hub.read_resource("courses://catalog")
        self.last_courses = []  # 上一輪回答中出現的課，給追問用
        self.log = log

    @property
    def mode(self):
        return f"LLM（{self.llm.model}）" if self.llm else "離線規則模式（未設定 Cloudflare 憑證）"

    def reset(self):
        self.last_courses = []

    def ask(self, question):
        t0 = time.perf_counter()
        trace = {"question": question, "steps": []}

        def step(name, **info):
            trace["steps"].append({"step": name, "t": round(time.perf_counter() - t0, 2), **info})

        # 1. Router（含 Guard）
        plan = router_agent.route(question, self.hub.tools, self.catalog, self.llm, self.last_courses)
        if plan["injection"]:
            step("guard", flagged=True, note="偵測到疑似提示注入，問題中的指令部分不會被執行")
        step("router", by=plan["by"], reason=plan.get("reason", ""), calls=plan["calls"],
             note=plan.get("note"))

        # 追問對象不明確：不猜，請使用者指定（回答不含任何課程資料，不需查核）
        if plan.get("clarify"):
            final = (f"上一輪列出了 {plan['clarify']} 門課，請告訴我是哪一門"
                     "（例如課號或開課班級），我再幫你查。")
            trace.update(answer=final, verified=True, attempts=[], evidence=[], router_by=plan["by"],
                         injection=bool(plan["injection"]), mode=self.mode,
                         latency_s=round(time.perf_counter() - t0, 2))
            if self.log:
                _log(trace)
            return trace

        # 2. 呼叫 MCP 工具
        evidence = []
        for call in plan["calls"]:
            s = time.perf_counter()
            res = self.hub.call(call["tool"], call["args"])
            evidence.append({"tool": call["tool"], "args": call["args"], "result": res})
            step("tool", server=self.hub.tool_owner.get(call["tool"]), tool=call["tool"],
                 args=call["args"], ms=round((time.perf_counter() - s) * 1000),
                 summary=_summary(res))

        # 只找到一門課、又問細節（考試、時間…）→ 自動補查 get_course
        if (len(evidence) == 1 and evidence[0]["tool"] in ("filter_courses", "list_sections")
                and any(w in question for w in DETAIL_WORDS)):
            rows = evidence[0]["result"].get("courses") or evidence[0]["result"].get("sections") or []
            if len(rows) == 1:
                args = {"course_id": rows[0]["id"]}
                res = self.hub.call("get_course", args)
                evidence.append({"tool": "get_course", "args": args, "result": res})
                step("tool", server="course-sql", tool="get_course", args=args, summary=_summary(res),
                     note="只找到一門課，自動補查完整資訊")

        # 3. Answer agent → 4. Verifier（最多重寫一次）
        attempts = []
        if self.llm:
            feedback = None
            for i in range(2):
                draft = answer_agent.write_with_llm(self.llm, question, evidence, feedback)
                v = verifier.verify(draft, evidence, self.catalog, question)
                attempts.append({"draft": draft, "verdict": v})
                step("answer", attempt=i + 1, by="llm")
                step("verifier", attempt=i + 1, ok=v["ok"], problems=v["problems"])
                if v["ok"]:
                    break
                feedback = v["problems"]
            final, ok = draft, v["ok"]
            if not ok:
                final = answer_agent.compose_safe(question, evidence)
                v = verifier.verify(final, evidence, self.catalog, question)
                step("fallback", note="LLM 兩次都未通過查核，改用工具資料直接產生的安全回答", ok=v["ok"])
        else:
            final = answer_agent.compose_safe(question, evidence)
            v = verifier.verify(final, evidence, self.catalog, question)
            attempts.append({"draft": final, "verdict": v})
            step("answer", attempt=1, by="rules")
            step("verifier", attempt=1, ok=v["ok"], problems=v["problems"])

        if not v["ok"]:  # 安全回答理論上一定通過；萬一沒有，寧可不答
            final = answer_agent.NO_DATA
        if plan["injection"]:
            final = "（問題中要求改變規則的指令已忽略，以下只依課程資料回答）\n" + final

        # 記住這一輪的課，給下一輪追問
        courses = verifier.index_evidence(evidence)["courses"]
        if courses:
            self.last_courses = [{"id": c["id"], "name": c.get("name"), "teacher": c.get("teacher")}
                                 for c in courses.values()]

        trace.update(answer=final, verified=v["ok"], attempts=attempts, evidence=evidence,
                     router_by=plan["by"], injection=bool(plan["injection"]),
                     latency_s=round(time.perf_counter() - t0, 2), mode=self.mode)
        if self.log:
            _log(trace)
        return trace


def _summary(res):
    if res.get("error"):
        return f"錯誤：{res['error'][:80]}"
    if res.get("found") is False or res.get("count") == 0 or res.get("returned") == 0:
        return "查無資料"
    if "count" in res:
        return f"符合 {res['count']} 門（回傳 {res.get('returned')}）"
    if "returned" in res:
        return f"搜尋回傳 {res['returned']} 門（{res.get('mode_used')}，非完整清單）"
    if res.get("found"):
        return f"取得 {res.get('name')} 的完整資訊"
    return ""


def _log(trace):
    LOG_PATH.parent.mkdir(exist_ok=True)
    rec = {"time": datetime.now().isoformat(timespec="seconds"),
           "question": trace["question"], "mode": trace["mode"], "router_by": trace["router_by"],
           "calls": [s for s in trace["steps"] if s["step"] == "tool"],
           "drafts": [{"draft": a["draft"], "ok": a["verdict"]["ok"], "problems": a["verdict"]["problems"]}
                      for a in trace["attempts"]],
           "answer": trace["answer"], "verified": trace["verified"], "latency_s": trace["latency_s"]}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    bot = Assistant()
    print("模式：", bot.mode)
    for q in sys.argv[1:] or ["推薦 3 學分以上的選修課"]:
        tr = bot.ask(q)
        print(f"\n問：{q}")
        for s in tr["steps"]:
            print("  ·", json.dumps(s, ensure_ascii=False)[:220])
        print(f"答（{tr['latency_s']}s，查核{'通過' if tr['verified'] else '未通過'}）：\n{tr['answer']}")
    bot.hub.close()
