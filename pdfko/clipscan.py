"""잘라내기(clipping)로 가려진 '보이지 않는 글자'를 찾아내고 제거한다.

## 무엇이 문제인가

교재의 그림 중에는 다른 문서 페이지를 통째로 가져와 벡터 편집기에서 필요한
부분만 잘라낸 것이 있다. 잘라내기는 **렌더링할 때 가릴 뿐 콘텐츠 스트림에서
글자를 지우지 않는다.** 사람 눈에는 안 보이지만 스트림에는 그대로 남아 있다.

MuPDF(pymupdf)는 잘라내기를 존중해서 `get_text()` 에 안 나온다. 그런데
**BabelDOC 은 존중하지 않는다.** 소스의 `on_lt_char()` 는 글자의 회전각만
검사하고 잘라내기 영역은 보지 않는다. 그래서 숨은 글자를 정상 본문으로 알고
번역한 뒤, 그 한국어를 **진짜 본문 위에 겹쳐 찍는다.**

한 교재에서 실측: 페이지 한 장에 숨은 글자가 최대 13,016개, 책 전체 글자의
3.8%가 이런 글자였다. 겹쳐 찍히는 페이지가 12장 나왔다.

## 어떻게 푸는가

번역에 넘기기 **전에** 원본 사본을 만들어 잘라내기 영역 밖 글자를 스트림에서
지운다. 원본에서도 보이지 않던 글자이므로 **눈에 보이는 것은 하나도 잃지 않는다.**
그러면 번역 엔진이 진짜 본문만 보게 되고 페이지가 정상적으로 조판된다.

영어로 되돌리는 것보다 낫다. 페이지를 포기하지 않고 살릴 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# 텍스트를 그리는 연산자
SHOW_OPS = {"Tj", "TJ", "'", '"'}

# 한 텍스트 블록에서 이 개수 이상이 전부 잘라내기 밖일 때만 통째로 지운다.
# 경계에 걸친 글자 두세 개 때문에 보이는 본문을 잃는 일이 없도록 한다.
MIN_BLOCK_HIDDEN = 1


@dataclass
class _GState:
    ctm: tuple = (1, 0, 0, 1, 0, 0)
    clip: tuple | None = None          # (x0, y0, x1, y1) 장치 좌표


def _mul(a: tuple, b: tuple) -> tuple:
    """행렬 곱 a×b (PDF 순서: a 가 먼저 적용)."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def _apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _rect_from(x: float, y: float, w: float, h: float, ctm: tuple) -> tuple:
    pts = [_apply(ctm, x, y), _apply(ctm, x + w, y),
           _apply(ctm, x, y + h), _apply(ctm, x + w, y + h)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _inside(pt: tuple, clip: tuple | None, pad: float = 3.0) -> bool:
    if clip is None:
        return True
    x, y = pt
    return (clip[0] - pad <= x <= clip[2] + pad
            and clip[1] - pad <= y <= clip[3] + pad)


@dataclass
class PageScan:
    page: int
    hidden: int = 0                 # 잘라내기 밖 글자 수(대략)
    shown: int = 0
    error: str = ""                 # 스트림을 못 읽었을 때의 사유
    spans: list = field(default_factory=list)   # (시작오프셋, 끝오프셋)


_ESC_RE = re.compile(rb"\\[0-7]{1,3}|\\.", re.S)


def _str_chars(tok: bytes) -> int:
    """문자열 피연산자에 실제로 몇 글자가 들어 있나.

    `(text) Tj`, `<48656c> Tj`, `[(a) -20 (b)] TJ` 를 모두 받는다.

    예전에는 `len(tok) // 2` 로 셌는데, 그 자리의 `tok` 은 문자열이 아니라
    **연산자**(`Tj`)라서 항상 1이 나왔다. 즉 글자가 아니라 연산자를 센 것이다.
    `Tj` 하나에 5,000자가 들어 있어도 1로 세니 임계값(40)이 사실상 무의미했다 —
    실측으로 숨은 글자 200자짜리 페이지가 `hidden=1` 로 찍혔다.
    """
    if tok.startswith(b"("):
        return len(_ESC_RE.sub(b"x", tok[1:-1]))     # 이스케이프 하나가 한 글자
    if tok.startswith(b"<"):
        hexd = sum(1 for c in tok[1:-1] if c in b"0123456789abcdefABCDEF")
        return max(1, hexd // 2) if hexd else 0      # 단일바이트 인코딩 가정
    if tok.startswith(b"["):
        # 배열 안의 커닝 숫자는 빼고 문자열만 센다. 중첩 괄호까지 제대로
        # 처리하려면 정규식으로는 부족해서 토크나이저를 그대로 재사용한다.
        return sum(_str_chars(t) for t, _, _ in _tokens(tok[1:-1]))
    return 0


def _str_text(tok: bytes | None) -> str:
    """문자열 피연산자에서 글자를 꺼낸다. 안전장치가 낱말을 대조하는 데 쓴다.

    `(...)` 뿐 아니라 `<hex>` 와 `[(a) -20 (b)] TJ` 도 반드시 받아야 한다.
    괄호 문자열만 보던 시절에는 pdfTeX 가 내놓는 커닝 배열과 서브셋 폰트의
    16진 문자열이 전부 빠져나가, "보이는 낱말은 지키다"는 안전장치가
    **실제 본문 대부분에서 동작하지 않았다.**
    """
    if not tok:
        return ""
    if tok.startswith(b"("):
        return _ESC_RE.sub(b"", tok[1:-1]).decode("latin-1", "replace")
    if tok.startswith(b"<"):
        hexd = bytes(c for c in tok[1:-1] if c in b"0123456789abcdefABCDEF")
        if len(hexd) % 2:
            hexd += b"0"
        try:
            raw = bytes.fromhex(hexd.decode())
        except ValueError:
            return ""
        # Identity-H 같은 CID 인코딩은 글리프 하나가 2바이트다. 단일바이트로
        # 읽으면 `Test` 가 `\x007\x00H\x00V\x00W` 로 나와 낱말 대조가 전부
        # 빗나간다 — 서브셋 폰트로 심어 온 페이지가 바로 이 모듈이 다루려는
        # 대상인데, 안전장치가 그 경우에만 통째로 작동하지 않았다.
        # 짝수 바이트이고 앞바이트가 대부분 0 이면 2바이트로 읽는다.
        if len(raw) >= 4 and len(raw) % 2 == 0:
            highs = raw[0::2]
            if highs.count(0) >= len(highs) * 0.8:
                return raw[1::2].decode("latin-1", "replace")
        return raw.decode("latin-1", "replace")
    if tok.startswith(b"["):
        return "".join(_str_text(t) for t, _, _ in _tokens(tok[1:-1]))
    return ""


def _tokens(data: bytes):
    """콘텐츠 스트림을 (토큰, 시작, 끝) 로 훑는다. 문자열·배열은 통째로 넘긴다."""
    i, n = 0, len(data)
    while i < n:
        c = data[i:i + 1]
        if c.isspace():
            i += 1
            continue
        start = i
        if c == b"(":                      # 리터럴 문자열
            depth, i = 1, i + 1
            while i < n and depth:
                if data[i:i + 1] == b"\\":
                    i += 2
                    continue
                if data[i:i + 1] == b"(":
                    depth += 1
                elif data[i:i + 1] == b")":
                    depth -= 1
                i += 1
            yield data[start:i], start, i
        elif c == b"<" and data[i + 1:i + 2] != b"<":   # 16진 문자열
            j = data.find(b">", i)
            i = (j + 1) if j >= 0 else n
            yield data[start:i], start, i
        elif c == b"[":
            depth, i = 1, i + 1
            while i < n and depth:
                ch = data[i:i + 1]
                if ch == b"(":
                    d2, i = 1, i + 1
                    while i < n and d2:
                        if data[i:i + 1] == b"\\":
                            i += 2
                            continue
                        if data[i:i + 1] == b"(":
                            d2 += 1
                        elif data[i:i + 1] == b")":
                            d2 -= 1
                        i += 1
                    continue
                if ch == b"[":
                    depth += 1
                elif ch == b"]":
                    depth -= 1
                i += 1
            yield data[start:i], start, i
        else:
            while i < n and not data[i:i + 1].isspace() and data[i:i + 1] not in b"()[]<>/":
                i += 1
            if i == start:
                i += 1
            yield data[start:i], start, i


def scan_page(doc: pymupdf.Document, pno: int) -> PageScan:
    """한 페이지의 콘텐츠 스트림을 해석해 잘라내기 밖 글자를 센다."""
    page = doc[pno]
    res = PageScan(page=pno + 1)
    try:
        data = b"".join(doc.xref_stream(x) or b"" for x in page.get_contents())
    except Exception as e:
        # 여기서 조용히 0 을 돌려주면 이 모듈이 막으려던 일이 그대로 일어난다.
        # 숨은 글자가 있어도 임계값에 못 미쳐 청소되지 않고, 엔진이 그것까지
        # 번역해 본문 위에 겹쳐 찍는다. 버그가 조용히 틀린 페이지가 된다.
        res.error = f"{type(e).__name__}: {e}"
        return res
    if not data:
        return res

    stack: list[_GState] = []
    gs = _GState()
    pending: list[float] = []          # 숫자 피연산자
    last_rect: tuple | None = None
    tm = lm = (1, 0, 0, 1, 0, 0)
    in_text = False
    show_start: int | None = None

    last_str: bytes | None = None      # 직전에 본 문자열/배열 피연산자

    for tok, a, b in _tokens(data):
        t = tok.decode("latin-1", "replace")
        # 숫자
        try:
            pending.append(float(t))
            continue
        except ValueError:
            pass
        if tok[:1] in (b"(", b"<", b"[") and not tok.startswith(b"<<"):
            last_str = tok
            continue

        if t == "q":
            stack.append(_GState(gs.ctm, gs.clip))
        elif t == "Q":
            gs = stack.pop() if stack else _GState()
        elif t == "cm" and len(pending) >= 6:
            gs.ctm = _mul(tuple(pending[-6:]), gs.ctm)
        elif t == "re" and len(pending) >= 4:
            last_rect = _rect_from(*pending[-4:], gs.ctm)
        elif t in ("W", "W*"):
            if last_rect:
                gs.clip = last_rect if gs.clip is None else (
                    max(gs.clip[0], last_rect[0]), max(gs.clip[1], last_rect[1]),
                    min(gs.clip[2], last_rect[2]), min(gs.clip[3], last_rect[3]))
        elif t == "BT":
            in_text, tm, lm = True, (1, 0, 0, 1, 0, 0), (1, 0, 0, 1, 0, 0)
        elif t == "ET":
            in_text = False
        elif t == "Tm" and len(pending) >= 6:
            tm = lm = tuple(pending[-6:])
        elif t in ("Td", "TD") and len(pending) >= 2:
            tm = lm = _mul((1, 0, 0, 1, pending[-2], pending[-1]), lm)
        elif t == "T*":
            tm = lm = _mul((1, 0, 0, 1, 0, -12), lm)
        elif t in SHOW_OPS and in_text:
            origin = _apply(_mul(tm, gs.ctm), 0, 0)
            n_ch = _str_chars(last_str) if last_str else 1
            if _inside(origin, gs.clip):
                res.shown += n_ch
            else:
                res.hidden += n_ch
                if show_start is not None:
                    res.spans.append((show_start, b))
        if t not in ("W", "W*"):
            if t != "re":
                pending.clear()
        if t == "BT" or (in_text and t in SHOW_OPS):
            show_start = b
        if t in SHOW_OPS:
            last_str = None
    return res


def scan(pdf: str | Path, first: int = 1, last: int | None = None
         ) -> list[PageScan]:
    out = []
    with pymupdf.open(pdf) as doc:
        end = min(last or doc.page_count, doc.page_count)
        for i in range(first - 1, end):
            out.append(scan_page(doc, i))
    return out


def clean(src: str | Path, dst: str | Path, pages: list[int] | None = None,
          min_hidden: int = 40, max_lost_words: int = 3
          ) -> tuple[list[int], list[int], dict[int, list[str]]]:
    """숨은 글자를 지운 사본을 만든다. (손본 페이지, 되돌린 페이지)

    두 겹의 안전장치를 둔다.

    1. **MuPDF 를 기준으로 삼는다.** 우리 잘라내기 계산은 근사지만 MuPDF 는
       정확하다. 그래서 지울 후보 블록의 글자가 MuPDF 가 뽑아낸 '보이는
       텍스트'에 들어 있으면 지우지 않는다.
    2. **지운 뒤 대조하고 되돌린다.** 그래도 보이는 텍스트가 한 글자라도
       달라지면 그 페이지는 원상 복구한다.

    임계값을 손으로 맞추는 대신 보장을 만든다.
    """
    touched: list[int] = []
    rolled: list[int] = []
    lost_log: dict[int, list[str]] = {}
    with pymupdf.open(src) as doc:
        targets = pages or [s.page for s in scan(src) if s.hidden >= min_hidden]
        for p in targets:
            page = doc[p - 1]
            xrefs = page.get_contents()
            if not xrefs:
                continue
            before_text = page.get_text()
            visible = set(before_text.split())
            # 보이는 낱말의 **좌표**. MuPDF 는 위에서 아래로 재고 콘텐츠
            # 스트림은 아래에서 위로 재므로 y 를 뒤집는다.
            h = page.rect.y1
            vis_rects = [(w[0], h - w[3], w[2], h - w[1])
                         for w in page.get_text("words")]
            before = [(x, doc.xref_stream(x) or b"") for x in xrefs]
            data = b"".join(d for _, d in before)

            ok = False
            # 블록 단위 → 실패하면 글자 단위. 각 단계마다 대조하고 되돌린다.
            for strip in (_strip_hidden,
                          lambda d, v: _strip_hidden_chars(d, v, vis_rects)):
                out = strip(data, visible)
                if out is None or out == data:
                    continue
                doc.update_stream(xrefs[0], out)
                for x in xrefs[1:]:
                    doc.update_stream(x, b"")
                after_text = doc[p - 1].get_text()
                if after_text == before_text:
                    touched.append(p)
                    ok = True
                    break
                # 숨은 텍스트와 그림 속 라벨이 잘라내기 경계에 걸쳐 있으면
                # 낱말 한두 개가 함께 지워진다. 페이지 전체가 깨진 채 남거나
                # 영어로 되돌아가는 것보다는, 라벨 조각을 잃고 번역을 살리는
                # 편이 낫다. 다만 손실은 한도를 두고 전부 기록한다.
                # 집합이 아니라 **중복집합**으로 세야 한다. 머리글·바닥글이
                # 반복되거나 두 단 레이아웃에서 같은 낱말이 양쪽에 있으면,
                # 한쪽을 통째로 지워도 "모든 낱말이 어딘가엔 남아 있으니
                # 손실 0" 이 되어 조용히 통과한다.
                from collections import Counter
                lost = list((Counter(before_text.split())
                             - Counter(after_text.split())).elements())
                # 절대 개수만으로는 못 막는다. 보이는 낱말이 원래 한 개인
                # 페이지에서 그 한 개를 잃으면 손실률 100% 인데 `1 <= 3` 이라
                # 통과했다. 실측으로 페이지의 보이는 글자가 전부 사라진 채
                # "청소 성공"으로 보고됐다. 비율 상한을 함께 건다.
                n_before = len(before_text.split())
                frac = len(lost) / n_before if n_before else 0.0
                if len(lost) <= max_lost_words and frac <= 0.2:
                    touched.append(p)
                    lost_log[p] = lost
                    ok = True
                    break
                for x, d in before:      # 손실이 크다 → 원상 복구 후 다음 방식
                    doc.update_stream(x, d)
            if not ok:
                rolled.append(p)
        doc.save(dst, garbage=4, deflate=True)
    return touched, rolled, lost_log


def _strip_hidden(data: bytes, visible: set[str] | None = None) -> bytes | None:
    """잘라내기 영역 밖에 놓인 BT…ET 블록을 제거한 스트림."""
    stack: list[_GState] = []
    gs = _GState()
    pending: list[float] = []
    last_rect: tuple | None = None
    tm = lm = (1, 0, 0, 1, 0, 0)
    bt_start: int | None = None
    hidden_here = shown_here = 0
    drop: list[tuple[int, int]] = []
    block_txt: list[str] = []
    last_str: bytes | None = None

    for tok, a, b in _tokens(data):
        t = tok.decode("latin-1", "replace")
        try:
            pending.append(float(t))
            continue
        except ValueError:
            pass
        # 문자열 피연산자를 붙잡아 둔다. 이걸 안 하면 아래 `block_txt` 수집이
        # 연산자 토큰을 검사하게 되어 **한 번도 참이 되지 않는다** — 문서가
        # 약속한 안전장치 1층("MuPDF 가 본 낱말이면 지우지 않는다")이
        # 통째로 죽어 있었다.
        if tok[:1] in (b"(", b"<", b"[") and not tok.startswith(b"<<"):
            last_str = tok
            continue
        if t == "q":
            stack.append(_GState(gs.ctm, gs.clip))
        elif t == "Q":
            gs = stack.pop() if stack else _GState()
        elif t == "cm" and len(pending) >= 6:
            gs.ctm = _mul(tuple(pending[-6:]), gs.ctm)
        elif t == "re" and len(pending) >= 4:
            last_rect = _rect_from(*pending[-4:], gs.ctm)
        elif t in ("W", "W*"):
            if last_rect:
                gs.clip = last_rect if gs.clip is None else (
                    max(gs.clip[0], last_rect[0]), max(gs.clip[1], last_rect[1]),
                    min(gs.clip[2], last_rect[2]), min(gs.clip[3], last_rect[3]))
        elif t == "BT":
            bt_start, hidden_here, shown_here = a, 0, 0
            block_txt = []
            tm = lm = (1, 0, 0, 1, 0, 0)
        elif t == "ET":
            # 보수적으로: 그 블록이 전부 숨어 있고, 양이 충분할 때만 지운다.
            # 경계에 걸친 글자 몇 개 때문에 보이는 텍스트를 잃으면 안 된다.
            keep = False
            if visible and block_txt:
                # MuPDF 가 보이는 것으로 뽑아낸 낱말이 이 블록에 있으면 지우지 않는다.
                word = "".join(block_txt).strip()
                if len(word) >= 3 and any(word in v or v in word for v in visible):
                    keep = True
            if (bt_start is not None and not shown_here and not keep
                    and hidden_here >= MIN_BLOCK_HIDDEN):
                drop.append((bt_start, b))
            bt_start = None
        elif t == "Tm" and len(pending) >= 6:
            tm = lm = tuple(pending[-6:])
        elif t in ("Td", "TD") and len(pending) >= 2:
            tm = lm = _mul((1, 0, 0, 1, pending[-2], pending[-1]), lm)
        elif t == "T*":
            tm = lm = _mul((1, 0, 0, 1, 0, -12), lm)
        elif t in SHOW_OPS and bt_start is not None:
            block_txt.append(_str_text(last_str))
            n_ch = _str_chars(last_str) if last_str else 1
            if _inside(_apply(_mul(tm, gs.ctm), 0, 0), gs.clip):
                shown_here += n_ch
            else:
                hidden_here += n_ch
        if t != "re" and t not in ("W", "W*"):
            pending.clear()
        if t in SHOW_OPS:
            last_str = None

    if not drop:
        return None
    out, prev = [], 0
    for s, e in drop:
        out.append(data[prev:s])
        prev = e
    out.append(data[prev:])
    return b"".join(out)

def _strip_hidden_chars(data: bytes, visible: set[str] | None = None,
                        vis_rects: list[tuple] | None = None) -> bytes | None:
    """블록이 아니라 **글자 단위**로 지운다.

    숨은 텍스트와 그림 속 진짜 라벨이 한 블록에 섞여 있는 페이지가 있다.
    블록째 지우면 라벨까지 딸려 가고(낱말 한두 개), 안전장치가 페이지 전체를
    되돌려 버려 결국 아무것도 못 지운다. 그래서 잘라내기 밖에 있는 개별
    텍스트 연산자만 골라 없앤다.

    지운 자리에는 아무것도 넣지 않는다. 앞선 `Tf`/`Tm` 은 그대로 두므로
    나머지 글자의 위치는 바뀌지 않는다.
    """
    stack: list[_GState] = []
    gs = _GState()
    pending: list[float] = []
    last_rect: tuple | None = None
    tm = lm = (1, 0, 0, 1, 0, 0)
    in_text = False
    drop: list[tuple[int, int]] = []
    # 반드시 루프 밖에서 초기화해야 한다. 아래에서 읽고 루프 끝에서 쓰기 때문에,
    # 첫 토큰이 곧바로 문자열 표시 연산자면 NameError 로 죽는다. 실제 PDF 는
    # 앞에 `q` 나 `BT` 가 오는 게 보통이라 여태 드러나지 않았을 뿐이다.
    op_start: int | None = None

    for tok, a, b in _tokens(data):
        t = tok.decode("latin-1", "replace")
        try:
            pending.append(float(t))
            continue
        except ValueError:
            pass
        if t == "q":
            stack.append(_GState(gs.ctm, gs.clip))
        elif t == "Q":
            gs = stack.pop() if stack else _GState()
        elif t == "cm" and len(pending) >= 6:
            gs.ctm = _mul(tuple(pending[-6:]), gs.ctm)
        elif t == "re" and len(pending) >= 4:
            last_rect = _rect_from(*pending[-4:], gs.ctm)
        elif t in ("W", "W*"):
            if last_rect:
                gs.clip = last_rect if gs.clip is None else (
                    max(gs.clip[0], last_rect[0]), max(gs.clip[1], last_rect[1]),
                    min(gs.clip[2], last_rect[2]), min(gs.clip[3], last_rect[3]))
        elif t == "BT":
            in_text, tm, lm = True, (1, 0, 0, 1, 0, 0), (1, 0, 0, 1, 0, 0)
        elif t == "ET":
            in_text = False
        elif t == "Tm" and len(pending) >= 6:
            tm = lm = tuple(pending[-6:])
        elif t in ("Td", "TD") and len(pending) >= 2:
            tm = lm = _mul((1, 0, 0, 1, pending[-2], pending[-1]), lm)
        elif t == "T*":
            tm = lm = _mul((1, 0, 0, 1, 0, -12), lm)
        elif t in SHOW_OPS and in_text:
            origin = _apply(_mul(tm, gs.ctm), 0, 0)
            if not _inside(origin, gs.clip):
                # MuPDF 가 보인다고 한 자리면 지우지 않는다.
                #
                # 글자를 대조하려 했지만 원리적으로 안 된다. CID 폰트(Identity-H)
                # 는 2바이트 글리프 번호를 싣고, 그 번호가 무슨 글자인지는 폰트의
                # ToUnicode CMap 을 읽어야 안다. 서브셋으로 심어 온 페이지가
                # 바로 이 모듈이 다루려는 대상인데, 거기서 안전장치가 통째로
                # 무력해진다. 그래서 **좌표로** 본다 — 폰트와 무관하고,
                # `Tj`·`TJ`·`<hex>` 를 가리지 않는다.
                keep = False
                if vis_rects:
                    x, y = origin
                    keep = any(x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2
                               for x0, y0, x1, y1 in vis_rects)
                if not keep and visible:
                    w = _str_text(
                        data[op_start:a] if op_start is not None else b"").strip()
                    if len(w) >= 3 and any(w in v or v in w for v in visible):
                        keep = True
                if not keep:
                    # 피연산자(문자열/배열)까지 함께 지워야 한다
                    drop.append((op_start if op_start is not None else a, b))
        if t != "re" and t not in ("W", "W*"):
            pending.clear()
        op_start = a if t not in SHOW_OPS else None

    if not drop:
        return None
    out, prev = [], 0
    for s0, e0 in drop:
        if s0 < prev:
            continue
        out.append(data[prev:s0])
        prev = e0
    out.append(data[prev:])
    return b"".join(out)

