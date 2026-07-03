# Agent Bridge 規模化擴展 — 完整規格（SPEC）

> 狀態：執行中。工作分支：`feat/scale-out`（僅本地，**禁止 push**）。
> 任務分解見 `specs/TASKS.md`；所有設計決策記錄於 `specs/DECISIONS.md`。

## 1. 背景與現況

Agent Bridge 是連接聊天平台與 AI agent 的三層橋接服務（Platform Adapter ←→ Bridge ←→ Agent Controller）。現況：

- Platforms：Slack（Socket Mode）、Heartbeat（排程觸發）
- Agents：Claude Code（`claude -p` subprocess + stream-json）
- Bridge：session key → UUID 映射（TTL + JSON 持久化）、全域併發 semaphore、prompt dedupe、usage 統計

### 現況架構缺口（優化目標）

| # | 缺口 | 影響 |
|---|------|------|
| G1 | 設定只有 env var，各元件 `from_env()` 各自 `load_dotenv()`，無統一載入器；`BridgeConfig` 類別預設值（10）與 env 預設值（5）不一致 | 無法巢狀管理多 platform/agent 設定；不可規模化 |
| G2 | `__init__.py` 硬編碼 import 並佈線 Slack/Heartbeat/Claude；新增元件必須改入口 | 佈線不可擴展 |
| G3 | `AgentController` protocol 缺 `cleanup_session`（入口卻直接呼叫）；介面契約只散落在 docstring | 介面不明確，新元件無所依循 |
| G4 | 只支援 1 個 platform 類型 ×1 個 agent；無 Telegram/LINE/HTTP API、無 Codex/OpenCode | 覆蓋面不足 |
| G5 | 測試 12 個檔案平鋪、無 unit/integration 分層、無共用 fixtures/helpers、無測試指南 | 新元件測試無範本 |
| G6 | 無 CI 測試 workflow（只有 commitlint/release）、無 lint/format 工具、無 issue/PR templates、無 CONTRIBUTING/CoC/SECURITY、無文件網站 | 開源專案基礎缺失 |

## 2. 目標範圍（對應使用者需求）

1. **R1 開源專案模式補全**：issue/PR templates、CONTRIBUTING、CODE_OF_CONDUCT、SECURITY、CI（test+lint）、README 重寫、docs 以 MkDocs Material 建站 + GitHub Pages workflow
2. **R2 明確介面與統一設定**：platform/agent 與 bridge 的接口契約文件化與 protocol 補全；每元件獨立 config；所有 config 均有明確 env var；支援 YAML 巢狀定義全部設定，secret 以 `$(VAR)` 於 YAML 內替換
3. **R3 全部在本地進行**：所有 commit 停在本地分支 `feat/scale-out`，不 push
4. **R4 新元件**：platforms 增加 Telegram / LINE / POST API；agents 增加 Codex / OpenCode
5. **R5 測試架構**：pytest 的 unit → integration 完整分層，含每種元件的測試方式文件

## 3. 詳細規格

### S1 統一設定系統（R2）

**檔案**：`src/agent_bridge/config.py` 保留 `BridgeConfig`；新增 `src/agent_bridge/config_loader.py`（或等價模組）。

1. **來源優先序**：內建預設 < YAML 檔 < 環境變數（env 永遠最高，符合 12-factor 部署覆蓋需求）。
2. **YAML 檔探索**：`AGENT_BRIDGE_CONFIG` env（或 CLI `-c/--config`）指定路徑（指定但不存在 → 啟動即 `ValueError`）；未指定時若 `./agent-bridge.yaml` 存在則使用；都沒有 → 純 env 模式（100% 向後相容）。
3. **YAML 結構**（巢狀）：
   ```yaml
   log_level: INFO
   agent: claude            # 選用的 agent（單一 active agent）
   bridge:
     session_store_path: ./sessions.json
     session_ttl_hours: 72
     max_concurrent_sessions: 5
     dedupe:
       ttl_seconds: 0
       max_entries: 512
       simhash_threshold: 0
   platforms:
     slack:
       bot_token: $(SLACK_BOT_TOKEN)
       app_token: $(SLACK_APP_TOKEN)
       ...
     telegram: { ... }
     line: { ... }
     api: { ... }
     heartbeat: { ... }
   agents:
     claude: { work_dir: ., permission_mode: acceptEdits, ... }
     codex: { ... }
     opencode: { ... }
   ```
   YAML 巢狀鍵與 env var 一一對應（例：`platforms.slack.bot_token` ⇔ `AGENT_BRIDGE_SLACK_BOT_TOKEN`；`bridge.dedupe.ttl_seconds` ⇔ `AGENT_BRIDGE_DEDUPE_TTL_SECONDS`）。對應表必須寫入 `docs/configuration.md`。
4. **`$(VAR)` secret 替換**：載入 YAML 後對所有字串值做 `$(VAR)` → `os.environ["VAR"]` 替換；未定義的 VAR → 啟動 `ValueError`（fail fast，訊息列出缺少的變數）；`$$(` 逸出為字面 `$(`。
5. **實作介面**：`ConfigSource`（合併視圖），每個元件 config 增加 `from_source(source)` classmethod；`from_env()` 保留並委派（等價於空 YAML 的 `from_source`）。驗證邏輯（`_validate`）不變。
6. **PyYAML 為核心相依**（非 optional）。
7. 提供範例檔 `agent-bridge.example.yaml`（涵蓋全部元件、以 `$(VAR)` 示範 secret）。

### S2 介面契約與 registry 佈線（R2）

1. **Protocol 補全**（`protocols.py`）：
   - `AgentController` 增加 `async def cleanup_session(self, session_id: str) -> None`（預設可為 no-op；bridge 週期清理會呼叫）。
   - `run(session_id, prompt, is_new, context, system_prompt) -> AsyncIterator[BridgeEvent]` 簽名不變。
   - `PlatformAdapter.start()/stop()` 不變。
2. **Registry**：`agents/registry.py` 與 `platforms/registry.py`，顯式 dict 註冊（不用 entry-points 魔法）：
   - platform 條目：`name -> build(source, bridge, session_manager) -> PlatformAdapter | None`（回傳 None = 未配置/停用；required 欄位缺失時記 log 並停用，與現行 Slack 行為一致；Heartbeat 維持顯式 `enabled=true` 才啟動）。
   - agent 條目：`name -> build(source) -> AgentController`。
3. **入口重構**：佈線邏輯移到 `src/agent_bridge/app.py`；`__init__.py` 只留 `main`/`main_sync` 與 re-export。入口流程：載入 ConfigSource → 依 `agent`（env `AGENT_BRIDGE_AGENT`，預設 `claude`）建 agent → 建 Bridge → 迭代 platform registry 建立啟用的 adapters → 生命週期管理（訊號、週期清理，行為與現行一致）。
4. **契約文件** `docs/architecture.md`：三層責任、事件模型逐一定義、`handle_message` 參數語義（`session_key` 格式、`context` 為平台自訂之 opaque `dict[str,str]`、`system_prompt` pass-through、`resumable` 語義）、agent 不得解析 platform-specific context 的規則、新增元件的步驟對照。

### S3 新 Platform Adapters（R4）

共通要求：`platforms/{name}/config.py`（`from_source`/`from_env` + `_validate`）、`adapter.py`（實作 `PlatformAdapter`）、registry 註冊、`docs/platforms/{name}.md`、`.env.example` 與 README/CLAUDE.md env 表更新、單元 + 整合測試（全部離線）。

**S3a Telegram**（long polling，免公網）
- 相依：`aiohttp`（extra `telegram`）。不用 python-telegram-bot。
- Config env：`AGENT_BRIDGE_TELEGRAM_BOT_TOKEN`（必填）、`AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS`（逗號分隔 chat id，空 = 全允許）、`AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS`（預設 30）、`AGENT_BRIDGE_TELEGRAM_STATE_PATH`（預設 `./telegram.json`，持久化 last update_id）、`AGENT_BRIDGE_TELEGRAM_API_BASE_URL`（預設 `https://api.telegram.org`，測試時可指向 fake server）。
- Session key：`telegram:{chat_id}:{message_thread_id|0}`（forum topic = thread；私聊/一般群 = 0）。
- 行為：私聊回應全部訊息；群組僅回應 @mention 或 reply 給 bot 的訊息（去除 mention 前綴）。prompt 前綴 `[{display_name} ({user_id})]: `。
- 渲染：收到後先送出 processing 佔位訊息，StatusUpdate 以 edit 更新；Completion edit 成最終文字（>4096 字切段補發）；錯誤標示清楚。per-session `asyncio.Lock`。

**S3b LINE**（webhook）
- 相依：`aiohttp`（extra `line`）。
- Config env：`AGENT_BRIDGE_LINE_CHANNEL_SECRET`、`AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN`（必填）、`AGENT_BRIDGE_LINE_WEBHOOK_HOST`（預設 `0.0.0.0`）、`AGENT_BRIDGE_LINE_WEBHOOK_PORT`（預設 `8080`）、`AGENT_BRIDGE_LINE_WEBHOOK_PATH`（預設 `/line/webhook`）、`AGENT_BRIDGE_LINE_API_BASE_URL`（預設 `https://api.line.me`，測試用）。
- Webhook：aiohttp server；驗證 `X-Line-Signature`（HMAC-SHA256(channel_secret, raw body) 的 base64）；驗證失敗回 403；驗證成功即回 200，訊息處理丟背景 task（LINE 要求快速 ack）。
- Session key：`line:{source_type}:{group_id|room_id|user_id}`（LINE 無 thread，聊天室即 session scope）。
- 渲染：LINE 不適合 streaming —— 緩衝 TextDelta，Completion 時以 Reply API（replyToken）送最終文字；token 已用/過期則 fallback Push API；>5000 字切段。StatusUpdate 僅記 log（私聊可選 loading animation，非必要）。
- prompt 前綴 `[{user_id}]: `（display name 需另呼叫 profile API，可選）。

**S3c POST API**（通用 HTTP 入口）
- 相依：`aiohttp`（extra `api`）。與 LINE 各自獨立 server（不共用，維持元件解耦）。
- Config env：`AGENT_BRIDGE_API_ENABLED`（必須顯式 `true` 才啟動）、`AGENT_BRIDGE_API_HOST`（預設 `127.0.0.1`）、`AGENT_BRIDGE_API_PORT`（預設 `8081`）、`AGENT_BRIDGE_API_AUTH_TOKEN`（選填；設定後所有請求須 `Authorization: Bearer <token>`，否則 401）。
- 端點：
  - `GET /healthz` → `{"status":"ok"}`
  - `POST /v1/messages`，body：`{"text": str（必填）, "session": str|null, "system_prompt": str|null, "context": dict[str,str]|null, "stream": bool=false}`
    - `session` 有值 → `session_key = api:client:{session}`、`resumable=True`（同 session 可續聊）；無值 → `resumable=False` 一次性。
    - `stream=false`：緩衝至 Completion，回 JSON `{"session", "text", "is_error", "usage", "status_updates": [...]}`。
    - `stream=true`：`text/event-stream` SSE，事件型別 `processing|text_delta|status|question|completion`，data 為 JSON。
  - 錯誤：400（缺 text/壞 JSON）、401（token 錯）、503（capacity_full 時 is_error completion 對應）。

### S4 新 Agent Controllers（R4）

共通要求：`agents/{name}/config.py`、`controller.py`、`events.py`（外部輸出 → BridgeEvent），只 yield 泛型 BridgeEvent；`system_prompt`/`prompt` 為 opaque 字串；registry 註冊；`docs/agents/{name}.md`（含所依據的 CLI 版本/旗標假設）；fake-CLI 測試（不需真 CLI）。兩者都必須：subprocess 以 `start_new_session=True` 隔離、整體 timeout、process group 清理、stderr 背景 drain、對未知事件型別容錯（log + skip）—— 模式參照 `agents/claude/controller.py`。

**S4a Codex**（OpenAI Codex CLI）
- 呼叫：`codex exec --json <prompt>`（JSONL 事件輸出）；resume：`codex exec resume <native_id> --json <prompt>`。實作前以 `codex --help` / 官方文件驗證旗標，差異記入 DECISIONS。
- Codex 自行產生 session/thread id（由事件流取得）→ controller 持久化 `bridge_session_id → native_id` 映射：`AGENT_BRIDGE_CODEX_SESSION_MAP_PATH`（預設 `./codex-sessions.json`）。
- Config env：`AGENT_BRIDGE_CODEX_WORK_DIR`（預設 `.`）、`AGENT_BRIDGE_CODEX_MODEL`（選填）、`AGENT_BRIDGE_CODEX_SANDBOX`（`read-only|workspace-write|danger-full-access`，預設 `workspace-write`）、`AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS`（預設 600）、SESSION_MAP_PATH。
- `system_prompt`：若 CLI 無等價旗標，以明確分隔符前置於 prompt（文件化此行為）。
- 事件映射：assistant/agent message → TextDelta；exec/tool 事件 → StatusUpdate；終態事件 → Completion（有 usage 就帶入 canonical keys）。

**S4b OpenCode**
- 呼叫：`opencode run <prompt>` 搭配 session 旗標（`--session <id>` 或等價）；輸出格式以 JSON 模式優先，無則解析最終文字（一次 TextDelta + Completion）。實作前驗證 CLI 介面，假設寫入 docs 與 DECISIONS。
- Session 映射同 Codex 模式（若 CLI 接受外部 id 則直接用 bridge session_id，免映射檔）：`AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH`（預設 `./opencode-sessions.json`，需要才建）。
- Config env：`AGENT_BRIDGE_OPENCODE_WORK_DIR`（預設 `.`）、`AGENT_BRIDGE_OPENCODE_MODEL`（選填）、`AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS`（預設 600）。

### S5 測試架構（R5）

1. **目錄重組**（既有測試搬遷，不改測試語義）：
   ```
   tests/
   ├── conftest.py            # 共用 fixtures：tmp store、env 清理/注入、事件收集器
   ├── helpers/               # FakeAgentController、fake CLI 腳本產生器、aiohttp fake server utils
   ├── unit/
   │   ├── bridge/            # bridge、session、dedupe、events、config loader
   │   ├── agents/{claude,codex,opencode}/
   │   └── platforms/{slack,heartbeat,telegram,line,api}/
   └── integration/           # 端到端：platform → bridge → (fake) agent
   ```
2. **Markers**：`integration`（`pyproject.toml` 註冊）；預設全跑，`-m "not integration"` 為快速通道。全部測試離線、不需真實 token/CLI。
3. **整合測試最低涵蓋**：
   - API platform：真 aiohttp server（ephemeral port）→ bridge → FakeAgentController（buffered + SSE 兩模式）
   - Telegram：fake Bot API server → adapter long-poll → bridge → fake agent → 驗證送出的訊息
   - LINE：aiohttp test client 打 webhook（含正確/錯誤簽章）→ 驗證 reply 呼叫
   - Claude/Codex/OpenCode：fake CLI 腳本（tmp 目錄產生、PATH 注入）驅動真 controller
   - Config：YAML + env 覆蓋 + `$(VAR)` 全鏈路載入
4. **`docs/testing.md`**：測試金字塔說明、每種元件（platform/agent/bridge）的測試 checklist 與範本、fake CLI / fake server 模式教學、如何跑（`uv run pytest`、markers）。

### S6 開源專案基礎建設（R1）

1. `.github/ISSUE_TEMPLATE/bug_report.yml`、`feature_request.yml`、`config.yml`；`.github/PULL_REQUEST_TEMPLATE.md`（checklist：conventional commits、tests、docs 同步）。
2. `CONTRIBUTING.md`（uv 開發流程、測試、lint、commit 規範、新增 platform/agent 指南連結）、`CODE_OF_CONDUCT.md`（Contributor Covenant 2.1）、`SECURITY.md`。
3. **Ruff**：lint + format（取代無工具現況），`[tool.ruff]` line-length=100、rules 至少 `E,F,W,I,B,UP,SIM`；全 codebase 修到乾淨；接入 `.pre-commit-config.yaml`。
4. **CI** `.github/workflows/ci.yml`：PR + push 觸發；uv、Python 3.12/3.13 matrix、`ruff check` + `ruff format --check` + `pytest`。
5. **MkDocs Material**：`mkdocs.yml`（nav 涵蓋 index/getting-started/configuration/architecture/platforms/*/agents/*/testing/contributing/releasing）、`docs/index.md`；dependency group `docs`；`mkdocs build --strict` 必須通過；`.github/workflows/docs.yml` push main 時部署 GitHub Pages（檔案建好即可，不 push）。
6. **README 重寫**：badges、platform × agent 支援矩陣、YAML/env 雙模式 quick start、文件網站連結。

## 4. 全域約束

- **C1**：所有變更僅在本地分支 `feat/scale-out`；**絕不執行 `git push`**。
- **C2**：每個任務結束以 Conventional Commit 提交（遵循 CLAUDE.md `### Commits`）。
- **C3**：每個任務把新決策 append 到 `specs/DECISIONS.md`。
- **C4**：文件同步規則（CLAUDE.md）：改元件 → 同步 `docs/…`；新 env var → 同步 `.env.example`、README 表、CLAUDE.md 表。
- **C5**：每個任務結束 `uv run pytest -q` 全綠；Task 8 之後 ruff 也必須乾淨。
- **C6**：不動使用者根目錄既有的 `issue.md`、`bridge-dedupe-plan.md`（歷史工作檔，不納入 commit）。
- **C7**：測試不得依賴網路或真實憑證。

## 5. 驗收條件（Task 9 逐項核對）

- [ ] A1 `uv run pytest -q` 全綠（unit + integration）
- [ ] A2 純 env 模式（無 YAML 檔）行為與現行完全相容
- [ ] A3 `agent-bridge.example.yaml` 可被 loader 載入；`$(VAR)` 未定義時啟動報錯且訊息點名變數；env var 覆蓋 YAML 值
- [ ] A4 5 個 platforms（slack/heartbeat/telegram/line/api）與 3 個 agents（claude/codex/opencode）均經 registry 建立，入口無元件硬編碼
- [ ] A5 `AgentController` protocol 含 `cleanup_session`；`docs/architecture.md` 完整契約
- [ ] A6 每個 platform/agent 有獨立 config + 明確 env var，且 YAML 鍵 ⇔ env var 對應表在 `docs/configuration.md`
- [ ] A7 tests/ 呈 unit/integration 分層，integration 至少涵蓋 S5.3 清單
- [ ] A8 `docs/testing.md` 含每種元件測試方式
- [ ] A9 issue/PR templates、CONTRIBUTING、CODE_OF_CONDUCT、SECURITY 存在且內容有效
- [ ] A10 `ruff check` 與 `ruff format --check` 乾淨；CI workflow 檔存在
- [ ] A11 `uv run mkdocs build --strict` 通過；docs workflow 檔存在
- [ ] A12 README 重寫（矩陣、badges、雙模式 quick start）；env var 表三處（README/CLAUDE.md/.env.example）一致
- [ ] A13 沒有任何 commit 被 push（`git status -sb` 顯示僅本地領先）
- [ ] A14 `specs/DECISIONS.md` 記錄所有重大決策
