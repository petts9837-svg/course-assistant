"""
System 2 介面：MCP 工具 + 多 agent 驗證的選課助理
執行（在專案根目錄）：streamlit run system2/app_s2.py --server.port 8502
"""

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402

st.set_page_config(page_title="輔大選課助理 System 2", page_icon="🛡️")
st.title("🛡️ 輔大課程大綱選課助理")
st.caption("System 2 · MCP tools + multi-agent verification（Cloudflare Workers AI）")

STATUS = {
    "verified": ("✅ 驗證通過", "success"),
    "repaired": ("🔁 第一版被攔截，修正後通過", "warning"),
    "fallback": ("🛡️ LLM 回答未通過驗證，改用工具資料直接產生", "info"),
}


@st.cache_resource
def get_tools():
    return agent.list_tools(), agent.tool_schemas()


try:
    mcp_tools, schemas = get_tools()
except Exception as e:  # MCP Server 啟動失敗、缺 fastmcp 等
    st.error(f"無法連到 MCP Server：{e}")
    st.stop()

st.session_state.setdefault("history", [])
st.session_state.setdefault("memory", [])

with st.sidebar:
    st.header("MCP Server：course-sql")
    for t in mcp_tools:
        st.markdown(f"- `{t.name}`")
    st.caption(f"Chat 模型：`{agent.CHAT_MODEL}`")
    st.divider()
    st.markdown("**測試問題**")
    for ex in ["推薦 3 學分以上的選修課", "哪些課沒有期末考？", "有沒有不用寫程式的課？",
               "資工系的程式設計概論在星期幾上課？", "程式設計概論是幾學分？",
               "吳周駿老師的程式設計概論有期中考嗎？", "那它在星期幾上課？", "輔大有開量子計算的課嗎？"]:
        if st.button(ex, use_container_width=True):
            st.session_state.pending = ex
    if st.button("清除對話"):
        st.session_state.history, st.session_state.memory = [], []


def show_trace(tr):
    label, kind = STATUS[tr["status"]]
    getattr(st, kind)(label)
    r = tr["route"]
    ev = tr["evidence"]
    count = ev.get("count")
    st.caption(f"工具：`{r['tool']}({json.dumps(r['args'], ensure_ascii=False)})`"
               + (f"｜符合總數 {count}" if count is not None else "")
               + f"｜{tr['seconds']} 秒")
    with st.expander("Agent 流程（Router → MCP → Answer → Verifier）"):
        st.markdown(f"**1. Router**（{'LLM tool calling' if r['by'] == 'llm' else '規則備援'}）"
                    f"選擇 `{r['tool']}`")
        if r.get("fixes"):
            st.markdown("**Router 防護**修正了工具參數：")
            for f in r["fixes"]:
                st.markdown(f"  - {f}")
        if tr.get("chained"):
            st.markdown(f"**2. MCP 工具回傳**：只找到 1 門，自動接著呼叫 "
                        f"`get_course({tr['chained']['args']['course_id']})` 取得完整資料")
        else:
            st.markdown(f"**2. MCP 工具回傳**：count = {count}，回傳 {ev.get('returned', '—')} 筆"
                        + (f"，{ev['note']}" if ev.get("note") else ""))
        for i, s in enumerate(tr["steps"], 3):
            name = {"draft": "Answer agent 初稿", "repair": "Answer agent 修正稿",
                    "fallback": "資料直出保底回答"}[s["stage"]]
            ok = s["verdict"]["ok"]
            st.markdown(f"**{i}. {name}** → Verifier：{'通過 ✅' if ok else '攔截 ⛔'}")
            for p in s["verdict"]["problems"]:
                st.markdown(f"  - {p}")
            if not ok:
                st.code(s["answer"][:800], language=None)


for turn in st.session_state.history:
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        st.text(turn["final"])
        show_trace(turn)

question = st.chat_input("輸入問題，例如：星期五晚上有什麼課？") or st.session_state.pop("pending", None)
if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.spinner("Router 選工具 → 查詢 MCP → 撰寫並驗證回答…"):
            try:
                tr = agent.answer(question, memory=st.session_state.memory, tools=schemas)
            except Exception as e:
                st.error(str(e))
                st.stop()
        st.text(tr["final"])
        show_trace(tr)
    st.session_state.memory = tr["memory"]
    st.session_state.history.append(tr)
