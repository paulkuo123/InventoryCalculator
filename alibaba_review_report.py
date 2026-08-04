import argparse
import csv
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List


BAD_OPTION_FRAGMENTS = (
    "￥",
    "售",
    "诸暨",
    "有限公司",
    "工厂",
    "支持",
    "批发",
    "批發",
    "包装形式",
    "卡装",
    "虾米",
)
SOCK_KEYWORDS = ("襪", "袜")


def is_sock_product_name(value: str) -> bool:
    return any(keyword in str(value or "") for keyword in SOCK_KEYWORDS)


def short_product_name(name: str) -> str:
    value = re.sub(r"隔日到貨[🔥\s]*", "", str(name or ""))
    return value[:70]


def clean_options(options: List[str]) -> List[str]:
    cleaned = []
    for option in options or []:
        text = str(option or "").strip()
        if not text or len(text) > 80:
            continue
        if any(fragment in text for fragment in BAD_OPTION_FRAGMENTS):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def options_for_display(options: List[str], limit: int = 24) -> str:
    cleaned = clean_options(options)
    shown = cleaned[:limit]
    suffix = "" if len(cleaned) <= limit else f" ... (+{len(cleaned) - limit})"
    return " / ".join(shown) + suffix if shown else ""


def classify(item: Dict[str, Any], url_result: Dict[str, Any]) -> str:
    status = item.get("mappingStatus", "")
    reason = item.get("reason", "")
    options = clean_options(url_result.get("colorOptions", []))
    if status == "error" or url_result.get("status") == "error":
        return "C. 頁面抓取錯誤，需要之後重抓"
    if not options:
        return "C. 沒抓到有效 1688 選項，需要人工開頁確認"
    if reason == "無法從蝦皮型號判斷顏色":
        return "B. 型號不是單純顏色，需人工判斷圖案/款式"
    if reason == "1688 顏色選項沒有可對應顏色":
        return "A. 有 1688 選項但規則沒對到，可人工選一個"
    return "B. 其他低信心，需要人工確認"


def flatten_review_rows(report: Dict[str, Any], socks_only: bool) -> List[Dict[str, Any]]:
    rows = []
    for result in report.get("results", []):
        for item in result.get("items", []):
            if socks_only and not is_sock_product_name(str(item.get("productName", ""))):
                continue
            needs_review = item.get("mappingStatus") != "mapped" or item.get("confidence") != "high"
            if not needs_review:
                continue
            row = dict(item)
            row["reviewCategory"] = classify(item, result)
            row["urlStatus"] = result.get("status", "")
            row["urlMessage"] = result.get("message", "")
            row["cleanColorOptions"] = options_for_display(result.get("colorOptions", []))
            row["optionCount"] = len(clean_options(result.get("colorOptions", [])))
            rows.append(row)
    product_counts = Counter(row.get("productId", "") for row in rows)
    category_rank = {
        "A. 有 1688 選項但規則沒對到，可人工選一個": 0,
        "B. 型號不是單純顏色，需人工判斷圖案/款式": 1,
        "B. 其他低信心，需要人工確認": 2,
        "C. 沒抓到有效 1688 選項，需要人工開頁確認": 3,
        "C. 頁面抓取錯誤，需要之後重抓": 4,
    }
    rows.sort(key=lambda row: (
        category_rank.get(row["reviewCategory"], 9),
        -product_counts[row.get("productId", "")],
        row.get("productId", ""),
        row.get("modelName", ""),
    ))
    return rows


def write_csv(rows: List[Dict[str, Any]], output_prefix: Path) -> Path:
    path = output_prefix.with_suffix(".csv")
    fields = [
        "reviewCategory",
        "productId",
        "productNameShort",
        "modelName",
        "specId",
        "existingSkuName",
        "suggestedSkuName",
        "cleanColorOptions",
        "optionCount",
        "url",
        "urlSource",
        "reason",
        "mappingStatus",
        "confidence",
        "urlStatus",
        "urlMessage",
        "人工選擇1688SKU",
        "備註",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "reviewCategory": row.get("reviewCategory", ""),
                "productId": row.get("productId", ""),
                "productNameShort": short_product_name(str(row.get("productName", ""))),
                "modelName": row.get("modelName", ""),
                "specId": row.get("specId", ""),
                "existingSkuName": row.get("existingSkuName", ""),
                "suggestedSkuName": row.get("suggestedSkuName", ""),
                "cleanColorOptions": row.get("cleanColorOptions", ""),
                "optionCount": row.get("optionCount", ""),
                "url": row.get("url", ""),
                "urlSource": row.get("urlSource", ""),
                "reason": row.get("reason", ""),
                "mappingStatus": row.get("mappingStatus", ""),
                "confidence": row.get("confidence", ""),
                "urlStatus": row.get("urlStatus", ""),
                "urlMessage": row.get("urlMessage", ""),
                "人工選擇1688SKU": "",
                "備註": "",
            })
    return path


def write_markdown(rows: List[Dict[str, Any]], report: Dict[str, Any], report_path: Path, output_prefix: Path) -> Path:
    path = output_prefix.with_name(output_prefix.name + "_summary").with_suffix(".md")
    category_counts = Counter(row["reviewCategory"] for row in rows)
    status_counts = Counter(row.get("mappingStatus", "") for row in rows)
    product_counts = Counter(row.get("productId", "") for row in rows)
    lines = [
        "# 1688 SKU 人工審核摘要",
        "",
        f"- 來源報告：`{report_path}`",
        f"- 報告狀態：`{report.get('status', '')}`",
        f"- 需人工確認總數：{len(rows)}",
        "",
        "## 分類統計",
        "",
    ]
    for category, count in category_counts.most_common():
        lines.append(f"- {category}：{count}")
    lines.extend(["", "## 狀態統計", ""])
    for status, count in status_counts.most_common():
        lines.append(f"- {status}：{count}")
    lines.extend([
        "",
        "## 優先處理建議",
        "",
        "1. 先看 A 類：有抓到 1688 選項，只是規則沒自動對到，可直接在 CSV 的 `人工選擇1688SKU` 欄填一個選項。",
        "2. B 類通常是圖案或款式，不是單純顏色，需要人工判斷。",
        "3. C 類先不要填，因為頁面抓取失敗或沒有有效選項，等 1688 不擋程式後再重抓。",
        "",
        "## 需確認最多的商品",
        "",
    ])
    for product_id, count in product_counts.most_common(20):
        sample = next(row for row in rows if row.get("productId") == product_id)
        lines.append(f"- {product_id}：{count} 筆，{short_product_name(str(sample.get('productName', '')))}")
    lines.extend(["", "## 前 80 筆預覽", "", "|分類|商品ID|型號|1688可選項|原因|", "|---|---:|---|---|---|"])
    for row in rows[:80]:
        options = str(row.get("cleanColorOptions", "")).replace("|", "/")
        lines.append(
            f"|{row.get('reviewCategory', '')}|{row.get('productId', '')}|{row.get('modelName', '')}|{options}|{row.get('reason', '')}|"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_html(rows: List[Dict[str, Any]], report_path: Path, output_prefix: Path) -> Path:
    path = output_prefix.with_suffix(".html")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("productId", "")].append(row)
    category_counts = Counter(row["reviewCategory"] for row in rows)
    product_order = sorted(grouped, key=lambda product_id: (-len(grouped[product_id]), product_id))
    parts = [
        '<!doctype html><meta charset="utf-8"><title>1688 SKU Manual Review</title>',
        """<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif;margin:24px;background:#fafafa;color:#222}
h1{margin-bottom:8px}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:12px 14px;box-shadow:0 1px 2px #0001}
.product{background:#fff;border:1px solid #ddd;border-radius:12px;margin:14px 0;padding:12px}summary{cursor:pointer;font-weight:700}.muted{color:#777}.catA{border-left:6px solid #2e7d32}.catB{border-left:6px solid #ef6c00}.catC{border-left:6px solid #c62828}
table{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}th,td{border-top:1px solid #eee;padding:8px;vertical-align:top;text-align:left}th{background:#f5f5f5}.opts{max-width:520px}.reason{max-width:260px}.model{font-weight:650;white-space:nowrap}.url{font-size:12px}.pill{display:inline-block;border-radius:999px;padding:2px 8px;background:#eee;margin-right:4px}.A{background:#e8f5e9}.B{background:#fff3e0}.C{background:#ffebee}
</style>""",
        "<h1>1688 SKU 人工審核</h1>",
        f'<p class="muted">來源：{html.escape(str(report_path))}</p>',
        '<div class="summary">',
        f'<div class="card"><b>需人工確認</b><br>{len(rows)}</div>',
    ]
    for category, count in category_counts.most_common():
        class_name = "A" if category.startswith("A.") else "B" if category.startswith("B.") else "C"
        parts.append(f'<div class="card"><span class="pill {class_name}">{html.escape(category)}</span><br>{count}</div>')
    parts.append("</div>")
    parts.append("<p>建議先處理 A 類。C 類多半是頁面抓取失敗，先不要填，等 1688 不擋時再重抓。</p>")
    for product_id in product_order:
        group = grouped[product_id]
        sample = group[0]
        class_name = "catC" if any(row["reviewCategory"].startswith("C.") for row in group) else "catB" if any(row["reviewCategory"].startswith("B.") for row in group) else "catA"
        parts.append(f'<details class="product {class_name}"><summary>{html.escape(product_id)}｜{len(group)} 筆｜{html.escape(short_product_name(str(sample.get("productName", ""))))}</summary>')
        parts.append("<table><thead><tr><th>分類</th><th>蝦皮型號</th><th>1688 可選項</th><th>建議</th><th>原因</th><th>URL</th></tr></thead><tbody>")
        for row in group:
            options_html = html.escape(str(row.get("cleanColorOptions", ""))).replace(" / ", "<br>")
            parts.extend([
                "<tr>",
                f'<td>{html.escape(str(row.get("reviewCategory", "")))}</td>',
                f'<td class="model">{html.escape(str(row.get("modelName", "")))}</td>',
                f'<td class="opts">{options_html}</td>',
                f'<td>{html.escape(str(row.get("suggestedSkuName", "") or "-"))}</td>',
                f'<td class="reason">{html.escape(str(row.get("reason", "") or row.get("urlMessage", "")))}</td>',
                f'<td class="url"><a href="{html.escape(str(row.get("url", "")))}" target="_blank">1688</a><br>{html.escape(str(row.get("urlSource", "")))}</td>',
                "</tr>",
            ])
        parts.append("</tbody></table></details>")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="整理 1688 SKU needs-review 報告")
    parser.add_argument("report", help="alibaba_sku_mapping_report_*.json")
    parser.add_argument("--output-prefix", default="", help="輸出檔案前綴，不含副檔名")
    parser.add_argument("--include-non-socks", action="store_true", help="不要用商品名稱過濾襪子商品")
    args = parser.parse_args()

    report_path = Path(args.report).resolve()
    with report_path.open(encoding="utf-8") as f:
        report = json.load(f)

    output_prefix = Path(args.output_prefix).resolve() if args.output_prefix else report_path.with_name(
        report_path.stem.replace("alibaba_sku_mapping_report", "alibaba_sku_manual_review_socks")
    )
    rows = flatten_review_rows(report, socks_only=not args.include_non_socks)
    csv_path = write_csv(rows, output_prefix)
    md_path = write_markdown(rows, report, report_path, output_prefix)
    html_path = write_html(rows, report_path, output_prefix)

    print(f"CSV：{csv_path}")
    print(f"摘要：{md_path}")
    print(f"HTML：{html_path}")
    print(f"需人工確認：{len(rows)}")
    for category, count in Counter(row["reviewCategory"] for row in rows).most_common():
        print(f"{category}: {count}")


if __name__ == "__main__":
    main()
