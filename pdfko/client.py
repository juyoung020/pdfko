"""미들웨어 프록시에 번역을 요청하는 클라이언트.

PDF 경로는 BabelDOC 이 프록시를 호출하지만, PPTX 는 우리가 직접 호출한다.
프록시를 거치는 이유는 같다 — 검증·재시도·캐시·용어집이 전부 거기 있다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# 프록시는 BabelDOC 이 쓰는 것과 같은 JSON 배열 프로토콜을 기대한다.
_PROMPT = """You are a professional Korean (한국어) native translator.

Translate each item into Korean. Keep the meaning, keep it concise.

## Output Format
Return a JSON array of the same length. Keep the same "id", remove "input",
add "output" with the translated text only. No extra text, no ``` fences.

"""


def translate_batch(items: list[dict], *, port: int = 8100,
                    model: str = "hy-mt2-7b", timeout: int = 900) -> dict[int, str]:
    """[{"id": n, "input": "..."}] → {n: "번역문"}

    실패한 항목은 결과에서 빠진다(원문 유지를 호출부가 결정하게 한다).
    """
    if not items:
        return {}
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": _PROMPT + json.dumps(items, ensure_ascii=False,
                                                      indent=1)}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = json.load(r)["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            return {}
    # 비정상 응답은 네 가지로 터진다: 본문이 JSON 이 아님(JSONDecodeError),
    # choices 가 빈 배열(IndexError), content 가 null(TypeError), 소켓 오류(OSError).
    # 하나라도 새어 나가면 덱 전체가 중간에 죽고 여태 번역한 것을 다 잃는다.
    # 어떤 예외도 새어 나가면 안 된다. PPTX 경로는 중간 저장이 없어서
    # 한 번 터지면 여태 번역한 것을 전부 잃는다. 실측으로 UnicodeDecodeError
    # (비UTF8 본문)와 IncompleteRead(잘린 응답)가 기존 목록을 빠져나갔다.
    except Exception:
        return {}
    try:
        arr = json.loads(content)
    # JSONDecodeError 만 잡으면 안 된다. 5만 겹 중첩된 배열은
    # RecursionError 로 터지고, 그건 이 except 를 빠져나가 덱 전체를 죽인다.
    except Exception:
        return {}
    out: dict[int, str] = {}
    for o in arr if isinstance(arr, list) else []:
        if isinstance(o, dict) and "id" in o and isinstance(o.get("output"), str):
            try:
                out[int(o["id"])] = o["output"]
            except (TypeError, ValueError):
                pass
    return out


def health(port: int = 8100) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        return True
    except Exception:
        return False


def set_concise(on: bool, port: int = 8100) -> None:
    """간결 모드 전환. 상자에 안 들어갈 때 다시 시도하는 용도."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mode",
            data=json.dumps({"concise": on}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
