# 多人幾A幾B網路對戰系統（BC/1.0）程式說明

本資料夾是「網路應用軟體設計期末專題」的基本實作：一個 Python 檔 `bc.py` 同時包含 **伺服器**、**人類玩家客戶端**、**AI 代理人** 三種角色，全部依照規範第 8–16 節的 **BC/1.0 應用層通訊協定** 實作，只使用 Python 3.10 以上的標準函式庫。

| 檔案 | 說明 |
|---|---|
| `bc.py` | 主程式（server / client / ai 三種模式） |
| `bc_test.py` | 獨立黑箱測試程式（不匯入 `bc.py`，只透過 TCP 測試伺服器、AI、人類客戶端） |
| `requirements.txt` | 套件清單（本專題不需要第三方套件，檔案只有註解） |
| `README.md` | 本說明文件 |

**驗證結果：** 以課程提供的 `bc_selftest.py` 執行全部 48 項檢查，**48 項全數通過**（含 2–6 人純 AI 對局各 10 局）；自寫的 `bc_test.py` 72 項也全數通過。

---

## 1. 快速開始

```bash
# 1) 啟動伺服器：每局 3 人，其中 1 個由伺服器自動啟動的 AI
python bc.py server --players 3 --bots 1

# 2) 另開終端機，用人類客戶端加入
python bc.py client --host 127.0.0.1 --name 小明 --team G07

# 3) 再開一個 AI 代理人加入（人數到齊就自動開局）
python bc.py ai --host 127.0.0.1 --name G07-AI --team G07
```

跑一場純 AI 對戰（4 個 AI、連續 5 局）：

```bash
python bc.py server --players 4 --bots 4 --games 5
```

協定自我測試（約 3 分鐘）：

```bash
python bc_selftest.py --server "python bc_game/bc.py server"
# 加 --skip-bots 可略過最花時間的純 AI 對局
```

### 1.1 命令列選項（規範 5.2 節）

**伺服器** `python bc.py server [選項]`

| 選項 | 預設 | 說明 |
|---|---|---|
| `--host` | `0.0.0.0` | 監聽位址 |
| `--port` | `5040` | TCP 埠號 |
| `--players` | `2` | 每局人數 N（2–6，含 AI）；人數到齊即開局 |
| `--bots` | `0` | 伺服器自動啟動的 AI 數量 K（0 ≤ K ≤ N），名稱為 `BOT-1`…`BOT-K` |
| `--guess-timeout` | `60` | 喊數時限（秒，可用小數如 1.5） |
| `--feedback-timeout` | `60` | 回報時限（秒） |
| `--commit-timeout` | `30` | 承諾時限（秒） |
| `--reveal-timeout` | `15` | 揭示時限（秒） |
| `--max-turns` | `120` | 一局最多回合數 |
| `--max-skips` | `3` | 連續幾次逾時未喊數就判定棄權 |
| `--games` | `1` | 連續進行幾局，`0` 表示不限 |
| `--log-dir` | `logs` | 日誌目錄（每局一個 JSON Lines 檔） |

伺服器執行中可在終端機輸入 `start`，在人數未滿但至少 2 人時提前開局。

**人類客戶端** `python bc.py client --host HOST [--port 5040] --name NAME [--team G00] [--secret 1234]`

**AI 代理人** `python bc.py ai --host HOST [--port 5040] [--name NAME] [--team G00] [--quiet] [--games 1]`

`--games N` 讓 AI 每局結束後自動重新連線、連續參加 N 局（`0` 表示不限），適合跨組對戰或 50 局測試；遇到 `GAME_IN_PROGRESS` 或伺服器暫時連不上會每秒重試。

### 1.2 人類客戶端操作

| 輸入 | 作用 |
|---|---|
| `0123`（4 個不同數字） | 輪到自己時喊數（同一回合只會送出一次） |
| `/secret 5079` | 開局前設定自己的秘密（也可用 `--secret` 指定，未設定則以 `secrets` 隨機產生） |
| `/chat 訊息` | 聊天（由伺服器轉播給所有人） |
| `/status` | 列出每位玩家是否存活，以及依公開結果還剩幾種可能（含自己） |
| `/history` | 列出所有回合的喊數與回報 |
| `/hint` | 用 AI 的演算法建議一個喊數 |
| `/quit` | 送出 `BYE` 離開（Ctrl+C 也會先送 `BYE`） |
| `/help` | 顯示說明 |

客戶端對照規範第 5 節的設計：

- **網路與鍵盤分開**：背景執行緒負責收訊，主執行緒讀鍵盤，所以停在 `input()` 時仍會即時回 `PONG`、顯示 `TURN`。收到格式怪異的訊息只會顯示提示，收訊執行緒不會停掉。
- **秘密只在本機**：秘密與 16 位元組鹽值只存在記憶體，送出的是 SHA-256 承諾值。
- **自動且誠實的回報**：別人喊數時，客戶端依自己的秘密**自動計算**幾A幾B並回報；收到 `REVEAL_REQUEST` 時自動揭示。
- **好讀的對局畫面**：每則 `TURN_RESULT` 都會顯示各玩家的結果與「還剩幾種可能」；公開資訊顯示自己的秘密只剩 3 種以內時會提醒。
- **不必相信伺服器**：別人揭示時，客戶端會自行驗算承諾值與他所有的回報，顯示「本機驗證相符／不符」。
- **版本檢查**：`WELCOME.protocol` 主版本不是 1 時會警告。

---

## 2. 遊戲規則摘要

- 每位玩家各自持有一個 **4 位、數字互不重複** 的秘密數字（共 10×9×8×7 = 5040 種）。
- 依伺服器隨機排定的順序輪流喊數；**一次喊數同時對所有其他存活玩家有效**，每位被猜者回報自己的 `xAyB`。
- 被回報 `4A0B` 的玩家「被猜中」出局；最後只剩 1 人（或到達 `max_turns`）時終局。
- 名次：存活者 > 被猜中者（越晚出局越前面）> 棄權者；被取消資格（DQ）者沒有名次。

---

## 3. 通訊協定實作重點

### 3.1 訊框與格式（規範第 8 節）

- 傳輸層為 TCP，一則訊息 = **一行 UTF-8 JSON 物件，以 LF 結尾**（JSON Lines）。
- 送出時使用 `json.dumps(..., ensure_ascii=False, allow_nan=False)`；接收時容許行尾 CR，拒絕 `NaN`／`Infinity`。
- 單行（不含 LF）超過 **4096 位元組** → `LINE_TOO_LONG`（致命）。
- 伺服器自行把收到的位元組累積在緩衝區，以 LF 切行，**不會**因為 TCP 分段或多則訊息黏在一起而解析錯誤。
- 未知欄位一律忽略；`turn_id`、`a`、`b` 必須是整數且**不可是布林值**（`isinstance(True, int)` 陷阱已處理）。

### 3.2 訊息一覽

客戶端 → 伺服器（9 種）

| 類型 | 主要欄位 | 用途 |
|---|---|---|
| `HELLO` | `protocol`, `name`, `team`, `kind`, (`software`) | 連線後 10 秒內必送，第一則訊息 |
| `COMMIT` | `commit` | 送出秘密的 SHA-256 承諾值 |
| `GUESS` | `turn_id`, `guess` | 喊數者喊數 |
| `FEEDBACK` | `turn_id`, `a`, `b` | 被猜者回報 xAyB |
| `REVEAL` | `secret`, `salt` | 揭示秘密與鹽值 |
| `CHAT` | `text`（1–200 字元） | 聊天 |
| `PING` / `PONG` | (`nonce`) | 連線檢測 |
| `BYE` | — | 主動離開 |

伺服器 → 客戶端（14 種）

| 類型 | 主要欄位 | 時機 |
|---|---|---|
| `WELCOME` | `protocol`, `player_id`, `server` | HELLO 成功 |
| `LOBBY` | `players`, `min_players`, `max_players` | 大廳成員變動，廣播 |
| `GAME_START` | `game_id`, `players`, `order`, `rules` | 開局，廣播 |
| `COMMITS` | `order`, `commits` | 承諾階段結束，廣播所有承諾值 |
| `TURN` | `turn_id`, `guesser`, `alive` | 每回合開始 |
| `GUESS_MADE` | `turn_id`, `guesser`, `guess`, `responders` | 喊數後廣播 |
| `TURN_RESULT` | `turn_id`, `guesser`, `guess`, `skipped`, `results`, `eliminated`, `forfeited`, `disqualified` | 回合結束 |
| `REVEAL_REQUEST` | `reason`（`ELIMINATED` / `GAME_END`） | 要求揭示 |
| `REVEALED` | `player`, `secret`, `salt`, `verdict`, `bad_turns` | 揭示驗證結果，廣播 |
| `GAME_OVER` | `game_id`, `reason`, `turns`, `ranking` | 局結束 |
| `CHAT` | `from`, `text` | 轉播聊天 |
| `PING` / `PONG` | (`nonce`) | 連線檢測 |
| `ERROR` | `code`, `message`, `fatal` | 錯誤；`fatal: true` 後伺服器關閉連線 |

### 3.3 錯誤碼

| 錯誤碼 | 情況 | 致命 |
|---|---|---|
| `BAD_JSON` | 無法解析為 JSON 物件或缺少 `type` | 否 |
| `LINE_TOO_LONG` | 單行超過 4096 位元組 | 是 |
| `UNKNOWN_TYPE` | 未知的訊息類型 | 否 |
| `BAD_FIELD` | 欄位缺少或型別錯誤 | HELLO 時是 |
| `INVALID_VALUE` | 型別對但值不合法（例如 `1123`、`3A1B`） | HELLO 時是 |
| `UNEXPECTED` | 現在不該送這種訊息 | HELLO 前是 |
| `NOT_YOUR_TURN` / `STALE_TURN` / `DUPLICATE` | 不是你喊數／turn_id 不符／重複送出 | 否 |
| `VERSION_UNSUPPORTED` | 協定主版本不是 1 | 是 |
| `GAME_IN_PROGRESS` / `ROOM_FULL` / `NAME_TAKEN` | 已開局／大廳已滿／名稱重複 | 是 |
| `TIMEOUT` | 10 秒未送 HELLO、承諾逾時 | 是 |
| `TOO_MANY_ERRORS` | 單一連線錯誤累計超過 20 次 | 是 |

---

## 4. 一局的流程（規範第 10、12 節）

```
LOBBY 大廳 ──人數到齊──▶ COMMIT 承諾 ──▶ PLAY 對局（回合循環）──▶ END 終局揭示 ──▶ OVER 結束
                           │ 承諾後不足 2 人
                           └───────────────────────────────────────────────▶ GAME_OVER(ABORTED)

PLAY 每回合：TURN → GUESS → GUESS_MADE → 各回報者 FEEDBACK → TURN_RESULT → (有人被猜中) 揭示驗證
```

1. **大廳**：收 HELLO，分配 `p1`、`p2`…，每次成員變動廣播 `LOBBY`。
2. **承諾**：廣播 `GAME_START`（含隨機順序與 `rules`），等待每人送 `COMMIT`；逾時者收到 `ERROR TIMEOUT` 並移出本局。不足 2 人則 `GAME_OVER(ABORTED)`，否則廣播 `COMMITS`。
3. **對局**：依順序輪流。喊數逾時 → 本回合跳過（`skipped: true`），連續 `max_skips` 次則棄權；回報逾時或斷線 → 該回合列入 `forfeited`。
4. **揭示驗證**：有人被猜中（4A0B）就立刻送 `REVEAL_REQUEST(ELIMINATED)` 並驗證；終局時所有存活者送 `REVEAL_REQUEST(GAME_END)`。
5. **結束**：廣播 `GAME_OVER` 與排名，關閉本局所有連線；若 `--games` 大於 1 就開下一局。

---

## 5. 公平性：承諾–揭示與一致性檢查（規範第 3、14 節）

**承諾–揭示（commit–reveal）**：客戶端產生秘密 `s` 與 16 位元組隨機鹽值 `r`（`secrets.token_hex(16)`，32 位小寫十六進位），開局時只送出

```
commit = SHA256(s + ":" + r)   # 64 位小寫十六進位
```

伺服器始終不知道任何人的秘密，揭示時再驗證 `SHA256(s:r) == commit`，否則判定 `BAD_COMMIT`。

**公開一致性檢查**：伺服器為每位玩家 X 維護候選集合 `C(X)`（初始 5040 個），每次 X 回報 `(g, a, b)` 就只留下 `score(g, s) == (a, b)` 的 `s`。

- `C(X)` 變成空集合 → 不可能有任何秘密能產生這些回報，當回合在 `TURN_RESULT.disqualified` 中取消資格。
- 揭示後逐筆重算 X 的所有回報，不符的回合列入 `bad_turns`，判定 `INCONSISTENT`。
- 揭示判定：`OK`、`BAD_COMMIT`、`INCONSISTENT`、`NO_REVEAL`（逾時未揭示）；非 `OK` 者一律 DQ、沒有名次。

**排名**直接採用規範中的 `compute_ranking()`：存活者第 1 類、被猜中者第 2 類（`out_turn` 越大越前）、棄權者第 3 類，同分同名次，DQ 的 `rank` 為 `null`。

---

## 6. 程式架構

| 類別／函式 | 角色 |
|---|---|
| `score()`, `make_commit()`, `encode()`, `decode()` | 共用工具：計算幾A幾B、承諾值、訊息編解碼 |
| `Server` | asyncio 伺服器：連線管理、訊息驗證、一局流程、日誌 |
| `Conn` / `Player` | 一條 TCP 連線／一位玩家的遊戲狀態（承諾值、候選集合、回報紀錄） |
| `Link` | 客戶端連線（`socket.makefile(encoding="utf-8")` 逐行讀取） |
| `AI` | AI 代理人 |
| `Human` | 人類客戶端：**背景執行緒收網路訊息、主執行緒讀鍵盤**，`input()` 不會卡住收訊 |

**伺服器併發設計**：使用 `asyncio.start_server`，每條連線一個協程負責讀取；遊戲流程是另一個協程，用 `asyncio.Event` 搭配逾時等待「所有人都送了／時間到」。訊息處理函式本身是同步的，因此同一個 TCP 封包裡的多則訊息會依序處理，不會互相搶跑（例如連送兩個 FEEDBACK 一定回 `DUPLICATE`）。一個慢速或惡意的連線只會影響自己，不會擋住其他玩家。

**AI 子行程**：`--bots K` 時伺服器每局用 `sys.executable bc.py ai --name BOT-i --quiet` 啟動 K 個子行程，監聽 `0.0.0.0` 時 AI 連 `127.0.0.1`。

### 6.1 伺服器的安全與健壯性（規範第 16 節）

| 威脅或常見錯誤 | 處理方式 |
|---|---|
| 惡意輸入 | 只用 `json.loads()` 解析，不用 `eval`、`exec`、`pickle`；過深的巢狀（如 4000 個 `[`）回 `BAD_JSON`，不會讓伺服器當掉 |
| HELLO 前送其他訊息（含未知類型） | 回 `UNEXPECTED` 並斷線 |
| 單一連線出現程式例外 | 只關閉該連線，對局照常進行 |
| 聊天洗版 | 每條連線 3 秒內最多 5 則 `CHAT`，超過回 `INVALID_VALUE`，累計超過 20 次錯誤就斷線 |
| 對方不讀資料 | 只有該連線被卡住；待送資料超過 1 MiB 就強制斷線 |
| 大量閒置連線 | 同時最多 64 條連線，HELLO 前 10 秒逾時 |
| 喊數順序被預測 | 用 `secrets.SystemRandom()` 洗牌 |
| Windows 主控台編碼（cp950） | 啟動時把 stdout 設成 UTF-8，日誌以 `encoding="utf-8"` 開檔 |
| 埠號已被占用 | 顯示中文錯誤訊息後結束 |

壓力測試：在 4 人對局進行中同時開 80 條連線、送過深的 JSON、在 HELLO 前送未知類型，並讓一位玩家連發 30 則聊天與 2000 個 3 KB 的 PING。結果伺服器各回正確錯誤，洗版者在第 21 次錯誤時被斷線，其餘 3 人正常打完（`LAST_PLAYER`）。另一個測試讓客戶端完全不讀資料，伺服器仍能立即接受新玩家。

---

## 7. AI 策略

### 7.1 演算法

1. **利用所有公開資訊**：對每位對手維護候選集合（初始 5040 個）。所有人的喊數結果都是公開的，所以每則 `TURN_RESULT` 都會縮小「每一位回報者」的集合，不只是自己的喊數。AI 也同步計算「別人眼中我的候選集合」。
2. **選擇目標**：鎖定候選最少的存活對手；只剩 1–2 個候選就直接喊。
3. **開局表**：對手只被喊過一次時，查一張離線窮舉算出的「第二手」表（以 `0123` 為基準，14 種結果各一手，用數字置換套用到任何第一手）。
4. **限時 most-parts 搜尋**（Kooi 2005）：先評估目標的候選，再評估非候選，挑讓目標分成最多組的數字；同分時偏好候選（有機會直接 4A）、再偏好最大組較小者。時間預算為 `guess_timeout × 0.3`（0.15–2 秒），所以 1.5 秒時限下也不會逾時。
5. **危險模式**：若公開資訊顯示自己的秘密只剩 ≤ 2 種可能，下一位對手很可能猜中我，此時只在候選中挑選，爭取這一手就猜中對方。
6. **誠實與自我驗證**：被問時一律依自己的秘密誠實回報；收到別人的 `REVEALED` 會自己重算承諾值與該玩家所有回報，與伺服器判定不一致時印出警告。
7. **健壯性**：欄位缺少或型別錯誤的訊息會被略過而不會當掉；`PING` 一律回 `PONG`；計算用 `a*5+b` 與數字位元遮罩的快速版 `fast_score()`（約快 3 倍）。

### 7.2 實驗結果

離線模擬（同一組 300 個隨機秘密，單一對手，計算猜中所需次數）：

| 策略 | 平均次數 | 最多 |
|---|---|---|
| 第一版（只在候選中抽樣 150 個評估） | 5.36 | 8 |
| **目前版本**（開局表＋候選與非候選限時搜尋） | **5.24** | 7 |

理論最佳約為 5.21 次（Tanaka），目前版本已很接近。

實際透過網路對戰（`bc.py server`，順序由伺服器隨機決定）：

| 實驗 | 局數 | 結果 |
|---|---|---|
| 2 人：目前版本 vs 第一版 | 100 | **目前版本勝 62 局**（先手方勝 63 局），平均 9.0 回合 |
| 4 人：2 個目前版本 vs 2 個第一版 | 60 | 平均正規化名次 0.51 vs 0.49，沒有顯著差異 |
| 6 人純 AI，所有時限 1.5 秒 | 30 | 30 局皆 `LAST_PLAYER`，0 次逾時跳過、0 次棄權、0 則 ERROR |
| `bc_selftest.py` 純 AI 對局（2–6 人各 10 局） | 50 | 全數正常結束，揭示驗證全為 OK |

多人局中策略差異不明顯，原因是所有喊數結果都公開，每位 AI 掌握的資訊幾乎相同，名次主要取決於喊數順序與運氣；我們另外模擬了「最大化對所有對手總資訊量」的策略，兩次 4 人模擬結果方向相反，沒有穩定優勢，因此維持目前做法。2 人局則能明確看出搜尋品質帶來的勝率提升。

重現實驗：

```bash
# 2 人對戰 100 局（另開兩個終端機分別執行兩個 AI）
python bc.py server --players 2 --games 100 --log-dir duel
python bc.py ai --name A --games 100 --quiet
python bc.py ai --name B --games 100 --quiet
```

## 8. 日誌（規範 5.6 節）

每局一個檔案 `logs/gameNNN_<game_id>.jsonl`，每送出或收到一則訊息就寫一行並立即 flush：

```json
{"ts": 1792087882.057, "dir": "recv", "peer": "127.0.0.1:57246", "msg": {"type": "HELLO", "protocol": "BC/1.0", "name": "BOT-1", "team": "BOT", "kind": "ai"}}
{"ts": 1792087882.057, "dir": "send", "peer": "p1", "msg": {"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1", "server": "BC-basic/1.0"}}
```

- `ts`：Unix 時間（秒，含小數）；`dir`：`send` 或 `recv`；`peer`：玩家編號（HELLO 前為 `IP:埠號`）。
- 廣播訊息會對每位收件者各記一筆。無法解析的行記成 `{"raw": "..."}`。

---

## 9. 跨網路連線

伺服器預設監聽 `0.0.0.0:5040`，同一區網內其他電腦用 `--host 伺服器IP` 即可連線。可先測試埠號是否打得通：

```bash
nc -vz 伺服器IP 5040                               # macOS / Linux
Test-NetConnection 伺服器IP -Port 5040             # Windows PowerShell
```

---

## 10. 獨立測試程式 `bc_test.py`

`bc_selftest.py` 只測伺服器。`bc_test.py` 補上它沒涵蓋的部分，並且**不匯入 `bc.py`**，只透過 TCP 用 BC/1.0 協定溝通，所以也能拿來測其他組的程式。

```bash
python bc_test.py --server "python bc.py server" --ai "python bc.py ai" --client "python bc.py client"
python bc_test.py --server "python bc.py server"          # 只測伺服器
# 其他選項：--port 5800（起始埠號）、--ai-games 10（AI 對局數）、--json 結果.json
```

全部約 10 秒，共 72 項：

| 節 | 測什麼 | 項數 |
|---|---|---|
| S1 訊框與 JSON | 訊息分段（切在 UTF-8 字元中間）、兩則黏在同一封包、CRLF、NaN、陣列、缺 type、過深巢狀、重複 HELLO | 10 |
| S2 HELLO 與大廳 | 名稱長度／控制字元、kind、空白組別、型別錯誤、次版本相容、LOBBY 廣播、player_id 編號、聊天轉播與限制、BYE、第 21 次錯誤斷線 | 15 |
| S3 對局欄位值 | 大寫承諾值、未被要求的 REVEAL、全形數字（`str.isdigit` 陷阱）、缺欄位、5A0B | 6 |
| S4 完整兩人局 | GAME_START／COMMITS／TURN／GUESS_MADE／TURN_RESULT 的欄位與順序、廣播一致、淘汰與揭示、GAME_OVER 與排名、結束後關閉連線、日誌格式 | 15 |
| S5–S7 | 三人局名次、MAX_TURNS 終局與並列第 1、`--games 2` 連續兩局 | 6 |
| A AI 代理人 | 本程式扮演假伺服器打 10 局：HELLO、承諾、回報誠實、揭示、PONG、遇到怪訊息不當掉、1.5 秒時限、平均猜中次數 ≤ 7 | 11 |
| C 人類客戶端 | 用標準輸入模擬鍵盤：自動承諾／回報／揭示、停在輸入時仍回 PONG、喊數帶正確 turn_id、顯示排名 | 9 |

**測試程式本身也經過驗證**：故意把伺服器改壞（排名反序、PONG 不帶 nonce），結果有 4 項失敗被抓出；拿第一版客戶端來測，它遇到缺欄位的訊息後就收不到任何訊息，這個問題也被抓到了。

## 11. 附註

- 專案資料夾中的規範 PDF（`期末專題規範與BC1.0通訊協定.pdf.crdownload`）是**下載未完成**的檔案，部分中文字型遺失。協定的訊息名稱、欄位、錯誤碼、範例對局與排名演算法已從 PDF 中的程式碼字型完整解碼出來；少數規範文字無法辨識的細節（例如 `ABORTED` 時 `ranking` 為空陣列、PING 的 `nonce` 為選填）採合理預設，並以 `bc_selftest.py` 48 項檢查驗證。
- 建議重新下載完整的 PDF，對照第 5.2、11、12 節確認細節。
