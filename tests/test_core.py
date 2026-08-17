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
    with pytest.raises(RuntimeError, match="쪽수가 모자란다"):
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
