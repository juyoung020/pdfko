"""BabelDOC ↔ 로컬 추론 서버 사이에 끼우는 OpenAI 호환 미들웨어.

BabelDOC은 원문 텍스트에 손댈 훅을 주지 않는다. 그런데 이 PDF는 텍스트
레이어가 손상되어 있어(README 참고) 산문에 `di↵erent` 같은 문자열이 섞인다.
그래서 요청이 모델에 닿기 전에 가로채 고쳐야 한다.

## BabelDOC의 프로토콜

평문을 주고받지 않는다. 사용자 메시지 끝에 JSON 배열을 붙여 보낸다:

    [ {"id": 0, "input": "...", "layout_label": "plain text"}, ... ]

그리고 같은 길이의 JSON 배열을 요구한다:

    [ {"id": 0, "output": "번역문"}, ... ]

따라서 이 프록시는 **JSON을 인식해야** 한다. 평문을 강요하는 지시를 붙이면
BabelDOC이 `Expecting value: line 1 column 1` 로 죽는다. 검증도 배열 전체가
아니라 **항목별로** 해야 한다 — 지시문에 예시로 적힌 `{v1}`, `{name}` 을
실제 자리표시자로 착각하면 안 되기 때문이다.

## 하는 일
  1. 본문 합자 복구            ligatures.repair()
  2. 수식 마스킹 누수 탐지      ligatures.find_math_leaks()
  3. ko-KR → Korean (한국어) 재작성 (폰트 선택용 코드가 지시문에 새는 것 방지)
  4. 샘플링 강제 (자리표시자 보호를 위해 낮은 온도)
  5. 항목별 자리표시자·한글 검사 → 재시도 → 실패 시 원문 반환
  6. SQLite 캐시 → 548쪽 작업이 중간에 죽어도 재개가 싸다

환경 변수
  UPSTREAM   기본 http://127.0.0.1:11500/v1
  MODEL      업스트림에 요청할 모델 이름
  CACHE_DB   기본 ../cache/trans.db
  LOG_DIR    기본 ../logs
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import glyphmap
from .repair import find_math_leaks, hangul_ratio, repair

HERE = Path(__file__).parent
UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:11500/v1").rstrip("/")
MODEL = os.environ.get("MODEL", "hy-mt2-7b")
# 기본 경로는 사용자 상태 폴더로. 패키지 디렉터리(site-packages)에 SQLite 를
# 만들면 읽기 전용 설치에서 임포트조차 안 되고, 지워도 잔재가 남는다.
_STATE = Path(os.environ.get("XDG_STATE_HOME",
                             Path.home() / ".local" / "state")) / "pdfko"
CACHE_DB = Path(os.environ.get("CACHE_DB", _STATE / "cache" / "trans.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", _STATE / "logs"))
# 임포트 시점에 폴더를 만들거나 DB 를 열지 않는다. `runner.Server` 가 규칙
# 지문을 계산하려고 이 모듈을 임포트하므로, **모든 CLI 실행이** 쓰지도 않을
# SQLite 를 부모 프로세스에 만들고 있었다. pytest 도 마찬가지였고, 읽기 전용
# HOME 에서는 임포트 자체가 PermissionError 로 죽었다.
SAMPLE_N = int(os.environ.get("SAMPLE_N", "8"))
# 깨진 합자 사전. cli 가 원본을 훑어 만들어 두고 경로를 넘겨준다.
GLYPHMAP = glyphmap.load(Path(os.environ["GLYPHMAP"])) if os.environ.get("GLYPHMAP") else {}

# 사용자가 준 용어집·추가 프롬프트의 지문. cli/web 이 파일 내용을 해시해 넘긴다.
#
# 요청 본문에서 뽑으려 했다가 실패했다. BabelDOC 의 LLM 경로는 **system 메시지를
# 아예 보내지 않는다** — 역할 지시도 용어집 표도 전부 user 메시지 한 덩어리에
# 말아 넣는다(`_build_llm_prompt` 의 `$role_block`, `$glossary_tables_block`).
# 실측으로 운영 중 요청의 역할 구성은 전부 `('user',)` 였고, system 을 해시한
# 값은 빈 문자열의 해시 `e3b0c44298fc` 로 고정이었다. 즉 용어집을 바꿔도 키가
# 그대로였다. 그 user 메시지를 통째로 해시할 수도 없다 — 안에 문단별 문맥
# 힌트가 섞여 있어 캐시가 산산조각 난다. 그래서 곁길로 받는다.
USER_RULES = os.environ.get("USER_RULES", "")

# ── 규칙 지문 ──────────────────────────────────────────────────────────
# 검증 규칙이나 프롬프트를 바꾸면 캐시가 자동으로 무효화되어야 한다.
# 손으로 지우는 방식은 네 번 연속 실패했다 — 규칙을 고쳐도 이미 캐시된
# 문단은 새 규칙을 거칠 기회가 없어서, 고쳤다고 믿은 채 같은 결과를 받았다.
#
# 그래서 **검증 함수의 소스 코드 자체**를 해시해 캐시 키에 넣는다.
# 규칙을 한 줄만 바꿔도 지문이 달라지고, 영향받는 항목만 다시 번역된다.
def _rules_fingerprint(gm: dict[str, str] | None = None,
                       user_sig: str | None = None) -> str:
    """검증 규칙이 바뀌면 캐시를 자동 무효화하기 위한 지문.

    예전에는 함수 세 개의 소스만 해시했다. 그런데 규칙을 지고 있는 곳은
    스무 곳이 넘는다 — 정규식, hangul_ratio, split_runs, parse_output,
    CONCISE_RULE, repair() … 실측해 보니 여덟 군데를 바꿔도 지문이 그대로였다.
    같은 수정을 네 번 반복하게 만든 실패를 막으려던 장치가 정작 그 실패를
    막지 못했다. 그래서 **모듈 전체**를 해시한다. 주석만 고쳐도 무효화되는
    대가를 치르지만, 정확성 캐시에서는 그쪽이 옳다.
    """
    import inspect
    import sys as _sys
    from . import glyphmap as _g
    from . import repair as _r
    parts = []
    for mod in (_sys.modules[__name__], _r, _g):
        try:
            parts.append(inspect.getsource(mod))
        except OSError:
            parts.append(getattr(mod, "__name__", "?"))
    # 합자 사전도 규칙의 일부다. 사전을 붙이면 지문이 달라져야
    # runner 가 낡은 프록시를 갈아치운다.
    parts += [str(PH_HEAVY), str(WIDTH_MAX),
              json.dumps(sorted((GLYPHMAP if gm is None else gm).items()),
                         ensure_ascii=False),
              USER_RULES if user_sig is None else user_sig]
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:12]


CACHE_EPOCH = "2"

PLACEHOLDER_RE = re.compile(r"\{v\d+\}")
# 엔진이 실제로 인식하는 태그만 정상으로 센다. `[^>]*` 로 헐겁게 받으면
# 모델이 `id=` 를 떨어뜨린 `<style '5'>` 도 멀쩡한 태그로 세어져 검사를
# 통과하고, 그 문자열이 **본문에 글자 그대로 찍힌다.** 실측으로 논문 한
# 페이지에 7개가 그대로 인쇄됐다.
STYLE_RE = re.compile(r"</style>|<style\s+id=['\"][^'\"]*['\"]\s*>")
# 괄호 비슷한 문자(전각 포함)에 붙어 있는 `style`. 태그를 쓰려다 만 흔적이다.
# 본문에 그냥 쓰인 낱말 `style` 은 여기 걸리지 않는다.
_STYLE_NEAR = re.compile(r"[<〈＜﹤]\s*/?\s*style", re.I)
LANG_RE = re.compile(r"\bko[-_]KR\b", re.I)

# 존댓말 어미. 지시문에서 금지하는데도 새어 나온다 — 출고본 490쪽에서 8곳,
# 미리보기 한 쪽에서는 5%까지 올라갔다. 한 화면 안에서 문체가 바뀌면
# 번역기 티가 난다. 지시만 하지 말고 검사해서 다시 시킨다.
_JONDAE_RE = re.compile(r"(?:습니다|합니다|입니다|됩니다|십시오|하세요|"
                        r"이에요|예요|드립니다|주세요)\s*[.!?)\]]?\s*$")

# BabelDOC의 프롬프트가 구조·자리표시자·용어집을 이미 상세히 지시한다.
# 여기서는 그 지시와 싸우지 않으면서 문체만 못박는다.
# 바이트 단위로 불변이라 추론 서버의 프롬프트 접두사 캐시가 그대로 산다.
#
# 주의: 예전 프롬프트는 "문어체 -이다/-한다" 를 무조건 강제했다. 그랬더니
# 모델이 `Chapter 6` 같은 **제목까지 문장으로 바꿔** "제6장이다" 로 만들면서
# 그 과정에서 자리표시자를 흡수해 버렸다. 실패의 상당수가 이것이었다.
# 그래서 문장에만 문어체를 적용하고 제목·캡션은 명사구로 두게 한다.
# 분야를 **말하지 않는다.** 예전에는 "academic machine-learning textbooks" 라고
# 못박아 두었다. 이건 용어집 하드코딩보다 나쁘다 — 용어집은 옵션이지만 이
# 지시문은 모든 문서의 모든 문단에 붙는다. 화학 교재를 넣어도 모델에게
# "머신러닝 교재를 번역하는 중"이라고 말하는 셈이었다.
SYSTEM_PREFIX = (
    "You are a professional English-to-Korean translator for academic and "
    "technical documents. Follow the user's instructions and output "
    "format exactly.\n"
    "- Full sentences: academic Korean, 문어체 declarative (-이다 / -한다). "
    "Never 존댓말.\n"
    "- Headings, titles, captions, table cells and short fragments: translate "
    "as a noun phrase. Do NOT turn them into sentences and do NOT append "
    "endings such as -이다 / -한다.\n"
    "- Never absorb, merge or drop a {vN} token while rephrasing."
)

# ── 간결 모드 ──────────────────────────────────────────────────────────
# 한국어가 영어보다 길어지면 그림·수식이 빽빽한 페이지에서 자리가 모자라
# 글자가 겹친다. 그 페이지만 다시 번역할 때 이 모드를 켠다. 의미는 지키되
# 길이를 줄이도록 지시한다. 되돌리기(원문 유지)보다 먼저 시도해야 할 단계다.
MODE = {"concise": False}

CONCISE_RULE = (
    "\n- IMPORTANT: keep the translation SHORT. The layout has no spare room. "
    "Aim for at most the same character count as the English source. "
    "Drop filler, prefer concise verb endings, avoid redundant particles. "
    "Never drop information or placeholders — only tighten wording."
)

# 자리표시자가 이보다 많은 문단은 통째로 번역시키지 않고 조각내서 보낸다.
# 조각 모드는 자리표시자를 우리가 원위치에 끼우므로 구조적으로 실패하지
# 않지만, **어순이 조각 단위로 굳는다** — `여기서 Bt, 라고 불리는 기준선,
# 는 에 의존하지…` 처럼 읽기 나쁜 문장이 된다. 그러니 문턱은 통짜 번역이
# 실제로 실패하기 시작하는 지점이어야 하고, 그보다 낮으면 성공할 수 있는
# 문단까지 굳혀 버린다. 값을 바꿔 재려면 환경 변수로 덮어쓸 수 있다.
PH_HEAVY = int(os.environ.get("PDFKO_PH_HEAVY", "15"))

# 번역문이 원문보다 이 배수를 넘으면 거부한다.
# 측정해 보니 한국어는 전체적으로 오히려 짧다(중앙값 0.93배). 문제는 평균이
# 아니라 꼬리다 — 7.3%가 1.3배를 넘고 p99는 2.13배다. 원인은 엔진이 문단을
# 중간에서 자른 조각(`... is identic`)을 모델이 **완성해 버리는** 것이다.
# 그 한 문단이 줄 수를 폭발시켜 페이지를 깨뜨린다.
WIDTH_MAX = float(os.environ.get("PDFKO_WIDTH_MAX", "1.35"))


def est_width(s: str) -> float:
    """대략적인 조판 폭(em). 한글은 1, 라틴은 0.5, 공백은 0.33."""
    s = PLACEHOLDER_RE.sub("", STYLE_RE.sub("", s or ""))
    w = 0.0
    for c in s:
        if c.isspace():
            w += 0.33
        elif "가" <= c <= "힣" or "\u3000" <= c <= "\u9fff":
            w += 1.0
        else:
            w += 0.5
    return w


# ---------------------------------------------------------------- JSON 추출
def extract_array(text: str) -> tuple[list | None, int, int]:
    """메시지 끝에 붙은 JSON 배열을 찾는다. (배열, 시작, 끝)

    지시문 안에도 `[[...]]` 같은 대괄호가 있으므로 끝에서부터 괄호를 맞춰
    거슬러 올라간다. 앞에서부터 정규식으로 잡으면 엉뚱한 곳이 걸린다.
    """
    end = text.rfind("]")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if text[i] == "]":
                depth += 1
            elif text[i] == "[":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(text[i:end + 1])
                        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                            return arr, i, end + 1
                    except json.JSONDecodeError:
                        pass
                    break
        end = text.rfind("]", 0, end)
    return None, -1, -1


def swap_array(text: str, start: int, end: int, items: list) -> str:
    """메시지 안의 JSON 배열을 다른 항목 목록으로 갈아끼운다.

    재시도할 때 **실패한 항목만** 다시 보내기 위해 쓴다. 배치 10개 중 1개가
    틀렸다고 10개를 다시 번역시키면 GPU가 재작업만 하게 된다.
    """
    return text[:start] + json.dumps(items, ensure_ascii=False, indent=1) + text[end:]


# 조각 모드에서 모델에 넘기지 않고 우리가 직접 원위치에 되돌릴 것들.
# `{vN}` 뿐 아니라 `<style>` 태그도 포함한다 — 태그를 본문에 섞어 보내면
# 모델이 괄호를 전각으로 바꾸거나 속성을 지워서 BabelDOC이 인식하지 못하고
# `</style〉` 가 글자 그대로 찍힌다.
KEEP_RE = re.compile(r"\{v\d+\}|</?style[^>]*>")


def split_runs(text: str) -> tuple[list[str], list[str]]:
    """자리표시자와 style 태그를 기준으로 본문 조각을 분리한다.

    반환: (조각 n+1개, 보존대상 n개) — 조각[0] + 보존[0] + 조각[1] + ... 로 복원된다.
    모델은 조각만 보므로 자리표시자도 태그도 구조적으로 잃을 수 없다.
    """
    return KEEP_RE.split(text), KEEP_RE.findall(text)


def is_translatable(run: str) -> bool:
    """번역할 값어치가 있는 조각인가. 공백·구두점뿐이면 그냥 둔다."""
    return sum(c.isalpha() for c in run) >= 3


def parse_output(raw: str) -> list | None:
    """모델 응답에서 JSON 배열을 꺼낸다. 코드펜스로 감싸 와도 받아준다."""
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t).strip()
    try:
        arr = json.loads(t)
        if isinstance(arr, list):
            return arr
    except json.JSONDecodeError:
        pass
    arr, _, _ = extract_array(t)
    if arr is not None:
        return arr
    # 닫는 괄호만 빠진 응답을 살린다.
    #
    # 이게 이 파일에서 가장 값싼 수정이면서 가장 큰 피해를 막는다. 실측으로
    # 모델이 **완전하고 올바른** 번역을 내놓고(자리표시자 7개 전부 보존,
    # 폭 0.77배, check() 통과) 마지막 `]` 하나만 빠뜨렸다. 그걸 버리는 바람에
    # 세 번의 재시도가 전부 실패하고 조각 모드로 떨어져, 사람이 읽을 수 없는
    # `v∗흥미로운 점은` 이 페이지에 실렸다. 같은 문단을 통짜 경로로 처리한
    # 다른 실행에서는 멀쩡한 한국어가 나왔다.
    #
    # 괄호를 보충해도 JSON 이 성립하지 않으면 그대로 실패한다 — 내용을
    # 지어내지는 않는다.
    for tail in ("]", "}]", '"}]', '"}]'):
        try:
            arr = json.loads(t + tail)
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            STATS["json_repaired"] += 1
            return arr
    return None


# ---------------------------------------------------------------- 캐시
_lock = threading.Lock()
_DB: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    """캐시 DB 를 처음 쓸 때 연다. **임포트 시점에 열지 않는다.**"""
    global _DB
    if _DB is None:
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(CACHE_DB, timeout=30, check_same_thread=False)
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.execute(
            "CREATE TABLE IF NOT EXISTS tr ("
            " k TEXT PRIMARY KEY, model TEXT, src TEXT, tgt TEXT,"
            " attempts INT, ts REAL)")
        _DB.commit()
    return _DB


def cache_get(k: str) -> str | None:
    with _lock:
        r = _db().execute("SELECT tgt FROM tr WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def cache_put(k: str, src: str, tgt: str, attempts: int) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO tr(k,model,src,tgt,attempts,ts) VALUES(?,?,?,?,?,?)",
            (k, MODEL, src[:8000], tgt, attempts, time.time()),
        )
        _db().commit()


STATS = {"requests": 0, "cache_hits": 0, "retries": 0, "failures": 0,
         "math_leaks": 0, "ligature_fixes": 0, "items": 0, "items_failed": 0,
         "items_rescued": 0, "style_dropped": 0, "fragment_mode": 0, "fragment_retried": 0,
         "ligature_dissolved": 0, "json_repaired": 0}
_sampled = 0

app = FastAPI(title="pdfko proxy")
client = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0))


def log_jsonl(name: str, obj: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / name, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 검증
_ENG_RUN = re.compile(r"(?:[A-Za-z][A-Za-z'’\-]*\s+){5,}[A-Za-z][A-Za-z'’\-]*")
_YEAR_RE = re.compile(r"\b(?:1[89]|20)\d\d[a-z]?\b")
_MASK_RE = re.compile(r"</?style[^>]*>|\{v\d+\}")


def leftover_english(tgt: str) -> str | None:
    """번역된 문단에 통째로 남은 **영어 산문**을 돌려준다. 없으면 None.

    ## 왜 한글 비율로는 안 되는가

    `check` 의 한글 바닥(0.15)은 문단 **전체** 비율이라, 여덟 문장 중 일곱이
    한국어면 남은 한 문장이 영어여도 통과한다. 실측: 이 책 3660문단 중
    165개(4.5%)가 그렇게 통과했고, 쪽 단위 검사는 "미번역 0쪽"이라고 찍었다.
    수식 자리표시자가 든 문단에서 특히 잦다 — 자리표시자 없는 문단은 실패율
    1%인데 있는 문단은 8~10%다. 모델이 `{v1}` 이 섞인 문장을 만나면 통째로
    영어를 되뱉는다.

    ## 왜 이걸로 되는가

    표본 24건을 다시 보냈더니 **24건 전부 한국어가 됐다.** 모델이 못 하는 게
    아니라 한 번에 놓치는 것이므로, 짚어서 다시 물으면 된다.

    ## 무엇을 봐주는가

    영어로 남기는 게 **맞는** 것들이 있다. 어휘 목록을 두지 않고 형태로만 가른다.

      · 문단이 아직 한국어가 아니면      → 다른 검사(한글 바닥)의 몫이다
      · 낱말이 전부 대문자              → 약어 풀이 (MATCHBOX EDUCABLE …)
      · 거의 모든 낱말이 첫 글자 대문자   → 인명·논문 제목
      · 근처에 연도가 있으면             → 인용 (Sutton and Barto, 1998)
      · 세 글자 이상 낱말이 넷 미만       → 행렬·축 라벨 (`A A A x v v x b`)
    """
    body = _MASK_RE.sub(" ", tgt)
    if hangul_ratio(body) < 0.15:
        return None
    for m in _ENG_RUN.finditer(body):
        run = m.group(0)
        words = run.split()
        if len([w for w in words if len(w) >= 3]) < 4:
            continue
        if run.isupper():
            continue
        if sum(w[:1].isupper() for w in words) >= len(words) - 1:
            continue
        if _YEAR_RE.search(body[max(0, m.start() - 60):m.end() + 60]):
            continue
        return run
    return None


_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+")
_WORDISH = re.compile(r"[A-Za-z][A-Za-z'-]*")


def has_prose(src: str) -> bool:
    """번역할 산문이 있는가. **이 판단은 여기서만 내린다.**

    예전에는 `라틴 글자 수 >= 12` 로 갈랐고, 그 식이 네 곳에 흩어져 있었다.
    축이 틀렸다:

      · `https://www.davidsilver.uk/` 는 글자가 21자라 기준을 **통과**한다.
        그래서 한글 검사를 받고 실패한다 — 정답이 원문 그대로인데도. 3회
        재시도에 조각 모드까지 태운 뒤 포기했다. 실측 53개 항목 중 7개.
      · 반대로 `guidance`, `Auto Pilot` 처럼 **번역해야 하는** 짧은 라벨은
        기준에 걸려 검사를 면제받았다.

    URL·자리표시자·서식 태그를 걷어내고 낱말이 남는지 본다. 낱말 하나뿐이면
    네 글자는 되어야 한다 — `RL`, `MDP` 같은 약어만 남은 것은 산문이 아니다.
    """
    t = _URL_RE.sub(" ", src or "")
    t = PLACEHOLDER_RE.sub(" ", t)
    t = STYLE_RE.sub(" ", t)
    words = [w for w in _WORDISH.findall(t) if len(w) >= 2]
    return len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 4)


class Reason(str):
    """실패 사유. 사람이 읽는 문자열이면서 **종류**를 따로 들고 있다.

    예전에는 문자열뿐이었고, 사다리는 `check(...)[0]` 으로 그마저 버렸다.
    그래서 `repair_hint` 가 같은 판정을 처음부터 다시 추론했다 — 자리표시자를
    다시 비교하고, 폭을 다시 재고, 영어를 다시 찾았다. 판정하는 곳과 이유를
    대는 곳이 둘로 갈라져 있으니 어긋날 수 있었고, 실제로 어긋났다:

        check 의 사유          repair_hint 의 추측      결과
        empty output      →   자리표시자 문제      →   3차까지 헛돌고 영어 잔존
        hangul 0.00 (URL) →   자리표시자 문제      →   URL 에 {vN} 이 박힘

    종류를 실어 보내면 힌트가 판정과 어긋나는 일이 구조적으로 불가능해진다.
    `str` 을 상속하므로 기존 호출자(부분 문자열 검사)는 그대로 쓸 수 있다.
    """

    kind: str

    def __new__(cls, kind: str, text: str) -> "Reason":
        o = super().__new__(cls, text)
        o.kind = kind
        return o


def check(items: list[dict], out: list | None) -> tuple[bool, str]:
    """응답 배열이 입력 배열과 정합한지 항목별로 본다."""
    if out is None:
        return False, Reason("json", "not JSON")
    if len(out) != len(items):
        return False, Reason("length", f"length {len(out)} != {len(items)}")

    by_id = {}
    for o in out:
        if not isinstance(o, dict) or "id" not in o:
            return False, Reason("id", "item missing id")
        by_id[str(o["id"])] = o

    for it in items:
        iid = str(it.get("id"))
        o = by_id.get(iid)
        if o is None:
            return False, Reason("id", f"id {iid} missing")

        tgt = item_output(o)
        if not isinstance(tgt, str) or not tgt.strip():
            return False, Reason("empty", f"id {iid} empty output")
        src = it.get("input", "")

        # 자리표시자는 개수·종류가 정확히 같아야 한다
        want = sorted(PLACEHOLDER_RE.findall(src))
        got = sorted(PLACEHOLDER_RE.findall(tgt))
        if want != got:
            return False, Reason("placeholder", f"id {iid} placeholder {want} != {got}")

        # 모델이 태그 괄호를 전각(〈 〉)이나 다른 문자로 바꿔 쓰면 BabelDOC이
        # 태그로 인식하지 못해 `</style〉` 가 본문에 글자 그대로 찍힌다.
        # 부분 패턴으로 잡으려다 놓친 적이 있어, **출력에 나오는 모든 'style'이
        # 제대로 된 태그의 일부인지** 를 센다. 하나라도 아니면 거른다.
        # 다만 **괄호 비슷한 문자 옆에 있는 `style` 만** 센다. 예전에는 출력에
        # 나오는 `style` 을 전부 셌는데, 그러면 본문에 그 낱말이 정당하게 등장하는
        # 문서(타이포그래피·미술사·CSS 교재)가 통째로 거부되고 3회 재시도 끝에
        # 영어로 남는다. 어느 분야는 되고 어느 분야는 안 되는 상태가 된다.
        n_style_bracketed = len(_STYLE_NEAR.findall(tgt))
        n_style_tag = len(STYLE_RE.findall(tgt))
        if n_style_bracketed > n_style_tag:
            return False, Reason("style", f"id {iid} malformed style tag")

        # <style> 태그는 '경고'까지만 한다.
        # 자리표시자는 수식이라 잃으면 페이지가 깨지지만, style 태그는 이탤릭·볼드
        # 같은 서식일 뿐이라 없어도 본문은 멀쩡하다. 모델이 자주 떨어뜨리는데
        # 이걸로 거부하면 멀쩡한 번역까지 버리고 원문으로 폴백하게 된다.
        if len(STYLE_RE.findall(src)) != len(STYLE_RE.findall(tgt)):
            STATS["style_dropped"] += 1

        # 문체가 섞이면 안 된다. 지시문에서 금지하지만 새어 나온다.
        if _JONDAE_RE.search(tgt.strip()):
            return False, Reason("jondae", f"id {iid} 존댓말")

        # 번역할 산문이 있었는데 한글이 없으면 반향이다.
        # 산문이 없으면(URL·수식·숫자뿐) 원문 그대로가 정답이므로 넘어간다.
        if has_prose(src) and hangul_ratio(tgt) < 0.15:
            return False, Reason("hangul", f"id {iid} hangul {hangul_ratio(tgt):.2f}")

        # 반쪽 번역. 한글 비율만 보면 통과한다 — 80%가 한국어인 문단에 영어
        # 한 문장이 남아도 0.15 바닥을 넘기 때문이다. 실측으로 이 책 3660문단
        # 중 165개(4.5%)가 그 상태였고, 쪽 단위 검사는 "미번역 0쪽"이라고 했다.
        if leftover_english(tgt):
            return False, Reason("english", f"id {iid} 영어 잔류")

        # 길이 폭주. 짧은 라벨과 수식은 면제한다.
        sw = est_width(src)
        if has_prose(src) and sw >= 10:
            ratio = est_width(tgt) / sw
            if ratio > WIDTH_MAX:
                return False, Reason("width", f"id {iid} width {ratio:.2f}x")
    return True, Reason("", "")


def item_output(o) -> str:
    """항목에서 번역문을 꺼낸다. `output` 이 없으면 `input` 도 본다.

    모델이 요청 스키마를 통째로 되풀이해 번역을 **`input` 에 담아** 보내는
    일이 있다. 실측(L02 발표자료):

        {"id": 0, "input": "RL(다른 분야와의 연계)이란 무엇인가?",
         "layout_label": "plain text"}

    번역문 자체는 멀쩡했고 폭 검사도 통과할 물건이었다. 그런데 여기서 빈
    문자열을 돌려주면 **모든 진단이 눈을 감는다** — 폭 초과도, 영어 잔존도,
    자리표시자 유실도 빈 문자열에서는 감지되지 않는다. 그래서 재시도 힌트가
    "자리표시자를 지켜라"로 떨어지고(그 문장에는 자리표시자가 하나도 없다),
    모델은 형식이 틀렸다는 말을 못 들은 채 세 번 다 같은 모양을 돌려준 뒤
    영어 원문이 그대로 남았다. 22쪽 중 11쪽이 그 꼴이었다.

    `input` 을 살려 쓰되 검증은 그대로 건다. 모델이 원문을 그냥 되풀이한
    것이라면 한글 검사에서 걸러진다.
    """
    if not isinstance(o, dict):
        return ""
    v = o.get("output")
    if isinstance(v, str) and v.strip():
        return v
    v = o.get("input")
    return v if isinstance(v, str) else ""


def repair_hint(failed: list[tuple[dict, str]], out: list | None) -> str:
    """무엇을 고쳐야 하는지 짚어 주는 재시도 지시.

    `failed` 는 `(항목, 사유)` 쌍이고, 사유는 `check()` 가 준 `Reason` 이다.
    **여기서 판정을 다시 하지 않는다.** 예전에는 사다리가 사유를 버리고
    (`check(...)[0]`) 이 함수가 처음부터 재추론했다 — 자리표시자를 다시
    비교하고 폭을 다시 재고 영어를 다시 찾았다. 판정하는 곳과 이유를 대는
    곳이 갈라져 있으니 어긋날 수 있었고, 실제로 어긋나 엉뚱한 지시가 나갔다.
    """
    items = [it for it, _ in failed]
    kinds = {getattr(r, "kind", "") for _, r in failed}
    if not out:
        return ("Your previous reply was not a valid JSON array. "
                "Reply with ONLY the JSON array, no prose, no ``` fences.")
    by_id = {str(o.get("id")): o for o in out if isinstance(o, dict)}
    lines = []
    for it in items:
        iid = str(it.get("id"))
        src = it.get("input", "")
        tgt = item_output(by_id.get(iid))
        want = PLACEHOLDER_RE.findall(src)
        got = PLACEHOLDER_RE.findall(tgt)
        missing = [p for p in want if got.count(p) < want.count(p)]
        extra = [p for p in got if want.count(p) < got.count(p)]
        if missing or extra:
            part = [f'id {iid}:']
            if missing:
                part.append("you dropped " + " ".join(sorted(set(missing))))
            if extra:
                part.append("you invented " + " ".join(sorted(set(extra))))
            lines.append(" ".join(part))
    # 길이 폭주는 대개 '잘린 조각을 모델이 완성해 버리는' 것이다.
    wide = []
    for it in items:
        iid = str(it.get("id"))
        tgt = item_output(by_id.get(iid))
        src = it.get("input", "")
        if est_width(src) >= 10:
            r = est_width(tgt) / est_width(src)
            if r > WIDTH_MAX:
                wide.append(f"id {iid}: {r:.1f}x too long")
    if "width" in kinds:
        return (
            "Your translation is far longer than the source.\n"
            + "\n".join(wide)
            + "\nThe input may be a TRUNCATED FRAGMENT of a longer sentence. "
              "Translate ONLY the words present. Do not complete the sentence, "
              "do not add information, do not restate a clause. Keep it at most "
              f"{WIDTH_MAX:.1f}x the source length.")

    # 반쪽 번역. 남은 문장을 그대로 인용해 준다 — "영어가 남았다" 라고만 하면
    # 모델이 어디를 말하는지 못 찾고 같은 출력을 되돌려준다.
    half = []
    for it in items:
        iid = str(it.get("id"))
        run = leftover_english(item_output(by_id.get(iid)))
        if run:
            half.append(f'id {iid}: still English — "{run[:80]}"')
    if "english" in kinds:
        return ("You translated part of these items and left the rest in "
                "English.\n" + "\n".join(half)
                + "\nRe-translate the WHOLE item into Korean. Keep proper "
                  "nouns, citations and code identifiers as they are, but no "
                  "English sentence may remain.")

    if "placeholder" not in kinds:
        # 자리표시자 얘기는 **원문에 실제로 있을 때만** 한다. 없는데 지키라고
        # 하면 모델은 없는 것을 만들어 넣는다. 실측으로 URL 항목이 한글이
        # 없다는 이유로 실패 판정을 받자 이 힌트가 붙었고, 다음 시도에서
        # `https://www.davidsilver.uk/{vN}/` 가 돌아왔다 — 멀쩡한 URL 이
        # 망가진 것이다. 지시가 틀리면 모델은 그 틀린 지시를 정확히 따른다.
        if "placeholder" in kinds:
            return ("Some items were wrong. Re-translate and keep every {vN} "
                    "placeholder exactly once.")
        return ("These items were not translated into Korean. Translate them "
                "fully. Leave URLs, code identifiers, citations and proper "
                "nouns exactly as they are — do not add, invent or reword "
                "anything that is not in the source.")
    return (
        "Your previous reply lost placeholders. Fix ONLY these items and "
        "reply with the complete JSON array again.\n"
        + "\n".join(lines)
        + "\nEvery {vN} token from the input must appear exactly once in the "
          "matching output, spelled identically. They are formulas — never "
          "translate, merge, renumber, or omit them."
    )


# ---------------------------------------------------------------- 엔드포인트
@app.get("/health")
async def health():
    # RULES 를 노출한다. 기동 중인 프록시가 지금 소스와 같은 코드인지
    # 호출자가 확인할 수 있어야 한다. 이게 없어서 13시간 묵은 프로세스를
    # 상대로 "고쳤다"를 반복했다.
    # log_dir/cache_db 도 함께 낸다. 프록시는 규칙만 같으면 다음 실행에서도
    # 재사용되므로, 로그와 캐시는 **처음 띄운 작업 디렉터리**에 계속 쌓인다.
    # 이걸 알리지 않으면 사용자(그리고 나)는 지금 작업 폴더의 logs/ 를 뒤지다
    # 아무것도 못 찾는다. 실제로 그렇게 헤맸다.
    # concise 도 낸다. 복구 중에 프록시가 죽으면 `finally` 의 되돌리기가 실행되지
    # 않아 간결 모드가 켜진 채 남는다. MODE 는 RULES 에 없으므로 다음 실행이
    # 그 프록시를 그대로 이어 쓰면 **책 전체가 간결 모드로 번역된다.**
    # pid 를 낸다. 이게 없으면 "우리 것이고 이 작업 폴더 것"임을 알고도
    # 내릴 방법이 없어, 고아 프록시가 포트를 영영 쥐고 `--fresh` 를
    # 무력화한다(삭제된 캐시 파일의 inode 를 계속 들고 있다).
    return {"ok": True, "upstream": UPSTREAM, "model": MODEL, "pid": os.getpid(),
            "rules": RULES, "log_dir": str(LOG_DIR), "cache_db": str(CACHE_DB),
            "concise": MODE["concise"], "stats": STATS}


@app.post("/mode")
async def set_mode(request: Request):
    """간결 모드를 켜고 끈다. 복구 사다리가 페이지별로 호출한다."""
    b = await request.json()
    MODE["concise"] = bool(b.get("concise"))
    return {"concise": MODE["concise"]}


@app.get("/v1/models")
async def models():
    return {"object": "list",
            "data": [{"id": MODEL, "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    global _sampled
    body = await request.json()
    STATS["requests"] += 1
    msgs = body.get("messages", [])

    # --- 1. 원문 수선 ---------------------------------------------------
    fixed, leaked = [], []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            before = c
            c = repair(c)
            if c != before:
                STATS["ligature_fixes"] += 1
            lk = find_math_leaks(c)
            if lk:
                leaked.extend(lk)
            c = LANG_RE.sub("Korean (한국어)", c)
            m = {**m, "content": c}
        fixed.append(m)

    if leaked:
        STATS["math_leaks"] += 1
        log_jsonl("math_leaks.jsonl",
                  {"ts": time.time(), "glyphs": sorted(set(leaked))})

    # JSON 배열이 들어 있는 사용자 메시지를 찾는다. 재시도 때 그 자리의 배열만
    # 갈아끼우려면 위치를 알아야 한다.
    arr_idx, items, a0, a1 = -1, None, -1, -1
    for i, m in enumerate(fixed):
        if m.get("role") != "user" or not isinstance(m.get("content"), str):
            continue
        cand, s, e = extract_array(m["content"])
        if cand is not None:
            arr_idx, items, a0, a1 = i, cand, s, e
    # 깨진 합자가 자리표시자로 둔갑해 도착한다(`di{v1}erent`). 원본에서 만든
    # 사전으로 되돌려 자리표시자 자체를 없앤다. 모델도 엔진도 그걸 다룰 일이
    # 없어진다. 사전에 없는 조합(진짜 수식)은 건드리지 않는다.
    if GLYPHMAP and arr_idx >= 0 and items:
        fixed_items, n_dis = [], 0
        for it in items:
            src2, n = glyphmap.dissolve(it.get("input", ""), GLYPHMAP)
            n_dis += n
            fixed_items.append({**it, "input": src2} if n else it)
        if n_dis:
            items = fixed_items
            STATS["ligature_dissolved"] += n_dis
            m = fixed[arr_idx]
            fixed[arr_idx] = {**m, "content": swap_array(m["content"], a0, a1, items)}
            a1 = a0 + len(json.dumps(items, ensure_ascii=False, indent=1))

    user = "\n".join(m.get("content", "") for m in fixed if m.get("role") == "user")
    if items is None:
        items = []
    STATS["items"] += len(items)

    if _sampled < SAMPLE_N:
        _sampled += 1
        log_jsonl("request_samples.jsonl", {"n": _sampled, "messages": fixed})

    # --- 2. 캐시 (실제 번역 대상 항목만으로 키를 만든다) -------------------
    # --- 2. 캐시 (문단 단위) ---------------------------------------------
    #
    # 예전에는 배치 전체를 한 키로 해시했다. 그런데 번역 엔진은 실행마다
    # 문단을 다르게 묶고 id 를 다시 매긴다. 그래서 같은 문장이라도 묶음이
    # 조금만 달라지면 전부 빗나갔다 — 500쪽 작업에서 실측 적중률 2.18%.
    # 게다가 같은 문장이 실행마다 다르게 번역되어 용어가 흔들렸다.
    #
    # 이제 **문단 하나가 캐시 한 줄**이다. id 는 키에 넣지 않는다.
    mode = "concise" if MODE["concise"] else "normal"

    # 용어집·추가 프롬프트는 USER_RULES 로 들어와 RULES 에 이미 녹아 있다.
    def item_key(src_text: str) -> str:
        return hashlib.sha256(
            (MODEL + "\x00" + mode + "\x00" + CACHE_EPOCH + "\x00" + RULES
             + "\x00" + src_text).encode()).hexdigest()

    ckey = item_key(user)
    results: dict[str, str] = {}
    if items:
        for it in items:
            hit = cache_get(item_key(it.get("input", "")))
            if hit is not None:
                results[str(it.get("id"))] = hit
                STATS["cache_hits"] += 1
        if len(results) == len(items):      # 전부 캐시에 있었다
            return JSONResponse(_shape(json.dumps(
                [{"id": it.get("id"), "output": results[str(it.get("id"))]}
                 for it in items], ensure_ascii=False)))
    else:
        hit = cache_get(ckey)
        if hit is not None:
            STATS["cache_hits"] += 1
            return JSONResponse(_shape(hit))

    # --- 3. system 은 반드시 하나 ----------------------------------------
    # Hy-MT2 템플릿은 system 슬롯이 하나뿐이다
    # (<|startoftext|>{system}<|extra_4|>{user}<|extra_0|>).
    extra = [m["content"] for m in fixed
             if m.get("role") == "system" and m.get("content")]
    prefix = SYSTEM_PREFIX + (CONCISE_RULE if MODE["concise"] else "")
    system = "\n\n".join([prefix] + extra)
    out_msgs = [{"role": "system", "content": system}] + [
        m for m in fixed if m.get("role") != "system"
    ]

    # --- 4. 번역 -----------------------------------------------------------
    async def call(msgs, temp, max_tok):
        payload = {"model": MODEL, "messages": msgs, "temperature": temp,
                   "top_p": 0.6, "top_k": 20, "repetition_penalty": 1.05,
                   "max_tokens": max_tok, "stream": False}
        r = await client.post(f"{UPSTREAM}/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""

    # 용어집 표를 지운다. 표가 "반드시 이 역어를 쓰라"고 못박으면 모델이
    # 그 낱말을 앞으로 끌어오면서 **자리표시자를 흡수하는** 일이 있다. 검증에서
    # 실측으로 잡혔고(용어집 있음 0/3, 없음 3/3), 그 결과 통짜 번역이 세 번
    # 다 실패해 조각 모드로 떨어졌다 — 조각 모드는 어순을 굳혀 훨씬 나쁜
    # 한국어를 만든다. 마지막 한 번은 용어집 없이 물어본다. 실패한 뒤에만
    # 도는 길이라 정상 경로에는 영향이 없다.
    _GLOSS_RE = re.compile(r"\n#+\s*Glossary.*?(?=\n#+\s|\Z)", re.S | re.I)

    def msgs_for(subset, hint=None, drop_glossary=False):
        """배열 자리에 subset 만 끼운 메시지 목록."""
        out = []
        for i, m in enumerate(fixed):
            if i == arr_idx:
                c = swap_array(m["content"], a0, a1, subset)
                if drop_glossary:
                    c = _GLOSS_RE.sub("\n", c)
                out.append({**m, "content": c})
            elif m.get("role") != "system":
                out.append(m)
        # prefix 를 써야 한다. SYSTEM_PREFIX 를 직접 쓰면 간결 모드 지시가
        # 모델에 도달하지 않는다 — 실제 번역은 전부 이 경로를 지나가는데,
        # 캐시 키에는 mode 가 들어가 있어서 캐시만 두 배로 불리고 결과는
        # 똑같은 상태였다.
        sysmsg = "\n\n".join(
            [prefix] + [m["content"] for m in fixed
                        if m.get("role") == "system" and m.get("content")])
        out = [{"role": "system", "content": sysmsg}] + out
        if hint:
            out.append({"role": "user", "content": hint})
        return out

    max_tok = body.get("max_tokens", 4096)
    temps = [0.2, 0.1, 0.0]

    if not items:  # JSON 프로토콜이 아닌 요청(탐침 등)은 그대로 통과
        try:
            raw = await call(out_msgs, temps[0], max_tok)
        except Exception as e:
            return JSONResponse({"error": {"message": repr(e)}}, status_code=502)
        cache_put(ckey, user, raw, 1)
        return JSONResponse(_shape(raw))

    async def fragment_pass(it, drop_glossary=False) -> tuple[str | None, int]:
        """자리표시자 사이의 본문만 번역해 되끼운다. → (결과, 갈아낀 조각 수)

        `drop_glossary` 는 (c) 사후 구제에서만 켠다. 거기는 통짜가 세 번 다
        실패한 뒤라 "마지막 한 번은 용어집 없이"가 적용되어야 하는 자리다.
        (a) 사전 조각은 그 문단의 **첫** 시도이므로 용어집을 유지한다.

        실패하면 (None, 0). 아래 (a) 사전 조각과 (c) 사후 구제가 **반드시**
        같은 코드를 지나가야 한다. 예전에는 검증이 (c) 에만 있어서 (a) 로
        들어온 영어 반향이 성공으로 반환됐고, 더 나쁘게는 그대로 캐시에
        박혔다 — 정상 모델로 다시 물어도 캐시가 영어를 돌려줬다.
        """
        runs, phs = split_runs(it.get("input", ""))
        idx = [k for k, r in enumerate(runs) if is_translatable(r)]
        if not idx:
            return it.get("input", ""), 0      # 자리표시자뿐 — 번역할 게 없다
        sub = [{"id": k, "input": runs[k], "layout_label": "fragment"} for k in idx]
        try:
            raw = await call(msgs_for(sub, drop_glossary=drop_glossary),
                             0.1, max_tok)
            got = {str(o.get("id")): item_output(o)
                   for o in (parse_output(raw) or []) if isinstance(o, dict)}
        except Exception:
            return None, 0
        def usable(v, src_run) -> bool:
            if not (isinstance(v, str) and v.strip()):
                return False
            if KEEP_RE.search(v) or _STYLE_NEAR.search(v):
                return False
            # 라틴 문자가 8자 넘는 조각인데 한글이 거의 없으면 번역이 아니다.
            if sum(1 for c in src_run if c.isalpha() and c.isascii()) >= 8 \
                    and hangul_ratio(v) < 0.15:
                return False
            return leftover_english(v) is None

        new_runs, hit, bad = list(runs), 0, []
        for k in idx:
            v = got.get(str(k))
            if usable(v, runs[k]):
                new_runs[k] = v
                hit += 1
            else:
                bad.append(k)

        # 못 갈아낀 조각은 **원문 영어가 그대로 남는다**. 조용히 두면 한국어
        # 문장 한복판에 영어 한 문장이 박힌 채로 출고된다 — 실측으로 이 책
        # 55쪽·58쪽이 그랬고, 통짜 경로에 잔류 검사를 넣은 뒤에도 조각 경로로
        # 새어 나왔다. 조각은 문장 중간에서 잘려 있는 일이 많아서(`decreases
        # as the number of…`) 모델이 한 번에 놓치기 쉽다. 짚어서 다시 묻는다.
        if bad:
            sub2 = [{"id": k, "input": runs[k], "layout_label": "fragment"}
                    for k in bad]
            hint2 = ("These are FRAGMENTS cut out of a sentence — they may "
                     "start or end mid-clause. Translate each into Korean as "
                     "the fragment it is. Do not complete the sentence, do not "
                     "add words, and do not leave any of them in English.")
            try:
                raw2 = await call(msgs_for(sub2, hint2,
                                           drop_glossary=drop_glossary),
                                  0.0, max_tok)
                got2 = {str(o.get("id")): item_output(o)
                        for o in (parse_output(raw2) or []) if isinstance(o, dict)}
            except Exception:
                got2 = {}
            for k in bad:
                v = got2.get(str(k))
                if usable(v, runs[k]):
                    new_runs[k] = v
                    hit += 1
                    STATS["fragment_retried"] += 1
        if not hit:
            return None, 0        # 한 조각도 못 갈았다 — 캐시에 넣어선 안 된다
        rebuilt = "".join(new_runs[k] + (phs[k] if k < len(phs) else "")
                          for k in range(len(new_runs)))
        # 조각별 검사만으로는 못 막는다. 자리표시자가 촘촘한 문단은 조각이
        # 죄다 짧아서(`column `, ` shows `) 라틴 8자 기준에 걸리지 않고
        # 전부 통과한다 — 실측으로 영어 반향이 그대로 성공 처리됐다.
        # 다시 짜맞춘 **결과 전체**를 원문과 견줘 한 번 더 본다.
        src_txt = it.get("input", "")
        if sum(1 for c in src_txt if c.isalpha() and c.isascii()) >= 8 \
                and hangul_ratio(rebuilt) < 0.15:
            return None, 0
        # 길이도 봐야 한다. 조각 모드는 자리표시자와 한글만 검사하고 폭은
        # 보지 않았다 — 실측으로 원문의 **9.41배**짜리 문단이 그대로 나갔고,
        # 사전 조각 경로(a)는 그것을 캐시에까지 넣었다. 같은 문단을 통짜
        # 경로에 넣으면 `width 9.41x` 로 거부되는 바로 그 텍스트다.
        # 길이 폭주는 페이지를 깨뜨리는 가장 흔한 원인이다.
        sw = est_width(src_txt)
        if sw >= 10 and est_width(rebuilt) / sw > WIDTH_MAX:
            return None, 0
        return rebuilt, hit

    # (a) 자리표시자가 아주 많은 문단은 **조각내서** 보낸다.
    #     `{v1}` 사이의 본문만 번역하고 자리표시자는 우리가 원위치에 다시 끼운다.
    #     모델이 자리표시자를 볼 일이 없으니 잃어버릴 수가 없다.
    #     대신 어순이 조각 단위로 고정되므로, 통짜 번역이 계속 실패하는
    #     기호 정리표 같은 극단적 문단에만 쓴다.
    heavy = [it for it in items
             if str(it.get("id")) not in results        # 캐시에 있으면 건너뛴다
             and len(PLACEHOLDER_RE.findall(it.get("input", ""))) > PH_HEAVY]
    for it in heavy:
        rebuilt, hit = await fragment_pass(it)
        if rebuilt is None:
            continue          # 통짜 경로 (b) 로 넘긴다. 캐시에도 넣지 않는다.
        results[str(it.get("id"))] = rebuilt
        if hit:
            cache_put(item_key(it.get("input", "")), it.get("input", ""), rebuilt, 1)
            STATS["fragment_mode"] += 1
            log_jsonl("fragments.jsonl", {"ts": time.time(), "why": "heavy",
                                          "src": it.get("input", "")[:300]})

    # (b) 나머지는 통짜로 보내되, **실패한 항목만** 다시 보낸다.
    #     배치 10개 중 1개가 틀렸다고 10개를 재번역하면 GPU가 재작업만 한다.
    pending = [it for it in items if str(it.get("id")) not in results]
    hint, why, last = None, "", ""
    for attempt, temp in enumerate(temps, 1):
        if not pending:
            break
        try:
            # 마지막 시도는 용어집 없이. 용어집이 자리표시자 유실의 원인일 때
            # 힌트만 더 붙여 봐야 소용이 없다 — 표가 그대로 남아 있기 때문이다.
            raw = await call(msgs_for(pending, hint,
                                      drop_glossary=(attempt == len(temps))),
                             temp, max_tok)
        except Exception as e:
            log_jsonl("errors.jsonl", {"ts": time.time(), "err": repr(e)})
            if attempt == len(temps):
                return JSONResponse({"error": {"message": repr(e)}}, status_code=502)
            continue
        last = raw
        parsed = parse_output(raw) or []
        by_id = {str(o.get("id")): o for o in parsed if isinstance(o, dict)}
        still = []
        for it in pending:
            iid = str(it.get("id"))
            cand = item_output(by_id.get(iid)) or None
            ok, why = ((False, Reason("empty", f"id {iid} empty output"))
                       if not isinstance(cand, str)
                       else check([it], [{"id": it.get("id"), "output": cand}]))
            if ok:
                results[iid] = cand
                # 원문과 글자 하나 안 다르면 번역이 아니다. `check` 는 라틴
                # 문자 12자 미만이면 한글을 요구하지 않으므로 `Chapter Six`
                # 같은 제목·표 칸이 반향 그대로 통과한다. 결과로 쓰는 건
                # 어쩔 수 없지만 **캐시에 박으면 영영 다시 시도하지 않는다.**
                if cand.strip() != (it.get("input") or "").strip():
                    cache_put(item_key(it.get("input", "")),
                              it.get("input", ""), cand, attempt)
            else:
                still.append((it, why))
        if not still:
            pending = []      # 비우지 않으면 아래 조각 구제가 성공 항목까지 덮어쓴다
            break
        STATS["retries"] += 1
        why = f"{len(still)}/{len(pending)} items failed"
        log_jsonl("retries.jsonl", {"ts": time.time(), "attempt": attempt,
                                    "why": why, "raw": raw[:400]})
        hint = repair_hint(still, parsed)
        pending = [it for it, _ in still]

    # (c) 통짜 번역이 3번 다 실패한 항목은 **영어로 버리기 전에 조각 모드**를 태운다.
    #     자리표시자 사이의 본문만 번역하고 자리표시자는 우리가 원위치에 끼우므로
    #     구조적으로 실패할 수 없다. 어순이 조각 단위로 굳는 대가를 치르지만,
    #     그 문단이 영어로 남는 것보다는 낫다.
    if pending:
        rescued_frag = []
        for it in pending:
            # 통짜가 세 번 다 실패한 뒤다. 용어집 표가 원인일 수 있으므로
            # 마지막 이 한 번은 표 없이 물어본다 — 위 (b) 의 마지막 시도와
            # 같은 이유이고, 실제로 상류가 보는 **맨 마지막** 요청이 여기다.
            rebuilt, hit = await fragment_pass(it, drop_glossary=True)
            if rebuilt is None or not hit:
                continue
            # 여기서는 캐시에 넣지 않는다. 조각 모드는 어순이 조각 단위로
            # 굳으므로 통짜 번역보다 질이 낮다. 통짜가 3번 실패한 이번 판에서만
            # 쓰고, 다음 실행에서는 다시 통짜부터 시도할 기회를 남긴다.
            results[str(it.get("id"))] = rebuilt
            rescued_frag.append(it)
            STATS["fragment_mode"] += 1
            # 어느 문단이 이렇게 됐는지 남긴다. 조각 모드는 어순을 조각
            # 단위로 굳혀서, 수식이 그 수식을 설명하는 구절에서 떨어져 나간다.
            # 세기만 하고 알리지 않으면 보고서가 "파손 0쪽"이라고 말하는
            # 동안 사용자는 읽기 어려운 문단을 만나게 된다.
            log_jsonl("fragments.jsonl", {"ts": time.time(), "why": "rescue",
                                          "src": it.get("input", "")[:300]})
        pending = [it for it in pending if it not in rescued_frag]

    # 조각 모드로도 안 되면 그때만 원문으로 둔다.
    if pending:
        STATS["failures"] += 1
        STATS["items_failed"] += len(pending)
        log_jsonl("failures.jsonl",
                  {"ts": time.time(), "why": why or "unknown", "raw": last[:600],
                   "ids": [i.get("id") for i in pending], "total": len(items)})
        for it in pending:
            results[str(it.get("id"))] = it.get("input", "")
    STATS["items_rescued"] += len(items) - len(pending)

    out_items = [{"id": it.get("id"), "output": results.get(str(it.get("id")),
                                                            it.get("input", ""))}
                 for it in items]
    text = json.dumps(out_items, ensure_ascii=False)
    # 문단별로 이미 저장했으므로 배치 단위 저장은 하지 않는다.
    # 실패한 문단은 저장하지 않아, 재실행하면 그것만 다시 시도된다.
    return JSONResponse(_shape(text))


def _shape(content: str) -> dict:
    return {
        "id": "chatcmpl-proxy", "object": "chat.completion",
        "created": int(time.time()), "model": MODEL,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# 모든 함수 정의가 끝난 뒤에 계산한다.
RULES = _rules_fingerprint()
