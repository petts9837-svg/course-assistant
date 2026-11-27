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

    def chat(self, messages, max_tokens=1024, temperature=0.1, retries=3):
        payload = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        for attempt in range(retries):
            resp = requests.post(self.url, json=payload, headers=self.headers, timeout=120)
            if resp.status_code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            if not resp.ok:
                raise RuntimeError(f"Cloudflare API 錯誤 {resp.status_code}: {resp.text[:300]}")
            result = resp.json()["result"]
            if isinstance(result, dict) and "response" in result:
                out = result["response"]
                return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            if isinstance(result, dict) and "choices" in result:
                return result["choices"][0]["message"]["content"]
            return str(result)


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
