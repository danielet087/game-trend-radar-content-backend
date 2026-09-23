# Game Trend Radar Content Backend

事件驅動的 Steam 遊戲內容補充後端。

這個 Repo 不負責搜尋候選遊戲，也不負責判斷 Followers。  
只有當主後端已使用 Steam Community 官方 `memberCount` 驗證遊戲 **Followers >= 5000** 後，才會透過 `repository_dispatch` 觸發這裡。

## 流程

```
game-trend-radar-backend
  Steam official Followers >= 5000
        ↓ repository_dispatch
game-trend-radar-content-backend
  補 Steam 內容
        ↓
game-trend-radar
  data/steam_upcoming.json
```

## 補充內容

- 台灣上市日期與時間
- 英文／繁中／簡中名稱
- 語言支援
- Steam header image
- Steam main capsule image
- Genres
- Steam Store Tags
- 遊戲簡介
- 主後端傳入的官方 Followers

## 事件

正式 workflow：

`.github/workflows/steam-content-enrichment-dispatch.yml`

接受：

- `steam_game_qualified`
- `steam_game_refresh`

## Secret

此 Repo 的 Actions 需要：

`FRONTEND_REPO_TOKEN`

這顆 token 只負責將補充完成的資料寫入前端 Repo，對：

- `danielet087/game-trend-radar`

具有 Contents read/write 即可。

主後端 `game-trend-radar-backend` 則使用獨立的
`CONTENT_BACKEND_TOKEN`，只負責對本 Repo 發送 `repository_dispatch`。

## 安全原則

- 不存放 Steam API Key
- 不存放 Followers 私有 cache
- 不在 Repo 內保存 GitHub token
- Followers 判定仍由主後端負責
- 相同 `appid + followers + release_date` 的重複事件採冪等處理
