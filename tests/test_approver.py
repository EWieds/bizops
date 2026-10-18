import httpx

from bizops.approver import Approver, _parse_jev


def test_parse_jev_shapes():
    assert _parse_jev({"choice": "allow", "confidence": 0.9}) == ("allow", 0.9, "")
    assert _parse_jev({"decision": "proceed", "score": "0.8", "reason": "fine"}) == ("allow", 0.8, "fine")
    assert _parse_jev({"verdict": {"value": "deny"}, "confidence": 0.99})[0] == "block"
    assert _parse_jev("nope") == ("", 0.0, "")


async def test_jev_decides_before_llm(core, org, monkeypatch):
    monkeypatch.setenv("JEV_API_URL", "https://jev.example/decide")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"choice": "block", "confidence": 0.93, "reason": "looks off"})
    ap = Approver(core.settings, core.llm, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    v, trail = await ap.review(agent=org["ceo"], tool_name="pay", args={"memo": "m", "task_id": "t"},
                               reason="r", context={})
    assert v.verdict == "block" and v.approver == "jev" and [s.approver for s in trail] == ["rules", "jev"]
    assert seen


async def test_low_confidence_jev_falls_through_to_llm(core, org, monkeypatch):
    monkeypatch.setenv("JEV_API_URL", "https://jev.example/decide")
    ap = Approver(core.settings, core.llm, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choice": "allow", "confidence": 0.3}))))
    v, trail = await ap.review(agent=org["ceo"], tool_name="pay", args={"memo": "m", "task_id": "t"},
                               reason="r", context={})
    assert v.approver == "llm" and v.verdict == "allow"


async def test_no_confident_verdict_blocks(core, org):
    core.settings.policy["approver"]["actions"]["pay"]["chain"] = ["rules", "jev"]  # jev not configured -> abstains
    v, _ = await Approver(core.settings, core.llm).review(agent=org["ceo"], tool_name="pay",
                                                          args={"memo": "m", "task_id": "t"}, reason="r", context={})
    assert v.verdict == "block" and v.approver == "fallback"
