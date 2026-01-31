# 蝦皮庫存管理系統

## 簡介
蝦皮庫存管理系統是一個整合了蝦皮商品爬蟲和庫存計算功能的工具，幫助賣家更有效地管理庫存和分析銷售數據。

## 主要功能
- **蝦皮商品爬蟲**：自動抓取蝦皮商品資訊和銷售數據
- **銷售數據分析**：分析商品銷售趨勢和表現
- **庫存計算**：根據銷售數據計算最佳庫存量

## 環境搭建

### 必要套件
```
pip install -r requirements.txt
```

> **注意**: webdriver-manager 套件可以自動下載與當前 Chrome 瀏覽器版本匹配的 ChromeDriver，避免版本不匹配的問題。

## 使用方法

### 下載專案
```
git clone https://github.com/paulkuo123/InventoryCalculator.git
```

### 切換至專案資料夾
```
cd InventoryCalculator
```

### Chrome瀏覽器設置
本程式使用 Selenium 控制 Chrome 瀏覽器進行爬蟲。

**必要條件**：
1. 請確保您已安裝 Google Chrome 瀏覽器 (建議更新至最新版)。

**關於 ChromeDriver**：
本專案已整合 `webdriver-manager`，程式執行時會**自動下載並配置**與您 Chrome 版本相匹配的 ChromeDriver，**您無需手動下載或設定**。

#### (進階選項) 手動管理 ChromeDriver
如果您因特殊需求需要手動管理 ChromeDriver (例如在無網路環境或自動下載失敗時)：
1. 可從 [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) 下載對應版本的 ChromeDriver。
2. Windows: 將 `chromedriver.exe` 放在專案根目錄。
3. Mac/Linux: 將 `chromedriver` 放在專案根目錄或是 `/opt/homebrew/bin/chromedriver` (Mac)。

MacOS 手動安裝注意事項：
若遇到 Mac 將手動下載的 chromedriver 視為惡意軟體，請執行：
```bash
xattr -d com.apple.quarantine /path/to/chromedriver
```

### 設置 Cookies
1. 至Chrome線上應用程式商店安裝Cookies-Editors (https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm?hl=zh-TW&utm_source=ext_sidebar)
2. 登入蝦皮賣家中心
3. 點擊Cookies-Editors 
4. Export輸出json格式
5. 複製內容貼到cookies.json

### 執行主程式
```
python main.py
```
執行後會自動開啟網頁介面，您可以在此進行商品搜尋和數據分析。

## 應用程式打包 (發布)

如果您希望將程式打包成獨立的執行檔 (例如方便分發給其他電腦使用)，請執行以下指令：

1. 確保已安裝打包工具：
```bash
pip install -r requirements.txt
```

2. 執行打包腳本：
```bash
python build.py
```

3. 打包完成後，執行檔將位於 `dist/` 資料夾中。
   - 將 `dist/ShopeeCrawler` (或 `ShopeeCrawler.exe`) 以及 `cookies.json` 複製到目標電腦即可使用，無需安裝 Python。

## Demo

![image](images/main.png)

### 額外小功能 - InventoryCalculator
除了主要的爬蟲功能外，本專案還包含一個獨立的庫存計算工具，可以幫助您計算最佳庫存量：
```
python calculator.py
```

## 系統需求
- Python 3.6+
- 作業系統：Windows, macOS, Linux

## Demo

![image](images/demo_v3.png)
