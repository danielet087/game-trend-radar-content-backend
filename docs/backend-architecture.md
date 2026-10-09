# 後端內容管線與發布架構

內容後端採用 Python 模組化批次管線，仍由 GitHub Actions 接收事件、執行補充與發布 JSON。這一批讓收集、規則、狀態與用例有可測試的邊界，未新增常駐服務或資料庫。

| 位置 | 責任 |
| --- | --- |
| `content_backend/radar_backend/domain/content.py` | 已取得來源的資格、成人排除、精確日期、台灣時間、語言／圖片欄位與純 snapshot 轉換 |
| `domain/catalog.py`、`domain/release.py` | 內容缺口、收錄來源資格、事件簽章與較新日期證據保留 |
| `application/enrichment.py` | 按原順序取得英文／繁中／簡中來源，驗證，再轉換記錄；依賴由 `EnrichmentPorts` 注入 |
| `application/reconciliation.py` | 依原批次數量、時間上限、失敗冷卻處理待補項目，並修復本機索引；依賴由 `ReconcilePorts` 注入 |
| `adapters/steam_store.py` | Steam HTTP、35 秒 timeout、原重試節奏、taxonomy fallback 與 429 中止 |
| `state/json_documents.py` | JSON 讀寫；分開嚴格讀取和舊有 best effort 讀取，保留各入口原失敗方式 |
| `publication/catalog.py` | 單款／月份 shard、排除清單、公開索引、清單及舊格式對帳 |
| `publication/content_snapshot.py` | 嚴格前置讀取、凍結已驗證記錄／對帳批次、重播最新 catalog、整套快照驗證及版本 manifest |
| `jobs/enrich_content.py`、`jobs/reconcile_content.py` | 解析 CLI、驗證參數、執行用例與輸出原報告 |
| `jobs/publish_content.py` | 固定來源版本，呼叫共用 Git 發布 adapter，僅收到 push 確認後產生成功收據 |
| `bootstrap.py` | 生產組裝：具體 HTTP、翻繁／描述來源、時鐘、JSON 與發布端口 |

既有 `enrich_game.py`、`reconcile_catalog.py` 提供相容函數及 CLI。舊測試對 `enrich_game.browse_one`、`reconcile_catalog.build_record` 等入口的替換，會在呼叫時注入到用例，因此維持同一份實作；新套件不反向匯入舊入口，也不以 `sys.modules` 建立別名。

## 執行及驗證

在 repository 根目錄安裝固定版本核心及原內容依賴：

```sh
python -m pip install -r content_backend/requirements.txt
PYTHONPATH=content_backend python -m unittest discover -s content_backend -p 'test_*.py' -q
```

下列入口使用同一個內容補充 job，既有參數、stdout 報告及 data schema 保留：

```sh
python content_backend/enrich_game.py --help
python -m content_backend.enrich_game --help
PYTHONPATH=content_backend python -m radar_backend.jobs.enrich_content --help
python content_backend/reconcile_catalog.py --help
python -m content_backend.reconcile_catalog --help
PYTHONPATH=content_backend python -m radar_backend.jobs.reconcile_content --help
```

生產 workflow 已以非 cone sparse checkout 取得整個 `content_backend/`，會包含分層套件。CI 執行的新增測試只使用 fake source、memory store、暫存 JSON 與本機 bare Git remote，不查詢 Steam。正常事件觸發、排程、Secrets 及收集批次上限不變。第三批採用包含共用發布 adapter 的固定核心版本。

## 第三批：凍結收集與 Git 發布

單款內容只收集一次，凍結實際通過驗證的 AppID 記錄；對帳同樣只執行一次原有的 60 款／40 分鐘收集，再凍結主清單來源、成功記錄及失敗 metadata。凍結檔和 push 收據存於 runner 暫存，位於可重設的 frontend checkout 外。

`radar_core.publication.publish_with_retry` 集中 Git fetch、重設、owned JSON 清理、stage／commit／push 與有上限的重試。單款保留最多 12 次，對帳保留最多 8 次。每次衝突只讀最新 frontend JSON、重播凍結記錄和修復本機索引；不重新執行 HTTP 收集，不延長本輪六小時失敗冷卻，也不刷新凍結的輸出時間。重播保留最新日期、Followers、分類及 Twitch 證據；被較新資料取代的單款會停止，批次則保留最新記錄並回報 partial／superseded。

發布前嚴格讀取現有 master 與 catalog 文件。缺少可重建的衍生檔可以修復，存在但損壞的 JSON、重複 key 或錯誤結構會停止作業。發布驗證比對 AppID、月份、index、upcoming／released、legacy 及 browser catalog 的實際內容、筆數與既有 v3 hash，不能僅信任 revision 標頭。所有投影和 `data/publication/steam_content.json` 在同一 commit 寫入，未宣告的 dirty file 不得進入發布。

manifest 的 `input_revision` 由凍結 frontend／master Git SHA 算出，`payload_revision` 識別凍結批次；`dataset_revision` 識別實際合併後的 owned JSON，排除 manifest 自身。既有 `catalog.json` version 3、欄位投影及 hash 算法保留，不向每款記錄加入版本欄位。真正的 `published_revision` 是 push 確認的 Git commit，僅在事後 artifact 收據中記錄，不寫成同一 commit 的自我引用。

空 diff 仍須收到 remote push 確認。失敗會輸出 failed 工作結果並使 job 失敗；收據不保留舊成功資訊。partial 批次可發布已確認的記錄，但 `collection_complete` 仍要求原收集批次完整、實際 catalog 無缺口且無 superseded 記錄。`actual_catalog_complete` 獨立描述最新 catalog 狀況，不把其他 publisher 補齊資料當成本輪收集完成。

## 保留的行為

- 低 Followers 仍需有效 Twitch 收錄證據；成人排除清單缺失仍 fail closed。
- 精確公開日期、歷史標題日期證據，以及 Twitch 台灣商店日期權威規則維持原判定。
- 英文／繁中／簡中三次 Browse 的順序及兩次 1 秒間隔保留；JSON 重試仍最多 4 次、4／8／12 秒，appdetails 外層仍最多 3 次、5／10 秒。
- 429 結束本輪遠端補充；仍可修復本機 shard 與索引，未完整的項目留在 pending，沿用 6 小時失敗冷卻。
- 既有翻繁、描述來源雜湊、metadata、多人分類證據、公開 catalog revision 與 JSON 欄位保留。

## 尚待後續處理

1. `public_catalog.write_catalog_projection` 仍為相容的 browser projection 實作；其版本 3 hash／欄位規則未重寫。`enrich_game.upsert_document` 仍保留舊單檔讀者的支援。
2. `localized_descriptions` 的在地化文字與版本化翻譯檔、`steam_taxonomy` 的 parser、`steam_player_modes` 的證據 helpers 尚未全面搬移；描述讀取及轉換透過 port 注入，不藏在 snapshot domain 裡。
3. `domain/catalog` 與 release 證據保留仍橋接既有 player-category helper，其五分鐘未來時間檢查會讀取 wall clock。這些規則已無 HTTP／JSON／Git 存取，但尚未全部改成顯式時鐘；應連同所有分類證據呼叫者一起抽出，避免只改一個入口導致判定分歧。
4. 部分 publication 檔案選取仍以 `Path` 掃描既有 shard，`application/reconciliation` 沿用歷史記錄存在性判斷；目前 state 層集中 JSON 讀寫，完整 filesystem repository 可在後續收斂。
