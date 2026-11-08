# 多人幾A幾B網路對戰系統（BC/1.0）

網路應用軟體設計 2026 期末專題。本專題實作一套 **2–6 人同時對戰的「幾A幾B」猜數字遊戲**，包含伺服器、人類玩家客戶端與 AI 代理人三種角色，三者之間全部依照課程規範的 **BC/1.0 應用層通訊協定**（TCP + 一行一則 UTF-8 JSON）溝通，因此可以和其他組的伺服器或客戶端互通對戰。

- 伺服器、客戶端與演算法 AI 只使用 **Python 3.10 以上的標準函式庫**。
- AI 代理人可選用 **LLM 模式**：呼叫 Anthropic Claude API，由 LLM 的回應決定喊數（需 `pip install anthropic` 與環境變數 `ANTHROPIC_API_KEY`；沒有時自動退回演算法）。詳見 [`bc_game/README.md` 第 7.3 節](bc_game/README.md#73-llm-模式由外部-llm-決定喊數)。
- 課程提供的 `bc_selftest.py` **48 項檢查全數通過**；自行撰寫的黑箱測試 `bc_test.py` **72 項全數通過**。

## 目錄結構

```
.
├── README.md              本文件（專案總覽）
├── bc_selftest.py         課程提供的協定自我測試程式（測伺服器）
├── bc_game/
│   ├── bc.py              主程式：server / client / ai 三種模式
│   ├── bc_test.py         自寫的獨立黑箱測試（不匯入 bc.py，只透過 TCP 測試）
│   ├── bc_llm_test.py     LLM 模式的單元測試（假的 LLM，不需金鑰）
│   ├── README.md          詳細技術說明（協定細節、錯誤碼、AI 策略與實驗數據）
│   └── requirements.txt   套件清單（LLM 模式選用 anthropic）
└── slides/
    ├── 期中簡報.pdf
    ├── 期中簡報.pptx
    └── 期中簡報.html
```

## 遊戲規則

1. 每位玩家各自持有一個 **4 位、數字互不重複** 的秘密數字（共 10×9×8×7 = 5040 種）。
2. 依伺服器隨機排定的順序輪流喊數；**一次喊數同時對所有其他存活玩家有效**，每位被猜者回報自己的 `xAyB`，結果公開給所有人。
3. 被回報 `4A0B` 的玩家出局；只剩 1 人或達到回合上限時終局，所有人揭示秘密。
4. 名次：存活者 > 被猜中者（越晚出局越前面）> 棄權者；作弊被取消資格（DQ）者沒有名次。

## 系統架構

採主從式（client–server）架構，所有角色都在同一支 `bc_game/bc.py` 中，以子命令切換：

```
 ┌──────────────┐     TCP      ┌───────────────────────────────┐
 │ 人類客戶端    │◀───────────▶│ 伺服器 server（asyncio）         │
 │ client       │  BC/1.0     │ ・連線管理（≤64 條，HELLO 10 秒逾時）│
 └──────────────┘  JSON Lines │ ・訊息解析、欄位驗證、錯誤碼        │
 ┌──────────────┐             │ ・遊戲流程：大廳→承諾→回合→揭示    │
 │ AI 代理人 ai  │◀───────────▶│ ・每位玩家的候選集合一致性檢查      │
 └──────────────┘             │ ・計算排名、寫入 logs/*.jsonl      │
 ┌──────────────┐             └───────────────────────────────┘
 │ BOT-1…BOT-K  │◀── 伺服器以 --bots K 自動啟動的 AI 子行程
 └──────────────┘
```

| 元件 | 說明 |
|---|---|
| **伺服器** `Server` | 以 `asyncio.start_server` 為每條連線開一個讀取協程，遊戲流程是另一個協程，用 `asyncio.Event` 搭配逾時等待。自行以 LF 切行，不受 TCP 分段或黏包影響；單一連線出錯只會影響自己。 |
| **人類客戶端** `Human` | 背景執行緒收網路訊息、主執行緒讀鍵盤，停在輸入時仍會即時回 `PONG`。秘密與鹽值只留在本機，回報與揭示都自動且誠實地計算，並會自行驗證別人揭示的結果。 |
| **AI 代理人** `AI` | 對每位對手維護候選集合，利用所有公開的喊數結果縮小範圍，搭配開局表與限時 most-parts 搜尋，單一對手平均約 5.24 次猜中（理論最佳約 5.21）。 |
| **共用工具** | `score()` 計算幾A幾B、`make_commit()` 產生承諾值、`encode()`/`decode()` 處理訊息編解碼。 |

### 通訊協定重點（BC/1.0）

- 傳輸層為 TCP，預設埠 **5040**；一則訊息 = 一行 UTF-8 JSON 物件，以 LF 結尾，單行上限 4096 位元組。
- 客戶端 → 伺服器 9 種訊息（`HELLO`、`COMMIT`、`GUESS`、`FEEDBACK`、`REVEAL`、`CHAT`、`PING`、`PONG`、`BYE`），伺服器 → 客戶端 14 種（`WELCOME`、`LOBBY`、`GAME_START`、`COMMITS`、`TURN`、`GUESS_MADE`、`TURN_RESULT`、`REVEAL_REQUEST`、`REVEALED`、`GAME_OVER`、`CHAT`、`PING`、`PONG`、`ERROR`）。
- **公平性**：開局時每人只送出承諾值 `SHA256(secret + ":" + salt)`，伺服器始終不知道任何人的秘密；揭示時驗證承諾值，並逐筆重算該玩家所有回報，說謊者判定 `INCONSISTENT` 並取消資格。
- 每局的所有收送訊息都寫入 `logs/gameNNN_<game_id>.jsonl`。

完整的訊息欄位、錯誤碼、流程圖、安全性設計與 AI 實驗數據請見 [`bc_game/README.md`](bc_game/README.md)。

## 執行方式

需求：Python 3.10 以上（Windows、macOS、Linux 皆可）。以下指令在 `bc_game/` 資料夾中執行。

```bash
cd bc_game

# 1) 啟動伺服器：每局 3 人，其中 1 個由伺服器自動啟動的 AI
python bc.py server --players 3 --bots 1

# 2) 另開終端機，以人類玩家身分加入
python bc.py client --host 127.0.0.1 --name 小明 --team G07

# 3) 再開一個 AI 代理人加入（人數到齊就自動開局）
python bc.py ai --host 127.0.0.1 --name G07-AI --team G07
```

AI 的 LLM 模式（預設開啟，沒有金鑰就自動改用演算法）：

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...        # Windows PowerShell：$env:ANTHROPIC_API_KEY="sk-ant-..."
python bc.py server --port 6000 --players 2 --guess-timeout 60
python bc.py ai --port 6000 --name LLM-AI                         # 用 LLM 喊數
python bc.py ai --port 6000 --name ALGO-AI --llm off              # 只用演算法
```

跑一場純 AI 對戰（4 個 AI、連續 5 局）：

```bash
python bc.py server --players 4 --bots 4 --games 5
```

常用的伺服器選項：`--port`（預設 5040）、`--players`（2–6）、`--bots`、`--games`（`0` 為不限）、`--guess-timeout`、`--max-turns`、`--log-dir`。伺服器執行中輸入 `start` 可在至少 2 人時提前開局。

人類客戶端輪到自己時直接輸入 4 位數字喊數，其他指令有 `/chat 訊息`、`/status`、`/history`、`/hint`（AI 建議）、`/secret 1234`、`/quit`、`/help`。

跨電腦連線時，伺服器預設監聽 `0.0.0.0:5040`，其他電腦以 `--host 伺服器IP` 連線即可。

## 測試方式

**1. 課程提供的協定自我測試**（在專案根目錄執行，約 3 分鐘）：

```bash
python bc_selftest.py --server "python bc_game/bc.py server"
# 加 --skip-bots 可略過最花時間的純 AI 對局
```

結果：48 項全數通過（含 2–6 人純 AI 對局各 10 局）。

**2. 自寫的黑箱測試 `bc_test.py`**（在 `bc_game/` 執行，約 10 秒）：

```bash
cd bc_game
python bc_test.py --server "python bc.py server" --ai "python bc.py ai" --client "python bc.py client"
python bc_test.py --server "python bc.py server"   # 只測伺服器
```

結果：72 項全數通過。`bc_test.py` 不匯入 `bc.py`，只透過 TCP 以 BC/1.0 協定溝通，因此也能拿來測試其他組的程式。涵蓋範圍包括訊框與 JSON 解析、HELLO 與大廳、對局欄位驗證、完整兩人與三人局、回合上限終局、連續多局，以及 AI 代理人與人類客戶端的行為。

## 期中簡報

[`slides/`](slides/) 資料夾內有期中簡報（10 頁），提供 PDF、PowerPoint 與 HTML 三種格式。

## 附註

課程規範 PDF 原檔下載不完整，少數規範文字無法辨識的細節採合理預設，並以 `bc_selftest.py` 驗證；詳見 [`bc_game/README.md`](bc_game/README.md) 第 11 節。
