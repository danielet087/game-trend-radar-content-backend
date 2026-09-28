# Game Trend Radar — 內容後端

只處理主後端已通過官方 Followers 與確切日期驗證的遊戲。此 Repo 不查 Followers、不擴大候選、不以補圖時間冒充 Followers 查詢時間。

## 兩條觸發路徑

- 即時事件：`steam_game_qualified`／`steam_game_refresh`，補充單一 AppID，成功後合併公開資料。
- 定時對帳：台灣時間每日 07:30、19:30，比對主後端與公開資料；先補未來新作，再補已收錄的歷史遊戲。每輪最多 60 款、40 分鐘內停止啟動新請求。

## 內容完整度與重試

檢查大圖、2x 圖片是否已確認、語言資料、TAG 是否已取得、Followers／日期是否和主後端一致。不是「有 header 就完成」。Steam 未提供 2x 或確實沒有 TAG 可以記為已確認；請求失敗與空白不能混為一談。

TAG 抓取失敗時保留先前有效 TAG，紀錄 `tags_fetch_status=retry`。單款失敗記錄原因並延後 6 小時重試，其餘遊戲繼續；遇 429 停止當輪。`content_refresh_status.json` 記錄實際完成／待補與重試時間，Actions summary 也會列出剩餘量。

## 圖片與內容

直接保存 Store Browse 回傳的 `header`、`main_capsule` 與對應 `_2x` 網址，完整保留各資產的 hash，不自行換檔名猜圖。語言支援、繁中／簡中名稱分別保存；簡中顯示名稱另轉繁體，不篡改原始名稱。

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
PYTHONPATH=content_backend python -m unittest -q content_backend/test_enrich_game.py
```

仍使用既有 `FRONTEND_REPO_TOKEN`，寫入 `danielet087/game-trend-radar`；未改動憑證或存取範圍。
