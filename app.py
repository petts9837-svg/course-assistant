"""
System 1 介面：課程大綱選課助理（Streamlit）
執行：streamlit run app.py
"""

import streamlit as st

import rag_core

st.set_page_config(page_title="輔大選課助理 System 1", page_icon="📚")
st.title("📚 輔大課程大綱選課助理")
st.caption("System 1 · Naive RAG prototype（Cloudflare Workers AI）")


@st.cache_resource
def get_retriever():
    return rag_core.Retriever()


with st.sidebar:
    st.header("設定")
    mode_label = st.radio("檢索方式", ["純向量搜尋", "關鍵字 + 向量（混合）"])
    mode = "hybrid" if mode_label.startswith("關鍵字") else "vector"
    k = st.slider("檢索課程數 (top-k)", 1, 10, 5)
    st.caption(f"Chat 模型：`{rag_core.CHAT_MODEL}`")
    st.caption(f"Embedding：`{rag_core.EMBED_MODEL}`")
    st.divider()
    st.markdown("**缺陷測試範例**")
    examples = [
        "有沒有不用寫程式的課？",
        "推薦 3 學分以上的選修課",
        "程式設計概論是幾學分？",
        "哪些課沒有期末考？",
        "資工系的程式設計概論在星期幾上課？",
    ]
    for ex in examples:
        if st.button(ex, use_container_width=True):
            st.session_state.pending = ex
    if st.button("清除對話", type="secondary"):
        st.session_state.history = []

try:
    retriever = get_retriever()
except Exception as e:  # 缺索引、缺環境變數等
    st.error(str(e))
    st.stop()

st.session_state.setdefault("history", [])

for turn in st.session_state.history:
    with st.chat_message("user"):
        st.write(turn["q"])
    with st.chat_message("assistant"):
        st.write(turn["a"])
        with st.expander(f"檢索到的課程（{turn['mode']}）"):
            for h in turn["hits"]:
                st.write(f"- {h['name']}（{h['teacher']}）· 課號 {h['id']} · score {h['score']:.4f}")

question = st.chat_input("輸入問題，例如：星期一晚上有什麼程式課？")
question = question or st.session_state.pop("pending", None)

if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.spinner("搜尋課程大綱中…"):
            try:
                reply, hits = rag_core.answer(retriever, question, k=k, mode=mode)
            except Exception as e:
                st.error(str(e))
                st.stop()
        st.write(reply)
        with st.expander(f"檢索到的課程（{mode_label}）"):
            for h in hits:
                st.write(f"- {h['name']}（{h['teacher']}）· 課號 {h['id']} · score {h['score']:.4f}")
    st.session_state.history.append({"q": question, "a": reply, "hits": hits, "mode": mode_label})
