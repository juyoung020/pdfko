"""번역 결과가 모이는 곳을 한 군데로 정한다.

예전에는 세 군데로 흩어져 있었다.

  · CLI  → `Path.cwd() / "<이름>_ko"`   — **명령을 친 폴더**
  · 웹   → `~/pdfko-작업/<이름>`
  · 사람 → 원본 PDF 옆에 있으려니 했다

CLI 쪽이 특히 나빴다. 저장소 안에서 번역을 돌리면 결과가 저장소에 쌓이는데,
`.gitignore` 는 `cache/ logs/ work/ parts/` 만 막는다. 정작 번역본·품질
보고서·용어집은 안 막혀서 커밋 대기 목록에 끼어들었다. public 저장소에서
`git add .` 한 번이면 남의 교재가 올라간다.

이제 전부 `<저장소>/out/` 하나로 모은다. `out/` 은 이미 `.gitignore` 에
있어서 새로 막을 것도 없다.
"""

from __future__ import annotations

import os
from pathlib import Path


def _base_for(pkg_dir: Path, home: Path, env: str | None) -> Path:
    """`out_base` 의 순수 함수 알맹이. 시험이 가짜 경로를 넣어 볼 수 있게 뺐다."""
    if env:
        return Path(env).expanduser()
    root = pkg_dir.parent
    # `pyproject.toml` 이 옆에 있으면 소스 체크아웃이다(`uv pip install -e .`).
    # 없으면 site-packages 에 설치된 사본이다 — 거기에 결과를 쓰면 권한
    # 오류가 나거나, 나더라도 재설치 때 통째로 날아간다.
    if (root / "pyproject.toml").is_file():
        return root / "out"
    return home / "pdfko" / "out"


def out_base() -> Path:
    """번역 결과가 모이는 폴더. 어디서 명령을 치든 항상 같은 곳이다."""
    return _base_for(Path(__file__).resolve().parent, Path.home(),
                     os.environ.get("PDFKO_OUT"))


def work_for(src: Path) -> Path:
    """이 원본에 대한 작업 폴더."""
    return out_base() / f"{src.stem}_ko"
