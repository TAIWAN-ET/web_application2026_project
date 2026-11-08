#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bc_test.py — BC/1.0 獨立黑箱測試程式（補足 bc_selftest.py 沒有涵蓋的部分）

本程式「不匯入」任何受測程式碼，只透過 TCP 與 BC/1.0 協定溝通，因此可以測試任何一組的實作：

  [S] 伺服器：以腳本化的原始客戶端連上受測伺服器，檢查訊框、欄位驗證、完整對局流程、
      排名、MAX_TURNS、連續多局與日誌格式。
  [A] AI 代理人：本程式扮演「假伺服器」，與受測 AI 打數局，檢查承諾、回報誠實、
      揭示、PING、容錯、時限與解題效率。
  [C] 人類客戶端：同樣用假伺服器，並透過標準輸入代替鍵盤，檢查自動承諾／回報／揭示、
      等待輸入時仍能回 PONG、喊數帶正確 turn_id。

用法：
  python bc_test.py --server "python bc.py server" --ai "python bc.py ai" --client "python bc.py client"
  python bc_test.py --server "python bc.py server"          # 只測伺服器
  選項：--port 5800（起始埠號）、--ai-games 10（AI 對局數）、--json 結果.json

受測程式的命令列介面須符合規範 5.2 節：
  伺服器會再加上 --port P --log-dir D 與其他選項；AI 與客戶端會再加上 --host 127.0.0.1 --port P --name NAME。
只使用 Python 3.10 以上的標準函式庫。
"""
import argparse
import asyncio
import glob
import hashlib
import itertools
import json
import os
import random
import re
import secrets
import shlex
import signal
import socket
import sys
import tempfile
import time

ALL = ["".join(p) for p in itertools.permutations("0123456789", 4)]
POSIX = os.name == "posix"
HEX64 = re.compile(r"[0-9a-f]{64}")


def score(g, s):
    a = sum(x == y for x, y in zip(g, s))
    return a, len(set(g) & set(s)) - a


def commit_of(secret, salt):
    return hashlib.sha256(f"{secret}:{salt}".encode("ascii")).hexdigest()


def valid_number(s):
    return isinstance(s, str) and re.fullmatch(r"[0-9]{4}", s) is not None and len(set(s)) == 4


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def line(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 結果紀錄
# ---------------------------------------------------------------------------
RESULTS = []          # [(section, label, ok, note)]


def check(section, label, cond, note=""):
    ok = bool(cond)
    RESULTS.append((section, label, ok, note))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f"（{note}）" if note else ""), flush=True)
    return ok


async def run_section(section, title, fn, *args):
    print(f"\n{title}", flush=True)
    try:
        await fn(*args)
    except Exception as ex:                                   # noqa: BLE001  測試工具必須能從任何錯誤中恢復
        check(section, f"{title} 全節執行完畢", False, f"{type(ex).__name__}: {ex}"[:200])


# ---------------------------------------------------------------------------
# 原始 TCP 客戶端
# ---------------------------------------------------------------------------
class Raw:
    def __init__(self, port):
        self.port = port
        self.r = self.w = None
        self.inbox, self.pending = [], []
        self.eof = False

    async def connect(self):
        self.r, self.w = await asyncio.open_connection("127.0.0.1", self.port, limit=1 << 20)
        return self

    def send(self, obj):
        try:
            self.w.write(obj if isinstance(obj, bytes) else line(obj))
        except (OSError, RuntimeError, AttributeError):
            pass

    async def _read(self, timeout):
        if self.eof:
            return None
        try:
            raw = await asyncio.wait_for(self.r.readline(), timeout)
        except asyncio.TimeoutError:
            raise
        except (ConnectionError, OSError, ValueError):
            raw = b""
        if not raw:
            self.eof = True
            return None
        try:
            m = json.loads(raw)
        except ValueError:
            m = None
        if not isinstance(m, dict):
            m = {"type": "__BAD_JSON__", "raw": raw[:200].decode("utf-8", "replace")}
        self.inbox.append(m)
        return m

    async def recv(self, timeout=5.0):
        if self.pending:
            return self.pending.pop(0)
        try:
            return await self._read(timeout)
        except asyncio.TimeoutError:
            return None

    async def expect(self, mtype, timeout=5.0):
        for i, m in enumerate(self.pending):
            if m.get("type") == mtype:
                return self.pending.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                m = await self._read(left)
            except asyncio.TimeoutError:
                return None
            if m is None:
                return None
            if m.get("type") == mtype:
                return m
            self.pending.append(m)

    async def closed_within(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not self.eof:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                m = await self._read(left)
            except asyncio.TimeoutError:
                return False
            if m is not None:
                self.pending.append(m)
        return True

    async def hello(self, name, kind="human", team="TEST", protocol="BC/1.0"):
        await self.connect()
        self.send({"type": "HELLO", "protocol": protocol, "name": name, "team": team, "kind": kind})
        return await self.expect("WELCOME")

    def close(self):
        try:
            if self.w:
                self.w.close()
        except Exception:                                    # noqa: BLE001
            pass


def is_err(m, code, fatal=None):
    return isinstance(m, dict) and m.get("type") == "ERROR" and m.get("code") == code \
        and (fatal is None or m.get("fatal") is fatal)


# ---------------------------------------------------------------------------
# 受測程式的啟動與結束
# ---------------------------------------------------------------------------
def split_cmd(cmd):
    if POSIX:
        return shlex.split(cmd)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(cmd, posix=False)]


class Ports:
    def __init__(self, base):
        self.next = base

    def __call__(self):
        while True:
            p, self.next = self.next, self.next + 1
            s = socket.socket()
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
            finally:
                s.close()


async def spawn(cmd, extra, stdin=False):
    kw = {"start_new_session": True} if POSIX else {}
    return await asyncio.create_subprocess_exec(
        *split_cmd(cmd), *extra, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.STDOUT if stdin else asyncio.subprocess.DEVNULL, **kw)


async def kill(proc):
    if proc is None or proc.returncode is not None:
        return
    if POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    await proc.wait()


class Server:
    """async with Server(cmd, port, logdir, [...]) as proc：啟動受測伺服器並等它開始監聽。"""

    def __init__(self, cmd, port, logdir, extra):
        self.cmd, self.port, self.logdir, self.extra = cmd, port, logdir, extra
        self.proc = None

    async def __aenter__(self):
        self.proc = await spawn(self.cmd, ["--port", str(self.port), "--log-dir", self.logdir, *self.extra])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.proc.returncode is None:
            try:
                _, w = await asyncio.open_connection("127.0.0.1", self.port)
                w.close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        return self.proc

    async def __aexit__(self, *exc):
        await kill(self.proc)
        return False


async def wait_exit(proc, timeout):
    try:
        await asyncio.wait_for(proc.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ---------------------------------------------------------------------------
# 腳本玩家：誠實回報，喊數策略由 policy(turn_msg, ctx) 決定
# ---------------------------------------------------------------------------
async def honest(raw, name, secret, policy, ctx=None):
    w = await raw.hello(name)
    if not w:
        raise RuntimeError(f"{name} 未收到 WELCOME")
    me = w["player_id"]
    salt = secrets.token_hex(16)
    ctx = ctx if ctx is not None else {}
    ctx.setdefault("players", {})[me] = secret
    while True:
        m = await raw.recv(40)
        if m is None:
            return me
        t = m.get("type")
        if t == "GAME_START":
            raw.send({"type": "COMMIT", "commit": commit_of(secret, salt)})
        elif t == "TURN" and m.get("guesser") == me:
            raw.send({"type": "GUESS", "turn_id": m["turn_id"], "guess": policy(m, me, ctx)})
        elif t == "GUESS_MADE" and me in m.get("responders", []):
            a, b = score(m["guess"], secret)
            raw.send({"type": "FEEDBACK", "turn_id": m["turn_id"], "a": a, "b": b})
        elif t == "REVEAL_REQUEST":
            raw.send({"type": "REVEAL", "secret": secret, "salt": salt})
        elif t == "GAME_OVER":
            return me


def of_type(raw, t):
    return [m for m in raw.inbox if m.get("type") == t]


# ===========================================================================
# [S] 伺服器測試
# ===========================================================================
async def s_framing(cmd, ports, logdir):
    port = ports()
    async with Server(cmd, port, os.path.join(logdir, "framing"), ["--players", "3"]):
        r = await Raw(port).connect()
        hello = line({"type": "HELLO", "protocol": "BC/1.0", "name": "分段測試", "team": "T", "kind": "human"})
        cut = hello.index("段".encode()) + 1                       # 從 UTF-8 多位元組字元中間切開
        r.send(hello[:cut])
        await asyncio.sleep(0.3)
        r.send(hello[cut:])
        w = await r.expect("WELCOME")
        check("S", "一則訊息分兩段送達（且切在 UTF-8 字元中間）仍正確解析", w is not None)
        r.send(line({"type": "PING", "nonce": "a"}) + line({"type": "PING", "nonce": "b"}))
        p1, p2 = await r.expect("PONG"), await r.expect("PONG")
        check("S", "兩則訊息黏在同一個封包仍依序處理", p1 and p2 and (p1.get("nonce"), p2.get("nonce")) == ("a", "b"))
        r.send(b'{"type":"PING","nonce":"crlf"}\r\n')
        p = await r.expect("PONG")
        check("S", "CRLF 行尾可被接受", p is not None and p.get("nonce") == "crlf")
        r.send({"type": "PING"})
        p = await r.expect("PONG")
        check("S", "PING 不帶 nonce 也回 PONG", p is not None)
        for raw_line, label in ((b'{"type":"PING","nonce":NaN}\n', "含 NaN → BAD_JSON"),
                                (b'[1,2,3]\n', "JSON 陣列（不是物件）→ BAD_JSON"),
                                (b'{"nonce":"x"}\n', "缺少 type → BAD_JSON"),
                                (b'[' * 3000 + b'\n', "過深的巢狀 → BAD_JSON")):
            r.send(raw_line)
            m = await r.expect("ERROR")
            check("S", label, is_err(m, "BAD_JSON", False), m.get("code") if m else "未回應")
        r.send({"type": "HELLO", "protocol": "BC/1.0", "name": "again", "team": "T", "kind": "human"})
        check("S", "重複 HELLO → UNEXPECTED（非致命）", is_err(await r.expect("ERROR"), "UNEXPECTED", False))
        r.send({"type": "PING", "nonce": "still"})
        p = await r.expect("PONG")
        check("S", "非致命錯誤後連線仍可使用", p is not None and p.get("nonce") == "still")
        r.close()


async def s_hello(cmd, ports, logdir):
    port = ports()
    async with Server(cmd, port, os.path.join(logdir, "hello"), ["--players", "4"]):
        cases = [({"name": "x" * 21}, "INVALID_VALUE", "名稱 21 個字元 → INVALID_VALUE"),
                 ({"name": "a\u0007b"}, "INVALID_VALUE", "名稱含控制字元 → INVALID_VALUE"),
                 ({"kind": "robot"}, "INVALID_VALUE", "kind 不是 human／ai → INVALID_VALUE"),
                 ({"team": ""}, "INVALID_VALUE", "空白組別 → INVALID_VALUE"),
                 ({"name": 123}, "BAD_FIELD", "name 不是字串 → BAD_FIELD")]
        for patch, code, label in cases:
            r = await Raw(port).connect()
            msg = {"type": "HELLO", "protocol": "BC/1.0", "name": "ok", "team": "T", "kind": "human"}
            msg.update(patch)
            r.send(msg)
            m = await r.recv()
            check("S", label, is_err(m, code, True), m.get("code") if m else "未回應")
            r.close()

        a = Raw(port)
        wa = await a.hello("甲", protocol="BC/1.7")
        check("S", "次版本不同（BC/1.7）仍可加入", wa is not None and wa.get("protocol", "").startswith("BC/1."))
        la = await a.expect("LOBBY")
        check("S", "加入後收到 LOBBY（含 players、min_players、max_players）",
              la is not None and {"players", "min_players", "max_players"} <= set(la)
              and la.get("max_players") == 4 and any(p.get("id") == wa.get("player_id") for p in la["players"]))
        b = Raw(port)
        wb = await b.hello("乙")
        check("S", "player_id 依加入順序編號（p1、p2）", (wa or {}).get("player_id") == "p1" and (wb or {}).get("player_id") == "p2")
        lb = await a.expect("LOBBY")
        check("S", "新玩家加入時廣播 LOBBY 給所有人", lb is not None and len(lb.get("players", [])) == 2)
        b.send({"type": "CHAT", "text": "嗨"})
        c = await a.expect("CHAT")
        check("S", "大廳中 CHAT 會轉播給其他人", c is not None and c.get("from") == "p2" and c.get("text") == "嗨")
        b.send({"type": "CHAT", "text": "x" * 201})
        check("S", "CHAT 超過 200 字 → INVALID_VALUE", is_err(await b.expect("ERROR"), "INVALID_VALUE"))
        b.send({"type": "CHAT", "text": 5})
        check("S", "CHAT text 不是字串 → BAD_FIELD", is_err(await b.expect("ERROR"), "BAD_FIELD"))
        b.send({"type": "BYE"})
        check("S", "BYE 後伺服器關閉連線", await b.closed_within(5))
        lc = await a.expect("LOBBY")
        check("S", "有人離開大廳時廣播 LOBBY", lc is not None and len(lc.get("players", [])) == 1)
        a.close()

        r = await Raw(port).connect()
        for _ in range(21):
            r.send(b"oops\n")
        await asyncio.sleep(1)
        codes = [m.get("code") for m in r.inbox] + [m.get("code") for m in r.pending]
        while True:
            m = await r.recv(2)
            if m is None:
                break
            codes.append(m.get("code"))
        check("S", "同一連線第 21 次錯誤 → TOO_MANY_ERRORS 並斷線",
              "TOO_MANY_ERRORS" in codes and r.eof, f"收到 {codes.count('BAD_JSON')} 次 BAD_JSON")
        r.close()


async def s_values(cmd, ports, logdir):
    """對局中的欄位值檢查：全形數字、大寫十六進位、a 超出範圍、缺欄位、未被要求的 REVEAL。"""
    port = ports()
    async with Server(cmd, port, os.path.join(logdir, "values"),
                      ["--players", "2", "--bots", "1", "--guess-timeout", "5", "--feedback-timeout", "5"]):
        p = Raw(port)
        w = await p.hello("測值")
        me = w["player_id"]
        await p.expect("GAME_START", 15)
        secret, salt = random.choice(ALL), secrets.token_hex(16)
        p.send({"type": "COMMIT", "commit": commit_of(secret, salt).upper()})
        check("S", "承諾值為大寫十六進位 → INVALID_VALUE", is_err(await p.expect("ERROR"), "INVALID_VALUE"))
        p.send({"type": "COMMIT", "commit": commit_of(secret, salt)})
        await p.expect("COMMITS", 10)
        p.send({"type": "REVEAL", "secret": secret, "salt": salt})
        check("S", "未被要求就 REVEAL → UNEXPECTED", is_err(await p.expect("ERROR"), "UNEXPECTED"))
        done_guess = done_fb = False
        while not (done_guess and done_fb):
            m = await p.recv(15)
            if m is None:
                break
            if m.get("type") == "TURN" and m.get("guesser") == me and not done_guess:
                tid = m["turn_id"]
                p.send({"type": "GUESS", "turn_id": tid, "guess": "１２３４"})
                check("S", "全形數字喊數 → INVALID_VALUE（不可用 str.isdigit）",
                      is_err(await p.expect("ERROR"), "INVALID_VALUE"))
                p.send({"type": "GUESS", "turn_id": tid})
                check("S", "GUESS 缺 guess → BAD_FIELD", is_err(await p.expect("ERROR"), "BAD_FIELD"))
                p.send({"type": "GUESS", "turn_id": tid, "guess": "9876" if secret != "9876" else "9875"})
                done_guess = True
            elif m.get("type") == "GUESS_MADE" and me in m.get("responders", []) and not done_fb:
                tid = m["turn_id"]
                p.send({"type": "FEEDBACK", "turn_id": tid, "a": 5, "b": 0})
                check("S", "回報 5A0B → INVALID_VALUE", is_err(await p.expect("ERROR"), "INVALID_VALUE"))
                p.send({"type": "FEEDBACK", "turn_id": tid, "a": 1})
                check("S", "FEEDBACK 缺 b → BAD_FIELD", is_err(await p.expect("ERROR"), "BAD_FIELD"))
                a, b = score(m["guess"], secret)
                p.send({"type": "FEEDBACK", "turn_id": tid, "a": a, "b": b})
                done_fb = True
        p.close()


async def s_flow(cmd, ports, logdir):
    """兩位腳本玩家打完整局，逐項檢查訊息內容與順序。"""
    port = ports()
    d = os.path.join(logdir, "flow")
    async with Server(cmd, port, d, ["--players", "2", "--guess-timeout", "5", "--feedback-timeout", "5"]) as proc:
        ctx = {"count": {}}

        def policy(m, me, ctx):                      # 自己第 2 次喊數時猜中對手
            ctx["count"][me] = ctx["count"].get(me, 0) + 1
            other = [s for p, s in ctx["players"].items() if p != me][0]
            if ctx["count"][me] >= 2:
                return other
            return next(x for x in ALL if score(x, other)[0] < 4 and x not in ctx["players"].values())

        secs = random.sample(ALL, 2)
        ra, rb = Raw(port), Raw(port)
        await asyncio.gather(honest(ra, "甲", secs[0], policy, ctx), honest(rb, "乙", secs[1], policy, ctx))
        closed = await ra.closed_within(5)
        ended = await wait_exit(proc, 10)

    gs = (of_type(ra, "GAME_START") or [{}])[0]
    order = gs.get("order", [])
    check("S", "GAME_START 含 game_id、players、order、rules",
          {"game_id", "players", "order", "rules"} <= set(gs) and sorted(order) == ["p1", "p2"]
          and [p.get("id") for p in gs.get("players", [])] == order)
    cm = (of_type(ra, "COMMITS") or [{}])[0]
    check("S", "COMMITS 的 commits 涵蓋 order 中每一位且為 64 位小寫十六進位",
          set(cm.get("commits", {})) == set(order) and all(HEX64.fullmatch(c) for c in cm.get("commits", {}).values()))
    turns = of_type(ra, "TURN")
    check("S", "turn_id 從 1 開始連續遞增", [t.get("turn_id") for t in turns] == list(range(1, len(turns) + 1)))
    check("S", "喊數者依 order 輪替", all(t.get("guesser") == order[i % len(order)] for i, t in enumerate(turns)))
    gm = of_type(ra, "GUESS_MADE")
    check("S", "GUESS_MADE 的 responders = 存活者扣除喊數者",
          bool(gm) and all(m.get("responders") == [p for p in order if p != m.get("guesser")] for m in gm))
    tr = of_type(ra, "TURN_RESULT")
    need = {"turn_id", "guesser", "guess", "skipped", "results", "eliminated", "forfeited", "disqualified"}
    check("S", "TURN_RESULT 欄位齊全", bool(tr) and all(need <= set(m) for m in tr))
    check("S", "兩位玩家收到完全相同的 TURN_RESULT 序列", tr == of_type(rb, "TURN_RESULT"))
    last = tr[-1] if tr else {}
    check("S", "猜中 4A0B 的玩家列在 eliminated", len(last.get("eliminated", [])) == 1)
    loser = (last.get("eliminated") or [None])[0]
    loser_raw = ra if loser == "p1" else rb
    rq = of_type(loser_raw, "REVEAL_REQUEST")
    check("S", "被猜中者收到 REVEAL_REQUEST（reason ELIMINATED）", bool(rq) and rq[0].get("reason") == "ELIMINATED")
    rv = [m for m in of_type(ra, "REVEALED") if m.get("player") == loser]
    check("S", "誠實玩家的 REVEALED 判定 OK 且 bad_turns 為空",
          bool(rv) and rv[0].get("verdict") == "OK" and rv[0].get("bad_turns") == [])
    over = (of_type(ra, "GAME_OVER") or [{}])[0]
    rk = {r.get("player"): r for r in over.get("ranking", [])}
    winner = [p for p in order if p != loser]
    check("S", "GAME_OVER：reason LAST_PLAYER、game_id 一致、turns 正確",
          over.get("reason") == "LAST_PLAYER" and over.get("game_id") == gs.get("game_id")
          and over.get("turns") == len(turns))
    check("S", "排名：存活者第 1 名（out_turn null），被猜中者第 2 名（out_turn = 該回合）",
          bool(winner) and rk.get(winner[0], {}).get("rank") == 1 and rk[winner[0]].get("out_turn") is None
          and rk.get(loser, {}).get("rank") == 2 and rk[loser].get("out_turn") == last.get("turn_id")
          and rk[winner[0]].get("status") == "SURVIVOR" and rk[loser].get("status") == "GUESSED")
    check("S", "GAME_OVER 之後伺服器關閉連線", closed)
    check("S", "--games 1 打完一局後伺服器自行結束", ended)

    files = sorted(f for f in glob.glob(os.path.join(d, "*")) if os.path.isfile(f))
    good, total = True, 0
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for ln in fh:
                total += 1
                try:
                    r = json.loads(ln)
                except ValueError:
                    good = False
                    continue
                good &= isinstance(r.get("ts"), (int, float)) and r.get("dir") in ("send", "recv") \
                    and isinstance(r.get("peer"), str) and (isinstance(r.get("msg"), dict) or "raw" in r)
    check("S", "日誌：一局一個檔，每行都是含 ts／dir／peer／msg 的 JSON", len(files) == 1 and total > 0 and good,
          f"{len(files)} 個檔、{total} 行")


async def s_ranking(cmd, ports, logdir):
    """三人局：每位喊數者都猜中「順序上的下一位存活者」，驗證出局先後與名次。"""
    port = ports()
    async with Server(cmd, port, os.path.join(logdir, "rank3"), ["--players", "3", "--guess-timeout", "5",
                                                                  "--feedback-timeout", "5"]):
        ctx = {}

        def policy(m, me, ctx):
            alive = m.get("alive", [])
            nxt = alive[(alive.index(me) + 1) % len(alive)]
            return ctx["players"][nxt]

        secs = random.sample(ALL, 3)
        rs = [Raw(port) for _ in range(3)]
        await asyncio.gather(*(honest(r, f"玩家{i}", s, policy, ctx) for i, (r, s) in enumerate(zip(rs, secs))))
    over = (of_type(rs[0], "GAME_OVER") or [{}])[0]
    rows = over.get("ranking", [])
    out = sorted((r for r in rows if r.get("status") == "GUESSED"), key=lambda r: -r.get("out_turn", 0))
    check("S", "三人局：越晚被猜中名次越前（第 2、3 名依 out_turn 遞減）",
          len(rows) == 3 and [r.get("rank") for r in out] == [2, 3]
          and sum(1 for r in rows if r.get("rank") == 1 and r.get("status") == "SURVIVOR") == 1,
          json.dumps(rows, ensure_ascii=False)[:150])


async def s_maxturns(cmd, ports, logdir):
    port = ports()
    async with Server(cmd, port, os.path.join(logdir, "maxturns"),
                      ["--players", "2", "--max-turns", "4", "--guess-timeout", "5", "--feedback-timeout", "5"]):
        secs = random.sample([x for x in ALL if x != "0123"], 2)
        ra, rb = Raw(port), Raw(port)
        await asyncio.gather(honest(ra, "A", secs[0], lambda *_: "0123"), honest(rb, "B", secs[1], lambda *_: "0123"))
    over = (of_type(ra, "GAME_OVER") or [{}])[0]
    check("S", "達 --max-turns 時以 MAX_TURNS 結束且 turns 正確", over.get("reason") == "MAX_TURNS" and over.get("turns") == 4)
    rq = of_type(ra, "REVEAL_REQUEST")
    check("S", "終局時存活者收到 REVEAL_REQUEST（reason GAME_END）", bool(rq) and rq[0].get("reason") == "GAME_END")
    rows = over.get("ranking", [])
    check("S", "兩位存活者同為 SURVIVOR 並列第 1 名",
          len(rows) == 2 and all(r.get("status") == "SURVIVOR" and r.get("rank") == 1 for r in rows))


async def s_games(cmd, ports, logdir):
    port = ports()
    d = os.path.join(logdir, "games2")
    async with Server(cmd, port, d, ["--players", "2", "--bots", "1", "--games", "2",
                                     "--guess-timeout", "5", "--feedback-timeout", "5"]) as proc:
        ids = []
        for i in range(2):
            r = Raw(port)
            for _ in range(50):                    # 第二局大廳開放前可能還連不上
                try:
                    me = await honest(r, f"連續{i}", random.choice(ALL), lambda *_: random.choice(ALL))
                    break
                except (OSError, RuntimeError):
                    await asyncio.sleep(0.2)
                    r = Raw(port)
            ids.append(me)
        ended = await wait_exit(proc, 15)
    files = [f for f in glob.glob(os.path.join(d, "*")) if os.path.isfile(f)]
    check("S", "--games 2：連續兩局後結束，日誌兩個檔", ended and len(files) == 2, f"{len(files)} 個檔")
    check("S", "每一局的 player_id 都重新從 p1 編號", all(x in ("p1", "p2") for x in ids), str(ids))


# ===========================================================================
# 假伺服器：用來測 AI 與人類客戶端
# ===========================================================================
class FakeGame:
    """本程式扮演伺服器，對手 p2 由本程式控制；受測程式固定是 p1。"""

    def __init__(self, port, guess_timeout=3.0, chaos=True):
        self.port, self.guess_timeout, self.chaos = port, guess_timeout, chaos
        self.notes, self.ok = [], {}
        self.conn = asyncio.get_running_loop().create_future()

    async def serve(self):
        async def on_conn(r, w):
            if not self.conn.done():
                self.conn.set_result((r, w))
        self.srv = await asyncio.start_server(on_conn, "127.0.0.1", self.port)

    async def read(self, r, timeout):
        while True:
            raw = await asyncio.wait_for(r.readline(), timeout)
            if not raw:
                raise ConnectionError("受測程式關閉了連線")
            m = json.loads(raw)
            if m.get("type") == "PONG":
                self.ok["pong"] = m.get("nonce") == self.ok.get("ping_nonce")
                continue
            return m

    async def play(self, on_my_turn=None, max_turns=40):
        r, w = await asyncio.wait_for(self.conn, 15)

        def send(m):
            w.write(line(m))

        hello = await self.read(r, 10)
        self.ok["hello"] = hello.get("type") == "HELLO" and hello.get("protocol", "").startswith("BC/1.") \
            and isinstance(hello.get("name"), str) and hello.get("kind") in ("human", "ai")
        self.ok["kind"] = hello.get("kind")
        send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1", "server": "bc_test"})
        send({"type": "LOBBY", "players": [{"id": "p1", "name": hello.get("name"), "team": "T", "kind": "ai"},
                                           {"id": "p2", "name": "對手", "team": "X", "kind": "ai"}],
              "min_players": 2, "max_players": 2})
        if self.chaos:
            send({"type": "FUTURE_MESSAGE", "x": 1})                         # 未知類型應被忽略
            send({"type": "TURN"})                                           # 缺欄位的訊息也不能讓程式當掉
            send({"type": "CHAT"})
        rules = {"guess_timeout": self.guess_timeout, "feedback_timeout": 5, "commit_timeout": 5,
                 "reveal_timeout": 5, "max_turns": 120, "max_skips": 3}
        send({"type": "GAME_START", "game_id": "fake", "players": [], "order": ["p2", "p1"], "rules": rules,
              "extra_field": "ignore me"})
        cm = await self.read(r, 5)
        commit = cm.get("commit", "")
        self.ok["commit"] = cm.get("type") == "COMMIT" and bool(HEX64.fullmatch(commit))
        opp = random.choice(ALL)
        opp_salt = secrets.token_hex(16)
        send({"type": "COMMITS", "order": ["p2", "p1"], "commits": {"p1": commit, "p2": commit_of(opp, opp_salt)}})
        records, my_guesses, guess_times = [], 0, []
        solved = eliminated = False
        for tid in range(1, max_turns + 1):
            guesser = "p2" if tid % 2 else "p1"
            send({"type": "TURN", "turn_id": tid, "guesser": guesser, "alive": ["p2", "p1"]})
            if guesser == "p2":
                g = random.choice(ALL)
                send({"type": "GUESS_MADE", "turn_id": tid, "guesser": "p2", "guess": g, "responders": ["p1"]})
                if self.chaos and tid == 3:
                    self.ok["ping_nonce"] = "n-" + secrets.token_hex(3)
                    send({"type": "PING", "nonce": self.ok["ping_nonce"]})
                    send({"type": "ERROR", "code": "STALE_TURN", "message": "測試用非致命錯誤", "fatal": False})
                fb = await self.read(r, 5)
                good = fb.get("type") == "FEEDBACK" and fb.get("turn_id") == tid and is_int(fb.get("a")) \
                    and is_int(fb.get("b"))
                self.ok.setdefault("feedback_format", True)
                self.ok["feedback_format"] &= good
                a, b = fb.get("a"), fb.get("b")
                records.append((g, a, b))
                send({"type": "TURN_RESULT", "turn_id": tid, "guesser": "p2", "guess": g, "skipped": False,
                      "results": [{"player": "p1", "a": a, "b": b}], "eliminated": ["p1"] if a == 4 else [],
                      "forfeited": [], "disqualified": []})
                if a == 4:
                    eliminated = True
                    break
            else:
                if on_my_turn:
                    await on_my_turn(tid)
                t0 = time.monotonic()
                gm = await self.read(r, self.guess_timeout + 5)
                guess_times.append(time.monotonic() - t0)
                good = gm.get("type") == "GUESS" and gm.get("turn_id") == tid and valid_number(gm.get("guess"))
                self.ok.setdefault("guess_format", True)
                self.ok["guess_format"] &= good
                g = gm.get("guess")
                my_guesses += 1
                a, b = score(g, opp)
                send({"type": "GUESS_MADE", "turn_id": tid, "guesser": "p1", "guess": g, "responders": ["p2"]})
                send({"type": "TURN_RESULT", "turn_id": tid, "guesser": "p1", "guess": g, "skipped": False,
                      "results": [{"player": "p2", "a": a, "b": b}], "eliminated": ["p2"] if a == 4 else [],
                      "forfeited": [], "disqualified": []})
                if a == 4:
                    solved = True
                    send({"type": "REVEALED", "player": "p2", "secret": opp, "salt": opp_salt, "verdict": "OK",
                          "bad_turns": []})
                    break
        send({"type": "REVEAL_REQUEST", "reason": "ELIMINATED" if eliminated else "GAME_END"})
        rv = await self.read(r, 5)
        sec, salt = rv.get("secret"), rv.get("salt")
        self.ok["reveal"] = rv.get("type") == "REVEAL" and valid_number(sec) and isinstance(salt, str) \
            and commit_of(sec, salt) == commit
        self.ok["honest"] = bool(self.ok["reveal"]) and all(score(g, sec) == (a, b) for g, a, b in records)
        send({"type": "GAME_OVER", "game_id": "fake", "reason": "LAST_PLAYER", "turns": tid,
              "ranking": [{"player": "p1", "status": "SURVIVOR" if not eliminated else "GUESSED",
                           "out_turn": None if not eliminated else tid, "rank": 1 if not eliminated else 2},
                          {"player": "p2", "status": "GUESSED" if solved else "SURVIVOR", "out_turn": tid,
                           "rank": 2 if solved else 1}]})
        await w.drain()
        w.close()
        self.srv.close()
        return {"solved": solved, "guesses": my_guesses, "times": guess_times, "eliminated": eliminated}


async def a_tests(cmd, ports, games):
    stats = []
    oks = {k: True for k in ("hello", "commit", "feedback_format", "guess_format", "reveal", "honest")}
    pong = None
    over_time = 0
    for i in range(games):
        port = ports()
        fg = FakeGame(port, guess_timeout=1.5 if i % 2 else 3.0, chaos=(i == 0))
        await fg.serve()
        proc = await spawn(cmd, ["--host", "127.0.0.1", "--port", str(port), "--name", f"AI-{i}"])
        try:
            res = await fg.play()
        finally:
            exited = await wait_exit(proc, 10)
            await kill(proc)
        if i == 0:
            pong = fg.ok.get("pong")
            check("A", "HELLO 格式正確且 kind 為 ai", fg.ok.get("hello") and fg.ok.get("kind") == "ai")
            check("A", "等待輸入或計算時仍回 PONG（nonce 相同）", pong)
            check("A", "收到未知類型、缺欄位、多餘欄位與非致命 ERROR 後仍正常對局", res["solved"] or res["eliminated"])
            check("A", "GAME_OVER 並關閉連線後 AI 自行結束", exited)
        for k in oks:
            oks[k] &= bool(fg.ok.get(k))
        over_time += sum(t > fg.guess_timeout for t in res["times"])
        stats.append(res)
    check("A", "COMMIT 為 64 位小寫十六進位", oks["commit"])
    check("A", "FEEDBACK 帶正確 turn_id，a、b 為整數", oks["feedback_format"])
    check("A", "GUESS 帶正確 turn_id 且為 4 個不同數字", oks["guess_format"])
    check("A", "REVEAL 的秘密與鹽值符合承諾值", oks["reveal"])
    check("A", "所有回報都與揭示的秘密一致（誠實）", oks["honest"])
    times = [t for s in stats for t in s["times"]]
    check("A", "每次喊數都在時限內送出（含 1.5 秒時限的局）", over_time == 0,
          f"最慢 {max(times):.2f} 秒" if times else "")
    solved = [s["guesses"] for s in stats if s["solved"]]
    avg = sum(solved) / len(solved) if solved else 99
    check("A", f"解題效率：{games} 局中猜中對手平均次數 ≤ 7", solved and avg <= 7,
          f"猜中 {len(solved)} 局，平均 {avg:.2f} 次，最多 {max(solved) if solved else '-'} 次")


async def c_tests(cmd, ports):
    port = ports()
    fg = FakeGame(port, guess_timeout=10.0, chaos=True)
    await fg.serve()
    proc = await spawn(cmd, ["--host", "127.0.0.1", "--port", str(port), "--name", "測試員"], stdin=True)
    output = []

    async def drain():
        while True:
            ln = await proc.stdout.readline()
            if not ln:
                return
            output.append(ln.decode("utf-8", "replace"))
    reader = asyncio.create_task(drain())
    typed = []

    async def my_turn(tid):
        await asyncio.sleep(0.5)                  # 讓客戶端停在 input()，再由「鍵盤」輸入
        g = random.choice(ALL)
        typed.append(g)
        proc.stdin.write((g + "\n").encode())
        await proc.stdin.drain()

    try:
        res = await fg.play(on_my_turn=my_turn, max_turns=8)
    finally:
        await asyncio.sleep(0.5)
        try:
            proc.stdin.write(b"\n")
            await proc.stdin.drain()
        except (OSError, ConnectionError):
            pass
        exited = await wait_exit(proc, 10)
        await kill(proc)
        reader.cancel()
    check("C", "HELLO 格式正確且 kind 為 human", fg.ok.get("hello") and fg.ok.get("kind") == "human")
    check("C", "開局自動送出 64 位小寫十六進位承諾值", fg.ok.get("commit"))
    check("C", "收到未知類型與缺欄位的訊息後，停在鍵盤輸入時仍立即回 PONG", fg.ok.get("pong"))
    check("C", "鍵盤輸入的數字以正確 turn_id 送出 GUESS", fg.ok.get("guess_format") and res["guesses"] == len(typed) > 0)
    check("C", "被喊數時自動回報且格式正確", fg.ok.get("feedback_format"))
    check("C", "自動揭示，秘密與鹽值符合承諾值", fg.ok.get("reveal"))
    check("C", "所有自動回報都與揭示的秘密一致", fg.ok.get("honest"))
    check("C", "連線關閉後按 Enter 即可結束程式", exited)
    check("C", "畫面有顯示最終排名", any("名" in x or "rank" in x.lower() for x in output[-10:]))


# ---------------------------------------------------------------------------
async def main():
    ap = argparse.ArgumentParser(description="BC/1.0 獨立黑箱測試程式")
    ap.add_argument("--server", help="啟動受測伺服器的命令，例如 \"python bc.py server\"")
    ap.add_argument("--ai", help="啟動受測 AI 的命令，例如 \"python bc.py ai\"")
    ap.add_argument("--client", help="啟動受測人類客戶端的命令，例如 \"python bc.py client\"")
    ap.add_argument("--port", type=int, default=5800, help="起始埠號（預設 5800）")
    ap.add_argument("--ai-games", type=int, default=10, help="AI 與假伺服器對打的局數（預設 10）")
    ap.add_argument("--json", help="把逐項結果另存為 JSON 檔")
    a = ap.parse_args()
    if not (a.server or a.ai or a.client):
        ap.error("至少要指定 --server、--ai、--client 其中一項")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ports = Ports(a.port)
    logdir = tempfile.mkdtemp(prefix="bc-test-")
    t0 = time.monotonic()
    if a.server:
        for title, fn in (("[S1] 訊框與 JSON 處理", s_framing), ("[S2] HELLO、大廳、聊天與錯誤累計", s_hello),
                          ("[S3] 對局中的欄位值", s_values), ("[S4] 完整兩人對局流程與日誌", s_flow),
                          ("[S5] 三人局排名", s_ranking), ("[S6] MAX_TURNS 終局", s_maxturns),
                          ("[S7] 連續多局", s_games)):
            await run_section("S", title, fn, a.server, ports, logdir)
    if a.ai:
        await run_section("A", f"[A] AI 代理人（與假伺服器對打 {a.ai_games} 局）", a_tests, a.ai, ports, a.ai_games)
    if a.client:
        await run_section("C", "[C] 人類客戶端（以標準輸入模擬鍵盤）", c_tests, a.client, ports)

    passed = sum(1 for r in RESULTS if r[2])
    failed = [r for r in RESULTS if not r[2]]
    print(f"\n通過 {passed} 項，失敗 {len(failed)} 項（共 {len(RESULTS)} 項），耗時 {time.monotonic() - t0:.0f} 秒。"
          f"伺服器日誌：{logdir}")
    for s, label, _, note in failed:
        print(f"  失敗：[{s}] {label}" + (f"（{note}）" if note else ""))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"tool": "bc_test", "total": len(RESULTS), "passed": passed,
                       "checks": [{"section": s, "label": l, "ok": ok, "note": n} for s, l, ok, n in RESULTS]},
                      f, ensure_ascii=False, indent=2)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
