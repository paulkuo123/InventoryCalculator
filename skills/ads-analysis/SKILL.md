---
name: ads-analysis
description: 用於分析 Shopee 商品廣告報表，輸出只包含需要調整商品的 HTML 報告、數學化建議與昨日基準的廣告調整判讀。
---

# Shopee 廣告分析 Skill

## 目的

用於分析 Shopee 商品廣告報表，輸出「只包含需要調整商品」的 HTML 報告與具體建議。

## 核心判準

### 1. 主判斷視窗

- 主要決策基準一律使用「昨天」。
- 「今天」只做監控，不作主要調整依據，因為資料在日內下載時通常尚未完整。

### 2. 基準線

- 每個商品廣告預設以 `投入產出比 (ROAS) >= 3` 為店內及格線。
- 若 `ROAS < 3`，視為未達標，可能已接近或進入賠錢區。
- 若 `直接投入產出比 < 3`，即使總 ROAS 看起來不差，也不能直接判定為可加碼。

### 3. 建議分類

- `立即加碼`
  - 昨天 ROAS >= 3
  - 昨天直接 ROAS >= 3
  - 近週 ROAS 仍穩定
  - 建議先檢查是否觸及預算上限，再評估加預算 10% 到 20%

- `優先降預算`
  - 昨天 ROAS < 3 或 昨天直接 ROAS < 3
  - 且昨天已有明顯花費
  - 建議先降預算 10% 到 30%，再判斷是素材問題還是商品頁問題

- `依賴間接轉換`
  - 昨天總 ROAS >= 3
  - 但昨天直接 ROAS < 3
  - 或直接銷售占比過低
  - 這類商品不可因總 ROAS 好看就直接加碼

## 輸出原則

- 不要把全部商品都列出來。
- 只輸出需要處理的商品：
  - 立即加碼
  - 優先降預算
  - 依賴間接轉換
- 每條建議都要帶數學驗算，不要空泛描述。
- 建議語氣直接、明確，不要過度客套。

## 建議寫法

- 先寫驗算：
  - 昨天花費
  - 昨天 ROAS
  - 昨天直接 ROAS
  - 昨天 CTR / CVR / CPC / CPA
  - 近週 ROAS / 近週直接 ROAS
  - 過去一個月 ROAS / 直接 ROAS
- 再寫動作：
  - 加預算多少
  - 降預算多少
  - 檢查素材或商品頁
  - 不要只看總 ROAS

## 指標要求

- 不能只看 `ROAS`。
- 至少一起考慮：
  - `ROAS`
  - `直接 ROAS`
  - `CTR`
  - `CVR`
  - `CPC`
  - `CPA`
  - `直接成交占比`
- 若是 OpenAI 分析，每個商品都應盡量輸出：
  - `primary_issue`
  - `reason`
  - `why_not_other_issue`
  - `direct_actions`
- 對於已檢查但暫不列入的商品，應輸出：
  - `excluded_but_reviewed_products`

## 圖片辨識

- 商品名稱相似時，一律以 `商品 ID + 商品圖片` 識別。
- 圖片來源以 `golden_table.json` 的 `商品圖片網址` 為準。

## 報告形式

- 網頁端不直接鋪開全部廣告診斷內容。
- 主要輸出為 HTML 報告。
- 網頁只顯示摘要、狀態與 HTML 下載入口。

## OpenAI Key 使用方式

- 廣告 AI 分析若要呼叫 OpenAI，應優先使用專案本地設定檔 `.env.local`。
- 初始化方式：
  - 執行 `python3 setup_openai_key.py`
  - 依提示輸入 OpenAI API Key
  - 腳本會將 `OPENAI_API_KEY` 寫入專案根目錄 `.env.local`
- 程式讀取順序固定為：
  - `OPENAI_API_KEY` 環境變數
  - 專案本地 `.env.local`
  - `~/.zshrc` / `~/.bashrc` 備援
- 網頁端或 Telegram 端不會主動幫使用者建立或填入 Key。
- 若尚未執行 `setup_openai_key.py`，OpenAI 分析可能會退回規則層摘要。
- `.env.local` 必須加入 `.gitignore`，不可提交到 GitHub。
- repo 內若需要提供範例，只能提供 `.env.example`，不可放真實 Key。

## 系統邊界

- 廣告功能應視為獨立子系統，不應混進原本的庫存補貨主流程。
- 庫存首頁只負責：
  - 商品搜尋
  - 庫存儀表板
  - 補貨判讀
- 廣告功能應集中在獨立頁面與獨立前端模組。
- 廣告 API 可共用同一個 server，但前端頁面與腳本要解耦。
- 廣告子系統架構與邊界規則見：
  - [references/architecture.md](references/architecture.md)
