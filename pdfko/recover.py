"""영어가 남은 페이지만 다시 번역한다.

## 무엇을 고치고 무엇을 안 고치나

고치는 것은 하나다 — **영어가 그대로 남은 쪽.** 번역이 실제로 안 된 것이라
다시 물어볼 이유가 분명하고, 나아졌는지도 같은 잣대로 확인할 수 있다.

자리가 어긋난 쪽(겹침·영역이탈·줄충돌)은 **손대지 않는다.** 예전에는 그것도
고쳤다 — 간결하게 다시 번역하고, 그래도 안 되면 영어 원문을 도로 붙였다.
그런데 한국어가 영어보다 길어 상자를 조금 넘는 것은 흔한 일이라, 잘된 번역이
영어로 되돌아가는 일이 생겼다. 번역이 처음부터 잘되면 쓸모없는 장치이면서,
잘됐을 때 오히려 손해를 끼쳤다.

검사는 그대로 한다(23쪽에 0.0초). 다만 보고서에 적어 사람이 보게 할 뿐,
행동의 근거로 삼지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from . import qa, runner
from .qa import PageVerdict


@dataclass
class Recovery:
    page: int              # 번역본 쪽번호 (1부터)
    orig_page: int         # 대응하는 원본 쪽번호 (1부터)
    reasons: list[str]
    action: str            # "retranslated" | "kept"
    note: str = ""         # 복구 중 터진 오류. 보고서에 그대로 싣는다.


from .proxy import leftover_english


def retranslate_page(page: int, orig_page: int, src: Path, work: Path, *,
                     model: str, proxy_port: int,
                     prompt_file: Path | None = None,
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

    def _mode(on: bool) -> bool:
        """간결 모드 전환. 실패하면 False — 호출부가 알아야 한다.

        조용히 넘기면 간결 모드가 안 켜진 채로 그냥 재번역이 돌고, 보고서에는
        "간결 재번역"이라고 적힌다. 1단계가 하는 일이 없는데 했다고 말하는 셈.
        """
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/mode",
                data=json.dumps({"concise": on}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            return False

    if concise and not _mode(True):
        return None      # 간결 모드가 안 켜지면 1단계는 의미가 없다
    try:
        cmd = [
            "babeldoc", "--files", str(src), "--pages", str(orig_page),
            "--lang-in", "en", "--lang-out", "ko-KR",
            "--openai", "--openai-model", model,
            "--openai-base-url", f"http://127.0.0.1:{proxy_port}/v1",
            "--openai-api-key", "sk-local",
            "--ignore-cache",
            "--no-auto-extract-glossary",
            # 기본값 5 는 `Yes`(3자)·`No`(2자) 같은 도식 라벨을 통째로
            # 건너뛴다. 번역본에 영어가 남아 그림혼재로 잡힌다. 짧은 것도
            # 보낸다 — 프록시가 산문 여부를 따로 보므로 쓰레기까지 번역하지는
            # 않는다.
            "--min-text-length", "1",
            # 자르는 방식은 본 실행과 같아야 한다. 재시도가 다르게 자르면
            # 고치려던 쪽만 다른 규칙으로 조판돼 앞뒤가 안 맞는다.
            "--split-short-lines",
            *runner.split_factor(src),
            "--primary-font-family", "serif",
            "--watermark-output-mode", "no_watermark",
            "--only-include-translated-page",
            "--qps", "10", "--pool-max-workers", "4",
            "--working-dir", str(work / "work"),
            "--output", str(out),
        ]
        # 사용자가 준 지시문은 여기서도 써야 한다. 예전에는 본 번역에만 붙고
        # 복구 재번역에서는 조용히 빠져서, 되살린 페이지만 문체가 달라졌다.
        if prompt_file and prompt_file.exists():
            cmd += ["--custom-system-prompt", prompt_file.read_text(encoding="utf-8")]
        (work / "logs").mkdir(parents=True, exist_ok=True)
        with (work / "logs" / f"repair_p{page}.log").open("wb") as log:
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


def _fragment_note(log_dir: Path) -> list[str]:
    """조각 모드로 번역된 문단을 보고서에 실을 줄로 만든다.

    조각 모드는 자리표시자 사이의 본문만 번역하고 자리표시자는 도구가
    원위치에 끼운다. 구조적으로 실패할 수 없는 대신 **어순이 조각 단위로
    굳는다** — 한국어는 어순이 바뀌는데 수식은 제자리에 남으므로, 수식이
    그것을 설명하는 구절에서 떨어져 나간다. 레이아웃 검사로는 안 잡힌다.
    좌표는 멀쩡하기 때문이다.
    """
    import json as _json
    f = log_dir / "fragments.jsonl"
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(_json.loads(line))
        except Exception:
            continue
    if not rows:
        return []
    out = ["## 어순이 고정된 문단", "",
           f"자리표시자가 많아 **조각 단위로 번역한 문단이 {len(rows)}개** 있습니다. "
           "수식은 제자리에 남고 그 사이 본문만 한국어가 되므로, 수식이 그것을 "
           "설명하는 구절에서 떨어져 보일 수 있습니다. 레이아웃 검사로는 "
           "잡히지 않습니다 — 글자 위치는 멀쩡하기 때문입니다.", ""]
    for r in rows[:5]:
        out.append(f"- `{r.get('src', '')[:90]}…`")
    if len(rows) > 5:
        out.append(f"- … 외 {len(rows) - 5}개")
    out.append("")
    return out


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



def repair_untranslated(trans_pdf: Path, orig_pdf: Path, offset: int,
                        src_pdf: Path, work: Path, *, model: str,
                        proxy_port: int,
                        prompt_file: Path | None = None,
                        on_step=None) -> list[Recovery]:
    """영어가 남은 페이지를 **다시 번역해서** 되살린다.

    ## 왜 좌표 판정으로는 못 찾나

    겹치지도 밀려나지도 않은 채 영어 문장만 남은 페이지는 `qa.scan` 에
    걸리지 않는다. 자리는 멀쩡하기 때문이다. 프록시가 문단 단위로 재시도를
    다 쓰고 포기하면 그걸로 끝이었다. 그래서 따로 훑는다.

    ## 왜 원문으로 되돌리지 않나

    영어가 남은 쪽을 통째로 원문으로 바꾸면 안 된다. 90%가
    한국어인 쪽을 통째로 영어로 바꾸는 것은 고치는 게 아니라 **더 나쁘게
    만드는 것**이다. 재번역이 실패하면 있는 그대로 두고 보고서에 적는다.

    ## 왜 간결 모드를 쓰지 않나

    간결 모드는 '길어서 자리가 없다'는 문제를 푼다. 여기 문제는 길이가
    아니라 번역이 안 된 것이라, 짧게 쓰라고 하면 오히려 내용을 잃는다.
    """
    rows = leftover_pages(trans_pdf)
    recs: list[Recovery] = []
    for page, run in rows:
        o = page + offset
        if on_step:
            on_step(page, "영어 잔류 — 재번역")
        got, why = None, ""
        try:
            got = retranslate_page(page, o, src_pdf, work, model=model,
                                   proxy_port=proxy_port,
                                   prompt_file=prompt_file, concise=False)
        except Exception as e:
            why = f"재번역 실패: {type(e).__name__}: {e}"
        if got and _is_korean(got):
            try:
                # 갈아 끼우기 전에 확인한다. 영어가 그대로면 끼울 이유가 없고,
                # 레이아웃이 깨졌다면 오히려 나빠진다.
                # **끼우기 전에** 다 확인한다. 예전에는 먼저 끼우고 나서
                # 봤고, 레이아웃이 깨졌으면 "되돌림"이라 적었다 — 실제로는
                # 되돌리지 않았다. 보고서만 거짓말을 하고 있었다.
                with pymupdf.open(got) as g, pymupdf.open(orig_pdf) as s:
                    still = leftover_english(g[0].get_text())
                    after = qa.inspect_page(s[o - 1], g[0], page)
                if still:
                    why = "재번역해도 영어가 남음"
                elif _is_severe(after):
                    why = "재번역하면 레이아웃이 깨져 그대로 둠"
                else:
                    splice_page(trans_pdf, page, got, trans_pdf)
                    recs.append(Recovery(page=page, orig_page=o,
                                         reasons=[f"영어 잔류: {run[:40]}"],
                                         action="retranslated"))
                    continue
            except Exception as e:
                why = f"끼워 넣기 실패: {type(e).__name__}: {e}"
        recs.append(Recovery(page=page, orig_page=o,
                             reasons=[f"영어 잔류: {run[:40]}"],
                             action="kept",
                             note=why or "재번역이 한국어를 내놓지 못함"))
    return recs



def leftover_pages(out_pdf: Path) -> list[tuple[int, str]]:
    """번역이 됐는데 영어 문장이 통째로 남은 페이지. [(쪽, 남은 문장)]

    쪽 단위 미번역 검사(`qa.coverage`)로는 안 보인다. 한 쪽이 80% 한국어면
    남은 영어 한 문장은 그 검사를 통과한다 — 실측으로 출고본 490쪽이
    "미번역 0쪽" 으로 찍혔는데 실제로는 145쪽에 영어 문장이 남아 있었다.
    프록시가 재시도로 대부분 잡지만, 다 잡지 못한 것은 세어서 알려야 한다.
    """
    rows: list[tuple[int, str]] = []
    try:
        with pymupdf.open(out_pdf) as d:
            for i in range(d.page_count):
                run = leftover_english(_body_text(d[i]))
                if run:
                    rows.append((i + 1, run))
    except Exception:
        return []
    return rows


# 머리글·쪽번호가 사는 띠. 위아래 8% 를 본문에서 뺀다.
_MARGIN = 0.08


def _body_text(page: pymupdf.Page) -> str:
    """머리글과 쪽번호를 뺀 본문 텍스트.

    번역 엔진은 반복되는 머리글(`Chapter 8: Planning and Learning with
    Tabular Methods`)을 번역하지 않고 그대로 둔다. 그 자체가 아쉬운 점이지만
    **이 수리 루프가 고칠 수 있는 것이 아니다** — 다시 돌려도 엔진은 같은
    이유로 또 건너뛴다. 여기 넣어 두면 책의 거의 모든 쪽이 매번 재번역
    대상이 되어 수리가 끝나지 않는다. 실측으로 30쪽 중 14쪽이 머리글
    하나 때문에 걸렸고, 본문에 실제로 영어가 남은 쪽은 1쪽이었다.
    """
    r = page.rect
    lo, hi = r.y0 + r.height * _MARGIN, r.y1 - r.height * _MARGIN
    out = []
    for b in page.get_text("blocks"):
        y0, y1, txt = b[1], b[3], b[4]
        if y1 < lo or y0 > hi:
            continue
        out.append(txt)
    return "\n".join(out)


def _leftover_note(rows: list[tuple[int, str]], offset: int) -> list[str]:
    if not rows:
        return []
    out = ["## 영어가 남은 페이지", "",
           f"아래 {len(rows)}쪽은 대부분 번역됐지만 영어 문장이 남아 있습니다. "
           "재시도를 다 쓰고도 한국어가 나오지 않은 문단입니다.", "",
           "| 번역본 쪽 | 원본 쪽 | 남은 문장 |", "|---|---|---|"]
    for p, run in rows[:12]:
        out.append(f"| {p} | {p + offset} | "
                   f"{run[:60].replace('|', '/')}… |")
    if len(rows) > 12:
        out.append(f"| … | | 외 {len(rows) - 12}쪽 |")
    out.append("")
    return out


def write_report(path: Path, verdicts: list[PageVerdict],
                 recoveries: list[Recovery], offset: int,
                 log_dir: Path | None = None,
                 out_pdf: Path | None = None) -> None:
    """무엇이 어떻게 처리됐는지 사람이 읽을 수 있게 남긴다.

    조용히 넘어가지 않는 것이 이 파일의 존재 이유다.
    """
    broken = [v for v in verdicts if v.broken]
    left = leftover_pages(out_pdf) if out_pdf else []
    lines = [
        "# 번역 품질 보고서",
        "",
        f"- 전체 {len(verdicts)}쪽",
        f"- 파손 감지 {len(broken)}쪽",
        f"- 영어가 남은 쪽 {len(left)}쪽",
        "",
    ]
    if broken:
        lines += ["## 자리가 어긋난 페이지", "",
                  "아래 쪽은 글자 위치가 원본과 어긋났습니다. **손대지 않았습니다** "
                  "— 자리가 어긋났다는 이유로 다시 번역하거나 영어 원문을 도로 "
                  "붙이면, 잘된 번역까지 잃기 때문입니다. 한국어가 영어보다 길어 "
                  "상자를 조금 넘는 것은 흔한 일입니다. 눈으로 보고 판단하세요.", "",
                  "| 번역본 쪽 | 원본 쪽 | 무엇이 |",
                  "|---|---|---|"]
        for v in broken:
            lines.append(f"| {v.page} | {v.page + offset} | "
                         f"{', '.join(v.reasons)} |")
        lines.append("")
    failed = [r for r in recoveries if r.note]
    if failed:
        lines += ["## 다시 번역해 봤지만 안 된 페이지", "",
                  "영어가 남아 있어 다시 물어봤는데 나아지지 않은 쪽입니다. "
                  "번역본은 그대로 두었습니다.", ""]
        lines += [f"- {r.page}쪽 — {r.note}" for r in failed]
        lines.append("")

    lines += _leftover_note(left, offset)
    if log_dir:
        lines += _fragment_note(log_dir)

    lines += [
        "## 이 표를 읽는 법", "",
        "- **겹침** — 글자가 서로 포개져 찍혔다. 한국어가 길어 자리가 부족한 경우.",
        "- **영역이탈** — 본문이 원본 여백 밖으로 밀려났다.",
        "- **줄충돌** — 서로 다른 줄이 같은 높이에 끼어들었다.",
        "- **그림혼재** — 그림 속 라벨 일부만 번역되어 언어가 섞였다.",
        "",
        "",
        "자리가 어긋났다는 이유로는 손대지 않는다. 한국어가 영어보다 길어",
        "상자를 조금 넘는 일은 흔하고, 그때마다 다시 번역하거나 영어 원문을",
        "도로 붙이면 잘된 번역까지 잃는다. 눈으로 보고 판단하면 된다.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
