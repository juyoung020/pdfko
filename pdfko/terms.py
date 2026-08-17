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

# 자리표시자·태그. 역어에 들어가면 없던 수식을 문단에 심게 된다.
KEEP_RE = re.compile(r"\{v\d+\}|</?style[^>]*>")

# **영어 문법의 닫힌 낱말 부류만** 넣는다 — 관사·전치사·대명사·접속사·조동사.
# 어느 분야에서도 전문 용어가 될 수 없는 낱말들이고, 이건 분야에 대한 지식이
# 아니라 영어라는 언어에 대한 사실이다.
#
# 내용어는 **한 개도** 넣지 않는다. 한때 잡음을 줄이려고 `learning`, `method`,
# `system` 을 넣었다가 곧바로 이 도구를 강화학습 전용으로 만들 뻔했다.
# 머신러닝 교재에서 `learning` 은 막아야 할 잡음이 아니라 가장 중요한 용어다.
# 분야를 모르는 도구가 어떤 낱말이 그 분야의 용어인지 미리 정할 수는 없다.
#
# 내용어를 거르는 일은 목록이 아니라 **모델**이 한다(`keep_terms`).
STOP = set("""
the a an and or but if then than that this these those of in on at to for with from by as
is are was were be been being am it its they them their we us our you your he him his she her
not no nor can could may might will would shall should must do does did done have has had
there here when where which who whom whose what how why all any both each either neither
few more most other others some such only own same so too very about above below under
over between into onto through during before after since until while although though because
however therefore hence thus also again yet than upon within without across among per via
another every""".split())
# 여기서 뺀 것들과 그 이유:
#   one two three four five, first second third — 숫자·서수는 닫힌 부류가 아니고,
#       다섯에서 자른 것은 문법이 아니라 빈도 맞춤이다. 실제로 `second messenger`
#       (세포생물학), `first order`(수학·약동학), `third party`(법학), `one hot`(ML),
#       `two sample`(통계)을 막고 있었다. 강화학습만 아무 손해가 없었다.
#   next last — 형용사다. `next generation`(유전체학), `last mile`(통신)을 막는다.
#   even still just — 부사·형용사. `even function`(수학), `still image`(영상),
#       `just cause`(법학)를 막는다.
# 흔한 낱말을 거르는 일은 목록이 아니라 모델(`keep_terms`)이 한다.

_WORD = re.compile(r"[^A-Za-z\- ]+")


def _norm(t: str, vocab: set[str] | None = None) -> str:
    """복수형을 단수로 접는다. `states` 와 `state` 를 따로 세면 안 된다.

    **예외 목록을 두지 않는다.** `-s` 로 끝나는 단수 명사는 라틴·그리스계에
    많고(`bias`, `analysis`, `virus`, `axis`, `species`, `physics`, `lens`),
    그걸 목록으로 막으려 들면 끝이 없다. 빠뜨리면 `bias→bia`, `physics→physic`
    이 되어 문서 어디에도 없는 문자열이 용어집에 실린다. 그 피해는 생물학·
    물리학·통계학에만 가고 강화학습은 멀쩡하다 — 분야에 따라 결과가 달라지는
    바로 그 상태다.

    그래서 **문서 자신에게 묻는다.** 단수형이 이 문서에 실제로 등장할 때만
    접는다. `states` 와 `state` 가 함께 나오면 접고, `physics` 만 있고
    `physic` 이 없으면 그대로 둔다. 어휘 목록이 필요 없고 어느 분야에서나
    같은 방식으로 동작한다.
    """
    if t.endswith("ies") and len(t) > 4 and (vocab is None or t[:-3] + "y" in vocab):
        return t[:-3] + "y"
    if t.endswith("sses") or t.endswith("shes"):
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        base = t[:-1]
        if vocab is None or base in vocab:
            return base
    return t


#: 정규화된 용어 → 문서에 실제로 찍혀 있는 표기들. `extract` 가 채운다.
SURFACES: dict[str, set[str]] = {}

_EDGE = " \t\r\n.,;:!?()[]{}\"'“”‘’"


def extract(pdf: str | Path, first: int = 1, last: int | None = None,
            min_count: int = 5, top: int = 60) -> list[tuple[int, str]]:
    """(출현횟수, 용어) 목록을 빈도순으로. 분야 어휘 목록을 쓰지 않는다.

    정규화된 용어와 함께 **문서에 실제로 찍혀 있는 표기**를 `SURFACES` 에
    모은다. 이게 없으면 용어집이 무용지물이 된다 — 번역 엔진은 용어집의
    원어를 원문 텍스트에 **그대로** 대조하는데, 우리가 넣는 것은 합자를
    복구하고 기호를 걷어낸 *깨끗한* 철자이기 때문이다.

        용어집에 넣은 것   off-policy            원문에 0회
        원문에 있는 것     o↵-policy             74회
        용어집에 넣은 것   state-action          원문에 0회
        원문에 있는 것     state–action (엔대시)  83회

    실측으로 그 책에서 가장 특징적인 용어 440여 회가 통째로 빗나갔다.
    그러면서 화면에는 "54개 용어의 역어를 고정했다"라고 찍혔다.
    """
    import pymupdf

    words: list[str] = []
    raws: list[str] = []
    italic: set[str] = set()
    SURFACES.clear()
    with pymupdf.open(pdf) as d:
        end = min(last or d.page_count, d.page_count)
        for i in range(max(0, first - 1), end):
            pg = d[i]
            # 낱말 단위로 훑는다. 페이지 전체를 한 번에 씻으면 정규화된 철자와
            # 원래 표기의 짝이 끊어져 위 문제를 고칠 수 없다.
            for raw in pg.get_text().split():
                raw = raw.strip(_EDGE)
                if not raw:
                    continue
                # 합자 손상을 먼저 고친다. 안 그러면 `arti cial`(artificial)과
                # `erent`(different)가 상위 후보로 올라온다. 실측으로 그랬다.
                clean = _WORD.sub(" ", repair(raw)).lower().strip()
                if not clean or " " in clean:
                    clean = clean.replace(" ", "")      # `di erent` → `dierent`
                if not clean:
                    continue
                words.append(clean)
                raws.append(raw)
            for b in pg.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    for s in ln.get("spans", []):
                        f = s.get("font", "")
                        # 수학 폰트(CMMI)는 변수라 이탤릭이어도 용어가 아니다
                        if ("TI" in f or "Italic" in f) and "CMMI" not in f:
                            italic.update(
                                _WORD.sub(" ", repair(s.get("text", ""))).lower().split())

    # 단복수 접기의 판단 근거는 **이 문서의 어휘**다. 예외 목록이 아니다.
    vocab = set(words)
    nm = lambda w: _norm(w, vocab)                        # noqa: E731
    ok = lambda w: len(w) > 2 and w not in STOP           # noqa: E731
    bi, hyp, uni = Counter(), Counter(), Counter()
    for j, w in enumerate(words):
        raw = raws[j]
        if j + 1 < len(words) and ok(w) and ok(words[j + 1]) \
                and w.isalpha() and words[j + 1].isalpha():
            key = f"{nm(w)} {nm(words[j + 1])}"
            bi[key] += 1
            SURFACES.setdefault(key, set()).add(f"{raw} {raws[j + 1]}")
        if "-" in w and 6 < len(w) < 30 and w.replace("-", "").isalpha():
            hyp[nm(w)] += 1
            SURFACES.setdefault(nm(w), set()).add(raw)
        if len(w) > 4 and w not in STOP and w.isalpha():
            uni[nm(w)] += 1
            SURFACES.setdefault(nm(w), set()).add(raw)

    # **구를 단일어보다 앞에 둔다.** 어휘 목록으로 거르는 대신 순서로 푼다.
    # 빈도만으로 줄을 세우면 `after`, `better` 같은 흔한 낱말이 `value function`
    # 앞에 온다. 그렇다고 그런 낱말을 목록에 박으면 분야 도구가 된다 —
    # 한때 `learning` 을 막아 놨는데, 머신러닝 교재에서는 그게 핵심 용어다.
    #
    # 두 낱말 구와 하이픈 낱말은 전문 용어일 확률이 훨씬 높다. 이건 분야가
    # 아니라 **형태**에 대한 사실이라 어느 문서에나 통한다.
    strong = [(n, t) for t, n in bi.most_common(top * 2) if n >= min_count]
    strong += [(n, t) for t, n in hyp.most_common(top) if n >= min_count]
    strong.sort(key=lambda x: -x[0])
    # 구에 이미 들어 있는 낱말은 단일어로 또 넣지 않는다
    inside = {w for _, p in strong for w in p.replace("-", " ").split()}
    ital_n = {nm(x) for x in italic}
    weak = sorted(((n, t) for t, n in uni.most_common(top * 4)
                   if n >= min_count and nm(t) in ital_n
                   and t not in inside), key=lambda x: -x[0])
    cand = strong + weak

    seen, out = set(), []
    for n, t in cand:
        t = t.strip().strip("-").strip()
        # 앞뒤 하이픈은 그리스 문자가 떨어져 나간 흔적이다(`ε-greedy` → `-greedy`).
        # 텍스트 레이어가 깨진 PDF 에서 흔하다.
        if not t or t in seen or len(t) < 3:
            continue
        seen.add(t)
        out.append((n, t))
    return out[:top]


def keep_terms(rows: list[tuple[int, str]], *, port: int, model: str,
               batch: int = 12) -> list[tuple[int, str]]:
    """후보 중 **전문 용어만** 모델에게 고르게 한다.

    통계는 "자주 나오는 말"까지만 알려 준다. 그중 무엇이 그 분야의 용어인지는
    분야를 아는 쪽이 판단해야 하고, 그건 낱말 목록이 아니라 모델이다.
    목록으로 거르면 그 순간 분야 전용 도구가 된다.

    모델이 답을 못 하면 후보를 그대로 쓴다. 잡음이 섞여도 역어가 하나로
    고정될 뿐이라 해롭지 않다 — 진짜 용어를 잃는 쪽이 훨씬 비싸다.

    `port` 는 **추론 서버(ollama)** 다. 미들웨어 프록시로 보내면 안 된다 —
    프록시는 모든 요청을 번역으로 보고 번역가 지시문을 붙이므로, 모델이
    "용어를 골라라"가 아니라 "이 목록을 한국어로 옮겨라"로 받아들인다.
    실측으로 프록시를 거치니 잡음 9개 중 0개가 걸러졌다.
    """
    import json
    import urllib.request

    kept: list[tuple[int, str]] = []
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        # **항목별로** 묻는다. "골라서 배열로 돌려달라"고 하면 어떤 분야에서는
        # 모델이 통째로 `[]` 를 뱉는다 — 실측으로 헌법학 용어 12개(judicial
        # review, due process …)에 5회 연속 빈 배열이었고, 같은 형식으로 물리
        # 용어 12개는 12개 전부 살렸다. 그러면 아래 폴백이 잡음까지 통과시켜
        # 법학 교재만 용어집이 오염된다.
        #
        # 항목마다 참/거짓을 답하게 하면 같은 모델이 같은 목록을 정확히
        # 분류한다. 고를 것을 고르는 것보다 하나씩 판정하는 쪽이 쉽다.
        prompt = (
            "For each phrase below, answer whether it is a domain-specific "
            "technical term (true) or an ordinary English phrase, a bibliography "
            "artifact, or a misspelled/truncated fragment (false).\n"
            "Answer with a JSON object mapping every phrase to true or false. "
            "Include every phrase. No explanation.\n\n"
            + json.dumps([t for _, t in chunk], ensure_ascii=False))
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=json.dumps({"model": model, "temperature": 0.0,
                                 "max_tokens": 800,
                                 "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = json.load(r)["choices"][0]["message"]["content"]
            s, e = raw.find("{"), raw.rfind("}")
            verdict = json.loads(raw[s:e + 1])
            if not isinstance(verdict, dict) or not verdict:
                raise ValueError("empty verdict")
            # 답이 없는 항목은 살린다. 모델이 빠뜨렸다고 용어를 잃으면 안 된다.
            # `v is not False` 만 보면 `"false"`(문자열)와 `0` 이 통과한다.
            no = (False, 0, "false", "False", "no", "No", "0")
            picked = {str(k).strip().lower()
                      for k, v in verdict.items() if v not in no}
            picked |= {t for _, t in chunk if t not in
                       {str(k).strip().lower() for k in verdict}}
        except Exception:
            kept += chunk            # 판단하지 못하면 통째로 살린다
            continue
        # **원문과 정확히 일치하는 것만** 남긴다. 손상된 항목에 대해 모델은
        # 버리는 대신 고쳐서 돌려주는 일이 있는데(`nite-horizon` → `finite
        # horizon`), 그 교정본은 실제 문서에 없는 문자열이라 용어집에 넣으면
        # 아무 데도 걸리지 않는다. 일치하지 않으면 자동으로 빠지는 셈이라
        # 손상 항목 제거가 공짜로 따라온다 — 실측 5개 중 5개.
        got = [(n, t) for n, t in chunk if t in picked]
        kept += got if got else chunk
    return kept


def _translate_terms(items: list[str], port: int, model: str) -> dict[int, str]:
    """용어를 추론 서버에 **직접** 물어 번역한다. {인덱스: 한국어}

    프록시를 거치지 않는다. 용어집은 프록시를 띄우기 **전에** 만들어져야
    하기 때문이다 — 용어집 지문은 프록시 기동 시점에 자식에게 넘어가고,
    그 뒤에 바꾸면 이미 뜬 프록시에는 반영되지 않는다. 반영되지 않으면
    용어집이 캐시 키에서 빠지고, 다시 돌렸을 때 옛 번역이 나온다.
    """
    import json
    import urllib.request

    prompt = ("Translate each English term into Korean. These are technical "
              "terms from a document, not sentences — answer with a short noun "
              "phrase for each.\nAnswer with a JSON object mapping every term "
              "to its Korean translation. No explanation.\n\n"
              + json.dumps(items, ensure_ascii=False))
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": model, "temperature": 0.1,
                             "max_tokens": 1200,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = json.load(r)["choices"][0]["message"]["content"]
        s, e = raw.find("{"), raw.rfind("}")
        got = json.loads(raw[s:e + 1])
    except Exception:
        return {}
    low = {str(k).strip().lower(): v for k, v in got.items()}
    return {i: low[t] for i, t in enumerate(items)
            if isinstance(low.get(t), str)}


def decide(rows: list[tuple[int, str]], *, port: int, model: str,
           batch: int = 20, via_proxy: bool = True) -> dict[str, str]:
    """뽑은 용어의 역어를 **한 번만** 정한다. {영어: 한국어}

    이게 자동 용어 통일의 핵심이다. 사람이 CSV 를 채우게 하면 원클릭이 아니다.
    도구가 먼저 정하고 문서 끝까지 그 역어를 지키면, 사람이 손대지 않아도
    `가치 함수`가 중간에 `값 함수`로 바뀌는 일이 없어진다.

    **어느 역어가 옳은지**를 도구가 판단하는 게 아니다. 무엇을 고르든 한 번만
    고르는 것이 목적이다. 취향이 다르면 `--glossary` 로 덮어쓰면 된다.
    """
    from .client import translate_batch
    from .repair import hangul_ratio

    picked: dict[str, str] = {}
    for i in range(0, len(rows), batch):
        chunk = [t for _, t in rows[i:i + batch]]
        got = (translate_batch([{"id": j, "input": t} for j, t in enumerate(chunk)],
                               port=port, model=model)
               if via_proxy else _translate_terms(chunk, port, model))
        for j, en in enumerate(chunk):
            ko = (got.get(j) or "").strip()
            # 용어 역어로 쓸 수 있는 값인지 본다. 짧은 명사구여야 하고,
            # 한글이 있어야 하며, 문장이 되어 돌아오면 안 된다.
            # `hangul_ratio` 는 글자가 하나도 없으면 1.0 을 돌려준다(번역 대상이
            # 아니라는 뜻). 그 값을 그대로 쓰면 `19`, `—`, `| 표 |`, `{v1} 노름`
            # 같은 것이 역어로 통과한다. `|` 는 엔진의 용어집 표를 깨뜨리고,
            # 자리표시자는 없던 수식을 문단에 심는다. 한글을 직접 요구한다.
            if not any("가" <= c <= "힣" for c in ko):
                continue
            if hangul_ratio(ko) < 0.5 or KEEP_RE.search(ko) or "|" in ko:
                continue
            # 개행뿐 아니라 캐리지리턴도 막는다 — 엔진의 CSV 파서가 죽는다.
            if len(ko) > max(24, len(en)) or "\n" in ko or "\r" in ko:
                continue
            if ko.endswith(("다.", "다")):
                continue
            picked[en] = ko
    return picked


def write_csv(path: Path, rows: list[tuple[int, str]],
              targets: dict[str, str] | None = None) -> None:
    """번역어 칸을 비운 채로 저장한다. 사용자가 채워 넣으면 그대로 쓸 수 있다.

    주석이나 여분 칸을 넣지 않는다. 이 파일은 그대로 `--glossary` 로 되돌아와
    번역 엔진의 CSV 파서를 통과해야 한다. 빈도는 줄 순서로 이미 드러난다.
    """
    import csv

    tg = targets or {}
    with path.open("w", newline="", encoding="utf-8") as f:
        # f-string 으로 이어 붙이면 역어에 쉼표가 하나만 들어가도 칸이 밀려
        # 항목이 **조용히 사라지고**, 개행이 들어가면 엔진이 CSV 파싱에서
        # 죽는다. 표준 writer 가 따옴표 처리를 해 준다.
        w = csv.writer(f)
        w.writerow(["source", "target", "tgt_lng"])
        for _, t in rows:
            ko = tg.get(t, "")
            if targets is not None and not ko:
                continue        # 역어를 못 정한 항목은 싣지 않는다.
                                # 빈 칸으로 실으면 모델에게 "이 용어의 역어는
                                # 없음"이라는 표를 보여 주는 셈이 된다.
            # 문서에 실제로 찍힌 표기를 모두 싣는다. 정규화한 철자 하나만
            # 넣으면 `off-policy` 는 지정하고 `o↵-policy` 74회는 놓친다.
            for surface in sorted(SURFACES.get(t, {t})) or [t]:
                w.writerow([surface, ko, ""])
