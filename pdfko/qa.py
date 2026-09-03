"""렌더링된 페이지의 레이아웃 파손을 탐지한다.

## 왜 필요한가

번역 엔진은 문단 단위로 텍스트를 갈아끼운다. 한국어는 영어보다 길어지는 일이
잦은데, 그림·수식이 빽빽한 페이지에서는 들어갈 자리가 없어 글자가 겹치거나
여백 밖으로 밀려난다. 그런데 **텍스트 추출로는 이게 보이지 않는다** —
한글 비율도 정상이고 미번역도 아니다. 지표만 보면 멀쩡한데 실물은 못 읽는다.

## 왜 잉크 농도로는 부족한가

처음엔 렌더링한 픽셀의 검은 비율을 원본과 비교했다. 글자가 겹치면 진해지니까.
실제로 심하게 겹친 페이지는 3.4배로 잡혔다. 그러나 실측해 보니:

    겹쳐서 못 읽는 페이지    ×3.38  ✓ 잡힘
    여백으로 밀려난 페이지    ×1.41  △ 임계값 문제
    글자가 뒤섞인 페이지      ×0.64  ✗ 못 잡음 (오히려 옅다)
    그림 라벨만 섞인 페이지    ×0.69  ✗ 못 잡음

한국어로 바뀌면 글자 수가 줄어 잉크가 **옅어지는** 게 정상이다(전체 중앙값
×0.84). 그래서 파손됐는데도 옅게 나오는 경우를 원리적으로 구분하지 못한다.

## 이 모듈이 보는 것

단어의 실제 좌표를 원본과 비교한다. 세 가지를 독립적으로 센다.

  1. 겹침    — 다른 단어와 상자가 포개진 단어의 비율
  2. 이탈    — 원본 본문 영역 밖으로 나간 단어의 비율
  3. 줄 충돌 — 같은 높이에 서로 다른 줄이 끼어든 정도

어느 하나라도 기준을 넘으면 그 페이지는 '파손'으로 판정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf


@dataclass
class PageVerdict:
    page: int
    overlap: float = 0.0      # 겹친 단어 비율
    outside: float = 0.0      # 본문 영역 밖 단어 비율
    collision: float = 0.0    # 줄 충돌 비율
    words: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def broken(self) -> bool:
        return bool(self.reasons)


# 기준값. 정상 페이지 표본에서 셋 다 0에 가깝게 나오도록 잡았다.
OVERLAP_MAX = 0.02      # 단어 2% 이상이 겹치면 파손
OUTSIDE_MAX = 0.03      # 3% 이상이 본문 영역 밖이면 파손
COLLISION_MAX = 0.04    # 줄 충돌 4% 이상이면 파손
MIN_WORDS = 15          # 이보다 적으면 판정하지 않는다 (그림·빈 페이지)
# 비율만 보면 **성긴 쪽이 과민해진다.** 이 지표는 낱말이 수백 개인 교재
# 쪽을 상정하고 만들었다. 거기서 3% 는 의미가 있지만 24낱말짜리 슬라이드에서는
# 한 낱말이 4% 다. 실측(L03 발표자료):
#     21쪽  24낱말 중 2개가 밖 → 8%  → '파손' → 19초짜리 재번역
#      5쪽  43낱말 중 3개가 밖 → 7%  → '파손'
# 기준 상자가 원본 낱말들의 바운딩 박스라 여유가 0인 것도 겹친다 — 원본
# 이탈률은 정의상 0.0% 이고, 번역 후 한 낱말이 1pt 만 나가도 세어진다.
OUTSIDE_MIN_WORDS = 5   # 비율과 함께 **개수**도 넘어야 파손으로 본다


def _words(page: pymupdf.Page) -> list[tuple[float, float, float, float, str]]:
    # 되돌린 페이지 하단에 남기는 `[pdfko]` 문구는 본문 상자 밖이라
    # '영역이탈' 로 잡힌다. 낱말이 적은 페이지에서는 그 9낱말이 31%를 차지해
    # 다시 심각 판정을 받고, `--recheck` 를 돌릴 때마다 이미 원문인 페이지를
    # 또 되돌린다. 도구가 제 표시를 파손으로 읽는 셈이다.
    out, skip = [], False
    for w in page.get_text("words"):
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if txt.startswith("[pdfko]"):
            skip = True                       # 이 줄부터 표시 문구다
        if skip and y0 > page.rect.y1 - 40:
            continue
        if txt.strip() and (x1 - x0) > 0 and (y1 - y0) > 0:
            out.append((x0, y0, x1, y1, txt))
    return out


def _text_block(words) -> tuple[float, float, float, float]:
    """단어들이 차지하는 전체 영역. 원본의 '본문이 있어야 할 자리'."""
    if not words:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(w[0] for w in words), min(w[1] for w in words),
            max(w[2] for w in words), max(w[3] for w in words))


def _overlap_ratio(words) -> float:
    """다른 단어와 크게 포개진 단어의 비율.

    y 로 정렬해 근처 것끼리만 비교한다. 전체 쌍 비교는 O(n²)라 느리다.
    """
    if len(words) < MIN_WORDS:
        return 0.0
    ws = sorted(words, key=lambda w: (w[1], w[0]))
    hit = 0
    for i, a in enumerate(ws):
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        if area_a <= 0:
            continue
        for b in ws[i + 1:i + 60]:
            if b[1] > a[3]:      # 이미 아래로 벗어났으면 이후는 볼 필요 없다
                break
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox > 0.5 and oy > 0.5 and (ox * oy) / area_a > 0.30:
                hit += 1
                break
    return hit / len(ws)


def _outside_ratio(words, block, pad: float = 6.0) -> float:
    """원본 본문 영역 밖으로 나간 단어의 비율."""
    if len(words) < MIN_WORDS:
        return 0.0
    x0, y0, x1, y1 = block
    out = sum(1 for w in words
              if w[0] < x0 - pad or w[2] > x1 + pad
              or w[1] < y0 - pad or w[3] > y1 + pad)
    return out / len(words)


def _collision_ratio(words) -> float:
    """같은 높이 띠에 서로 다른 줄이 끼어든 정도.

    정상 조판이면 한 줄의 단어들은 x 가 겹치지 않는다. 문단이 덧그려지면
    같은 y 대역에 x 가 겹치는 단어가 여럿 생긴다.
    """
    if len(words) < MIN_WORDS:
        return 0.0
    rows: dict[int, list[tuple[float, float]]] = {}
    for x0, y0, x1, y1, _ in words:
        key = int((y0 + y1) / 2 / 4)      # 4pt 단위 띠
        rows.setdefault(key, []).append((x0, x1))
    bad = 0
    for spans in rows.values():
        spans.sort()
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1] - 0.5:
                bad += 1
    return bad / len(words)


def inspect_page(orig: pymupdf.Page, trans: pymupdf.Page, page_no: int) -> PageVerdict:
    """번역 페이지를 원본과 대조해 파손 여부를 판정한다.

    절대 기준으로 재면 안 된다. 수식이 빽빽한 페이지는 **원본에서도** 상자가
    겹치게 조판되어 있어서(위·아래 첨자, 큰 괄호) 겹침이 4%씩 나온다. 그걸
    파손으로 세면 멀쩡한 페이지를 버리게 된다. 그래서 같은 지표를 원본에서도
    재고 **늘어난 만큼만** 본다.
    """
    ow, tw = _words(orig), _words(trans)
    v = PageVerdict(page=page_no, words=len(tw))
    if len(tw) < MIN_WORDS:
        return v                      # 그림뿐이거나 빈 페이지 — 판정 보류

    block = _text_block(ow)
    # 원본의 기준선
    base_ov = _overlap_ratio(ow)
    base_co = _collision_ratio(ow)
    base_out = _outside_ratio(ow, block)

    v.overlap = max(0.0, _overlap_ratio(tw) - base_ov)
    v.outside = max(0.0, _outside_ratio(tw, block) - base_out)
    v.collision = max(0.0, _collision_ratio(tw) - base_co)

    if v.overlap > OVERLAP_MAX:
        v.reasons.append(f"겹침 +{v.overlap*100:.0f}%")
    if v.outside > OUTSIDE_MAX and v.outside * len(tw) >= OUTSIDE_MIN_WORDS:
        v.reasons.append(f"영역이탈 +{v.outside*100:.0f}%")
    if v.collision > COLLISION_MAX:
        v.reasons.append(f"줄충돌 +{v.collision*100:.0f}%")
    return v


# ---------------------------------------------------------------- 그림 라벨
def mixed_language_figures(trans: pymupdf.Page) -> int:
    """한 그림 안에서 한국어와 영어가 섞인 라벨 묶음의 수.

    번역 엔진이 그림 속 라벨 중 긴 것만 번역하고 짧은 것은 건너뛰면
    다이어그램 한복판에 `simulated 경험` 같은 반쪽짜리가 남는다. 겹침도
    이탈도 아니라 좌표 검사로는 안 잡히므로 따로 센다.

    같은 블록 안에 한글 줄과 라틴 줄이 함께 있으면 섞인 것으로 본다.
    """
    mixed = 0
    for block in trans.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        # 2~3줄·60자로 좁게 잡았더니 실제 범례를 놓쳤다 — 4줄 65자짜리
        # `['n = 100, 예상되는 Sarsa', 'n = 1E5, Sarsa', …]` 가 그대로
        # 통과하면서 보고서는 "파손 0쪽"이라고 찍혔다. 실측으로 490쪽에서
        # 21%를 놓치고 있었다(19쪽 → 24쪽).
        if not (2 <= len(lines) <= 8):
            continue                       # 한 줄짜리·긴 문단은 라벨이 아니다

        texts = ["".join(s.get("text", "") for s in ln.get("spans", []))
                 for ln in lines]
        if sum(len(t) for t in texts) > 160:
            continue                       # 라벨치고 너무 길면 본문이다

        # 산문은 한 줄 안에 한국어와 영어가 섞인다(용어 병기).
        # 라벨 파손은 **줄 단위로 언어가 갈린다**: 한 줄은 순 한글, 다른 줄은 순 영어.
        pure_ko = pure_en = 0
        for t in texts:
            ko = sum("가" <= c <= "힣" for c in t)
            en = sum(c.isalpha() and c.isascii() for c in t)
            if ko >= 2 and en == 0:
                pure_ko += 1
            elif en >= 3 and ko == 0 and any(c.islower() for c in t):
                # 소문자가 있어야 **번역되지 않고 남은 낱말**이다. 대문자만인
                # 것은 약어라 그대로 두는 것이 맞다 — `LLM`, `API`, `MDP`.
                # 실측(AI Agent 1주차 7쪽): `['LLM', '프롬프트']` 가 그림혼재로
                # 잡혔는데, LLM 은 번역할 것이 아니다.
                pure_en += 1
        if pure_ko and pure_en:
            mixed += 1
    return mixed


def coverage(trans_pdf: str, floor: float = 0.15) -> tuple[int, list[int]]:
    """번역이 실제로 일어났는가. (판정한 쪽수, 한글이 거의 없는 쪽 목록)

    이게 없으면 **가장 나쁜 실패가 조용히 성공으로 보고된다.** 모델 이름을
    잘못 적거나 추론 서버가 죽어 있으면 번역 엔진은 영어 페이지를 그대로
    내놓으면서 종료 코드 0을 돌려준다. 실측으로 한글 0자짜리 PDF 가
    "파손 0쪽 · 완료"로 나왔다. 500쪽이면 서너 시간을 버리고, 사용자가
    열어 보기 전까지 알 수 없다.

    글자가 적은 쪽(그림·수식뿐인 쪽)은 판정하지 않는다.
    """
    import pymupdf

    empty: list[int] = []
    judged = 0
    with pymupdf.open(trans_pdf) as d:
        for i in range(d.page_count):
            t = d[i].get_text()
            letters = [c for c in t if c.isalpha()]
            if len(letters) < 50:
                continue                 # 판정 보류 — 그림·수식뿐인 쪽
            judged += 1
            ko = sum(1 for c in letters if "가" <= c <= "힣")
            if ko / len(letters) < floor:
                empty.append(i + 1)
    return judged, empty


def scan(orig_pdf: str, trans_pdf: str, offset: int = 0,
         progress=None) -> list[PageVerdict]:
    """번역본 전체를 훑는다.

    offset: 번역본 1쪽에 대응하는 원본 쪽번호 - 1
            (본문만 번역해 원본 13쪽부터라면 offset=12)
    """
    out = []
    with pymupdf.open(orig_pdf) as o, pymupdf.open(trans_pdf) as t:
        for i in range(t.page_count):
            oi = i + offset
            if oi >= o.page_count:
                break
            v = inspect_page(o[oi], t[i], i + 1)
            out.append(v)
            if progress:
                progress(i + 1, t.page_count, v)
    return out
