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
pip install PyQt5
pip install selenium
pip install psutil
pip install webdriver-manager
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
本程式使用Selenium控制Chrome瀏覽器進行爬蟲，請確保您已安裝：
1. Google Chrome瀏覽器 (建議更新至最新版)

> **注意**: 安裝 webdriver-manager 套件後，系統會自動下載與您的 Chrome 瀏覽器版本匹配的 ChromeDriver，無需手動下載和安裝。

如果您仍然希望手動管理 ChromeDriver：
1. 可從 https://chromedriver.chromium.org/downloads 下載與您 Chrome 版本相符的 ChromeDriver
2. 將下載的 chromedriver.exe (Windows) 或 chromedriver (Mac/Linux) 放在適當位置

MacOS
如果遇到Mac將chromedriver視為惡意軟體的提示，請在終端執行以下指令：
```
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
