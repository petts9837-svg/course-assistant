# 輔大課程大綱選課助理（期中 System 1）

用 RAG 回答選課問題：課程資料來自輔大課程大綱系統，LLM 與 embedding 用 Cloudflare Workers AI。

## 檔案
| 檔案 | 用途 |
|---|---|
| `fju_list_scraper.py` | 抓課程清單（學分、必選修、時段、分類）存進 `courses.db` |
| `fju_outline_fetcher.py` | 抓每門課的大綱（目標、評分方式、每週進度、教學方法） |
| `rag_core.py` | 向量索引、純向量 / 混合搜尋、LLM 問答、問答紀錄 |
| `app.py` | Streamlit 聊天介面 |

## 第一次設定
1. 安裝 Python 3.10 以上，接著：
   ```
   pip install -r requirements.txt
   ```
2. 設定 Cloudflare 憑證（向組長要，或用自己的帳號：Cloudflare 後台 → Workers AI → REST API）。
   **不要把 token 寫進程式或傳到群組。**
   ```
   set CF_ACCOUNT_ID=帳號ID
   set CF_API_TOKEN=token
   ```
   （`set` 只對目前視窗有效；想永久保存用 `setx`，設完要重開視窗。）

## 執行
repo 裡已經附上 `courses.db`，可以跳過步驟 1、2，直接從步驟 3 開始。
```
python fju_list_scraper.py --hy 115 --ht 1 --keyword 程式   # 1. 抓課程清單（可換關鍵字多抓幾次）
python fju_outline_fetcher.py                             # 2. 抓大綱
python rag_core.py build                                  # 3. 建立向量索引
streamlit run app.py                                      # 4. 開啟網頁 http://localhost:8501
```

## 缺陷測試
- 側邊欄可切換「純向量」與「關鍵字＋向量」，同一題兩種都問。
- 展開「檢索到的課程」，判斷錯誤是**檢索錯**還是 **LLM 答錯**。
- 所有問答自動記錄在 `data/qa_log.jsonl`，寫 AI Verification Log 時使用。

## 注意
- **這個 repo 必須保持 private**：`courses.db` 是學校課程資料，只在組內使用。
- `courses.db` 由組長統一更新後上傳，其他人不要自己重抓後 push，避免檔案衝突。
- `data/`（向量索引、問答紀錄）每個人自己產生，不上傳。
- 抓資料時程式已設定每次請求間隔 1 秒以上，請勿調低。
