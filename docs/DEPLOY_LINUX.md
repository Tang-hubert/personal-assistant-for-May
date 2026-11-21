# 如何把小幫手部署到 Linux 家用伺服器

本文件是操作手冊：在 Linux x86_64 主機上用 Docker Compose 讓 bot 24 小時運作，並在出國前確認主機能自己撐住。架構背景見 `docs/ARCHITECTURE.md` §9。

## 1. 準備兩個不同的 bot

dev 和 prod 必須用不同的 `TELEGRAM_BOT_TOKEN`。兩個 long polling 程序用同一個 token 時，Telegram 回 `409 Conflict`，兩邊都會漏訊息。

| 環境 | Bot | 執行位置 |
| :--- | :--- | :--- |
| Dev | 測試小號 bot | Windows 開發機，`uv run python -m src.main` |
| Prod | `@MayLin14_assistant_bot` | Linux 主機，`docker compose up -d` |

- 用 @BotFather 各建一個 bot，各自保存 token。
- prod token 只放在 Linux 主機的 `.env`。Windows 開發機的 `.env` 永遠只放 dev token。
- 如果 log 出現 `Conflict: terminated by other getUpdates request`，代表有第二個程序在用同一個 token。先找出它並停掉，再重啟。

## 2. 取得程式碼

```bash
sudo apt install -y git sqlite3
git clone <private-repo-url> personal-assistant-for-May
cd personal-assistant-for-May
```

Docker Engine 和 Compose plugin 請依 Docker 官方文件安裝，然後讓 Docker 開機自動啟動：

```bash
sudo systemctl enable --now docker
```

## 3. 建立 `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

- 填入 prod token、`GEMINI_API_KEY`、`DEEPSEEK_API_KEY`、`DISCORD_OPS_WEBHOOK_URL`，並設定 `APP_ENV=prod`。
- `.env` 必須存成 **UTF-8 無 BOM**、LF 換行。BOM 會讓第一個 key 讀不到。
- 用下面的指令檢查。第一行輸出不可以是 `efbbbf`，第二行的數字必須是 0：

```bash
head -c3 .env | xxd -p
grep -c $'\r' .env
```

- 如果檔案是從 Windows 複製過來的，用 `sed -i 's/\r$//' .env` 移除 CR。
- 驗證設定時只用安靜模式。一般的 `docker compose config` 會把 `.env` 裡的密鑰印到螢幕上：

```bash
docker compose config --quiet && echo OK
```

## 4. 設定白名單

`TELEGRAM_ALLOWED_USER_IDS` 是逗號分隔的 Telegram 數字 ID，例如 `TELEGRAM_ALLOWED_USER_IDS=123456789,987654321`。白名單是空的時候，程式拒絕啟動。

查自己的數字 ID：

1. 先讓 bot 跑起來。
2. 用要加入的帳號傳一句話給 bot。bot 不會回覆陌生人，但會記一行 log。
3. 在主機上找出那行 log：

```bash
docker compose logs app | grep "not allowlisted"
```

4. 把 ID 加進 `.env`，然後重啟：`docker compose up -d`。新帳號要等重啟後才會被建立，重啟前它傳的訊息 bot 一律不回。

每次啟動，程式都會替白名單裡的每個帳號建立獨立的 `user_id`（同屬 `default` tenant）。這一步可以重跑，已存在的帳號不會被改動。所以你的測試帳號和 May 的記憶不會混在一起。同一個 tenant 共用每日預算（`DEFAULT_DAILY_BUDGET_USD`）。

> `DEFAULT_DAILY_BUDGET_USD` 只在第一次建立 tenant 時寫入。之後修改 `.env` 不會改變既有 tenant 的預算。

## 5. 準備資料夾權限

容器以 UID 1000 執行。`./data` 必須屬於 1000:1000，否則 SQLite 和照片寫不進去。

```bash
mkdir -p data
sudo chown -R 1000:1000 data
```

## 6. 啟動

```bash
docker compose up -d --build
docker compose logs -f app
```

啟動時依序執行：資料庫 migration、建立白名單帳號、補送重啟前沒回完的照片批次，然後才開始收訊息。`docker-compose.yml` 已設定 `restart: unless-stopped`，主機重開機後 Docker 會自動把 bot 帶起來。停止時，`stop_grace_period: 30s` 讓 bot 有時間回完正在處理的訊息，並送出還在等待中的照片批次回覆。

## 7. 確認 log rotation

`docker-compose.yml` 使用 json-file driver，每個檔案上限 10 MB、最多 3 個檔案。用下面的指令確認設定已生效：

```bash
docker inspect --format '{{json .HostConfig.LogConfig}}' $(docker compose ps -q app)
```

輸出必須包含 `"max-size":"10m"` 和 `"max-file":"3"`。

## 8. 排程備份

`sqlite3 .backup` 在 bot 運作中也能安全複製 WAL 模式的資料庫。照片原始檔在 `data/media/`，要一起備份。

1. 建立備份資料夾：

```bash
sudo mkdir -p /srv/may-backups
```

2. 編輯 root 的 crontab：`sudo crontab -e`。
3. 加入下面三行（每天 03:30 備份資料庫、03:45 同步照片、04:00 刪除 14 天前的資料庫備份）。把路徑換成你的 repo 位置：

```cron
30 3 * * * sqlite3 /home/<you>/personal-assistant-for-May/data/app.db ".backup '/srv/may-backups/app-$(date +\%F).db'"
45 3 * * * rsync -a /home/<you>/personal-assistant-for-May/data/media/ /srv/may-backups/media/
0 4 * * * find /srv/may-backups -name 'app-*.db' -mtime +14 -delete
```

4. 隔天確認 `/srv/may-backups` 有當天的檔案。

從備份還原：

```bash
docker compose stop app
cp /srv/may-backups/app-YYYY-MM-DD.db data/app.db
rm -f data/app.db-wal data/app.db-shm
rsync -a /srv/may-backups/media/ data/media/
sudo chown -R 1000:1000 data
docker compose start app
```

## 9. 更新程式

```bash
git pull
docker compose up -d --build
```

migration 會在啟動時自動執行。更新前先手動跑一次第 8 節的 `.backup`。

## 10. 出國前檢查表

出國前逐項確認。任何一項沒過，就先別出門。

- [ ] **Tailscale**：已安裝並登入（`sudo tailscale up`），並設定開機自動啟動（`sudo systemctl enable --now tailscaled`）。
- [ ] **Tailscale key expiry**：在 Tailscale 管理後台把這台主機設成 Disable key expiry。
- [ ] **遠端連線**：手機改用行動網路，經由 Tailscale 成功 SSH 進主機。
- [ ] **BIOS 斷電復電**：「Restore on AC Power Loss」（或同義選項）設為 **Power On**，停電後恢復供電會自動開機。
- [ ] **不休眠**：執行 `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`。
- [ ] **重開機測試**：`sudo reboot`，然後確認 bot 不用任何手動操作就能回訊息。
- [ ] **時間同步**：`timedatectl` 顯示 `System clock synchronized: yes`。
- [ ] **磁碟空間**：`df -h` 顯示資料磁碟剩餘超過 20%。
- [ ] **預付餘額**：Gemini 和 DeepSeek 的餘額夠用到回國，而且自動儲值是關閉的。
- [ ] **Discord 警報**：從主機送一則測試訊息，確認 Ops 頻道收到。webhook URL 本身就是密鑰，所以從 `.env` 讀，不要直接打在指令裡：

```bash
url=$(grep '^DISCORD_OPS_WEBHOOK_URL=' .env | cut -d= -f2-)
curl -fsS -H 'Content-Type: application/json' -d '{"content":"[TEST] 出國前檢查"}' "$url" && echo sent
```
- [ ] **備份可還原**：在另一台機器或另一個資料夾，用第 8 節的步驟實際還原一次最新備份，並確認資料能讀。
- [ ] **白名單**：`TELEGRAM_ALLOWED_USER_IDS` 只包含 May 和你自己的帳號。
