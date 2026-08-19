"""pdfko — 영문 교재와 논문을 한국어로, 레이아웃을 유지한 채 번역한다.

    pdfko book.pdf

한 줄이면 끝난다. 사전 점검 → 서버 기동 → 구간 번역 → 병합 →
파손 검사 → 자동 복구 → 보고서까지 자동으로 진행된다.
중간에 끊겨도 같은 명령을 다시 실행하면 이어서 간다.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import clipscan, client, glyphmap, qa, recover, runner
from .repair import looks_damaged

_HANGUL = re.compile(r"[가-힣]")


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def info(s: str) -> None:
    print(f"  {s}")


def step(s: str) -> None:
    print(_c(f"\n▶ {s}", "1;36"))


def warn(s: str) -> None:
    print(_c(f"  ! {s}", "33"))


# ---------------------------------------------------------------- 사전 점검
def preflight(src: Path, first: int = 1, last: int | None = None
              ) -> tuple[int, bool, bool]:
    """PDF 를 열어보고 위험 신호를 미리 알린다. (쪽수, 손상여부, 텍스트있음)

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
        sample = "".join(d[i].get_text() for i in range(lo, max(hi, lo + 1)))
    info(f"{n}쪽")

    if len(sample.strip()) < 500:
        # 판정만 하고 그대로 진행하면 500쪽 스캔본에 서너 시간을 쓰고
        # 영어 PDF 를 내놓는다. 여기서 멈춘다.
        warn("텍스트 레이어가 거의 없습니다 — 스캔한 PDF 로 보입니다.")
        warn("글자가 이미지인 문서는 이 도구로 번역할 수 없습니다.")
        return n, False, False

    damaged = looks_damaged(sample)
    if damaged:
        info("텍스트 레이어 손상 감지 → 합자·글리프를 자동 복구합니다")

    import shutil as _sh
    if not _sh.which("pdffonts"):
        return n, damaged, True  # poppler 가 없으면 폰트 점검만 건너뛴다
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
    return n, damaged, True


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


def main(argv: list[str] | None = None) -> int:
    """어떤 경로로 끝나든 우리가 띄운 서버를 반드시 내린다.

    예전에는 정상 종료 한 곳에서만 내렸다. 구간 실패·병합 실패·
    Ctrl-C 는 프록시를 그대로 남겼고, 그 고아들이 `--fresh` 를
    무력화하고 포트 창을 잠식했다.
    """
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\n중단했습니다. 같은 명령을 다시 실행하면 이어서 갑니다.")
        return 130
    finally:
        runner.stop_all()


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pdfko",
        description="영문 교재와 논문을 레이아웃 그대로 한국어로 번역한다.")
    p.add_argument("pdf", type=Path, help="번역할 PDF 또는 PPTX")
    p.add_argument("-o", "--out", type=Path, help="결과 PDF 경로")
    p.add_argument("-w", "--work", type=Path, help="작업 디렉터리 (기본: ./<이름>_ko)")
    p.add_argument("-p", "--pages", help="번역할 쪽 범위 (예: 13-502). 기본 전체")
    p.add_argument("--chunk", type=int, default=40, help="구간 크기(쪽), 기본 40")
    p.add_argument("--model", default="hy-mt2-7b", help="ollama 모델 태그")
    p.add_argument("--gguf", type=Path, help="등록할 GGUF 파일 (최초 1회)")
    p.add_argument("--glossary", type=Path, help="용어집 CSV (source,target)")
    p.add_argument("--no-glossary", action="store_true",
                   help="용어 자동 통일을 끈다 (기본은 켜짐)")
    p.add_argument("--make-glossary", type=Path, nargs="?", const=Path("glossary.csv"),
                   metavar="CSV",
                   help="이 문서에서 용어 후보를 뽑아 CSV 로 저장하고 끝낸다 "
                        "(번역어 칸은 비어 있으니 채워서 --glossary 로 쓰면 된다)")
    p.add_argument("--prompt", type=Path, help="추가 번역 지시문 파일")
    p.add_argument("--no-recover", action="store_true",
                   help="파손 페이지를 원문으로 되돌리지 않습니다")
    p.add_argument("--recheck", action="store_true",
                   help="번역은 건너뛰고 검사·복구만 다시 합니다")
    p.add_argument("--fresh", action="store_true",
                   help="캐시를 비우고 처음부터 (검증 규칙을 바꿨을 때)")
    a = p.parse_args(argv)

    # --recheck 는 번역을 하지 않으므로 캐시를 비우는 것이 무의미하다.
    # 조용히 무시하면 사용자는 캐시가 지워진 줄 안다. PPTX 쪽은 이미
    # 무시되는 옵션을 오류로 막고 있는데 여기만 빠져 있었다.
    if a.make_glossary and (a.no_glossary or a.glossary):
        other = "--no-glossary" if a.no_glossary else "--glossary"
        print(f"--make-glossary 와 {other} 는 같이 쓸 수 없습니다 "
              f"(--make-glossary 는 후보만 뽑고 번역하지 않습니다)")
        return 2

    if a.recheck and a.fresh:
        print("--recheck 와 --fresh 는 같이 쓸 수 없습니다 "
              "(--recheck 는 번역을 하지 않으므로 캐시를 비울 이유가 없습니다)")
        return 2

    # 없는 용어집·프롬프트를 조용히 무시하면 안 된다. 오타 하나로 용어집이
    # 빠진 채 500쪽을 돌리고, 사용자는 적용된 줄 안다. 번역 엔진은 없는
    # 파일을 그냥 무시하고, `Server.signature` 도 OSError 를 삼킨다.
    for flag, path in (("--glossary", a.glossary), ("--prompt", a.prompt)):
        if path and not path.expanduser().exists():
            print(f"{flag} 파일이 없습니다: {path}")
            return 2
    if a.glossary:
        from . import terms as _t
        why = _t.check_csv(a.glossary.expanduser())
        if why:
            print(f"용어집을 쓸 수 없습니다 — {why}")
            print("  예:  source,target\n       policy,정책")
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
    # --glossary 를 준 사용자가 용어집이 적용된 줄 알고 배포하면 안 된다.
    if src.suffix.lower() in (".pptx", ".ppt"):
        ignored = [n for n, v in (("--pages", a.pages), ("--chunk", a.chunk != 40),
                                  ("--glossary", a.glossary), ("--prompt", a.prompt),
                                  ("--recheck", a.recheck), ("--fresh", a.fresh),
                                  ("--make-glossary", a.make_glossary),
                                  ("--no-glossary", a.no_glossary),
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

    # 용어집 만들기는 번역 없이 끝난다. 추론 서버도 필요 없다.
    if a.make_glossary:
        from . import terms
        step("용어 후보 추출")
        rows = terms.extract(src, first, last)
        if not rows:
            print("  반복되는 전문 용어를 찾지 못했습니다 "
                  "(문서가 짧거나 텍스트 레이어가 없을 수 있습니다)")
            return 1
        out_csv = a.make_glossary.expanduser().resolve()
        # 손으로 채운 파일을 말없이 덮어쓰면 안 된다. 문서에 적힌 흐름이
        # "뽑아서 → 손으로 채우고 → 다시 넘긴다"이므로, 덮어쓸 가능성이
        # 가장 큰 파일이 바로 사용자가 한 시간 들여 채운 그 파일이다.
        if out_csv.exists():
            print(f"이미 있는 파일입니다: {out_csv}")
            print("  덮어쓰지 않았습니다. 다른 이름을 주거나 파일을 옮기세요.")
            return 2
        try:
            terms.write_csv(out_csv, rows)
        except OSError as e:
            print(f"용어집을 저장할 수 없습니다: {out_csv}  ({e.strerror or e})")
            return 2
        info(f"{len(rows)}개 후보 → {out_csv}")
        info("번역어 칸을 채운 뒤 --glossary 로 넘기세요. 필요 없는 줄은 지우면 됩니다.")
        print()
        for n, t in rows[:10]:
            print(f"    {t}  ({n}회)")
        if len(rows) > 10:
            print(f"    … 외 {len(rows) - 10}개")
        return 0

    work = (a.work or Path.cwd() / f"{src.stem}_ko").expanduser().resolve()
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
    _, damaged, has_text = preflight(src, first, last)
    if not has_text:
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
            # 용어집을 바꾼 사용자가 기대하는 탈출구가 바로 이 옵션이다.
            # WAL/SHM 까지 지워야 한다 — 남겨 두면 지운 행이 되살아난다.
            for suffix in ("", "-wal", "-shm"):
                (work / "cache" / f"trans.db{suffix}").unlink(missing_ok=True)
            (work / "용어집.csv").unlink(missing_ok=True)   # 용어집도 새로
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

        # 용어 후보는 서버 기동 전에 뽑아 둔다(추론이 필요 없는 단계).
        auto_terms = []
        if not a.glossary and not a.no_glossary:
            from . import terms as _terms
            auto_terms = _terms.extract(src, first, last)

        step("서버 기동")
        srv = runner.Server(work, a.model)
        srv.glyphmap = srv_glyphmap
        # 용어집·프롬프트가 바뀌면 캐시가 무효화되어야 한다. 요청 본문에서는
        # 뽑을 수 없다 — BabelDOC 은 그것들을 user 메시지 안에 말아 넣는다.
        srv.start_ollama()
        info(f"추론 서버 :{srv.op}")
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
        # 용어 통일은 **프록시를 띄우기 전에** 끝낸다. 용어집 지문은 프록시
        # 기동 시점에 자식에게 넘어가므로, 그 뒤에 용어집을 만들면 캐시 키에
        # 반영되지 않는다. 그러면 역어가 달라져도 옛 번역이 그대로 나온다.
        glossary = a.glossary
        # 작업 폴더에 이미 용어집이 있으면 **그대로 쓴다.** README 가 "마음에
        # 안 드는 역어가 있으면 그 파일을 고쳐서 다시 넘기면 됩니다" 라고
        # 안내하는데, 정작 다음 실행이 그 파일을 말없이 덮어썼다. 사용자가
        # 손본 것을 도구가 지우면 안 된다.
        kept_glossary = work / "용어집.csv"
        if not glossary and kept_glossary.exists():
            glossary = kept_glossary
            auto_terms = []
            info(f"{kept_glossary.name} 를 그대로 씁니다 "
                 f"(새로 만들려면 이 파일을 지우거나 --fresh)")
        if auto_terms:
            step("용어 통일")
            from . import terms as _terms
            # 무엇이 이 분야의 용어인지는 모델이 고른다. 낱말 목록으로 거르면
            # 그 순간 분야 전용 도구가 된다. 둘 다 추론 서버에 직접 묻는다.
            auto_terms = _terms.keep_terms(auto_terms, port=srv.op, model=a.model)
            picked = _terms.decide(auto_terms, port=srv.op, model=a.model,
                                   via_proxy=False)
            if picked:
                gpath = work / "용어집.csv"
                _terms.write_csv(gpath, auto_terms, picked)
                glossary = gpath
                info(f"{len(picked)}개 용어의 역어를 고정했습니다 → {gpath.name}")
                for en, ko in list(picked.items())[:5]:
                    info(f"    {en} → {ko}")
                if len(picked) > 5:
                    info(f"    … 외 {len(picked) - 5}개")
            else:
                warn("용어 역어를 정하지 못했습니다 — 용어집 없이 진행합니다")

        srv.user_sig = runner.Server.signature(glossary, a.prompt)
        srv.start_proxy(sys.executable)
        info(f"미들웨어 :{srv.pp}")
        pl = srv.proxy_log_dir()
        if pl and pl.resolve() != (work / "logs").resolve():
            info(f"  앞선 실행의 프록시를 재사용합니다 — 미들웨어 로그는 {pl}")

        step("번역")
        for i, c in enumerate(chunks, 1):
            if c.done:
                info(f"[{i}/{len(chunks)}] {c.name} 건너뜀 (완료됨)")
                continue
            info(f"[{i}/{len(chunks)}] {c.name} …")
            ok = runner.translate_chunk(
                c, src, work, model=a.model, proxy_port=srv.pp,
                glossary=glossary, prompt_file=a.prompt)
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
    severe = [v for v in broken
              if v.overlap > 0.15 or v.outside > 0.10 or v.collision > 0.10]
    info(f"파손 {len(broken)}쪽 (심각 {len(severe)}쪽)")

    recs = []
    if severe and not a.no_recover:
        step("자동 복구")
        # 복구가 터져도 번역본은 이미 out 에 있다. 여기서 예외를 놓치면
        # 몇 시간 번역한 결과를 마지막 한 걸음에서 잃는다.
        try:
            if a.recheck:
                # --recheck 는 서버를 띄우지 않으므로 재번역을 못 한다.
                recs = recover.revert_pages(out, src, [v.page for v in severe],
                                            offset, out)
            else:
                recs = recover.repair_pages(
                    out, src, severe, offset, src, work,
                    model=a.model, proxy_port=srv.pp, glossary=glossary,
                    prompt_file=a.prompt,
                    on_step=lambda p, what: info(
                        f"  {p}쪽 {what}" if p else f"  {what}"))
            again = sum(1 for r in recs if r.action == "retranslated")
            back = sum(1 for r in recs if r.action == "reverted")
            info(f"재번역으로 살린 {again}쪽, 원문 유지 {back}쪽"
                 + (" (되돌린 쪽 하단에 표시가 남습니다)" if back else ""))
        except Exception as e:
            warn(f"자동 복구 실패({type(e).__name__}: {e}) — "
                 f"번역본 {out.name} 은 그대로 쓸 수 있습니다")
            # 보고서에도 남긴다. 여기서 버리면 모든 파손 페이지가
            # "복구가 실행되지 않음" 으로만 찍혀 이유를 알 수 없다.
            recs = [recover.Recovery(page=v.page, orig_page=v.page + offset,
                                     reasons=v.reasons, action="",
                                     note=f"복구 중단: {type(e).__name__}: {e}")
                    for v in severe]

    rep = work / "품질보고서.md"
    recover.write_report(rep, verdicts, recs, offset)

    runner.stop_all()
    # 엔진이 남긴 중간 산출물은 쪽수에 비례해 쌓인다(3쪽에 13MB).
    # 결과 PDF 를 만든 뒤에는 쓸모가 없다.
    runner.cleanup_work(work)
    step("완료")
    info(f"결과   {out}")
    info(f"보고서 {rep}")
    if broken:
        # 되돌린 페이지를 '복구'로 세면 안 된다. 전부 영어로 남겨 놓고
        # "12쪽 중 12쪽 복구"라고 말하게 된다.
        fixed_n = sum(1 for r in recs if r.action == "retranslated")
        back_n = sum(1 for r in recs if r.action == "reverted")
        info(f"파손 {len(broken)}쪽 — 재번역으로 살림 {fixed_n}쪽, "
             f"원문 유지 {back_n}쪽, 그대로 둠 {len(broken) - fixed_n - back_n}쪽 "
             f"(보고서 참고)")
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

    work = (a.work or Path.cwd() / f"{src.stem}_ko").expanduser().resolve()
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
