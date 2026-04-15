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

# 記錄啟動信息
logger.info(
    f"===== 程序啟動於 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====="
)
logger.info(f"操作系統: {os.name}, Python版本: {sys.version}")

PORT = 8080  # 改為其他未被使用的端口，如 8080, 8888, 9000 等
FILE_NAME = get_resource_path("index.html")
current_crawler_process = None

# 確保 index.html 存在 (僅在非打包環境檢查，或確保打包時已包含)
if not os.path.exists(FILE_NAME) and not getattr(sys, 'frozen', False):
    with open(FILE_NAME, "w", encoding="utf-8") as f:
        f.write("<h1>伺服器運行中！</h1>")
    logger.info(f"創建了 {FILE_NAME} 文件")


# 自定義處理器
class CustomHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
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

        if self.path.startswith('/analyze_ads'):
            try:
                include_ai = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                ).get('includeAI', ['true'])[0].lower() == 'true'

                result = self.run_ads_analysis(include_ai=include_ai)

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
        if self.path == '/api/golden-table/model-alibaba':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(post_data) if post_data else {}
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

    def _send_json_response(self, status_code, payload):
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def _update_golden_table_model_alibaba(self, payload):
        product_id = normalize_identifier(payload.get("productId", ""))
        spec_id = normalize_identifier(payload.get("specId", ""))
        model_name = str(payload.get("modelName", "")).strip()
        alibaba_product_name = str(payload.get("alibabaProductName", "")).strip()
        alibaba_product_url = str(payload.get("alibabaProductUrl", "")).strip()
        apply_scope = str(payload.get("applyScope", "single")).strip()

        if apply_scope not in ("single", "fill_missing", "overwrite_all"):
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

        product = golden_table.get(product_id)
        if not isinstance(product, dict):
            raise FileNotFoundError(f"找不到商品ID: {product_id}")

        models = product.get("型號", [])
        if not isinstance(models, list):
            raise ValueError(f"商品 {product_id} 的型號資料格式不正確")

        target_model = self._find_golden_model(models, spec_id, model_name)
        if target_model is None:
            raise FileNotFoundError("找不到對應型號")

        if apply_scope == "single":
            models_to_update = [target_model]
        elif apply_scope == "fill_missing":
            models_to_update = [
                model for model in models
                if not str(model.get("阿里巴巴商品URL", "")).strip()
            ]
        else:
            models_to_update = models

        if target_model not in models_to_update:
            models_to_update.append(target_model)

        for model in models_to_update:
            model["阿里巴巴商品名稱"] = alibaba_product_name
            model["阿里巴巴商品URL"] = alibaba_product_url

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{golden_path}.bak_{timestamp}"
        shutil.copy2(golden_path, backup_path)

        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(golden_table, f, ensure_ascii=False, indent=4)

        return {
            "status": "success",
            "message": "已更新阿里巴巴資料",
            "productId": product_id,
            "applyScope": apply_scope,
            "updatedCount": len(models_to_update),
            "updatedModels": models_to_update
        }

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

    def stop_running_crawler(self):
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

    def run_ads_analysis(self, include_ai=True):
        """執行蝦皮廣告分析程序"""
        output_path = "ads_analysis_latest.json"
        script_name = "ads_analysis.py"
        mode_args = [
            "--output", output_path,
            "--history-output", "ads_history.json",
            "--markdown-output", "ads_analysis_report.md",
            "--include-ai", str(include_ai).lower(),
            "--refresh-source", "false",
            "--trend-weeks", "6",
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
        try:
            import pandas as pd
            xlsx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopee_products.xlsx")
            if not os.path.exists(xlsx_path):
                return {}
            df = pd.read_excel(xlsx_path)
            links_map = {}
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
            return {}

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

        class TCPServerReuse(socketserver.TCPServer):
            allow_reuse_address = True

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

# 先啟動伺服器
server_thread = threading.Thread(target=start_server, daemon=True)
server_thread.start()

# 等待 2 秒，確保伺服器已啟動
time.sleep(2)

# 嘗試開啟瀏覽器
webbrowser.open(f"http://localhost:{PORT}")
logger.info(f"已嘗試在瀏覽器中打開 http://localhost:{PORT}")

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
        # 創建一個 CustomHandler 實例來訪問 stop_running_crawler 方法
        handler = CustomHandler(None, None, None)
        handler.stop_running_crawler()
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
            # 創建一個 CustomHandler 實例來訪問 stop_running_crawler 方法
            handler = CustomHandler(None, None, None)
            handler.stop_running_crawler()
    except Exception as e:
        logger.exception(f"退出時清理資源出錯: {e}")
    # 如果還有其他需要清理的資源，在這裡添加
    logger.info("===== 程序結束於 %s =====" %
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


atexit.register(cleanup_resources)
