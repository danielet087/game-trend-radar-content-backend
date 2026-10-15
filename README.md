# Game Trend Radar — 內容後端

只處理主後端已通過官方 Followers 與確切日期驗證的遊戲。此 Repo 不查 Followers、不擴大候選、不以補圖時間冒充 Followers 查詢時間。

## 兩條觸發路徑

- 即時事件：`steam_game_qualified`／`steam_game_refresh`／`steam_game_twitch_discovered`，補充單一 AppID，成功後合併公開資料。
- 定時對帳：台灣時間每日 07:30、19:30，由既有 [Cloudflare 排程控制器](https://github.com/danielet087/game-trend-radar-twitch-backend/tree/main/scheduler/cloudflare) 透過 `workflow_dispatch` 觸發；原 GitHub `schedule` 已移除。比對主後端與公開資料，先補未來新作，再補已收錄的歷史遊戲。每輪最多 60 款、40 分鐘內停止啟動新請求。既有 `repository_dispatch` 內容事件繼續即時處理。

Twitch 正式新作是獨立收錄來源，可低於 5,000 Steam Followers。新事件與後續 refresh 必須提供完整 `twitch_admission`；內容後端讀取證據指定的同一 GitHub commit，核對 Twitch 正式入列、IGDB 身分與官方 Steam 外部 AppID，再驗證台灣商店正式遊戲、確切日期及成人內容規則。官方 Followers 仍由主後端查詢並提供，失敗不補零。沒有有效來源證據的事件沿用既有門檻。

正式 Twitch 新作的確切台灣商店日期與 Store Browse 時間戳不同時，以同一 AppID 的 `cc=TW`、`l=tchinese` 官方完整日期作月曆依據，記錄 `release_date_normalization=steam_taiwan_store_date_authoritative`、來源與驗證時間。原始 UTC、實際換算的台灣日期與衝突診斷仍完整保存，不偽造時間戳。只有完整來源、日期與診斷互相一致的紀錄能使用此規則；沒有完整日期仍延後處理。既有 UTC 跨午夜一天的正常化規則保留。

直接執行 `enrich_game.py` 時，以 `--twitch-admission JSON_PATH` 傳入來源；來源證據會保留於完整遊戲檔、catalog、月曆、清單與後續對帳，不因一般內容更新消失。

## 內容完整度與重試

檢查大圖、2x 圖片是否已確認、語言資料、TAG 是否已取得、繁中介紹是否已查詢、Followers／日期是否和主後端一致。不是「有 header 就完成」。Steam 未提供 2x 或確實沒有 TAG 可以記為已確認；請求失敗與空白不能混為一談。

TAG 抓取失敗時保留先前有效 TAG，紀錄 `tags_fetch_status=retry`。單款失敗記錄原因並延後 6 小時重試，其餘遊戲繼續；遇 429 停止當輪。`content_refresh_status.json` 記錄實際完成／待補與重試時間，Actions summary 也會列出剩餘量。

## 圖片與內容

直接保存 Store Browse 回傳的 `header`、`main_capsule` 與對應 `_2x` 網址，完整保留各資產的 hash，不自行換檔名猜圖。語言支援、繁中／簡中名稱分別保存；簡中顯示名稱另轉繁體，不篡改原始名稱。

## 台灣商店的 TAG 與遊戲類型

內容補充使用 `cc=TW`、`l=tchinese`。TAG 與遊戲類型直接取自同一款遊戲的台灣繁中商店頁；補充用的 appdetails 也採繁中。頁面資料取不到時，使用同為台灣區繁中的 Store Browse、官方標籤字典與 appdetails 補足，來源欄位會如實區分頁面與 API。名稱與介紹延續繁中優先、簡中轉繁的既有流程。

`tags` 和 `genres` 保留原有英文識別，另存 Steam 官方的 `tag_labels_zh_tw` 與 `genre_labels_zh_tw`。TAG 以 Steam tag ID 對應（`tag_ids`），每個工作階段只查一次官方英文標籤字典，不依不同語言的列表位置硬配。新增但尚無英文名稱的 TAG 使用穩定的 `steam-tag:<id>` 識別。前端官方繁中名稱優先，舊英文 TAG 網址與跨遊戲篩選仍相容。

來源網址、語言、查詢時間及 TAG／類型的成功狀態分別保存。網頁改版、地區不可用或暫時抓取失敗時保留既有標籤與對照資料，交由對帳重試；429 會停止當輪。未取得的類型不會被當成成功的空白清單。

已收錄遊戲可單獨補 TAG／類型，無須重查 Followers、發售日期、介紹或圖片：

```sh
python content_backend/localize_taxonomy.py --data-dir /path/to/frontend/data --cache-dir /path/to/new-run-cache
```

## 繁體中文介紹

同一 AppID 的 Steam 官方繁中優先，其次是官方簡中經 OpenCC `s2tw` 轉成繁體字。沒有官方中文時，保留既有、原文 SHA-256 相符的本站翻譯，再直接顯示 Steam 英文原文。沒有任何介紹時才標記為尚未提供，不進行自動機器翻譯。原文與翻譯分開保存，簡介的語言不代表遊戲本身支援中文。

`short_description` 保存顯示用繁中或英文，`short_description_en` 保存新鮮英文原文，並記錄 `short_description_language`、`short_description_source`、來源網址及 `description_checked_at`。每一輪都取得英文、繁中、簡中三語，不以原文未變、已有英文或已填寫檢查時間作為永久跳過的理由。Steam 日後新增官方中文會自動取代英文。

簡介有獨立排程與到期佇列，不依賴 Followers、成長追蹤、圖片、TAG 或一般內容缺漏。既有 Cloudflare 排程控制器在台灣時間 01:30、07:30、13:30、19:30 觸發 `steam-description-refresh.yml`，每輪最多 100 款、每批 25 款，在 9 分鐘收集預算後停止啟動下一批。合併到 main 的簡介流程更新也會觸發一次，便於立即驗證及補資料。

| 狀態 | 官方簡介回查間隔 |
| --- | --- |
| 缺官方中文，距上市超過 60 天 | 7 天 |
| 缺官方中文，上市前 60 天至上市後 30 天 | 1 天 |
| 已有官方繁中或簡中 | 7 天 |
| 上市超過 30 天的歷史詳情頁 | 30 天 |

`description_refresh_status.json` 保存每款到期與重試 checkpoint。傳輸失敗或三語回應不完整時，保留舊的有效中文與成功檢查時間，6 小時後重試；429 會停止當輪，其餘未處理項目下輪續跑。英文是有效介紹，依正常每日／每週／每月間隔回查，不因缺中文而每 6 小時重抓。

英文備援使用 `short_description_language=en`、`short_description_source=steam_english`，中文則維持 `zh-TW` 與各自官方／人工來源。既有有效官方中文在暫時缺中文時仍保留；舊機器翻譯不能優先於英文。官方日後補上中文，下一次到期回查就會升級。

可單獨執行介紹更新：

```sh
python content_backend/localize_descriptions.py --data-dir /path/to/frontend/data --max-games 100 --max-seconds 540
```

`--force` 可忽略到期時間，但仍遵守每輪數量上限。`--cache-dir` 只輸出本輪診斷資料，絕不讀取舊三語回應來冒充新鮮官方查詢。

簡介、月份、清單、catalog 與 checkpoint 在同一 commit 發布。發布衝突使用共用 `publish_with_retry`，僅重播已凍結的簡介欄位，不重新呼叫 Steam，也不覆蓋併發更新的 Followers、日期或其他內容。較新的官方簡介檢查時間優先。發布收據保存真實 Git commit，`data/publication/steam_descriptions.json` 保存最後發布的排程 slot 以避免同一 slot 重做。

## 同步契約

- `data/games/{appid}.json`：完整單款紀錄。
- `data/catalog.json`：精簡、壓縮排版的清單資料，含內容版本指紋。
- `data/calendar/{YYYY-MM}.json`：月份完整紀錄。
- `data/index.json`、`data/lists/*.json`：索引。
- `data/steam_upcoming.json`：一致的舊格式備援。

上述檔案在同一 commit 發布。比對完整欄位，不能只比筆數或事件 signature。發布衝突時重新合併最新前端；已抓取的成功內容保存在 runner 暫存，重試發布時不再次呼叫 Steam。較舊官方事件不能覆蓋較新已公開的 Followers／日期。

發布由 `radar_backend.jobs.publish_content` 組裝固定版本 `radar_core.publication` 共用 adapter。單款最多 12 次、對帳最多 8 次；對帳的來源收集和失敗冷卻只執行一次。`data/publication/steam_content.json` 與 catalog 同 commit 保存來源及實際資料版本，push 確認後才另產生包含真實 Git commit 的 artifact 收據。空 diff 同樣需要 remote 確認；未確認會失敗，partial 批次仍如實回報 partial。

發布前先嚴格檢查既有 JSON；損壞檔案不會被當成空資料覆蓋。AppID、月份、清單、index、legacy 及 v3 browser catalog 的內容必須一致。架構細節見 [backend-architecture.md](docs/backend-architecture.md)。

`data/publication/activity_origins.json` 與公開 catalog 在同一 commit 記錄本次實際新增或改日期的遊戲，保留最近 1,000 筆。觸發事件於收集後凍結，發布衝突只重新比較最新公開 catalog；重試及僅修改圖片、名稱等內容不會重複產生活動。`steam_game_twitch_discovered` 標為 `twitch_rankings`、`steam_game_qualified` 標為 `steam_official_verification`、`steam_game_refresh` 標為 `steam_metadata_refresh`，定時對帳標為 `steam_catalog_sync`。既有 Twitch 收錄證據不代表本次日期更新也來自 Twitch。只有上游明示 `discovery_source=steam_daily_search`，且來源 repository 與 workflow 分別為 `danielet087/game-trend-radar-backend`、`steam-two-phase.yml` 時，官方資格事件才標為 `steam_daily_search`；目前未提供這組證據的事件沿用官方驗證標示。

`public_catalog.py` 的欄位投影與主後端同名模組同步維護。

## 驗證

```bash
python -m pip install -r content_backend/requirements.txt
PYTHONPATH=content_backend python -m unittest discover -s content_backend -p "test_*.py" -q
```

仍使用既有 `FRONTEND_REPO_TOKEN`，寫入 `danielet087/game-trend-radar`；未改動憑證或存取範圍。
