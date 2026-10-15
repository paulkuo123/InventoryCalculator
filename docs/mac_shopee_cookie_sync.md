# Mac 把蝦皮賣家中心登入同步到遠端盒

遠端盒上的 Chrome 常常被登出。庭安的 Mac 用 Ego Lite 登入蝦皮，但 Mac 多半關機，所以這不是排程，是 **Mac 有開的時候手動跑一行**。

這支工具預設只讀 Mac 上 Ego Lite 預設設定檔裡 `shopee.tw` 和 `*.shopee.tw` 的 cookie（要用 Chrome 加 `--browser chrome`），用 `ssh` 從標準輸入寫到盒上（遠端先 `umask 077`，檔案一建立就是權限 `600`），再由盒上**已經開著**的賣家中心 Chrome（CDP）匯入，打開賣家中心看有沒有登入。看完一定刪掉暫存檔。不會把 cookie 內容印出來，也不會改爬蟲、不會寫 `cookies.json`、不會關掉遠端 Chrome。

## 要先準備什麼

不用另外裝 Python 套件。解密用的是 Python 標準函式庫。

### Mac

- Python 3（跟這個專案一樣）
- 系統內建的 `security`（讀鑰匙圈）、`ssh`
- [Tailscale](https://tailscale.com/) 已連上，而且跟遠端盒在同一個 tailnet
- Ego Lite 預設設定檔已經登入蝦皮賣家中心（要用 Chrome 時改 `--browser chrome`）。瀏覽器可以開著；程式只複製 Cookies 資料庫來讀，不會改原本的設定檔

先在 Mac 手動登入盒一次，確認主機指紋，之後要能免密碼（ssh 金鑰）：

```bash
ssh 你的帳號@100.82.41.67
```

`100.82.41.67` 是目前這台盒的 Tailscale 位址，**只是例子**。程式沒有把這個 IP 寫死，請用下面的參數或環境變數自己帶。換位址就改參數，不用改程式。

### 遠端盒

- 這份專案已更新到含 `shopee_cookie_sync.py` 的版本（盒上要先拉下來）
- 賣家中心那個 Chrome 已開 remote debugging。沒另外指定時，程式會先試 `127.0.0.1:9235`，不行再試 `9239`
- 盒上已有 Playwright（爬蟲本來就要）

廣告週報的 `9232`、1688 的 `9223`／`9227` 這支工具不會拿去用。

`--remote-dir` 請寫盒上的**絕對路徑**（不要寫 `~`，引號包起來之後不會展開家目錄）。

## Ego Lite 查到什麼

官方站和 GitHub 都寫 Ego Lite 是 Chromium 核心，目前有 macOS 版：

- [lite.ego.app](https://lite.ego.app/)（「Built on Chromium」、「custom Chromium engine」）
- [citrolabs/ego-lite](https://github.com/citrolabs/ego-lite)

cookie 資料庫路徑和鑰匙圈名稱，官方文件沒寫。第三方 [SweetCookieKit 的瀏覽器目錄](https://github.com/steipete/SweetCookieKit/blob/main/Sources/SweetCookieKit/BrowserCatalog.swift) 把 Ego Lite 當成 Chromium，路徑是 `~/Library/Application Support/Citro Labs/ego lite`，鑰匙圈服務名稱是 `ego safe storage`、帳號是 `ego`。同一套程式在 [ChromeCookieImporter.swift](https://github.com/steipete/SweetCookieKit/blob/main/Sources/SweetCookieKit/ChromeCookieImporter.swift) 會先找 `Default/Network/Cookies`，沒有才用 `Default/Cookies`，而且只解 `v10`。

加密前綴是 `v10` 還是 `v11`：**未確認**。官方沒寫。這支工具仍用現有的 Chromium `v10` 解法。若實際是 `v11`，終端只會看到「不支援 v11 cookie 加密」，不會印出 cookie。那時不要硬解。

下面兩個預設都依 SweetCookieKit，不是官方規格，所以都可以覆寫。

## 第一次：自己確認路徑和鑰匙圈（不要印出密碼）

先看 cookie 檔在不在（這行只列檔名，不會讀內容）：

```bash
ls -d "$HOME/Library/Application Support/Citro Labs/ego lite/Default/Network/Cookies" \
      "$HOME/Library/Application Support/Citro Labs/ego lite/Default/Cookies"
```

再看鑰匙圈有沒有這個項目。**不要加 `-w` 或 `-g`**，那兩個會把密碼印出來：

```bash
security find-generic-password -s "ego safe storage"
```

有這個項目就會印出服務名稱（`svce`）和帳號（`acct`），不會印密碼。預設程式用服務名稱 `ego safe storage`、帳號 `ego`。對不上就用參數改，不要把查到的密碼貼進指令。

第一次真正同步時，macOS 會跳出視窗，問要不要允許 `security` 讀這個 Safe Storage。按允許（或永遠允許）。

沒按的話會失敗，終端只會看到「讀不到瀏覽器 Safe Storage」，不會看到密碼，也不會看到 cookie。

## 怎麼跑

在專案目錄：

```bash
python3 shopee_cookie_sync.py sync --ssh 你的帳號@100.82.41.67 --remote-dir /home/你的帳號/InventoryCalculator
```

或先設環境變數，之後只要一行：

```bash
export SHOPEE_COOKIE_SYNC_SSH='你的帳號@100.82.41.67'
export SHOPEE_COOKIE_SYNC_REMOTE_DIR='/home/你的帳號/InventoryCalculator'
python3 shopee_cookie_sync.py sync
```

只想指定其中一個 CDP port：

```bash
python3 shopee_cookie_sync.py sync --ssh 你的帳號@100.82.41.67 --remote-dir /home/你的帳號/InventoryCalculator --cdp 9235
```

也可以設 `SHOPEE_SELLER_CDP=9235,9239`（參數會蓋過環境變數）。

預設讀 Ego Lite。路徑或鑰匙圈和上面查到的不一樣時，自己覆寫：

```bash
python3 shopee_cookie_sync.py sync \
  --ssh 你的帳號@100.82.41.67 \
  --remote-dir /home/你的帳號/InventoryCalculator \
  --cookie-db "$HOME/Library/Application Support/Citro Labs/ego lite/Default/Network/Cookies" \
  --keychain-service "ego safe storage" \
  --keychain-account "ego"
```

要用 Mac 上的 Chrome，而不是 Ego Lite：

```bash
python3 shopee_cookie_sync.py sync --browser chrome --ssh 你的帳號@100.82.41.67 --remote-dir /home/你的帳號/InventoryCalculator
```

Chrome 的預設路徑是 `~/Library/Application Support/Google/Chrome/Default/Network/Cookies`，找不到才用 `Default/Cookies`，鑰匙圈是 `Chrome Safe Storage`／帳號 `Chrome`。

`--browser` 也可以用環境變數 `SHOPEE_COOKIE_SYNC_BROWSER`（`egolite` 或 `chrome`）。cookie 路徑可以用 `--cookie-db` 或 `SHOPEE_COOKIE_DB`（舊的 `--chrome-cookies`／`SHOPEE_CHROME_COOKIES_DB` 仍然有效）。鑰匙圈用 `--keychain-service`、`--keychain-account`，或 `SHOPEE_COOKIE_SYNC_KEYCHAIN_SERVICE`、`SHOPEE_COOKIE_SYNC_KEYCHAIN_ACCOUNT`。

## 成功長這樣

```text
cookie 筆數：12
結果：成功
說明：賣家中心已登入
CDP：http://127.0.0.1:9235
```

結束碼是 0。畫面上只有筆數、成功，以及用了哪個 CDP。不會有 cookie 內容。

## 失敗長這樣

登入還是過期：

```text
cookie 筆數：12
結果：失敗
說明：賣家中心仍未登入
CDP：http://127.0.0.1:9235
```

盒上的 Chrome 沒開，或 port 不對：

```text
cookie 筆數：12
結果：失敗
說明：連不上賣家中心 Chrome
```

鑰匙圈沒按允許：

```text
cookie 筆數：0
結果：失敗
說明：讀不到瀏覽器 Safe Storage，請在鑰匙圈視窗按允許
```

傳不過去（Tailscale 或 ssh 沒通）：

```text
cookie 筆數：12
結果：失敗
說明：傳到 box 失敗
```

結束碼是 1。暫存檔還是會刪掉，失敗也一樣。Mac 本機的暫存檔也會刪。

常見原因：

- 鑰匙圈視窗沒按允許
- Tailscale 沒連，或 ssh 金鑰還沒設好（先手動 `ssh` 一次）
- 盒上 Chrome 沒開，或 CDP 不是 9235／9239
- Mac 上的蝦皮登入本身已經過期（有筆數，但賣家中心仍未登入）
- 盒上的專案目錄不對，或還沒拉到這支程式

## 安全

- 只處理 `shopee.tw` 和 `*.shopee.tw`，其他網域不會送上盒
- 暫存檔用 `ssh` 寫入，遠端先 `umask 077`，一建立就是權限 `600`；用完就刪，失敗也刪。cookie 只走標準輸入，不會放進命令參數
- 不把 cookie 值印到終端
- 這些暫存檔名已在 `.gitignore`，不要提交
