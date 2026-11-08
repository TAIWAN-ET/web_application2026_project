"""LLM 模式的測試：在本機開一個假的 OpenAI 相容伺服器（/v1/chat/completions），
不需要網路、金鑰或真的模型，只用標準函式庫。

  python bc_llm_test.py
"""
import http.server
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bc

MODE = {"name": "good"}
REQUESTS = []


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append((self.path, dict(self.headers), body))
        st = json.loads(body["messages"][1]["content"])
        m = MODE["name"]
        content = None
        if m == "good":
            content = json.dumps({"guess": st["options"][0]["guess"], "reason": "最多分組"})
        elif m == "second":
            content = json.dumps({"guess": st["options"][1]["guess"], "reason": "次佳"})
        elif m == "think":
            content = "<think>先想想 {不是答案}</think>\n```json\n" + json.dumps(
                {"guess": st["options"][1]["guess"], "reason": "推理模型"}) + "\n```"
        elif m == "garbage":
            content = "我覺得是 12345"
        elif m == "dup":
            content = '{"guess":"1123","reason":"x"}'
        elif m == "bad_quality":
            content = '{"guess":"0123","reason":"x"}'
        elif m == "slow":
            time.sleep(5)
            content = '{"guess":"0123","reason":"x"}'
        elif m == "no_json_mode":
            if "response_format" in body:
                return self.reply(400, {"error": {"message": "response_format not supported"}})
            content = json.dumps({"guess": st["options"][0]["guess"], "reason": "ok"})
        elif m == "bad_model":
            return self.reply(404, {"error": {"message": "model not found"}})
        elif m == "server_error":
            return self.reply(500, {"error": {"message": "boom"}})
        self.reply(200, {"choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": content}}]})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}/v1"


def make_ai(mode, timeout=60.0, extra=()):
    MODE["name"] = mode
    ai = bc.AI(bc.parse_args(["ai", "--quiet", "--llm", "openai", "--llm-base-url", BASE,
                              "--llm-model", "fake-model", *extra]))
    ai.me = "p1"
    ai.guess_timeout = timeout
    ai.cands = {"p2": list(bc.ALL_NUMBERS), "p3": list(bc.ALL_NUMBERS)}
    ai.hist = {"p2": [], "p3": []}
    for g in ("0123", "4567"):
        for p, secret in (("p2", "5079"), ("p3", "9812")):
            a, b = bc.score(g, secret)
            ai.cands[p] = [s for s in ai.cands[p] if bc.score(g, s) == (a, b)]
            ai.hist[p].append((g, a, b))
    return ai


fails = 0


def check(name, cond, note=""):
    global fails
    fails += not cond
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"（{note}）" if note else ""), flush=True)


ALIVE = ["p1", "p2", "p3"]
for mode, expect in [("good", "llm"), ("second", "llm"), ("think", "llm"), ("no_json_mode", "llm"),
                     ("garbage", "fallback:bad_json"), ("dup", "fallback:bad_guess"),
                     ("bad_quality", "fallback:rejected"), ("slow", "fallback:timeout"),
                     ("server_error", "fallback:http_500"), ("bad_model", "fallback:http_404")]:
    ai = make_ai(mode, timeout=4.0 if mode == "slow" else 60.0)
    t0 = time.monotonic()
    g = ai.choose(ALIVE)
    dt = time.monotonic() - t0
    check(f"{mode}：喊 {g}", bc.valid_number(g) and dt < ai.guess_timeout and ai.stats.get(expect) == 1,
          f"{dt:.2f} 秒，{ai.stats}")
    if mode == "bad_model":
        check("模型不存在（404）後停用 LLM", not ai.llm.enabled)
    if mode == "no_json_mode":
        check("不支援 response_format 時自動改用純 prompt", ai.llm.json_mode is False)

REQUESTS.clear()
ai = make_ai("good", timeout=1.5)
ai.choose(ALIVE)
check("1.5 秒時限不呼叫 LLM", not REQUESTS and ai.stats.get("fallback:no_time") == 1, str(ai.stats))

REQUESTS.clear()
os.environ["BC_TEST_KEY"] = "sk-test-123"
ai = make_ai("good", extra=["--llm-key-env", "BC_TEST_KEY"])
ai.choose(ALIVE)
path, headers, body = REQUESTS[0]
st = json.loads(body["messages"][1]["content"])
check("POST /v1/chat/completions", path == "/v1/chat/completions")
check("system 訊息是 LLM_SYSTEM_PROMPT", body["messages"][0] == {"role": "system", "content": bc.LLM_SYSTEM_PROMPT})
check("帶模型名稱、json_object 與 Bearer 金鑰",
      body["model"] == "fake-model" and body["response_format"] == {"type": "json_object"}
      and headers.get("Authorization") == "Bearer sk-test-123")
check("提示不含自己的秘密", ai.secret not in body["messages"][1]["content"])
check("局面欄位齊全", {"target", "opponents", "options", "my_exposure", "danger"} <= set(st))

REQUESTS.clear()
ai = make_ai("good", extra=["--llm-key-env", "BC_NO_SUCH_KEY"])
ai.choose(ALIVE)
check("沒有金鑰時不送 Authorization（Ollama）", "Authorization" not in REQUESTS[0][1])

ai = make_ai("bad_quality")
ai.mine = ["1111", "2222"]                     # 危險模式
g = ai.choose(ALIVE)
check("危險模式拒用非候選", ai.stats.get("fallback:rejected") == 1 and g in ai.cands["p3"], f"{g} {ai.stats}")

srv.shutdown()
ai = make_ai("good", extra=["--llm-base-url", "http://127.0.0.1:1/v1"])
t0 = time.monotonic()
g = ai.choose(ALIVE)
check("LLM 伺服器沒開：立即退回演算法", bc.valid_number(g) and ai.stats.get("fallback:connect_error") == 1
      and time.monotonic() - t0 < 2, str(ai.stats))

ai = bc.AI(bc.parse_args(["ai", "--quiet", "--llm", "off"]))
check("--llm off 只用演算法", not ai.llm.enabled)
print(f"\n失敗 {fails} 項")
sys.exit(1 if fails else 0)
