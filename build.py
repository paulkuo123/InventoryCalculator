import PyInstaller.__main__
import os
import shutil
import re

def get_version():
    """從 version.py 讀取版本號"""
    version_file = os.path.join(os.path.dirname(__file__), "version.py")
    if not os.path.exists(version_file):
        print("⚠️  找不到 version.py，使用預設版本 0.0.0")
        return "0.0.0"
    
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            content = f.read()
            match = re.search(r'CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"⚠️  讀取版本號失敗：{e}")
    
    return "0.0.0"

def build():
    # 取得版本號
    version = get_version()
    print(f"📦 開始打包版本：v{version}")
    
    # 清理舊的構建文件
    if os.path.exists('dist'):
        shutil.rmtree('dist')
    if os.path.exists('build'):
        shutil.rmtree('build')

    # 定義資源文件 (源文件, 目標文件夾)
    # Windows 使用 ; 分隔，Mac/Linux 使用 : 分隔
    separator = ';' if os.name == 'nt' else ':'

    add_data = [
        f'index.html{separator}.',
        f'script.js{separator}.',
        f'styles.css{separator}.',
        f'version.py{separator}.'  # 加入 version.py 以便執行檔能檢查版本
    ]

    # PyInstaller 參數
    args = [
        'main.py',                        # 主程序入口
        '--name=ShopeeCrawler',           # 執行檔名稱
        '--onefile',                      # 打包成單一文件
        '--log-level=INFO',               # 日誌級別
        '--noconfirm',                    # 不詢問確認
        '--clean',                        # 清理緩存
        f'--add-version={version}',       # 加入版本號

        # 添加數據文件
        *[f'--add-data={data}' for data in add_data],

        # 隱藏導入 (確保這些庫被包含)
        '--hidden-import=requests',
        '--hidden-import=PyQt5',
        '--hidden-import=pandas',
        '--hidden-import=playwright',

        # 排除不需要的模組以減小體積 (可選)
        # '--exclude-module=tkinter',
    ]

    print("開始打包程序...")
    PyInstaller.__main__.run(args)
    print(f"✅ 打包完成！版本 v{version}，執行檔位於 dist/ 資料夾中。")

if __name__ == '__main__':
    build()
