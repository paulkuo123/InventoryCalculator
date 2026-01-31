import http.server
import socketserver
import webbrowser
import threading
import os
import time
import json
import urllib.parse
import subprocess
import tempfile
import signal
import psutil  # 需要安裝: pip install psutil
import atexit
import socket
import sys
import logging
import datetime

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
        if self.path == '/' or any(self.path.endswith(ext) for ext in ['.html', '.css', '.js']):
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
        global current_crawler_process  # 全局變量聲明必須在函數開頭

        try:
            # 使用固定的輸出路徑
            output_path = "shopee_products.json"

            logger.info(f"===== 開始執行爬蟲 =====")
            logger.info(f"搜尋關鍵字: {keyword}")
            logger.info(f"顯示瀏覽器: {show_browser}")
            logger.info(f"庫存月份: {inventory_month}")

            # 設定爬蟲命令
            # 設定爬蟲命令
            cmd = []
            # 確保執行檔路徑為絕對路徑
            executable = os.path.abspath(sys.executable)
            
            if getattr(sys, 'frozen', False):
                # 凍結環境 (打包後)：呼叫自身並帶上 --worker 參數，利用 multiprocessing 技術
                cmd = [executable, "--worker"]
            else:
                # 開發環境：直接呼叫 crawler.py
                cmd = [executable, "crawler.py"]

            # 添加通用參數
            cmd.extend([
                keyword, "--output", output_path,
                "--headless",
                str(not show_browser).lower(), "--inventory-month",
                str(inventory_month)
            ])

            logger.info(f"爬蟲命令: {' '.join(cmd)}")

            # 執行爬蟲腳本，並實時顯示輸出
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,  # 行緩衝，確保輸出及時顯示
                # 在 Unix/Linux 上創建新進程組，以便於後續終止
                preexec_fn=None if os.name == 'nt' else os.setsid)

            # 保存當前爬蟲進程的引用
            current_crawler_process = process

            logger.info(f"爬蟲進程已啟動，PID: {process.pid}")

            # 創建線程來實時讀取和顯示輸出，並記錄到日誌文件
            def read_output(pipe, prefix):
                for line in pipe:
                    line_text = line.strip()
                    print(f"{prefix}: {line_text}")
                    # 同時記錄到日誌文件
                    logger.info(f"{prefix}: {line_text}")

            # 啟動輸出讀取線程
            stdout_thread = threading.Thread(target=read_output,
                                             args=(process.stdout, "爬蟲輸出"),
                                             daemon=True)
            stderr_thread = threading.Thread(target=read_output,
                                             args=(process.stderr, "爬蟲錯誤"),
                                             daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            logger.info("爬蟲輸出讀取線程已啟動")

            # 等待爬蟲完成，設置超時
            try:
                process.wait(timeout=20000)
            except subprocess.TimeoutExpired:
                self.stop_running_crawler()
                logger.error("爬蟲執行超時，已強制終止")
                return {"error": "爬蟲執行超時，已強制終止"}

            # 確保輸出讀取線程完成
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

            # 清除當前爬蟲進程的引用
            current_crawler_process = None

            logger.info(f"爬蟲進程已完成，返回碼: {process.returncode}")

            if process.returncode != 0:
                logger.error(f"爬蟲執行失敗，返回碼: {process.returncode}")
                return {"error": "爬蟲執行失敗"}

            # 檢查結果文件是否存在
            if not os.path.exists(output_path):
                logger.error("爬蟲未生成結果文件")
                return {"error": "爬蟲未生成結果文件"}

            # 讀取爬蟲結果
            try:
                with open(output_path, 'r', encoding='utf-8') as f:
                    result = json.load(f)

                logger.info(f"爬取完成，找到 {len(result)} 個商品")
                return result
            except json.JSONDecodeError:
                logger.error("爬蟲結果不是有效的 JSON 格式")
                return {"error": "爬蟲結果不是有效的 JSON 格式"}

        except Exception as e:
            logger.exception(f"執行爬蟲時出錯: {e}")
            return {"error": f"執行爬蟲時出錯: {e}"}


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
