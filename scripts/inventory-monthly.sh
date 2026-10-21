#!/usr/bin/env bash
# 唯讀包裝：跑 InventoryCalculator 的每月庫存與水位分析。
# 詳細產出寫到 /workspace/reports，不寫回程式庫。不加車、不改 golden、不重抓蝦皮。
set -u

REPO="/workspace/InventoryCalculator"
SCRIPT="${REPO}/scripts/monthly_inventory_report.py"
OUT_ROOT="/workspace/reports/inventory-monthly"
DETAIL="${OUT_ROOT}/pipeline"
MIN_KIB=$((2 * 1024 * 1024))
MONTH_NOW="$(TZ=Asia/Taipei date +%Y-%m)"

mkdir -p "${OUT_ROOT}"

write_alert() {
  local reason="$1"
  local md="${OUT_ROOT}/${MONTH_NOW}.md"
  python3 - "${md}" "${MONTH_NOW}" "${reason}" <<'PY'
import sys
from pathlib import Path
path, month, reason = sys.argv[1:]
Path(path).write_text(
    f"# {month} 每月庫存摘要\n\n狀態：未完成。\n\n{reason}\n\n沒有補貨、沒有加車、沒有改對照表。\n",
    encoding="utf-8",
)
print(path)
PY
  printf '%s\n' "報告：${md}"
  printf '%s\n' "STATUS=ALERT"
  printf '%s\n' "${reason}"
  printf '%s\n' "沒有補貨，也沒有改庫存或對照表。"
  exit 1
}

mem_kib="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
if [[ ! "${mem_kib}" =~ ^[0-9]+$ ]] || (( mem_kib <= MIN_KIB )); then
  write_alert "可用記憶體不到 2 GiB，這次沒有跑每月庫存分析。"
fi

if [[ ! -f "${SCRIPT}" ]]; then
  write_alert "找不到每月庫存分析程式，這次沒有計算。"
fi

log="$(mktemp)"
err="$(mktemp)"
set +e
timeout 180 python3 "${SCRIPT}" --out "${DETAIL}" >"${log}" 2>"${err}"
rc=$?
set -u

python3 - "${OUT_ROOT}" "${DETAIL}" "${rc}" "${err}" "${MONTH_NOW}" <<'PY'
import json
import sys
from pathlib import Path

out_root, detail, rc_text, err_path, month_now = sys.argv[1:]
rc = int(rc_text)
detail_path = Path(detail)
report_json = detail_path / "monthly_report.json"
signal_md = detail_path / "watchlist_stock_signal.md"

def plain_error():
    text = Path(err_path).read_text(encoding="utf-8", errors="replace") if Path(err_path).is_file() else ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("錯誤："):
            message = line.split("：", 1)[1].strip()
            lowered = message.lower()
            if any(word in lowered for word in ("cookie", "token", "password", "secret", "bearer")):
                return "每月庫存分析失敗，錯誤訊息已略過。"
            return message or "每月庫存分析失敗。"
    if rc == 124:
        return "每月庫存分析超過時限，已停止。"
    return "每月庫存分析沒有完成。"

def fmt(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"

status = "ALERT"
reason = plain_error()
period = month_now
summary = {}
signal_line = ""
first_batch = []
full_md = ""
if rc == 0 and report_json.is_file():
    try:
        payload = json.loads(report_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("summary"), dict):
        summary = payload["summary"]
        raw_period = str(payload.get("period") or "")
        if len(raw_period) == 6 and raw_period.isdigit():
            period = f"{raw_period[:4]}-{raw_period[4:]}"
        status = "OK"
        data_time = str(payload.get("data_time") or "")
        reason = (
            f"已用現成月報算完 {period}。"
            f"危急型號 {fmt(summary.get('critical_specs'))} 個、"
            f"建議 {fmt(summary.get('critical_suggested_qty'))} 件。"
        )
        if data_time:
            reason += f"商品檔時間 {data_time}。"
        batch = payload.get("first_batch")
        if isinstance(batch, list):
            first_batch = [row for row in batch if isinstance(row, dict)][:12]
        note = payload.get("stock_signal") if isinstance(payload.get("stock_signal"), dict) else {}
        if note.get("ran") is True:
            signal_line = "水位訊號已一併算出，完整內容在同一個輸出目錄。"
        elif note.get("note"):
            signal_line = "水位訊號這次沒有完成，月報數字仍在。"
        if signal_md.is_file():
            for line in signal_md.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("結論："):
                    signal_line = line.strip()
                    break
        full_md = str(detail_path / "monthly_report.md")

md_path = Path(out_root) / f"{period}.md"
lines = [f"# {period} 每月庫存摘要", "", reason, ""]
if status == "OK":
    lines.extend([
        "這次只讀現成商品檔、觀察清單與對照表，沒有重抓蝦皮、沒有重抓 1688、沒有加車、沒有改對照表。",
        "",
        f"- 觀察清單抓到 {fmt(summary.get('matched'))} 個商品，有賣出的型號 {fmt(summary.get('selling_specs'))} 個。",
        f"- 危急型號 {fmt(summary.get('critical_specs'))} 個，建議 {fmt(summary.get('critical_suggested_qty'))} 件。",
        f"- 斷貨 {fmt(summary.get('oos_specs'))} 個型號、{fmt(summary.get('oos_suggested_qty'))} 件。",
        f"- 第一批（斷貨且建議至少 20 件）{fmt(summary.get('first_batch_specs'))} 個型號、{fmt(summary.get('first_batch_suggested_qty'))} 件。",
        f"- 補到目標水位一共 {fmt(summary.get('target_gap_qty'))} 件（{fmt(summary.get('target_gap_specs'))} 個規格）。",
        f"- 手機殼危急 {fmt(summary.get('phone_case_critical'))} 個，吊飾或掛繩危急 {fmt(summary.get('charm_critical'))} 個，其餘 {fmt(summary.get('other_critical'))} 個。",
    ])
    if signal_line:
        lines.append(f"- {signal_line}")
    if full_md:
        lines.extend(["", f"完整月報：`{full_md}`"])
    if first_batch:
        lines.extend(["", "第一批前幾筆：", ""])
        for row in first_batch:
            name = str(row.get("product_name") or "").replace("\n", " ")
            model = str(row.get("model_name") or "").replace("\n", " ")
            lines.append(
                f"- {name}／{model}：建議 {fmt(row.get('suggested_qty'))} 件"
                f"（月銷 {row.get('monthly_sales')}，庫存 {row.get('stock')}）。"
            )
else:
    lines.append("沒有寫出新的水位建議，也沒有改任何庫存檔。")
lines.append("")
md_path.write_text("\n".join(lines), encoding="utf-8")
PY
summary_rc=$?
rm -f "${log}" "${err}"

if [[ "${summary_rc}" -ne 0 ]]; then
  write_alert "每月庫存摘要沒有寫成。"
fi

md_written="$(python3 -c '
import sys
from pathlib import Path
root = Path(sys.argv[1])
files = sorted(root.glob("20??-??.md"), key=lambda path: path.stat().st_mtime)
print(files[-1] if files else "")
' "${OUT_ROOT}")"

if [[ -z "${md_written}" || ! -f "${md_written}" ]]; then
  write_alert "每月庫存摘要沒有寫成。"
fi

status_line="ALERT"
if [[ "${rc}" -eq 0 && -f "${DETAIL}/monthly_report.json" ]]; then
  status_line="OK"
fi
reason_line="$(sed -n '3p' "${md_written}")"

printf '%s\n' "報告：${md_written}"
if [[ "${status_line}" == "OK" ]]; then
  printf '%s\n' "STATUS=OK"
  printf '%s\n' "${reason_line}"
  printf '%s\n' "水位與月報都只讀現成檔案，沒有補貨、沒有加車。"
  exit 0
fi
printf '%s\n' "STATUS=ALERT"
printf '%s\n' "${reason_line:-每月庫存分析沒有完成。}"
printf '%s\n' "沒有補貨，也沒有改庫存或對照表。"
exit 1
