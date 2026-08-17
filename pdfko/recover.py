"""파손된 페이지를 자동으로 되살린다.

## 설계 원칙

문단 단위로 아무리 검증해도 번역 엔진 내부의 조판 실패는 잡히지 않는다.
그래서 **렌더링된 결과를 보고 페이지 단위로 판정**하고, 파손된 페이지만
골라 되살린다. 되살리는 방법은 단계적으로 약해진다.

    1단계  좁은 번역   그 페이지만 '간결하게' 다시 번역해 길이를 줄인다
    2단계  원문 유지   그래도 안 되면 원문 페이지를 그대로 쓴다

2단계까지 가면 그 페이지는 영어로 남는다. 읽을 수 없는 한국어보다 낫지만,
**사용자에게 반드시 알려야 한다.** 조용히 영어로 바꿔치기하면 사용자는
"번역이 빠졌다"고 느낀다. 그래서 보고서에 남기고, 원하면 페이지에 표시도 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from . import qa
from .qa import PageVerdict


@dataclass
class Recovery:
    page: int              # 번역본 쪽번호 (1부터)
    orig_page: int         # 대응하는 원본 쪽번호 (1부터)
    reasons: list[str]
    action: str            # "retranslated" | "reverted" | "kept"


def _mark_reverted(page: pymupdf.Page) -> None:
    """원문으로 되돌린 페이지임을 표시한다.

    문구는 반드시 ASCII 로 둔다. pymupdf 기본 폰트(Helvetica)에는 한글
    글리프가 없어서, 한글로 넣으면 **조용히 아무것도 찍히지 않는다.**
    표시를 남겼다고 믿었는데 실제로는 없던 적이 있다.
    """
    r = page.rect
    page.draw_rect(pymupdf.Rect(r.x0 + 18, r.y1 - 26, r.x0 + 250, r.y1 - 10),
                   color=None, fill=(1, 0.96, 0.90))
    page.insert_text((r.x0 + 22, r.y1 - 15),
                     "[pdfko] original page kept - Korean did not fit",
                     fontsize=6.5, color=(0.65, 0.25, 0.15))


def retranslate_page(page: int, orig_page: int, src: Path, work: Path, *,
                     model: str, proxy_port: int, glossary: Path | None,
                     concise: bool = True) -> Path | None:
    """한 페이지만 다시 번역한다. 성공하면 그 1쪽짜리 PDF 경로.

    되돌리기 전에 반드시 이걸 먼저 시도해야 한다. 겹침의 원인은 '한국어가
    길어 자리가 모자란 것'이므로, 짧게 번역하면 들어갈 여지가 있다.
    40쪽 구간을 통째로 다시 돌릴 필요는 없다 — BabelDOC 은 `--pages 143`
    처럼 단일 페이지를 받는다.
    """
    import json
    import subprocess
    import urllib.request

    # 폴더를 **비우고** 시작한다. 이 경로는 번역본 쪽번호로 잡히는데, 같은
    # 작업 폴더를 다른 `--pages` 로 다시 쓰면 같은 번호가 다른 원본을 뜻한다.
    # 남아 있던 지난번 결과를 집어 들면 **엉뚱한 페이지를 끼워 넣고 "재번역"
    # 이라고 보고한다.** 판정기는 겹침만 보므로 대개 통과해 버린다.
    import shutil as _sh
    out = work / "repair" / f"p{page}"
    _sh.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    def _mode(on: bool) -> None:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/mode",
                data=json.dumps({"concise": on}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    _mode(concise)
    try:
        cmd = [
            "babeldoc", "--files", str(src), "--pages", str(orig_page),
            "--lang-in", "en", "--lang-out", "ko-KR",
            "--openai", "--openai-model", model,
            "--openai-base-url", f"http://127.0.0.1:{proxy_port}/v1",
            "--openai-api-key", "sk-local",
            "--no-auto-extract-glossary",
            "--primary-font-family", "serif",
            "--watermark-output-mode", "no_watermark",
            "--only-include-translated-page",
            "--qps", "10", "--pool-max-workers", "4",
            "--working-dir", str(work / "work"),
            "--output", str(out),
        ]
        if glossary:
            cmd += ["--glossary-files", str(glossary)]
        (work / "logs").mkdir(parents=True, exist_ok=True)
        with (work / "logs" / f"repair_p{page}.log").open("ab") as log:
            r = subprocess.run(cmd, stdout=log, stderr=log)
    finally:
        _mode(False)

    if r.returncode != 0:
        return None            # 실패를 성공과 구별하지 않으면 낡은 파일을 집는다
    # mtime 내림차순. 알파벳순으로 고르면 `RLbook…` 이 `cleaned…` 보다 앞서서
    # 엉뚱한 파일을 집는다 — 같은 실수를 runner.Chunk.pdf() 에서 이미 겪었다.
    got = sorted(out.glob("*.mono.pdf"), key=lambda p: p.stat().st_mtime,
                 reverse=True)
    return got[0] if got else None


def _save_tmp(doc: pymupdf.Document, out_pdf: Path) -> Path:
    """열어 둔 문서를 저장한다. **자기 자신 위에 덮어써도 안전하다.**

    pymupdf 는 연 파일과 같은 경로로 저장하면 거부한다 —
    `ValueError: save to original must be incremental`.
    호출부 대부분이 번역본을 제자리에서 고치므로 이 경로가 기본값이었고,
    그래서 자동 복구는 **실행될 때마다 100% 죽었다.** 파손 0쪽인 문서만
    통과해서 여태 드러나지 않았다.

    `saveIncr()` 로도 되지만 증분 저장은 쓰레기 수집을 못 해 되돌린 페이지의
    옛 객체가 파일에 그대로 쌓인다. 임시 파일에 쓰고 원자적으로 바꿔치운다.

    바꿔치기는 **문서를 닫은 뒤**에 해야 한다. 여기서 닫으면 호출부의
    `with` 가 한 번 더 닫으려다 `document closed` 로 죽는다. 그래서 이
    함수는 임시 경로만 돌려주고, 교체는 `with` 밖에서 한다.
    """
    tmp = out_pdf.with_name(out_pdf.name + ".tmp")
    try:
        doc.save(tmp, garbage=4, deflate=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _replace(tmp: Path, out_pdf: Path) -> None:
    """임시 파일을 결과 자리로 옮긴다. 실패해도 잔재를 남기지 않는다."""
    try:
        tmp.replace(out_pdf)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def splice_page(target: Path, page: int, source: Path, out: Path) -> None:
    """target 의 page 자리에 source 의 1쪽을 끼워 넣는다.

    쪽수와 크기를 확인한다. 엔진이 여러 쪽을 내놓았거나 용지 크기가 다르면
    책 한가운데 크기가 다른 페이지가 조용히 끼어든다.
    """
    with pymupdf.open(target) as doc, pymupdf.open(source) as s:
        if s.page_count != 1:
            raise ValueError(f"끼워 넣을 PDF 가 1쪽이 아니다: {s.page_count}쪽")
        tr, sr = doc[page - 1].rect, s[0].rect
        if abs(tr.width - sr.width) > 1 or abs(tr.height - sr.height) > 1:
            raise ValueError(
                f"용지 크기가 다르다: {tr.width:.0f}×{tr.height:.0f} ≠ "
                f"{sr.width:.0f}×{sr.height:.0f}")
        doc.delete_page(page - 1)
        doc.insert_pdf(s, from_page=0, to_page=0, start_at=page - 1)
        tmp = _save_tmp(doc, out)
    _replace(tmp, out)


def revert_pages(trans_pdf: Path, orig_pdf: Path, pages: list[int],
                 offset: int, out_pdf: Path,
                 mark: bool = True) -> list[Recovery]:
    """지정한 페이지를 원문으로 되돌린다.

    pages : 번역본 기준 쪽번호(1부터)
    offset: 번역본 1쪽 = 원본 (1+offset)쪽
    mark  : 되돌린 페이지 여백에 작은 표시를 남긴다

    pdfseparate + pdfunite 로 하면 낱장 분해·재결합 과정에서 페이지마다 폰트가
    중복 삽입되어 파일이 수십 배로 부풀고 xref 가 깨진다. pymupdf 는 객체를
    공유하며 붙이므로 그런 일이 없다.
    """
    recs: list[Recovery] = []
    with pymupdf.open(trans_pdf) as doc, pymupdf.open(orig_pdf) as src:
        for p in sorted(pages, reverse=True):     # 뒤에서부터 바꿔야 인덱스가 안 밀린다
            o = p + offset
            if not (1 <= o <= src.page_count):
                continue
            doc.delete_page(p - 1)
            doc.insert_pdf(src, from_page=o - 1, to_page=o - 1, start_at=p - 1)
            if mark:
                _mark_reverted(doc[p - 1])
            recs.append(Recovery(page=p, orig_page=o, reasons=[], action="reverted"))
        tmp = _save_tmp(doc, out_pdf)
    _replace(tmp, out_pdf)
    return list(reversed(recs))


def _is_severe(v: PageVerdict) -> bool:
    return v.overlap > 0.15 or v.outside > 0.10 or v.collision > 0.10


def _is_korean(one_page_pdf: Path, floor: float = 0.3) -> bool:
    """재번역 결과가 실제로 한국어인가.

    종료 코드만으로는 부족하다. 상류가 죽어 있어도 **babeldoc 은 0 을 돌려주고
    영어 페이지를 내놓는다** — 실측으로 없는 포트를 줘도 mono.pdf 가 나왔다.
    그걸 그대로 끼우면 영어 페이지를 '재번역 성공'으로 보고하게 된다.
    """
    from .repair import hangul_ratio
    try:
        with pymupdf.open(one_page_pdf) as d:
            txt = d[0].get_text() if d.page_count else ""
    except Exception:
        return False
    if len(txt.split()) < 15:
        return False          # 글자가 거의 없다 — 판정할 수 없으니 믿지 않는다
    return hangul_ratio(txt) >= floor


def repair_pages(trans_pdf: Path, orig_pdf: Path, severe: list[PageVerdict],
                 offset: int, src_pdf: Path, work: Path, *,
                 model: str, proxy_port: int, glossary: Path | None,
                 on_step=None) -> list[Recovery]:
    """파손된 페이지를 사다리로 되살린다. 1단계 간결 재번역 → 2단계 원문 유지.

    1단계를 건너뛰면 안 된다. 원문으로 되돌리는 것은 **고치는 게 아니라
    포기하는 것**이고, 사용자에게는 "번역이 빠졌다"로 보인다. 겹침의 원인은
    한국어가 길어 자리가 모자란 것이므로, 짧게 다시 번역하면 들어갈 여지가
    있다. 그 페이지 하나만 다시 돌리므로 비용도 작다.

    되돌리기는 1단계가 실패한 페이지에만 적용한다.
    """
    recs: list[Recovery] = []
    give_up: list[int] = []
    for v in severe:
        o = v.page + offset
        if on_step:
            on_step(v.page, "간결 재번역")
        got = None
        try:
            got = retranslate_page(v.page, o, src_pdf, work, model=model,
                                   proxy_port=proxy_port, glossary=glossary)
        except Exception:
            got = None
        if got and _is_korean(got):
            try:
                splice_page(trans_pdf, v.page, got, trans_pdf)
                with pymupdf.open(trans_pdf) as t, pymupdf.open(orig_pdf) as s:
                    again = qa.inspect_page(s[o - 1], t[v.page - 1], v.page)
                if not _is_severe(again):
                    recs.append(Recovery(page=v.page, orig_page=o,
                                         reasons=v.reasons, action="retranslated"))
                    continue
            except Exception:
                pass          # 끼워 넣기가 실패하면 되돌리기로 내려간다
        give_up.append(v.page)

    if give_up:
        if on_step:
            on_step(0, f"{len(give_up)}쪽 원문 유지")
        recs += revert_pages(trans_pdf, orig_pdf, give_up, offset, trans_pdf)
    return recs


def write_report(path: Path, verdicts: list[PageVerdict],
                 recoveries: list[Recovery], offset: int) -> None:
    """무엇이 어떻게 처리됐는지 사람이 읽을 수 있게 남긴다.

    조용히 넘어가지 않는 것이 이 파일의 존재 이유다.
    """
    broken = [v for v in verdicts if v.broken]
    lines = [
        "# 번역 품질 보고서",
        "",
        f"- 전체 {len(verdicts)}쪽",
        f"- 파손 감지 {len(broken)}쪽",
        f"- 원문으로 되돌림 {sum(1 for r in recoveries if r.action == 'reverted')}쪽",
        "",
    ]
    if broken:
        lines += ["## 파손이 감지된 페이지", "",
                  "| 번역본 쪽 | 원본 쪽 | 사유 | 처리 |",
                  "|---|---|---|---|"]
        act = {r.page: r.action for r in recoveries}
        for v in broken:
            a = {"reverted": "원문 유지", "retranslated": "재번역"}.get(
                act.get(v.page, ""), "그대로 둠")
            lines.append(f"| {v.page} | {v.page + offset} | "
                         f"{', '.join(v.reasons)} | {a} |")
        lines.append("")
    lines += [
        "## 이 표를 읽는 법", "",
        "- **겹침** — 글자가 서로 포개져 찍혔다. 한국어가 길어 자리가 부족한 경우.",
        "- **영역이탈** — 본문이 원본 여백 밖으로 밀려났다.",
        "- **줄충돌** — 서로 다른 줄이 같은 높이에 끼어들었다.",
        "- **그림혼재** — 그림 속 라벨 일부만 번역되어 언어가 섞였다.",
        "",
        "'원문 유지'로 표시된 페이지는 번역하면 읽을 수 없게 되어 영어 원문을",
        "그대로 두었다. 해당 쪽 하단에 작은 표시가 있다.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
