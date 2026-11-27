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
import re
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


class GuardedLLM:
    """包住 LLM：呼叫失敗（每日額度用完、網路斷線）時記下原因並暫停一段時間，
    暫停期間整個系統自動改用離線規則模式，而不是讓現場 demo 當掉。"""

    def __init__(self, llm):
        self.inner, self.model = llm, llm.model
        self.paused_until, self.last_error = 0.0, None
        self.supports_tools = hasattr(llm, "tool_call")

    @property
    def available(self):
        return time.time() >= self.paused_until

    def _guard(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            msg = str(e)
            quota = "429" in msg or "allocation" in msg or "neurons" in msg
            self.last_error = "每日免費額度用完" if quota else f"LLM 無法使用：{msg[:80]}"
            self.paused_until = time.time() + (600 if quota else 60)
            raise

    def chat(self, *a, **kw):
        return self._guard(self.inner.chat, *a, **kw)

    def tool_call(self, *a, **kw):
        return self._guard(self.inner.tool_call, *a, **kw)


class Assistant:
    def __init__(self, hub=None, llm="auto", log=True):
        self.hub = hub or MCPHub()
        raw = get_llm() if llm == "auto" else llm
        self.llm = GuardedLLM(raw) if raw is not None and not isinstance(raw, GuardedLLM) else raw
        self.catalog = self.hub.read_resource("courses://catalog")
        self.last_courses = []  # 上一輪回答中出現的課，給追問用
        self.log = log

    @property
    def mode(self):
        if not self.llm:
            return "離線規則模式（未設定 Cloudflare 憑證）"
        if not self.llm.available:
            return f"離線規則模式（LLM 暫停：{self.llm.last_error}）"
        return f"LLM（{self.llm.model}）"

    @property
    def active_llm(self):
        return self.llm if self.llm and self.llm.available else None

    def reset(self):
        self.last_courses = []

    def ask(self, question):
        t0 = time.perf_counter()
        trace = {"question": question, "steps": []}

        def step(name, **info):
            trace["steps"].append({"step": name, "t": round(time.perf_counter() - t0, 2), **info})

        # 1. Router（含 Guard）
        terms = None
        llm = self.active_llm
        if llm:
            # MCP 工具 match_terms：找出問題中「資料庫裡真的存在」的課名、老師、系所，讓 Router 照抄
            terms = self.hub.call("match_terms", {"text": question})
            if terms.get("error"):
                terms = None
            else:
                step("terms", **terms)
        plan = router_agent.route(question, self.hub.tools, self.catalog, llm, self.last_courses, terms)
        if plan["injection"]:
            step("guard", flagged=True, note="偵測到疑似提示注入，問題中的指令部分不會被執行")
        step("router", by=plan["by"], reason=plan.get("reason", ""), calls=plan["calls"],
             note=plan.get("note"))

        # 追問對象不明確：不猜，請使用者指定（回答不含任何課程資料，不需查核）
        if plan.get("clarify"):
            final = (f"上一輪列出了 {plan['clarify']} 門課，請告訴我是哪一門"
                     "（例如課號、老師、開課班級，或「第 2 門」），我再幫你查。")
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
        found = verifier.index_evidence(evidence)["courses"]
        if (len(found) == 1 and any(w in question for w in DETAIL_WORDS)
                and not any(ev["tool"] == "get_course" for ev in evidence)):
            if True:
                args = {"course_id": next(iter(found))}
                res = self.hub.call("get_course", args)
                evidence.append({"tool": "get_course", "args": args, "result": res})
                step("tool", server="course-sql", tool="get_course", args=args, summary=_summary(res),
                     note="只找到一門課，自動補查完整資訊")

        # 3. Answer agent → 4. Verifier（最多重寫一次）
        attempts = []
        llm = self.active_llm      # Router 失敗時可能剛被暫停
        if llm:
            feedback = None
            for i in range(2):
                try:
                    draft = answer_agent.write_with_llm(llm, question, evidence, feedback)
                except Exception:
                    draft = answer_agent.compose_safe(question, evidence)
                    v = verifier.verify(draft, evidence, self.catalog, question)
                    attempts.append({"draft": draft, "verdict": v})
                    step("fallback", note=f"{self.llm.last_error} → 改用工具資料直接產生的回答", ok=v["ok"])
                    break
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
        # 依回答中出現的順序記住（「第 2 門」要對到學生看到的第 2 門）
        courses = verifier.index_evidence(evidence)["courses"]
        if courses:
            shown = [int(x) for x in re.findall(r"\[(\d{6})\]", final) if int(x) in courses]
            order = list(dict.fromkeys(shown)) or list(courses)
            self.last_courses = [{"id": i, "name": courses[i].get("name"), "teacher": courses[i].get("teacher"),
                                  "dept": courses[i].get("dept")} for i in order]

        trace["llm_error"] = self.llm.last_error if self.llm and not self.llm.available else None
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
