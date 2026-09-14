# Golden AI-2 CDP live-health — P2 url_suspect 應補（36 unique offers）

日期（UTC 探測）：2026-09-09 01:55–02:04Z（title enrich 02:06–02:10Z）  
方法：`cdp/browser`（Chrome CDP `http://127.0.0.1:9232`，chrome-profile-10，DISPLAY :10）  
Session：重用既有已登入 1688 分頁，只導航、只讀 DOM，**不加車／不點購買**  
Golden SHA-256（跑前＝跑後）：`8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e`  
**未修改** `golden_table.json`。未 merge main。

範圍：`priority_label=P2_url_suspect_restock`（109 型號列／應補約 860；36 獨立 offer）。P1 無 URL、P3 mapping_suspect 不在本批。  
批次：**1 / 1**（預建清單 36/36）。P2 剩餘 unique URL：**0**。

未碰到 login wall 或 captcha，36 筆全部分類完成，無需 3B。

## 計數

| health | 筆數 | 說明 |
|---|---:|---|
| `alive` | **11** | 商品詳情載入（context／price／sku；標題取 `document.title`） |
| `dead` | **23** | 15 筆導向 `www.1688.com/?spm=…notfound` 首頁；8 筆 `wrongpage.html`（404） |
| `off_shelf` | **2** | 詳情頁明確「商品已下架」（口紅化妝包、側邊小熊殼） |
| `waf` | 0 | 未出現 captcha／punish／滑塊 |
| `login_wall` | 0 | 未導向登入 |
| `oid_mismatch` | 0 | 最終仍在 `/offer/{id}` 者 id 與請求一致 |
| `error` | 0 | |
| **合計** | **36** | |

## 明細

| offer_id | health | suggested_qty | title | notes |
|---|---|---:|---|---|
| 734419757382 | off_shelf | 90 | 韩版简约方形小号化妆包学生小众耳机零钱包少女便携口红收纳包 | 詳情頁「商品已下架」 |
| 644276542345 | dead | 60 | 1688 homepage (notfound redirect) | `www.1688.com` `spm=…notfound` |
| 675106205147 | alive | 20 | suxinaimin袜子女夏季款糖果色卷边中筒棉袜ins潮日系可爱堆堆袜 | context+price+sku |
| 896158447887 | alive | 20 | 包包女2021新款ins迷你帆布包女学生韩版可爱精致小手提包手拎包 | context+price+sku |
| 578246788500 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 616560873695 | alive | 10 | 气囊支架 创意卡通滴胶360度旋转伸缩懒人支架挂钩 礼品LOGO批发 | context+price+sku |
| 621170303718 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 662375994319 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 674019917510 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 674778394852 | alive | 10 | 日系莫兰迪简约纯色内裤女士中腰透气纯棉裆包臀薄款学生三角裤 | context+price+sku |
| 702421478513 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 715793566068 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 731077655765 | dead | 10 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 742408973734 | alive | 10 | 2025新款韩版卡通帆布包少女心卡通可爱水杯套斜跨保温杯帆布袋 | context+price+sku |
| 838528968671 | alive | 10 | 适用苹果17手机壳iphone14涂鸦彩色爱心13镜面12pro透明软边xs/xr | context+price+sku |
| 600411913321 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 600413297881 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 601777449827 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 612901941751 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 617123543564 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 622441480241 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 624135914858 | alive | 5 | 魔方仿液态硅胶16苹果手机壳iphone17pro适用13软11简约12/X/8套 | context+price+sku |
| 635124351042 | off_shelf | 5 | 侧边小熊苹果13全包iphone15手机壳XR彩绘8p软胶14pro max适用11 | 詳情頁「商品已下架」 |
| 640066215865 | dead | 5 | 1688 homepage (notfound redirect) | notfound 首頁 |
| 641213194100 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 647266157547 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 658920430384 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 680647200035 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 689652914948 | alive | 5 | 编织纹15适用iphone13苹果手机壳14pro max针织16纯色12/11套8p软 | context+price+sku |
| 691886666407 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 704268485591 | alive | 5 | 可爱黑色煤球airpods pro耳机套适用苹果无线蓝牙硅胶1/2代保护壳 | context+price+sku |
| 707606084272 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 713047372087 | alive | 5 | 毛绒可爱挂件玩偶钥匙扣加油鸭子丑萌公仔背包挂件ins帕恰狗挂饰 | context+price+sku |
| 716467310974 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 716952591571 | dead | 5 | 404-阿里巴巴 | `wrongpage.html` |
| 740525848630 | alive | 5 | 少女心碎花折叠镜化妆镜随身旅行便携学生宿舍梳妆台式可立小镜子 | context+price+sku |

## Alive（11）

- 675106205147 糖果色卷边中筒棉袜（應補 20）
- 896158447887 迷你帆布包（應補 20）
- 616560873695 气囊支架
- 674778394852 莫兰迪纯色内裤
- 742408973734 卡通帆布包／水杯套
- 838528968671 涂鸦彩色爱心手机壳
- 624135914858 魔方仿液态硅胶手机壳
- 689652914948 编织纹手机壳
- 704268485591 煤球 AirPods 套
- 713047372087 毛绒挂件
- 740525848630 碎花折叠化妆镜

Alive 頁未見明確「售罄」橫幅。初抓 `h1` 常為店名，標題改取 `document.title`。

## off_shelf（2）

- 734419757382 口红化妆包 — body：「商品已下架」（應補 90，P2 最高）
- 635124351042 侧边小熊手机壳 — body：「商品已下架」

## Dead — notfound 首頁（15）

644276542345, 578246788500, 621170303718, 662375994319, 674019917510, 702421478513, 715793566068, 731077655765, 600411913321, 600413297881, 601777449827, 612901941751, 617123543564, 622441480241, 640066215865

最終 URL 皆為 `https://www.1688.com/?spm=a260k.24848612.notfound&theme=index`，`final_offer_id` 空白。首頁推薦卡有 price 選擇器，**不可**當該 offer 的 alive。

## Dead — 404 wrongpage（8）

641213194100, 647266157547, 658920430384, 680647200035, 691886666407, 707606084272, 716467310974, 716952591571

最終 URL 皆為 `https://page.1688.com/shtml/static/wrongpage.html`。

## 方法備註

- 連 CDP 9232 page websocket（`suppress_origin=True`）
- 重用 1688 分頁，未新開未登入 Chrome
- 每 URL：`Page.navigate` → 等 load → ~3s hydration → `Runtime.evaluate` 分類；間隔約 1.5s；唯讀
- 與 CDP24 差異：部分已消失 offer 不再進 `wrongpage.html`，改導 `www.1688.com` `notfound` 首頁

## 檔案

- CSV：`/workspace/_handoff/golden_ai2_health_20260909.csv`
- 本 SUMMARY
- RESULT：`/workspace/_handoff/golden-ai2-RESULT-20260909.md`
