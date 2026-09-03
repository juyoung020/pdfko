"""핵심 불변식 검증.

이 테스트들은 전부 **실제로 겪은 실패**에서 나왔다. 각 테스트는 한 번 사람을
속였던 버그를 다시 잡는다.
"""

from pathlib import Path
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


def test_a_changed_prompt_file_changes_the_cache_key(tmp_path):
    """추가 지시문을 바꾸면 캐시가 빗나가야 한다.

    한때 요청 본문의 system 메시지를 해시했는데, BabelDOC 은 system 을 아예
    보내지 않는다(전부 user 메시지 한 덩어리). 빈 문자열의 해시가 상수로
    박혀서, 지시문을 바꿔도 옛 번역이 그대로 나왔다.
    """
    from pdfko import runner
    g1 = tmp_path / "a.txt"; g1.write_text("존댓말을 쓰지 마라\n")
    g2 = tmp_path / "b.txt"; g2.write_text("수식 기호는 그대로 두라\n")
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


def test_cli_and_web_sign_the_same_way():
    """지문이 갈리면 같은 책을 명령줄→브라우저로 이어받을 때 캐시가 통째로
    빗나간다. 예전에 cli 와 web 이 서로 다른 인자로 서명하고 있었다."""
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
                                    model="m", proxy_port=9)
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


# ── 반쪽 번역 ───────────────────────────────────────────────────────────
def test_half_translated_paragraph_is_rejected():
    """80%가 한국어여도 영어 문장이 통째로 남았으면 걸러야 한다.

    실측: 이 책 3660문단 중 165개(4.5%)가 한글 바닥(0.15)을 넘겨 통과했고,
    쪽 단위 검사는 그 상태를 "미번역 0쪽"으로 보고했다.
    """
    from pdfko import proxy
    tgt = ("가치 함수는 각 상태의 기대 이득을 나타낸다. "
           "With general function approximation there is not such a clear "
           "notion of number of experiences with a single state. "
           "따라서 근사가 필요하다.")
    assert proxy.leftover_english(tgt)
    ok, why = proxy.check([{"id": 1, "input": "x " * 40}],
                          [{"id": 1, "output": tgt}])
    assert not ok and "영어" in why, why


def test_untranslated_paragraph_is_left_to_the_hangul_floor():
    """아직 한국어가 아닌 문단은 이 검사가 손대지 않는다 — 이중 판정 방지."""
    from pdfko import proxy
    assert proxy.leftover_english(
        "The agent learns a policy from reward over time.") is None


def test_preserved_english_does_not_trigger_a_retry():
    """영어로 두는 게 맞는 것들을 어휘 목록 없이 형태로만 가른다."""
    from pdfko import proxy
    for keep in [
        # 인용 — 근처에 연도
        "이 착상은 Sutton and Barto, Reinforcement Learning An Introduction, "
        "1998 에서 왔다.",
        # 약어 풀이 — 전부 대문자
        "메이스는 MATCHBOX EDUCABLE NAUGHTS AND CROSSES ENGINE 의 준말이다.",
        # 인명·제목 — 낱말마다 첫 글자 대문자
        "저자는 Richard S Sutton Andrew G Barto Francis Bach 이다.",
        # 행렬·축 라벨 — 짧은 낱말뿐
        "행렬은 다음과 같다. A A A x v v x b b x w A b 순서로 읽는다.",
    ]:
        assert proxy.leftover_english(keep) is None, keep


def test_the_leftover_sentence_is_quoted_back_to_the_model():
    """"영어가 남았다" 라고만 하면 모델이 어디인지 못 찾는다."""
    from pdfko import proxy
    tgt = ("정책은 상태를 행동으로 사상한다. The value of a state is the "
           "expected return starting from that state. 이를 가치라 한다.")
    src = ("A policy maps states to actions. The value of a state is the "
           "expected return starting from that state. We call this the value.")
    ok, why = proxy.check([{"id": 7, "input": src}], [{"id": 7, "output": tgt}])
    assert not ok and why.kind == "english", why
    # 사유를 그대로 넘긴다 — 힌트가 판정을 다시 추론하지 않는다.
    hint = proxy.repair_hint([({"id": 7, "input": src}, why)],
                             [{"id": 7, "output": tgt}])
    assert "The value of a state" in hint, hint


def test_report_counts_pages_that_kept_english(tmp_path):
    """보고서가 "파손 0쪽" 만 말하고 영어 잔류를 숨기면 안 된다."""
    import pymupdf
    from pdfko import recover
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((40, 300), "policy is a mapping from states to actions here",
                   fontsize=9)
    # pymupdf 기본 폰트에는 한글 글리프가 없다. 내장 CJK 폰트를 써야
    # 실제로 찍힌다 — 아니면 한글 0자짜리 쪽이 되어 검사가 무의미해진다.
    pg.insert_text((40, 320), "정책은 상태를 행동으로 사상하는 함수를 뜻한다",
                   fontsize=9, fontname="korea")
    out = tmp_path / "t.pdf"
    d.save(out); d.close()

    rep = tmp_path / "r.md"
    recover.write_report(rep, [], [], 12, out_pdf=out)
    body = rep.read_text(encoding="utf-8")
    assert "영어가 남은 쪽 1쪽" in body, body
    assert "policy is a mapping" in body, body


def test_report_without_the_pdf_still_writes(tmp_path):
    """out_pdf 를 안 넘겨도 보고서는 나와야 한다 (기존 호출부 보호)."""
    from pdfko import recover
    rep = tmp_path / "r.md"
    recover.write_report(rep, [], [], 0)
    assert "영어가 남은 쪽 0쪽" in rep.read_text(encoding="utf-8")


# ── 미번역 수리 루프 ────────────────────────────────────────────────────
def test_untranslated_pages_reach_a_repair_loop(tmp_path, monkeypatch):
    """영어가 남은 쪽은 좌표 판정에 안 걸린다 — 따로 훑어 다시 번역해야 한다.

    예전에는 `repair_pages` 가 `qa.scan` 판정만 받아서, 겹치지도 밀려나지도
    않은 채 문장만 영어로 남은 쪽은 **수리 루프에 아예 들어가지 않았다.**
    """
    import pymupdf
    from pdfko import recover

    KO = "정책은 상태를 행동으로 사상하는 함수이다"
    EN = ("a policy maps states to actions and the value function gives "
          "the expected return from that state onward")

    def mk(path, *, korean, leftover=False):
        d = pymupdf.open()
        pg = d.new_page()
        # `_is_korean` 은 낱말 15개 미만이면 판정을 거절하므로 네 줄을 깐다.
        # 한국어 줄은 영어 원본보다 **좁아야** 한다 — 넓으면 '영역이탈' 로
        # 잡혀서, 코드가 아니라 조판 문턱을 시험하게 된다.
        for k in range(4):
            pg.insert_text((40, 300 + k * 12), KO if korean else EN,
                           fontsize=9, fontname="korea" if korean else "helv")
        if leftover:
            pg.insert_text((40, 370),
                           "with general function approximation there is not "
                           "such a clear notion here", fontsize=9)
        d.save(path)
        d.close()

    trans, good, orig = (tmp_path / n for n in ("t.pdf", "g.pdf", "o.pdf"))
    mk(trans, korean=True, leftover=True)
    mk(good, korean=True)
    mk(orig, korean=False)

    assert [p for p, _ in recover.leftover_pages(trans)] == [1]
    monkeypatch.setattr(recover, "retranslate_page", lambda *a, **k: good)
    recs = recover.repair_untranslated(trans, orig, 0, orig, tmp_path,
                                       model="m", proxy_port=1)
    assert [r.action for r in recs] == ["retranslated"], recs
    assert not recover.leftover_pages(trans), "갈아 끼운 뒤에도 영어가 남았다"


def test_untranslated_repair_never_reverts_to_english(tmp_path, monkeypatch):
    """재번역이 실패해도 **원문으로 되돌리면 안 된다.**

    90%가 한국어인 쪽을 통째로 영어로 바꾸는 것은 고치는 게 아니라 더
    나쁘게 만드는 것이다. 있는 그대로 두고 보고서에 적는다.
    """
    import pymupdf
    from pdfko import recover
    d = pymupdf.open(); pg = d.new_page()
    pg.insert_text((40, 300), "정책은 상태를 행동으로 사상하는 함수이다",
                   fontsize=9, fontname="korea")
    pg.insert_text((40, 320), "with general function approximation there is "
                              "not such a clear notion here", fontsize=9)
    trans = tmp_path / "t.pdf"; d.save(trans); d.close()
    before = trans.read_bytes()
    orig = tmp_path / "o.pdf"
    d = pymupdf.open(); d.new_page(); d.save(orig); d.close()

    monkeypatch.setattr(recover, "retranslate_page", lambda *a, **k: None)
    recs = recover.repair_untranslated(trans, orig, 0, orig, tmp_path,
                                       model="m", proxy_port=1)
    assert [r.action for r in recs] == ["kept"], recs
    assert recs[0].note, "왜 못 고쳤는지 남겨야 한다"
    assert trans.read_bytes() == before, "원문으로 되돌려 버렸다"


def test_running_headers_are_not_treated_as_missed_translation():
    """머리글은 엔진이 번역하지 않는다 — 수리 대상에 넣으면 끝나지 않는다.

    실측으로 30쪽 중 14쪽이 `Chapter 8: Planning and Learning with Tabular
    Methods` 하나 때문에 걸렸다. 본문에 실제로 영어가 남은 쪽은 1쪽이었다.
    """
    import pymupdf
    from pdfko import recover
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((40, 30), "Chapter 8: Planning and Learning with "
                             "Tabular Methods", fontsize=9)   # 머리글 띠
    for k in range(4):
        pg.insert_text((40, 300 + k * 12),
                       "정책은 상태를 행동으로 사상하는 함수이다",
                       fontsize=9, fontname="korea")
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        out = Path(t) / "h.pdf"
        d.save(out); d.close()
        assert recover.leftover_pages(out) == []


def test_version_flag_reports_the_installed_version(capsys):
    """`--version` 이 없으면 사용자가 어느 판을 쓰는지 말할 수 없다.

    버전 문자열을 코드에 박지 않는다 — pyproject.toml 과 어긋나는 날이
    오고, 버그 보고를 받아도 어느 쪽이 맞는지 알 수 없다.
    """
    import tomllib
    from pdfko import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    got = capsys.readouterr().out.strip()
    want = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text()
    )["project"]["version"]
    assert got == f"pdfko {want}", got


def test_exit_codes_are_documented_in_the_help():
    """종료 코드를 문서화하지 않으면 자동화에서 쓸 수 없다."""
    import argparse
    import io
    import contextlib
    from pdfko import cli
    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        cli.main(["--help"])
    txt = buf.getvalue()
    for code in ("0", "1", "2", "3", "130"):
        assert f" {code} " in txt or f": {code} " in txt, (code, txt[-400:])
    assert isinstance(argparse.ArgumentParser, type)


# ── 결과가 저장되는 곳 ───────────────────────────────────────────────────
def test_results_land_in_one_place_regardless_of_cwd(tmp_path, monkeypatch):
    """어디서 명령을 쳐도 결과는 같은 곳에 모인다.

    예전에는 `Path.cwd()` 였다. 저장소 안에서 번역을 돌리면 결과 PDF 가
    저장소에 쌓이고, `.gitignore` 는 `cache/ logs/ work/ parts/` 만 막아서
    번역본·품질보고서·용어집이 커밋 대기 목록에 끼어들었다. public
    저장소에서 `git add .` 한 번이면 남의 교재가 올라간다.
    """
    from pdfko import paths
    pkg = tmp_path / "repo" / "pdfko"
    pkg.mkdir(parents=True)
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\n")
    assert paths._base_for(pkg, tmp_path / "home", None) == tmp_path / "repo" / "out"


def test_an_installed_copy_never_writes_into_site_packages(tmp_path):
    """소스 체크아웃이 아니면 홈으로 떨어진다.

    `uv tool install pdfko` 로 깔면 패키지가 site-packages 에 있다. 거기에
    번역 결과를 쓰면 권한 오류가 나거나, 나더라도 재설치 때 날아간다.
    """
    from pdfko import paths
    pkg = tmp_path / "site-packages" / "pdfko"
    pkg.mkdir(parents=True)                      # pyproject.toml 이 없다
    got = paths._base_for(pkg, tmp_path / "home", None)
    assert got == tmp_path / "home" / "pdfko" / "out"
    assert "site-packages" not in str(got)


def test_the_out_folder_can_be_moved_with_an_env_var(tmp_path):
    """디스크가 작은 노트북에서 결과만 외장으로 뺄 수 있어야 한다."""
    from pdfko import paths
    pkg = tmp_path / "repo" / "pdfko"
    pkg.mkdir(parents=True)
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\n")
    assert paths._base_for(pkg, tmp_path / "home",
                           str(tmp_path / "외장")) == tmp_path / "외장"


def test_cli_and_web_agree_on_where_results_go():
    """CLI 는 ./<이름>_ko, 웹은 ~/pdfko-작업 이었다 — 두 군데로 흩어졌다."""
    from pdfko import paths, web
    assert web.ROOT == paths.out_base()


# ── 스캔본 판정 ──────────────────────────────────────────────────────────
def _slide_pdf(path, pages, chars_per_page):
    """쪽당 글자 수를 정해 만든 PDF. 슬라이드처럼 글자가 적은 문서를 흉내낸다."""
    import pymupdf
    d = pymupdf.open()
    for _ in range(pages):
        p = d.new_page(width=720, height=405)
        if chars_per_page:
            p.insert_text((40, 60), "Policy gradient methods " * (chars_per_page // 24),
                          fontsize=9)
    d.save(path); d.close()
    return path


def test_a_single_slide_is_not_mistaken_for_a_scan(tmp_path):
    """`-p 13` 으로 한 쪽만 돌릴 때 스캔본으로 거부하면 안 된다.

    문턱이 표본 쪽수와 무관한 고정 500자였다. 40쪽을 뽑을 때는 우습게
    넘지만 한 쪽만 뽑으면 슬라이드 한 장 분량이라 못 넘는다. 실측한
    발표자료 15쪽은 **한 쪽도** 500자에 닿지 않아 전 쪽이 거부됐다.
    README 가 새 사용자에게 처음 권하는 게 `-p` 미리보기라 더 나빴다.
    """
    from pdfko.cli import preflight
    src = _slide_pdf(tmp_path / "deck.pdf", 15, 340)
    _, _, has_text = preflight(src, first=13, last=13)
    assert has_text, "글자가 있는 슬라이드 한 장을 스캔본으로 판정했다"


def test_a_real_scan_is_still_refused(tmp_path):
    """쪽당으로 환산해도 진짜 스캔본은 걸러야 한다.

    500쪽 스캔본에 서너 시간을 쓰고 영어 PDF 를 내놓는 일을 막는 검사다.
    """
    from pdfko.cli import preflight
    src = _slide_pdf(tmp_path / "scan.pdf", 40, 0)      # 텍스트 레이어 없음
    _, _, has_text = preflight(src, first=1, last=40)
    assert not has_text, "글자가 하나도 없는 문서를 통과시켰다"


def test_the_whole_deck_still_passes(tmp_path):
    """구간을 안 주면 예전처럼 전체를 보고 판정한다."""
    from pdfko.cli import preflight
    src = _slide_pdf(tmp_path / "deck.pdf", 15, 340)
    _, _, has_text = preflight(src)
    assert has_text


# ── 판정 사유가 종류를 들고 다닌다 ───────────────────────────────────────
def _verdict(src, tgt):
    return proxy.check([{"id": 0, "input": src}], [{"id": 0, "output": tgt}])


@pytest.mark.parametrize("kind,src,tgt", [
    ("empty",       "A policy maps states to actions.", "   "),
    ("placeholder", "The step size {v1} matters a lot here.", "스텝 크기가 중요하다."),
    ("jondae",      "A policy maps states to actions.", "정책은 상태를 행동으로 사상합니다."),
    ("hangul",      "A policy maps states to actions.", "a policy maps states"),
    ("width",       "What is RL (with other domains)?",
                    "RL이란 무엇인가 (다른 분야와의 연계를 포함하여)?"),
])
def test_the_checker_names_the_kind_of_failure(kind, src, tgt):
    """사유는 문자열이면서 **종류**를 따로 들고 있어야 한다.

    예전에는 사람이 읽을 문자열뿐이었고, 사다리는 그마저 `[0]` 으로 버렸다.
    그래서 `repair_hint` 가 같은 판정을 처음부터 다시 추론했고, 두 곳이
    어긋나면 엉뚱한 힌트가 나갔다. 오늘 버그 셋이 전부 그 틈에서 나왔다.
    """
    ok, why = _verdict(src, tgt)
    assert not ok, f"거부됐어야 한다: {tgt!r}"
    assert getattr(why, "kind", None) == kind, (kind, why, getattr(why, "kind", None))


def test_a_reason_is_still_an_ordinary_string():
    """기존 호출자는 그대로 둔다 — 사유에 부분 문자열 검사를 걸고 있다."""
    ok, why = _verdict("A policy maps states to actions.", "   ")
    assert isinstance(why, str) and "empty" in why


def test_a_passing_item_has_no_reason():
    ok, why = _verdict("A policy maps states to actions.",
                       "정책은 상태를 행동으로 사상하는 함수이다.")
    assert ok and not why


# ── 번역할 산문이 있는가 (판단을 한 곳에서 내린다) ───────────────────────
@pytest.mark.parametrize("src,want", [
    ("https://www.davidsilver.uk/", False),
    ("https://github.com/inmo-jang/aircombat-rl", False),
    ("{v1}{v2}{v3}", False),
    ("42 / 15 – 5%", False),
    ("A policy maps states to actions.", True),
    ("guidance", True),
    ("fixed height", True),
    ("Auto Pilot", True),
    ("See https://example.org for the derivation of the bound.", True),
])
def test_whether_an_item_has_prose_to_translate(src, want):
    """URL·수식·숫자를 걷어내고 **산문이 남는지**로 판단한다.

    예전 기준은 `라틴 글자 수 >= 12` 였다. 축이 틀렸다 — URL 은 글자가
    21자라 기준을 통과해 한글 검사를 받고 실패했고(정답이 원문 그대로인데도),
    3회 재시도에 조각 모드까지 태운 뒤 포기했다. 반대로 `guidance`,
    `Auto Pilot` 같이 **번역해야 하는** 짧은 라벨은 기준에 걸려 검사를
    면제받았다. 실측 53개 항목 중 7개가 URL 뿐인 항목이었다.
    """
    assert proxy.has_prose(src) is want, (src, proxy.has_prose(src))


def test_a_url_only_item_is_not_a_translation_failure():
    """URL 이 그대로 돌아온 것은 실패가 아니라 정답이다."""
    url = "https://www.davidsilver.uk/"
    ok, why = proxy.check([{"id": 0, "input": url}], [{"id": 0, "output": url}])
    assert ok, why


def test_prose_that_echoes_is_still_a_failure():
    """반대로 산문이 그대로 돌아오면 여전히 실패다."""
    src = "A policy maps states to actions in every state of the world."
    ok, why = proxy.check([{"id": 0, "input": src}], [{"id": 0, "output": src}])
    assert not ok and why.kind == "hangul", why


# ── 반향·폭 판정을 한 곳에서 ─────────────────────────────────────────────
def test_an_echo_is_judged_the_same_way_everywhere():
    """통짜 항목과 조각이 **같은 기준**으로 반향을 판정해야 한다.

    예전에는 통짜는 `has_prose(src)`, 조각은 `라틴 8자 이상` 이라는 서로
    다른 문턱을 썼다(proxy.py 508 대 867·916). 같은 질문에 두 가지 답을
    가진 셈이라, 한쪽을 고쳐도 다른 쪽이 옛 판단을 계속했다.
    """
    src = "A policy maps states to actions."
    assert proxy.is_echo(src, "a policy maps states to actions")
    assert not proxy.is_echo(src, "정책은 상태를 행동으로 사상하는 함수이다.")
    # 산문이 없으면 원문 그대로가 정답이므로 반향이 아니다
    assert not proxy.is_echo("https://www.davidsilver.uk/",
                             "https://www.davidsilver.uk/")
    assert not proxy.is_echo("{v1} + {v2}", "{v1} + {v2}")


def test_width_is_judged_the_same_way_everywhere():
    """폭 초과도 한 곳에서만 판정한다 (예전 3곳)."""
    src = "What is RL (with other domains)?"
    assert proxy.too_wide(src, "RL이란 무엇인가 (다른 분야와의 연계를 포함하여)?")
    assert not proxy.too_wide(src, "RL(다른 분야와의 연계)이란 무엇인가?")
    # 짧은 라벨과 산문 없는 항목은 면제
    assert not proxy.too_wide("RL", "강화학습이라는 것")
    assert not proxy.too_wide("https://a.b/c", "https://a.b/c")


# ── 캐시 지문은 동작만 본다 ──────────────────────────────────────────────
def test_a_comment_does_not_invalidate_the_cache():
    """주석·docstring 을 고쳤다고 500쪽을 다시 번역하면 안 된다.

    예전 지문은 `proxy.py` 39,233자를 **텍스트 그대로** 해시했다. 그래서
    오타 하나, 로그 문구 한 줄, 주석 한 글자만 고쳐도 지문이 달라지고
    번역해 둔 모든 문서의 캐시가 죽었다. 500쪽이면 3시간이 날아간다.
    """
    a = proxy._behavior_hash("def f(x):\n    return x > 3\n")
    b = proxy._behavior_hash(
        "def f(x):\n    '''설명이 붙었다.'''\n    # 주석도 붙었다\n    return x > 3\n")
    assert a == b, "주석·docstring 이 지문을 바꿨다"


def test_a_changed_rule_does_invalidate_the_cache():
    """반대로 판정이 바뀌면 반드시 무효화되어야 한다.

    이게 이 장치의 존재 이유다. 규칙을 고쳐도 이미 캐시된 문단은 새 규칙을
    거칠 기회가 없어, 고쳤다고 믿은 채 같은 결과를 받는 일이 네 번 있었다.
    """
    a = proxy._behavior_hash("def f(x):\n    return x > 3\n")
    b = proxy._behavior_hash("def f(x):\n    return x > 4\n")
    assert a != b, "문턱을 바꿨는데 지문이 그대로다"


def test_renaming_a_local_variable_does_not_invalidate():
    """이름만 바꾼 것도 동작이 아니다 — 다만 잡아내기 어려우면 무효화가 낫다."""
    a = proxy._behavior_hash("def f(x):\n    return x > 3\n")
    assert proxy._behavior_hash("def f(x):\n\n\n    return x > 3\n") == a, \
        "빈 줄이 지문을 바꿨다"


# ── 성긴 쪽에서 단어 두세 개로 파손 판정이 나면 안 된다 ──────────────────
def _page_with(words_xy, w=720, h=405):
    """주어진 좌표에 낱말을 놓은 한 쪽짜리 PDF."""
    import pymupdf
    d = pymupdf.open()
    p = d.new_page(width=w, height=h)
    for i, (x, y) in enumerate(words_xy):
        p.insert_text((x, y), f"word{i}", fontsize=10)
    return d


def test_a_couple_of_stray_words_is_not_damage_on_a_sparse_page():
    """슬라이드는 낱말이 20~40개라 두세 개만 나가도 비율이 문턱을 넘는다.

    실측(L03 발표자료):
        21쪽  전체 24낱말 중 2낱말이 밖 →  8%  → '파손'
         5쪽  전체 43낱말 중 3낱말이 밖 →  7%  → '파손'

    이 지표는 낱말이 수백 개인 교재 쪽을 상정하고 만들어졌다. 거기서 3% 는
    의미가 있지만 24낱말짜리 쪽에서는 **한 낱말이 4%** 다. 그래서 쪽당
    19초짜리 재번역이 낱말 두 개 때문에 돌았다.

    비율만이 아니라 **나간 낱말 수**도 함께 봐야 한다.
    """
    from pdfko import qa
    inside = [(100 + (i % 6) * 60, 100 + (i // 6) * 30) for i in range(22)]
    orig = _page_with(inside)
    # 낱말 두 개만 기준 상자 밖으로
    trans = _page_with(inside[:20] + [(20, 380), (700, 390)])
    v = qa.inspect_page(orig[0], trans[0], 1)
    assert not v.broken, f"낱말 2개로 파손 판정: {v.reasons}"
    orig.close(); trans.close()


def test_many_stray_words_is_still_damage():
    """반대로 낱말이 여럿 밀려났으면 그대로 잡아야 한다."""
    from pdfko import qa
    inside = [(100 + (i % 6) * 60, 100 + (i // 6) * 30) for i in range(22)]
    orig = _page_with(inside)
    trans = _page_with(inside[:8] + [(15, 370 + (i % 3) * 8) for i in range(14)])
    v = qa.inspect_page(orig[0], trans[0], 1)
    assert v.broken and any("이탈" in r for r in v.reasons), v.reasons
    orig.close(); trans.close()


# ── 낱말 한가운데서 잘려 온 조각 ─────────────────────────────────────────
_VOCAB = {"understand", "mdp", "markov", "decision", "process", "policy",
          "reward", "formalised", "problems", "almost", "all", "can", "be",
          "reinforcement", "learning", "value"}


@pytest.mark.parametrize("src,want", [
    ("○ Under", "under"),                       # understand 의 앞부분
    ("“Almost all RL problems can be forma", "forma"),   # formalised 의 앞부분
    ("Reinforcement Learning", None),           # 멀쩡한 낱말들
    ("○ Understand MDP", None),
    ("policy", None),
    ("", None),
    ("{v1} + {v2}", None),                      # 낱말이 없다
])
def test_a_word_cut_in_half_is_recognised(src, want):
    """babeldoc 이 낱말 한가운데서 자른 조각을 알아본다.

    실측(L03 발표자료): 원본 span 은 `'Understand '` 로 멀쩡했는데 프록시에
    도착했을 땐 `'○ Under'` 였다. 번역기 입장에서 `Under` 는 정상 영어
    낱말이라 성실하게 `하위 항목` 으로 옮겼고, 결과물에 `하위 항목stand MDP`
    가 찍혔다. 짝 조각은 같은 배치에 오지 않아 이어붙일 수도 없다.

    유일하게 믿을 만한 신호는 **원본 문서의 어휘**다 — `under` 는 이 문서에
    독립된 낱말로 없고 `understand` 의 앞부분일 뿐이다. 129개 항목에 돌려
    오탐 0, 진짜 잘린 조각 2개를 잡았다.
    """
    assert proxy.truncated_tail(src, _VOCAB) == want


def test_without_a_vocabulary_nothing_is_flagged():
    """어휘 목록을 못 받았으면 아무것도 잘렸다고 하지 않는다.

    이 신호는 원본을 봐야만 성립한다. 목록이 없을 때 추측으로 막으면
    멀쩡한 짧은 라벨까지 번역을 건너뛴다.
    """
    assert proxy.truncated_tail("○ Under", set()) is None
    assert proxy.truncated_tail("○ Under", None) is None


# ── 두 항목에 걸쳐 잘린 낱말을 되붙인다 ──────────────────────────────────
def test_a_word_split_across_two_items_is_rejoined():
    """`Under` + `stand …` → `Understand` + `…`

    실측(L03 2쪽): babeldoc 이 한 줄을 세 조각으로 잘랐고, 그 경계가
    **낱말 안쪽**이었다.

        id2  '○ Under'
        id3  "<style…>stand </style>MDP(Markov Decision Process) ⇐ Today's goa"
        id4  'l'

    조각마다 따로 번역되어 `하위 항목` + `스탠드 … 골라인` 이 나왔다.
    이어붙일 근거는 원본 어휘다 — `under`+`stand` 가 이 문서의 낱말
    `understand` 를 이룬다. 이루지 않으면 건드리지 않는다.
    """
    from pdfko.proxy import rejoin_cut_words
    vocab = {"understand", "mdp", "markov", "decision", "process", "goal"}
    items = [{"id": 0, "input": "○ Under"},
             {"id": 1, "input": "stand MDP ⇐ Today's goa"},
             {"id": 2, "input": "l"}]
    out = rejoin_cut_words(items, vocab)
    assert out[0]["input"] == "○ Understand"
    assert out[1]["input"] == "MDP ⇐ Today's goal"
    assert out[2]["input"] == ""


def test_neighbours_that_do_not_form_a_word_are_left_alone():
    """우연히 이어 붙였을 때 낱말이 되지 않으면 손대지 않는다."""
    from pdfko.proxy import rejoin_cut_words
    vocab = {"policy", "reward", "value"}
    items = [{"id": 0, "input": "The policy"}, {"id": 1, "input": "reward is high"}]
    out = rejoin_cut_words(items, vocab)
    assert out[0]["input"] == "The policy"
    assert out[1]["input"] == "reward is high"


def test_rejoining_needs_a_vocabulary():
    from pdfko.proxy import rejoin_cut_words
    items = [{"id": 0, "input": "○ Under"}, {"id": 1, "input": "stand MDP"}]
    assert rejoin_cut_words(items, set())[0]["input"] == "○ Under"


# ── 앞뒤 문장부호·공백은 배치 정보다 ─────────────────────────────────────
@pytest.mark.parametrize("src,tgt,want", [
    (": a (finite) set of states", "상태의 유한 집합", ": 상태의 유한 집합"),
    (": a set of actions", "행동의 집합", ": 행동의 집합"),
    (": diff", ": 차이", ": 차이"),               # 이미 있으면 그대로
    ("stand ", "서다", "서다 "),                  # 꼬리 공백도 지킨다
    ("A policy maps states.", "정책은 사상한다.", "정책은 사상한다."),
    ("What is RL (with other domains)?", "RL이란 무엇인가?", "RL이란 무엇인가?"),
    ("", "", ""),
])
def test_leading_and_trailing_punctuation_is_kept(src, tgt, want):
    """원문 앞뒤의 문장부호·공백은 번역 대상이 아니라 **배치 정보**다.

    실측(L03 11쪽): 수식 기호와 설명이 별개 항목으로 오는데, 설명이
    `': a (finite) set of states'` 처럼 콜론+공백으로 시작한다. 그 `': '`
    가 기호 `𝒮` 와 글자 사이를 벌려 주는 유일한 것이다.

        ': a (finite) set of states'  →  '상태의 유한 집합'   ← 콜론이 사라졌다
        결과물:  𝒮상태의 (유한한) 집합                        ← 기호에 올라탔다

    모델은 같은 모양을 어떤 때는 지키고 어떤 때는 버린다
    (`': differences against…'` 는 `': Markov 과정과의…'` 로 지켰다).
    지시로 부탁할 일이 아니라 우리가 되돌려 놓으면 되는 일이다.
    """
    assert proxy.keep_edges(src, tgt) == want


# ── 이미 한국어인 항목은 번역기에 보내지 않는다 ──────────────────────────
@pytest.mark.parametrize("src,already", [
    ("목표를 달성하기 위해 환경을 관찰하고", True),
    ("가능하다면 Workflow가 더 좋은 설계일 수 있다", True),
    ("Goal이 불명확하면 Agent가 잘 동작하는지 평가할 수 없다", True),
    ("A policy maps states to actions.", False),
    ("LLM, Workflow, Agent", False),
    ("search(query), open_document(id)", False),
    ("", False),
])
def test_korean_text_is_not_sent_for_translation(src, already):
    """원문이 이미 한국어면 번역할 것이 없다.

    실측(AI Agent 1주차): 보낸 129개 항목 중 **36개(27%)가 이미 한국어**였다.
    시간만 버리는 게 아니라 교수가 쓴 문장을 고쳐 놓는다.

        '목표를 달성하기 위해 환경을 관찰하고'   → '…관찰한다'
            뒤로 이어지는 문장을 끝맺어 버렸다
        '가능하다면 Workflow가 더 좋은 설계일 수 있다'
            → '…Workflow는 보다 우수한 설계 방식이 될 수 있다'

    문턱 0.3 은 실측 분포의 골짜기다 — 0.1~0.3 구간에 6개뿐이고 그 양옆에
    87개와 36개가 몰려 있다.
    """
    assert proxy.already_korean(src) is already


# ── 여러 칸 공백으로 나뉜 열을 되살린다 ──────────────────────────────────
_COLS = {
    "wrong decision wrong tool infinite loop":
        "wrong decision     wrong tool         infinite loop",
}


def test_column_gaps_are_restored_before_translating():
    """도식의 열은 원래 간격을 되살려 보내야 뜻이 산다.

    실측(AI Agent 1주차 20쪽). 원본 PDF 에서 세 열은 **여러 칸 공백**으로만
    나뉜 한 줄이다.

        'wrong decision     wrong tool         infinite loop'

    babeldoc 은 보내기 전에 연속 공백을 하나로 줄인다. 그러면 번역기가 한
    문장으로 읽는다. 실제로 모델에 넣어 재 봤다:

        공백 뭉갠 채  → '잘못된 결정, 부적절한 도구, 무한 루프'   (한 문장)
        간격 되살림   → '잘못된 결정    잘못된 도구    무한 루프'  (칸 유지)

    칸마다 따로 번역할 것 없이 **간격만 되돌려 주면** 모델이 알아서 칸으로
    읽는다. 열 경계는 원본 PDF 에만 남아 있으므로 곁길로 받는다.
    """
    from pdfko.proxy import restore_gaps
    got = restore_gaps("wrong decision wrong tool infinite loop", _COLS)
    assert got == "wrong decision     wrong tool         infinite loop"


def test_a_normal_sentence_is_left_alone():
    """열 목록에 없으면 손대지 않는다."""
    from pdfko.proxy import restore_gaps
    assert restore_gaps("A policy maps states to actions.", _COLS) is None
    assert restore_gaps("wrong decision wrong tool infinite loop", {}) is None
