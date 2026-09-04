"""pdfko — 영문 교재와 논문을 한국어로, 레이아웃을 유지한 채 번역한다.

    pdfko book.pdf

한 줄이면 끝난다. 사전 점검 → 서버 기동 → 구간 번역 → 병합 →
파손 검사 → 영어 잔존 재번역 → 보고서까지 자동으로 진행된다.
중간에 끊겨도 같은 명령을 다시 실행하면 이어서 간다.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

from . import clipscan, client, glyphmap, paths, qa, recover, runner
from .repair import looks_damaged

_HANGUL = re.compile(r"[가-힣]")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_MULTISPACE = re.compile(r"\s{3,}")
# est_width 가 공백에 매기는 폭. proxy 와 같은 값을 써야 채운 칸이
# 원본과 같은 자리에 선다.
_SPACE_EM = 0.33
# 열로 보려면 줄 높이의 몇 배나 벌어져야 하는가. 실측한 골짜기.
_COLUMN_GAP = 2.0


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def info(s: str) -> None:
    print(f"  {s}")


def step(s: str) -> None:
    print(_c(f"\n▶ {s}", "1;36"))


def warn(s: str) -> None:
    print(_c(f"  ! {s}", "33"))


# ---------------------------------------------------------------- 사전 점검
class Preflight(NamedTuple):
    """사전 점검 결과.

    `verdict` 를 함께 돌려주는 이유는 **앞단이 둘**이기 때문이다. 예전에는
    cli 가 화면에 찍는 문구로만 구분을 표현했고, web 은 그 출력을 통째로
    버린 뒤 자기 문구를 따로 들고 있었다. 그래서 cli 만 고치면 web 은 옛
    문구를 계속 말한다 — 실제로 그랬다.

      ''       문제 없음
      'scan'   문서 전체에 텍스트 레이어가 없다 (스캔본)
      'thin-range'  고른 쪽에만 글자가 없다 (그림뿐인 쪽)
    """

    pages: int
    damaged: bool
    has_text: bool
    verdict: str = ""


def _spread(n: int, k: int) -> list[int]:
    """문서 전체에 고르게 흩어 k 쪽을 고른다.

    앞쪽만 보면 안 된다. 앞머리(표지·목차)에는 텍스트 레이어가 멀쩡하고
    본문은 스캔 이미지인 책이 있다. 앞 40쪽만 탐침하면 "글자가 있으니 전체를
    돌려보라" 고 하고, 사용자가 그대로 하면 몇 시간을 쓰고 영어 PDF 를
    받는다 — 이 검사가 막으려던 바로 그 결과다.
    """
    if n <= k:
        return list(range(n))
    step = n / k
    return sorted({min(n - 1, int(i * step)) for i in range(k)})


def preflight(src: Path, first: int = 1, last: int | None = None
              ) -> Preflight:
    """PDF 를 열어보고 위험 신호를 미리 알린다.

    표본은 **번역할 구간**에서 뽑는다. 예전에는 무조건 앞 40쪽을 봤는데,
    서지 정보가 멀쩡한 책은 본문이 깨져 있어도 손상 없음으로 판정돼 합자
    사전 없이 진행했다.
    """
    import subprocess

    import pymupdf

    with pymupdf.open(src) as d:
        n = d.page_count
        lo = max(0, min(first, n) - 1)
        hi = min(n, (last or n), lo + 40)
        pages = max(hi, lo + 1) - lo
        sample = "".join(d[i].get_text() for i in range(lo, max(hi, lo + 1)))
    info(f"{n}쪽")

    # 문턱은 **표본 쪽수에 비례해야** 한다. 예전에는 고정 500자였다. 40쪽을
    # 뽑을 때는 우습게 넘지만 `-p 13` 으로 한 쪽만 뽑으면 그 한 장이 500자를
    # 채워야 했다. 슬라이드는 원래 글자가 적어서 — 실측한 발표자료 15쪽은
    # 한 쪽도 500자에 닿지 않았다 — 어떤 쪽을 골라도 스캔본으로 거부됐다.
    # README 가 새 사용자에게 처음 권하는 것이 `-p` 미리보기라 더 나빴다.
    # 500/40 = 쪽당 12.5자. 그 비율을 그대로 쓴다.
    if len(sample.strip()) < 12.5 * pages:
        # 판정만 하고 그대로 진행하면 500쪽 스캔본에 서너 시간을 쓰고
        # 영어 PDF 를 내놓는다. 여기서 멈춘다.
        #
        # 다만 **고른 쪽이 얇은 것**과 **문서가 스캔본인 것**은 다르다.
        # 실측: 15쪽 발표자료의 그림뿐인 5쪽을 고르면 "스캔한 PDF 라 번역할
        # 수 없습니다" 가 나왔다 — 14쪽에는 글자가 있는데도. 사용자는 도구를
        # 포기하거나 없는 문제를 고치려 든다(위 `-p` 문단 참고).
        if pages < n:
            probe = _spread(n, 40)
            with pymupdf.open(src) as d:
                whole = "".join(d[i].get_text() for i in probe)
            if len(whole.strip()) >= 12.5 * len(probe):
                # 본 쪽만 말한다. 표본은 최대 40쪽이라 `-p 61-560` 이어도
                # 61-100 만 열어 본다. 범위를 통째로 읊으면 460쪽을 열어
                # 보지도 않고 "글자가 없다" 고 단언하는 셈이다.
                seen = f"{lo + 1}" + (f"-{hi}" if hi > lo + 1 else "")
                warn(f"고른 쪽({seen})에는 번역할 글자가 없습니다 — "
                     f"그림뿐인 쪽입니다.")
                warn("글자가 있는 다른 쪽을 골라보세요.")
                return Preflight(n, False, False, "thin-range")
        warn("텍스트 레이어가 거의 없습니다 — 스캔한 PDF 로 보입니다.")
        warn("글자가 이미지인 문서는 이 도구로 번역할 수 없습니다.")
        return Preflight(n, False, False, "scan")

    damaged = looks_damaged(sample)
    if damaged:
        info("텍스트 레이어 손상 감지 → 합자·글리프를 자동 복구합니다")

    import shutil as _sh
    if not _sh.which("pdffonts"):
        return Preflight(n, damaged, True)  # poppler 없으면 폰트 점검만 건너뛴다
    # 번역할 구간만 본다. 전체에 돌리면 33MB 책에서 3.7초가 매 실행마다
    # 나가는데, 그 결과로 하는 일은 안내 한 줄이다.
    fonts = subprocess.run(
        ["pdffonts", "-f", str(first), "-l", str(min(last or first + 40, first + 40)),
         str(src)], capture_output=True, text=True).stdout
    if fonts and " no " in fonts:
        uni_no = sum(1 for l in fonts.splitlines()[2:]
                     if len(l.split()) >= 5 and l.split()[-2] == "no")
        if uni_no:
            info(f"ToUnicode 없는 폰트 {uni_no}종 — 추출 텍스트가 깨질 수 있습니다")
    return Preflight(n, damaged, True)


# ---------------------------------------------------------------- 본 흐름
def _parse_pages(spec: str | None, total: int) -> tuple[int, int] | str:
    """`13-502`, `7`, 빈 값 → (첫쪽, 끝쪽). 잘못됐으면 오류 문구를 돌려준다.

    맨손 `int()` 로 받던 시절에는 `abc` 가 날 traceback, `5-2` 는 한참 뒤
    엉뚱한 곳에서 "합칠 구간이 없다", **`0-3` 은 조용히 통과**했다.
    `0` 이 통과하면 offset 이 -1 이 되어 모든 페이지가 한 칸 밀린 원본과
    비교되고, 되돌리기가 엉뚱한 원본을 끼워 넣는다.
    """
    if not spec or not spec.strip():
        return 1, total
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec)
    if not m:
        return f"쪽 범위 형식이 잘못됐습니다: {spec!r}  — 예: 13-502 또는 7"
    # `7` 은 7쪽 한 장이다. 예전에는 "7쪽부터 끝까지"로 읽어서, 한 쪽만
    # 보려던 사용자가 500쪽을 돌리게 됐다.
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if lo > total:
        return f"시작 쪽이 문서 범위를 벗어났습니다: {lo}쪽 (전체 {total}쪽)"
    if hi < lo:
        return f"쪽 범위가 뒤집혔습니다: {lo}-{hi}"
    first, last = max(1, lo), min(total, hi)
    if first > last:
        return f"번역할 쪽이 없습니다: {spec} (전체 {total}쪽)"
    return first, last


def build_vocab(src: Path, out: Path) -> int:
    """원본 문서에 실제로 쓰인 낱말을 모아 둔다. → 낱말 수

    프록시가 **낱말 한가운데서 잘려 온 조각**을 알아보는 데 쓴다. 요청 본문만
    봐서는 `Under`(잘린 것)와 `Reward`(멀쩡한 라벨)를 가를 수 없고, 원본에
    그 낱말이 독립적으로 존재하는지를 봐야만 알 수 있다. 합자 사전과 같은
    통로로 넘긴다.
    """
    import pymupdf
    words: set[str] = set()
    with pymupdf.open(src) as d:
        for pg in d:
            words.update(w.lower() for w in _WORD_RE.findall(pg.get_text()))
    out.write_text("\n".join(sorted(words)), encoding="utf-8")
    return len(words)


def _widest(line: str) -> int:
    """그 줄에서 가장 넓은 칸의 칸 수."""
    g = _MULTISPACE.findall(line)
    return max((len(x) for x in g), default=0)


def build_columns(src: Path, out: Path) -> int:
    """도식의 열 간격을 적어 둔다. → 줄 수

    원본에서 열이 나뉘는 방식은 두 가지다.

      ① **여러 칸 공백**으로만 나뉜 한 줄
      ② 같은 baseline 위에 **멀리 떨어진 별개 조각**

    babeldoc 은 둘 다 한 덩어리로 만들어 보낸다 — ①은 공백을 하나로 줄이고,
    ②는 조각을 이어 붙인다. 그러면 번역기가 세 칸을 한 문장으로 읽는다.
    실측(16쪽): 눈금 양 끝의 `Low`(x=34)와 `High`(x=311)가 `낮음 높음` 으로
    붙어 눈금이라는 뜻이 사라졌다.

    공백을 뭉갠 형태를 열쇠로, 값은 두 벌을 넣는다 — 보낼 것과 맞출 것.

    여기 적힌 줄이 늘어도 위험하지 않다. babeldoc 이 **정확히 그 문자열을 한
    항목으로** 보낼 때만 쓰이고, 안 맞으면 아무 일도 일어나지 않는다.
    """
    import json as _json

    import pymupdf
    cols: dict[str, list[str]] = {}
    with pymupdf.open(src) as d:
        for pg in d:
            for b in pg.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    t = "".join(sp["text"] for sp in ln["spans"]).rstrip()
                    if _MULTISPACE.search(t.strip()):
                        # 두 벌을 적는다 — **보낼 것**과 **맞출 것**.
                        #
                        # 보낼 것은 간격을 그 줄의 가장 좁은 칸으로 통일한다.
                        # 넓은 칸을 그대로 보내면 모델이 견디지 못한다.
                        #
                        # 맞출 것은 원본 그대로다. 원본은 행마다 **다른 칸
                        # 수**를 써서 같은 x 에 열을 세운다. 실측(20쪽):
                        #
                        #   wrong decision  5칸  → 2열 x=160
                        #   bad plan       14칸  → 2열 x=161
                        #   hallucination   8칸  → 2열 x=158
                        #
                        # 좁은 쪽으로 통일하면 이 정보가 사라져 열이 행마다
                        # 어긋난다. 정렬은 번역 뒤에 하므로 모델은 넓은 칸을
                        # 볼 일이 없다 — 조판기가 못 견디는 것은 프록시의
                        # 채움 상한이 막는다.
                        body = t.strip()
                        w = min(len(g) for g in _MULTISPACE.findall(body))
                        key = re.sub(r"\s+", " ", body)
                        # 같은 열쇠가 서로 다른 칸으로 두 번 나오면 **넓은
                        # 쪽**을 남긴다. 실측(L03): 9쪽은 14칸, 12쪽은 6칸인데
                        # 공백을 뭉개면 열쇠가 같아진다. 좁은 쪽을 남기면
                        # 넓은 쪽에서 글자가 수식 위로 밀려 겹친다. 넓은 쪽을
                        # 남기면 좁은 쪽이 조금 벌어질 뿐이다.
                        old = cols.get(key)
                        if old and _widest(old[1]) >= _widest(body):
                            continue
                        cols[key] = [_MULTISPACE.sub(" " * w, body), body]
    with pymupdf.open(src) as d:
        for pg in d:
            band: dict[int, list[tuple[float, float, float, str]]] = {}
            for b in pg.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    t = "".join(sp["text"] for sp in ln["spans"]).strip()
                    if not t or _MULTISPACE.search(t):
                        continue          # ①에서 이미 다뤘다
                    size = max((sp["size"] for sp in ln["spans"]), default=0) or 1
                    band.setdefault(round(ln["bbox"][1]), []).append(
                        (ln["bbox"][0], ln["bbox"][2], size, t))
            for parts in band.values():
                if len(parts) < 2:
                    continue
                parts.sort()
                gaps = [b[0] - a[1] for a, b in zip(parts, parts[1:])]
                # 줄 높이의 **두 배**보다 넓게 벌어져야 열로 본다. 글꼴
                # 크기에 매이지 않는 기준이라 문서가 달라져도 흔들리지 않는다.
                #
                # 문턱은 실측한 골짜기다. 줄 안에 수식이 끼어 갈라진 문장이
                # 아래쪽에, 진짜 열이 위쪽에 몰려 있다(548쪽 교재·23쪽 발표
                # 자료):
                #
                #    0.8배  '…copyright holder.' | 'This work is licensed'
                #    1.7배  'regular predictors of' | 'over this interval'
                #   ─────────────────────────────────────────────────────
                #    2.4배  '2023-03-15' | '10'          쪽 바닥글
                #    2.8배  'Yes' | 'No'                 판단 도식
                #    3.9배  'Qt(a)' | 'estimate at time' 기호표
                #   13.5배  'Low' | 'High'               자율성 눈금
                #   21.6배  'Preface …' | 'xiii'         목차
                #
                # 낮게 잡으면 교재의 문장이 두 조각으로 번역된다.
                if any(g < a[2] * _COLUMN_GAP
                       for g, a in zip(gaps, parts)):
                    continue
                key = " ".join(p[3] for p in parts)
                if key in cols:
                    continue
                send = true = parts[0][3]
                for gap, part in zip(gaps, parts[1:]):
                    # 맞출 것은 원본 간격 그대로 — 프록시가 조판기의 한계
                    # 안에서 잘라 쓴다.
                    n = max(1, round(gap / (part[2] * _SPACE_EM)))
                    # 보낼 것은 5칸. 우리 검출기는 3칸부터 열로 보는데,
                    # 모델이 한두 칸을 흘려도 경계가 남도록 여유를 둔다.
                    send += " " * min(n, 5) + part[3]
                    true += " " * n + part[3]
                cols[key] = [send, true]

    out.write_text(_json.dumps(cols, ensure_ascii=False), encoding="utf-8")
    return len(cols)


def dual_path(out: Path) -> Path:
    """대역본 경로. **결과물 이름**을 따라간다.

    예전에는 원본 경로를 따랐는데, 잘라내기를 걷어낸 내부 사본이 `cleaned.pdf`
    라서 `-o live.pdf` 로 돌려도 `cleaned_한영대역.pdf` 가 나왔다. 사용자가
    준 적 없는 이름이고, 번역본과 한 쌍으로 보이지도 않는다.

    `_한국어` 로 끝나면 그 자리를 바꾼다 — `책_한국어.pdf` 옆에
    `책_한국어_한영대역.pdf` 가 아니라 `책_한영대역.pdf` 가 놓인다.
    """
    stem = out.stem
    if stem.endswith("_한국어"):
        stem = stem[:-len("_한국어")]
    return out.with_name(f"{stem}_한영대역.pdf")


def _with_progress(port: int, run):
    """번역이 도는 동안 문단 수를 한 줄로 갱신해 보여 준다.

    총 문단 수는 엔진이 문서를 다 뜯어야 알 수 있다. 모르는 값을 지어내
    가짜 백분율을 그리느니, 실제로 센 것을 그대로 보여 준다.
    """
    import json as _json
    import sys as _sys
    import threading as _th
    import urllib.request as _u

    stop = _th.Event()
    live = _sys.stdout.isatty()

    def tick() -> None:
        seen = 0
        while not stop.wait(1.0):
            try:
                with _u.urlopen(f"http://127.0.0.1:{port}/progress", timeout=2) as r:
                    n = _json.loads(r.read()).get("items", 0)
            except Exception:
                continue
            if n == seen:
                continue
            seen = n
            if live:
                print(f"\r      문단 {n}개 번역함", end="", flush=True)

    t = _th.Thread(target=tick, daemon=True)
    t.start()
    try:
        return run()
    finally:
        stop.set()
        if live:
            print("\r" + " " * 34 + "\r", end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    """어떤 경로로 끝나든 우리가 띄운 서버를 반드시 내린다.

    예전에는 정상 종료 한 곳에서만 내렸다. 구간 실패·병합 실패·
    Ctrl-C 는 프록시를 그대로 남겼고, 그 고아들이 `--fresh` 를
    무력화하고 포트 창을 잠식했다.
    """
    from . import use_safe_output
    use_safe_output()
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\n중단했습니다. 같은 명령을 다시 실행하면 이어서 갑니다.")
        return 130
    finally:
        runner.stop_all()


def _version() -> str:
    """설치된 배포판의 버전. 소스에서 바로 돌릴 때도 죽지 않는다."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("pdfko")
    except PackageNotFoundError:
        return "0+unknown"


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pdfko",
        description="영문 교재와 논문을 레이아웃 그대로 한국어로 번역한다.",
        epilog="종료 코드: 0 성공 · 1 번역 실패 · 2 입력·설정 오류 · "
               "3 모델 없음 · 130 사용자 중단")
    # 버전은 설치 메타데이터에서 읽는다. 여기에 문자열을 박아 두면
    # pyproject.toml 과 어긋나는 날이 오고, 어느 쪽이 맞는지 알 수 없다.
    p.add_argument("--version", action="version", version=f"pdfko {_version()}")
    p.add_argument("pdf", type=Path, help="번역할 PDF 또는 PPTX")
    p.add_argument("-o", "--out", type=Path, help="결과 PDF 경로")
    p.add_argument("-w", "--work", type=Path, help="작업 디렉터리 (기본: ./<이름>_ko)")
    p.add_argument("-p", "--pages", help="번역할 쪽 범위 (예: 13-502). 기본 전체")
    p.add_argument("--chunk", type=int, default=40, help="구간 크기(쪽), 기본 40")
    p.add_argument("--model", default="hy-mt2-7b", help="ollama 모델 태그")
    p.add_argument("--gguf", type=Path, help="등록할 GGUF 파일 (최초 1회)")
    p.add_argument("--prompt", type=Path, help="추가 번역 지시문 파일")
    p.add_argument("--no-recover", action="store_true",
                   help="영어가 남은 쪽을 다시 번역하지 않습니다")
    p.add_argument("--recheck", action="store_true",
                   help="번역은 건너뛰고 검사·보고서만 다시 만듭니다")
    p.add_argument("--fresh", action="store_true",
                   help="캐시를 비우고 처음부터 (검증 규칙을 바꿨을 때)")
    a = p.parse_args(argv)

    # --recheck 는 번역을 하지 않으므로 캐시를 비우는 것이 무의미하다.
    # 조용히 무시하면 사용자는 캐시가 지워진 줄 안다. PPTX 쪽은 이미
    # 무시되는 옵션을 오류로 막고 있는데 여기만 빠져 있었다.
    if a.recheck and a.fresh:
        print("--recheck 와 --fresh 는 같이 쓸 수 없습니다 "
              "(--recheck 는 번역을 하지 않으므로 캐시를 비울 이유가 없습니다)")
        return 2

    # 없는 프롬프트 파일을 조용히 무시하면 안 된다. 오타 하나로 지시문이
    # 빠진 채 500쪽을 돌리고, 사용자는 적용된 줄 안다. 번역 엔진은 없는
    # 파일을 그냥 무시하고, `Server.signature` 도 OSError 를 삼킨다.
    if a.prompt and not a.prompt.expanduser().exists():
        print(f"--prompt 파일이 없습니다: {a.prompt}")
        return 2

    src = a.pdf.expanduser().resolve()
    if not src.exists():
        print(f"파일이 없습니다: {src}")
        return 2
    if src.suffix.lower() not in (".pdf", ".pptx", ".ppt"):
        print(f"지원하지 않는 형식입니다: {src.suffix or '(확장자 없음)'}"
              "  — .pdf 또는 .pptx 를 넣어주세요")
        return 2
    if src.stat().st_size == 0:
        print("빈 파일입니다.")
        return 2

    import shutil as _sh
    need = ["ollama"] + ([] if src.suffix.lower() != ".pdf" else ["babeldoc"])
    lack = [b for b in need if not _sh.which(b)]
    if lack:
        print("필요한 프로그램이 없습니다: " + ", ".join(lack))
        print("  babeldoc:  uv tool install --python 3.12 babeldoc")
        print("  ollama:    https://ollama.com/download")
        return 3

    # PPTX 에서 무시되는 PDF 전용 옵션은 조용히 넘기지 않는다.
    if src.suffix.lower() in (".pptx", ".ppt"):
        ignored = [n for n, v in (("--pages", a.pages), ("--chunk", a.chunk != 40),
                                  ("--prompt", a.prompt),
                                  ("--recheck", a.recheck), ("--fresh", a.fresh),
                                  ("--no-recover", a.no_recover)) if v]
        if ignored:
            print("PPTX 에서는 쓸 수 없는 옵션입니다: " + ", ".join(ignored))
            return 2

    if src.suffix.lower() in (".pptx", ".ppt"):
        return _run_pptx(src, a)

    # 작업 폴더를 만들기 **전에** PDF 가 열리는지 본다. 아니면 못 읽는 파일에도
    # 작업 트리와 해시 표식이 남고, 그다음에야 C 레벨 예외가 튄다.
    import pymupdf as _fitz
    try:
        with _fitz.open(src) as _d:
            total = _d.page_count
        if total == 0:
            print("페이지가 없는 PDF 입니다.")
            return 2
    except Exception as e:
        print(f"PDF 를 열 수 없습니다 ({type(e).__name__}): {src}")
        print("  손상된 파일이거나 PDF 가 아닐 수 있습니다.")
        return 2

    # 쪽 범위는 **여기서** 검증한다. 예전에는 작업 폴더를 만들고 사전 점검과
    # 숨은 글자 청소(500쪽이면 수 분, cleaned.pdf 34MB)를 전부 마친 뒤에야
    # `--pages abc` 를 거절했다. 문서를 읽을 필요조차 없는 문법 오류다.
    rng = _parse_pages(a.pages, total)
    if isinstance(rng, str):
        print(rng)
        return 2
    first, last = rng


    work = (a.work or paths.work_for(src)).expanduser().resolve()
    out = (a.out or work / f"{src.stem}_한국어.pdf").expanduser().resolve()
    if work.exists() and not work.is_dir():
        print(f"작업 경로가 디렉터리가 아닙니다: {work}")
        return 2
    # 출력 경로는 **번역을 시작하기 전에** 확인한다. 몇 시간 돌린 뒤 저장
    # 단계에서 권한 오류로 죽으면 결과를 통째로 잃는다. 디렉터리를 가리키면
    # pymupdf 가 그 디렉터리를 지우고 파일로 덮어쓴다(빈 폴더가 사라진다).
    if out.exists() and out.is_dir():
        print(f"결과 경로가 이미 디렉터리입니다: {out}")
        print("  -o 에는 파일 경로를 주세요.")
        return 2
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _probe = out.parent / f".pdfko_write_test_{os.getpid()}"
        _probe.touch()
        _probe.unlink()
    except OSError as e:
        print(f"결과를 저장할 수 없는 경로입니다: {out.parent}  ({e.strerror})")
        return 2

    for d in ("parts", "logs", "cache", "work"):
        (work / d).mkdir(parents=True, exist_ok=True)

    # 작업 디렉터리에 **어떤 문서**를 넣었는지 새겨 둔다.
    # 구간 완료 표식(.done)만 보고 이어가면, -w 를 재사용하거나 두 책의
    # 파일 이름이 같을 때 앞 문서의 번역본을 다음 문서의 결과라며 내놓는다.
    # 실측으로 다른 PDF 를 넣었는데 앞 책 내용이 그대로 나왔다.
    import hashlib as _h
    import shutil as _shutil
    digest = _h.sha256(src.read_bytes()).hexdigest()[:16]
    stamp = work / "source.sha256"
    if stamp.exists() and stamp.read_text().strip() != digest:
        info("작업 폴더에 다른 문서의 기록이 있습니다 — 구간을 비우고 새로 시작합니다")
        _shutil.rmtree(work / "parts", ignore_errors=True)
        (work / "parts").mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest)

    step("사전 점검")
    # 손상 판정은 **번역할 구간**을 봐야 한다. 문서 앞 40쪽만 표본으로 삼으면,
    # 앞쪽 서지 정보가 멀쩡한 책은 본문이 깨져 있어도 사전 없이 진행한다.
    pre = preflight(src, first, last)
    if not pre.has_text:
        return 2
    info(f"번역 범위 {first}-{last}쪽")

    # 잘라내기로 가려진 '보이지 않는 글자'를 미리 제거한다.
    # 번역 엔진은 잘라내기를 무시하고 그 글자까지 번역해 진짜 본문 위에
    # 겹쳐 찍는다. 이 책에서 페이지 하나에 최대 1,094자가 숨어 있었다.
    # 번역할 구간만 훑는다. 전체를 훑으면 548쪽 문서에서 쓰지도 않을 페이지를
    # 재고, 게다가 clean() 안에서 한 번 더 훑어 같은 일을 두 번 한다.
    scans = clipscan.scan(src, first, last)
    unreadable = [r.page for r in scans if r.error]
    if unreadable:
        # 조용히 넘기면 그 페이지의 숨은 글자가 남아 본문 위에 겹쳐 찍힌다.
        warn(f"{len(unreadable)}쪽은 내용을 읽지 못해 숨은 글자 검사를 "
             f"건너뛰었습니다 (예: {', '.join(str(p) for p in unreadable[:5])})")
    hidden = [r for r in scans if r.hidden >= 40]
    if hidden:
        info(f"가려진 글자가 있는 페이지 {len(hidden)}쪽 — 청소본을 만듭니다")
        cleaned = work / "cleaned.pdf"
        touched, rolled, lost = clipscan.clean(
            src, cleaned, pages=[r.page for r in hidden], min_hidden=40)
        info(f"청소 {len(touched)}쪽"
             + (f", 안전 복구 {len(rolled)}쪽" if rolled else "")
             + (f", 낱말 손실 {sum(len(v) for v in lost.values())}개" if lost else ""))
        for pg, words in sorted(lost.items()):
            info(f"    {pg}쪽에서 잃은 낱말: {', '.join(words)}")
        if touched:
            src = cleaned          # 이후 모든 번역은 청소본을 쓴다

    offset = first - 1

    chunks = runner.plan_chunks(first, last, a.chunk, work)
    info(f"{len(chunks)}개 구간 × 최대 {a.chunk}쪽")

    if not a.recheck:
        if a.fresh:
            step("캐시 비우기")
            # 이 작업 폴더의 프록시가 이미 떠 있으면 **먼저 내려야 한다.**
            # 살아 있는 프로세스가 삭제된 DB 의 inode 를 계속 쥐고 있어서,
            # 파일만 지우면 `--fresh` 가 조용히 아무 일도 하지 않는다.
            # Ctrl-C 뒤 재실행이 정확히 이 상황이다.
            runner.stop_all()
            probe = runner.Server(work, a.model)
            if probe.drop_own_proxy():
                info("도는 프록시를 먼저 내렸습니다 (캐시 파일을 쥐고 있었습니다)")
            for c in chunks:
                (c.outdir / ".done").unlink(missing_ok=True)
            # 문단 캐시도 지워야 한다. 예전에는 엔진 캐시와 구간 표식만
            # 지워서, `--fresh` 를 줘도 프록시가 옛 번역을 그대로 돌려줬다.
            # WAL/SHM 까지 지워야 한다 — 남겨 두면 지운 행이 되살아난다.
            for suffix in ("", "-wal", "-shm"):
                (work / "cache" / f"trans.db{suffix}").unlink(missing_ok=True)
            info("엔진 캐시·문단 캐시·구간 표식을 지웠습니다")

        # 깨진 합자 사전을 원본에서 직접 만든다.
        # 엔진은 `di↵erent` 의 `↵` 를 수식으로 오인해 `di{v1}erent` 로 마스킹하고,
        # 번역이 끝나면 그 자리에 원래 글리프(`↵`)를 도로 그려 넣는다. 한국어
        # 본문 한가운데 `↵` 가 박히는 이유다. 사전이 있으면 프록시가 번역 전에
        # 자리표시자를 녹여 `different` 로 되돌린다.
        # `damaged` 판정에 걸지 않는다. `looks_damaged` 는 표본에서 20개
        # 넘게 나와야 참인데, 몇 쪽만 돌리면 그 문턱에 못 미친다. 실측으로
        # `-p 155` 한 장 번역에서 사전이 안 만들어져 `↵` 6개가 그대로 나왔다.
        # 사전 만들기는 텍스트 한 번 훑는 비용뿐이고, 찾은 게 없으면 빈 사전이다.
        # 구간이 아니라 **문서 전체**에서 만든다. 같은 합자 낱말이 책 곳곳에
        # 나오므로 표본이 넓을수록 잘 잡힌다. 실측: 155쪽 한 장에서 1쌍,
        # 548쪽 전체에서 91쌍이고 비용은 1.4초뿐이다.
        srv_glyphmap = None
        gm = glyphmap.build_table(src)
        if gm:
            step("합자 사전")
            gm_path = work / "glyphmap.json"
            glyphmap.save(gm, gm_path)
            srv_glyphmap = gm_path

            info(f"손상된 합자 {len(gm)}쌍을 원본에서 찾았습니다")

        step("서버 기동")
        srv = runner.Server(work, a.model)
        vocab_path = work / "vocab.txt"
        n_vocab = build_vocab(src, vocab_path)
        srv.glyphmap = srv_glyphmap
        srv.vocab = vocab_path if n_vocab else None
        cols_path = work / "columns.json"
        srv.columns = cols_path if build_columns(src, cols_path) else None
        # 용어집·프롬프트가 바뀌면 캐시가 무효화되어야 한다. 요청 본문에서는
        # 뽑을 수 없다 — BabelDOC 은 그것들을 user 메시지 안에 말아 넣는다.
        srv.start_ollama()
        info(f"추론 서버 :{srv.op}")
        # 이미 떠 있던 서버를 빌려 쓰면 모델 저장소도 그 서버 것이다.
        # 모른 척하면 "등록은 한 번이면 된다"는 약속이 조용히 깨진다 —
        # 다음에 우리가 서버를 띄우면 다른 저장소를 보면서 모델이 없다고 하고,
        # 사용자가 다시 등록하면 똑같은 6GB 가 한 벌 더 생긴다.
        if srv.borrowed:
            st = srv.model_store()
            if st and st.resolve() != runner.MODEL_STORE.resolve():
                info(f"  이미 떠 있는 ollama 를 씁니다 — 모델 저장소는 {st}")
        if a.gguf:
            runner.ensure_model(work, a.gguf.resolve(), a.model, srv.op)
            info(f"모델 등록 {a.model}")
        # 번역을 시작하기 **전에** 모델이 있는지 본다. 없으면 엔진이 영어를
        # 그대로 내놓고 종료 코드 0 을 돌려준다 — 500쪽이면 서너 시간 뒤에야 안다.
        if not srv.model_ready():
            warn(f"모델 '{a.model}' 이 추론 서버에 없습니다.")
            warn(f"    OLLAMA_HOST=127.0.0.1:{srv.op} ollama list   ← 확인")
            warn(f"    pdfko {src.name} --gguf <모델.gguf>          ← 최초 1회 등록")
            return 3
        srv.user_sig = runner.Server.signature(a.prompt)
        srv.start_proxy(sys.executable)
        info(f"미들웨어 :{srv.pp}")
        pl = srv.proxy_log_dir()
        if pl and pl.resolve() != (work / "logs").resolve():
            info(f"  앞선 실행의 프록시를 재사용합니다 — 미들웨어 로그는 {pl}")

        step("번역")
        # 어떤 설정으로 끝난 구간인지 대조할 지문. pdfko 가 엔진을 부르는
        # 방식이 바뀌면 지문이 달라져, 끝난 구간도 다시 번역한다.
        stamp = runner.settings_stamp(runner.babeldoc_cmd(
            src, work, "", work, model=a.model, proxy_port=0,
            prompt_file=a.prompt))
        for i, c in enumerate(chunks, 1):
            if c.done(stamp):
                info(f"[{i}/{len(chunks)}] {c.name} 건너뜀 (완료됨)")
                continue
            info(f"[{i}/{len(chunks)}] {c.name}쪽 …")
            # 번역이 도는 동안 미들웨어에 진행을 물어 한 줄로 갱신한다.
            # 구간이 하나뿐이면(기본 40쪽이라 흔하다) 이게 없으면 몇 분 동안
            # 아무 소식이 없다. babeldoc 의 진행 표시는 파이프로 넘기면 끝에
            # 한 번만 나와서 못 쓴다 — 실측으로 열한 줄이 0.01초에 몰렸다.
            ok = _with_progress(
                srv.pp,
                lambda: runner.translate_chunk(
                    c, src, work, model=a.model, proxy_port=srv.pp,
                    prompt_file=a.prompt))
            if not ok:
                warn(f"{c.name} 실패 — logs/part_{c.name}.log 를 확인하세요. "
                     f"같은 명령을 다시 실행하면 여기서부터 이어갑니다.")
                return 1

    step("병합")
    try:
        n = runner.merge(chunks, out)
    except RuntimeError as e:
        # `--recheck` 를 번역 전에 쓰면 여기로 온다. 날 traceback 대신
        # 무엇을 해야 하는지 말한다.
        print(f"  {e}")
        if a.recheck:
            warn("--recheck 는 이미 번역한 결과를 다시 검사하는 옵션입니다. "
                 "먼저 --recheck 없이 한 번 실행하세요.")
        return 1
    info(f"{n}쪽 → {out.name}")

    # 대역본은 덤이다. 엔진이 이미 구간마다 만들어 두었으니 합치기만 하면
    # 된다. 실패해도 번역본은 멀쩡하므로 여기서 작업을 죽이지 않는다.
    dual_out = dual_path(out)
    try:
        nd = runner.merge_dual(chunks, dual_out)
        if nd:
            info(f"{nd}쪽 → {dual_out.name} (원문·번역 나란히)")
    except Exception as e:
        info(f"대역본은 만들지 못했습니다 ({type(e).__name__}) — 번역본은 정상입니다")
        dual_out = None
    else:
        if not nd:
            dual_out = None

    # 번역이 실제로 됐는지부터 본다. 레이아웃 검사는 그다음이다 —
    # 영어 그대로인 페이지는 레이아웃이 완벽하다.
    judged, empty = qa.coverage(str(out))
    if judged and len(empty) == judged:
        print()
        warn(f"번역이 하나도 되지 않았습니다 — {judged}쪽 전부 영어입니다.")
        warn("추론 서버나 모델 이름을 확인하세요:")
        warn(f"    OLLAMA_HOST=127.0.0.1:{srv.op if not a.recheck else 11500} "
             f"ollama list")
        warn(f"결과 파일 {out.name} 은 번역되지 않은 상태입니다.")
        return 1
    if empty:
        warn(f"{len(empty)}/{judged}쪽이 영어로 남았습니다 "
             f"(예: {', '.join(str(p) for p in empty[:8])}"
             f"{' …' if len(empty) > 8 else ''}) — 보고서를 확인하세요")

    step("레이아웃 파손 검사")
    verdicts = qa.scan(str(src), str(out), offset=offset)
    import pymupdf
    with pymupdf.open(out) as d:
        for v in verdicts:
            mx = qa.mixed_language_figures(d[v.page - 1])
            if mx:
                v.reasons.append(f"그림혼재{mx}")
    broken = [v for v in verdicts if v.broken]
    info(f"파손 {len(broken)}쪽")

    # 좌표 판정으로는 **손대지 않는다.** 복구는 번역이 잘못됐을 때 쓰는
    # 장치인데, 상자를 넘었다는 이유로 부르면 잘된 번역을 되돌린다. 한국어가
    # 영어보다 길어 살짝 넘치는 것은 흔한 일이다. 검사 결과는 보고서에
    # 남겨 사람이 보게 하고, 고치는 것은 아래 한 가지 경우뿐이다.
    recs = []

    # 영어가 그대로 남은 쪽. 이건 번역이 실제로 안 된 것이라 다시 물어볼
    # 이유가 분명하다. 겹치지도 밀려나지도 않은 채 문장만 영어라, 좌표
    # 판정에는 걸리지 않는다.
    if not a.no_recover and not a.recheck:
        left = recover.leftover_pages(out)
        if left:
            step("영어가 남은 쪽 재번역")
            info(f"{len(left)}쪽")
            try:
                more = recover.repair_untranslated(
                    out, src, offset, src, work,
                    model=a.model, proxy_port=srv.pp,
                    prompt_file=a.prompt,
                    on_step=lambda p, what: info(f"  {p}쪽 {what}"))
                recs += more
                fixed = sum(1 for r in more if r.action == "retranslated")
                info(f"{fixed}쪽 살림, {len(more) - fixed}쪽 그대로")
            except Exception as e:
                warn(f"재번역 실패({type(e).__name__}: {e}) — "
                     f"번역본 {out.name} 은 그대로 쓸 수 있습니다")

    rep = work / "품질보고서.md"
    recover.write_report(rep, verdicts, recs, offset,
                         log_dir=work / "logs", out_pdf=out)

    runner.stop_all()
    # 엔진이 남긴 중간 산출물은 쪽수에 비례해 쌓인다(3쪽에 13MB).
    # 결과 PDF 를 만든 뒤에는 쓸모가 없다.
    runner.cleanup_work(work)
    step("완료")
    info(f"결과   {out}")
    info(f"보고서 {rep}")
    if broken:
        info(f"레이아웃 검사에서 {len(broken)}쪽이 걸렸습니다 — 손대지 않았습니다. "
             f"보고서에서 무엇이 걸렸는지 볼 수 있습니다")
    return 0


# ---------------------------------------------------------------- PPTX
def _run_pptx(src: Path, a) -> int:
    """PPTX 는 편집 가능한 문서라 조판을 되살릴 필요가 없다.

    런(run) 단위로 글자만 갈아끼우면 서식·위치·애니메이션이 그대로 남고,
    파워포인트가 알아서 다시 흘려 준다. 상자에 안 들어갈 때만 글씨를 줄인다.
    """
    from . import pptx_doc

    if src.suffix.lower() == ".ppt":
        print("옛 형식(.ppt)은 읽을 수 없습니다. 파워포인트에서 .pptx 로 저장해주세요.")
        return 2

    work = (a.work or paths.work_for(src)).expanduser().resolve()
    out = (a.out or work / f"{src.stem}_한국어.pptx").expanduser().resolve()
    # 원본 위에 쓰면 안 된다. python-pptx 는 패키지를 메모리에 들고 있어서
    # 조용히 성공하고, 사용자의 원본이 사라진다. PDF 경로는 pymupdf 가
    # 같은 경로 저장을 거부해서 우연히 안전했을 뿐이다.
    if out.resolve() == src.resolve():
        print("결과 경로가 원본과 같습니다 — 원본을 덮어쓰지 않았습니다.")
        print(f"  다른 이름을 주세요: -o {src.stem}_한국어.pptx")
        return 2
    if out.exists() and out.is_dir():
        print(f"결과 경로가 이미 디렉터리입니다: {out}")
        return 2
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _probe = out.parent / f".pdfko_write_test_{os.getpid()}"
        _probe.touch()
        _probe.unlink()
    except OSError as e:
        print(f"결과를 저장할 수 없는 경로입니다: {out.parent}  ({e.strerror or e})")
        return 2
    (work / "logs").mkdir(parents=True, exist_ok=True)

    step("사전 점검")
    try:
        prs, units = pptx_doc.extract(src)
    except Exception as e:
        print(f"PPTX 를 열 수 없습니다 ({type(e).__name__}): {src}")
        return 2
    # 차트·SmartArt 안의 글자는 손이 닿지 않는다. **세는 시점이 여기여야 한다** —
    # 나중에 세면 "번역 11/11개 (100%)" 를 찍어 놓고 그 아래에 "영어 6개가
    # 남았다"를 붙이게 된다. README 가 하지 않겠다고 적어 둔 바로 그 짓이다.
    stuck = pptx_doc.untouchable_text(prs)
    total_units = len(units) + len(stuck)
    info(f"슬라이드 {len(prs.slides)}장, 번역할 문단 {len(units)}개"
         + (f" (+ 차트·SmartArt {len(stuck)}개는 번역 불가)" if stuck else ""))
    if not units:
        warn("번역할 텍스트가 없습니다")
        return 1

    step("서버 확인")
    srv = runner.Server(work, a.model)
    srv.start_ollama()
    if a.gguf:
        runner.ensure_model(work, a.gguf.resolve(), a.model, srv.op)
    if not srv.model_ready():
        warn(f"모델 '{a.model}' 이 추론 서버에 없습니다 "
             f"(OLLAMA_HOST=127.0.0.1:{srv.op} ollama list 로 확인)")
        return 3
    srv.start_proxy(sys.executable)
    info(f"미들웨어 :{srv.pp}")

    step("번역")
    keys = [(u.slide, u.path, u.para) for u in units]
    trans: dict = {}
    echoed = 0          # 프록시가 검증에 실패해 원문을 그대로 돌려준 건수
    BATCH = 12
    for i in range(0, len(units), BATCH):
        chunk = units[i:i + BATCH]
        got = client.translate_batch(
            [{"id": i + j, "input": u.text} for j, u in enumerate(chunk)],
            port=srv.pp, model=a.model)
        for j, u in enumerate(chunk):
            v = got.get(i + j)
            if not v:
                continue
            # 미들웨어는 검증에 실패하면 **원문을 그대로** 돌려준다. 그걸
            # 성공으로 세면 100% 번역했다고 보고하면서 영어 파일을 내놓게 된다.
            # 한글 비율로 판정한다. `search` 만 쓰면 한 글자만 섞여도 통과하고,
            # else 분기로 흘려보내면 영어를 번역으로 세게 된다.
            alpha = sum(1 for c in v if c.isalpha())
            ko = sum(1 for c in v if "가" <= c <= "힣")
            if alpha and ko / alpha >= 0.3:
                trans[keys[i + j]] = v
            elif not alpha:
                trans[keys[i + j]] = v          # 숫자·기호뿐이면 그대로 둔다
            else:
                echoed += 1
        info(f"  {min(i + BATCH, len(units))}/{len(units)}")

    done = len(trans)
    # 분모는 차트까지 포함한 전체다. 손이 닿는 것만 세면 영어가 남아 있는데도
    # 100% 라고 말하게 된다.
    info(f"번역 {done}/{total_units}개 ({done * 100 // max(total_units, 1)}%)"
         + (f" — 검증 실패로 원문 반환 {echoed}개" if echoed else "")
         + (f", 차트·SmartArt {len(stuck)}개 번역 불가" if stuck else ""))

    if done == 0:
        warn("한 문단도 번역되지 않았습니다 — 추론 서버를 확인하세요")
        warn("결과 파일을 만들지 않습니다 (영어 그대로인 파일이 남으면 혼동됩니다)")
        return 1

    step("되돌려 넣기")
    reports = pptx_doc.apply(prs, trans)
    shrunk = sum(len(r.shrunk) for r in reports)
    merged = sum(len(r.skipped) for r in reports)
    out.parent.mkdir(parents=True, exist_ok=True)
    pptx_doc.save(prs, out)
    info(f"저장 {out.name}" + (f" (글씨 축소 {shrunk}곳)" if shrunk else ""))
    if merged:
        info(f"문단 {merged}곳은 여러 서식이 하나로 합쳐졌습니다 "
             f"(문장 속 굵게·색·링크는 보존되지 않습니다)")

    step("완료")
    info(f"결과 {out}")
    if done < len(units):
        info(f"번역 안 된 문단 {len(units) - done}개는 원문 그대로 남았다")
    if stuck:
        warn(f"차트·SmartArt 안의 텍스트 {len(stuck)}개는 번역되지 않았습니다 "
             f"(파워포인트 한계 — 직접 고쳐야 합니다)")
        for s in stuck[:5]:
            info(f"    {s[:60]}")
        if len(stuck) > 5:
            info(f"    … 외 {len(stuck) - 5}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
