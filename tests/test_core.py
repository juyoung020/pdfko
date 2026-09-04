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
    from pathlib import Path
    from pdfko import recover, runner
    # 소스 문자열이 아니라 **실제로 조립되는 명령줄**을 본다.
    cmd = runner.babeldoc_cmd(Path("/x/a.pdf"), Path("/tmp/w"), "1-4",
                              Path("/tmp/o"), model="m", proxy_port=1,
                              prompt_file=None)
    assert "--ignore-cache" in cmd
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

    자리표시자가 **둘 이상**인 문단만 센다([[어순이 실제로 굳는 자리]]).
    """
    import json
    from pdfko import qa, recover
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fragments.jsonl").write_text(
        "\n".join(json.dumps({"why": "heavy",
                              "src": f"{{v1}}term {i} and {{v2}}rest"})
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


def _label_block(*lines):
    """한 블록 안에 여러 줄이 든 쪽. 두 번 insert_text 하면 블록이 갈린다."""
    import pymupdf
    d = pymupdf.open()
    p = d.new_page(width=400, height=200)
    p.insert_textbox(pymupdf.Rect(20, 20, 380, 180), "\n".join(lines),
                     fontsize=11, fontname="korea")
    return d, p


def test_an_acronym_beside_korean_is_not_mixed_language():
    """`LLM` 옆에 한국어가 있다고 '번역이 덜 됐다'고 하면 안 된다.

    실측(AI Agent 1주차 7쪽): 라벨 묶음 `['LLM', '프롬프트']` 가 그림혼재로
    잡혔다. `LLM` 은 약어라 그대로 두는 것이 맞다.

    구분 신호는 **소문자**다 — 번역되지 않고 남은 영어 낱말에는 소문자가
    있고(`Yes`, `wrong tool`), 약어에는 없다(`LLM`, `API`, `MDP`).
    """
    from pdfko import qa
    d, p = _label_block("LLM", "프롬프트")
    assert qa.mixed_language_figures(p) == 0, "약어를 미번역으로 셌다"
    d.close()


def test_a_real_untranslated_word_beside_korean_is_still_caught():
    """반대로 소문자가 든 진짜 영어 낱말은 그대로 잡는다."""
    from pdfko import qa
    d, p = _label_block("Yes", "최종 추천")
    assert qa.mixed_language_figures(p) == 1, "미번역 낱말을 놓쳤다"
    d.close()


def test_the_gap_left_by_a_rejoin_survives_into_a_real_span():
    """되붙인 뒤 남는 공백을 **내용이 있는 span 안으로** 옮긴다.

    `stand ` 를 걷어내면 그 자리에 `<style id='1'> </style>` 처럼 공백만 든
    span 이 남는다. babeldoc 은 그런 span 을 렌더링에서 버려서, 결과물에
    `이해하기MDP` 로 두 항목이 붙어 찍힌다(실측, L03 2쪽).

    공백을 다음 span 의 내용 앞에 붙이면 버려질 자리가 없어진다.
    """
    from pdfko.proxy import rejoin_cut_words
    vocab = {"understand", "mdp"}
    items = [{"id": 0, "input": "○ Under"},
             {"id": 1, "input": "<style id='1'>stand </style><style id='3'>MDP</style>"}]
    out = rejoin_cut_words(items, vocab)
    assert out[0]["input"] == "○ Understand"
    assert "> </style>" not in out[1]["input"], f"공백만 든 span 이 남았다: {out[1]['input']!r}"
    # 일반 공백이 아니라 `\xa0`. babeldoc 은 span 앞머리의 일반 공백을
    # 잘라낸다 — 내용 있는 span 안으로 옮겨도 마찬가지였다(실측).
    assert ">\u00a0MDP<" in out[1]["input"], repr(out[1]["input"])


def test_the_overlap_detector_actually_fires():
    """겹침·줄충돌 검사가 실제로 도는지 못박아 둔다.

    실측 25쪽에서 두 값이 **정확히 0.0000** 이었다. 문턱 아래가 아니라 0이라,
    검사가 아예 안 도는 것인지 문서에 정말 겹침이 없는 것인지 알 수 없었다.
    일부러 낱말을 포갠 쪽을 넣어 확인했다 — 잡는다.

    그러므로 문턱(겹침 2%·줄충돌 4%)은 손댈 근거가 없다. 과민한 것도
    느슨한 것도 아니고, 잡을 것이 없었을 뿐이다. 이 시험이 없으면 다음에
    누군가 같은 의심을 다시 하게 된다.
    """
    import pymupdf
    from pdfko import qa

    def page(pile):
        d = pymupdf.open()
        p = d.new_page(width=400, height=300)
        for i in range(20):
            x = 40 if pile and i >= 10 else 40 + (i % 5) * 60
            y = 60 if pile and i >= 10 else 60 + (i // 5) * 30
            p.insert_text((x, y), f"word{i}", fontsize=10)
        return d, p

    o, op = page(False)
    t, tp = page(True)
    v = qa.inspect_page(op, tp, 1)
    assert v.overlap > qa.OVERLAP_MAX, v.overlap
    assert v.collision > qa.COLLISION_MAX, v.collision
    assert v.broken and any("겹침" in r for r in v.reasons), v.reasons
    o.close(); t.close()


# ── 발표자료인가 교재인가 ────────────────────────────────────────────────
def test_a_landscape_document_is_treated_as_slides():
    """줄 분리는 발표자료에만 켠다. 교재에 켜면 문단이 토막난다.

    babeldoc 의 `--split-short-lines` 는 짧은 줄을 따로 떼어 준다. 발표자료
    에서는 이것이 옳다 — 화살표·목록 항목이 원래 각자 한 줄이기 때문이다.

        전  '입력↓' / 'LLM↓' / '출력'
        후  '입력' / '↓' / 'LLM' / '↓' / '출력'

    그런데 교재에 켜면 반대가 된다. 실측(548쪽 교재): 배율 3.0 에서
    121쪽은 44줄 **전부**가 분리 대상이었다. 이어지는 문단이 줄마다 토막나
    문맥 없이 번역된다.

    가르는 신호는 쪽 모양이다 — 실측으로 발표자료 1.78, 교재 0.78 이었다.
    내용이 아니라 문서 형식의 성질이라 흔들리지 않는다.
    """
    import pymupdf
    from pdfko.runner import looks_like_slides

    wide = pymupdf.open()
    wide.new_page(width=720, height=405)          # 16:9
    tall = pymupdf.open()
    tall.new_page(width=595, height=842)          # A4 세로

    assert looks_like_slides(wide) is True
    assert looks_like_slides(tall) is False
    wide.close(); tall.close()


def test_settings_change_invalidates_a_finished_chunk():
    """엔진 호출 방식이 바뀌면 끝난 구간도 다시 번역해야 한다.

    구간 `.done` 표식은 **중단된 실행을 이어가려고** 있다. 그런데 표식이
    "끝났다"만 기록하면, pdfko 를 고쳐 놓고 같은 파일을 다시 돌렸을 때
    옛 결과가 조용히 그대로 나온다 — 실측으로 겪었다. 줄 분리 배율을 새로
    넣고 재실행했더니 `1-23 건너뜀 (완료됨)` 만 찍히고 옛 PDF 가 나왔다.

    표식에 **무슨 설정으로 만든 것인지**를 같이 적어 두면, 설정이 그대로일
    때만 건너뛴다. 이어하기는 살고 함정은 사라진다.
    """
    import tempfile
    from pathlib import Path
    from pdfko.runner import Chunk

    with tempfile.TemporaryDirectory() as d:
        c = Chunk(1, 4, Path(d) / "1-4")
        c.outdir.mkdir()
        assert c.done("설정A") is False        # 아직 안 끝났다

        c.mark_done("설정A")
        assert c.done("설정A") is True         # 같은 설정이면 건너뛴다
        assert c.done("설정B") is False        # 설정이 바뀌면 다시 한다


def test_the_stamp_ignores_settings_that_do_not_change_the_output():
    """포트·경로처럼 실행마다 달라지는 값은 표식에 넣지 않는다.

    넣으면 이어하기가 죽는다 — 미들웨어 포트는 실행마다 새로 잡히므로,
    중단된 번역을 이어가려 해도 매번 표식이 어긋나 처음부터 다시 돈다.
    """
    from pdfko.runner import settings_stamp

    base = ["babeldoc", "--files", "/x/a.pdf", "--pages", "1-4",
            "--openai-base-url", "http://127.0.0.1:8101/v1",
            "--min-text-length", "1", "--qps", "10",
            "--working-dir", "/tmp/w1", "--output", "/tmp/o1"]
    moved = ["babeldoc", "--files", "/x/a.pdf", "--pages", "5-8",
             "--openai-base-url", "http://127.0.0.1:9999/v1",
             "--min-text-length", "1", "--qps", "4",
             "--working-dir", "/tmp/w2", "--output", "/tmp/o2"]
    assert settings_stamp(base) == settings_stamp(moved)

    # 번역 결과를 바꾸는 값은 반드시 표식을 흔들어야 한다
    for changed in (
        base + ["--short-line-split-factor", "3.0"],
        base[:-4] + ["--primary-font-family", "sans"] + base[-4:],
    ):
        assert settings_stamp(base) != settings_stamp(changed)


def test_the_caller_computes_the_same_stamp_the_writer_records():
    """묻는 쪽과 찍는 쪽의 지문이 같아야 한다.

    `translate_chunk` 는 실제 포트·쪽 범위가 든 명령줄로 지문을 찍는다.
    호출부는 그것들을 아직 모르니 빈 값으로 조립해 묻는다. 두 지문이
    어긋나면 **이어하기가 영영 죽는다** — 끝난 구간마다 표식이 안 맞아
    매번 처음부터 다시 돈다. 조용히 느려질 뿐 오류가 안 나서 알아채기 어렵다.
    """
    from pathlib import Path
    from pdfko.runner import babeldoc_cmd, settings_stamp

    src, work = Path("/x/a.pdf"), Path("/tmp/w")
    asked = settings_stamp(babeldoc_cmd(
        src, work, "", work, model="m", proxy_port=0, prompt_file=None))
    written = settings_stamp(babeldoc_cmd(
        src, work, "5-8", work / "part", model="m", proxy_port=8123,
        prompt_file=None))
    assert asked == written


# ── 홑글자뿐인 항목은 모델에 보내지 않는다 ──────────────────────────────
def test_single_letters_have_nothing_to_translate():
    """`A → B → C → D` 같은 항목은 번역할 게 없다.

    실측(AI Agent 1주차 9쪽). 원문 `A {v1}B {v2}C {v3}D` 를 평문 경로로
    모델에 보냈더니 이렇게 돌아왔다:

        A {v1}B {v2}C {v3}D  →  {v1}B {v2}C {v3}D 형태의 {v1}

    앞의 `A` 가 사라지고, 없던 `형태의` 가 생기고, 자리표시자 하나가
    중복됐다. 보내지만 않았으면 멀쩡했을 항목이다.

    그렇다고 "산문이 없으면 보내지 마"로 막으면 안 된다. `No` 도 그 검사에
    걸려서 도식의 `아니오` 가 영어로 남는다 — `--min-text-length 1` 을 넣은
    이유가 바로 그것이었다. 기준은 **남는 라틴 낱말이 전부 홑글자인가**다.
    """
    from pdfko.proxy import nothing_to_translate

    # 보낼 필요가 없는 것
    for s in ("A {v1}B {v2}C {v3}D", "A → B → C", "2023-03-15", "9",
              "{v1}{v2}", "  ", "x"):
        assert nothing_to_translate(s) is True, s

    # 반드시 보내야 하는 것 — 여기서 막히면 영어가 그대로 남는다
    for s in ("No", "Yes", "STOP", "{v1}Tool A", "Goal", "A Tool"):
        assert nothing_to_translate(s) is False, s


def test_the_plain_path_skips_single_letter_bodies_without_crashing():
    """평문 경로가 실제로 그 항목을 걸러 내고, 세는 칸도 있어야 한다.

    앞 시험은 판정 함수만 봤다. 그러면 `STATS` 에 열쇠가 없어 `KeyError` 가
    나도 통과한다 — 실제로 그럴 뻔했다. 여기서는 경로를 밟는다.
    """
    from fastapi.testclient import TestClient
    from pdfko import proxy

    body = "A {v1}B {v2}C {v3}D"
    prompt = f"Now translate the following text:\n\n{body}"
    r = TestClient(proxy.app).post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": prompt}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == body
    assert proxy.STATS["nothing_to_translate"] >= 1


# ── 열 위치는 글자 수가 아니라 그려지는 폭으로 맞춘다 ────────────────────
def test_columns_land_where_the_source_put_them():
    """칸을 글자 수로 세면 한국어에서 열이 무너진다.

    원본 `Reasoning       Tool       Control` 은 7칸씩 띄워 세 열을 만든다.
    모델은 그 7칸을 그대로 지켜 `추론       도구       제어` 를 돌려준다 —
    지시대로다. 그런데 `Reasoning`(9자)과 `추론`(2자)의 **그려지는 폭**이
    달라서, 같은 7칸을 둬도 열이 왼쪽으로 무너진다.

        원본   Reasoning x=73   Tool x=220   Control x=396
        번역   추론      x=75   도구 x=148   제어    x=237

    그러면 슬라이드에 그려진 화살표는 제자리에 남아 열과 어긋난다.
    칸 수가 아니라 **원본이 잡아 둔 폭**에 맞춰 채운다.
    """
    from pdfko.proxy import align_columns, est_width

    src = "Reasoning       Tool       Control"
    tgt = "추론       도구       제어"
    got = align_columns(src, tgt)

    import re
    cells_src = re.split(r"  +", src)
    cells_got = re.split(r"  +", got)
    assert cells_got == ["추론", "도구", "제어"]      # 글자는 그대로

    # 각 열이 원본이 잡아 둔 자리 근처에서 시작한다
    def starts(line, cells):
        out, at = [], 0
        for c in cells:
            i = line.index(c, at)
            out.append(est_width(line[:i]))
            at = i + len(c)
        return out
    for want, have in zip(starts(src, cells_src), starts(got, cells_got)):
        assert abs(want - have) <= 0.5, (want, have)


def test_column_alignment_keeps_its_hands_off_ordinary_lines():
    """칸이 없는 줄, 칸 수가 안 맞는 답은 건드리지 않는다.

    억지로 맞추면 멀쩡한 문장에 공백이 박힌다. 확신이 없으면 손대지 않는 쪽이
    항상 낫다 — 열이 조금 좁은 것보다 문장이 깨지는 쪽이 훨씬 나쁘다.
    """
    from pdfko.proxy import align_columns

    assert align_columns("a normal line", "평범한 줄") == "평범한 줄"
    # 모델이 칸을 잃어버린 경우 — 되살릴 근거가 없다
    assert align_columns("A       B       C", "가 나 다") == "가 나 다"
    # 칸 수가 다른 경우
    assert align_columns("A       B       C", "가       나") == "가       나"


def test_padding_stops_where_the_typesetter_stops_drawing_spaces():
    """넓은 칸은 조판기가 마침표 하나로 바꾼다 — 채우는 데 상한이 있다.

    원본 20쪽의 진짜 간격은 7칸과 **21칸**이다. 거기에 맞춰 채워 보니
    조판된 PDF 에 `도구.제어` 가 찍혔다 — 21칸이 마침표가 됐다. 같은 실행에서
    15칸은 멀쩡히 그려졌다(`추론` 줄 x=54..213).

    모델이 아니라 **조판기**가 하는 일이라, 번역 뒤에 채워도 피할 수 없다.
    실측으로 안전이 확인된 데까지만 채운다.
    """
    import re
    from pdfko.proxy import align_columns, _PAD_MAX

    # 아주 넓은 칸을 요구해도 상한을 넘지 않는다
    wide = "A" + " " * 60 + "B"
    got = align_columns(wide, "가" + " " * 60 + "나")
    assert max(len(g) for g in re.findall(r"  +", got)) <= _PAD_MAX


def test_columns_stay_lined_up_across_rows():
    """행마다 칸 수가 달라도 열은 같은 자리에 서야 한다.

    원본 20쪽의 세 행은 **다른 칸 수**로 같은 열을 만든다. 실측한 2열의 x:

        wrong decision   5칸  → x=160
        bad plan        14칸  → x=161
        hallucination    8칸  → x=158

    이걸 "그 줄의 가장 좁은 칸"으로 통일하면 정보가 사라져, 번역 뒤 열이
    행마다 어긋난다(실측 em 7.0 / 5.3 / 9.3). 원본 칸에 맞추면 다시 모인다.
    """
    from pdfko.proxy import align_columns, est_width, _MULTISPACE

    rows = [("wrong decision     wrong tool", "잘못된 결정     잘못된 도구"),
            ("bad plan              bad argument", "나쁜 계획     나쁜 논거"),
            ("hallucination        timeout", "환각 현상     시간 초과")]
    starts = []
    for src, tgt in rows:
        got = align_columns(src, tgt)
        second = _MULTISPACE.split(got)[1]
        starts.append(est_width(got[:got.index(second)]))
    assert max(starts) - min(starts) <= 1.0, starts


def test_pieces_far_apart_on_one_baseline_are_columns_too():
    """열이 '여러 칸 공백'으로만 오지는 않는다.

    실측(16쪽). 자율성 눈금의 양 끝 `Low` 와 `High` 는 같은 baseline 에
    있으면서 242pt 떨어진 **별개 조각**이다(글꼴 18pt — 줄 높이의 13배).
    열 지도가 여러 칸 공백만 보고 있어서 이 줄은 아예 안 잡혔고, 번역본에서
    `낮음 높음` 으로 붙어 눈금이라는 뜻이 사라졌다.

    가르는 기준은 줄 높이다. 줄 하나보다 넓게 벌어진 가로 간격은 낱말
    사이가 아니라 열 사이다 — 글꼴 크기에 매이지 않는 기준이라 문서가
    달라져도 흔들리지 않는다.
    """
    import json
    import tempfile
    from pathlib import Path

    import pymupdf
    from pdfko.cli import build_columns

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.pdf"
        doc = pymupdf.open()
        pg = doc.new_page(width=600, height=200)
        pg.insert_text((34, 100), "Low", fontsize=18)
        pg.insert_text((311, 100), "High", fontsize=18)
        pg.insert_text((34, 140), "Script", fontsize=18)      # 혼자인 줄은 아니다
        doc.save(src); doc.close()

        out = Path(d) / "columns.json"
        build_columns(src, out)
        cols = json.loads(out.read_text(encoding="utf-8"))

        assert "Low High" in cols, cols
        send, want = cols["Low High"]
        assert "   " in send                      # 모델이 열로 읽을 만큼은 띄운다
        assert want.count(" ") > send.count(" ")  # 맞출 것은 원본 간격만큼 넓다
        assert "Script" not in " ".join(cols)     # 혼자 있는 줄은 건드리지 않는다


def test_alignment_survives_style_tags():
    """서식 태그가 붙어 와도 열은 맞춘다.

    실측(16쪽)에서 그 줄은 이렇게 도착했다:

        "<style id='1'>Low </style><style id='3'>High</style>"

    태그를 무시하고 글자만 보면 열쇠가 맞고, 채운 칸은 두 칸 **사이**에
    들어가야 한다. 태그를 버리면 원본의 색이 사라진다.
    """
    import re

    from pdfko.proxy import align_columns, restore_gaps, true_line

    cols = {"Low High": ["Low     High", "Low" + " " * 41 + "High"]}
    src = "<style id='1'>Low </style><style id='3'>High</style>"

    # ① 보낼 때는 **건드리지 않는다.** 태그 안쪽에 칸을 넣었더니 모델이 그
    #    공백을 떨어뜨려 `낮음높음` 으로 붙었다.
    assert restore_gaps(src, cols) is None

    # ② 모델이 칸을 지키든 떨어뜨리든, 태그 하나가 곧 한 칸이다.
    for got in ("<style id='1'>낮음 </style><style id='3'>높음</style>",
                "<style id='1'>낮음</style><style id='3'>높음</style>"):
        out = align_columns(true_line(src, cols), got)
        assert out.count("<style") == 2 and out.count("</style>") == 2
        assert "낮음" in out and "높음" in out
        assert max(len(g) for g in re.findall(r"  +", out)) > 5


def test_markup_letters_do_not_count_against_the_translation():
    """서식 태그의 글자를 세면 짧은 라벨이 멀쩡한 번역을 거부당한다.

    실측(16쪽). 모델이 정확히 옮겨 줬는데도 거부됐다:

        보낸 것  "<style id='1'>Low </style><style id='3'>High</style>"
        받은 것  "<style id='1'>낮음</style><style id='3'>높음</style>"
        판정     id 1 hangul 0.14  → 반향으로 보고 버림

    `style`·`id` 의 라틴 글자가 한글 4자를 눌러 비율이 0.14 로 떨어진 것이다.
    태그는 서식이지 본문이 아니다. 자리표시자도 마찬가지다.
    """
    from pdfko.proxy import is_echo, prose_hangul_ratio

    src = "<style id='1'>Low </style><style id='3'>High</style>"
    good = "<style id='1'>낮음</style><style id='3'>높음</style>"
    assert prose_hangul_ratio(good) == 1.0
    assert is_echo(src, good) is False        # 멀쩡한 번역을 버리지 않는다

    # 진짜 반향은 여전히 걸러진다
    assert is_echo(src, src) is True
    assert is_echo("{v1}Tool A", "{v1}Tool A") is True


def test_the_chunk_stamp_covers_proxy_rules_too():
    """엔진 인자만 보면 절반이다 — 검증 규칙이 바뀌어도 다시 번역해야 한다.

    구간 표식은 "무슨 설정으로 만든 것인가"를 적어 둔다. 그런데 pdfko 가
    번역을 바꾸는 방법은 두 가지다:

      · babeldoc 에 넘기는 인자   (줄 분리, 글꼴 …)
      · 프록시의 검증·수리 규칙   (반향 판정, 열 정렬 …)

    인자만 지문에 넣으면 규칙을 고쳐 놓고 다시 돌려도 옛 결과가 조용히
    그대로 나온다 — 이 함정을 이미 한 번 겪었다.
    """
    from pathlib import Path
    from pdfko import proxy, runner

    cmd = runner.babeldoc_cmd(Path("/x/a.pdf"), Path("/tmp/w"), "1-4",
                              Path("/tmp/o"), model="m", proxy_port=1,
                              prompt_file=None)
    before = runner.settings_stamp(cmd)

    real = proxy._rules_fingerprint
    try:
        proxy._rules_fingerprint = lambda *a, **k: "규칙이-바뀌었다"
        assert runner.settings_stamp(cmd) != before
    finally:
        proxy._rules_fingerprint = real
    assert runner.settings_stamp(cmd) == before      # 되돌리면 같아진다


def test_a_sentence_split_by_inline_math_is_not_a_column():
    """줄 안에 수식이 끼어 갈라진 문장은 열이 아니다.

    떨어진 조각을 모두 열로 보면 교재가 망가진다. 실측(548쪽 교재):

        1.7배  'regular predictors of' | 'over this interval'   ← 문장이다
        0.8배  '…copyright holder.'    | 'This work is licensed' ← 문장이다

    반면 진짜 열은 훨씬 넓게 벌어진다:

        2.4배  '2023-03-15' | '10'                 (쪽 바닥글)
        2.8배  'Yes'        | 'No'                 (판단 도식)
        3.9배  'Qt(a)'      | 'estimate at time t' (기호표)
       13.5배  'Low'        | 'High'               (자율성 눈금)
       21.6배  'Preface …'  | 'xiii'               (목차)

    골짜기는 1.7배와 2.4배 사이다. 줄 높이의 두 배를 문턱으로 둔다.
    """
    import json
    import tempfile
    from pathlib import Path

    import pymupdf
    from pdfko.cli import build_columns

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.pdf"
        doc = pymupdf.open()
        pg = doc.new_page(width=600, height=300)
        # 'regular predictors of' 는 x=40..129 에 그려진다. 뒤 조각을
        # x=145 에 두면 간격 16pt — 줄 높이의 1.6배로, 실측한 1.7배와 같다.
        pg.insert_text((40, 80), "regular predictors of", fontsize=10)
        pg.insert_text((145, 80), "over this interval", fontsize=10)
        # 'Low' 는 x=40..58. 뒤를 x=120 에 두면 62pt — 6.2배로 열이다.
        pg.insert_text((40, 140), "Low", fontsize=10)
        pg.insert_text((120, 140), "High", fontsize=10)
        doc.save(src); doc.close()

        out = Path(d) / "columns.json"
        build_columns(src, out)
        cols = json.loads(out.read_text(encoding="utf-8"))

    assert "Low High" in cols
    assert not any("regular predictors" in k for k in cols), cols


# ── 복구는 영어가 남았을 때만 한다 ────────────────────────────────────────
def test_recovery_only_runs_for_leftover_english():
    """레이아웃이 깨졌다고 다시 번역하거나 원문으로 되돌리지 않는다.

    복구는 번역이 잘못됐을 때 쓰는 장치인데, 좌표 판정으로 부르면 **잘된
    번역을 되돌린다.** 한국어가 영어보다 길어 상자를 살짝 넘는 것은 흔한
    일이고, 그때마다 재번역하거나 영어 원문을 도로 붙이면 손해다.

    남기는 것은 하나뿐이다 — 영어가 그대로 남은 쪽. 그건 번역이 실제로
    안 된 것이라 다시 물어볼 이유가 분명하다.

    레이아웃 검사 자체는 남긴다. 23쪽에 0.0초라 공짜이고, 보고서에 무엇이
    어떻게 놓였는지 적어 두는 것은 여전히 쓸모가 있다 — 다만 **읽을거리**지
    행동의 근거가 아니다.
    """
    import inspect

    from pdfko import cli, recover, web

    for src in (inspect.getsource(cli._main), inspect.getsource(web._run)):
        assert "repair_untranslated" in src        # 영어 잔존은 고친다
        assert "qa.scan" in src                    # 검사와 보고서는 남는다
        assert "write_report" in src
        assert "repair_pages" not in src           # 레이아웃으로는 손대지 않는다
        assert "revert_pages" not in src

    # 되돌리는 기능 자체가 없어야 한다 — 남겨 두면 언젠가 다시 불린다
    assert not hasattr(recover, "repair_pages")
    assert not hasattr(recover, "revert_pages")


def test_a_retranslation_that_would_wreck_the_page_is_not_spliced(tmp_path,
                                                                  monkeypatch):
    """재번역이 자리를 망가뜨리면 끼우지 않는다 — **끼우기 전에** 본다.

    예전에는 먼저 끼우고 나서 봤다. 깨졌으면 보고서에 "되돌림"이라 적었는데,
    실제로 되돌리는 코드는 없었다. 망가진 쪽을 그대로 둔 채 보고서만
    거짓말을 하고 있었다.
    """
    import pymupdf
    from pdfko import recover

    # 원본: 쪽 가운데 좁은 띠에만 영어가 있다
    orig = tmp_path / "o.pdf"
    d = pymupdf.open(); pg = d.new_page(width=400, height=600)
    for k in range(4):
        pg.insert_text((60, 300 + k * 12),
                       "with general function approximation there is not",
                       fontsize=9)
    d.save(orig); d.close()

    # 번역본: 같은 자리에 한국어 + 영어 한 줄이 남았다
    trans = tmp_path / "t.pdf"
    d = pymupdf.open(); pg = d.new_page(width=400, height=600)
    for k in range(4):
        pg.insert_text((60, 300 + k * 12), "정책은 상태를 행동으로 사상한다",
                       fontsize=9, fontname="korea")
    pg.insert_text((60, 360), "with general function approximation there is "
                              "not such a clear notion here", fontsize=9)
    d.save(trans); d.close()
    before = trans.read_bytes()

    # 재번역 결과: 한국어지만 원본 본문 밖으로 한참 흩어졌다
    wrecked = tmp_path / "w.pdf"
    d = pymupdf.open(); pg = d.new_page(width=400, height=600)
    for k in range(20):
        pg.insert_text((20, 30 + k * 26), "정책은 상태를 행동으로 사상하는 함수",
                       fontsize=9, fontname="korea")
    d.save(wrecked); d.close()

    monkeypatch.setattr(recover, "retranslate_page", lambda *a, **k: wrecked)
    recs = recover.repair_untranslated(trans, orig, 0, orig, tmp_path,
                                       model="m", proxy_port=1)
    assert [r.action for r in recs] == ["kept"], recs
    assert "레이아웃" in recs[0].note, recs[0].note
    assert trans.read_bytes() == before, "망가질 쪽을 이미 끼워 넣었다"


# ── 낱말 한가운데서 잘려 온 조각은 번역하지 않는다 ──────────────────────
def test_a_cut_word_fragment_is_left_alone():
    """`Agent` 가 `Age` + `nt` 로 잘려 오면 `Age` 를 번역하면 안 된다.

    실측(2쪽 부제). 번역 엔진이 낱말 한가운데를 끊어 `Age` 를 홀로 보냈다.
    번역기에게 `Age` 는 정상 영어 낱말이라 성실하게 옮겼고, 조판된 쪽에는
    이렇게 찍혔다:

        원본  'Agent vs Workflow — 목표, 환경, 자율성, 성공과 실패'
        결과  '연령nt vs Workflow — 목표, 환경, 자율성, 성공과 실패'

    가르는 근거는 원본 문서의 어휘다 — `age` 는 이 문서에 독립된 낱말로 없고
    `agent` 의 앞부분일 뿐이다. 그대로 두면 뒤 조각 `nt` 가 바로 이어져
    `Agent` 로 읽힌다.

    판정 함수(`truncated_tail`)는 진작 있었는데 **아무도 부르지 않았다.**
    있는 줄 알고 지나간 자리다.

    다만 항목에 낱말이 하나뿐일 때만 통째로 넘긴다. 긴 항목까지 넘기면
    (`search(query), open_document(id), run_pytho`) 멀쩡한 앞부분까지 영어로
    남는다.
    """
    from pdfko.proxy import is_cut_fragment

    vocab = {"agent", "workflow", "python", "goal", "tool"}
    assert is_cut_fragment("Age", vocab) is True         # agent 의 앞부분
    assert is_cut_fragment("Work", vocab) is True        # workflow 의 앞부분

    assert is_cut_fragment("Agent", vocab) is False      # 온전한 낱말
    assert is_cut_fragment("Goal", vocab) is False
    assert is_cut_fragment("Age of tools", vocab) is False   # 낱말이 여럿
    assert is_cut_fragment("Age", set()) is False        # 어휘가 없으면 판정 안 함


# ── 한 낱말은 한 낱말로 ──────────────────────────────────────────────────
def test_a_one_word_label_must_not_grow_into_a_phrase():
    """원문이 한 낱말이면 번역도 한 낱말이어야 한다.

    실측(바닥글). 엔진이 `Copyright 2025. Korea Aerospace University…` 에서
    `Copyright` 를 떼어 홀로 보냈고, 번역이 `저작권 정보` 로 돌아왔다. 원본
    조각의 상자보다 넓어서 조판기가 줄을 접었다 — 23쪽 중 10쪽에서
    `저작권 정` / `보` 로 갈라져 찍혔다.

    폭으로는 못 가른다. `저작권 정보`(1.18배)가 멀쩡한 `스크립트`(1.33배)나
    `에이전트`(1.60배)보다 **좁다**. 가르는 축은 낱말 수다. 실측한 24개
    라틴 한-낱말 항목 중 늘어난 것은 넷뿐이었고, 그중 셋(`Copyright`
    `Input` `Output`)은 같은 캐시에 한-낱말 답도 함께 있었다 — 모델이 낼 수
    있는 답이다.
    """
    from pdfko.proxy import check

    def verdict(src, tgt):
        return check([{"id": 0, "input": src, "layout_label": "plain text"}],
                     [{"id": 0, "output": tgt}])

    ok, why = verdict("Copyright", "저작권 정보")
    assert ok is False and why.kind == "wordy", (ok, why)
    assert verdict("Input", "입력 데이터")[0] is False
    assert verdict("Output", "출력 결과")[0] is False

    # 한 낱말로 옮긴 것은 통과한다
    for src, tgt in (("Copyright", "저작권"), ("Input", "입력"),
                     ("No", "아니오"), ("Environment", "환경"),
                     ("Script", "스크립트")):
        assert verdict(src, tgt)[0] is True, (src, tgt)

    # 원문이 여러 낱말이면 이 규칙은 끼어들지 않는다
    assert verdict("AI Agent", "AI 에이전트")[0] is True
    assert verdict("Agent Diagram", "에이전트 다이어그램")[0] is True


def test_the_authors_own_korean_must_survive():
    """원문에 이미 있는 한국어는 한 글자도 바꾸지 않는다.

    강의 자료는 영어와 한국어가 한 줄에 섞여 온다. 영어를 옮기려면 보내야
    하는데, 보내면 모델이 한국어까지 다시 쓴다. 실측 5건:

        '…Agent가 완성된다'        → '…에이전트가 생성된다'
        'Flexibility…가 높아지고,'  → '유연성과 적응력이 향상된다,'
        '…구분하여야 한다'          → '…구분해야 한다'

    둘째 것이 특히 나쁘다. `높아지고,` 는 다음 항목으로 이어지는 절인데
    `향상된다,` 로 끝맺어 버려 문장이 끊긴다.
    """
    from pdfko.proxy import check

    def verdict(src, tgt):
        return check([{"id": 0, "input": src, "layout_label": "plain text"}],
                     [{"id": 0, "output": tgt}])

    ok, why = verdict("{v1}Stop condition으로 Agent가 완성된다",
                      "{v1}정지 조건에 따라 에이전트가 생성된다")
    assert ok is False and why.kind == "korean", (ok, why)

    # 원문의 한국어를 그대로 둔 번역은 통과한다
    assert verdict("{v1}Stop condition으로 Agent가 완성된다",
                   "{v1}정지 조건으로 에이전트가 완성된다")[0] is True
    # 한국어가 없던 문단은 이 규칙과 무관하다
    assert verdict("Agent Loop", "에이전트 반복")[0] is True


def test_width_is_gated_by_word_count_not_by_em_width():
    """짧은 라벨 면제를 폭으로 재면 칼끝에서 빗나간다.

    실측(L03 12쪽). `Let's address it with` 는 뒤에 ChatGPT 로고가 이어지는
    **조각**인데, `이를 다음 방법을 통해 다루어 보도록 하자.` 로 끝맺어져
    1.95배가 됐고 말풍선 밖으로 잘렸다. 폭 검사가 잡았어야 하는데 면제됐다 —
    원문 폭이 **9.99em** 이고 면제 문턱이 `10em 이상` 이었다. 0.01em 차이다.

    두 문서에서 상한을 넘고도 면제된 항목을 모아 보니 축이 분명했다:

        4낱말  1.95배  'Let's address it with'  → 조각을 문장으로 끝맺음
        4낱말  1.73배  'can be reduced to'      → 같은 패턴
        ────────────────────────────────────────────────────
        3낱말  1.65배  'Workflow or Agent?'     → 올바른 번역
        3낱말  1.42배  '(no terminal state)'    → 올바른 번역

    짧은 라벨인지는 낱말 수가 말해 준다. 한국어는 짧은 라벨에서 원문보다
    넓어지는 것이 정상이라 폭으로는 가릴 수 없다.
    """
    from pdfko.proxy import too_wide

    assert too_wide("Let’s address it with",
                    "이를 다음 방법을 통해 다루어 보도록 하자.") is True
    assert too_wide("can be reduced to", "다음과 같이 단순화될 수 있다.") is True

    # 세 낱말 이하는 면제한다 — 늘어나는 것이 정상이다
    assert too_wide("Workflow or Agent?", "워크플로우인가, 에이전트인가?") is False
    assert too_wide("(no terminal state)", "（종료 상태가 존재하지 않음）") is False
    assert too_wide("Agent Diagram", "에이전트 다이어그램") is False
    assert too_wide("No", "아니오") is False


def test_column_gaps_survive_placeholders():
    """`=` 가 `{v1}` 로 바뀌어 와도 열 간격을 찾아야 한다.

    실측(L03 12쪽). 원본 span 은 이렇게 생겼다:

        'state:      = {sunny, cloudy} = {1, 2}'

    `state:` 와 `=` 사이 공백 6칸은 그 자리에 따로 그려지는 수식 글리프
    `𝒮` 의 자리다. 그런데 엔진은 `=` 를 자리표시자로 바꿔 보낸다:

        'state: {v1}{sunny, cloudy} {v2}{1, 2}'

    열 지도에는 그 줄이 **들어 있는데** 열쇠가 안 맞아 간격이 복원되지
    않았고, 6칸이 한 칸으로 줄어 `𝒮` 와 `=` 가 겹쳐 `=𝒮 sunny…` 로 찍혔다.

    자리표시자를 아무 글자로 보고 맞춘다.
    """
    from pdfko.proxy import restore_gaps

    cols = {"state: = {sunny, cloudy} = {1, 2}":
            ["state:      = {sunny, cloudy} = {1, 2}",
             "state:      = {sunny, cloudy} = {1, 2}"]}
    got = restore_gaps("state: {v1}{sunny, cloudy} {v2}{1, 2}", cols)
    assert got is not None, "자리표시자 때문에 못 찾았다"
    assert got.startswith("state:      {v1}"), got     # 6칸이 살아났다
    assert "{v1}" in got and "{v2}" in got             # 자리표시자는 그대로

    # 열쇠에 없는 줄은 건드리지 않는다
    assert restore_gaps("other: {v1}text", cols) is None


def test_a_gap_before_a_placeholder_is_restored_after_translation():
    """모델이 칸을 없애도, 자리표시자를 기준으로 되살린다.

    실측(L03 12쪽). 열 간격을 넣어 보냈는데 모델이 도로 없앴다:

        보낸 것  'state:      {v1}{sunny, cloudy} {v2}{1, 2}'
        받은 것  '상태: {v1}{sunny, cloudy} {v2}{1, 2}'

    그 6칸은 그 자리에 따로 그려지는 수식 글리프 `𝒮` 의 자리다. 없어지면
    `𝒮` 와 `=` 가 겹쳐 `=𝒮 sunny…` 로 찍힌다.

    칸 뒤에 자리표시자가 오는 모양이면 기준점이 확실하다 — 자리표시자는
    번역을 거쳐도 그대로 남는다(그러라고 검사까지 건다). 목표문에서 같은
    자리표시자를 찾아 그 앞에 칸을 되돌린다.
    """
    from pdfko.proxy import restore_placeholder_gaps

    got = restore_placeholder_gaps(
        "state:      {v1}{sunny, cloudy} {v2}{1, 2}",
        "상태: {v1}{sunny, cloudy} {v2}{1, 2}")
    assert got == "상태:      {v1}{sunny, cloudy} {v2}{1, 2}", got

    # 서식 태그가 끼어 있어도 자리표시자만 있으면 된다
    got = restore_placeholder_gaps(
        "<style id='1'>state</style>:      {v3}x",
        "<style id='1'>상태</style>: {v3}x")
    assert "      {v3}" in got, got

    # 손대면 안 되는 경우
    assert restore_placeholder_gaps("plain text", "평범한 글") == "평범한 글"
    assert restore_placeholder_gaps("a      b", "가 나") == "가 나"   # 칸 뒤가 자리표시자가 아니다
    assert restore_placeholder_gaps("a      {v1}", "가 나") == "가 나"  # 목표에 자리표시자가 없다


def test_placeholder_gaps_are_found_through_style_tags():
    """서식 태그가 붙어 와도 자리표시자 앞의 칸을 찾아야 한다.

    실측(L03 9쪽). 원본 span 은 `'state:              = {sunny, cloudy} …'`
    이고 그 14칸에 수식 글리프 `S_t ∈ 𝒮` 가 그려진다. 엔진은 이렇게 보낸다:

        "<style id='1'>state</style>: {v3}{sunny, cloudy} {v4}…"

    태그 때문에 열쇠가 안 맞아 칸이 복원되지 않았고, `{맑음, 흐림}` 이
    수식 자리(x=437..525)로 밀려들어 겹쳐 찍혔다 — 글자가 사라진 것이
    아니라 가려진 것이다.

    태그를 걷어내고 자리표시자를 아무 글자로 보아 열쇠를 찾은 뒤, **어느
    자리표시자 앞에 몇 칸이 필요한지**를 돌려준다. 태그를 가로질러 조각을
    찾을 필요가 없어진다.
    """
    from pdfko.proxy import placeholder_gaps

    cols = {"state: = {sunny, cloudy} = {1, 2}":
            ["state:      = {sunny, cloudy} = {1, 2}"] * 2}
    tagged = ("<style id='1'>state</style>: {v3}{sunny, cloudy} "
              "{v4}<style id='5'>{1, 2}</style>")
    assert placeholder_gaps(tagged, cols) == [("{v3}", 6)]

    # 태그가 없어도 같은 답
    plain = "state: {v1}{sunny, cloudy} {v2}{1, 2}"
    assert placeholder_gaps(plain, cols) == [("{v1}", 6)]

    # 열쇠에 없으면 빈 목록
    assert placeholder_gaps("other: {v1}x", cols) == []


def test_a_repeated_column_key_keeps_the_widest_gap():
    """같은 열쇠가 서로 다른 칸으로 두 번 나오면 넓은 쪽을 남긴다.

    실측(L03). 9쪽은 14칸, 12쪽은 6칸인데 공백을 뭉개면 열쇠가 같아진다.
    좁은 쪽을 남기면 넓은 쪽에서 글자가 수식 위로 밀려 겹친다. 넓은 쪽을
    남기면 좁은 쪽이 조금 벌어질 뿐이다 — 겹치는 것보다 낫다.
    """
    import json
    import tempfile
    from pathlib import Path

    import pymupdf
    from pdfko.cli import build_columns

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.pdf"
        doc = pymupdf.open()
        p1 = doc.new_page(width=600, height=200)
        p1.insert_text((40, 80), "state:      = {a, b}", fontname="cour", fontsize=11)
        p2 = doc.new_page(width=600, height=200)
        p2.insert_text((40, 80), "state:              = {a, b}", fontname="cour", fontsize=11)
        doc.save(src); doc.close()
        out = Path(d) / "c.json"
        build_columns(src, out)
        cols = json.loads(out.read_text(encoding="utf-8"))

    import re
    send = cols["state: = {a, b}"][0]
    assert max(len(g) for g in re.findall(r"  +", send)) == 14, send


def test_a_style_tag_with_the_wrong_brackets_is_caught():
    """모델이 괄호를 바꿔 쓰면 태그가 본문에 그대로 찍힌다.

    실측(교재 299쪽). 모델이 꺾쇠 대신 **중괄호**로 태그를 썼다:

        받은 것  '…이는 오프라인 {style id='14'}-라인 알고리즘의 최적 경우…'
        찍힌 것  그대로. 독자에게 `{style id='14'}` 가 보인다.

    검사기는 꺾쇠 계열만 보고 있었다(`[<〈＜﹤]`). 괄호 모양이 무엇이든
    `style` 이 붙어 있으면 태그를 쓰려다 만 것이다.
    """
    from pdfko.proxy import check

    src = "<style id='1'>Figure: </style>the off-line algorithm"
    bad = "<style id='1'>그림: </style>이는 오프라인 {style id='14'}-라인 알고리즘"
    ok, why = check([{"id": 0, "input": src, "layout_label": "plain text"}],
                    [{"id": 0, "output": bad}])
    assert ok is False and why.kind == "style", (ok, why)

    for wrong in ("[style id='2']x", "(style id='2')x", "＜style id='2'＞x"):
        assert check([{"id": 0, "input": src, "layout_label": "plain text"}],
                     [{"id": 0, "output": f"<style id='1'>그림: </style>{wrong}"}]
                     )[0] is False, wrong

    # 제대로 쓴 태그는 통과한다
    good = "<style id='1'>그림: </style>오프라인 알고리즘"
    assert check([{"id": 0, "input": src, "layout_label": "plain text"}],
                 [{"id": 0, "output": good}])[0] is True


# ── 낱말 끝에서 끊긴 합자 ────────────────────────────────────────────────
def test_a_ligature_broken_at_the_end_of_a_word():
    """`tradeoff.` 처럼 낱말 끝에서 끊긴 합자도 되돌린다.

    실측(교재 297쪽). 원본 텍스트 레이어에 `tradeo↵.` 가 들어 있는데, 사전은
    합자 **양쪽에 글자가 있을 때만** 모으고 있었다(`tradeo↵s` → `tradeoffs`).
    그래서 이 낱말은 사전에 없었고, 자리표시자가 그대로 남아 번역문에
    `…타협이 수반된다.ff.` 로 `ff` 가 튀어나왔다.

    548쪽 교재에서 이런 자리는 15가지 29회였다 — `Hoff` `Utgoff` `Ratcliff`
    같은 인용 인명과 `cliff` `tradeoff` `puff` `off`.

    신뢰 모형은 그대로다. 사전은 **원본에서 실제로 본 것만** 담고, 딱 맞는
    열쇠일 때만 되돌린다.
    """
    from pdfko import glyphmap

    # `↵` 는 글꼴에 없는 글자라 시험용 PDF 에 넣으면 `·` 로 바뀐다.
    # 그래서 글을 훑는 부분(`harvest`)만 따로 시험한다.
    t = glyphmap.harvest(
        "involves a tradeo↵. Hereafter\n"
        "cited by Utgo↵, and by Ho↵(1990) and di↵erent", {})
    assert t.get("tradeo\x00") == "tradeoff", t
    assert t.get("Utgo\x00") == "Utgoff"
    assert t.get("Ho\x00") == "Hoff"

    got, n = glyphmap.dissolve("involves a tradeo{v1}. Hereafter", t)
    assert got == "involves a tradeoff. Hereafter", got
    assert n == 1

    # 서식 태그가 끼어 있어도 된다
    got, _ = glyphmap.dissolve("a <style id='1'>tradeo</style>{v1}. x", t)
    assert "tradeoff" in got.replace("</style>", "").replace(
        "<style id='1'>", ""), got

    # 사전에 없는 조합은 절대 건드리지 않는다 — 진짜 수식이다
    for math in ("G{v1}. x", "TD({v3}). y", "w{v2}, z"):
        assert glyphmap.dissolve(math, t) == (math, 0), math


# ── 보고서는 실제로 문제가 되는 것만 말해야 한다 ────────────────────────
def test_the_report_only_warns_where_word_order_can_actually_break():
    """자리표시자가 없는 문단을 '어순이 고정됐다'고 알리면 안 된다.

    실측(AI Agent 1주차). 보고서가 `조각 단위로 번역한 문단이 58개` 라고
    했는데, 뜯어 보니 이랬다:

        26건  자리표시자 0개   ← 어순과 아무 상관이 없다
        30건  자리표시자 1개   ← 하나뿐이면 앞뒤가 바뀔 것이 없다
         2건  자리표시자 2개   ← 같은 항목이 두 번, 그나마 쉼표 목록

    조각 모드는 자리표시자가 많아서만 쓰이는 게 아니라 **짧은 라벨을
    구제할 때도** 쓰인다(`Example`, `Agent – Goal`). 그걸 뭉뚱그려 세니
    거짓 경고가 됐고, 읽는 사람이 없는 문제를 찾게 만든다.

    어순이 실제로 굳는 것은 **자리표시자가 둘 이상인 문단**뿐이다.
    """
    import json
    import tempfile
    from pathlib import Path

    from pdfko.recover import _fragment_note

    with tempfile.TemporaryDirectory() as d:
        log = Path(d)
        log.joinpath("fragments.jsonl").write_text("\n".join(
            json.dumps({"ts": 0, "why": "rescue", "src": s}, ensure_ascii=False)
            for s in ("Example",
                      "Agent – Goal",
                      "{v1}Agent 후보",
                      "search(query), open{v1}document(id), run{v2}pytho")
        ), encoding="utf-8")
        note = "\n".join(_fragment_note(log))

    assert "1개" in note, note              # 셋은 빼고 하나만 센다
    assert "58" not in note
    assert "search(query)" in note          # 진짜 그 항목을 보여 준다
    assert "Example" not in note            # 자리표시자 없는 것은 싣지 않는다

    # 하나도 없으면 절 자체가 없어야 한다
    with tempfile.TemporaryDirectory() as d:
        log = Path(d)
        log.joinpath("fragments.jsonl").write_text(
            json.dumps({"src": "Example"}), encoding="utf-8")
        assert _fragment_note(log) == []


def test_the_item_path_also_skips_items_with_nothing_to_translate():
    """번역할 게 없는 항목은 **어느 경로로 와도** 보내지 않는다.

    실측(L03 3쪽). 우상단의 강의 번호 `L02` 를 모델에 보냈더니 이렇게
    돌아왔다:

        'L02'  →  '● Goals'

    같은 묶음에 있던 다른 항목의 내용을 베낀 것이다. 원본에서 `L02` 상자는
    폭이 20pt 뿐이라 `● Goals` 가 네 줄(`●` `G` `o` `als`)로 접혀 찍혔다.

    `nothing_to_translate` 는 `L02` 를 정확히 걸러 낸다 — 라틴 낱말이
    `L` 하나뿐이다. 그런데 **평문 경로에만** 걸려 있었고 항목 경로에는
    없었다. 가드는 판정이 아니라 배선이 빠지면 소용이 없다.
    """
    from fastapi.testclient import TestClient
    from pdfko import proxy

    before = proxy.STATS["nothing_to_translate"]
    body = ('[\n {\n  "id": 0,\n  "input": "L02",\n'
            '  "layout_label": "plain text"\n }\n]')
    r = TestClient(proxy.app).post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": body}]})
    assert r.status_code == 200
    out = r.json()["choices"][0]["message"]["content"]
    assert '"L02"' in out, out            # 그대로 돌려준다
    assert "Goals" not in out
    # 상류가 죽어 실패-복귀한 것과 구별해야 한다. 가드가 센 것이어야 한다.
    assert proxy.STATS["nothing_to_translate"] > before
