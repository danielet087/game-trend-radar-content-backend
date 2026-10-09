# 後端內容管線與發布架構

內容後端採用 Python 模組化批次管線，仍由 GitHub Actions 接收事件、執行補充與發布 JSON。第二、三批建立收集、規則、狀態、用例與共用 Git 發布邊界；第十六批完成共用分類、taxonomy、描述與 browser projection 的分層，未新增常駐服務或資料庫。

| 位置 | 責任 |
| --- | --- |
| `content_backend/radar_backend/domain/content.py` | 已取得來源的資格、成人排除、精確日期、台灣時間、語言／圖片欄位與純 snapshot 轉換 |
| `domain/catalog.py`、`domain/release.py` | 內容缺口、收錄來源資格、事件簽章與較新日期證據保留；分類證據由明確 callbacks 注入 |
| `application/enrichment.py` | 按原順序取得英文／繁中／簡中來源，驗證，再轉換記錄；依賴由 `EnrichmentPorts` 注入 |
| `application/reconciliation.py` | 依原批次數量、時間上限、失敗冷卻處理待補項目，並修復本機索引；依賴由 `ReconcilePorts` 注入 |
| `adapters/steam_store.py` | Steam HTTP、35 秒 timeout、原重試節奏、taxonomy fallback 與 429 中止 |
| `state/json_documents.py` | JSON 讀寫；分開嚴格讀取和舊有 best effort 讀取，保留各入口原失敗方式 |
| `publication/catalog.py` | 單款／月份 shard、排除清單、公開索引、清單及舊格式對帳 |
| `publication/content_snapshot.py` | 嚴格前置讀取、凍結已驗證記錄／對帳批次、重播最新 catalog、整套快照驗證及版本 manifest |
| `jobs/enrich_content.py`、`jobs/reconcile_content.py` | 解析 CLI、驗證參數、執行用例與輸出原報告 |
| `jobs/publish_content.py` | 固定來源版本，呼叫共用 Git 發布 adapter，僅收到 push 確認後產生成功收據 |
| `bootstrap.py` | 生產組裝：具體 HTTP、翻繁／描述來源、分類規則、時鐘、JSON、存在性與發布端口 |

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

## 第十六批：共用分類、文字與 browser projection

| 責任 | 位置（相對於 `content_backend/radar_backend/`） |
| --- | --- |
| 官方 player categories、時間驗證與較新證據保留 | `domain/player_categories.py` |
| 分類的實際時鐘及 release／metadata 規則接線 | `adapters/player_categories.py`、`adapters/catalog_rules.py` |
| zh-TW Store taxonomy parser、tag／genre 身分與 retry 保存 | `domain/steam_taxonomy.py`、`adapters/steam_taxonomy.py` |
| 文字清理、中文判定、來源輸出與原子描述合併 | `domain/localized_descriptions.py` |
| 惰性翻譯選擇、版本化翻譯 JSON 讀取與 OpenCC 接線 | `application/localized_descriptions.py`、`state/description_translations.py`、`adapters/localized_descriptions.py` |
| 全記錄雜湊、允許欄位及 catalog v3 純投影 | `domain/catalog_projection.py` |
| 保持原讀寫／no-op 順序的 catalog 保存與接線 | `state/catalog_projection.py`、`adapters/catalog_projection.py` |
| 既有分類／描述維護工具的 canonical 來源接線 | `adapters/content_helpers.py` |

四個舊 feature helpers 保留公開簽名、常數、當下 globals 與 cache 介面；正式 enrichment／reconciliation／publication 與兩個 localize 工具使用 canonical owner。`enrich_game`、`reconcile_catalog` 的舊 CLI 仍可使用，舊單檔 `upsert_document` 相容功能保留。沒有透過模組 alias 建立第二套實作。

分類只接受同一 AppID 的官方 Browse player IDs 或 appdetails categories，明確空列表是已查證的負向結果；功能／控制器 ID 不能冒稱多人證據。原有五分鐘未來時間上限、naive 時間拒絕、逐次時鐘讀取、短路順序、較新資料與獨立 Twitch／日期證據保持。Domain 的 release／metadata helpers 現要求明確的純 callbacks，runtime adapter 與舊入口保留原呼叫介面。ReconcilePorts 保留原 14 個 positional arguments，追加規則與檔案存在性 ports；正式 composition 顯式供應，application 不直接讀取 filesystem 或實際 wall clock，既有文字報告保留。

Taxonomy 使用原 parser，不改 regex、AppID／zh-TW 要求、20 個 tag 上限、穩定 tag 身分、genre URL 或錯誤範圍。Descriptions 保留至少四個漢字且排除日文假名的規則、OpenCC `s2twp`、繁中 → 簡中 → editorial 的順序；只有官方內容不合格才讀取版本化翻譯 JSON。來源 SHA-256 不符不沿用翻譯，`unavailable` 會按原契約清除舊中文描述，locale 與文字一起合併；`lru_cache(maxsize=1)` 的 reference、clear／info 及讀取失敗行為保持。

Catalog revision 仍由所有 accepted rows 的 canonical JSON 計算 SHA-256 前 20 碼，而非只雜湊 browser 欄位；描述等 metadata 更新也會使 revision 改變。公開欄位、version 3、UTF-8 compact JSON、來源順序、同 revision 不寫入、舊檔讀取容錯與錯形例外維持。兩個既有 publication 模組除 imports 外本體不變，凍結批次、strict JSON、partial／superseded、有限重試、空 diff 真實 push 及成功收據仍由原發布流程負責。

本批接續 Content 第三批 PR #3，其他後端相依 PR 順序保持。三個 workflows、non-cone sparse patterns、Secrets、requirements、Core 0.2.0 immutable SHA `bf1d4bc64b361ec35cd4041d78c5016396d5d785` 與翻譯 JSON 均不變。驗證涵蓋舊／新 helper APIs、完整 canonical enrichment → JSON projection、兩個 metadata-only 維護工具、阻擋舊入口的正式 import graph、時鐘邊界、原 source oracle、乾淨 tracked checkout、實際 sparse checkout 與本機 bare Git 發布。

## 剩餘驗收範圍

原四個 consumer（Steam、Twitch、Content、前端 Python／Core 接線）已完成規劃內的分層拆分，下一批做跨 consumer 總驗收，預留一次修正，預估再 1～2 次。前端 UI 已於先前完成；IGDB 獨立後端與全面重寫退役工具另列範圍。

Publication 的 shard 掃描與 state 的 JSON 存取屬外層發布／保存責任；保留既有檔案格式與順序，不為本次分層引入新的通用 filesystem repository。
