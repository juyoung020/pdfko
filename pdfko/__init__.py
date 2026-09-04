"""pdfko — 영문 교재·논문의 한국어 번역기 (레이아웃 유지)."""
__version__ = "0.1.0"


def use_safe_output() -> None:
    """출력 인코딩 때문에 번역이 죽는 일을 막는다.

    한국어 윈도우의 기본 인코딩은 cp949 인데 그 표에 `—`(U+2014)가 없다.
    이 코드에는 안내 문구에 `—` 가 27줄 들어 있고, 그중 하나만 찍혀도
    UnicodeEncodeError 로 **번역 전체가 죽는다.** 실측으로 1283쪽 교재의
    사전 점검 단계, 진행률 0% 에서 그렇게 죽었다. 세 시간짜리 작업이
    안내 한 줄 때문에 시작도 못 한 것이다.

    콘솔에 붙어 있을 때는 파이썬이 이미 유니코드로 내보내므로 건드리지
    않는다. 리다이렉트된 출력만 UTF-8 로 바꾼다 — 그래야 로그 파일에
    `—` 가 `?` 로 뭉개지지 않는다. 어느 쪽이든 `errors="replace"` 를
    걸어, 앞으로 어떤 글자가 들어와도 출력이 작업을 죽이지 못하게 한다.

    바꿀 수 없는 스트림이면 그냥 둔다. 여기서 죽으면 본말이 전도된다.
    """
    import sys
    for st in (sys.stdout, sys.stderr):
        try:
            if st is not None and not st.isatty():
                st.reconfigure(encoding="utf-8", errors="replace")
            elif st is not None:
                st.reconfigure(errors="replace")
        except Exception:
            pass
