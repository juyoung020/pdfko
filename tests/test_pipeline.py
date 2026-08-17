"""CLI 와 웹이 **같은 일을 하는지** 확인한다.

## 왜 이 파일이 필요한가

`cli._main` 과 `web._run` 은 거의 같은 파이프라인을 각자 들고 있다. 그래서
한쪽만 고치고 다른 쪽을 잊는 일이 이번 주에만 다섯 번 있었다 —
엔진 캐시, 합자 사전, 용어 통일, 모델 확인, 번역 여부 검사. 매번 웹 사용자만
조용히 나쁜 결과를 받았고, 화면에는 똑같이 "완료"가 찍혔다.

여기서 두 경로를 **추론 없이** 끝까지 돌려 같은 결과가 나오는지 본다.
번역 자체는 가짜로 바꾼다(그건 다른 테스트의 일이다). 확인하려는 것은
파이프라인의 **단계 구성**이 같은가이다.
"""

from __future__ import annotations

import textwrap

import pymupdf
import pytest


ENG = ("The membrane potential of the cell changes when an ion channel opens "
       "and sodium ions flow inward across the lipid bilayer. Membrane "
       "proteins embedded in the lipid bilayer act as ion channels. ")
KOR = ("세포막 전위는 이온 통로가 열려 나트륨 이온이 지질 이중층을 가로질러 "
       "안으로 흐를 때 변화한다. 지질 이중층에 박힌 막 단백질이 이온 통로 "
       "역할을 한다. ")


def _make_pdf(path, body, font, pages=2):
    d = pymupdf.open()
    for _ in range(pages):
        pg = d.new_page()
        y = 60
        for line in textwrap.wrap(body * 3, 46)[:38]:
            pg.insert_text((40, y), line, fontsize=9, fontname=font)
            y += 18
    d.save(path)
    d.close()
    return path


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """추론과 번역 엔진을 걷어낸다. 남는 것은 파이프라인 뼈대뿐."""
    from pdfko import runner, terms

    src = _make_pdf(tmp_path / "src.pdf", ENG, "helv")
    ko = _make_pdf(tmp_path / "ko.pdf", KOR, "korea")

    def fake_chunk(chunk, *a, **k):
        chunk.outdir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(ko) as s:
            d = pymupdf.open()
            d.insert_pdf(s, from_page=0, to_page=chunk.last - chunk.first)
            d.save(chunk.outdir / "x.mono.pdf")
            d.close()
        chunk.mark_done()
        return True

    monkeypatch.setattr(runner, "translate_chunk", fake_chunk)
    monkeypatch.setattr(runner, "clear_engine_cache", lambda: None)
    monkeypatch.setattr(runner.Server, "start_ollama", lambda self: None)
    monkeypatch.setattr(runner.Server, "start_proxy", lambda self, py: None)
    monkeypatch.setattr(runner.Server, "model_ready", lambda self: True)
    monkeypatch.setattr(runner, "stop_all", lambda: None)
    # 용어 단계는 모델이 필요하다 — 껍데기만 남긴다
    monkeypatch.setattr(terms, "keep_terms", lambda rows, **k: rows[:3])
    monkeypatch.setattr(terms, "decide", lambda rows, **k: {})
    return src


def test_cli_runs_end_to_end(stubbed, tmp_path, monkeypatch):
    """명령줄 경로가 산출물과 보고서를 만든다."""
    from pdfko import cli
    work = tmp_path / "w"
    rc = cli.main([str(stubbed), "-w", str(work)])
    assert rc == 0
    assert (work / "src_한국어.pdf").exists()
    assert (work / "품질보고서.md").exists()
    assert not (work / "work").exists()          # 중간물 정리됨


def test_web_runs_end_to_end(stubbed, tmp_path, monkeypatch):
    """브라우저 경로도 같은 산출물을 만든다."""
    from pdfko import web
    work = tmp_path / "wjob"
    work.mkdir()
    job = web.Job(name="src.pdf", src=stubbed, work=work)
    web._run(job, "", None)
    assert job.done and not job.error, job.error
    assert job.out and job.out.exists()
    assert job.report and job.report.exists()


def test_both_paths_do_the_same_steps():
    """단계 구성이 갈리면 한쪽 사용자만 조용히 나쁜 결과를 받는다.

    이 목록이 이번 주에 다섯 번 어긋났다. 새 단계를 한쪽에만 넣으면
    여기서 걸린다.
    """
    import inspect
    from pdfko import cli, web

    cli_src = inspect.getsource(cli._main)
    web_src = inspect.getsource(web._run)
    for step in ("clipscan.scan", "clipscan.clean", "glyphmap.build_table",
                 "clear_engine_cache", "model_ready", "keep_terms", "decide",
                 "plan_chunks", "translate_chunk", "merge", "qa.scan",
                 "coverage", "mixed_language_figures", "repair_pages",
                 "write_report", "cleanup_work"):
        assert step in cli_src, f"cli 에 {step} 없음"
        assert step in web_src, f"web 에 {step} 없음"
