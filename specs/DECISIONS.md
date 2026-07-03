# 決策記錄（Decision Log）

> 格式：`D<編號> [任務] 決策 — 理由`。由各任務 session 持續 append。

- **D1 [規劃]** 設定優先序採 `內建預設 < YAML < env var` — env 最高符合 12-factor，部署端可覆蓋 repo 內設定檔；同時保證無 YAML 時行為與現行純 env 完全相同。
- **D2 [規劃]** Secret 替換語法採使用者指定的 `$(VAR)`（非 `${VAR}`）；未定義變數啟動即報錯（fail fast）；`$$(` 逸出成字面 `$(`。不做 `$(VAR:default)` 預設值語法 — 保持最小。
- **D3 [規劃]** YAML 檔探索：`AGENT_BRIDGE_CONFIG` env / CLI `--config` > `./agent-bridge.yaml` > 純 env 模式。PyYAML 列為核心相依（設定屬核心功能，且無傳遞相依負擔）。
- **D4 [規劃]** Platform/Agent 採顯式 registry dict（非 entry-points plugin 魔法）— 專案規模下顯式優於動態發現，type-check 友善、除錯直觀。
- **D5 [規劃]** 單一 active agent（`agent:` / `AGENT_BRIDGE_AGENT`，預設 `claude`），不做 per-platform agent 路由 — 該需求尚不存在，避免過度設計；registry 架構已預留未來擴充空間。
- **D6 [規劃]** Telegram 用 aiohttp 直呼 Bot API long-polling，不引入 python-telegram-bot — 相依輕（aiohttp 已是 slack extra 相依）、long-poll 免公網（與 Slack Socket Mode 同哲學）。session key 以 chat+topic 為界。
- **D7 [規劃]** LINE 必須 webhook（平台無 polling 選項）；不做 streaming（reply token 一次性），緩衝到 Completion 一次回覆，過期 fallback Push API。
- **D8 [規劃]** POST API adapter 需顯式 `AGENT_BRIDGE_API_ENABLED=true`（無 secret 可判斷是否配置）；預設綁 127.0.0.1；bearer token 選配。支援 buffered JSON 與 SSE 兩種回應。LINE 與 API 各開獨立 aiohttp server，不共用 — 維持 platform 之間互相獨立（解耦）。
- **D9 [規劃]** Codex/OpenCode 均為 subprocess controller，鏡射 claude 模組結構；CLI 不接受外部 session id 時以 JSON 檔持久化 `bridge_session_id → native_id` 映射；事件解析對未知型別容錯（log + skip）— 外部 CLI 版本演進快。
- **D10 [規劃]** Codex `system_prompt`：CLI 若無等價旗標則以分隔符前置於 prompt 並文件化 — 維持 agent 對 platform 透明的契約。
- **D11 [規劃]** 測試分層 `tests/unit/`（鏡射 src 結構）+ `tests/integration/`；全部離線，外部 CLI 用 tmp fake 腳本、HTTP 平台用 aiohttp test server/client。
- **D12 [規劃]** Lint/format 採 ruff（line-length 100）；CI 用 uv + Python 3.12/3.13 matrix；文件網站採 MkDocs Material（docs/ 已是 Markdown，遷移成本最低）。
- **D13 [規劃]** 所有工作在本地分支 `feat/scale-out`，Conventional Commits，絕不 push（使用者要求全部先在 local）。
- **D14 [規劃]** `AgentController` protocol 補上 `cleanup_session`（入口既已呼叫但 protocol 未宣告，屬契約漏洞）。
- **D15 [規劃]** `BridgeConfig.max_concurrent_sessions` 類別預設 10 與 env 預設 5 不一致 → 統一為 5（.env.example 與 README 已公告 5）。
- **D16 [規劃]** 根目錄 `issue.md`、`bridge-dedupe-plan.md` 為使用者歷史工作檔，維持 untracked 不動、不 commit。
- **D17 [T1]** `ConfigSource` 介面定為 `get(env_key, yaml_path, default) -> str | None`，一律回傳字串（env var 語意，呼叫端照舊自行轉型）— YAML 純量字串化：bool → `"true"/"false"`、數字 → `str()`、純量 list → 逗號串接（讓逗號分隔型 env var 可自然寫成 YAML list）；巢狀 mapping 出現在葉節點 → ValueError。`env` 可注入（測試隔離），預設 live 讀 `os.environ`。
- **D18 [T1]** 空字串 env var 視為未設定（fall through 到 YAML/default）— `.env` 範本慣例含 `KEY=` 空值佔位，不得遮蔽 YAML 值；與現行「空 token 視為缺失」行為一致。
- **D19 [T1]** `load_dotenv()` 從所有 config 類別移除，只在入口 `app.main()` 呼叫一次（讀 YAML 前，讓 `.env` 同時供覆蓋與 `$(VAR)` 替換）。`from_env()` = `from_source(空 source)`，只讀行程環境、不再隱式改動 `os.environ` — 這同時修復了兩個因開發者本機 `.env` 汙染而失敗的既有測試（effort 預設、heartbeat prompt 必填）。
- **D20 [T1]** Slack 的 `cleanup_stale_sessions()` 以可選 hook 處理：app 週期清理用 `getattr` 探測各 adapter 是否提供，不納入 `PlatformAdapter` protocol — 單一平台的需求不應加寬共用契約。
- **D21 [T1]** Platform registry 的 builder 內部才 lazy import 依賴第三方套件的 adapter 模組（slack-bolt 只在 Slack 有配置時載入）— optional extra 未安裝也能跑其他平台；config 模組（無外部相依）維持頂層 import。
- **D22 [T1]** CLI `-c/--config` 優先於 `AGENT_BRIDGE_CONFIG` env（顯式調用勝過環境）；兩者指定的路徑不存在都直接 ValueError，不 fallback。
- **D23 [T1]** HeartbeatConfig 的 enabled 解析由 `== "true"` 放寬為共用 truthy 集合 `{true,1,yes,on}`，與 Slack/Claude 的布林 env 解析一致。
- **D24 [T1]** Slack usage report 的 YAML 鍵採巢狀 `platforms.slack.usage_report.{enabled,template}`（對應既有 env var 不變），鏡射 `bridge.dedupe.*` 的巢狀風格；agent 專屬啟動 log（work_dir 等）移入該 agent 的 registry builder，app 入口保持元件無知。
