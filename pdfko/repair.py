"""PDF 텍스트 레이어 손상 복구.

LaTeX으로 만든 PDF를 다른 도구(특히 macOS Quartz)가 다시 저장하면, 폰트에
ToUnicode CMap 이 빠지면서 추출 텍스트가 망가지는 일이 흔하다. 추출기가
TeX 폰트의 **글리프 슬롯 번호**를 엉뚱한 유니코드로 해석하기 때문이다.

가장 골치 아픈 점은 **같은 유니코드가 원본 폰트에 따라 다른 글자를 뜻한다**
는 것이다.

    슬롯 0x0B → U+21B5 '↵'
        본문 폰트(CMR/CMBX)에서는  ff  합자
        수학 폰트(CMMI)에서는      α

그래서 일괄 치환은 위험하다. `di↵erent` 는 `different` 가 맞지만
`↵w` 는 `αw`(알파 곱하기 w)이지 `ffw` 가 아니다.

판별 규칙: **앞 글자가 라틴 문자면 합자, 아니면 수학 기호.**

`pdffonts` 로 `uni no` 가 보이면 이 손상을 의심할 것.
"""

from __future__ import annotations

import re
import unicodedata

# 앞 글자가 라틴 문자인 ↵ → ff 합자, 그 밖에는 α
_LIG_FF = re.compile(r"(?<=[A-Za-z])↵")

# 수학 폰트에서 유래한 글리프. 치환하지 않고 **탐지만** 한다.
# 값은 '실제로는 이 글자였다'는 참고 기록이다. 번역 도구가 수식을 제대로
# 마스킹했다면 이 문자들은 모델까지 오지 않는다. 오면 마스킹 실패 신호다.
MATH_GLYPHS: dict[str, str] = {
    "⇡": "π", "⇤": "∗", "✓": "θ", "⇢": "ρ/⊂", "⌧": "τ", "⇥": "×",
    "⌘": "η", "⇠": "ξ/∼", "⇣": "σ/⊃", "⇧": "Π", "⌦": "Ω", "◆": "ι/)",
}
# 빈 키가 하나라도 섞이면 정규식 대안이 빈 문자열로 끝나 **모든 위치에 매칭**된다.
# 실제로 그런 항목이 있어서 오탐률이 100%였다 — 깨끗한 논문 PDF에서 39,499건,
# 운영 중이던 프록시에서 요청 1,877건 전부가 '수식 누수'로 찍혔다.
_MATH_RE = re.compile("|".join(re.escape(k) for k in MATH_GLYPHS if k))

# 조판 잔재 제어문자. 개행·탭은 남긴다.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")

# 번역 엔진이 넣는 자리표시자와 리치텍스트 태그. 이 구간은 절대 건드리지 않는다.
_PROTECT_RE = re.compile(r"(\{v\d+\}|</?style[^>]*>)")


def _repair_segment(text: str) -> str:
    text = _LIG_FF.sub("ff", text)
    text = text.replace("↵", "α")
    # 결합 문자(v̂, x̄)를 합성형으로 접어 토크나이저 친화적으로 만든다
    text = unicodedata.normalize("NFC", text)
    text = _CTRL_RE.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def repair(text: str) -> str:
    """본문 텍스트를 수선한다. 자리표시자·태그 구간은 그대로 보존한다.

    줄바꿈 하이픈 결합(dehyphenation)은 하지 않는다. 번역 엔진이 문자 상자에서
    문단을 스스로 재조립하므로 pdftotext 에서 보이던 분절이 여기엔 없고,
    섣불리 이으면 `on-policy` 같은 정당한 하이픈 용어를 망친다.
    """
    if not text:
        return text
    parts = _PROTECT_RE.split(text)
    # split 결과에서 홀수 인덱스가 보호 구간이다
    return "".join(p if i % 2 else _repair_segment(p) for i, p in enumerate(parts))


def find_math_leaks(text: str) -> list[str]:
    """수식 마스킹이 새어 나왔는지 탐지한다. 비어 있으면 정상."""
    return sorted(set(_MATH_RE.findall(text or "")))


def hangul_ratio(text: str) -> float:
    """한글 음절 비율. 번역이 실제로 일어났는지 판정하는 데 쓴다.

    글자가 하나도 없으면(기호뿐이면) 번역 대상이 아니므로 1.0 을 돌려준다.
    """
    if not text:
        return 0.0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if "가" <= c <= "힣") / len(letters)


def looks_damaged(text: str, threshold: int = 20) -> bool:
    """이 텍스트 레이어가 손상되었는지 대략 판정한다.

    `↵` 나 수학 글리프가 임계값 넘게 나오면 손상으로 본다.
    도구가 시작할 때 경고를 띄우는 용도.
    """
    return text.count("↵") + len(_MATH_RE.findall(text)) >= threshold
