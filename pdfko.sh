#!/usr/bin/env bash
# pdfko 원샷 실행 — 딸깍 한 번으로 번역 화면까지.
#
# 필요한 걸 하나씩 점검해서 **없는 것만** 설치하고, 다 갖춰졌으면 곧장
# 번역 화면을 연다. 두 번째 실행부터는 점검만 몇 초 하고 바로 열린다.
#
#   ./pdfko.sh
#
# 각 단계는 "왜 이걸 확인하는가"가 분명해야 한다. 그냥 설치 명령을 쭉
# 나열하면 이미 깔린 사람이 5.8GB 를 또 받는다.

set -uo pipefail
# 스크립트가 놓인 폴더를 기준점으로 삼는다. 어디서 실행하든 — 바탕화면
# 아이콘이든, 다른 폴더에서 친 명령이든 — 같은 곳을 보게 하려는 것이다.
HERE="$(dirname "$(readlink -f "$0")")"
cd "$HERE" || exit 1

MODEL_TAG=hy-mt2-7b
GGUF_NAME=HY-MT2-7B-Q6_K.gguf
HF_REPO=tencent/Hy-MT2-7B-GGUF

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
work() { printf '  \033[33m→\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; hold; exit 1; }

# 딸깍으로 띄운 터미널은 스크립트가 끝나면 창째 사라진다. 오류를 읽을
# 시간을 준다.
hold() { [ -t 0 ] && { echo; read -rp "엔터를 누르면 닫힙니다..." _; }; }

bold "▶ pdfko 준비"

# ── 1. uv ────────────────────────────────────────────────────────────────
# 우분투 22.04 에는 파이썬 3.12 가 없다. uv 가 알아서 받아온다.
if command -v uv >/dev/null 2>&1; then ok "uv"
else
  work "uv 설치"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
    || die "uv 설치 실패 — 인터넷 연결을 확인해주세요"
fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
command -v uv >/dev/null 2>&1 || die "uv 를 PATH 에서 찾지 못했습니다"

# ── 2. 가상환경 + pdfko ──────────────────────────────────────────────────
if [ -x .venv/bin/pdfko ]; then ok "pdfko"
else
  work "가상환경 만들고 pdfko 설치 (1~2분)"
  uv venv --python 3.12 >/dev/null 2>&1 || die "가상환경 생성 실패"
  uv pip install -q -e . || die "pdfko 설치 실패"
fi

# ── 3. babeldoc ──────────────────────────────────────────────────────────
# 번역 엔진. pdfko 와 같은 환경에 섞으면 onnxruntime·pymupdf 가 충돌해서
# 반드시 따로 격리 설치한다(pyproject.toml 주석 참고).
if command -v babeldoc >/dev/null 2>&1; then ok "babeldoc"
else
  work "babeldoc 설치 (2~3분)"
  uv tool install -q --python 3.12 babeldoc || die "babeldoc 설치 실패"
fi

# ── 4. ollama ────────────────────────────────────────────────────────────
if command -v ollama >/dev/null 2>&1; then ok "ollama"
else
  work "ollama 설치 (sudo 암호를 물어볼 수 있습니다)"
  curl -fsSL https://ollama.com/install.sh | sh || die "ollama 설치 실패"
fi

# ── 5. 번역 모델 ─────────────────────────────────────────────────────────
# 등록만 돼 있으면 GGUF 원본 파일은 필요 없다 — ollama 가 자기 저장소로
# 복사해 두기 때문이다. 그래서 이 검사가 통과하면 6번(5.8GB)을 건너뛴다.
#
# **`ollama list` 로 검사하면 안 된다.** 그건 :11434 의 시스템 데몬에게
# 묻는데, 그 데몬은 `ollama` 사용자로 돌아서 저장소가
# `/usr/share/ollama/.ollama/models` 다. pdfko 는 자기 데몬을 :11500 에
# 띄우고 `~/.ollama/models` 를 쓴다. 즉 등록해 놔도 `ollama list` 에는
# 영영 안 보이고, 실행할 때마다 5.8GB 를 다시 받게 된다.
STORE="${PDFKO_MODELS:-$HOME/.ollama/models}"
if [ -f "$STORE/manifests/registry.ollama.ai/library/${MODEL_TAG}/latest" ]; then
  ok "번역 모델 (${MODEL_TAG})"
else
  # 이미 받아 둔 GGUF 가 어딘가 있을 수 있다. 5.8GB 를 또 받기 전에 찾는다.
  GGUF=""
  # 찾아볼 곳: 저장소 옆 → 표준 위치들. 특정 컴퓨터에만 있는 경로를
  # 여기 박으면 안 된다. 남의 폴더에 있으면 PDFKO_GGUF 로 알려주면 된다.
  for d in "$HERE/models" "${PDFKO_GGUF:-}" "$HOME/models" \
           "$HOME/Downloads" "$HOME/.cache/huggingface/hub"; do
    [ -n "$d" ] || continue
    [ -d "$d" ] || continue
    found=$(find "$d" -name "$GGUF_NAME" -type f -print -quit 2>/dev/null)
    [ -n "$found" ] && { GGUF="$found"; break; }
  done

  if [ -n "$GGUF" ]; then
    ok "모델 파일 찾음 — $GGUF"
  else
    work "번역 모델 내려받기 (5.8GB, 최초 1회만)"
    command -v hf >/dev/null 2>&1 || uv tool install -q huggingface_hub
    mkdir -p "$HERE/models"
    # 5.8GB 를 받는 동안 회선이 한 번 끊기는 건 드문 일이 아니다. 한 번
    # 끊겼다고 여기서 죽으면 사용자는 처음부터 다시 실행해야 한다. hf 가
    # 이어받기를 해 주므로 받은 만큼은 남는다 — 될 때까지 다시 부른다.
    #
    # 성공 판정은 종료 코드가 아니라 파일이 생겼는지로 한다. 받다 만 것은
    # .incomplete 라 이 검사에 걸리지 않는다.
    n=1
    while [ "$n" -le 20 ]; do
      hf download "$HF_REPO" --include "*Q6_K*" --local-dir "$HERE/models" || true
      GGUF=$(find "$HERE/models" -name "$GGUF_NAME" -type f -print -quit)
      [ -n "$GGUF" ] && break
      work "연결이 끊겼습니다 — 이어받기 재시도 ($n/20)"
      sleep 5
      n=$((n + 1))
    done
    [ -n "$GGUF" ] || die "모델 내려받기 실패 — 다시 실행하면 받은 곳부터 이어받습니다"
  fi
  work "모델 등록 (1~2분)"
fi

# ── 6. poppler (선택) ────────────────────────────────────────────────────
command -v pdffonts >/dev/null 2>&1 && ok "poppler (폰트 점검)" \
  || work "poppler 없음 — 폰트 점검만 꺼집니다 (sudo apt install poppler-utils)"

# ── 7. 다음부터 딸깍하게 ─────────────────────────────────────────────────
# Nautilus 는 .sh 를 더블클릭해도 실행하지 않는다. 바탕화면 아이콘을 만든다.
DESK=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
if [ -d "$DESK" ] && [ ! -f "$DESK/pdfko.desktop" ]; then
  cat > "$DESK/pdfko.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=pdfko — PDF 한국어 번역
Comment=영문 PDF·PPTX 를 레이아웃 그대로 한국어로
Exec=$HERE/pdfko.sh
Path=$HERE
Icon=application-pdf
Terminal=true
Categories=Office;
DESKTOP
  chmod +x "$DESK/pdfko.desktop"
  gio set "$DESK/pdfko.desktop" metadata::trusted true 2>/dev/null
  ok "바탕화면에 아이콘을 만들었습니다 — 다음부터 딸깍하세요"
fi

# ── 8. 번역 화면 ─────────────────────────────────────────────────────────
echo
bold "▶ 번역 화면을 엽니다"
echo "  브라우저가 자동으로 열립니다. PDF 를 끌어다 놓으세요."
echo "  창을 닫아도 번역은 계속됩니다. 끝내려면 이 터미널에서 Ctrl+C."
echo

# 모델이 아직 등록 안 됐으면 웹이 뜨기 전에 등록한다(최초 1회, 1~2분).
if [ -n "${GGUF:-}" ]; then
  .venv/bin/python -c "
import sys; from pathlib import Path
from pdfko import runner
from pdfko.paths import out_base
w = out_base(); w.mkdir(parents=True, exist_ok=True)
srv = runner.Server(w, '${MODEL_TAG}'); srv.start_ollama()
runner.ensure_model(w, Path('${GGUF}').resolve(), '${MODEL_TAG}', srv.op)
" || die "모델 등록 실패"
  ok "모델 등록 완료"
fi

.venv/bin/pdfko-web
hold
