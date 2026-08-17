"""문서에서 용어집 후보를 뽑는다. **분야별 어휘 목록을 쓰지 않는다.**

## 왜 필요한가

번역 품질을 재보면 흔한 용어는 모델이 이미 정확히 옮긴다. 용어집이 값을 하는
곳은 따로 있다 — **역어가 갈리는 용어**와 **표기 흔들림**이다. 8쪽을 용어집
있이/없이 번역해 비교하니 여섯 용어 중 넷은 완전히 같았고, 갈린 것은 둘이었다.

    return          이득(용어집)   ↔  수익(없음)
    value function  가치 함수 ×3   ↔  가치 함수 ×2 + 값 함수 ×1

그래서 용어집은 정확성 장치가 아니라 **표기를 고정하는 장치**다. 그런데 무엇을
고정할지는 교재마다 다르다. 강화학습 용어 목록을 도구에 박아 두면 그 책에만
맞는 도구가 된다.

## 어떻게 뽑는가

분야를 모르는 채로 "이 책의 전문 용어"를 찾아야 한다. 세 가지 신호를 쓴다.

1. **반복되는 두 낱말 구** — `value function`, `dynamic programming`.
   전문 용어는 구인 경우가 많고, 구는 잡음이 적다.
2. **하이픈 낱말** — `off-policy`, `temporal-difference`.
3. **이탤릭으로 쓰인 적 있는 단일어** — 교재는 용어를 처음 정의할 때
   이탤릭으로 쓴다(`\\emph`). 이 신호가 없으면 `case`, `because`, `possible`
   같은 흔한 낱말이 빈도 상위를 채운다.

`babeldoc` 에도 자동 추출이 있지만 7B 모델로 8쪽을 돌렸을 때 **0개**를
뽑았고 결과도 용어집 없음과 동일했다. 그래서 직접 뽑는다. LLM 을 쓰지 않으므로
비용은 텍스트 한 번 훑는 것뿐이다.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .repair import repair

# 어느 분야에서든 용어가 아닌 낱말. 분야 어휘가 아니라 **기능어와 서술어**다.
STOP = set("""
the a an and or but if then than that this these those of in on at to for with from by as is are
was were be been being it its they them their we our you your he she his her not no can may might
will would shall should must do does did have has had there here when where which who whom what
how why all any both each few more most other some such only own same so too very just one two
three four five first second third also into over under between about following above below thus
however therefore hence while since although though because case possible many given used see let
now consider suppose called shown means example examples figure chapter section exercise equation
table page part appendix note notes eqs eqs like well make makes made take takes taken good better
best large small later often always never every another using based upon within without
after right general point complete single solution again still even much less able need result
results section sections show shows shown described discussion introduction summary
different learn learns learned learning method methods approach approaches system systems problem
problems idea ideas thing things way ways form forms type types kind number numbers
""".split())

_WORD = re.compile(r"[^A-Za-z\- ]+")


def _norm(t: str) -> str:
    """복수형을 단수로 접는다. `states` 와 `state` 를 따로 세면 안 된다."""
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("sses") or t.endswith("shes"):
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


def extract(pdf: str | Path, first: int = 1, last: int | None = None,
            min_count: int = 5, top: int = 60) -> list[tuple[int, str]]:
    """(출현횟수, 용어) 목록을 빈도순으로. 분야 어휘 목록을 쓰지 않는다."""
    import pymupdf

    words: list[str] = []
    italic: set[str] = set()
    with pymupdf.open(pdf) as d:
        end = min(last or d.page_count, d.page_count)
        for i in range(max(0, first - 1), end):
            pg = d[i]
            # 합자 손상을 먼저 고친다. 안 그러면 `arti cial`(artificial)과
            # `erent`(different)가 상위 후보로 올라온다. 실측으로 그랬다.
            words += _WORD.sub(" ", repair(pg.get_text())).lower().split()
            for b in pg.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    for s in ln.get("spans", []):
                        f = s.get("font", "")
                        # 수학 폰트(CMMI)는 변수라 이탤릭이어도 용어가 아니다
                        if ("TI" in f or "Italic" in f) and "CMMI" not in f:
                            italic.update(
                                _WORD.sub(" ", repair(s.get("text", ""))).lower().split())

    ok = lambda w: len(w) > 2 and w not in STOP           # noqa: E731
    bi = Counter(f"{_norm(a)} {_norm(b)}" for a, b in zip(words, words[1:])
                 if ok(a) and ok(b) and a.isalpha() and b.isalpha())
    hyp = Counter(_norm(w) for w in words
                  if "-" in w and 6 < len(w) < 30 and w.strip("-").replace("-", "").isalpha())
    uni = Counter(_norm(w) for w in words if len(w) > 4 and w not in STOP and w.isalpha())

    cand = [(n, t) for t, n in bi.most_common(top * 2) if n >= min_count]
    cand += [(n, t) for t, n in hyp.most_common(top) if n >= min_count]
    # 구에 이미 들어 있는 낱말은 단일어로 또 넣지 않는다
    inside = {w for _, p in cand for w in p.replace("-", " ").split()}
    cand += [(n, t) for t, n in uni.most_common(top * 4)
             if n >= min_count and _norm(t) in {_norm(x) for x in italic}
             and t not in inside][:top // 2]

    seen, out = set(), []
    for n, t in sorted(cand, key=lambda x: -x[0]):
        t = t.strip().strip("-").strip()
        # 앞뒤 하이픈은 그리스 문자가 떨어져 나간 흔적이다(`ε-greedy` → `-greedy`).
        # 텍스트 레이어가 깨진 PDF 에서 흔하다.
        if not t or t in seen or len(t) < 3:
            continue
        seen.add(t)
        out.append((n, t))
    return out[:top]


def write_csv(path: Path, rows: list[tuple[int, str]]) -> None:
    """번역어 칸을 비운 채로 저장한다. 사용자가 채워 넣으면 그대로 쓸 수 있다.

    주석이나 여분 칸을 넣지 않는다. 이 파일은 그대로 `--glossary` 로 되돌아와
    번역 엔진의 CSV 파서를 통과해야 한다. 빈도는 줄 순서로 이미 드러난다.
    """
    lines = ["source,target,tgt_lng"]
    lines += [f"{t},," for _, t in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
