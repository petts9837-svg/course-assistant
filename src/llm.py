"""
llm.py：System 2 使用的 LLM（Cloudflare Workers AI，與 System 1 相同的模型）

環境變數：
    CF_ACCOUNT_ID, CF_API_TOKEN   必填（沒有設定時 get_llm() 回傳 None，系統改用離線規則模式）
    CF_CHAT_MODEL                 選填，預設 @cf/meta/llama-3.3-70b-instruct-fp8-fast
"""

import json
import os
import re
import time

import requests
from pathlib import Path


def load_env(path=Path(__file__).resolve().parents[1] / ".env"):
    """讀取專案根目錄的 .env（KEY=VALUE），不覆蓋已經設定的環境變數；不需要額外套件。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

CHAT_MODEL = os.environ.get("CF_CHAT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")


class CloudflareLLM:
    name = "cloudflare"

    def __init__(self, account, token, model=CHAT_MODEL):
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.model = model

    def run(self, payload, retries=3):
        """送出一次請求，回傳 Workers AI 的原始 result（可能含 response 或 tool_calls）。"""
        for attempt in range(retries):
            resp = requests.post(self.url, json=payload, headers=self.headers, timeout=120)
            if resp.status_code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            if not resp.ok:
                raise RuntimeError(f"Cloudflare API 錯誤 {resp.status_code}: {resp.text[:300]}")
            return resp.json()["result"]

    def chat(self, messages, max_tokens=1024, temperature=0.1):
        return text_of(self.run({"messages": messages, "max_tokens": max_tokens, "temperature": temperature}))

    def tool_call(self, messages, tools, max_tokens=300):
        """原生 tool calling：把 MCP 工具的 schema 交給模型，回傳 (tool_calls, 文字回覆)。"""
        result = self.run({"messages": messages, "tools": tools, "temperature": 0, "max_tokens": max_tokens})
        return tool_calls_of(result), text_of(result)


def text_of(result):
    if isinstance(result, dict):
        out = result.get("response")
        if isinstance(out, str):
            return out
        if out is not None:
            return json.dumps(out, ensure_ascii=False)
        if result.get("choices"):
            return result["choices"][0]["message"].get("content") or ""
        return ""
    return str(result)


def tool_calls_of(result):
    """相容 Workers AI 的 result.tool_calls 與 OpenAI 相容格式 choices[0].message.tool_calls。"""
    calls = []
    raw = (result or {}).get("tool_calls") if isinstance(result, dict) else None
    if raw is None and isinstance(result, dict) and result.get("choices"):
        raw = result["choices"][0]["message"].get("tool_calls")
    for c in raw or []:
        fn = c.get("function", c)
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        calls.append({"tool": fn.get("name"), "args": args or {}})
    return calls


def get_llm():
    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if account and token:
        return CloudflareLLM(account, token)
    return None


def extract_json(text):
    """從 LLM 回覆中取出第一個 JSON 物件（模型有時會在前後加說明文字或 ``` 區塊）。"""
    if isinstance(text, dict):
        return text
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("回覆中找不到 JSON")
