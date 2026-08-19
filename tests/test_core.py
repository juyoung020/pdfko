"""핵심 불변식 검증.

이 테스트들은 전부 **실제로 겪은 실패**에서 나왔다. 각 테스트는 한 번 사람을
속였던 버그를 다시 잡는다.
"""

import re
import shutil

import pytest

from pdfko import glyphmap, proxy, repair


# ── 텍스트 레이어 손상 복구 ──────────────────────────────────────────────
def test_ligature_context_sensitive():
    """`↵` 는 앞 글자에 따라 ff 이기도 하고 α 이기도 하다."""
    assert repair.repair("di↵erent") == "different"
    assert repair.repair("o↵-policy") == "off-policy"
    assert repair.repair("Ho↵") == "Hoff"
    # 앞이 라틴 문자가 아니면 수학 기호다. ffw 로 바꾸면 안 된다.
    assert repair.repair("↵w") == "αw"
    assert repair.repair("↵n") == "αn"


def test_repair_preserves_placeholders():
    """자리표시자와 태그 구간은 절대 건드리지 않는다."""
    src = "The {v1} is di↵erent from <style id='2'>o↵-policy</style> {v2}."
    out = repair.repair(src)
    assert "{v1}" in out and "{v2}" in out
    assert "<style id='2'>" in out and "</style>" in out
    assert "different" in out and "off-policy" in out


def test_intact_ligatures_untouched():
    """fi·fl 합자는 깨지지 않았다. 멀쩡한 낱말을 건드리면 안 된다."""
    s = "specific efficient define final"
    assert repair.repair(s) == s


# ── 합자 자리표시자 소멸 ─────────────────────────────────────────────────
def test_dissolve_only_known_pairs():
    """사전에 있는 조합만 되돌리고 진짜 수식은 남긴다."""
    table = {"di\x00erent": "different", "o\x00-policy": "off-policy"}
    out, n = glyphmap.dissolve("two di{v1}erent MDPs", table)
    assert out == "two different MDPs" and n == 1
    # 사전에 없는 것 = 진짜 수식. 손대면 방정식이 사라진다.
    for math in ("BE{v2}BE", "MDP{v4}MDP", "w{v2}x"):
        out, n = glyphmap.dissolve(math, table)
        assert out == math and n == 0


# ── 응답 검증 ────────────────────────────────────────────────────────────
def _chk(src, tgt):
    return proxy.check([{"id": 0, "input": src}], [{"id": 0, "output": tgt}])


def test_placeholder_must_round_trip():
    ok, _ = _chk("a {v1} b {v2} c", "가 {v1} 나 {v2} 다")
    assert ok
    ok, why = _chk("a {v1} b {v2} c", "가 {v1} 나 다")
    assert not ok and "placeholder" in why


def test_english_echo_rejected():
    ok, why = _chk("the policy is optimal here", "the policy is optimal here")
    assert not ok


def test_malformed_style_tag_rejected():
    """모델이 태그 괄호를 전각으로 쓰면 엔진이 인식 못 해 글자로 찍힌다."""
    ok, _ = _chk("a <style id='1'>b</style> c", "가 <style id='1'>나</style> 다")
    assert ok
    for bad in ("가 </style〉 나", "가 〈style id='3'> 나"):
        ok, why = _chk("a <style id='1'>b</style> c", bad)
        assert not ok and "style" in why


def test_runaway_length_rejected():
    """엔진이 자른 조각을 모델이 '완성'해 버리면 줄 수가 폭발한다."""
    src = "the effect of the policy on the value function"
    ok, _ = _chk(src, "정책이 가치 함수에 미치는 영향")
    assert ok
    ok, why = _chk(src, "정책이 가치 함수에 미치는 영향이다. " * 6)
    assert not ok and "width" in why


# ── 조각 모드 ────────────────────────────────────────────────────────────
def test_split_runs_round_trip():
    """조각과 보존대상을 번갈아 이으면 원문이 정확히 복원되어야 한다."""
    for src in (
        "where {v1}is <style id='2'>soft-max</style> and {v3} holds",
        "{v1}{v2}{v3}",
        "no placeholders at all",
        "trailing {v9}",
    ):
        runs, keep = proxy.split_runs(src)
        back = "".join(runs[i] + (keep[i] if i < len(keep) else "")
                       for i in range(len(runs)))
        assert back == src


def test_fragment_mode_isolates_tags():
    """조각 모드는 자리표시자와 태그를 **둘 다** 모델에서 떼어놓는다."""
    _, keep = proxy.split_runs("a {v1} b <style id='2'>c</style> d")
    assert "{v1}" in keep
    assert any("style" in k for k in keep)


# ── 캐시 정합성 ──────────────────────────────────────────────────────────
def test_rules_fingerprint_tracks_changes():
    """규칙을 바꾸면 지문이 달라져 캐시가 자동 무효화되어야 한다.

    이 장치가 없어서 같은 수정을 네 번 반복했다.
    """
    a = proxy._rules_fingerprint()
    old = proxy.WIDTH_MAX
    try:
        proxy.WIDTH_MAX = old + 0.5
        assert proxy._rules_fingerprint() != a
    finally:
        proxy.WIDTH_MAX = old
    assert proxy._rules_fingerprint() == a


# ── 출력 정리 ────────────────────────────────────────────────────────────
def test_output_parsing_tolerates_fences():
    """모델이 코드펜스로 감싸 와도 배열을 꺼낼 수 있어야 한다.

    평문 시절 쓰던 머리말 제거는 JSON 프로토콜로 바뀌면서 필요 없어졌다.
    지금은 응답 전체가 JSON 배열이어야 하고, 그 앞뒤 장식만 걷어낸다.
    """
    want = [{"id": 0, "output": "정책을 개선한다."}]
    for raw in (
        '[{"id": 0, "output": "정책을 개선한다."}]',
        '```json\n[{"id": 0, "output": "정책을 개선한다."}]\n```',
        '```\n[{"id": 0, "output": "정책을 개선한다."}]\n```',
    ):
        assert proxy.parse_output(raw) == want
    # 배열이 아니면 None — 호출부가 재시도하도록
    assert proxy.parse_output("죄송합니다, 번역할 수 없습니다.") is None


def test_json_array_extracted_from_tail():
    """지시문 안의 대괄호가 아니라 **끝에 붙은** 배열을 잡아야 한다."""
    msg = ("rules mention [[...]] and %s\n\n"
           '[\n {"id": 0, "input": "a"},\n {"id": 1, "input": "b"}\n]')
    arr, s, e = proxy.extract_array(msg)
    assert arr is not None and len(arr) == 2 and arr[1]["id"] == 1


# ── 실행기 불변식 ────────────────────────────────────────────────────────
def test_plan_chunks_rejects_zero():
    """구간 크기 0 은 무한 루프를 만든다. 예전에 OOM 까지 갔다."""
    import pytest
    from pathlib import Path
    from pdfko import runner
    for bad in (0, -5):
        with pytest.raises(ValueError):
            runner.plan_chunks(1, 15, bad, Path("/tmp"))
    assert len(runner.plan_chunks(1, 100, 40, Path("/tmp"))) == 3


def test_merge_refuses_gaps(tmp_path):
    """구간이 비면 합치지 않는다. 조용히 건너뛰면 이후 쪽 번호가 어긋나
    검사기가 엉뚱한 원본과 비교하고 복구가 멀쩡한 쪽을 덮어쓴다."""
    import pytest
    import pymupdf
    from pdfko import runner
    chunks = runner.plan_chunks(1, 60, 20, tmp_path)
    for c in chunks:
        c.outdir.mkdir(parents=True, exist_ok=True)
    for c in (chunks[0], chunks[2]):        # 가운데를 비워둔다
        d = pymupdf.open(); d.new_page(); d.save(c.outdir / "a.mono.pdf"); d.close()
    with pytest.raises(RuntimeError):
        runner.merge(chunks, tmp_path / "out.pdf")


def test_chunk_picks_newest(tmp_path):
    """알파벳순으로 뽑으면 옛 결과가 새 결과를 이긴다."""
    import time
    import pymupdf
    from pdfko import runner
    c = runner.Chunk(1, 10, tmp_path / "p")
    c.outdir.mkdir(parents=True)
    for name in ("RLbook.zzz.mono.pdf", "cleaned.aaa.mono.pdf"):
        d = pymupdf.open(); d.new_page(); d.save(c.outdir / name); d.close()
        time.sleep(0.02)
    assert c.pdf().name == "cleaned.aaa.mono.pdf"


# ── 숨은 글자 청소 ───────────────────────────────────────────────────────
def test_clipscan_preserves_visible_text(tmp_path):
    """잘라내기 밖 글자만 지우고 보이는 글자는 하나도 잃지 않아야 한다."""
    import pymupdf
    from pdfko import clipscan
    src = tmp_path / "s.pdf"
    d = pymupdf.open(); p = d.new_page()
    p.insert_text((72, 100), "VISIBLE BODY TEXT ON THE PAGE", fontsize=12)
    # 작은 잘라내기 창 밖에 글자를 그린다 → 화면에는 안 보인다
    p.draw_rect(pymupdf.Rect(10, 10, 30, 30))
    d.save(src); d.close()
    before = pymupdf.open(src)[0].get_text()
    dst = tmp_path / "c.pdf"
    touched, rolled, lost = clipscan.clean(src, dst, min_hidden=1)
    after = pymupdf.open(dst)[0].get_text()
    assert after == before          # 보이는 텍스트는 그대로
    assert isinstance(lost, dict)   # 손실 기록을 돌려준다


# ── 이번 라운드의 차단 결함들 ─────────────────────────────────────────────
def test_revert_pages_can_save_over_itself(tmp_path):
    """자동 복구는 번역본을 제자리에서 고친다. 예전엔 그때마다 100% 죽었다."""
    import pymupdf
    from pdfko import recover
    orig = tmp_path / "o.pdf"
    d = pymupdf.open()
    for t in ("ONE", "TWO", "THREE"):
        d.new_page().insert_text((72, 200), t, fontsize=20)
    d.save(orig); d.close()
    trans = tmp_path / "t.pdf"
    shutil.copy(orig, trans)
    recs = recover.revert_pages(trans, orig, [2], 0, trans)   # 같은 경로
    assert [r.action for r in recs] == ["reverted"]
    with pymupdf.open(trans) as t:
        assert t.page_count == 3
        assert "TWO" in t[1].get_text()
    assert not list(tmp_path.glob("*.tmp"))


def test_clipscan_counts_characters_not_operators():
    """`Tj` 하나에 든 글자를 세야 한다. 연산자를 세면 임계값이 무의미해진다."""
    from pdfko.clipscan import _str_chars
    assert _str_chars(b"(" + b"H" * 200 + b")") == 200
    assert _str_chars(b"[(AB) -20 (CDE)]") == 5
    assert _str_chars(b"<48656c6c6f>") == 5
    assert _str_chars(b"(a\\(b)") == 3          # 이스케이프는 한 글자


def test_glyphmap_keeps_style_tags_balanced():
    """엔진은 합자 자리에서 서식을 끊었다 잇는다. 태그를 지우면 짝이 깨진다."""
    from pdfko import glyphmap
    tbl = {"di\x00erence": "difference"}
    src = "<style id='4'>temporal-di</style>{v6}<style id='5'>erence</style>"
    out, n = glyphmap.dissolve(src, tbl)
    assert n == 1
    assert out.count("<style") == src.count("<style")
    assert out.count("</style>") == src.count("</style>")
    assert "difference" in re.sub(r"</?style[^>]*>", "", out)
    # 사전에 없는 조합은 진짜 수식이다 — 건드리지 않는다
    assert glyphmap.dissolve("w{v2}x", tbl) == ("w{v2}x", 0)


def test_merge_rejects_short_chunk(tmp_path):
    """구간 파일이 있기만 하면 통과시키면 쪽 번호가 통째로 밀린다."""
    import pymupdf
    from pdfko import runner
    work = tmp_path / "w"
    (work / "parts").mkdir(parents=True)
    chunks = runner.plan_chunks(1, 6, 3, work)
    for c, npages in zip(chunks, (3, 2)):        # 두 번째가 2/3 쪽
        c.outdir.mkdir(parents=True, exist_ok=True)
        d = pymupdf.open()
        for _ in range(npages):
            d.new_page()
        d.save(c.outdir / "x.mono.pdf"); d.close()
    with pytest.raises(RuntimeError, match="온전하지 않습니다"):
        runner.merge(chunks, tmp_path / "out.pdf")


def test_proxy_port_is_not_stolen(tmp_path):
    """남이 쥔 포트는 죽이지 않고 비켜 간다. 예전엔 무관한 서버를 SIGTERM 했다."""
    import socket
    from pdfko import runner
    for d in ("logs", "cache", "work", "parts"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    srv = runner.Server(tmp_path, "m")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        taken = s.getsockname()[1]
        assert srv.identify(taken) is None      # 우리 것이 아니다
        assert not srv._port_is_free(taken)
        assert srv._free_port(taken) != taken   # 비켜 간다


def test_glossary_changes_the_cache_key(tmp_path):
    """용어집을 바꾸면 캐시가 빗나가야 한다.

    한때 요청 본문의 system 메시지를 해시했는데, BabelDOC 은 system 을 아예
    보내지 않는다(전부 user 메시지 한 덩어리). 빈 문자열의 해시가 상수로
    박혀서, 용어집을 바꿔도 옛 번역이 그대로 나왔다.
    """
    from pdfko import runner
    g1 = tmp_path / "a.csv"; g1.write_text("source,target\nreward,보상\n")
    g2 = tmp_path / "b.csv"; g2.write_text("source,target\nreward,리워드\n")
    s1, s2 = runner.Server.signature(g1), runner.Server.signature(g2)
    assert s1 != s2
    assert s1 == runner.Server.signature(g1)
    assert s1 != runner.Server.signature(None)
    for d in ("logs", "cache", "work", "parts"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    srv = runner.Server(tmp_path, "m")
    srv.user_sig = s1; r1 = srv.expected_rules()
    srv.user_sig = s2; r2 = srv.expected_rules()
    assert r1 != r2          # 규칙 지문이 갈리면 item_key 도 갈린다


def test_lost_words_counted_as_multiset():
    """머리글이 반복되는 페이지에서 한쪽을 통째로 지워도 '손실 0' 이 됐다."""
    from collections import Counter
    before = "HEADER alpha beta HEADER alpha beta".split()
    after = "HEADER alpha beta".split()
    assert [w for w in before if w not in after] == []        # 옛 방식: 못 잡음
    assert len(list((Counter(before) - Counter(after)).elements())) == 3


def test_parse_pages():
    """`7` 은 7쪽 한 장이고, `0` 계열은 offset 을 음수로 만들면 안 된다."""
    from pdfko.cli import _parse_pages
    assert _parse_pages("", 100) == (1, 100)
    assert _parse_pages("7", 100) == (7, 7)          # 끝까지가 아니다
    assert _parse_pages("13-502", 600) == (13, 502)
    assert _parse_pages("2-999999", 100) == (2, 100)
    assert _parse_pages("0-3", 100) == (1, 3)        # offset 이 -1 이 되면 안 된다
    for bad in ("abc", "-5", "5-2", "0-0", "999-1000"):
        assert isinstance(_parse_pages(bad, 100), str), bad


def test_splice_page_checks_geometry(tmp_path):
    """쪽수나 용지 크기가 다르면 책 한가운데 이상한 페이지가 끼어든다."""
    import pymupdf
    from pdfko import recover
    tgt = tmp_path / "t.pdf"
    d = pymupdf.open()
    for _ in range(3):
        d.new_page()
    d.save(tgt); d.close()
    two = tmp_path / "two.pdf"
    d = pymupdf.open(); d.new_page(); d.new_page(); d.save(two); d.close()
    small = tmp_path / "s.pdf"
    d = pymupdf.open(); d.new_page(width=200, height=200); d.save(small); d.close()
    for bad in (two, small):
        with pytest.raises(ValueError):
            recover.splice_page(tgt, 2, bad, tgt)


def test_term_extraction_is_domain_agnostic(tmp_path):
    """분야 어휘 목록 없이 문서에서 용어를 뽑는다.

    강화학습 용어를 도구에 박아 두면 그 책에만 맞는 도구가 된다. 여기서는
    생물학 문서를 넣고 생물학 용어가 나오는지 본다.
    """
    import pymupdf
    from pdfko import terms
    body = ("The cell membrane regulates transport. Membrane proteins embedded in "
            "the cell membrane act as ion channels. An ion channel opens when the "
            "membrane potential changes. Membrane potential depends on ion channel "
            "density. Cell membrane repair follows membrane potential collapse. "
            "Ion channels and membrane proteins together set the membrane potential. ")
    src = tmp_path / "bio.pdf"
    d = pymupdf.open()
    for _ in range(3):
        p = d.new_page()
        y = 80
        for line in [body[i:i + 90] for i in range(0, len(body), 90)] * 3:
            p.insert_text((60, y), line, fontsize=9)
            y += 14
    d.save(src); d.close()

    got = [t for _, t in terms.extract(src, min_count=3, top=20)]
    assert any("membrane" in t for t in got), got
    # 기능어는 후보가 되면 안 된다
    assert not {"the", "and", "when", "with"} & set(got)


def test_generated_glossary_is_parseable(tmp_path):
    """만든 CSV 가 그대로 --glossary 로 되돌아가야 한다. 주석·여분 칸 금지."""
    import csv
    from pdfko import terms
    out = tmp_path / "g.csv"
    terms.write_csv(out, [(9, "value function"), (5, "off-policy")])
    rows = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert [r["source"] for r in rows] == ["value function", "off-policy"]
    assert all(r["target"] == "" for r in rows)      # 사용자가 채울 자리


def test_decide_rejects_bad_term_translations(monkeypatch):
    """역어로 쓸 수 없는 응답은 버린다. 문장이 통째로 오면 용어집이 망가진다."""
    from pdfko import terms
    fake = {0: "가치 함수",          # 정상
            1: "value function",     # 영어 반향
            2: "이 용어는 정책을 뜻한다.",   # 문장으로 돌아옴
            3: "",                   # 빈 응답
            4: "정책"}               # 정상
    monkeypatch.setattr(terms, "translate_batch", lambda *a, **k: fake, raising=False)
    import pdfko.client as _c
    monkeypatch.setattr(_c, "translate_batch", lambda *a, **k: fake)
    got = terms.decide([(9, "value function"), (8, "echo"), (7, "sentence"),
                        (6, "empty"), (5, "policy")], port=1, model="m")
    assert got == {"value function": "가치 함수", "policy": "정책"}


def test_stoplist_has_no_content_words():
    """STOP 은 영어 기능어만 담는다. 내용어가 하나라도 들어가면 분야 도구가 된다.

    한때 잡음을 줄이려고 `learning`, `method`, `system` 을 넣었다. 머신러닝
    교재에서 `learning` 은 막아야 할 잡음이 아니라 가장 중요한 용어다.
    """
    from pdfko.terms import STOP
    content = {"learning", "learn", "method", "system", "problem", "model", "policy",
               "reward", "agent", "state", "value", "action", "function", "network",
               "cell", "gradient", "error", "signal", "control", "search", "solution",
               "energy", "force", "market", "gene", "protein", "algorithm",
               # 숫자·서수·형용사도 안 된다. 이것들이 막고 있던 것:
               #   second messenger(세포생물학) · first order(수학) · third party(법학)
               #   next generation(유전체학) · even function(수학) · still image(영상)
               "one", "two", "three", "four", "five", "first", "second", "third",
               "next", "last", "even", "still", "just"}
    assert not (STOP & content), STOP & content


def test_keep_terms_drops_common_words(monkeypatch):
    """무엇이 용어인지는 목록이 아니라 모델이 고른다.

    형식은 **항목별 참/거짓**이다. "골라서 배열로 돌려달라"고 하면 어떤
    분야에서는 모델이 통째로 `[]` 를 뱉는다 — 실측으로 헌법학 용어에
    5회 연속 빈 배열이었고, 그러면 폴백이 잡음까지 전부 통과시킨다.
    """
    import json
    from pdfko import terms

    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        sent = json.loads(req.data)["messages"][0]["content"]
        cands = json.loads(sent[sent.find("["):])
        verdict = {c: (" " in c or "-" in c) for c in cands}    # 구만 용어라 치자
        return _Resp(json.dumps(
            {"choices": [{"message": {"content": json.dumps(verdict)}}]}).encode())

    # terms 는 함수 안에서 urllib 를 임포트하므로 전역 urlopen 을 갈아끼운다
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", fake_urlopen)
    rows = [(9, "value function"), (8, "after"), (7, "off-policy"), (6, "better")]
    got = [t for _, t in terms.keep_terms(rows, port=1, model="m")]
    assert got == ["value function", "off-policy"]


def test_keep_terms_survives_a_dead_model(monkeypatch):
    """모델이 답을 못 하면 후보를 통째로 살린다. 용어를 잃는 쪽이 더 비싸다."""
    from pdfko import terms
    import urllib.request as _u

    def boom(*a, **k):
        raise OSError("upstream down")

    monkeypatch.setattr(_u, "urlopen", boom)
    rows = [(9, "value function"), (8, "after")]
    assert terms.keep_terms(rows, port=1, model="m") == rows


def test_stemming_asks_the_document_not_a_word_list():
    """`-s` 로 끝나는 단수 명사를 망가뜨리면 안 된다.

    `bias→bia`, `analysis→analysi`, `physics→physic` 이 되면 문서에 없는
    문자열이 용어집에 실린다. 그 피해는 생물학·물리학·통계학에만 가고
    강화학습은 멀쩡하다 — 분야에 따라 결과가 달라지는 바로 그 상태다.
    예외 목록 대신 **문서에 단수형이 실제로 있는지**로 판단한다.
    """
    from pdfko.terms import _norm
    doc = {"state", "states", "policy", "policies", "channel", "channels",
           "bias", "analysis", "physics", "species", "lens", "virus", "axis"}
    assert _norm("states", doc) == "state"          # 단수형이 있으니 접는다
    assert _norm("policies", doc) == "policy"
    assert _norm("channels", doc) == "channel"
    for solo in ("bias", "analysis", "physics", "species", "lens", "virus", "axis"):
        assert _norm(solo, doc) == solo, solo       # 단수형이 없으니 그대로


def test_style_word_in_prose_is_not_rejected():
    """본문에 `style` 이 정당하게 나오는 문서를 거부하면 안 된다.

    타이포그래피·미술사·CSS 교재가 통째로 3회 재시도 끝에 영어로 남았다.
    괄호 옆에 붙은 `style` 만 깨진 태그로 본다.
    """
    from pdfko import proxy
    src = "the baroque style of the period"
    assert proxy.check([{"id": 0, "input": src}],
                       [{"id": 0, "output": "그 시대의 바로크 style 양식"}])[0]
    tagged = "a <style id='1'>b</style> c"
    for broken in ("가 </style〉 나", "가 〈style id='3'> 나"):
        ok, why = proxy.check([{"id": 0, "input": tagged}],
                              [{"id": 0, "output": broken}])
        assert not ok and "style" in why


def test_system_prompt_names_no_subject_area():
    """모든 문단에 붙는 지시문이 분야를 말하면 안 된다.

    한때 "academic machine-learning textbooks" 라고 못박혀 있었다. 용어집은
    옵션이지만 이 문장은 모든 문서의 모든 문단에 붙는다.
    """
    from pdfko import proxy
    low = (proxy.SYSTEM_PREFIX + proxy.CONCISE_RULE).lower()
    for field in ("machine-learning", "machine learning", "reinforcement",
                  "biology", "chemistry", "physics", "law", "medicine"):
        assert field not in low, field


def test_keep_terms_survives_an_empty_verdict(monkeypatch):
    """모델이 빈 답을 주면 후보를 통째로 살린다.

    실측으로 헌법학 용어에 `[]` 가 5회 연속 나왔다. 그때 아무것도 안 남기면
    그 분야만 용어 통일이 통째로 사라진다. 용어를 잃는 쪽이 더 비싸다.
    """
    import json
    from pdfko import terms
    import urllib.request as _u

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def empty(req, timeout=0):
        return _Resp(json.dumps(
            {"choices": [{"message": {"content": "{}"}}]}).encode())

    monkeypatch.setattr(_u, "urlopen", empty)
    rows = [(9, "judicial review"), (8, "due process")]
    assert terms.keep_terms(rows, port=1, model="m") == rows


def test_coverage_catches_untranslated_output(tmp_path):
    """번역이 하나도 안 됐는데 성공으로 보고하던 최악의 실패.

    모델 이름을 잘못 적으면 번역 엔진은 영어 페이지를 그대로 내놓으면서
    종료 코드 0 을 돌려준다. 실측으로 한글 0자짜리 PDF 가 "파손 0쪽 · 완료"
    로 나왔다. 500쪽이면 서너 시간을 버린다.
    """
    import textwrap
    import pymupdf
    from pdfko import qa

    def build(name, body, font):
        f = tmp_path / name
        d = pymupdf.open()
        pg = d.new_page()
        y = 60
        for line in textwrap.wrap(body, 40)[:30]:
            pg.insert_text((50, y), line, fontsize=10, fontname=font)
            y += 18
        d.save(f); d.close()
        return str(f)

    eng = ("The membrane potential changes when an ion channel opens and "
           "sodium flows inward across the lipid bilayer. ") * 4
    kor = ("세포막 전위는 이온 통로가 열릴 때 변화하며 나트륨이 지질 이중층을 "
           "가로질러 안으로 흐른다. ") * 4

    judged, empty = qa.coverage(build("en.pdf", eng, "helv"))
    assert judged == 1 and empty == [1], (judged, empty)   # 전부 영어 → 잡는다

    judged, empty = qa.coverage(build("ko.pdf", kor, "korea"))
    assert judged == 1 and empty == [], (judged, empty)    # 한국어 → 통과


def test_model_store_is_shared_not_per_workdir():
    """모델 저장소가 작업 폴더 안에 있으면 `--gguf` 가 책마다 필요해진다.

    그때 엔진은 영어를 그대로 내놓고 성공을 보고하고, 6GB 사본이 책 수만큼
    쌓인다. 실측으로 첫 사용자가 정확히 이 함정에 빠졌다.
    """
    from pathlib import Path
    from pdfko import runner
    assert Path.home() in runner.MODEL_STORE.parents
    for probe in (Path("/tmp/a_ko"), Path("/tmp/b_ko")):
        assert probe not in runner.MODEL_STORE.parents


def test_glossary_uses_the_spelling_the_document_actually_has(tmp_path):
    """용어집의 원어는 **원문에 그대로 있는 문자열**이어야 한다.

    번역 엔진은 용어집 원어를 원문 텍스트에 그대로 대조한다. 우리가 합자를
    복구하고 기호를 걷어낸 깨끗한 철자를 넣으면 한 번도 걸리지 않는다.
    실측: 용어집에 `off-policy` 를 넣었는데 원문에는 `o↵-policy` 만 169회,
    `state-action` 을 넣었는데 원문에는 엔대시 `state–action` 만 91회 있었다.
    """
    import csv
    import pymupdf
    from pdfko import terms
    body = ("The o↵-policy method updates the state–action pair while "
            "the o↵-policy target di↵ers from behaviour. ")
    src = tmp_path / "s.pdf"
    d = pymupdf.open()
    for _ in range(3):
        p = d.new_page()
        y = 60
        for line in [body] * 8:
            p.insert_text((40, y), line, fontsize=8)
            y += 16
    d.save(src); d.close()

    rows = terms.extract(src, min_count=3)
    got = {t for _, t in rows}
    assert any("policy" in t for t in got), got
    # 합자가 복구된 깨끗한 철자로 후보가 잡히고
    key = next(t for t in got if "policy" in t)
    # 실제 표기는 원문에 그대로 있어야 한다
    raw = "".join(pymupdf.open(src)[i].get_text() for i in range(3))
    surfaces = terms.SURFACES.get(key, set())
    assert surfaces and all(s in raw for s in surfaces), (key, surfaces)

    out = tmp_path / "g.csv"
    terms.write_csv(out, rows, {key: "비활성 정책"})
    sources = [r["source"] for r in
               csv.DictReader(out.read_text(encoding="utf-8").splitlines())]
    assert sources and all(s in raw for s in sources), sources


def test_write_csv_survives_hostile_targets(tmp_path):
    """역어에 쉼표·따옴표가 들어가도 CSV 가 깨지면 안 된다.

    f-string 으로 이어 붙이던 때는 쉼표 하나에 칸이 밀려 항목이 조용히
    사라지고, 개행이 들어가면 엔진의 CSV 파서가 죽었다.
    """
    import csv
    from pdfko import terms
    terms.SURFACES.clear()
    out = tmp_path / "g.csv"
    terms.write_csv(out, [(9, "sample mean"), (8, "policy")],
                    {"sample mean": '표본 평균, 샘플 평균', "policy": '"정책"'})
    rows = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert {r["source"] for r in rows} == {"sample mean", "policy"}
    assert rows[0]["target"] == "표본 평균, 샘플 평균"   # 칸이 밀리지 않는다


def test_decide_rejects_letterless_and_placeholder_targets(monkeypatch):
    """`hangul_ratio` 는 글자가 없으면 1.0 이라, 숫자·기호가 역어로 통과했다."""
    from pdfko import terms
    import pdfko.client as _c
    bad = {0: "19", 1: "—", 2: "| 표 |", 3: "{v1} 노름", 4: "가치 함수입니다"}
    monkeypatch.setattr(_c, "translate_batch", lambda *a, **k: bad)
    got = terms.decide([(9, f"t{i}") for i in range(5)], port=1, model="m")
    assert got == {}


def test_bad_glossary_is_refused_not_ignored(tmp_path):
    """잘못된 용어집을 조용히 무시하면 안 된다.

    번역 엔진은 헤더가 틀린 CSV 를 말없이 건너뛰고 그냥 번역한다. 사용자는
    용어집이 적용된 줄 알고 500쪽 결과를 쓰게 된다.
    """
    from pdfko import terms
    good = tmp_path / "g.csv"
    good.write_text("source,target,tgt_lng\npolicy,정책,\n", encoding="utf-8")
    assert terms.check_csv(good) == ""

    bad_head = tmp_path / "b.csv"
    bad_head.write_text("word,korean\npolicy,정책\n", encoding="utf-8")
    assert "source,target" in terms.check_csv(bad_head)

    empty = tmp_path / "e.csv"
    empty.write_text("source,target\n", encoding="utf-8")
    assert terms.check_csv(empty)

    assert terms.check_csv(tmp_path / "nope.csv")      # 없는 파일


def test_cli_and_web_sign_glossaries_the_same_way():
    """지문이 갈리면 같은 책을 명령줄→브라우저로 이어받을 때 캐시가 통째로
    빗나간다. cli 는 (glossary, prompt), web 은 (glossary) 로 서명하고 있었다."""
    import inspect
    from pdfko import cli, web
    cli_sig = [l for l in inspect.getsource(cli._main).splitlines()
               if "Server.signature(" in l]
    web_sig = [l for l in inspect.getsource(web._run).splitlines()
               if "Server.signature(" in l]
    assert cli_sig and web_sig
    n = lambda l: l.count(",") + 1        # noqa: E731  인자 개수
    assert n(cli_sig[0]) == n(web_sig[0]), (cli_sig, web_sig)


def test_merge_refuses_a_truncated_chunk(tmp_path):
    """잘린 구간은 **쪽수가 맞는 빈 페이지**로 병합된다.

    pymupdf 가 페이지 트리를 복원해 주기 때문에 쪽수 검사를 통과하고, 뒤쪽
    검사도 전부 통과한다 — qa.coverage 는 글자가 적은 쪽을 판정 보류로
    넘기고 qa.inspect_page 도 마찬가지다. 빈 책이 "완료 · 파손 0쪽"으로
    나간다. 실측으로 절반 잘린 구간이 빈 페이지 3장이 됐다.
    """
    import pymupdf
    from pdfko import runner
    work = tmp_path / "w"
    (work / "parts").mkdir(parents=True)
    chunks = runner.plan_chunks(1, 3, 3, work)
    c = chunks[0]
    c.outdir.mkdir(parents=True, exist_ok=True)
    d = pymupdf.open()
    for i in range(3):
        d.new_page().insert_text((60, 100), f"본문 {i} " * 30, fontname="korea")
    f = c.outdir / "x.mono.pdf"
    d.save(f); d.close()

    assert runner.merge(chunks, tmp_path / "ok.pdf") == 3      # 온전하면 통과

    data = f.read_bytes()
    f.write_bytes(data[:len(data) // 2])                       # 절반으로 자른다
    with pytest.raises(RuntimeError) as ei:
        runner.merge(chunks, tmp_path / "bad.pdf")
    assert "1-3" in str(ei.value)                              # 어느 구간인지 말한다


def test_fragment_mode_rejects_runaway_length():
    """조각 모드가 길이를 안 보면 원문의 9배짜리 문단이 그대로 나간다.

    사전 조각 경로는 그것을 캐시에까지 넣어, 다시 돌려도 같은 결과가 나왔다.
    같은 텍스트를 통짜 경로에 넣으면 `width 9.41x` 로 거부된다.
    """
    from pdfko import proxy
    src = " ".join(f"col {{v{i}}} x" for i in range(1, 17))
    runs, phs = proxy.split_runs(src)
    long_ko = "이것은 아주 길게 늘어난 한국어 번역문이며 원문보다 훨씬 깁니다"
    rebuilt = "".join((long_ko if proxy.is_translatable(r) else r)
                      + (phs[k] if k < len(phs) else "")
                      for k, r in enumerate(runs))
    sw = proxy.est_width(src)
    assert sw >= 10
    assert proxy.est_width(rebuilt) / sw > proxy.WIDTH_MAX     # 거부 대상이다


def test_recovery_failures_carry_a_reason(tmp_path, monkeypatch):
    """복구가 실패한 것을 "원문 유지"라는 **판단**으로 보고하면 안 된다.

    예외를 던지는 경로만 이유를 남기고 있었다. 조용히 None 을 돌려주거나
    결과가 영어로 오는 두 경로는 여전히 도구가 판단해서 그렇게 한 것처럼
    보고됐다. 정직한 보고가 이 도구의 존재 이유다.
    """
    import pymupdf
    from pdfko import qa, recover

    orig = tmp_path / "o.pdf"
    d = pymupdf.open()
    for t in ("ONE", "TWO", "THREE"):
        d.new_page().insert_text((72, 200), t, fontsize=20)
    d.save(orig); d.close()

    def report_for(fake):
        trans = tmp_path / f"t{id(fake)}.pdf"
        shutil.copy(orig, trans)
        work = tmp_path / f"w{id(fake)}"
        (work / "logs").mkdir(parents=True)
        monkeypatch.setattr(recover, "retranslate_page", fake)
        v = qa.PageVerdict(page=2, words=200)
        v.overlap, v.reasons = 0.99, ["겹침99%"]
        recs = recover.repair_pages(trans, orig, [v], 0, orig, work,
                                    model="m", proxy_port=9, glossary=None)
        rep = work / "r.md"
        recover.write_report(rep, [v], recs, 0)
        return rep.read_text(encoding="utf-8")

    # (a) 예외 없이 None
    txt = report_for(lambda *a, **k: None)
    assert "복구 실패" in txt and "원문 유지 |" not in txt

    # (b) 영어 페이지를 돌려줌
    def english(*a, **k):
        f = tmp_path / "eng.pdf"
        d = pymupdf.open(); p = d.new_page(); y = 60
        for _ in range(20):
            p.insert_text((50, y), "This page is entirely English text", fontsize=9)
            y += 18
        d.save(f); d.close()
        return f
    txt = report_for(english)
    assert "한국어가 아닙니다" in txt


def test_marker_does_not_retrigger_reversion(tmp_path):
    """되돌린 표시를 검사기가 본문으로 세면 `--recheck` 마다 또 되돌린다.

    표시는 9낱말인데 본문 상자 밖이라 '영역이탈' 로 잡힌다. 낱말이 적은
    페이지에서는 31%를 차지해 계속 심각 판정을 받았다.
    """
    import pymupdf
    from pdfko import qa, recover
    src = tmp_path / "s.pdf"
    d = pymupdf.open(); p = d.new_page(); y = 80
    for i in range(0, 20, 8):
        p.insert_text((60, y), " ".join(f"word{j}" for j in range(i, i + 8)),
                      fontsize=10)
        y += 16
    d.save(src); d.close()

    marked = tmp_path / "m.pdf"
    d = pymupdf.open(src); recover._mark_reverted(d[0]); d.save(marked); d.close()

    with pymupdf.open(src) as o, pymupdf.open(marked) as t:
        v = qa.inspect_page(o[0], t[0], 1)
    assert not v.broken, v.reasons          # 표시 때문에 다시 파손이 되면 안 된다


def test_truncated_json_is_recovered_not_discarded():
    """닫는 괄호 하나 때문에 멀쩡한 번역을 버리면 안 된다.

    실측으로 모델이 **완전하고 올바른** 번역을 내놓고(자리표시자 7개 전부
    보존, 폭 0.77배, check() 통과) 마지막 `]` 만 빠뜨렸다. 그걸 버려서 세 번의
    재시도가 전부 실패하고 조각 모드로 떨어졌고, 사람이 읽을 수 없는
    `v∗흥미로운 점은` 이 페이지에 실렸다.
    """
    from pdfko import proxy
    want = [{"id": 0, "output": "{v1} 에 대한 번역"}]
    full = '[{"id": 0, "output": "{v1} 에 대한 번역"}]'
    for cut in (1, 2, 3):                       # ] / }] / "}]
        assert proxy.parse_output(full[:-cut]) == want, cut
    # 내용을 지어내지는 않는다
    assert proxy.parse_output("죄송합니다, 번역할 수 없습니다") is None
    assert proxy.parse_output('[{"id":0,"outp') is None


def test_malformed_style_tag_without_id_is_caught():
    """`id=` 가 빠진 `<style '5'>` 가 정상 태그로 세어져 통과했다.

    엔진이 인식하지 못하므로 그 문자열이 **본문에 글자 그대로 찍힌다.**
    실측으로 논문 한 페이지에 7개가 인쇄됐다.
    """
    from pdfko import proxy
    assert proxy.STYLE_RE.match("<style id='5'>")
    assert proxy.STYLE_RE.match("</style>")
    assert not proxy.STYLE_RE.match("<style '5'>")

    src = "see (Konda <style id='5'>and</style> Tsitsiklis) for the two-scale"
    bad = "이중 스케일에 대해서는 (Konda<style '5'>, 치치클리스) 를 참고한다"
    ok, why = proxy.check([{"id": 0, "input": src}], [{"id": 0, "output": bad}])
    assert not ok and "style" in why


def test_existing_glossary_is_not_overwritten(tmp_path, monkeypatch):
    """사용자가 손본 용어집을 다음 실행이 지우면 안 된다.

    README 는 "마음에 안 드는 역어가 있으면 그 파일을 고쳐서 다시 넘기면
    됩니다" 라고 안내하는데, 정작 그냥 다시 돌리면 말없이 덮어썼다.
    실측으로 손으로 고친 역어가 사라졌다.
    """
    import inspect
    from pdfko import cli
    src = inspect.getsource(cli._main)
    # 이미 있으면 그대로 쓰고, --fresh 일 때만 새로 만든다
    assert "kept_glossary" in src
    assert 'unlink(missing_ok=True)   # 용어집도 새로' in src


def test_engine_cache_is_bypassed_not_deleted():
    """엔진 캐시를 지우면 동시에 도는 다른 실행의 DB 까지 날린다.

    `~/.cache/babeldoc` 는 계정에 하나뿐이다. 실측으로 다른 실행이
    `(deleted)` 핸들을 쥔 채 도는 것이 관찰됐고, WAL 짝이 어긋나면 SQLite 가
    깨진다. pdfko 밖에서 babeldoc 을 쓰는 사람의 캐시까지 날아간다.
    이번 실행만 안 쓰면 목적은 똑같이 이룬다.
    """
    import inspect
    from pdfko import recover, runner
    assert "--ignore-cache" in inspect.getsource(runner.translate_chunk)
    assert "--ignore-cache" in inspect.getsource(recover.retranslate_page)
    assert not hasattr(runner, "ENGINE_CACHE")     # 지울 대상 자체가 없다


def test_model_store_is_ollama_default_not_ours():
    """우리만의 폴더를 쓰면 저장소가 갈라져 같은 6GB 가 두 벌 생긴다.

    사용자가 `ollama serve` 를 이미 띄워 놨으면 그 서버는 자기 저장소를
    보는데, 우리가 나중에 서버를 띄우면 우리 폴더를 보며 "모델이 없다"고
    한다. 다시 등록하면 사본이 하나 더 생긴다. 실측으로 이 컴퓨터에 같은
    blob 해시를 가진 저장소가 두 개, 11.6GB 쌓여 있었다.
    """
    from pathlib import Path
    from pdfko import runner
    assert runner.MODEL_STORE == Path.home() / ".ollama" / "models"
    # 작업 폴더 안이면 책마다 한 벌씩 생긴다
    for probe in (Path("/tmp/a_ko"), Path("/tmp/b_ko")):
        assert probe not in runner.MODEL_STORE.parents


def test_ensure_model_does_not_pretend_to_pick_the_store():
    """`OLLAMA_MODELS` 를 클라이언트에서 정해 봐야 소용없다.

    `ollama create`/`list` 는 서버 쪽 동작이라 클라이언트 환경변수를 보지
    않는다. 실측으로 없는 경로를 줘도 서버 저장소 목록이 그대로 나왔다.
    그 줄이 남아 있으면 통제하지 못하는 것을 통제하는 척하게 된다.
    """
    import inspect
    from pdfko import runner
    # 주석에 낱말이 나오는 것은 괜찮다 — **대입**이 없어야 한다
    src = inspect.getsource(runner.ensure_model)
    assert 'env["OLLAMA_MODELS"]' not in src


def test_fragment_mode_is_reported_not_just_counted(tmp_path):
    """조각 모드로 번역한 문단을 사용자에게 알려야 한다.

    조각 모드는 어순을 조각 단위로 굳혀서, 수식이 그것을 설명하는 구절에서
    떨어져 나간다. 품질 검증이 "수식 어긋남의 85%가 여기서 나온다" 고
    지목한 곳인데, 세기만 하고 보고서는 침묵하면서 "파손 0쪽" 이라고
    말하고 있었다. 레이아웃 검사로는 안 잡힌다 — 좌표는 멀쩡하기 때문이다.
    """
    import json
    from pdfko import qa, recover
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fragments.jsonl").write_text(
        "\n".join(json.dumps({"why": "heavy", "src": f"{{v1}}term {i}"})
                  for i in range(3)) + "\n", encoding="utf-8")
    rep = tmp_path / "r.md"
    recover.write_report(rep, [qa.PageVerdict(page=1, words=100)], [], 0,
                         log_dir=logs)
    txt = rep.read_text(encoding="utf-8")
    assert "어순이 고정된 문단" in txt
    assert "3개" in txt

    # 조각 모드를 안 썼으면 그 절은 나오지 않는다
    rep2 = tmp_path / "r2.md"
    recover.write_report(rep2, [qa.PageVerdict(page=1, words=100)], [], 0,
                         log_dir=tmp_path / "nologs")
    assert "어순이 고정된 문단" not in rep2.read_text(encoding="utf-8")
