# 審查紀錄：/architect 盲點審查（2026-11-21）

- **範圍**：Windows 開發 × Linux Docker 部署 × 三層有界 Session × Telegram→LINE 合約
- **審查依據**：`CLAUDE.md`、`docs/ARCHITECTURE.md` v1.2、`docs/PHASE1_TASKS.md`（當時版本）、`scripts/test.py`、`.gitattributes`、`.claude/setting.json`
- **審查者**：Claude Code（Local Dev Engineer）
- **裁決者**：System Architect（Gemini），v1.3 正式裁決
- **審查方式**：當時 repo 尚未有 `src/`，所以只做了 `/architect` 的 Phase A（ground）與 red-flag 篩檢。型別草圖（Phase B arena）延到 T1 開工時再做。
- **標記說明**：**[實測]** 表示在 repo 檔案中直接確認；**[推論]** 表示根據平台或 API 知識推得，「驗證」欄寫明如何證實。

## 0. 環境與防線

| # | 嚴重度 | 發現 | v1.3 裁決 / 處置 | 落點 |
|---|---|---|---|---|
| 0.1 | P0 | [實測] 設定檔名為 `.claude/setting.json`，Claude Code 不會讀取，因此 deny 規則與 Stop hook 都沒有生效 | 使用者已手動改名為 `settings.json`（檔案存在已確認） | — |
| 0.2 | P1 | [實測] deny 規則用了 `Bash(Remove-Item ...)` 前綴，攔不到 PowerShell 工具的呼叫 | 使用者回報已補上 PowerShell 與 pytest 安全規則 | — |
| 0.3 | P0 | [實測] `TEST_NO_SEND=1` 只在 `scripts/test.py` 裡設定，但 `uv run pytest` 被允許直接執行。[推論] 三個 SDK 底層都走 httpx，只在 adapter 層攔截一定會漏 | 在 `tests/conftest.py` 用 autouse fixture 強制設定 env，並在 httpx transport / socket 層全域阻斷 | T1 DoD、ARCHITECTURE §6、CLAUDE.md |

## 1. Windows 開發 ↔ Linux Docker

| # | 嚴重度 | 發現 | v1.3 裁決 | 落點 |
|---|---|---|---|---|
| 1.1 | P0 | [推論] dev 與 prod 共用同一個 bot token 做 long polling 會出現 `409 Conflict`。驗證：用同一個 token 同時啟動兩個 polling | dev 與 prod 強制使用不同 token | `.env.example`、`DEPLOY_LINUX.md`（T1/T5）、§9 |
| 1.2 | P0 | [實測] 沒有任何 DoD 在 Linux 容器內實際跑過測試 | T1 與 T5 加入 `docker compose run --rm app python scripts/test.py`（使用容器內 named volume） | T1/T5 DoD、§9 |
| 1.3 | P1 | [推論] slim image 缺 libGL，`opencv-python` 會 import 失敗 | 改用 `opencv-python-headless`，另加 `pillow-heif` | T2、§9 |
| 1.4 | P0 | [實測] `test.py` 設定 `TZ`；[推論] Windows 不認得 IANA 格式的 `TZ`，也沒有 tzdata | 加入 `tzdata`、注入 `Clock`、用 `ZoneInfo` 換算 UTC 區間 | ADR-0005、T1 |
| 1.5 | P0 | [推論] SQLite 不會保存時區資訊，讀回來是 naive datetime | 使用 `UTCDateTime` TypeDecorator | ADR-0005、T1 |
| 1.6 | P1 | [推論] Windows Docker Desktop 的 bind mount 會讓 SQLite WAL 不可靠 | 容器測試用 named volume；`./data` bind mount 只用於 Linux prod | §9、CLAUDE.md |
| 1.7 | P1 | [實測] DB 存的是平台相依的路徑（`local_path`、`media_bytes_ref: Path`） | 改存相對 `DATA_DIR` 的 POSIX 字串；domain 只傳 `asset_id` | ADR-0005、§2、§5 |
| 1.8 | P1 | [實測] 文件沒寫備份、log rotation、restart 策略、容器 UID | 寫入 `DEPLOY_LINUX.md`：`sqlite3 .backup`、`restart: unless-stopped`、log rotation、非 root | T1/T5、§9 |
| 1.9 | P2 | [推論] shell form 的 CMD 讓 PID 1 收不到 SIGTERM；Windows 不支援 `add_signal_handler` | exec-form CMD + `init: true`；優雅關機交給 PTB `run_polling()` | T1/T5、§9 |
| 1.10 | P2 | `.env` 可能帶 BOM；Python 版本沒有釘死 | `.python-version` 設為 3.12；文件註明 `.env` 要用 UTF-8 無 BOM | T1、§9 |

## 2. 三層有界 Session

| # | 嚴重度 | 發現 | v1.3 裁決 | 落點 |
|---|---|---|---|---|
| 2.1 | P0 | [實測] Layer 1 完成後就 close，但 Layer 2 需要「剛剛的」產出，兩者矛盾 | Layer 1 完成後開一個乾淨的 Layer 2，`state_json` 只放 `last_artifact_id` 與 `workflow` | ADR-0003、T4 |
| 2.2 | P0 | [實測] 估算器寫成 `char_len/2`，對中文是低估。驗證：用 Gemini `count_tokens` 抽樣 | 改為 `ceil(len*1.2)`，再用 API 回傳的 usage 校正 | ADR-0003、T3 |
| 2.3 | P0 | [實測] 「500–2,500 token」的承諾算起來不成立；圖片與語音沒有列入預算 | 制定預算表：文字 3,500、多模態 6,000、語音最長 120 秒、單則文字最多 2,000 字 | ADR-0003、§4.2、T3 |
| 2.4 | P0 | [實測] 抽槽可以重複執行，等於遞迴壓縮 | `extraction_count ≤ 1`，以 Pydantic 驗證後決定性截斷；第二次撞頂就強制關閉 | ADR-0003、T3 |
| 2.5 | P1 | [實測] 「7th turn」與「6 messages」兩種說法混用 | 改寫成不變量：每次 commit 後 `len(recent_turns) ≤ 6` 且歷史 ≤ 1,500 | §4.3、T3 DoD (c) |
| 2.6 | P1 | [推論] 每則訊息都呼叫 LLM 判斷換話題，掛掉或預算用完時沒有定義行為 | 路由優先序：預算 100% 直接回固定文案；callback 與照片批次走 0 LLM；只有自由文字才呼叫 DeepSeek | §4.4、T3 |
| 2.7 | P1 | [推論] 同一個 user 的並發事件可能互相覆寫 session | partial unique index + per-user `asyncio.Lock`，並明訂單一 process 假設 | §4.5、T3 |
| 2.8 | P1 | [實測] `media_batches` 狀態機不完整 | 補上狀態轉移表與啟動時的孤兒回收 | §4.6、T2 |
| 2.9 | P0 | [推論] FTS5 的 `unicode61` 不會切中文，搜「翡翠」找不到「翡翠放紅櫃子」。驗證：T1 先寫紅燈測試 | 新增 `search_tokens` 欄位（DeepSeek 關鍵詞 + 字元 bigram） | ADR-0004、T1/T5 |
| 2.10 | P1 | [推論] FTS5 搬不到 Postgres，Alembic autogenerate 也可能誤刪 FTS 表 | 手寫 migration + `include_object` 排除 FTS 內部表 | ADR-0004、T1 |
| 2.11 | P2 | 價格表與 model ID 要手動同步；80% 告警重啟後會重發 | 價格表與 model ID 放在同一個 config，未知 model 啟動即失敗；告警日期持久化 | §3、§6、T1/T3 |

## 3. Telegram → LINE 合約

| # | 嚴重度 | 發現 | v1.3 裁決 | 落點 |
|---|---|---|---|---|
| 3.1 | P0 | [實測] `users` 綁死單一 channel，換到 LINE 會失去所有記憶 | 拆出 `users`（人）與 `channel_identities`，並加 UNIQUE 約束 | ADR-0001、T1 |
| 3.2 | P0 | [推論] `reply_handle` 把永久的 chat_id 和一次性的 replyToken 混成一個欄位。驗證：查 LINE 官方文件的 replyToken 時效 | 改用 `ReplyTarget(address, ephemeral_token, token_expires_at)`，reply 或 push 由 adapter 決定 | ADR-0002、§2 |
| 3.3 | P0 | [推論] 先送「處理中...」會把 LINE 的 reply token 用掉 | 單一回覆不變量；進度只用 chat action | ADR-0002、T5 |
| 3.4 | P0 | [推論] CopyTextButton 上限 256 字，LINE Clipboard 上限 1,000 字 | `body_text` 上限 1,000；Telegram 超過 256 字改用 `<pre>`，並測 255/256/257 邊界 | ADR-0002、T1/T5 |
| 3.5 | P1 | [推論] Telegram `callback_data` 上限 64 bytes，LINE label 上限 20 字 | label ≤20、使用伺服器端短 token（見下方「實作細化」） | ADR-0002、§2、T1 |
| 3.6 | P1 | [實測] 缺少冪等鍵 | 加入 `event_id`（見下方「實作細化」） | ADR-0002、§2、T5 |
| 3.7 | P1 | [推論] 沒有處理 document 圖片、HEIC、貼圖 | 新增 `unsupported` 類型；把 document 與 HEIC 正規化成 image | ADR-0002、T2/T5 |
| 3.8 | P1 | [推論] 只存 Telegram `file_id` 的話，Phase 2 拿不到原圖 | 本地必須保留原始檔，`external_file_id` 只當快取 | ADR-0002、§5 |
| 3.9 | P2 | 缺 `altText`、沒有 escape、沒有用到 imageSet 的 index/total | `alt_text` 自動衍生、adapter 負責 escape、新增選填的 `group_index` 與 `group_total` | §2、T1/T5 |

## 實作細化（v1.3 未逐字列出，依原審查建議寫入文件；v1.3.1 架構師原則核准）

v1.3 的第 5 項以「3.1~3.9」概括裁決，第 3 項也要求補上狀態表。以下內容在文件中已具體化，但 v1.3 原文沒有逐字寫到：

1. **3.5**：`ActionButton.label` ≤20 字、`callback_data` 改用伺服器端短 token（≤64 bytes）。
2. **3.6**：`IncomingEvent.event_id` 作為冪等鍵，處理前先去重。
3. **2.10**：搜尋經由 `StoragePort.search_knowledge()`，Phase 3 由 Postgres 實作同一個介面。
4. **2.11**：80% 告警日期存放位置定為 `tenants.budget_alert_80_date`。
5. **2.8**：狀態轉移表的細節。全部模糊時 → `done`；已 `done` 的批次再收到 callback → 冪等 no-op；新的連拍開新批次；孤兒回收會補送該批次唯一的一次回覆。
6. **ADR-0001**：身分解析 `(channel, external_user_id)` 是唯一不帶 `tenant_id` 與 `user_id` 的查詢，因為這兩個 ID 正是由它取得。

## v1.3 遺留待裁決（v1.3.1 已裁決）

以下衝突已由架構師 v1.3.1 裁決，結果寫在 ADR-0003 Decision 3、9、10：文字預算 4,500（retrieved_context 1,200 共用槽）、單則自由文字 850 字、每則 recent_turn ≤ 250 tokens、`awaiting_intent` TTL 15 分鐘。原始描述保留如下。

- **G1**：Layer 2 只存 `last_artifact_id`，但改寫文案需要把原文放進 prompt，預算表沒有替原文留位置。A 版加 B 版約 2,400 tokens。
- **G2**：一則 assistant 回覆（例如銷售文案）本身就可能超過 history 1,500 的上限，第一個 turn 就違反不變量。
- **G3**：單則文字上限 2,000 字約等於 2,400 tokens，但 `current_msg` 的預算只有 650。
- **G4**：`awaiting_intent` 的按鈕有效期限沒有裁決。

**影響**：G4 由 T2 實作，G1 到 G3 由 T3 與 T4 實作。

## 裁決 → 文件對照（驗證用）

| v1.3 項目 | 落點 |
|---|---|
| 1 測試網路全域攔截 | T1 Deliverables/DoD、ARCHITECTURE §6、CLAUDE.md Workflow |
| 2 跨平台與容器 | ARCHITECTURE §9、ADR-0005、T1/T2/T5、CLAUDE.md Environment |
| 3 Session 與預算 | ARCHITECTURE §4、ADR-0003、T2/T3/T4、CLAUDE.md 3-Layer |
| 4 中文檢索 | ARCHITECTURE §5、ADR-0004、T1/T5 |
| 5 跨平台合約 | ARCHITECTURE §2/§5、ADR-0001、ADR-0002、T1/T5、CLAUDE.md Contract |
