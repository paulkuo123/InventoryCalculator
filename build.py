import PyInstaller.__main__
import os
import shutil

def build():
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
        f'styles.css{separator}.'
    ]

    # PyInstaller 參數
    args = [
        'main.py',                        # 主程序入口
        '--name=ShopeeCrawler',           # 執行檔名稱
        '--onefile',                      # 打包成單一文件
        '--log-level=INFO',               # 日誌級別
        '--noconfirm',                    # 不詢問確認
        '--clean',                        # 清理緩存
        
        # 添加數據文件
        *[f'--add-data={data}' for data in add_data],
        
        # 隱藏導入 (確保這些庫被包含)
        '--hidden-import=selenium',
        '--hidden-import=webdriver_manager',
        '--hidden-import=requests',
        '--hidden-import=PyQt5',
        
        # 排除不需要的模組以減小體積 (可選)
        # '--exclude-module=tkinter',
    ]

    print("開始打包程序...")
    PyInstaller.__main__.run(args)
    print("打包完成！執行檔位於 dist/ 資料夾中。")

if __name__ == '__main__':
    build()
