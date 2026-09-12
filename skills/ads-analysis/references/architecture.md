# 廣告子系統架構邊界

## 目的

這份檔案用來約束 Shopee 廣告功能與原本庫存補貨系統的邊界，避免後續修改再次互相影響。

## 前端分層

- 庫存首頁：
  - `index.html`
  - `script.js`
  - `styles.css`
- 廣告工作台：
  - `ads.html`
  - `ads.js`
  - `ads.css`

## 後端 API

- 庫存：
  - `/search`
- 廣告：
  - `/export_ads`
  - `/analyze_ads`

## Python 子系統

- 庫存與賣場主流程：
  - `crawler.py`
- 廣告分析：
  - `ads_analysis.py`
  - `ads_weekly.py`（週報抓取，預設遠端 CDP）
  - `ads_session.py`
  - `config_loader.py`
  - `setup_openai_key.py`

## 邊界原則

- 不要把廣告 DOM、按鈕狀態、進度條邏輯放回庫存首頁。
- 不要讓庫存首頁依賴廣告分析 JSON 或 HTML 報告。
- 不要因為廣告分析需要而更改原本庫存搜尋的 request / response 形狀。
- 廣告報告輸出屬於廣告子系統資產，不應作為庫存頁面必需依賴。
- 若未來要擴充廣告功能，優先在 `ads.html` / `ads.js` / `ads.css` 內完成。
- OpenAI Key 不應寫死在程式碼、README 範例或 Git 追蹤檔中。
- OpenAI Key 應優先存於專案本地 `.env.local`，並由 `setup_openai_key.py` 初始化。
- 程式可以保留 shell 環境作為備援，但不得依賴 shell 設定作為唯一主要來源。
