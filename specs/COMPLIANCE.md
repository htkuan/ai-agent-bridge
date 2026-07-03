# SPEC 驗收合規報告（Task 9）

> 核對日期：2026-07-04。分支：`feat/scale-out`（僅本地，未 push）。
> 核對範圍：`specs/SPEC.md` 第 5 節 A1–A14 全數，加跨任務一致性抽查（B 節）。
> 修正 commit：`chore: final spec compliance fixes`（見文末「已修復問題」）。

## A1–A14 逐項核對

| # | 驗收條件 | 結果 | 證據 |
|---|----------|------|------|
| A1 | `uv run pytest -q` 全綠 | **PASS** | `562 passed in 7.78s`；`-m integration` → `40 passed, 522 deselected`；`-m "not integration"` → `522 passed, 40 deselected` |
| A2 | 純 env 模式與現行完全相容 | **PASS** | 每個 config 的 `from_env()` 皆委派 `from_source(ConfigSource.empty())`；`ConfigSource.get` 在無 YAML 時 live 讀 `os.environ`（`config_loader.py`）；`load_config_source` 無檔案時回傳空 source。單元測試 `tests/unit/bridge/test_config_loader.py`、`test_config_from_source.py` 及各元件 config 測試涵蓋 |
| A3 | example YAML 可載入；缺 `$(VAR)` 報錯點名；env 覆蓋 YAML | **PASS** | 臨時腳本實測（15 項全過）：全部 9 個元件 config 以 `from_source` 從 `agent-bridge.example.yaml` 建立且值正確；移除 `TELEGRAM_BOT_TOKEN` → `ValueError: Config file references undefined environment variables: TELEGRAM_BOT_TOKEN`；`AGENT_BRIDGE_SESSION_TTL_HOURS=24` 覆蓋 YAML 的 72、env 亦可覆蓋 `$(VAR)` 已替換的值 |
| A4 | 5 platforms + 3 agents 經 registry 建立，入口無硬編碼 | **PASS** | `platforms/registry.py`：slack/telegram/line/api/heartbeat 五個 builder；`agents/registry.py`：claude/codex/opencode 三個 builder；`app.py` 只 import 兩個 registry 與核心模組，元件 import 全在 builder 內 lazy 進行 |
| A5 | protocol 含 `cleanup_session`；`docs/architecture.md` 契約完整 | **PASS** | `protocols.py:31` `async def cleanup_session`（含 no-op 合法語義）；`docs/architecture.md` 涵蓋三層責任表、五事件逐一定義＋順序保證、`handle_message` 五參數語義、context 不可解析規則、registry 佈線、新增元件雙清單 |
| A6 | 每元件獨立 config + env var；對應表在 `docs/configuration.md` | **PASS** | 9 個元件各有 `config.py`（`from_source`/`from_env` + `_validate`）；`docs/configuration.md` 表格經腳本比對涵蓋全部 51 個變數（含 `AGENT_BRIDGE_CONFIG`、`AGENT_BRIDGE_AGENT`、`ANTHROPIC_API_KEY` 特註），無遺漏 |
| A7 | tests 分層；integration 涵蓋 S5.3 | **PASS** | `tests/{conftest.py,helpers/,unit/{bridge,agents/{claude,codex,opencode},platforms/{slack,heartbeat,telegram,line,api}},integration/}`；integration 檔：api / telegram / line / claude / codex / opencode / config / app_wiring 八個 end-to-end，S5.3 七項全數對應 |
| A8 | `docs/testing.md` 含每種元件測試方式 | **PASS** | 金字塔說明、markers 用法、共用 fixtures/helpers（fake CLI、FakeApiServer）教學、「Playbook: testing a new platform adapter」與「Playbook: testing a new agent controller」雙 checklist、bridge 層測試位置（`tests/unit/bridge/`）與 ground rules |
| A9 | 社群文件存在且有效 | **PASS** | `bug_report.yml`（98 行，platform×agent dropdown）、`feature_request.yml`（59 行）、`config.yml`（關 blank issues + 導流）、`PULL_REQUEST_TEMPLATE.md`（conventional commits / tests / docs 同步 checklist）、`CONTRIBUTING.md`（124 行，uv 流程）、`CODE_OF_CONDUCT.md`（Contributor Covenant 2.1）、`SECURITY.md`（私密回報 + threat-model 範圍） |
| A10 | ruff 乾淨；CI workflow 存在 | **PASS** | `ruff check .` → `All checks passed!`；`ruff format --check .` → `102 files already formatted`；`.github/workflows/ci.yml`：lint job + 3.12/3.13 test matrix，指令與本地一致 |
| A11 | `mkdocs build --strict` 通過；docs workflow 存在 | **PASS** | strict build 成功（0.52s，無 warning）；`.github/workflows/docs.yml` 走官方 Pages actions（build `--strict` → artifact → deploy） |
| A12 | README 重寫；env 表三處一致 | **FAIL → 修復後 PASS** | README 有 badges、5×3 支援矩陣、.env/YAML 雙模式 quick start。三處變數集合腳本 diff 初查：README 缺 10 個、CLAUDE.md 缺 2 個、`.env.example` 完整 —— 已補齊，修復後三處各 51 個變數、diff 為空 |
| A13 | 無任何 commit 被 push | **PASS** | `git status -sb` → `## feat/scale-out`（無 upstream）；`git branch -r` 無 `origin/feat/scale-out`；`main..HEAD` 全部 14 個 commit 僅存在本地 |
| A14 | DECISIONS.md 記錄完整 | **PASS** | D1–D66 連號（腳本驗證：無斷號、無重號），涵蓋規劃期與 T1–T9 各任務決策 |

## B 節：跨任務一致性抽查

| # | 抽查項 | 結果 |
|---|--------|------|
| B1 | example YAML 鍵 vs 各 config `from_source` 的 `yaml_path` | **無問題**。實際載入驗證（A3 腳本）：全部巢狀鍵（含 `platforms.slack.usage_report.enabled`、`platforms.line.webhook.host/port/path`、`bridge.dedupe.*`）被對應 config 正確讀取，值與 YAML 宣告一致 |
| B2 | 三個 agent controller 的 `context`/`system_prompt` 契約 | **無問題**。三者的 `context` 僅出現在 `run()` 簽名、無任何 key 解析；codex/opencode 以相同的 `<platform-directives>` 分隔符前置 `system_prompt`（D10/D51），claude 用 `--append-system-prompt` —— 均為 opaque pass-through |
| B3 | 五個 platform session key 格式 | **無問題**。`slack:{channel}:{thread_ts}`、`telegram:{chat_id}:{thread_id\|0}`、`line:{source_type}:{target}`、`api:client:{session}` / `api:oneshot:{uuid}`、`heartbeat:tick:{iso_ts}` —— 全部符合 `{platform}:{scope}:{identifier}`，最易變段在末（dedupe scope 規則） |
| B4 | docs 交叉連結 / README 連結 | **無問題**。`mkdocs build --strict` 零 warning；跨頁錨點（`bridge.md#usage-reporting`、`platforms/slack.md#setup`、`agents/{codex,opencode}.md#prerequisites`、`agents/claude.md#worktree-mode`）逐一比對標題 slug 均存在；README 連結全為絕對 URL，指向的站內頁面皆在 `mkdocs.yml` nav 中 |
| B5 | `.env.example` vs `docs/configuration.md` | **無問題**。腳本 diff：兩者變數集合相等（51 個），configuration.md 每個變數都有對應表列 |
| B6 | CLAUDE.md 專案結構樹 vs 實際 `src/**/*.py` | **發現問題 → 已修復**。樹漏列 `dedupe.py`（scale-out 前即遺漏）；補上後 40 個 `.py`（含 `__init__.py`）與樹逐一對應 |
| B7 | CLI 可跑；config→registry 組裝不炸 | **無問題**。`uv run agent-bridge --help` 正常；以 heartbeat-only YAML 實跑 `app.main` 全生命週期（載入 config、建 agent/bridge/adapters、啟動、訊號、乾淨關閉，log 完整）；`tests/integration/test_app_wiring.py` 亦覆蓋同路徑。附帶驗證：`.env` 的 `AGENT_BRIDGE_HEARTBEAT_ENABLED=false` 正確覆蓋 YAML 的 `enabled: true`（env > YAML 實跑成立） |

## 已修復問題（本次 commit）

1. **README env 表缺 10 個變數**（違反 A12 與 D65）：補 `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL`、`AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE`、`AGENT_BRIDGE_SLACK_ALLOW_CHANNELS`、`AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE`、`AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED`、`AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE`、`AGENT_BRIDGE_CLAUDE_EFFORT`、`AGENT_BRIDGE_DEDUPE_TTL_SECONDS`、`AGENT_BRIDGE_DEDUPE_MAX_ENTRIES`、`AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD`。
2. **CLAUDE.md env 表缺 2 個變數**：補 `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL/MESSAGE`（此二者早於 scale-out 存在，歷來即漏列）。
3. **CLAUDE.md 專案結構樹缺 `dedupe.py`**：補列。

程式碼（`src/`、`tests/`）零改動 —— T1–T8 的實作與測試未發現需要修復的缺陷。

## 修復後最終驗證

- `uv run pytest -q` → **562 passed**
- `uv run ruff check .` → All checks passed!；`uv run ruff format --check .` → 102 files already formatted
- `uv run mkdocs build --strict` → 通過，無 warning
- env 表三處 diff → 空（README = CLAUDE.md = `.env.example` = 51 個變數）
