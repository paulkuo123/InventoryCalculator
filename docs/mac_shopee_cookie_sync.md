# Mac 把蝦皮賣家中心登入同步到遠端盒

遠端盒上的 Chrome 常常被登出。庭安的 Mac Chrome 有登入，但 Mac 多半關機，所以這不是排程，是 **Mac 有開的時候手動跑一行**。

這支工具只讀 Mac Chrome 預設設定檔裡 `shopee.tw` 和 `*.shopee.tw` 的 cookie，用 `scp`／`ssh` 送到盒上，再由盒上**已經開著**的賣家中心 Chrome（CDP）匯入，打開賣家中心看有沒有登入。看完一定刪掉暫存檔。不會把 cookie 內容印出來，也不會改爬蟲、不會寫 `cookies.json`、不會關掉遠端 Chrome。

## 要先準備什麼

不用另外裝 Python 套件。解密用的是 Python 標準函式庫。

### Mac

- Python 3（跟這個專案一樣）
- 系統內建的 `security`（讀鑰匙圈）、`ssh`、`scp`
- [Tailscale](https://tailscale.com/) 已連上，而且跟遠端盒在同一個 tailnet
- Chrome 預設設定檔已經登入蝦皮賣家中心。Chrome 可以開著；程式只複製 Cookies 資料庫來讀，不會改原本的設定檔

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

## 第一次：鑰匙圈允許

第一次執行，macOS 會跳出視窗，問要不要允許 `security` 使用鑰匙圈裡的「Chrome Safe Storage」。按允許（或永遠允許）。

沒按的話會失敗，終端只會看到「讀不到 Chrome Safe Storage」，不會看到密碼，也不會看到 cookie。

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

Chrome 的 Cookies 資料庫預設先找：

`~/Library/Application Support/Google/Chrome/Default/Network/Cookies`

找不到才用舊路徑 `Default/Cookies`。要改路徑用 `--chrome-cookies` 或 `SHOPEE_CHROME_COOKIES_DB`。

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
說明：讀不到 Chrome Safe Storage，請在鑰匙圈視窗按允許
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
- 暫存檔權限 `600`，用完就刪，失敗也刪
- 不把 cookie 值印到終端
- 這些暫存檔名已在 `.gitignore`，不要提交
