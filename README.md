# Game Trend Radar — 內容後端

只處理主後端已通過官方 Followers 與確切日期驗證的遊戲。此 Repo 不查 Followers、不擴大候選、不以補圖時間冒充 Followers 查詢時間。

## 兩條觸發路徑

- 即時事件：`steam_game_qualified`／`steam_game_refresh`，補充單一 AppID，成功後合併公開資料。
- 定時對帳：台灣時間每日 07:30、19:30，比對主後端與公開資料；先補未來新作，再補已收錄的歷史遊戲。每輪最多 60 款、40 分鐘內停止啟動新請求。

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

Store Browse 繁中介紹優先，其次是官方簡中經 OpenCC `s2twp` 轉換。僅英文的現有 7 款，使用 `descriptions_zh_tw.json` 中對照原文 SHA-256 的本站翻譯；Steam 原文變更就不沿用舊翻譯。官方中文一旦提供，會優先取代本站翻譯。

`short_description` 僅存可顯示的繁中，原文另存 `short_description_en`，搭配 `short_description_language=zh-TW`、`short_description_source` 與 `description_checked_at`。完整欄位一起合併，舊格式英文事件不能覆蓋已確認的繁中。沒有可用中文時不假裝翻譯成功，前端顯示「繁體中文遊戲介紹整理中」；對帳結果的 `description_translation_pending` 列出待翻譯 AppID，與 Steam 請求失敗的重試佇列分開。

可單獨補介紹，無須重查 Followers、發售日、圖片或 TAG：

```sh
python content_backend/localize_descriptions.py --data-dir /path/to/frontend/data
```

此指令將繁中／簡中分批查詢（每批 25 款），更新 AppID、月份及備援資料；既有收錄條件與統計值保持不變。`--cache-dir` 僅用於同一輪重試，需指定新的目錄以重新取得 Steam 原文。

## 同步契約

- `data/games/{appid}.json`：完整單款紀錄。
- `data/catalog.json`：精簡、壓縮排版的清單資料，含內容版本指紋。
- `data/calendar/{YYYY-MM}.json`：月份完整紀錄。
- `data/index.json`、`data/lists/*.json`：索引。
- `data/steam_upcoming.json`：一致的舊格式備援。

上述檔案在同一 commit 發布。比對完整欄位，不能只比筆數或事件 signature。發布衝突時重新合併最新前端；已抓取的成功內容保存在 runner 暫存，重試發布時不再次呼叫 Steam。較舊官方事件不能覆蓋較新已公開的 Followers／日期。

`public_catalog.py` 的欄位投影與主後端同名模組同步維護。

## 驗證

```bash
python -m pip install -r content_backend/requirements.txt
PYTHONPATH=content_backend python -m unittest discover -s content_backend -p "test_*.py" -q
```

仍使用既有 `FRONTEND_REPO_TOKEN`，寫入 `danielet087/game-trend-radar`；未改動憑證或存取範圍。
