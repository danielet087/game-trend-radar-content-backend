# 後端第二批：內容補充與公開對帳分層

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
| `jobs/enrich_content.py`、`jobs/reconcile_content.py` | 解析 CLI、驗證參數、執行用例與輸出原報告 |
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

生產 workflow 已以非 cone sparse checkout 取得整個 `content_backend/`，會包含分層套件。CI 執行的新增測試只使用 fake source、memory store 與暫存 JSON，不查詢 Steam。正常事件觸發、排程、Secrets、批次上限及核心 SHA 不變。

## 保留的行為

- 低 Followers 仍需有效 Twitch 收錄證據；成人排除清單缺失仍 fail closed。
- 精確公開日期、歷史標題日期證據，以及 Twitch 台灣商店日期權威規則維持原判定。
- 英文／繁中／簡中三次 Browse 的順序及兩次 1 秒間隔保留；JSON 重試仍最多 4 次、4／8／12 秒，appdetails 外層仍最多 3 次、5／10 秒。
- 429 結束本輪遠端補充；仍可修復本機 shard 與索引，未完整的項目留在 pending，沿用 6 小時失敗冷卻。
- 既有翻繁、描述來源雜湊、metadata、多人分類證據、公開 catalog revision 與 JSON 欄位保留。

## 尚待後續處理

1. Workflow 裡的 Git fetch/reset/push、衝突重試及發布成功收據尚未集中為 publication adapter；本批只抽離內容與 JSON projection 用例，不宣稱 Git 發布協定已完成。
2. `public_catalog.write_catalog_projection` 仍為相容的 browser projection 實作；其版本 3 hash／欄位規則未重寫。`enrich_game.upsert_document` 仍保留舊單檔讀者的支援。
3. `localized_descriptions` 的在地化文字與版本化翻譯檔、`steam_taxonomy` 的 parser、`steam_player_modes` 的證據 helpers 尚未全面搬移；描述讀取及轉換透過 port 注入，不藏在 snapshot domain 裡。
4. `domain/catalog` 與 release 證據保留仍橋接既有 player-category helper，其五分鐘未來時間檢查會讀取 wall clock。這些規則已無 HTTP／JSON／Git 存取，但尚未全部改成顯式時鐘；下一批應連同所有分類證據呼叫者一起抽出，避免只改一個入口導致判定分歧。
5. 部分 publication 檔案選取仍以 `Path` 掃描既有 shard，`application/reconciliation` 沿用歷史記錄存在性判斷；目前 state 層集中 JSON 讀寫，完整 filesystem repository 可在下一批收斂。
