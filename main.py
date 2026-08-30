import sys
import subprocess
import os
import re
import importlib.metadata

def ensure_dependencies():
    """檢查並安裝缺失的依賴套件"""
    # 獲取腳本所在目錄及 requirements.txt 路徑
    base_path = os.path.dirname(os.path.abspath(__file__))
    requirements_file = os.path.join(base_path, "requirements.txt")
    
    if not os.path.exists(requirements_file):
        return

    try:
        with open(requirements_file, "r", encoding="utf-8") as f:
            packages = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except Exception as e:
        print(f"讀取 requirements.txt 時出錯: {e}")
        return

    missing_packages = []
    for package in packages:
        # 解析套件名稱（處理版本號，例如 requests>=2.25.1 -> requests）
        package_name = re.split(r'[<>=!]', package)[0].strip()
        
        # 特殊映射（如果有的話）
        # pyinstaller 的 metadata 名稱就是 pyinstaller (小寫)
        
        try:
            importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            missing_packages.append(package)

    if missing_packages:
        print("\n" + "="*50)
        print(f"偵測到缺失的套件: {', '.join(missing_packages)}")
        print("正在嘗試自動安裝，這可能需要幾分鐘時間...")
        print("="*50 + "\n")
        
        try:
            # 使用當前 Python 解析器執行 pip
            subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing_packages)
            print("\n✅ 套件安裝成功。")
            
            # Playwright 額外處理
            if any("playwright" in p.lower() for p in missing_packages):
                print("偵測到 Playwright，正在安裝 Chromium 瀏覽器...")
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
                print("✅ Playwright 瀏覽器安裝完成。")
                
        except subprocess.CalledProcessError as e:
            print(f"\n❌ 自動安裝過程出錯: {e}")
            print("請手動執行: pip install -r requirements.txt")
            print("="*50 + "\n")
            # 繼續嘗試執行，但也許會因導入失敗而崩潰
        except Exception as e:
            print(f"\n❌ 發生非預期錯誤: {e}")

# 立即執行依賴檢查
ensure_dependencies()

# 原有導入
import http.server
import socketserver
import webbrowser
import threading
import time
import json
import urllib.parse
import tempfile
import signal
import psutil  # 需要安裝: pip install psutil
import atexit
import socket
import logging
import datetime
import shutil
import uuid
from pathlib import Path
from alibaba_client import AlibabaApiClient
from alibaba_review_report import clean_options, classify, is_sock_product_name
from procurement_store import ProcurementStore, parse_offer_id
from inbound_store import InboundStore
from product_catalog import build_product_catalog
from ads_analysis import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    OPENAI_MODEL_OPTIONS,
    OPENAI_REASONING_EFFORTS,
    validate_openai_model,
    validate_reasoning_effort,
)
from config_loader import load_openai_api_key, load_openai_config_value
from cookie_import import MAX_COOKIE_IMPORT_BYTES, save_shopee_cookies
from shopee_products_import import (
    MAX_SHOPEE_PRODUCTS_IMPORT_BYTES,
    merge_shopee_products_with_golden,
    replace_shopee_products,
    validate_shopee_products,
)
from sku_mapping_service import MappingConflict, SkuMappingService, mapping_candidate_key, prune_golden_table_backups
from golden_import import apply_import_mapping, preview_models, source_product_candidates
from housekeeping import remove_files, remove_stale_matching_files
from restock_rules import resolve_restock_quantity, validate_restock_sku_count
from home_bootstrap import load_home_bootstrap
from restock_batch import (
    STATUS_RUNNING,
    begin_run,
    build_preview,
    build_report,
    create_state,
    extract_failure_records,
    find_current_batch,
    load_state,
    product_items,
    public_state,
    recover_interrupted_batches,
    render_report_html,
    resume_state,
    run_batch_loop,
    save_state,
    write_failure_artifacts,
    write_job_artifact,
    write_reports,
)

# 導入版本管理


def normalize_identifier(value):
    """將 Excel 讀出的 ID 正規化成不帶 .0 的字串。"""
    text = str(value).strip()
    if text in ("", "nan", "None"):
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text
    return text


def requires_alibaba_second_sku(product_name, model_name):
    """手機殼等雙規格商品必須同時指定樣式與機型，不能只選第一規格。"""
    parts = [part.strip() for part in re.split(r"[,，]", str(model_name or "")) if part.strip()]
    if len(parts) < 2:
        return False
    return bool(
        re.search(r"(手機殼|手机壳|iphone|ipad)", str(product_name or ""), re.IGNORECASE) and
        re.match(r"^(?:iphone)?(?:\d{1,2}|x(?:r|s(?:\s*max)?)?|se\d*)", parts[1], re.IGNORECASE)
    )


def is_alibaba_sku_discontinued(value):
    """停售標記不可當作實際 1688 SKU 送進補貨流程。"""
    return str(value or "").strip().lower() in {"停售", "已停售", "以後不賣了", "以后不卖了"}
from version import check_for_updates, CURRENT_VERSION

# 設置日誌記錄
LOG_FILE = "debug.log"

def get_resource_path(relative_path):
    """獲取資源文件的絕對路徑，支持開發環境和 PyInstaller 打包環境"""
    try:
        # PyInstaller 創建的臨時文件夾
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

# 如果是 worker 模式，直接執行爬蟲邏輯
if len(sys.argv) > 1 and sys.argv[1] == '--worker':
    # 設置環境變量以確保 crawler 能正確找到資源
    # if getattr(sys, 'frozen', False):
    #     os.chdir(sys._MEIPASS)
    
    # 避免循環導入
    import crawler
    # 移除 --worker 參數，讓 crawler 的 argparse 能正常工作
    sys.argv.pop(1)
    crawler.main()
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == '--inbound-worker':
    import inbound_worker
    sys.argv.pop(1)
    inbound_worker.main()
    sys.exit(0)

# 檢查是否存在舊的日誌文件，如果存在則刪除
if os.path.exists(LOG_FILE):
    try:
        os.remove(LOG_FILE)
        print(f"已刪除舊的日誌文件: {LOG_FILE}")
    except Exception as e:
        print(f"刪除舊日誌文件時出錯: {e}")

# 設置日誌配置，使用 'w' 模式創建新文件
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w',
                            encoding='utf-8'),  # 使用 'w' 模式而不是 'a'
        logging.StreamHandler()  # 同時輸出到控制台
    ])
logger = logging.getLogger(__name__)


def build_openai_status():
    """回傳可安全顯示在前端的 OpenAI 設定，不包含 API Key 本身。"""
    api_key, key_source = load_openai_api_key()
    configured_model, model_source = load_openai_config_value(
        "OPENAI_MODEL", DEFAULT_OPENAI_MODEL
    )
    configured_effort, effort_source = load_openai_config_value(
        "OPENAI_REASONING_EFFORT", DEFAULT_OPENAI_REASONING_EFFORT
    )
    config_errors = []
    try:
        configured_model = validate_openai_model(configured_model)
    except ValueError as exc:
        config_errors.append(str(exc))
        configured_model = DEFAULT_OPENAI_MODEL
    try:
        configured_effort = validate_reasoning_effort(configured_effort)
    except ValueError as exc:
        config_errors.append(str(exc))
        configured_effort = DEFAULT_OPENAI_REASONING_EFFORT
    return {
        "status": "success",
        "configured": bool(api_key),
        "key_source": key_source or "not_configured",
        "default_model": configured_model,
        "model_source": model_source,
        "default_reasoning_effort": configured_effort,
        "reasoning_effort_source": effort_source,
        "models": OPENAI_MODEL_OPTIONS,
        "reasoning_efforts": OPENAI_REASONING_EFFORTS,
        "setup_command": "python3 setup_openai_key.py",
        "config_errors": config_errors,
    }

# 記錄啟動信息
logger.info(
    f"===== 程序啟動於 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====="
)
logger.info(f"操作系統: {os.name}, Python版本: {sys.version}")

PORT = 8080  # 改為其他未被使用的端口，如 8080, 8888, 9000 等
FILE_NAME = get_resource_path("index.html")
current_crawler_process = None
ALIBABA_RESTOCK_SESSION_CLOSED_MARKER = "__INVENTORY_1688_SESSION_CLOSED__"


def restock_pause_seconds(payload):
    """0 means close the 1688 window when done. Do not treat 0 as missing."""
    raw = payload.get("pauseSeconds") if isinstance(payload, dict) else None
    if raw is None or raw == "":
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


alibaba_restock_jobs = {}
alibaba_restock_jobs_lock = threading.Lock()
restock_batch_lock = threading.Lock()
restock_batch_runtime = {"runId": None, "thread": None, "stop": False}
inbound_jobs = {}
inbound_jobs_lock = threading.Lock()
sku_mapping_service = None
sku_mapping_service_lock = threading.Lock()

removed_stale_temp_files = remove_stale_matching_files(
    tempfile.gettempdir(),
    (
        "inventory_inbound_*_input.json",
        "inventory_inbound_*_output.json",
        "inventory_inbound_*_status.json",
        "inventory_alibaba_restock_*.json",
        "alibaba_restock_result_*.json",
    ),
    older_than_seconds=24 * 60 * 60,
)
if removed_stale_temp_files:
    logger.info("已清除 %s 個超過一天的專案暫存檔", len(removed_stale_temp_files))

# 確保 index.html 存在 (僅在非打包環境檢查，或確保打包時已包含)
if not os.path.exists(FILE_NAME) and not getattr(sys, 'frozen', False):
    with open(FILE_NAME, "w", encoding="utf-8") as f:
        f.write("<h1>伺服器運行中！</h1>")
    logger.info(f"創建了 {FILE_NAME} 文件")


# 自定義處理器
class CustomHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        request_path = parsed_path.path

        if request_path == '/api/golden-table/import/candidates':
            try:
                golden_path = self._golden_table_path()
                source_path = golden_path.with_name("shopee_products.json")
                golden_table = self._load_json_file(golden_path)
                source_table = self._load_json_file(source_path) if source_path.exists() else {}
                candidates = source_product_candidates(source_table, golden_table)
                self._send_json_response(200, {
                    "status": "success",
                    "sourceCount": len(source_table),
                    "goldenCount": len(golden_table),
                    "candidateCount": len(candidates),
                    "candidates": candidates,
                    "sourceFile": str(source_path),
                    "message": (
                        "請先執行一次蝦皮搜尋／更新，讓 shopee_products.json 取得最新商品"
                        if not source_table else ""
                    ),
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"載入新增商品候選失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/summary':
            try:
                self._send_json_response(200, self._sku_mapping_store().summary())
            except Exception as e:
                logger.exception(f"載入 SKU mapping 摘要失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/url-groups':
            try:
                params = urllib.parse.parse_qs(parsed_path.query)
                self._send_json_response(200, self._sku_mapping_store().url_groups(
                    query=params.get("query", [""])[0],
                    status=params.get("status", ["all"])[0],
                    mapping_status=params.get("mappingStatus", [""])[0],
                    link_status=params.get("linkStatus", ["all"])[0],
                ))
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"載入 1688 URL 群組失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/queue':
            try:
                params = urllib.parse.parse_qs(parsed_path.query)
                self._send_json_response(200, self._sku_mapping_store().queue(
                    status=params.get("status", ["review"])[0],
                    query=params.get("query", [""])[0],
                    restock_only=params.get("restockOnly", ["false"])[0].lower() == "true",
                    offer_id=params.get("offerId", [""])[0],
                    tier=params.get("tier", [""])[0],
                    url_presence=params.get("urlPresence", ["with"])[0],
                    page=int(params.get("page", ["1"])[0]),
                    page_size=int(params.get("pageSize", ["50"])[0]),
                ))
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"載入 SKU mapping queue 失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/catalog':
            try:
                params = urllib.parse.parse_qs(parsed_path.query)
                product_id = params.get("productId", [""])[0]
                model_id = params.get("modelId", [""])[0]
                self._send_json_response(200, self._sku_mapping_store().catalog_for_model(product_id, model_id))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"載入 SKU catalog 失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        mapping_job_match = re.match(r'^/api/sku-mapping/jobs/([^/]+)$', request_path)
        if mapping_job_match:
            try:
                self._send_json_response(200, self._sku_mapping_store().job(mapping_job_match.group(1)))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/home/bootstrap':
            try:
                self._send_json_response(200, self._home_bootstrap())
            except Exception as e:
                logger.exception("載入首頁資料失敗: %s", e)
                self._send_json_response(500, {
                    "status": "error",
                    "message": "載入首頁資料失敗，請檢查 shopee_products.json 與觀察清單",
                    "batchRestockEnabled": False,
                    "products": None,
                })
            return

        if request_path == '/api/alibaba-restock/batches/current':
            try:
                self._send_json_response(200, self._current_restock_batch())
            except Exception as e:
                logger.exception("讀取目前批次補貨失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        restock_report_html_match = re.match(
            r'^/api/alibaba-restock/batches/([^/]+)/report\.html$',
            request_path,
        )
        if restock_report_html_match:
            try:
                self._send_html_response(200, self._restock_batch_report_html(restock_report_html_match.group(1)))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception("讀取批次補貨 HTML 報告失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        restock_report_match = re.match(
            r'^/api/alibaba-restock/batches/([^/]+)/report$',
            request_path,
        )
        if restock_report_match:
            try:
                self._send_json_response(200, self._restock_batch_report(restock_report_match.group(1)))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception("讀取批次補貨報告失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        restock_batch_match = re.match(r'^/api/alibaba-restock/batches/([^/]+)$', request_path)
        if restock_batch_match:
            try:
                self._send_json_response(200, {
                    "status": "success",
                    "batch": public_state(self._load_restock_batch(restock_batch_match.group(1))),
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception("讀取批次補貨失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        restock_job_match = re.match(r'^/api/alibaba-restock/jobs/([a-f0-9]+)$', request_path)
        if restock_job_match:
            try:
                self._send_json_response(200, self._read_alibaba_restock_job(restock_job_match.group(1)))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            return
        if request_path == '/api/inbound/status':
            self._send_json_response(200, self._inbound_status())
            return

        inbound_job_match = re.match(r'^/api/inbound/jobs/([a-f0-9]+)$', request_path)
        if inbound_job_match:
            try:
                self._send_json_response(200, self._read_inbound_job(inbound_job_match.group(1)))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/inbound/orders':
            self._send_json_response(200, {
                "status": "success",
                "orders": self._inbound_store().list_orders(),
            })
            return

        inbound_order_match = re.match(r'^/api/inbound/orders/(\d+)$', request_path)
        if inbound_order_match:
            try:
                self._send_json_response(
                    200, self._inbound_store().get_order(int(inbound_order_match.group(1)))
                )
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            return

        inbound_receipt_match = re.match(r'^/api/inbound/receipts/(\d+)$', request_path)
        if inbound_receipt_match:
            try:
                self._send_json_response(
                    200, self._inbound_store().get_receipt(int(inbound_receipt_match.group(1)))
                )
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/inbound/shopee-models':
            params = urllib.parse.parse_qs(parsed_path.query)
            query = params.get("query", [""])[0]
            self._send_json_response(200, {
                "status": "success",
                "models": self._inbound_store().catalog_models(query=query),
            })
            return

        if request_path == '/api/alibaba/sku-review/reports':
            try:
                self._send_json_response(200, self._list_sku_review_reports())
            except Exception as e:
                logger.exception(f"載入 1688 SKU review reports 失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if request_path == '/api/alibaba/sku-review':
            try:
                params = urllib.parse.parse_qs(parsed_path.query)
                report_name = params.get("report", ["latest"])[0]
                filter_name = params.get("filter", ["review"])[0]
                self._send_json_response(200, self._load_sku_review(report_name, filter_name))
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"載入 1688 SKU review 失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/alibaba/auth/status':
            try:
                self._send_json_response(200, self._alibaba_client().auth_status())
            except Exception as e:
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/alibaba/bindings':
            try:
                self._send_json_response(200, self._list_alibaba_bindings())
            except Exception as e:
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if request_path == '/api/golden-table/catalog':
            try:
                params = urllib.parse.parse_qs(parsed_path.query)
                query = params.get("query", [""])[0]
                limit = int(params.get("limit", ["30"])[0])
                golden_table = self._load_json_file(self._golden_table_path())
                self._send_json_response(200, {
                    "status": "success",
                    **build_product_catalog(golden_table, query=query, limit=limit),
                })
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"載入商品資料目錄失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if self.path.startswith('/api/procurement/drafts/'):
            try:
                match = re.match(r'^/api/procurement/drafts/(\d+)$', self.path)
                if not match:
                    self._send_json_response(404, {
                        "status": "error",
                        "message": "找不到採購草稿 API"
                    })
                    return
                draft = self._procurement_store().get_draft(int(match.group(1)))
                self._send_json_response(200, draft)
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/procurement/orders':
            try:
                self._send_json_response(200, {
                    "status": "success",
                    "orders": self._procurement_store().list_orders()
                })
            except Exception as e:
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        # 處理搜尋請求
        if self.path.startswith('/search'):
            try:
                # 解析查詢參數
                query = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(query)
                keyword = params.get('keyword', [''])[0]
                showBrowser = params.get('showBrowser',
                                         ['false'])[0].lower() == 'true'
                inventoryMonth = int(params.get('inventoryMonth', ['4'])[0])

                # 執行爬蟲並獲取結果，無論關鍵字是否為空
                result = self.run_crawler(keyword, showBrowser, inventoryMonth)

                # 設置響應頭
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()

                # 發送JSON響應
                self.wfile.write(
                    json.dumps(result, ensure_ascii=False).encode('utf-8'))

                return
            except Exception as e:
                # 處理錯誤
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "error": str(e)
                    }, ensure_ascii=False).encode('utf-8'))
                return

        if self.path.startswith('/export_ads'):
            try:
                showBrowser = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                ).get('showBrowser', ['true'])[0].lower() == 'true'

                result = self.run_ads_export(showBrowser)

                status_code = 200 if result.get("status") in ("success", "partial_success") else 500
                self.send_response(status_code)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps(result, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "status": "error",
                        "message": str(e)
                    }, ensure_ascii=False).encode('utf-8'))
                return

        if self.path.startswith('/openai_status'):
            try:
                result = build_openai_status()
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "error",
                    "message": str(e),
                }, ensure_ascii=False).encode('utf-8'))
                return

        if self.path.startswith('/analyze_ads'):
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                include_ai = query.get('includeAI', ['true'])[0].lower() == 'true'
                model = validate_openai_model(query.get('model', [''])[0])
                reasoning_effort = validate_reasoning_effort(
                    query.get('reasoningEffort', [''])[0]
                )

                if include_ai and not build_openai_status()["configured"]:
                    raise RuntimeError(
                        "尚未設定 OpenAI API Key。請先在專案終端執行 "
                        "python3 setup_openai_key.py；Key 不要貼到網頁或聊天中。"
                    )

                result = self.run_ads_analysis(
                    include_ai=include_ai,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )

                status_code = 200 if result.get("status") == "success" else 500
                self.send_response(status_code)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps(result, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "status": "error",
                        "message": str(e)
                    }, ensure_ascii=False).encode('utf-8'))
                return

        # 處理阿里巴巴連結查詢
        if self.path == '/api/alibaba-links':
            try:
                links_map = self._load_alibaba_links()
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(
                    json.dumps(links_map, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "status": "error",
                        "message": str(e)
                    }, ensure_ascii=False).encode('utf-8'))
                return

        # 處理中斷爬蟲請求
        if self.path == '/stop_crawler':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()

            # 嘗試中斷爬蟲
            success = self.stop_running_crawler()

            # 發送JSON響應
            response = {
                "status": "success" if success else "failed",
                "message": "爬蟲已中斷" if success else "中斷爬蟲失敗"
            }
            self.wfile.write(
                json.dumps(response, ensure_ascii=False).encode('utf-8'))
            return

        # 處理關閉請求
        if self.path == '/shutdown':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Server shutting down...")
            print("收到關閉請求，程式即將結束...")
            # 使用線程在回應後關閉伺服器
            threading.Thread(target=self.shutdown_server, daemon=True).start()
            return

        # 處理其他請求
        # 處理靜態文件與首頁請求
        if self.path == '/' or any(self.path.endswith(ext) for ext in ['.html', '.css', '.js', '.pdf', '.json', '.md']):
            if self.path == '/':
                target_file = FILE_NAME
            else:
                # 移除開頭的 /，並獲取絕對資源路徑
                target_file = get_resource_path(self.path.lstrip('/'))
            
            if os.path.exists(target_file):
                self.send_response(200)
                if target_file.endswith('.css'):
                    self.send_header('Content-type', 'text/css; charset=utf-8')
                elif target_file.endswith('.js'):
                    self.send_header('Content-type', 'application/javascript; charset=utf-8')
                elif target_file.endswith('.pdf'):
                    self.send_header('Content-type', 'application/pdf')
                elif target_file.endswith('.json'):
                    self.send_header('Content-type', 'application/json; charset=utf-8')
                elif target_file.endswith('.md'):
                    self.send_header('Content-type', 'text/markdown; charset=utf-8')
                else:
                    self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                
                try:
                    with open(target_file, 'rb') as f:
                        self.wfile.write(f.read())
                except Exception as e:
                    logger.error(f"讀取文件失敗: {e}")
                return
            
        return http.server.SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        request_path = urllib.parse.urlparse(self.path).path

        if request_path == '/api/shopee-products/import':
            try:
                data = self._read_json_body(MAX_SHOPEE_PRODUCTS_IMPORT_BYTES)
                validate_shopee_products(data)
                golden_table = self._load_json_file(self._golden_table_path())
                merged_products, match_counts = merge_shopee_products_with_golden(
                    data, golden_table
                )
                replace_shopee_products(self._shopee_products_path(), merged_products)
                self._send_json_response(200, {
                    "status": "success",
                    "message": "已匯入並合併最新 shopee_products.json；未執行 crawler",
                    **match_counts,
                    "products": merged_products,
                })
            except json.JSONDecodeError:
                self._send_json_response(400, {
                    "status": "error",
                    "message": "匯入內容不是有效的 JSON 檔案",
                })
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except OSError:
                logger.exception("寫入 shopee_products.json 失敗")
                self._send_json_response(500, {
                    "status": "error",
                    "message": "shopee_products.json 寫入失敗，原檔未完成替換",
                })
            except Exception as e:
                logger.exception(f"匯入 shopee_products.json 失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": "匯入商品資料失敗，請檢查檔案內容或伺服器日誌",
                })
            return

        if request_path == '/api/cookies/import':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length > MAX_COOKIE_IMPORT_BYTES + 100_000:
                    raise ValueError("Cookie 文字太大，請確認貼上的內容是否正確")
                data = self._read_json_body()
                result = save_shopee_cookies(
                    os.path.dirname(os.path.abspath(__file__)),
                    data.get("cookiesText", ""),
                )
                self._send_json_response(200, result)
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except OSError:
                logger.exception("寫入 Cookie 檔案失敗")
                self._send_json_response(500, {"status": "error", "message": "Cookie 檔案寫入失敗"})
            except Exception:
                logger.exception("匯入 Cookie 失敗")
                self._send_json_response(500, {"status": "error", "message": "Cookie 匯入失敗"})
            return

        if request_path == '/api/golden-table/import/preview':
            try:
                data = self._read_json_body()
                product_id = str(data.get("productId") or "").strip()
                alibaba_url = str(data.get("alibabaProductUrl") or "").strip()
                if not product_id:
                    raise ValueError("缺少蝦皮商品 ID；請先從候選清單選擇商品")
                if not re.match(r'^https?://', alibaba_url, re.IGNORECASE):
                    raise ValueError("1688 商品網址必須以 http:// 或 https:// 開頭")

                golden_path = self._golden_table_path()
                source_path = golden_path.with_name("shopee_products.json")
                golden_table = self._load_json_file(golden_path)
                source_table = self._load_json_file(source_path)
                if product_id in golden_table:
                    raise ValueError(f"商品 {product_id} 已存在於 golden table")
                source_product = source_table.get(product_id)
                if not isinstance(source_product, dict):
                    raise FileNotFoundError(
                        f"找不到商品 {product_id}；請先執行蝦皮搜尋／更新資料"
                    )

                service = self._sku_mapping_store()
                offer_id = parse_offer_id(alibaba_url)
                if not offer_id:
                    raise ValueError("1688 商品網址中找不到 offer ID")
                snapshot = service._get_cached_snapshot(offer_id, False)
                if snapshot is None:
                    preview_job_id = f"golden-import-preview-{int(time.time())}-{os.getpid()}"
                    snapshot = service._fetch_live_snapshot(alibaba_url, offer_id, preview_job_id)
                if snapshot.get("status") != "ok":
                    raise RuntimeError(
                        str(snapshot.get("error_message") or snapshot.get("status") or "1688 SKU 讀取失敗")
                    )

                models = preview_models(source_product, snapshot, service)
                self._send_json_response(200, {
                    "status": "success",
                    "productId": product_id,
                    "product": {
                        "productName": str(source_product.get("商品名稱") or ""),
                        "productImageUrl": str(source_product.get("商品圖片網址") or ""),
                        "modelCount": len(models),
                    },
                    "snapshot": {
                        "offerId": str(snapshot.get("offer_id") or offer_id),
                        "productName": str(snapshot.get("product_name") or ""),
                        "productUrl": str(snapshot.get("product_url") or alibaba_url),
                        "fingerprint": str(snapshot.get("fingerprint") or ""),
                        "skuCount": len(snapshot.get("skus") or []),
                    },
                    "models": models,
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(502, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"建立新增商品預覽失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/golden-table/import/commit':
            try:
                data = self._read_json_body()
                product_id = str(data.get("productId") or "").strip()
                offer_id = str(data.get("offerId") or "").strip()
                fingerprint = str(data.get("fingerprint") or "").strip()
                mappings = data.get("mappings")
                if not product_id or not offer_id or not isinstance(mappings, list):
                    raise ValueError("缺少商品 ID、1688 offer ID 或規格對應")

                golden_path = self._golden_table_path()
                source_path = golden_path.with_name("shopee_products.json")
                golden_table = self._load_json_file(golden_path)
                source_table = self._load_json_file(source_path)
                if product_id in golden_table:
                    raise ValueError(f"商品 {product_id} 已存在於 golden table，未覆蓋既有資料")
                source_product = source_table.get(product_id)
                if not isinstance(source_product, dict):
                    raise FileNotFoundError(f"找不到商品 {product_id}；請先更新蝦皮資料")

                service = self._sku_mapping_store()
                catalog = service._snapshot_catalog(offer_id=offer_id)
                if catalog.get("catalogStatus") != "ok":
                    raise ValueError("找不到可用的 1688 SKU 快照，請重新預覽")
                if fingerprint and fingerprint != str(catalog.get("fingerprint") or ""):
                    raise ValueError("1688 商品資料已更新，請重新預覽後再寫入")
                snapshot = {
                    "offer_id": str(catalog.get("offerId") or offer_id),
                    "product_name": str(catalog.get("productName") or data.get("alibabaProductName") or ""),
                    "product_url": str(catalog.get("productUrl") or data.get("alibabaProductUrl") or ""),
                    "fingerprint": str(catalog.get("fingerprint") or ""),
                    "skus": catalog.get("skus") or [],
                }
                product = apply_import_mapping(source_product, snapshot, mappings)
                backup_path = self._write_golden_table_import(golden_path, golden_table, product_id, product)

                try:
                    service.migrate_legacy_mappings()
                except Exception:
                    logger.exception("新增商品後同步 SKU mapping 索引失敗")
                for model in product.get("型號", []) or []:
                    self._procurement_store().upsert_binding({
                        "productId": product_id,
                        "modelId": str(model.get("規格ID") or model.get("型號名稱") or ""),
                        "productName": product.get("商品名稱", ""),
                        "modelName": model.get("型號名稱", ""),
                        "alibabaProductName": model.get("阿里巴巴商品名稱", ""),
                        "alibabaProductUrl": model.get("阿里巴巴商品URL", ""),
                        "alibabaOfferId": model.get("1688_offer_id", ""),
                        "alibabaSkuId": model.get("1688_sku_id", ""),
                        "alibabaSkuName": model.get("1688_sku_name", ""),
                        "alibabaSkuSecondName": model.get("1688_sku_second_name", ""),
                        "alibabaSpecText": model.get("1688_spec_text", ""),
                        "alibabaMappingStatus": model.get("1688_mapping_status", "approved"),
                        "alibabaOfferFingerprint": model.get("1688_offer_fingerprint", ""),
                    })
                self._send_json_response(200, {
                    "status": "success",
                    "productId": product_id,
                    "modelCount": len(product.get("型號", []) or []),
                    "backupPath": str(backup_path),
                    "message": "新商品已寫入 golden table",
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"寫入新增商品失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/scans':
            try:
                data = self._read_json_body()
                job = self._sku_mapping_store().start_scan(
                    scope=data.get("scope", "all"),
                    force=bool(data.get("force")),
                    use_ai=data.get("useAi", True) is not False,
                    rebuild=data.get("rebuild", False) is True,
                    product_id=data.get("productId", ""),
                    model_id=data.get("modelId", ""),
                    offer_id=data.get("offerId", ""),
                    targets=data.get("targets") if isinstance(data.get("targets"), list) else None,
                )
                self._send_json_response(202, {"status": "success", **job})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(409, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"啟動 SKU mapping 掃描失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/url-health-checks':
            try:
                data = self._read_json_body()
                targets = data.get("targets") if isinstance(data.get("targets"), list) else []
                job = self._sku_mapping_store().start_url_health_check(targets)
                self._send_json_response(202, {"status": "success", **job})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(409, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"啟動 1688 URL 健康檢查失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/url-changes/preview':
            try:
                data = self._read_json_body()
                result = self._sku_mapping_store().preview_url_change(
                    product_id=data.get("productId", ""),
                    model_id=data.get("modelId", ""),
                    new_url=data.get("newUrl", ""),
                    mode=data.get("mode", "replace"),
                )
                self._send_json_response(200, result)
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(502, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"預覽 1688 URL 更新失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/url-changes/commit':
            try:
                data = self._read_json_body()
                result = self._sku_mapping_store().commit_url_change(
                    product_id=data.get("productId", ""),
                    model_id=data.get("modelId", ""),
                    source_version=data.get("sourceVersion", ""),
                    models=data.get("models") if isinstance(data.get("models"), list) else [],
                    new_url=data.get("newUrl", ""),
                    snapshot_fingerprint=data.get("snapshotFingerprint", ""),
                    mode=data.get("mode", "replace"),
                    reviewer=str(data.get("reviewer") or "local_user"),
                )
                self._send_json_response(200, result)
            except MappingConflict as e:
                self._send_json_response(409, {"status": "conflict", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"提交 1688 URL 更新失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/reanalyze-existing':
            try:
                data = self._read_json_body()
                job = self._sku_mapping_store().start_snapshot_reanalysis(
                    use_ai=data.get("useAi", True) is not False,
                    rebuild=data.get("rebuild", False) is True,
                    ai_only=data.get("aiOnly", False) is True,
                    targets=data.get("targets") if isinstance(data.get("targets"), list) else None,
                )
                self._send_json_response(202, {"status": "success", **job})
            except RuntimeError as e:
                self._send_json_response(409, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"啟動現有快照重判失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/ai-reviews':
            try:
                data = self._read_json_body()
                result = self._sku_mapping_store().rerun_ai(
                    data.get("productId", ""),
                    data.get("modelId", ""),
                    force_match=data.get("forceMatch", False) is True,
                )
                self._send_json_response(200, result)
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(503, {"status": "error", "message": str(e)})
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"重跑 SKU mapping AI 初判失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/sku-mapping/decisions':
            try:
                data = self._read_json_body()
                items = data.get("items") if isinstance(data.get("items"), list) else [data]
                result = self._sku_mapping_store().decisions(
                    items,
                    reviewer=str(data.get("reviewer") or "local_user"),
                    batch=data.get("batch") is True,
                )
                self._send_json_response(200, result)
            except MappingConflict as e:
                self._send_json_response(409, {"status": "conflict", "message": str(e)})
            except (ValueError, FileNotFoundError) as e:
                self._send_json_response(400 if isinstance(e, ValueError) else 404, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"套用 SKU mapping 決定失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/inbound/orders/import':
            try:
                data = self._read_json_body()
                if not str(data.get("reference") or data.get("orderReference") or "").strip() and not isinstance(data.get("order"), dict):
                    raise ValueError("請輸入 1688 訂單編號或連結")
                job = self._start_inbound_job("import", data)
                self._send_json_response(202, job)
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(409, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"啟動 1688 訂單匯入失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/inbound/receipts':
            try:
                receipt = self._inbound_store().create_receipt(self._read_json_body())
                self._send_json_response(201, {"status": "success", "receipt": receipt})
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"建立到貨單失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        inbound_preview_match = re.match(r'^/api/inbound/receipts/(\d+)/preview$', request_path)
        if inbound_preview_match:
            try:
                receipt_id = int(inbound_preview_match.group(1))
                payload = self._inbound_store().build_preview_payload(receipt_id)
                job = self._start_inbound_job("preview", payload)
                self._send_json_response(202, job)
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(409, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"啟動蝦皮庫存預覽失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        inbound_apply_match = re.match(r'^/api/inbound/receipts/(\d+)/apply$', request_path)
        if inbound_apply_match:
            try:
                if not self._inbound_write_enabled():
                    raise PermissionError(
                        "蝦皮入庫寫入目前為唯讀模式；請在 .env.local 設定 "
                        "SHOPEE_INBOUND_WRITE_ENABLED=true 後重新啟動"
                    )
                if self._inbound_worker_busy():
                    raise RuntimeError("目前已有其他瀏覽器流程在執行，請稍後再試")
                data = self._read_json_body()
                receipt_id = int(inbound_apply_match.group(1))
                payload = self._inbound_store().prepare_apply(
                    receipt_id,
                    str(data.get("previewVersion") or ""),
                    data.get("confirmed") is True,
                )
                payload["confirmed"] = True
                job = self._start_inbound_job("apply", payload)
                self._send_json_response(202, job)
            except PermissionError as e:
                self._send_json_response(403, {"status": "error", "message": str(e)})
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except RuntimeError as e:
                self._send_json_response(409, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception(f"啟動蝦皮入庫更新失敗: {e}")
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if self.path == '/api/golden-table/model-1688-sku':
            try:
                data = self._read_json_body()
                response = self._update_golden_table_model_1688_sku(data)
                self._send_json_response(200, response)
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"更新 golden_table.json 1688 SKU 對應失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/golden-table/product-1688-skus':
            try:
                data = self._read_json_body()
                response = self._update_golden_table_product_1688_skus(data)
                self._send_json_response(200, response)
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"批次更新 golden_table.json 1688 SKU 對應失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/alibaba/sku-review/apply':
            try:
                data = self._read_json_body()
                response = self._apply_sku_review_updates(data)
                self._send_json_response(200, response)
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"寫入 1688 SKU review 結果失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/golden-table/model-alibaba':
            try:
                data = self._read_json_body()
                response = self._update_golden_table_model_alibaba(data)
                self._send_json_response(200, response)
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"更新 golden_table.json 阿里巴巴資料失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/alibaba/bindings':
            try:
                data = self._read_json_body()
                response = self._procurement_store().upsert_binding(data)
                self._send_json_response(200, {
                    "status": "success",
                    "binding": response
                })
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"儲存 1688 綁定失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/procurement/drafts':
            try:
                data = self._read_json_body()
                draft = self._procurement_store().create_draft(data)
                self._send_json_response(200, {
                    "status": "success",
                    "draft": draft
                })
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"建立採購草稿失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        submit_match = re.match(r'^/api/procurement/drafts/(\d+)/submit$', self.path)
        if submit_match:
            try:
                order = self._procurement_store().submit_draft(
                    int(submit_match.group(1)),
                    self._alibaba_client(),
                )
                self._send_json_response(200, {
                    "status": "success",
                    "order": order
                })
            except PermissionError as e:
                self._send_json_response(403, {
                    "status": "error",
                    "message": str(e)
                })
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"送出採購草稿失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if self.path == '/api/procurement/orders/sync':
            try:
                result = self._procurement_store().sync_orders(self._alibaba_client())
                self._send_json_response(200, result)
            except Exception as e:
                logger.exception(f"同步 1688 訂單失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        if request_path == '/api/alibaba-restock/batches/preview':
            try:
                data = self._read_json_body()
                snapshot = build_preview(
                    data.get("products") if isinstance(data, dict) else None,
                    keyword=str((data or {}).get("keyword") or ""),
                    cart_sku_count=(data or {}).get("cartSkuCount"),
                )
                self._send_json_response(200, {"status": "success", "preview": snapshot})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception("建立批次補貨預覽失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if request_path == '/api/alibaba-restock/batches':
            try:
                data = self._read_json_body()
                self._send_json_response(200, self._start_restock_batch(data if isinstance(data, dict) else {}))
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception("啟動批次補貨失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        restock_resume_match = re.match(r'^/api/alibaba-restock/batches/([^/]+)/resume$', request_path)
        if restock_resume_match:
            try:
                data = self._read_json_body() if int(self.headers.get('Content-Length', 0) or 0) else {}
                self._send_json_response(200, self._resume_restock_batch(
                    restock_resume_match.group(1),
                    data if isinstance(data, dict) else {},
                ))
            except FileNotFoundError as e:
                self._send_json_response(404, {"status": "error", "message": str(e)})
            except ValueError as e:
                self._send_json_response(400, {"status": "error", "message": str(e)})
            except Exception as e:
                logger.exception("續跑批次補貨失敗: %s", e)
                self._send_json_response(500, {"status": "error", "message": str(e)})
            return

        if self.path == '/api/alibaba-restock':
            try:
                data = self._read_json_body()
                response = self.start_alibaba_restock(data)
                self._send_json_response(200, response)
            except ValueError as e:
                self._send_json_response(400, {
                    "status": "error",
                    "message": str(e)
                })
            except FileNotFoundError as e:
                self._send_json_response(404, {
                    "status": "error",
                    "message": str(e)
                })
            except Exception as e:
                logger.exception(f"啟動 1688 採購車流程失敗: {e}")
                self._send_json_response(500, {
                    "status": "error",
                    "message": str(e)
                })
            return

        # 處理中斷爬蟲請求
        if self.path == '/stop_crawler':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length).decode('utf-8')

            try:
                data = json.loads(post_data)
                if data.get('action') == 'stop':
                    # 嘗試中斷爬蟲
                    success = self.stop_running_crawler()

                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()

                    response = {
                        "status": "success" if success else "failed",
                        "message": "爬蟲已中斷" if success else "中斷爬蟲失敗"
                    }
                    self.wfile.write(
                        json.dumps(response,
                                   ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "error": str(e)
                    }, ensure_ascii=False).encode('utf-8'))
            return

    def log_message(self, format, *args):
        try:
            super().log_message(format, *args)
        except BrokenPipeError:
            pass

    def _send_json_response(self, status_code, payload):
        try:
            self.send_response(status_code)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        except BrokenPipeError:
            logger.warning("回應寫入時連線已關閉")

    def _send_html_response(self, status_code, body):
        self.send_response(status_code)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode('utf-8') if isinstance(body, str) else body)

    def _read_json_body(self, max_bytes=None):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            raise ValueError("請求內容長度無效")
        if max_bytes is not None and content_length > max_bytes:
            raise ValueError(f"匯入檔案過大，限制為 {max_bytes // (1024 * 1024)} MB")
        post_data = self.rfile.read(content_length)
        if len(post_data) != content_length:
            raise ValueError("請求內容不完整")
        try:
            post_data = post_data.decode('utf-8')
        except UnicodeDecodeError as e:
            raise ValueError("匯入檔案必須使用 UTF-8 編碼") from e
        return json.loads(post_data) if post_data else {}

    def _procurement_store(self):
        return ProcurementStore(os.path.dirname(os.path.abspath(__file__)))

    def _sku_mapping_store(self):
        global sku_mapping_service
        if sku_mapping_service is None:
            with sku_mapping_service_lock:
                if sku_mapping_service is None:
                    sku_mapping_service = SkuMappingService(os.path.dirname(os.path.abspath(__file__)))
        return sku_mapping_service

    def _inbound_store(self):
        return InboundStore(os.path.dirname(os.path.abspath(__file__)))

    def _inbound_write_enabled(self):
        value, _ = load_openai_config_value(
            "SHOPEE_INBOUND_WRITE_ENABLED",
            "false",
            os.path.dirname(os.path.abspath(__file__)),
        )
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _inbound_worker_busy(self):
        global current_crawler_process
        return current_crawler_process is not None and current_crawler_process.poll() is None

    def _inbound_status(self):
        active_job_id = ""
        with inbound_jobs_lock:
            for job_id, job in inbound_jobs.items():
                if job.get("status") in ("reading_order", "awaiting_login", "applying"):
                    active_job_id = job_id
                    break
        return {
            "status": "success",
            "writeEnabled": self._inbound_write_enabled(),
            "busy": self._inbound_worker_busy(),
            "activeJobId": active_job_id,
            "message": (
                "蝦皮入庫寫入已啟用"
                if self._inbound_write_enabled()
                else "目前為唯讀模式；完成真實訂單預覽驗證後，再設定 SHOPEE_INBOUND_WRITE_ENABLED=true"
            ),
        }

    @staticmethod
    def _load_json_if_exists(path):
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _read_alibaba_restock_job(job_id):
        with alibaba_restock_jobs_lock:
            job = alibaba_restock_jobs.get(job_id)
            if not job:
                raise FileNotFoundError("找不到 1688 補貨工作")
            return {key: value for key, value in job.items() if not key.startswith("_")}

    def _read_inbound_job(self, job_id):
        with inbound_jobs_lock:
            job = inbound_jobs.get(job_id)
            if not job:
                raise FileNotFoundError("找不到入庫工作")
            status_file = job.get("_statusFile")
            public = {key: value for key, value in job.items() if not key.startswith("_")}
        if public.get("status") in ("reading_order", "awaiting_login", "applying"):
            worker_status = self._load_json_if_exists(status_file)
            if worker_status.get("status"):
                public["status"] = worker_status["status"]
                public["message"] = worker_status.get("message", public.get("message", ""))
                public["progress"] = {
                    key: value
                    for key, value in worker_status.items()
                    if key not in ("status", "message", "updatedAt")
                }
        return public

    def _start_inbound_job(self, action, payload):
        global current_crawler_process
        if self._inbound_worker_busy():
            raise RuntimeError("目前已有其他瀏覽器流程在執行，請稍後再試")

        base_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(base_dir, "inbound_worker.py")
        if not os.path.exists(script_path):
            raise FileNotFoundError("找不到 inbound_worker.py")
        job_id = uuid.uuid4().hex
        input_path = os.path.join(tempfile.gettempdir(), f"inventory_inbound_{job_id}_input.json")
        output_path = os.path.join(tempfile.gettempdir(), f"inventory_inbound_{job_id}_output.json")
        status_path = os.path.join(tempfile.gettempdir(), f"inventory_inbound_{job_id}_status.json")
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

        initial_status = "reading_order" if action == "import" else "applying"
        initial_message = {
            "import": "正在讀取 1688 訂單",
            "preview": "正在讀取蝦皮即時庫存",
            "apply": "正在更新蝦皮庫存",
        }[action]
        job = {
            "jobId": job_id,
            "action": action,
            "status": initial_status,
            "message": initial_message,
            "createdAt": int(time.time()),
            "updatedAt": int(time.time()),
            "result": None,
            "_inputFile": input_path,
            "_outputFile": output_path,
            "_statusFile": status_path,
            "_workerPayload": payload,
        }
        with inbound_jobs_lock:
            inbound_jobs[job_id] = job

        cmd = [os.path.abspath(sys.executable)]
        if getattr(sys, 'frozen', False):
            cmd.append("--inbound-worker")
        else:
            cmd.append(script_path)
        cmd.extend([
            "--action", action,
            "--input", input_path,
            "--output", output_path,
            "--status-file", status_path,
        ])
        try:
            process = subprocess.Popen(
                cmd,
                cwd=base_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                preexec_fn=None if os.name == "nt" else os.setsid,
            )
        except Exception:
            with inbound_jobs_lock:
                inbound_jobs.pop(job_id, None)
            failed_cleanup = remove_files((input_path, output_path, status_path))
            if failed_cleanup:
                logger.warning("入庫工作啟動失敗後無法刪除暫存檔: %s", failed_cleanup)
            raise
        current_crawler_process = process
        with inbound_jobs_lock:
            inbound_jobs[job_id]["_process"] = process

        def drain(pipe, label):
            try:
                for line in pipe:
                    text = line.strip()
                    if text:
                        logger.info(f"入庫{label}: {text}")
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        threading.Thread(target=drain, args=(process.stdout, "輸出"), daemon=True).start()
        threading.Thread(target=drain, args=(process.stderr, "錯誤"), daemon=True).start()
        threading.Thread(
            target=self._finish_inbound_job,
            args=(job_id, process),
            daemon=True,
        ).start()
        return {"status": initial_status, "jobId": job_id, "message": initial_message}

    def _finish_inbound_job(self, job_id, process):
        global current_crawler_process
        process.wait()
        with inbound_jobs_lock:
            job = inbound_jobs.get(job_id)
            if not job:
                return
            action = job["action"]
            payload = job.get("_workerPayload", {})
            output_path = job.get("_outputFile")
            cleanup_paths = [job.get("_inputFile"), output_path, job.get("_statusFile")]
        worker_result = self._load_json_if_exists(output_path)
        try:
            if process.returncode != 0 or worker_result.get("status") != "success":
                message = str(worker_result.get("message") or "入庫瀏覽器工作失敗")
                if action == "preview":
                    receipt = self._inbound_store().record_preview(int(payload["receiptId"]), [])
                    final_status = receipt["status"]
                    final_result = {"receipt": receipt}
                elif action == "apply":
                    fallback = [
                        {"updateId": item.get("id"), "status": "manual_review", "message": message}
                        for item in payload.get("updates", [])
                    ]
                    receipt = self._inbound_store().record_apply_results(int(payload["receiptId"]), fallback)
                    final_status = receipt["status"]
                    final_result = {"receipt": receipt}
                else:
                    final_status = "partial_failed"
                    final_result = None
                raise RuntimeError(message)

            if action == "import":
                order = self._inbound_store().import_order(worker_result.get("order") or {})
                final_status = "completed" if order["status"] == "ready" else "needs_review"
                final_result = {"order": order}
                final_message = "1688 訂單已匯入" if final_status == "completed" else "訂單已匯入，部分品項需要確認蝦皮對照"
            elif action == "preview":
                receipt = self._inbound_store().record_preview(
                    int(payload["receiptId"]), worker_result.get("results") or []
                )
                final_status = receipt["status"]
                final_result = {"receipt": receipt}
                final_message = "蝦皮庫存預覽完成" if final_status == "preview_ready" else "部分規格需要人工確認"
            else:
                receipt = self._inbound_store().record_apply_results(
                    int(payload["receiptId"]), worker_result.get("results") or []
                )
                final_status = receipt["status"]
                final_result = {"receipt": receipt}
                final_message = "蝦皮入庫完成" if final_status == "completed" else "部分庫存更新未完成"
            with inbound_jobs_lock:
                job = inbound_jobs.get(job_id)
                if job:
                    job.update({
                        "status": final_status,
                        "message": final_message,
                        "result": final_result,
                        "updatedAt": int(time.time()),
                    })
        except Exception as exc:
            logger.exception(f"入庫工作 {job_id} 完成處理失敗: {exc}")
            with inbound_jobs_lock:
                job = inbound_jobs.get(job_id)
                if job:
                    # preview/apply 可能已在上方寫入更精確的 receipt 狀態。
                    job.update({
                        "status": locals().get("final_status", "partial_failed"),
                        "message": str(exc),
                        "result": locals().get("final_result"),
                        "updatedAt": int(time.time()),
                    })
        finally:
            if current_crawler_process is process:
                current_crawler_process = None
            failed_cleanup = remove_files(cleanup_paths)
            if failed_cleanup:
                logger.warning("入庫工作完成後無法刪除暫存檔: %s", failed_cleanup)

    def _list_alibaba_bindings(self):
        """以 golden_table.json 補強採購 binding，讓批次掃描結果立即反映到前端。"""
        bindings = self._procurement_store().list_bindings()
        golden_path = self._golden_table_path()
        if not golden_path.exists():
            return bindings
        try:
            golden_table = self._load_json_file(golden_path)
        except (OSError, json.JSONDecodeError):
            return bindings

        for product_id, product in golden_table.items():
            if not isinstance(product, dict):
                continue
            product_name = str(product.get("商品名稱") or "")
            for model in product.get("型號", []):
                if not isinstance(model, dict):
                    continue
                sku_name = str(model.get("1688_sku_name") or "").strip()
                sku_second_name = str(model.get("1688_sku_second_name") or "").strip()
                product_url = str(model.get("阿里巴巴商品URL") or "").strip()
                offer_id = normalize_identifier(model.get("1688_offer_id")) or parse_offer_id(product_url)
                sku_id = normalize_identifier(model.get("1688_sku_id"))
                if not sku_name and not sku_second_name and not product_url and not offer_id and not sku_id:
                    continue
                model_id = normalize_identifier(model.get("規格ID", "")) or str(model.get("型號名稱") or "").strip()
                if not model_id:
                    continue
                key = f"{product_id}|||{model_id}"
                binding = dict(bindings.get(key) or {})
                binding.setdefault("productId", str(product_id))
                binding.setdefault("modelId", model_id)
                binding.setdefault("productName", product_name)
                binding.setdefault("modelName", str(model.get("型號名稱") or ""))
                binding.setdefault("alibabaProductName", str(model.get("阿里巴巴商品名稱") or ""))
                binding.setdefault("alibabaProductUrl", product_url)
                # golden_table.json 是人工編輯與批次掃描共用的唯一 SKU 來源。
                if sku_name:
                    binding["alibabaSkuName"] = sku_name
                if sku_second_name:
                    binding["alibabaSkuSecondName"] = sku_second_name
                if sku_id:
                    binding["alibabaSkuId"] = sku_id
                if offer_id:
                    binding["alibabaOfferId"] = offer_id
                if model.get("1688_spec_text"):
                    binding["alibabaSpecText"] = str(model.get("1688_spec_text"))
                binding["alibabaMappingStatus"] = str(model.get("1688_mapping_status") or ("pending" if model.get("1688_sku_name") else "missing"))
                binding["alibabaOfferFingerprint"] = str(model.get("1688_offer_fingerprint") or "")
                bindings[key] = binding
        return bindings

    def _alibaba_client(self):
        return AlibabaApiClient(os.path.dirname(os.path.abspath(__file__)))

    def _debug_snapshots_dir(self):
        return Path(os.path.dirname(os.path.abspath(__file__))) / "debug_snapshots"

    def _golden_table_path(self):
        return Path(os.path.dirname(os.path.abspath(__file__))) / "golden_table.json"

    def _shopee_products_path(self):
        return Path(os.path.dirname(os.path.abspath(__file__))) / "shopee_products.json"

    def _watchlist_path(self):
        return Path(os.path.dirname(os.path.abspath(__file__))) / "watchlists" / "personal_watchlist.json"

    def _restock_batches_root(self):
        return self._debug_snapshots_dir() / "restock_batches"

    def _home_bootstrap(self):
        return load_home_bootstrap(
            self._shopee_products_path(),
            self._watchlist_path(),
            self._golden_table_path(),
        )

    def _load_restock_batch(self, run_id):
        safe_id = str(run_id or "").strip()
        if not safe_id or "/" in safe_id or "\\" in safe_id or safe_id in {".", ".."}:
            raise FileNotFoundError("找不到批次補貨工作")
        return load_state(self._restock_batches_root() / safe_id)

    def _current_restock_batch(self):
        with restock_batch_lock:
            live_id = restock_batch_runtime.get("runId")
        if live_id:
            try:
                return {"status": "success", "batch": public_state(self._load_restock_batch(live_id))}
            except FileNotFoundError:
                pass
        state = find_current_batch(self._restock_batches_root())
        return {"status": "success", "batch": public_state(state) if state else None}

    def _restock_batch_report(self, run_id):
        state = self._load_restock_batch(run_id)
        report_path = Path(state.get("reportPath") or "")
        if report_path.exists():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload.setdefault("status", state.get("status"))
            return payload
        return build_report(state)

    def _restock_batch_report_html(self, run_id):
        state = self._load_restock_batch(run_id)
        html_path = Path(state.get("reportHtmlPath") or "")
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return render_report_html(build_report(state))

    def _start_restock_batch(self, payload):
        if _restock_batch_is_running():
            raise ValueError("已有批次補貨在執行，請等待完成或先處理暫停中的批次")
        snapshot = payload.get("preview") if isinstance(payload.get("preview"), dict) else build_preview(
            payload.get("products"),
            keyword=str(payload.get("keyword") or ""),
            cart_sku_count=payload.get("cartSkuCount"),
        )
        if not snapshot.get("readyProducts"):
            raise ValueError("目前畫面沒有可執行的補貨型號")
        state = create_state(snapshot)
        launched = launch_restock_batch(state)
        return {
            "status": "success",
            "message": launched.get("message") or "已開始依序補貨",
            "runId": launched.get("runId"),
            "batch": public_state(launched),
        }

    def _resume_restock_batch(self, run_id, payload):
        if _restock_batch_is_running():
            raise ValueError("已有批次補貨在執行")
        state = self._load_restock_batch(run_id)
        state = resume_state(state, cart_cleared=bool((payload or {}).get("cartCleared")))
        launched = launch_restock_batch(state)
        return {
            "status": "success",
            "message": launched.get("message") or "已繼續剩餘商品",
            "runId": launched.get("runId"),
            "batch": public_state(launched),
        }

    def _load_json_file(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_golden_table_import(self, golden_path, golden_table, product_id, product):
        """Atomically add one new product and retain a recoverable backup."""
        updated = dict(golden_table)
        updated[str(product_id)] = product
        backup_path = golden_path.with_name(
            f"golden_table.json.backup_before_import_{int(time.time())}"
        )
        shutil.copy2(golden_path, backup_path)
        prune_golden_table_backups(backup_path)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".json", delete=False,
                dir=str(golden_path.parent)
            ) as handle:
                temp_path = handle.name
                json.dump(updated, handle, ensure_ascii=False, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, golden_path)
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
        return backup_path

    def _review_report_paths(self):
        debug_dir = self._debug_snapshots_dir()
        if not debug_dir.exists():
            return []
        return sorted(
            debug_dir.glob("alibaba_sku_mapping_report_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    def _summarize_sku_review_report(self, path):
        report = self._load_json_file(path)
        items = [
            item
            for result in report.get("results", [])
            for item in result.get("items", [])
            if isinstance(item, dict)
        ]
        sock_items = [
            item for item in items
            if is_sock_product_name(str(item.get("productName", "")))
        ]
        mapped = [
            item for item in sock_items
            if item.get("mappingStatus") == "mapped"
        ]
        high = [
            item for item in mapped
            if item.get("confidence") == "high"
        ]
        errors = [
            item for item in sock_items
            if item.get("mappingStatus") == "error"
        ]
        needs_review = [
            item for item in sock_items
            if item.get("mappingStatus") != "mapped" or item.get("confidence") != "high"
        ]
        report_status = str(report.get("status") or "legacy").strip()
        valid = len(sock_items) > 0 and len(high) > 0 and report_status not in ("aborted", "fatal")
        invalid_reason = ""
        if not valid:
            if report_status in ("aborted", "fatal"):
                invalid_reason = report.get("abortedReason") or "報告已中止"
            elif len(sock_items) == 0:
                invalid_reason = "報告沒有襪子型號"
            elif len(high) == 0:
                invalid_reason = "沒有任何 high confidence 對應，可能是 1688 驗證碼攔截或抓取失敗"

        return {
            "fileName": path.name,
            "path": str(path),
            "createdAt": report.get("createdAt", ""),
            "status": report_status,
            "valid": valid,
            "invalidReason": invalid_reason,
            "mtime": int(path.stat().st_mtime),
            "productCount": len({item.get("productId", "") for item in sock_items}),
            "modelCount": len(sock_items),
            "mappedCount": len(mapped),
            "highConfidenceCount": len(high),
            "needsReviewCount": len(needs_review),
            "errorCount": len(errors),
        }

    def _list_sku_review_reports(self):
        reports = []
        for path in self._review_report_paths():
            try:
                reports.append(self._summarize_sku_review_report(path))
            except Exception as e:
                reports.append({
                    "fileName": path.name,
                    "path": str(path),
                    "valid": False,
                    "invalidReason": str(e),
                    "mtime": int(path.stat().st_mtime),
                    "status": "read_error",
                    "productCount": 0,
                    "modelCount": 0,
                    "mappedCount": 0,
                    "highConfidenceCount": 0,
                    "needsReviewCount": 0,
                    "errorCount": 0,
                })
        recommended = next((report for report in reports if report.get("valid")), None)
        return {
            "status": "success",
            "reports": reports,
            "recommendedReport": recommended.get("fileName") if recommended else "",
        }

    def _resolve_sku_review_report_path(self, report_name):
        reports = self._list_sku_review_reports().get("reports", [])
        if report_name in ("", "latest"):
            selected = next((report for report in reports if report.get("valid")), None)
            if not selected:
                raise FileNotFoundError("找不到可用的 1688 SKU mapping report")
            return Path(selected["path"])

        safe_name = os.path.basename(str(report_name))
        if safe_name != report_name:
            raise ValueError("report 檔名不正確")
        if not re.match(r"^alibaba_sku_mapping_report_\d+\.json$", safe_name):
            raise ValueError("report 檔名格式不正確")

        path = self._debug_snapshots_dir() / safe_name
        if not path.exists():
            raise FileNotFoundError(f"找不到 report: {safe_name}")
        return path

    def _sku_review_item_category(self, item, result):
        if item.get("mappingStatus") == "mapped" and item.get("confidence") == "high":
            return "High confidence"
        return classify(item, result)

    def _current_golden_sku_map(self):
        golden_path = self._golden_table_path()
        if not golden_path.exists():
            return {}
        try:
            golden_table = self._load_json_file(golden_path)
        except Exception:
            return {}
        sku_map = {}
        for product_id, product in golden_table.items():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []):
                if not isinstance(model, dict):
                    continue
                sku_name = str(model.get("1688_sku_name") or "").strip()
                spec_id = normalize_identifier(model.get("規格ID", ""))
                model_name = str(model.get("型號名稱") or "").strip()
                if spec_id:
                    sku_map[f"{product_id}|||spec|||{spec_id}"] = sku_name
                if model_name:
                    sku_map[f"{product_id}|||name|||{model_name}"] = sku_name
        return sku_map

    def _current_sku_for_review_item(self, sku_map, item):
        product_id = str(item.get("productId", ""))
        spec_id = normalize_identifier(item.get("specId", ""))
        model_name = str(item.get("modelName", "")).strip()
        if spec_id:
            value = sku_map.get(f"{product_id}|||spec|||{spec_id}")
            if value is not None:
                return value
        return sku_map.get(f"{product_id}|||name|||{model_name}", str(item.get("existingSkuName", "")))

    def _build_sku_review_rows(self, report, filter_name, current_sku_map=None):
        rows = []
        current_sku_map = current_sku_map or {}
        allowed_filters = {"all", "high", "review", "actionable", "error"}
        if filter_name not in allowed_filters:
            filter_name = "review"

        for result in report.get("results", []):
            clean_option_list = clean_options(result.get("colorOptions", []))
            for item in result.get("items", []):
                if not isinstance(item, dict):
                    continue
                if not is_sock_product_name(str(item.get("productName", ""))):
                    continue
                is_high = item.get("mappingStatus") == "mapped" and item.get("confidence") == "high"
                category = self._sku_review_item_category(item, result)

                if filter_name == "high" and not is_high:
                    continue
                if filter_name == "review" and is_high:
                    continue
                if filter_name == "actionable" and not category.startswith("A."):
                    continue
                if filter_name == "error" and not category.startswith("C."):
                    continue

                rows.append({
                    "productId": str(item.get("productId", "")),
                    "productName": str(item.get("productName", "")),
                    "modelName": str(item.get("modelName", "")),
                    "specId": str(item.get("specId", "")),
                    "url": str(item.get("url", "")),
                    "urlSource": str(item.get("urlSource", "")),
                    "existingSkuName": str(self._current_sku_for_review_item(current_sku_map, item) or ""),
                    "suggestedSkuName": str(item.get("suggestedSkuName", "")),
                    "mappingStatus": str(item.get("mappingStatus", "")),
                    "confidence": str(item.get("confidence", "")),
                    "reason": str(item.get("reason", "")),
                    "reviewCategory": category,
                    "colorOptions": clean_option_list,
                    "urlStatus": str(result.get("status", "")),
                    "urlMessage": str(result.get("message", "")),
                })
        return rows

    def _group_sku_review_rows(self, rows):
        groups = {}
        for row in rows:
            product_id = row["productId"]
            if product_id not in groups:
                groups[product_id] = {
                    "productId": product_id,
                    "productName": row["productName"],
                    "rows": [],
                    "counts": {
                        "total": 0,
                        "high": 0,
                        "actionable": 0,
                        "error": 0,
                    }
                }
            group = groups[product_id]
            group["rows"].append(row)
            group["counts"]["total"] += 1
            if row["reviewCategory"] == "High confidence":
                group["counts"]["high"] += 1
            elif row["reviewCategory"].startswith("A."):
                group["counts"]["actionable"] += 1
            elif row["reviewCategory"].startswith("C."):
                group["counts"]["error"] += 1
        return sorted(
            groups.values(),
            key=lambda group: (-group["counts"]["total"], group["productId"])
        )

    def _load_sku_review(self, report_name, filter_name):
        path = self._resolve_sku_review_report_path(report_name)
        report = self._load_json_file(path)
        report_summary = self._summarize_sku_review_report(path)
        current_sku_map = self._current_golden_sku_map()
        rows = self._build_sku_review_rows(report, filter_name, current_sku_map)
        all_rows = self._build_sku_review_rows(report, "all", current_sku_map)
        summary = {
            "total": len(all_rows),
            "high": len([row for row in all_rows if row["reviewCategory"] == "High confidence"]),
            "review": len([row for row in all_rows if row["reviewCategory"] != "High confidence"]),
            "actionable": len([row for row in all_rows if row["reviewCategory"].startswith("A.")]),
            "error": len([row for row in all_rows if row["reviewCategory"].startswith("C.")]),
            "shown": len(rows),
        }
        return {
            "status": "success",
            "report": report_summary,
            "filter": filter_name,
            "summary": summary,
            "groups": self._group_sku_review_rows(rows),
        }

    def _apply_sku_review_updates(self, payload):
        items = payload.get("items")
        overwrite = bool(payload.get("overwrite", True))
        if not isinstance(items, list) or not items:
            raise ValueError("沒有要寫入的 SKU 對應")

        golden_path = self._golden_table_path()
        if not golden_path.exists():
            raise FileNotFoundError("找不到 golden_table.json")

        golden_table = self._load_json_file(golden_path)
        backup_path = golden_path.with_name(
            f"golden_table.json.backup_before_sku_review_{int(time.time())}"
        )
        updated = []
        skipped = []

        for item in items:
            if not isinstance(item, dict):
                continue
            product_id = normalize_identifier(item.get("productId", ""))
            spec_id = normalize_identifier(item.get("specId", ""))
            model_name = str(item.get("modelName", "")).strip()
            sku_name = str(item.get("skuName") or item.get("suggestedSkuName") or "").strip()
            if not product_id or (not spec_id and not model_name) or not sku_name:
                skipped.append({
                    "productId": product_id,
                    "specId": spec_id,
                    "modelName": model_name,
                    "reason": "缺少商品ID、型號或 SKU 名稱",
                })
                continue

            product = golden_table.get(product_id)
            if not isinstance(product, dict):
                skipped.append({
                    "productId": product_id,
                    "specId": spec_id,
                    "modelName": model_name,
                    "reason": "找不到商品",
                })
                continue

            target_model = self._find_golden_model(product.get("型號", []), spec_id, model_name)
            if target_model is None:
                skipped.append({
                    "productId": product_id,
                    "specId": spec_id,
                    "modelName": model_name,
                    "reason": "找不到型號",
                })
                continue

            current_sku = str(target_model.get("1688_sku_name") or "").strip()
            if current_sku and current_sku != sku_name and not overwrite:
                skipped.append({
                    "productId": product_id,
                    "specId": spec_id,
                    "modelName": model_name,
                    "reason": f"已有 SKU：{current_sku}",
                })
                continue

            target_model["1688_sku_name"] = sku_name
            updated.append({
                "productId": product_id,
                "specId": normalize_identifier(target_model.get("規格ID", "")),
                "modelName": str(target_model.get("型號名稱", "")).strip(),
                "skuName": sku_name,
            })

        if not updated:
            return {
                "status": "success",
                "message": "沒有寫入任何 SKU 對應",
                "updatedCount": 0,
                "skipped": skipped,
            }

        shutil.copy2(golden_path, backup_path)
        prune_golden_table_backups(backup_path)
        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(golden_table, f, ensure_ascii=False, indent=4)
            f.write("\n")

        return {
            "status": "success",
            "message": f"已寫入 {len(updated)} 筆 1688 SKU 對應",
            "updatedCount": len(updated),
            "updated": updated,
            "skipped": skipped,
            "backupPath": str(backup_path),
        }

    def start_alibaba_restock(self, payload):
        """啟動 1688 瀏覽器採購車流程，不會付款或送出正式訂單。"""
        global current_crawler_process

        if current_crawler_process is not None and current_crawler_process.poll() is None:
            raise ValueError("目前已有其他流程在執行，請稍後再試")

        restock_payload = self._build_alibaba_restock_payload(payload)
        items = restock_payload["items"]
        skipped = restock_payload.get("skipped") or []
        if not items:
            details = "；".join(
                f"{item.get('modelName') or item.get('specId') or '未命名'}：{item.get('reason')}"
                for item in skipped
                if isinstance(item, dict)
            )
            message = "沒有可啟動 1688 採購車流程的有效型號"
            if details:
                message = f"{message}：{details}"
            return {
                "status": "skipped",
                "message": message,
                "skipped": skipped,
                "itemCount": 0,
                "totalQty": 0,
            }
        validate_restock_sku_count(len(items))

        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alibaba_restocker.py")
        if not os.path.exists(script_path):
            raise FileNotFoundError("找不到 alibaba_restocker.py")

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="inventory_alibaba_restock_",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump(restock_payload, f, ensure_ascii=False, indent=2)
            input_path = f.name

        product_id = normalize_identifier(restock_payload.get("productId", "batch")) or "batch"
        output_path = os.path.join(
            tempfile.gettempdir(),
            f"alibaba_restock_result_{product_id}_{int(time.time())}.json"
        )
        job_id = uuid.uuid4().hex

        cmd = [
            os.path.abspath(sys.executable),
            script_path,
            "--input", input_path,
            "--output", output_path,
            "--headless", "false",
            "--pause-seconds", str(restock_pause_seconds(payload)),
        ]
        if restock_payload.get("addToCart", True):
            cmd.append("--add-to-cart")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                preexec_fn=None if os.name == "nt" else os.setsid
            )
        except Exception:
            failed_cleanup = remove_files((input_path, output_path))
            if failed_cleanup:
                logger.warning("1688 採購車啟動失敗後無法刪除暫存檔: %s", failed_cleanup)
            raise
        current_crawler_process = process
        with alibaba_restock_jobs_lock:
            alibaba_restock_jobs[job_id] = {
                "jobId": job_id,
                "status": "running",
                "message": "1688 補貨流程執行中",
                "startedAt": datetime.datetime.now().isoformat(timespec="seconds"),
                "itemCount": len(items),
                "totalQty": sum(item["restockQty"] for item in items),
                "completed": 0,
                "total": len(items),
                "pageIndex": 0,
            }

        def publish_worker_result(worker_result=None, fallback_message=""):
            result = worker_result if isinstance(worker_result, dict) else self._load_json_if_exists(output_path)
            with alibaba_restock_jobs_lock:
                job = alibaba_restock_jobs.get(job_id)
                if not job or job.get("status") in ("completed", "failed"):
                    return bool(result)
                if result:
                    job.update({
                        "status": "completed",
                        "message": result.get("message") or "1688 補貨流程已完成",
                        "completedAt": datetime.datetime.now().isoformat(timespec="seconds"),
                        "result": result,
                    })
                    return True
                if fallback_message:
                    job.update({
                        "status": "failed",
                        "message": fallback_message,
                        "completedAt": datetime.datetime.now().isoformat(timespec="seconds"),
                    })
            return False

        def watch_result_file():
            # 只輪詢本機結果檔，不會向 1688 發送任何額外請求。
            while process.poll() is None:
                if publish_worker_result():
                    return
                time.sleep(0.5)
            if not publish_worker_result():
                publish_worker_result(fallback_message=f"1688 補貨流程異常結束（返回碼 {process.returncode}）")

        def read_output(pipe, prefix):
            global current_crawler_process
            from alibaba_restocker import apply_progress_line
            try:
                for line in pipe:
                    line_text = line.strip()
                    if line_text == ALIBABA_RESTOCK_SESSION_CLOSED_MARKER:
                        # Worker 已寫完結果，使用者也已關閉 Chrome。
                        # Playwright 的本機清理偶爾還會拖幾秒，不讓這段
                        # 純清理時間繼續擋住下一次補貨。
                        if current_crawler_process is process:
                            current_crawler_process = None
                            logger.info("1688 視窗已關閉，已提前解除補貨執行中狀態")
                        continue
                    if line_text:
                        print(f"{prefix}: {line_text}")
                        logger.info(f"{prefix}: {line_text}")
                        with alibaba_restock_jobs_lock:
                            job = alibaba_restock_jobs.get(job_id)
                            if job and job.get("status") == "running":
                                apply_progress_line(job, line_text)
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        def wait_and_clear():
            global current_crawler_process
            try:
                process.wait()
                logger.info(f"1688 採購車流程完成，返回碼: {process.returncode}")
            finally:
                if not publish_worker_result():
                    publish_worker_result(fallback_message=f"1688 補貨流程未產生結果（返回碼 {process.returncode}）")
                if current_crawler_process is process:
                    current_crawler_process = None
                failed_cleanup = remove_files((input_path, output_path))
                if failed_cleanup:
                    logger.warning("1688 採購車完成後無法刪除暫存檔: %s", failed_cleanup)

        threading.Thread(target=read_output, args=(process.stdout, "1688採購車輸出"), daemon=True).start()
        threading.Thread(target=read_output, args=(process.stderr, "1688採購車錯誤"), daemon=True).start()
        threading.Thread(target=watch_result_file, daemon=True).start()
        threading.Thread(target=wait_and_clear, daemon=True).start()

        pause_seconds = restock_pause_seconds(payload)
        inspect_note = (
            f"瀏覽器會保留約 {pause_seconds} 秒供檢查。"
            if pause_seconds > 0
            else "完成後會關閉瀏覽器。"
        )
        return {
            "status": "success",
            "message": f"已啟動 1688 採購車流程：{len(items)} 個型號。{inspect_note}",
            "jobId": job_id,
            "draftId": restock_payload.get("draftId"),
            "productId": restock_payload.get("productId", ""),
            "itemCount": len(items),
            "totalQty": sum(item["restockQty"] for item in items),
            "skipped": restock_payload.get("skipped") or [],
        }

    def _build_alibaba_restock_payload(self, payload):
        draft_id = payload.get("draftId")
        add_to_cart = bool(payload.get("addToCart", True))
        raw_lines = payload.get("items") or payload.get("lines") or []
        product_id = normalize_identifier(payload.get("productId", ""))
        product_name = str(payload.get("productName", "")).strip()

        if draft_id:
            draft = self._procurement_store().get_draft(int(draft_id))
            raw_lines = draft.get("lines", [])
            first_line = raw_lines[0] if raw_lines else {}
            product_id = product_id or normalize_identifier(first_line.get("shopee_product_id", ""))
            product_name = product_name or str(first_line.get("shopee_product_name", "")).strip()

        if not isinstance(raw_lines, list) or len(raw_lines) == 0:
            raise ValueError("缺少補貨型號")

        first_url_by_product = {}
        for line in raw_lines:
            if not isinstance(line, dict):
                continue
            line_product_id = self._line_product_id(line, product_id)
            line_url = self._line_alibaba_url(line)
            if line_product_id and line_url and line_product_id not in first_url_by_product:
                first_url_by_product[line_product_id] = line_url

        items = []
        skipped = []
        for line in raw_lines:
            if not isinstance(line, dict):
                continue

            line_product_id = self._line_product_id(line, product_id)
            line_model_name = str(line.get("shopee_model_name") or line.get("modelName") or "").strip()
            line_model_id = normalize_identifier(line.get("shopee_model_id") or line.get("modelId") or line.get("specId"))
            line_url = self._line_alibaba_url(line)
            if not line_url and self._has_sku_mapping(line_product_id):
                line_url = first_url_by_product.get(line_product_id, "")
            golden_mapping = self._golden_mapping_for_model(line_product_id, line_model_id, line_model_name)
            line_sku_id = (
                normalize_identifier(golden_mapping.get("sku_id"))
                or normalize_identifier(line.get("alibaba_sku_id") or line.get("alibabaSkuId"))
            )
            line_sku_name = (
                str(golden_mapping.get("sku_name") or "").strip()
                or str(line.get("alibaba_sku_name") or line.get("alibabaSkuName") or "").strip()
            )
            line_sku_second_name = (
                str(golden_mapping.get("second_name") or "").strip()
                or str(line.get("alibaba_sku_second_name") or line.get("alibabaSkuSecondName") or "").strip()
            )
            mapping_status = str(
                golden_mapping.get("status")
                or line.get("alibaba_mapping_status")
                or line.get("alibabaMappingStatus")
                or "missing"
            ).strip()
            offer_fingerprint = str(
                golden_mapping.get("offer_fingerprint")
                or line.get("alibaba_offer_fingerprint")
                or line.get("alibabaOfferFingerprint")
                or ""
            ).strip()
            spec_text = str(
                golden_mapping.get("spec_text")
                or line.get("alibaba_spec_text")
                or line.get("alibabaSpecText")
                or ""
            ).strip()
            line_offer_id = (
                normalize_identifier(golden_mapping.get("offer_id"))
                or normalize_identifier(line.get("alibaba_offer_id") or line.get("alibabaOfferId"))
            )
            restock_qty = self._line_restock_qty(line)

            if restock_qty <= 0:
                continue
            if not line_model_name and not line_model_id:
                continue
            if not re.match(r'^https?://', line_url, re.IGNORECASE):
                continue
            skip_target = {
                "specId": line_model_id,
                "modelName": line_model_name,
            }
            if not golden_mapping:
                skipped.append({**skip_target, "reason": "沒有 golden table SKU mapping"})
                continue
            if mapping_status != "approved":
                skipped.append({
                    **skip_target,
                    "reason": f"1688 SKU mapping 尚未核准（{mapping_status}）",
                })
                continue
            if not line_sku_name:
                skipped.append({**skip_target, "reason": "缺少 1688 SKU 名稱"})
                continue

            if is_alibaba_sku_discontinued(line_sku_name):
                skipped.append({**skip_target, "reason": f"1688 {line_sku_name}"})
                continue

            line_product_name = str(line.get("shopee_product_name") or line.get("productName") or product_name).strip()
            try:
                dimension_count = int(float(golden_mapping.get("dimension_count") or (2 if line_sku_second_name else 1)))
            except (TypeError, ValueError):
                dimension_count = 2 if line_sku_second_name else 1
            if dimension_count > 2:
                skipped.append({**skip_target, "reason": "1688 商品超過兩層規格"})
                continue
            if (dimension_count >= 2 or requires_alibaba_second_sku(line_product_name, line_model_name)) and not line_sku_second_name:
                skipped.append({**skip_target, "reason": "缺少 1688 第二規格"})
                continue

            items.append({
                "productId": line_product_id,
                "productName": line_product_name,
                "modelId": line_model_id,
                "modelName": line_model_name,
                "alibabaOfferId": line_offer_id,
                "alibabaSkuId": line_sku_id,
                "alibabaSkuName": line_sku_name,
                "alibabaSkuSecondName": line_sku_second_name,
                "alibabaDimensionCount": dimension_count,
                "alibabaSpecText": spec_text,
                "alibabaMappingStatus": mapping_status,
                "alibabaOfferFingerprint": offer_fingerprint,
                "restockQty": restock_qty,
                "alibabaUrl": line_url,
            })

        return {
            "draftId": draft_id,
            "productId": product_id,
            "productName": product_name,
            "addToCart": add_to_cart,
            "items": items,
            "skipped": skipped,
        }

    def _golden_mapping_for_model(self, product_id, model_id, model_name):
        """Return only the exact approved golden mapping; never fuzzy-match cart rows."""
        try:
            with open(self._golden_table_path(), "r", encoding="utf-8") as f:
                golden = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        product = golden.get(str(product_id or ""), {})
        if not isinstance(product, dict):
            return {}
        for model in product.get("型號", []) or []:
            if not isinstance(model, dict):
                continue
            current_id = normalize_identifier(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current_id != str(model_id or "") and str(model.get("型號名稱") or "").strip() != str(model_name or "").strip():
                continue
            sku_id = normalize_identifier(model.get("1688_sku_id"))
            status = str(model.get("1688_mapping_status") or ("pending" if model.get("1688_sku_name") else "missing")).strip()
            try:
                dimension_count = int(float(model.get("1688_dimension_count") or (2 if model.get("1688_sku_second_name") else 1)))
            except (TypeError, ValueError):
                dimension_count = 2 if model.get("1688_sku_second_name") else 1
            return {
                "sku_id": sku_id,
                "offer_id": normalize_identifier(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL")),
                "sku_name": str(model.get("1688_sku_name") or "").strip(),
                "second_name": str(model.get("1688_sku_second_name") or "").strip(),
                "spec_text": str(model.get("1688_spec_text") or "").strip(),
                "dimension_count": dimension_count,
                "status": status,
                "offer_fingerprint": str(model.get("1688_offer_fingerprint") or "").strip(),
                "mapping_fingerprint": str(model.get("1688_mapping_fingerprint") or "").strip(),
            }
        return {}

    def _line_product_id(self, line, fallback_product_id):
        return normalize_identifier(line.get("shopee_product_id") or line.get("productId") or fallback_product_id)

    def _line_alibaba_url(self, line):
        return str(line.get("alibaba_product_url") or line.get("alibabaProductUrl") or line.get("alibabaUrl") or "").strip()

    def _line_restock_qty(self, line):
        return resolve_restock_quantity(line, self._round_restock_qty)

    def _round_restock_qty(self, quantity):
        qty = int(quantity or 0)
        if qty <= 0:
            return 0
        return ((qty + 5) // 10) * 10

    def _fallback_sku_selection(self, product_id, model_name):
        sku_mapping = self._sku_mapping_for_product(product_id)
        selection = sku_mapping.get(str(model_name or "").strip(), {})
        if not isinstance(selection, dict):
            return {"primary": str(selection or "").strip(), "secondary": ""}
        return {
            "primary": str(selection.get("primary") or "").strip(),
            "secondary": str(selection.get("secondary") or "").strip(),
        }

    def _fallback_sku_name(self, product_id, model_name):
        return self._fallback_sku_selection(product_id, model_name)["primary"]

    def _has_sku_mapping(self, product_id):
        return bool(self._sku_mapping_for_product(product_id))

    def _sku_mapping_for_product(self, product_id):
        golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_table.json")
        try:
            with open(golden_path, "r", encoding="utf-8") as f:
                golden_table = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        product = golden_table.get(str(product_id or ""), {})
        if not isinstance(product, dict):
            return {}

        sku_mapping = {}
        models = product.get("型號", [])
        if not isinstance(models, list):
            return {}
        for model in models:
            if not isinstance(model, dict):
                continue
            model_name = str(model.get("型號名稱") or "").strip()
            sku_name = str(model.get("1688_sku_name") or "").strip()
            sku_second_name = str(model.get("1688_sku_second_name") or "").strip()
            if model_name and sku_name:
                sku_mapping[model_name] = {
                    "primary": sku_name,
                    "secondary": sku_second_name,
                }
        return sku_mapping

    def _update_golden_table_model_alibaba(self, payload):
        product_id = normalize_identifier(payload.get("productId", ""))
        spec_id = normalize_identifier(payload.get("specId", ""))
        model_name = str(payload.get("modelName", "")).strip()
        alibaba_product_name = str(payload.get("alibabaProductName", "")).strip()
        alibaba_product_url = str(payload.get("alibabaProductUrl", "")).strip()
        # 商品 URL 是 Offer ID 的唯一來源；網址變更時不可保留舊 Offer ID。
        alibaba_offer_id = parse_offer_id(alibaba_product_url) or normalize_identifier(
            payload.get("alibabaOfferId", "")
        )
        alibaba_sku_id = normalize_identifier(payload.get("alibabaSkuId", ""))
        alibaba_sku_name = str(payload.get("alibabaSkuName", "")).strip()
        alibaba_sku_second_name = str(payload.get("alibabaSkuSecondName", "")).strip()
        alibaba_min_order_qty = int(payload.get("alibabaMinOrderQty") or 1)
        alibaba_package_multiple = int(payload.get("alibabaPackageMultiple") or 1)
        alibaba_last_price_cny = payload.get("alibabaLastPriceCny")
        mapping_approved = bool(payload.get("mappingApproved"))
        apply_scope = str(payload.get("applyScope", "single")).strip()

        if apply_scope not in ("single", "fill_missing", "overwrite_all", "selected_models"):
            raise ValueError("套用範圍不正確")
        if not product_id:
            raise ValueError("缺少商品ID")
        if not spec_id and not model_name:
            raise ValueError("缺少規格ID或型號名稱")
        if alibaba_product_url and not re.match(r'^https?://', alibaba_product_url, re.IGNORECASE):
            raise ValueError("阿里巴巴商品URL 必須以 http:// 或 https:// 開頭")

        golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_table.json")
        if not os.path.exists(golden_path):
            raise FileNotFoundError("找不到 golden_table.json")

        with open(golden_path, "r", encoding="utf-8") as f:
            golden_table = json.load(f)

        product, product_created = self._ensure_golden_table_product(
            golden_table, product_id, spec_id, model_name
        )

        models = product.get("型號", [])
        if not isinstance(models, list):
            raise ValueError(f"商品 {product_id} 的型號資料格式不正確")

        target_model = self._find_golden_model(models, spec_id, model_name)
        if target_model is None:
            raise FileNotFoundError("找不到對應型號")

        current_url = str(target_model.get("阿里巴巴商品URL") or "").strip()
        current_offer_id = normalize_identifier(target_model.get("1688_offer_id")) or parse_offer_id(current_url)
        if alibaba_product_url and alibaba_offer_id != current_offer_id:
            raise ValueError("1688 offer 已改變；請到「1688 SKU Mapping → URL 管理」檢查新連結與 SKU 後再更新")

        if apply_scope == "single":
            models_to_update = [target_model]
        elif apply_scope == "fill_missing":
            models_to_update = [
                model for model in models
                if not str(model.get("阿里巴巴商品URL", "")).strip()
            ]
            if target_model not in models_to_update:
                models_to_update.append(target_model)
        elif apply_scope == "overwrite_all":
            models_to_update = models
        else:
            selected_models = payload.get("selectedModels")
            if not isinstance(selected_models, list) or len(selected_models) == 0:
                raise ValueError("請至少選擇一個要套用的型號")

            models_to_update = []
            seen_model_ids = set()
            for selected_model in selected_models:
                if not isinstance(selected_model, dict):
                    continue

                selected_spec_id = normalize_identifier(selected_model.get("specId", ""))
                selected_model_name = str(selected_model.get("modelName", "")).strip()
                selected_golden_model = self._find_golden_model(
                    models, selected_spec_id, selected_model_name)
                if selected_golden_model is None:
                    continue

                model_identity = id(selected_golden_model)
                if model_identity not in seen_model_ids:
                    models_to_update.append(selected_golden_model)
                    seen_model_ids.add(model_identity)

            if len(models_to_update) == 0:
                raise ValueError("找不到勾選的型號")

        for model in models_to_update:
            model["阿里巴巴商品名稱"] = alibaba_product_name
            model["阿里巴巴商品URL"] = alibaba_product_url
            model["1688_offer_id"] = alibaba_offer_id
            model["1688_sku_id"] = alibaba_sku_id
            model["1688_sku_name"] = alibaba_sku_name
            if alibaba_sku_second_name:
                model["1688_sku_second_name"] = alibaba_sku_second_name
            else:
                model.pop("1688_sku_second_name", None)
            model["1688_min_order_qty"] = alibaba_min_order_qty
            model["1688_package_multiple"] = alibaba_package_multiple
            model["1688_last_price_cny"] = alibaba_last_price_cny
            model["1688_mapping_status"] = "approved" if mapping_approved and alibaba_sku_name else "missing"
            model["1688_mapping_source"] = "manual" if mapping_approved and alibaba_sku_name else "legacy_import"
            if mapping_approved and alibaba_sku_name:
                model["1688_dimension_count"] = 2 if alibaba_sku_second_name else 1
                model["1688_mapping_fingerprint"] = mapping_candidate_key(alibaba_offer_id, alibaba_sku_name, alibaba_sku_second_name)
                model["1688_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        backup_path = f"{golden_path}.bak"
        shutil.copy2(golden_path, backup_path)

        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(golden_table, f, ensure_ascii=False, indent=4)

        store = self._procurement_store()
        for model in models_to_update:
            store.upsert_binding({
                "productId": product_id,
                "modelId": normalize_identifier(model.get("規格ID", "")) or str(model.get("型號名稱", "")).strip(),
                "productName": product.get("商品名稱", ""),
                "modelName": model.get("型號名稱", ""),
                "alibabaProductName": alibaba_product_name,
                "alibabaProductUrl": alibaba_product_url,
                "alibabaOfferId": alibaba_offer_id,
                "alibabaSkuId": alibaba_sku_id,
                "alibabaSkuName": alibaba_sku_name,
                "alibabaSkuSecondName": alibaba_sku_second_name,
                "alibabaMinOrderQty": alibaba_min_order_qty,
                "alibabaPackageMultiple": alibaba_package_multiple,
                "alibabaLastPriceCny": alibaba_last_price_cny,
                "alibabaMappingStatus": model.get("1688_mapping_status", "missing"),
                "alibabaSpecText": model.get("1688_spec_text", ""),
                "alibabaOfferFingerprint": model.get("1688_offer_fingerprint", ""),
            })

        message = "已新增商品並更新阿里巴巴資料" if product_created else "已更新阿里巴巴資料"

        return {
            "status": "success",
            "message": message,
            "productId": product_id,
            "productCreated": product_created,
            "applyScope": apply_scope,
            "updatedCount": len(models_to_update),
            "updatedModels": models_to_update
        }

    def _update_golden_table_model_1688_sku(self, payload):
        """只更新單一型號的 1688 顯示名稱，保留網址與其他採購欄位。"""
        product_id = normalize_identifier(payload.get("productId", ""))
        spec_id = normalize_identifier(payload.get("specId", ""))
        model_name = str(payload.get("modelName", "")).strip()
        alibaba_sku_name = str(payload.get("alibabaSkuName", "")).strip()
        alibaba_sku_second_name = str(payload.get("alibabaSkuSecondName", "")).strip()

        if not product_id:
            raise ValueError("缺少商品ID")
        if not spec_id and not model_name:
            raise ValueError("缺少規格ID或型號名稱")

        golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_table.json")
        if not os.path.exists(golden_path):
            raise FileNotFoundError("找不到 golden_table.json")

        with open(golden_path, "r", encoding="utf-8") as f:
            golden_table = json.load(f)

        product, product_created = self._ensure_golden_table_product(
            golden_table, product_id, spec_id, model_name
        )

        models = product.get("型號", [])
        if not isinstance(models, list):
            raise ValueError(f"商品 {product_id} 的型號資料格式不正確")

        target_model = self._find_golden_model(models, spec_id, model_name)
        if target_model is None:
            raise FileNotFoundError("找不到對應型號")

        current_sku_name = str(target_model.get("1688_sku_name") or "").strip()
        current_sku_second_name = str(target_model.get("1688_sku_second_name") or "").strip()
        changed = current_sku_name != alibaba_sku_name or current_sku_second_name != alibaba_sku_second_name
        backup_path = ""
        if changed or product_created:
            backup_path = f"{golden_path}.bak"
            shutil.copy2(golden_path, backup_path)
            if alibaba_sku_name:
                target_model["1688_sku_name"] = alibaba_sku_name
            else:
                target_model.pop("1688_sku_name", None)
            if alibaba_sku_second_name:
                target_model["1688_sku_second_name"] = alibaba_sku_second_name
            else:
                target_model.pop("1688_sku_second_name", None)
            if alibaba_sku_name and str(target_model.get("1688_mapping_status") or "") == "approved":
                offer_id = normalize_identifier(target_model.get("1688_offer_id")) or parse_offer_id(target_model.get("阿里巴巴商品URL"))
                target_model["1688_dimension_count"] = 2 if alibaba_sku_second_name else 1
                target_model["1688_mapping_fingerprint"] = mapping_candidate_key(offer_id, alibaba_sku_name, alibaba_sku_second_name)
                target_model["1688_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            with open(golden_path, "w", encoding="utf-8") as f:
                json.dump(golden_table, f, ensure_ascii=False, indent=4)

        self._sync_model_1688_sku_binding(product_id, product, target_model, alibaba_sku_name)

        return {
            "status": "success",
            "message": (
                "已新增商品並更新 1688 對應型號" if product_created else
                ("已更新 1688 對應型號" if changed else "1688 對應型號未變更")
            ),
            "productId": product_id,
            "productCreated": product_created,
            "changed": changed,
            "backupPath": backup_path,
            "updatedModel": target_model
        }

    def _sync_model_1688_sku_binding(
        self,
        product_id,
        product,
        target_model,
        alibaba_sku_name,
        alibaba_sku_second_name=None,
    ):
        """同步採購草稿資料庫，讓批次與單筆編輯共用相同行為。"""
        model_id = normalize_identifier(target_model.get("規格ID", "")) or str(target_model.get("型號名稱", "")).strip()
        store = self._procurement_store()
        existing_binding = store.get_binding(product_id, model_id) or {}
        if alibaba_sku_second_name is None:
            alibaba_sku_second_name = target_model.get("1688_sku_second_name", "")

        store.upsert_binding({
            "productId": product_id,
            "modelId": model_id,
            "productName": product.get("商品名稱", ""),
            "modelName": target_model.get("型號名稱", ""),
            "alibabaProductName": existing_binding.get("alibabaProductName") or target_model.get("阿里巴巴商品名稱", ""),
            "alibabaProductUrl": existing_binding.get("alibabaProductUrl") or target_model.get("阿里巴巴商品URL", ""),
            "alibabaOfferId": existing_binding.get("alibabaOfferId") or target_model.get("1688_offer_id", ""),
            "alibabaSkuId": existing_binding.get("alibabaSkuId") or target_model.get("1688_sku_id", ""),
            "alibabaSkuName": alibaba_sku_name,
            "alibabaSkuSecondName": target_model.get("1688_sku_second_name", ""),
            "alibabaSpecText": target_model.get("1688_spec_text", ""),
            "alibabaMappingStatus": target_model.get("1688_mapping_status") or ("pending" if target_model.get("1688_sku_name") else "missing"),
            "alibabaOfferFingerprint": target_model.get("1688_offer_fingerprint", ""),
            "alibabaMinOrderQty": existing_binding.get("alibabaMinOrderQty") or target_model.get("1688_min_order_qty", 1),
            "alibabaPackageMultiple": existing_binding.get("alibabaPackageMultiple") or target_model.get("1688_package_multiple", 1),
            "alibabaLastPriceCny": existing_binding.get("alibabaLastPriceCny")
            if existing_binding.get("alibabaLastPriceCny") is not None
            else target_model.get("1688_last_price_cny"),
        })

    def _update_golden_table_product_1688_skus(self, payload):
        """一次更新同商品的所有 1688 型號對應，確保寫檔與備份只發生一次。"""
        product_id = normalize_identifier(payload.get("productId", ""))
        mappings = payload.get("mappings")
        if not product_id:
            raise ValueError("缺少商品ID")
        if not isinstance(mappings, list) or not mappings:
            raise ValueError("缺少要更新的型號對應")

        golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_table.json")
        if not os.path.exists(golden_path):
            raise FileNotFoundError("找不到 golden_table.json")

        with open(golden_path, "r", encoding="utf-8") as f:
            golden_table = json.load(f)
        product, product_created = self._ensure_golden_table_product(golden_table, product_id)

        models = product.get("型號", [])
        if not isinstance(models, list):
            raise ValueError(f"商品 {product_id} 的型號資料格式不正確")

        resolved = []
        seen_model_ids = set()
        for index, mapping in enumerate(mappings, 1):
            if not isinstance(mapping, dict):
                raise ValueError(f"第 {index} 筆型號對應格式不正確")
            spec_id = normalize_identifier(mapping.get("specId", ""))
            model_name = str(mapping.get("modelName", "")).strip()
            if not spec_id and not model_name:
                raise ValueError(f"第 {index} 筆缺少規格ID或型號名稱")
            target_model = self._find_golden_model(models, spec_id, model_name)
            if target_model is None:
                raise FileNotFoundError(f"找不到型號：{model_name or spec_id}")
            model_id = normalize_identifier(target_model.get("規格ID", "")) or str(target_model.get("型號名稱", "")).strip()
            if model_id in seen_model_ids:
                raise ValueError(f"型號重複：{target_model.get('型號名稱', model_id)}")
            seen_model_ids.add(model_id)
            resolved.append((
                target_model,
                str(mapping.get("alibabaSkuName", "")).strip(),
                str(mapping.get("alibabaSkuSecondName", "")).strip(),
            ))

        changed_models = []
        for target_model, sku_name, sku_second_name in resolved:
            current_sku_name = str(target_model.get("1688_sku_name") or "").strip()
            current_sku_second_name = str(target_model.get("1688_sku_second_name") or "").strip()
            if current_sku_name == sku_name and current_sku_second_name == sku_second_name:
                continue
            if sku_name:
                target_model["1688_sku_name"] = sku_name
            else:
                target_model.pop("1688_sku_name", None)
            if sku_second_name:
                target_model["1688_sku_second_name"] = sku_second_name
            else:
                target_model.pop("1688_sku_second_name", None)
            changed_models.append(target_model)

        backup_path = ""
        if changed_models or product_created:
            backup_path = f"{golden_path}.bak"
            shutil.copy2(golden_path, backup_path)
            with open(golden_path, "w", encoding="utf-8") as f:
                json.dump(golden_table, f, ensure_ascii=False, indent=4)

        for target_model, sku_name, _ in resolved:
            self._sync_model_1688_sku_binding(product_id, product, target_model, sku_name)

        return {
            "status": "success",
            "message": (
                f"已新增商品並更新 {len(changed_models)} 個 1688 型號對應"
                if product_created else f"已更新 {len(changed_models)} 個 1688 型號對應"
            ),
            "productId": product_id,
            "productCreated": product_created,
            "changedCount": len(changed_models),
            "backupPath": backup_path,
            "updatedModels": [target_model for target_model, _, _ in resolved],
        }

    def _ensure_golden_table_product(self, golden_table, product_id, spec_id="", model_name=""):
        """必要時從本次蝦皮搜尋快取匯入新商品，讓編輯 API 可直接寫入 golden table。"""
        product = golden_table.get(product_id)
        if isinstance(product, dict):
            return product, False

        source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopee_products.json")
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"找不到商品ID: {product_id}（且沒有本次蝦皮搜尋快取）")
        try:
            with open(source_path, "r", encoding="utf-8") as f:
                source_table = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise FileNotFoundError(f"找不到商品ID: {product_id}（無法讀取蝦皮搜尋快取）") from e

        source_product = source_table.get(product_id)
        if not isinstance(source_product, dict):
            raise FileNotFoundError(f"找不到商品ID: {product_id}")

        source_models = source_product.get("型號", [])
        if not isinstance(source_models, list):
            raise ValueError(f"商品 {product_id} 的型號資料格式不正確")
        if spec_id or model_name:
            source_model = self._find_golden_model(source_models, spec_id, model_name)
            if source_model is None:
                raise FileNotFoundError(f"商品ID {product_id} 在蝦皮搜尋快取中找不到對應型號")

        # 使用 JSON round-trip 複製，避免直接共用快取物件並保留既有欄位結構。
        product = json.loads(json.dumps(source_product, ensure_ascii=False))
        golden_table[product_id] = product
        return product, True

    def _find_golden_model(self, models, spec_id, model_name):
        if spec_id:
            for model in models:
                if normalize_identifier(model.get("規格ID", "")) == spec_id:
                    return model

        if model_name:
            for model in models:
                if str(model.get("型號名稱", "")).strip() == model_name:
                    return model

        return None

    def shutdown_server(self):
        """關閉伺服器並釋放端口"""
        # 等待一小段時間確保回應已發送
        time.sleep(0.5)

        # 嘗試終止所有爬蟲進程
        self.stop_running_crawler()

        logger.info("正在關閉伺服器並釋放端口...")

        # 嘗試正常關閉伺服器
        try:
            # 使用 threading.Timer 延遲關閉，確保回應已發送
            def delayed_exit():
                logger.info("程序正常退出")
                # 使用 sys.exit 代替 os._exit 以允許正常的清理
                import sys
                sys.exit(0)

            threading.Timer(1.0, delayed_exit).start()
        except Exception as e:
            logger.exception(f"關閉伺服器時出錯: {e}")
            # 如果正常關閉失敗，使用強制關閉
            os._exit(0)

    @staticmethod
    def stop_running_crawler():
        """中斷正在運行的爬蟲進程及其所有子進程"""
        global current_crawler_process

        if current_crawler_process is not None and current_crawler_process.poll(
        ) is None:
            try:
                logger.info(f"嘗試中斷爬蟲進程 (PID: {current_crawler_process.pid})")

                # 使用 psutil 獲取進程及其所有子進程
                parent = psutil.Process(current_crawler_process.pid)
                children = parent.children(recursive=True)

                # 先終止所有子進程
                for child in children:
                    try:
                        logger.info(f"終止子進程 PID: {child.pid}")
                        child.terminate()
                    except Exception as e:
                        logger.error(f"終止子進程 {child.pid} 時出錯: {e}")

                # 等待子進程終止
                gone, alive = psutil.wait_procs(children, timeout=3)

                # 強制終止仍然存活的子進程
                for p in alive:
                    try:
                        logger.info(f"強制終止子進程 PID: {p.pid}")
                        p.kill()
                    except Exception as e:
                        logger.error(f"強制終止子進程 {p.pid} 時出錯: {e}")

                # 終止主進程
                try:
                    parent.terminate()
                    parent.wait(timeout=3)
                    logger.info(f"已終止主進程 PID: {parent.pid}")
                except Exception as e:
                    logger.error(f"終止主進程時出錯: {e}")
                    try:
                        parent.kill()
                        logger.info(f"已強制終止主進程 PID: {parent.pid}")
                    except Exception as e:
                        logger.error(f"強制終止主進程時出錯: {e}")

                # 確保進程已終止
                if current_crawler_process.poll() is None:
                    if os.name == 'nt':
                        logger.info(
                            f"使用taskkill終止進程樹 PID: {current_crawler_process.pid}"
                        )
                        os.system(
                            f"taskkill /F /PID {current_crawler_process.pid} /T"
                        )
                    else:
                        logger.info(
                            f"使用SIGKILL終止進程 PID: {current_crawler_process.pid}"
                        )
                        os.kill(current_crawler_process.pid, signal.SIGKILL)

                logger.info(f"爬蟲進程已成功中斷")
                current_crawler_process = None
                return True

            except Exception as e:
                logger.exception(f"中斷爬蟲進程時出錯: {e}")
                import traceback
                traceback.print_exc()
                return False
        else:
            logger.info("沒有正在運行的爬蟲進程")
            return False

    def run_crawler(self, keyword, show_browser=False, inventory_month=4):
        """執行爬蟲程序"""
        output_path = "shopee_products.json"
        mode_args = [
            keyword, "--output", output_path,
            "--headless",
            str(not show_browser).lower(), "--inventory-month",
            str(inventory_month)
        ]
        result = self.run_worker_process(
            mode_args,
            output_path,
            task_name="爬蟲",
            timeout=20000,
        )
        if result.get("status") == "error":
            return {"error": result.get("message", "爬蟲執行失敗")}
        return result

    def run_ads_export(self, show_browser=True):
        """執行蝦皮廣告匯出程序"""
        output_path = "ads_export_result.json"
        mode_args = [
            "--mode", "ads-export",
            "--output", output_path,
            "--headless", str(not show_browser).lower()
        ]
        return self.run_worker_process(
            mode_args,
            output_path,
            task_name="廣告匯出",
            timeout=5400,
        )

    def run_ads_analysis(
        self,
        include_ai=True,
        model=DEFAULT_OPENAI_MODEL,
        reasoning_effort=DEFAULT_OPENAI_REASONING_EFFORT,
    ):
        """執行蝦皮廣告分析程序"""
        output_path = "ads_analysis_latest.json"
        script_name = "ads_analysis.py"
        mode_args = [
            "--output", output_path,
            "--history-output", "ads_history.json",
            "--markdown-output", "ads_analysis_report.md",
            "--include-ai", str(include_ai).lower(),
            "--refresh-source", "false",
            "--trend-weeks", "4",
            "--model", validate_openai_model(model),
            "--reasoning-effort", validate_reasoning_effort(reasoning_effort),
        ]
        return self.run_worker_process(
            mode_args,
            output_path,
            task_name="廣告分析",
            timeout=5400,
            script_name=script_name,
        )

    def _load_alibaba_links(self):
        """從 shopee_products.xlsx 讀取阿里巴巴連結映射。

        優先使用 型號ID；若缺少型號ID，則退回 商品名稱+型號名稱。
        """
        links_map = {}
        try:
            bindings = self._procurement_store().list_bindings()
            for key, binding in bindings.items():
                url = binding.get("alibabaProductUrl")
                if url:
                    links_map[key] = url
                    model_id = binding.get("modelId")
                    if model_id:
                        links_map[str(model_id)] = url
        except Exception as e:
            logger.warning(f"載入 SQLite 1688 綁定失敗: {e}")

        try:
            import pandas as pd
            xlsx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopee_products.xlsx")
            if not os.path.exists(xlsx_path):
                return links_map
            df = pd.read_excel(xlsx_path, engine="calamine")
            for _, row in df.iterrows():
                product_name = str(row.get("商品名稱", "")).strip()
                model_name = str(row.get("型號名稱", "")).strip()
                model_id = normalize_identifier(row.get("型號ID", ""))
                alibaba_link = str(row.get("阿里巴巴商品URL", "")).strip()
                if not alibaba_link or alibaba_link in ("", "nan", "None"):
                    continue
                if model_id and model_id not in ("", "nan", "None"):
                    links_map[model_id] = alibaba_link
                if product_name and product_name not in ("nan", "None") and model_name and model_name not in ("nan", "None"):
                    links_map[f"{product_name}|||{model_name}"] = alibaba_link
            return links_map
        except Exception as e:
            logger.warning(f"載入阿里巴巴連結失敗: {e}")
            return links_map

    def run_worker_process(self, worker_args, output_path, task_name="任務", timeout=20000, script_name="crawler.py"):
        """執行 worker 子程序並讀取 JSON 結果"""
        global current_crawler_process  # 全局變量聲明必須在函數開頭

        try:
            if current_crawler_process is not None and current_crawler_process.poll() is None:
                logger.warning(f"{task_name}啟動失敗：已有進程在執行")
                return {"status": "error", "message": "目前已有其他流程在執行，請稍後再試"}

            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    logger.warning(f"無法刪除舊結果文件: {output_path}")

            logger.info(f"===== 開始執行{task_name} =====")
            cmd = []
            executable = os.path.abspath(sys.executable)
            
            if getattr(sys, 'frozen', False) and script_name == "crawler.py":
                cmd = [executable, "--worker"]
            else:
                cmd = [executable, script_name]

            cmd.extend(worker_args)

            logger.info(f"{task_name}命令: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,  # 行緩衝，確保輸出及時顯示
                preexec_fn=None if os.name == 'nt' else os.setsid)

            current_crawler_process = process

            logger.info(f"{task_name}進程已啟動，PID: {process.pid}")

            def read_output(pipe, prefix):
                for line in pipe:
                    line_text = line.strip()
                    print(f"{prefix}: {line_text}")
                    # 同時記錄到日誌文件
                    logger.info(f"{prefix}: {line_text}")

            stdout_thread = threading.Thread(target=read_output,
                                             args=(process.stdout, f"{task_name}輸出"),
                                             daemon=True)
            stderr_thread = threading.Thread(target=read_output,
                                             args=(process.stderr, f"{task_name}錯誤"),
                                             daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            logger.info(f"{task_name}輸出讀取線程已啟動")

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.stop_running_crawler()
                logger.error(f"{task_name}執行超時，已強制終止")
                return {"status": "error", "message": f"{task_name}執行超時，已強制終止"}

            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

            current_crawler_process = None

            logger.info(f"{task_name}進程已完成，返回碼: {process.returncode}")

            if process.returncode != 0:
                if os.path.exists(output_path):
                    try:
                        with open(output_path, 'r', encoding='utf-8') as f:
                            error_result = json.load(f)
                        if error_result.get("status") == "error":
                            return error_result
                    except Exception as e:
                        logger.warning(f"讀取{task_name}錯誤結果失敗: {e}")
                if process.returncode == 77:
                    message = "Cookies 已失效，請重新更新 cookies.json"
                else:
                    message = f"{task_name}執行失敗"
                logger.error(f"{task_name}執行失敗，返回碼: {process.returncode}")
                return {"status": "error", "message": message}

            if not os.path.exists(output_path):
                logger.error(f"{task_name}未生成結果文件")
                return {"status": "error", "message": f"{task_name}未生成結果文件"}

            try:
                with open(output_path, 'r', encoding='utf-8') as f:
                    result = json.load(f)

                if isinstance(result, dict) and result.get("status") == "error":
                    return result

                logger.info(f"{task_name}完成")
                return result
            except json.JSONDecodeError:
                logger.error(f"{task_name}結果不是有效的 JSON 格式")
                return {"status": "error", "message": f"{task_name}結果不是有效的 JSON 格式"}

        except Exception as e:
            logger.exception(f"執行{task_name}時出錯: {e}")
            current_crawler_process = None
            return {"status": "error", "message": f"執行{task_name}時出錯: {e}"}


class RestockBatchPersister:
    def __init__(self, directory):
        self.directory = Path(directory)

    def __call__(self, state):
        saved = save_state(self.directory, state)
        if saved.get("status") != STATUS_RUNNING:
            paths = write_reports(self.directory, saved)
            saved["reportPath"] = str(paths["json"])
            saved["reportHtmlPath"] = str(paths["html"])
            save_state(self.directory, saved)

    def write_artifacts(self, product, job, row, result):
        write_job_artifact(self.directory, str(product.get("productId") or "unknown"), job)
        write_failure_artifacts(
            self.directory,
            extract_failure_records(product, row, result),
        )


def _restock_batches_root():
    return Path(os.path.dirname(os.path.abspath(__file__))) / "debug_snapshots" / "restock_batches"


def _restock_batch_is_running():
    with restock_batch_lock:
        thread = restock_batch_runtime.get("thread")
        return bool(thread and thread.is_alive())


def _restock_job_host():
    return CustomHandler.__new__(CustomHandler)


def start_single_product_from_batch(product):
    host = _restock_job_host()
    try:
        return host.start_alibaba_restock({
            "productId": product.get("productId"),
            "productName": product.get("productName"),
            "addToCart": True,
            "pauseSeconds": 0,
            "items": product_items(product),
        })
    except Exception as error:
        return {"status": "error", "message": str(error)}


def read_single_restock_job(job_id):
    return CustomHandler._read_alibaba_restock_job(job_id)


def run_restock_batch_thread(run_id):
    directory = _restock_batches_root() / run_id
    persister = RestockBatchPersister(directory)
    try:
        state = load_state(directory)
        run_batch_loop(
            state,
            start_single_product_from_batch,
            read_single_restock_job,
            persister,
            should_stop=lambda: restock_batch_runtime.get("stop"),
        )
    except Exception:
        logger.exception("批次補貨執行失敗")
        try:
            state = load_state(directory)
            state["status"] = "paused_attention"
            state["message"] = "批次控制器異常停止，請核對採購車後再繼續"
            persister(state)
        except Exception:
            logger.exception("寫入批次失敗狀態失敗")
    finally:
        with restock_batch_lock:
            if restock_batch_runtime.get("runId") == run_id:
                restock_batch_runtime["runId"] = None
                restock_batch_runtime["thread"] = None
                restock_batch_runtime["stop"] = False


def launch_restock_batch(state):
    with restock_batch_lock:
        thread = restock_batch_runtime.get("thread")
        if thread and thread.is_alive():
            raise ValueError("已有批次補貨在執行")
        run_id = str(state.get("runId") or "")
        directory = _restock_batches_root() / run_id
        save_state(directory, begin_run(state) if state.get("status") != STATUS_RUNNING else state)
        restock_batch_runtime["runId"] = run_id
        restock_batch_runtime["stop"] = False
        worker = threading.Thread(target=run_restock_batch_thread, args=(run_id,), daemon=True)
        restock_batch_runtime["thread"] = worker
        worker.start()
    return load_state(directory)


def kill_process_on_port(port):
    """終止佔用指定端口的進程"""
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                connections = proc.net_connections()
                for conn in connections:
                    if conn.laddr.port == port:
                        logger.info(f"發現進程 {proc.pid} ({proc.name()}) 正在使用端口 {port}")
                        logger.info(f"正在終止進程 {proc.pid}...")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                            logger.info(f"進程 {proc.pid} 已成功終止")
                        except psutil.TimeoutExpired:
                            logger.warning(f"進程 {proc.pid} 未在 3 秒內終止，強制終止...")
                            proc.kill()
                            logger.info(f"進程 {proc.pid} 已強制終止")
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        logger.info(f"端口 {port} 未被任何進程佔用")
        return False
    except Exception as e:
        logger.error(f"檢查端口 {port} 時出錯: {e}")
        return False


def find_free_port(start_port):
    port = start_port
    max_port = start_port + 100  # 嘗試 100 個端口

    while port < max_port:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", port))
                return port
        except OSError:
            port += 1

    raise RuntimeError("無法找到可用的端口")


# 啟動 HTTP 伺服器
def start_server():
    global httpd  # 將 httpd 設為全局變量，以便其他函數可以訪問

    try:
        # 在啟動伺服器前，先終止佔用端口的進程
        logger.info(f"檢查端口 {PORT} 是否被佔用...")
        kill_process_on_port(PORT)
        
        # 等待一小段時間確保端口已釋放
        time.sleep(0.5)

        class TCPServerReuse(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            # A stalled browser request must not block mapping decisions,
            # summary refreshes, or other local API calls.
            daemon_threads = True

        with TCPServerReuse(("", PORT), CustomHandler) as httpd:
            # 獲取實際分配的端口
            actual_port = httpd.server_address[1]
            logger.info(f"✅ 伺服器啟動於 http://localhost:{PORT}")

            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                logger.info("接收到鍵盤中斷，正在關閉伺服器...")
            finally:
                httpd.server_close()
                logger.info("伺服器已關閉，端口已釋放")
    except Exception as e:
        logger.exception(f"伺服器啟動失敗: {e}")


# 全局變量
httpd = None

# ===== 啟動時檢查版本更新 =====
try:
    check_for_updates()
except Exception as e:
    logger.warning(f"版本檢查失敗：{e}")
# ============================

try:
    recovered_restock_batches = recover_interrupted_batches(_restock_batches_root())
    if recovered_restock_batches:
        logger.info("已將中斷的批次補貨標記為待核對：%s", ", ".join(recovered_restock_batches))
except Exception as e:
    logger.warning("還原批次補貨狀態失敗：%s", e)

# 先啟動伺服器
server_thread = threading.Thread(target=start_server, daemon=True)
server_thread.start()

# 等待 2 秒，確保伺服器已啟動
time.sleep(2)

# 正式補貨腳本會自己開 review 頁，避免再彈一個沒載入資料的分頁。
if os.environ.get("INVENTORY_SKIP_BROWSER") != "1":
    webbrowser.open(f"http://localhost:{PORT}")
    logger.info(f"已嘗試在瀏覽器中打開 http://localhost:{PORT}")
else:
    logger.info("INVENTORY_SKIP_BROWSER=1，改由啟動腳本開啟瀏覽器")

# 主線程等待
try:
    # 使用 server_thread.join() 而不是無限循環
    while server_thread.is_alive():
        time.sleep(1)
except KeyboardInterrupt:
    logger.info("接收到鍵盤中斷，程式即將結束...")
    # 發送關閉請求
    try:
        import urllib.request
        urllib.request.urlopen(f"http://localhost:{PORT}/shutdown")
    except Exception as e:
        logger.error(f"發送關閉請求時出錯: {e}")
except Exception as e:
    logger.exception(f"發生未預期的錯誤: {e}")
finally:
    # 確保所有資源都被釋放
    logger.info("正在清理資源...")
    # 嘗試終止所有爬蟲進程
    try:
        CustomHandler.stop_running_crawler()
    except Exception as e:
        logger.exception(f"清理資源時出錯: {e}")
    # 如果還有其他需要清理的資源，在這裡添加
    logger.info("===== 程序結束於 %s =====" %
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


# 註冊退出時的清理函數
def cleanup_resources():
    logger.info("程式退出，正在清理資源...")
    # 嘗試終止所有爬蟲進程
    try:
        if 'current_crawler_process' in globals(
        ) and current_crawler_process is not None:
            CustomHandler.stop_running_crawler()
    except Exception as e:
        logger.exception(f"退出時清理資源出錯: {e}")
    # 如果還有其他需要清理的資源，在這裡添加
    logger.info("===== 程序結束於 %s =====" %
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


atexit.register(cleanup_resources)
