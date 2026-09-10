# Ads workbench

The ads workbench lets a seller export six Shopee ad CSVs and run a local + optional OpenAI analysis. The default verification path only loads the page and the OpenAI status banner; it does not export from Shopee or spend API quota.

## Sub-features

- `ads-load` shows `莉莉安蝦皮廣告工作台` and the export / analyze cards.
- `ads-openai-status` renders `#openaiStatus` from `GET /openai_status` (configured or missing key).
- `ads-export-live` is `#adsExportButton` / `GET /export_ads` — live Shopee download, default-forbidden.
- `ads-analyze` is `#adsAnalyzeButton` / `GET /analyze_ads` — reads `ads_exports/` and may call OpenAI, default-forbidden.

## How to get to it (user POV)

- Choose **前往廣告分析** on the inventory home header.
- Open `http://127.0.0.1:8080/ads.html` directly.

## Driving it with control-inventory

Preconditions:

- `control-inventory doctor` reports `"healthy": true`.
- OpenAI key is optional. Missing key is a valid `#openaiStatus` missing state.
- Do not require `ads_exports/` for the default proof.

- **Open workbench.** Open the ads page. Run `control-inventory drive ads-workbench`. The `h1` contains `廣告工作台`.
- **Wait for status.** Wait until `#openaiStatus` no longer has class `checking`. The banner text either contains `OpenAI API 已設定` or `尚未設定 API Key`.
- **Second view.** Run `control-inventory http GET /openai_status`. HTTP 200 JSON includes `configured` (boolean). Do not copy `key_source` secrets; the endpoint only names the source, not the key.
- **Controls present, unused.** `#adsExportButton` text includes `匯出 6 份`. `#adsAnalyzeButton` text includes `分析現有廣告報表`. Do not click either.
- **Proof.** Keep `ads.png`, `ads.dom.txt`, `ads.openai_status.json`, and `proof.json`. The screenshot shows the heading and the OpenAI status chip.

## Gotchas

- `#adsExportButton` launches Playwright against Shopee seller ads and writes `ads_exports/`. That is not a dry-run.
- `#adsAnalyzeButton` with `#includeAiAnalysis` checked calls OpenAI. Unchecking it still reads local CSVs and writes `ads_analysis_report.html`.
- `#openaiStatus` starts in class `checking`. Assert after it leaves that class, not after `domcontentloaded`.
- Do not paste an API key into the page. Setup is `python3 setup_openai_key.py` writing `.env.local` outside this skill.
- `GET /openai_status` is the safe local proof when live export cannot run.
