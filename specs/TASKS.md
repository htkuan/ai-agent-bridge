# 任務分解（每個任務 = 一個乾淨 session）

> 執行規則：依序執行 T1→T9；每個任務開始前先讀 `specs/SPEC.md` 與本檔對應章節；
> 結束時：測試全綠 → 更新文件 → append `specs/DECISIONS.md` → Conventional Commit（本地，不 push）。

## T1 統一設定系統 + Registry 佈線（SPEC S1 + S2）

- 新增 `config_loader.py`：YAML 載入、`$(VAR)` 替換（`$$(` 逸出、缺 var 即 ValueError）、env > yaml > default 的 `ConfigSource`
- `AGENT_BRIDGE_CONFIG` / `-c/--config` / `./agent-bridge.yaml` 探索邏輯
- 全部既有 config（Bridge/Slack/Heartbeat/Claude）加 `from_source`；`from_env` 委派；修正 BridgeConfig 類別預設 10 vs env 預設 5 的不一致（以 5 為準）
- `agents/registry.py`、`platforms/registry.py`；`app.py` 佈線；`__init__.py` 瘦身
- `protocols.py`：`AgentController.cleanup_session` 補進 protocol
- `agent-bridge.example.yaml`、`docs/configuration.md`、`docs/architecture.md`
- 相依：pyproject 加 `pyyaml`（core）
- 測試：loader 單元測試（precedence、$(VAR)、逸出、缺檔、壞 YAML）、registry 測試、既有測試全綠
- Commit：`feat(config): unified yaml config with $(VAR) secrets and registry wiring`

## T2 測試架構重組（SPEC S5）

- 平鋪測試搬入 `tests/unit/{bridge,agents/claude,platforms/slack,platforms/heartbeat}/`（含 T1 新測試歸位）
- `tests/conftest.py` 共用 fixtures、`tests/helpers/`（FakeAgentController、事件收集、fake CLI builder、aiohttp fake server utils）
- `integration` marker 註冊；建 `tests/integration/`（先放 config 全鏈路 + claude fake-CLI 端到端）
- `docs/testing.md`
- Commit：`test: restructure into unit/integration layers with shared helpers`

## T3 Telegram adapter（SPEC S3a）

- `platforms/telegram/{config,adapter}.py` + registry 註冊 + extra `telegram`
- long-poll `getUpdates`（offset 持久化）、mention/reply 過濾、佔位訊息 edit 渲染、4096 切段
- 單元測試（config、session key、渲染、過濾）+ 整合測試（fake Bot API server）
- `docs/platforms/telegram.md`、`.env.example`、README/CLAUDE.md 表、example yaml
- Commit：`feat(telegram): telegram platform adapter via long polling`

## T4 LINE adapter（SPEC S3b）

- `platforms/line/{config,adapter}.py` + registry + extra `line`
- aiohttp webhook server、HMAC-SHA256 簽章驗證、快速 ack + 背景處理、reply→push fallback、5000 切段
- 單元 + 整合測試（test client 打 webhook、正/誤簽章、fake LINE API）
- `docs/platforms/line.md` + 文件/範例同步
- Commit：`feat(line): line platform adapter via webhook`

## T5 POST API adapter（SPEC S3c）

- `platforms/api/{config,adapter}.py` + registry + extra `api`
- `POST /v1/messages`（buffered JSON + SSE）、`GET /healthz`、bearer auth、session/resumable 語義
- 單元 + 整合測試（真 server ephemeral port、兩種回應模式、401/400、capacity）
- `docs/platforms/api.md` + 文件/範例同步
- Commit：`feat(api): generic http post platform adapter`

## T6 Codex agent（SPEC S4a）

- `agents/codex/{config,controller,events}.py` + registry
- `codex exec --json` / `codex exec resume`；native session 映射持久化；事件容錯
- fake-CLI 單元 + 整合測試（正常流、resume、timeout、壞輸出、非零退出）
- `docs/agents/codex.md` + 文件/範例同步
- Commit：`feat(codex): codex agent controller`

## T7 OpenCode agent（SPEC S4b）

- `agents/opencode/{config,controller,events}.py` + registry
- CLI 介面查證後實作（JSON 優先、文字 fallback）；session 處理
- fake-CLI 測試同 T6 範圍
- `docs/agents/opencode.md` + 文件/範例同步
- Commit：`feat(opencode): opencode agent controller`

## T8 開源基礎建設（SPEC S6）

- `.github/ISSUE_TEMPLATE/*`、`PULL_REQUEST_TEMPLATE.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`
- ruff（pyproject 設定、全庫修乾淨、pre-commit hook）
- `.github/workflows/ci.yml`（ruff + pytest matrix 3.12/3.13）
- MkDocs Material：`mkdocs.yml`、`docs/index.md`、docs group、`mkdocs build --strict` 通過、`.github/workflows/docs.yml`
- README 重寫
- Commit 可拆多個：`chore(lint): adopt ruff` / `ci: add test workflow` / `docs: mkdocs site + community files` 等

## T9 最終驗證

- 逐項核對 SPEC 第 5 節 A1–A14，產出核對報告（appende 到 specs/ 下）
- 全測試、ruff、mkdocs build --strict、env 表三處一致性 diff 檢查
- 發現問題就地修復；彙整 DECISIONS.md
- Commit：`chore: final spec compliance fixes`（如有修復）
