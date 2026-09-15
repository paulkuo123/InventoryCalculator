import os
import re
import shutil

# Web 入口 main.py 會服務的靜態頁（含 products / golden-import）。
# 獨立 PyQt 工具 calculator.py 不打進這個 bundle。
STATIC_ASSETS = (
    "index.html",
    "script.js",
    "styles.css",
    "inbound.html",
    "inbound.js",
    "inbound.css",
    "ads.html",
    "ads.js",
    "ads.css",
    "sku-mapping.html",
    "sku-mapping.js",
    "sku-mapping.css",
    "products.html",
    "products.js",
    "products.css",
    "golden-import.html",
    "golden-import.js",
    "golden-import.css",
    "version.py",
)


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
    version = get_version()
    print(f"📦 開始打包版本：v{version}")

    missing = [name for name in STATIC_ASSETS if not os.path.exists(name)]
    if missing:
        raise SystemExit("打包缺少靜態檔，請補檔或從 STATIC_ASSETS 移除：%s" % ", ".join(missing))

    if os.path.exists("dist"):
        shutil.rmtree("dist")
    if os.path.exists("build"):
        shutil.rmtree("build")

    separator = ";" if os.name == "nt" else ":"
    add_data = [f"{name}{separator}." for name in STATIC_ASSETS]

    args = [
        "main.py",
        "--name=ShopeeCrawler",
        "--onefile",
        "--log-level=INFO",
        "--noconfirm",
        "--clean",
        *[f"--add-data={data}" for data in add_data],
        "--hidden-import=requests",
        "--hidden-import=playwright",
    ]

    print("開始打包程序...")
    import PyInstaller.__main__
    PyInstaller.__main__.run(args)
    print(f"✅ 打包完成！版本 v{version}，執行檔位於 dist/ 資料夾中。")


if __name__ == "__main__":
    build()
