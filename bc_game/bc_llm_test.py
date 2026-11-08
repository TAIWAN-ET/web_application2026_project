"""用假的 LLM 客戶端測試 bc.py 的 LLM 模式（不需網路與金鑰，但需要 pip install anthropic）。

  python bc_llm_test.py
"""
import sys, time, json, types, os, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bc
import anthropic

class Block:
    def __init__(s, t): s.type, s.text = "text", t
class Resp:
    def __init__(s, t, stop="end_turn"): s.content, s.stop_reason = [Block(t)], stop

calls = []
class FakeMessages:
    def __init__(s, mode): s.mode = mode
    def create(s, **kw):
        calls.append(kw)
        st = json.loads(kw["messages"][0]["content"])
        m = s.mode
        if m == "good":   return Resp(json.dumps({"guess": st["options"][0]["guess"], "reason": "最多分組"}))
        if m == "second": return Resp(json.dumps({"guess": st["options"][1]["guess"], "reason": "次佳"}))
        if m == "garbage":return Resp("我覺得是 12345")
        if m == "dup":    return Resp('{"guess":"1123","reason":"x"}')
        if m == "bad_quality": return Resp('{"guess":"0123","reason":"x"}')
        if m == "slow":   time.sleep(5); return Resp('{"guess":"0123","reason":"x"}')
        if m == "refusal":return Resp("", "refusal")
        if m == "auth":   raise anthropic.AuthenticationError("bad key", response=types.SimpleNamespace(status_code=401, headers={}, request=None), body=None)
class FakeClient:
    def __init__(s, mode): s.beta = types.SimpleNamespace(messages=FakeMessages(mode))
    def with_options(s, **kw): return s

def make_ai(mode, timeout=60.0):
    a = bc.parse_args(["ai", "--quiet", "--llm", "anthropic"])
    os.environ["ANTHROPIC_API_KEY"] = "test"
    ai = bc.AI(a)
    ai.llm.client = FakeClient(mode)
    ai.me = "p1"; ai.guess_timeout = timeout
    secret = "5079"
    ai.cands = {"p2": list(bc.ALL_NUMBERS), "p3": list(bc.ALL_NUMBERS)}
    ai.hist = {"p2": [], "p3": []}
    for g in ("0123", "4567"):
        for p in ("p2", "p3"):
            a_, b_ = bc.score(g, secret if p == "p2" else "9812")
            ai.cands[p] = [s for s in ai.cands[p] if bc.score(g, s) == (a_, b_)]
            ai.hist[p].append((g, a_, b_))
    return ai

fails = 0
def check(name, cond, note=""):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + name, note)

for mode, expect in [("good", "llm"), ("second", "llm"), ("garbage", "fallback:bad_json"),
                     ("dup", "fallback:bad_guess"), ("bad_quality", None), ("slow", "fallback:timeout"),
                     ("refusal", "fallback:refusal"), ("auth", "fallback:api_error:AuthenticationError")]:
    ai = make_ai(mode, timeout=4.0 if mode == "slow" else 60.0)
    t0 = time.monotonic(); g = ai.choose(["p1", "p2", "p3"]); dt = time.monotonic() - t0
    ok = bc.valid_number(g) and dt < ai.guess_timeout
    if expect: ok = ok and ai.stats.get(expect) == 1
    else: ok = ok and (ai.stats.get("fallback:rejected") == 1 or ai.stats.get("llm") == 1)
    check(f"{mode}: 喊 {g}", ok, f"{dt:.2f}s stats={ai.stats}")
    if mode == "auth": check("auth 錯誤後停用 LLM", not ai.llm.enabled)

# 1.5 秒時限：不呼叫 LLM
calls.clear(); ai = make_ai("good", timeout=1.5); g = ai.choose(["p1", "p2", "p3"])
check("1.5 秒時限不呼叫 LLM", not calls and ai.stats.get("fallback:no_time") == 1, str(ai.stats))

# 提示內容不含自己的秘密、請求參數正確
calls.clear(); ai = make_ai("good"); ai.choose(["p1", "p2", "p3"])
kw = calls[0]; st = json.loads(kw["messages"][0]["content"])
check("請求帶 system prompt 與 json_schema", kw["system"] == bc.LLM_SYSTEM_PROMPT and kw["output_config"]["format"]["type"] == "json_schema")
check("模型與 fallbacks", kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default")
check("提示不含自己的秘密", ai.secret not in kw["messages"][0]["content"])
check("state 欄位", {"target", "opponents", "options", "my_exposure", "danger"} <= set(st), json.dumps(st, ensure_ascii=False)[:400])

# 危險模式：非候選被拒
ai = make_ai("good"); ai.mine = ["1111", "2222"]
ai.llm.client = FakeClient("bad_quality")
S = ai.cands[min(ai.cands, key=lambda p: len(ai.cands[p]))]
g = ai.choose(["p1", "p2", "p3"])
check("危險模式只喊候選", g in S or g in ai.cands["p2"] or g in ai.cands["p3"], f"{g} stats={ai.stats}")

# 沒有金鑰
os.environ.pop("ANTHROPIC_API_KEY")
a = bc.parse_args(["ai", "--quiet"]); ai = bc.AI(a)
check("沒有金鑰自動停用", not ai.llm.enabled, ai.llm.status)
print("失敗", fails)
