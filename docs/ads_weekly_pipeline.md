# 莉莉安蝦皮廣告週報抓取（遠端 CDP）

給碼農／營運顧問：週報數字**只走遠端已登入的 Shopee 賣家中心**，不再依賴 ListMachines，也不再預設打使用者 Mac。

分析規則不變，仍用：

- [`docs/ads_analysis_rules.md`](ads_analysis_rules.md)
- [`docs/ads_metrics_dictionary.md`](ads_metrics_dictionary.md)
- [`docs/ads_report_prompt_spec.md`](ads_report_prompt_spec.md)
- [`skills/ads-analysis/SKILL.md`](../skills/ads-analysis/SKILL.md)

本流程**只抓取 + 分析**，禁止改預算、出價或任何廣告設定。Cookies／secrets 不進 Git、不印在 log。

## 為什麼以前會卡在 Mac

舊週報 bot 在 ListMachines 為空時失敗。它會找歷史 Mac `machineId` `0bccf28d-539a-4827-b81e-fd771c6896a3`，然後在那台跑：

```bash
python3 ads_analysis.py --refresh-source true
```

`--refresh-source true` 會呼叫 `crawler.py --mode ads-export`，而 crawler **預設新開本機 Playwright Chromium**，再把 `cookies.json` 灌進去。這綁定了：

| 輸入 | 為什麼綁 Mac |
|---|---|
| ListMachines 有在線 Mac | bot 才能 SSH／遠端執行 |
| Mac 上的 `cookies.json` | 新開的 Chromium 沒有賣家中心工作階段 |
| 本機 Chrome profile | 不是連「已經開著、已經登入」的瀏覽器 |

Mac 不在線 → ListMachines 空 → 寫出 `reports/ads_weekly/YYYYMMDD/BLOCKER.md` 這種失敗。

**新預設：** 連 Grok Bot 遠端盒上**已經登入**的 Chrome（CDP），沿用那個工作階段匯出報表。不需要 ListMachines，不需要 Mac。

## 碼農現在就跑（遠端盒）

在**已登入蝦皮賣家中心**的遠端 Chrome 那台機器上操作（Grok Bot remote box），repo 根目錄：

```bash
# 1) 確認 CDP 活著（預設埠 9232；或看環境變數 SHOPEE_ADS_CDP）
curl -sS http://127.0.0.1:9232/json/version

# 若 Chrome 還沒開 remote debugging，用「已登入的 user-data-dir」重開，例如：
# google-chrome --remote-debugging-port=9232 --user-data-dir="$HOME/shopee_chrome_profile"

# 2) 週報抓取 + 分析（預設 remote，不碰 Mac）
python3 ads_weekly.py

# 指定 CDP（擇一）
python3 ads_weekly.py --cdp-endpoint http://127.0.0.1:9232
SHOPEE_ADS_CDP=http://127.0.0.1:9232 python3 ads_weekly.py
```

舊指令若一定要沿用，也已改成遠端，**不要再送到 Mac**：

```bash
python3 ads_analysis.py --refresh-source true --source remote
```

營運週報請用 `ads_weekly.py`，輸出會進日期目錄，營運顧問才找得到。

### 不要做的事

- 不要 `ListMachines`、不要 `--source mac`
- 不要複製上周 `reports/ads_weekly/<舊日期>/` 的 CSV／JSON 來充數
- 不要在 Seller Center 改預算或廣告設定
- 不要把 `cookies.json` 提交進 Git，也不要在 log 印 cookie

## 輸出路徑（營運顧問讀這裡）

根目錄：`reports/ads_weekly/YYYYMMDD/`（`reports/` 已 gitignore）

以 2026-09-11 為例：

```text
reports/ads_weekly/20260911/
  ads_exports/
    ads_overall_past_month_*.csv      # 過去一個月（上月同日至昨天，29–32 天）
    ads_overall_yesterday_*.csv       # 昨天（主決策視窗）
    ads_overall_week_01_*_*.csv       # 滾動近 7 天
    ads_overall_week_02_*_*.csv
    ads_overall_week_03_*_*.csv
    ads_overall_week_04_*_*.csv
  ads_export_result.json
  ads_analysis_latest.json            # 與現有分析 JSON 同形狀
  ads_history.json
  ads_analysis_report.html            # 營運顧問主要閱讀檔
  ads_analysis_report.md
  SCOPE.md                            # 焦點關鍵字 + A1 campaign
  manifest.json                       # 成功清單；reused_previous_week 永遠 false
  BLOCKER.md                          # 只有失敗才會出現
```

必要視窗（缺一就 STOP）：**昨天 / `week_01` / 過去一個月**。  
`week_02`～`week_04` 是趨勢層；缺了會警告但仍繼續，**不會**用上周檔案補洞。

分析焦點（規則仍是店內 ROAS／直接 ROAS，不另立規則集）：

- 關鍵字：`airpods`、`氣囊`、`吊飾|掛飾|掛件|掛繩`
- 固定追蹤活動 A1：`18025139892`

## 登入牆／驗證碼／工作階段死亡：STOP

偵測到下列任一狀況，**立刻失敗**：

- `LOGIN_WALL`：被導到登入頁
- `CAPTCHA`：安全驗證／驗證碼
- `SESSION_DEAD`：遠端 Chrome 沒有有效賣家中心頁
- `CDP_UNAVAILABLE`：連不上 CDP（Chrome 沒開 debugging 或埠錯了）
- `MISSING_WINDOWS`：本趟沒抓到昨天／近 7 天／過去一個月

失敗時：

1. 寫 `reports/ads_weekly/YYYYMMDD/BLOCKER.md`
2. process **exit 非 0**
3. **禁止**沿用上周數字或 repo 根目錄 `ads_exports/` 舊檔
4. 同一天重跑會先清空**當天**目錄的 CSV／報告，再抓新的

成功時**不會**留下 `BLOCKER.md`。

## 遠端 Chrome 怎麼開 CDP

預設 endpoint：`http://127.0.0.1:9232`  
覆寫：`--cdp-endpoint` 或環境變數 `SHOPEE_ADS_CDP`

程式用 Playwright `connect_over_cdp` 掛上**已經開著的 Chrome**，沿用已登入 context：

- 不讀、不印 `cookies.json`
- 結束時只斷開 CDP，**不會關掉**遠端 Chrome

`crawler.py --mode ads-export` 若沒帶 `--browser-source`，仍維持舊的本機 Chromium + cookies（給 Telegram／廣告工作台）。**週報不要用那條路。**

## 程式入口對照

| 誰跑 | 指令 | 預設來源 |
|---|---|---|
| 週報 bot／碼農 | `python3 ads_weekly.py` | **remote** |
| 舊 refresh 指令 | `python3 ads_analysis.py --refresh-source true` | **remote** |
| Telegram `/廣告匯出`、`ads.html` 匯出 | `crawler.py --mode ads-export` | mac／cookies（不變） |

可選後備（程式還留著，營運預設不要用）：`python3 ads_weekly.py --source mac`

## 這個 Cloud Agent VM 能不能現場抓？

**不能。** 這裡沒有已登入的 Shopee 賣家中心 Cookie／Chrome profile。請在 Grok Bot **遠端盒**（已登入 Seller Center 的那台）跑上面的指令。本 PR 交付的是 CLI、fail-closed 行為與測試，不是在此 VM 產出的本週 CSV。
