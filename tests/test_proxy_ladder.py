"""프록시의 재시도 사다리를 **끝에서 끝까지** 태운다.

## 왜 이 파일이 따로 있는가

`test_core.py` 는 `check()` 를 직접 불러 항목 하나를 판정한다. 그건 규칙이
맞는지만 본다. 정작 중요한 것은 **규칙이 틀렸다고 판정한 다음에 무슨 일이
벌어지는가** 다 — 그 항목만 다시 보내는지, 힌트가 붙는지, 마지막에 용어집을
떼는지, 조각 모드로 떨어지는지, 끝내 실패하면 원문을 돌려주는지.

그 경로는 지금까지 한 줄도 시험되지 않았다(`proxy.py` 커버리지 44%). 그런데
이 도구가 하는 일의 거의 전부가 거기 있다. 실제로 여기서 났던 사고들:

  · 조각 모드 결과가 검증을 건너뛰어 영어 반향이 성공으로 처리됨
  · 실패한 문단이 캐시에 박혀 정상 모델로 다시 물어도 영어가 나옴
  · 완전한 번역을 `]` 하나 때문에 버리고 조각 모드로 떨어짐

## 어떻게 시험하나

GPU 도 추론 서버도 쓰지 않는다. `httpx.MockTransport` 로 상류를 가로채
**대본대로** 응답을 돌려준다. 대본을 바꿔 가며 사다리의 각 칸을 밟는다.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from pdfko import proxy

PH = re.compile(r"\{v\d+\}")


# ── 대본대로 답하는 가짜 상류 ────────────────────────────────────────────
class Upstream:
    """호출될 때마다 대본의 다음 줄을 돌려준다.

    대본이 떨어지면 마지막 줄을 계속 돌려준다 — 재시도가 몇 번인지에
    시험이 매달리지 않게 하려는 것이다.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.seen: list[str] = []          # 상류가 실제로 받은 사용자 메시지

    def _next(self, sent_items):
        i = min(len(self.seen) - 1, len(self.script) - 1)
        step = self.script[i]
        return step(sent_items) if callable(step) else step

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user = "\n".join(m["content"] for m in body["messages"])
            self.seen.append(user)
            arr, _, _ = proxy.extract_array(body["messages"][0]["content"])
            for m in body["messages"]:
                got, _, _ = proxy.extract_array(m["content"])
                if got:
                    arr = got
            return httpx.Response(200, json={"choices": [
                {"message": {"content": self._next(arr or [])}}]})
        return httpx.MockTransport(handler)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """가짜 상류를 물린 프록시. 캐시와 로그는 임시 폴더로 보낸다."""
    monkeypatch.setattr(proxy, "CACHE_DB", tmp_path / "c.db")
    monkeypatch.setattr(proxy, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(proxy, "_DB", None, raising=False)

    def build(*script):
        up = Upstream(*script)
        monkeypatch.setattr(proxy, "client",
                            httpx.AsyncClient(transport=up.transport()))
        return up, TestClient(proxy.app)
    return build


def ask(cli, *inputs):
    """BabelDOC 이 보내는 모양 그대로 요청한다."""
    items = [{"id": i, "input": t, "layout_label": "plain text"}
             for i, t in enumerate(inputs)]
    body = ("Translate into ko-KR. Reply with a JSON array.\n\n"
            + json.dumps(items, ensure_ascii=False))
    r = cli.post("/v1/chat/completions",
                 json={"model": "m", "messages": [{"role": "user",
                                                   "content": body}]})
    assert r.status_code == 200, r.text
    out, _, _ = proxy.extract_array(
        r.json()["choices"][0]["message"]["content"])
    return {str(o["id"]): o["output"] for o in out}


def arr(*outs):
    return json.dumps([{"id": i, "output": t} for i, t in enumerate(outs)],
                      ensure_ascii=False)


# ── 사다리 한 칸씩 ───────────────────────────────────────────────────────
def test_a_clean_answer_passes_through(rig):
    up, cli = rig(arr("정책은 상태를 행동으로 사상하는 함수이다."))
    got = ask(cli, "A policy maps states to actions.")
    assert got["0"] == "정책은 상태를 행동으로 사상하는 함수이다."
    assert len(up.seen) == 1, "멀쩡한 답에 재시도가 붙었다"


def test_truncated_json_is_repaired_not_retried(rig):
    """완전한 번역을 닫는 괄호 하나 때문에 버리면 안 된다.

    실측으로 이것이 조각 모드로 떨어지는 가장 큰 원인이었다.
    """
    good = '[{"id": 0, "output": "정책은 상태를 행동으로 사상하는 함수이다."}'
    up, cli = rig(good)                      # `]` 가 없다
    got = ask(cli, "A policy maps states to actions.")
    assert got["0"] == "정책은 상태를 행동으로 사상하는 함수이다."
    assert len(up.seen) == 1, "고칠 수 있는 답을 버리고 다시 물었다"


def test_only_the_failed_item_is_resent(rig):
    """배치 10개 중 1개가 틀렸다고 10개를 다시 보내면 GPU가 재작업만 한다."""
    ko = "정책은 상태를 행동으로 사상하는 함수이다."
    ko2 = "가치 함수는 기대 이득을 나타낸다."
    up, cli = rig(
        arr(ko, "The value function gives the expected return."),   # 2번이 영어
        lambda items: json.dumps([{"id": items[0]["id"], "output": ko2}],
                                 ensure_ascii=False),
    )
    got = ask(cli, "A policy maps states to actions.",
              "The value function gives the expected return.")
    assert got["0"] == ko and got["1"] == ko2
    assert len(up.seen) == 2
    second, _, _ = proxy.extract_array(up.seen[1])
    assert [i["id"] for i in second] == [1], "성공한 항목까지 다시 보냈다"


def test_a_dropped_placeholder_is_named_in_the_retry(rig):
    """수식을 흘렸으면 **어느 토큰인지 지목해서** 고치게 해야 한다."""
    src = "The step size {v1} controls how fast {v2} changes over time."
    up, cli = rig(
        arr("스텝 크기 {v1}가 변화 속도를 조절한다."),        # {v2} 유실
        arr("스텝 크기 {v1}가 {v2}의 변화 속도를 조절한다."),
    )
    got = ask(cli, src)
    assert sorted(PH.findall(got["0"])) == ["{v1}", "{v2}"]
    assert "{v2}" in up.seen[1], f"어느 토큰이 빠졌는지 안 알려줬다: {up.seen[1][-300:]}"


def test_leftover_english_triggers_a_retry_quoting_the_sentence(rig):
    """반쪽 번역. 한글 비율만 보면 통과한다 — 실측 4.5%가 이렇게 샜다."""
    half = ("정책은 상태를 행동으로 사상한다. The value of a state is the "
            "expected return starting from that state. 이를 가치라 한다.")
    whole = ("정책은 상태를 행동으로 사상한다. 어떤 상태의 가치는 그 상태에서 "
             "시작하는 기대 이득이다. 이를 가치라 한다.")
    up, cli = rig(arr(half), arr(whole))
    got = ask(cli, "A policy maps states to actions. The value of a state is "
                   "the expected return starting from that state. "
                   "We call this the value.")
    assert got["0"] == whole
    assert "The value of a state" in up.seen[1], up.seen[1][-300:]


def test_jondae_is_rejected(rig):
    """한 화면 안에서 문체가 바뀌면 번역기 티가 난다."""
    up, cli = rig(arr("정책은 상태를 행동으로 사상하는 함수입니다."),
                  arr("정책은 상태를 행동으로 사상하는 함수이다."))
    got = ask(cli, "A policy maps states to actions in this setting.")
    assert got["0"].endswith("함수이다.")
    assert len(up.seen) == 2


def test_a_hopeless_item_comes_back_as_the_source(rig):
    """끝내 안 되면 **원문을 돌려준다.** 빈 문자열을 주면 페이지가 사라진다."""
    src = "A policy maps states to actions in every state of the world."
    up, cli = rig(arr("still english here and nothing else at all"))
    got = ask(cli, src)
    assert got["0"] == src
    assert len(up.seen) >= 3, "포기하기 전에 세 번은 물어봐야 한다"


def test_a_failed_item_is_not_cached(rig, tmp_path):
    """실패를 캐시에 박으면 정상 모델로 다시 물어도 영어가 나온다."""
    import sqlite3
    src = "A policy maps states to actions in every state of the world."
    _, cli = rig(arr("still english here and nothing else at all"))
    ask(cli, src)
    db = sqlite3.connect(tmp_path / "c.db")
    rows = [r for (r,) in db.execute("select tgt from tr")]
    assert not any("still english" in r for r in rows), rows


def test_the_upstream_never_sees_broken_ligatures(rig):
    """`di↵erent` 가 그대로 가면 번역 품질이 무너진다 — 원문 1,763곳."""
    up, cli = rig(arr("서로 다른 정책들을 비교한다."))
    ask(cli, "We compare di↵erent policies under the same conditions.")
    assert "different" in up.seen[0], up.seen[0][-200:]
    assert "di↵erent" not in up.seen[0]


# ── 모델이 스키마를 되풀이해 돌려줄 때 ───────────────────────────────────
def test_a_translation_returned_under_the_input_key_is_still_accepted(rig):
    """모델이 `output` 대신 `input` 에 번역을 담아 보내도 살려 쓴다.

    실측(L02 발표자료): 모델이 요청 스키마를 그대로 되풀이했다.

        {"id": 0, "input": "RL(다른 분야와의 연계)이란 무엇인가?",
         "layout_label": "plain text"}

    번역문 자체는 멀쩡했다. 그런데 `o.get("output")` 이 None 이라 tgt 가 빈
    문자열이 되고, 그때부터 **모든 진단이 눈을 감는다** — 폭 초과도, 영어
    잔존도, 자리표시자 유실도 빈 문자열에서는 감지되지 않는다. 그래서
    재시도 힌트가 "자리표시자를 지켜라"로 떨어졌고(그 문장에는 자리표시자가
    하나도 없다), 모델은 형식이 틀렸다는 말을 못 들어 3차까지 같은 모양을
    돌려줬다. 결국 영어 원문이 그대로 남았다. 22쪽 중 11쪽이 이 꼴이었다.
    """
    src = "What is RL (with other domains)?"
    echoed = json.dumps([{"id": 0, "input": "RL이란 무엇인가?",
                          "layout_label": "plain text"}], ensure_ascii=False)
    up, cli = rig(echoed)
    got = ask(cli, src)
    assert got["0"] == "RL이란 무엇인가?", got
    assert len(up.seen) == 1, "살릴 수 있는 답을 버리고 다시 물었다"


def test_the_normal_output_key_still_wins(rig):
    """둘 다 있으면 `output` 이 정답이다 — 되풀이된 `input` 은 원문일 수 있다."""
    both = json.dumps([{"id": 0, "input": "What is RL?",
                        "output": "RL이란 무엇인가?"}], ensure_ascii=False)
    _, cli = rig(both)
    assert ask(cli, "What is RL?")["0"] == "RL이란 무엇인가?"


def test_an_echoed_source_is_not_mistaken_for_a_translation(rig):
    """`input` 을 살려 쓰되 검증은 그대로 건다. 영어 반향은 통과하면 안 된다."""
    src = "A policy maps states to actions in every state of the world."
    echo = json.dumps([{"id": 0, "input": src}], ensure_ascii=False)
    up, cli = rig(echo)
    assert ask(cli, src)["0"] == src        # 끝내 실패 → 원문 반환
    assert len(up.seen) >= 3, "영어 반향을 번역으로 받아들였다"


def test_a_format_mistake_is_named_in_the_retry(rig):
    """출력이 통째로 비면 **형식이 틀렸다**고 말해야 한다.

    자리표시자가 없는 문장에 "자리표시자를 지켜라"라고 하면 모델은 무엇을
    고쳐야 할지 알 수 없다.
    """
    nothing = json.dumps([{"id": 0, "nonsense": "x"}], ensure_ascii=False)
    up, cli = rig(nothing, nothing, nothing)
    ask(cli, "A policy maps states to actions in every state of the world.")
    assert any("output" in s for s in up.seen[1:]), \
        f"형식을 짚어주지 않았다: {up.seen[1][-200:]}"


def test_no_placeholder_advice_when_the_source_has_none(rig):
    """원문에 자리표시자가 없으면 자리표시자 얘기를 꺼내면 안 된다.

    실측: URL 항목이 한글이 없다는 이유로 실패 판정을 받자, 재시도 힌트가
    "keep every {vN} placeholder exactly once" 로 떨어졌다. 원문에는 {vN} 이
    하나도 없다. 모델은 시키는 대로 **없는 것을 만들어 넣었다.**

        1차  https://www.davidsilver.uk/
        2차  https://www.davidsilver.uk/{vN}/     ← 멀쩡한 URL 이 망가졌다

    지시가 틀리면 모델은 그 틀린 지시를 정확히 따른다.
    """
    src = "https://www.davidsilver.uk/ and https://example.org/paper"
    echo = json.dumps([{"id": 0, "output": src}], ensure_ascii=False)
    up, cli = rig(echo, echo, echo)
    ask(cli, src)
    # `SYSTEM_PREFIX` 에도 "never drop a {vN} token" 이 있지만 그건 무해하다.
    # 해로운 것은 **없는 것을 지키라고 요구하는** 재시도 힌트다.
    bad = "keep every {vN} placeholder"
    for i, sent in enumerate(up.seen[1:], 2):
        assert bad not in sent, \
            f"{i}차 힌트가 없는 자리표시자를 지키라고 했다:\n{sent[-260:]}"


def test_placeholder_advice_still_given_when_the_source_has_them(rig):
    """반대로, 진짜 자리표시자가 있으면 그 지시는 남아야 한다."""
    src = "The step size {v1} controls how fast {v2} changes over time."
    dropped = json.dumps([{"id": 0, "output": "스텝 크기가 속도를 조절한다."}],
                         ensure_ascii=False)
    up, cli = rig(dropped, dropped, dropped)
    ask(cli, src)
    assert any("{v" in s for s in up.seen[1:]), "자리표시자 지시가 사라졌다"


# ── 힌트는 판정 사유를 따른다 ────────────────────────────────────────────
@pytest.mark.parametrize("src,bad,must_mention", [
    # 폭 초과 → 짧게 하라고 해야지, 자리표시자 얘기를 하면 안 된다
    ("What is RL (with other domains)?",
     "RL이란 무엇인가 (다른 분야와의 연계를 포함하여)?", "long"),
    # 자리표시자 유실 → 어느 토큰인지 지목
    ("The step size {v1} controls how fast {v2} changes over time.",
     "스텝 크기가 변화 속도를 조절한다.", "{v"),
    # 번역 자체가 안 됨 → 형식·번역 얘기지 자리표시자가 아니다
    ("A policy maps states to actions in every state.",
     "a policy maps states to actions", "Korean"),
])
def test_the_hint_addresses_the_reason_the_checker_gave(rig, src, bad, must_mention):
    """힌트는 **판정이 준 사유**를 따라야 한다.

    예전에는 `check()` 가 사유를 알고도 `[0]` 으로 버렸고, `repair_hint` 가
    같은 판정을 처음부터 다시 추론했다. 두 곳이 어긋나면 엉뚱한 지시가
    나갔고, 모델은 그 엉뚱한 지시를 정확히 따랐다.
    """
    reply = json.dumps([{"id": 0, "output": bad}], ensure_ascii=False)
    up, cli = rig(reply, reply, reply)
    ask(cli, src)
    assert len(up.seen) > 1, "재시도가 없었다"
    hint_area = up.seen[1][-700:]
    assert must_mention in hint_area, \
        f"{must_mention!r} 를 짚지 않았다:\n{hint_area}"
