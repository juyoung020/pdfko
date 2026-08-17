"""깨진 합자가 '수식'으로 오인되는 문제를 푼다.

## 무엇이 일어나는가

LaTeX PDF의 손상된 텍스트 레이어에는 `di↵erent`(= different) 같은 문자열이
들어 있다. 그런데 U+21B5 `↵` 는 **번역 엔진이 출력에 쓸 수 있는 어떤 폰트에도
없다.** 그래서 엔진은 "이 글자는 못 그린다 → 수식이다"라고 판단하고 자리표시자로
바꿔 버린다. 모델에게는 이렇게 도착한다:

    di{v1}erent

모델은 가운데가 뭔지 모른 채 번역해야 하고, 번역문 어딘가에 `{v1}` 을 남긴다.
엔진은 그 자리에 다시 `↵` 글리프를 그린다. 결과적으로 **한국어 문장 한복판에
`↵` 가 박힌다.**

즉 미들웨어에서 합자를 고쳐도 소용이 없다. 고칠 텍스트가 미들웨어에 도착하기
전에 이미 자리표시자로 바뀌어 있기 때문이다.

## 해법

원본 PDF를 미리 훑어 `([A-Za-z]+)↵([A-Za-z]+)` 형태를 모아 사전을 만든다.
번역 요청이 오면 `di{v1}erent` 처럼 **라틴 문자 사이에 낀 자리표시자**를 찾아
사전과 대조하고, 맞으면 자리표시자를 지우고 원래 단어로 되돌린다.

    di{v1}erent  →  different      (자리표시자 소멸)

자리표시자가 사라졌으니 모델도 엔진도 그것을 다룰 일이 없다.

## 무엇을 건드리면 안 되는가

`BE{v2}BE`, `MDP{v4}MDP`, `w{v2}x` 처럼 **진짜 수식**도 라틴 문자 사이에 낀다.
사전에 없는 조합은 절대 건드리지 않는다. 사전은 원본 문서에서 실제로 관측한
것만 담으므로 추측이 섞이지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 라틴 문자 사이에 낀 자리표시자
# 오른쪽이 하이픈으로 시작하는 경우도 잡는다: `o{v3}-policy` = off-policy
#
# 엔진은 합자 자리에서 서식 구간을 끊었다 다시 여는 일이 잦다.
#     `<style id='4'>temporal-di</style>{v6}<style id='5'>erence</style>`
# 글자가 자리표시자에 바로 붙어 있지 않으므로 태그를 건너뛰고 봐야 한다.
# 실측: 이걸 안 넘으면 `temporal-difference` 가 `시간적 차이↵에` 로 나온다.
_TAGS = r"(?:</?style[^>]*>\s*)*"
WEDGE_RE = re.compile(rf"([A-Za-z]+)({_TAGS})(\{{v\d+\}})({_TAGS})(-?[A-Za-z]+)")

# 원본에서 찾을 손상 합자. repair.py 의 판별과 같은 규칙이다.
_SRC_RE = re.compile(r"([A-Za-z]+)↵(-?[A-Za-z]+)")


def build_table(src_pdf: str | Path, first: int = 1, last: int | None = None
                ) -> dict[str, str]:
    """원본을 훑어 (왼쪽, 오른쪽) → 복구된 단어 사전을 만든다.

    키는 `"왼쪽\\x00오른쪽"` 형태의 문자열이다(JSON 으로 저장하기 위해).
    """
    import pymupdf

    table: dict[str, str] = {}
    with pymupdf.open(src_pdf) as doc:
        end = min(last or doc.page_count, doc.page_count)
        for i in range(first - 1, end):
            for left, right in _SRC_RE.findall(doc[i].get_text()):
                table[f"{left}\x00{right}"] = f"{left}ff{right}"
    return table


def save(table: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def dissolve(text: str, table: dict[str, str]) -> tuple[str, int]:
    """`di{v1}erent` → `different`. (고친 텍스트, 없앤 자리표시자 수)

    엔진이 문단을 중간에서 자르는 일이 있어(`is identic` 처럼) 정확히 일치하지
    않을 수 있다. 그래서 사전의 왼쪽은 **접미사**로, 오른쪽은 **접두사**로 맞춘다.
    """
    if not text or not table:
        return text, 0

    removed = 0

    def sub(m: re.Match) -> str:
        nonlocal removed
        left, t1, t2, right = m.group(1), m.group(2), m.group(4), m.group(5)

        def fixed() -> str:
            """자리표시자만 `ff` 로 바꾸고 **태그는 그대로 둔다.**

            태그를 함께 지우면 `<style>` 이 짝을 잃는 배치가 있다
            (`un<style>balanced di</style>{v9}erence`). 자리에 글자만 끼우면
            어떤 배치에서도 여닫이가 어긋나지 않고, 이어 붙인 본문은
            `difference` 로 올바르다. `ff` 두 글자가 서식 구간 밖에 놓이는
            것이 유일한 대가다.
            """
            return f"{left}{t1}ff{t2}{right}"

        key = f"{left}\x00{right}"
        if key in table:
            removed += 1
            return fixed()
        # 잘린 조각 대응: 사전 항목의 끝/시작과 맞는지 본다.
        # 양쪽 **두 글자 이상**일 때만 본다. 한 글자까지 허용하면 `e{v1}i` 가
        # 아무 사전 항목에나 걸려 진짜 수식 자리표시자를 지운다 — 실측으로
        # 한 글자 기준은 676건 중 25건 오탐, 두 글자 기준은 4,096건 중 0건이었다.
        if len(left) >= 2 and len(right.lstrip("-")) >= 2:
            for k in table:
                kl, _, kr = k.partition("\x00")
                if kl.endswith(left) and kr.startswith(right):
                    removed += 1
                    return fixed()
        return m.group(0)

    return WEDGE_RE.sub(sub, text), removed
