# pdfko 원샷 실행 (윈도우) — 딸깍 한 번으로 번역 화면까지.
#
# 필요한 걸 하나씩 점검해서 **없는 것만** 설치하고, 다 갖춰졌으면 곧장
# 번역 화면을 연다. 두 번째 실행부터는 점검만 몇 초 하고 바로 열린다.
#
# 더블클릭으로 쓰려면 pdfko.bat 을 쓰세요 — 윈도우는 .ps1 을 더블클릭해도
# 실행하지 않고 메모장으로 엽니다.

$ErrorActionPreference = 'Stop'
# 스크립트가 놓인 폴더를 기준점으로 삼는다. 어디서 실행하든 — 바탕화면
# 아이콘이든, 다른 폴더에서 친 명령이든 — 같은 곳을 보게 하려는 것이다.
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

$ModelTag = 'hy-mt2-7b'
$GgufName = 'HY-MT2-7B-Q6_K.gguf'
$HfRepo   = 'tencent/Hy-MT2-7B-GGUF'

function Bold($m) { Write-Host $m -ForegroundColor White }
function Ok($m)   { Write-Host "  [O] $m" -ForegroundColor Green }
function Work($m) { Write-Host "  [.] $m" -ForegroundColor Yellow }
function Die($m)  {
  Write-Host "  [X] $m" -ForegroundColor Red
  Read-Host "`n엔터를 누르면 닫힙니다"
  exit 1
}
function Has($n) { $null -ne (Get-Command $n -ErrorAction SilentlyContinue) }

Bold "▶ pdfko 준비"

# ── 1. uv ────────────────────────────────────────────────────────────────
# 윈도우에 파이썬 3.12 가 없어도 uv 가 알아서 받아온다.
if (Has 'uv') { Ok 'uv' }
else {
  Work 'uv 설치'
  try { irm https://astral.sh/uv/install.ps1 | iex } catch { Die 'uv 설치 실패 — 인터넷 연결을 확인해주세요' }
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (-not (Has 'uv')) { Die 'uv 를 PATH 에서 찾지 못했습니다' }

# ── 2. 가상환경 + pdfko ──────────────────────────────────────────────────
$Venv = Join-Path $Here '.venv\Scripts'
if (Test-Path "$Venv\pdfko.exe") { Ok 'pdfko' }
else {
  Work '가상환경 만들고 pdfko 설치 (1~2분)'
  uv venv --python 3.12 | Out-Null
  uv pip install -q -e .
  if ($LASTEXITCODE -ne 0) { Die 'pdfko 설치 실패' }
}

# ── 3. babeldoc ──────────────────────────────────────────────────────────
# 번역 엔진. pdfko 와 같은 환경에 섞으면 onnxruntime·pymupdf 가 충돌해서
# 반드시 따로 격리 설치한다(pyproject.toml 주석 참고).
if (Has 'babeldoc') { Ok 'babeldoc' }
else {
  Work 'babeldoc 설치 (2~3분)'
  uv tool install -q --python 3.12 babeldoc
  if ($LASTEXITCODE -ne 0) { Die 'babeldoc 설치 실패' }
}

# ── 4. ollama ────────────────────────────────────────────────────────────
if (Has 'ollama') { Ok 'ollama' }
else {
  Work 'ollama 설치'
  if (Has 'winget') {
    winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements
    $env:Path = "$env:LOCALAPPDATA\Programs\Ollama;$env:Path"
  }
  if (-not (Has 'ollama')) {
    Die "ollama 를 자동 설치하지 못했습니다. https://ollama.com/download 에서 받아 설치한 뒤 다시 실행해주세요"
  }
}

# ── 5. 번역 모델 ─────────────────────────────────────────────────────────
# 등록만 돼 있으면 GGUF 원본 파일은 필요 없다 — ollama 가 자기 저장소로
# 복사해 두기 때문이다. 그래서 이 검사가 통과하면 내려받기를 건너뛴다.
#
# `ollama list` 로 검사하면 안 된다. 그건 떠 있는 데몬에게 묻는 것이라
# 그 데몬의 저장소만 보인다. pdfko 는 자기 데몬을 :11500 에 따로 띄우고
# 자기 저장소를 쓰므로, 등록해 놔도 목록에 안 보여 매번 5.8GB 를 다시
# 받게 된다. 저장소의 매니페스트 파일을 직접 본다.
$Gguf = ''
$Store = if ($env:PDFKO_MODELS) { $env:PDFKO_MODELS }
         else { Join-Path $env:USERPROFILE '.ollama\models' }
$Manifest = Join-Path $Store "manifests\registry.ollama.ai\library\$ModelTag\latest"
if (Test-Path $Manifest) { Ok "번역 모델 ($ModelTag)" }
else {
  # 이미 받아 둔 GGUF 가 어딘가 있을 수 있다. 5.8GB 를 또 받기 전에 찾는다.
  # 찾아볼 곳: 저장소 옆 → 표준 위치들. 특정 컴퓨터에만 있는 경로를
  # 여기 박으면 안 된다. 남의 폴더에 있으면 PDFKO_GGUF 로 알려주면 된다.
  foreach ($d in @((Join-Path $Here 'models'), $env:PDFKO_GGUF,
                   (Join-Path $env:USERPROFILE 'models'),
                   (Join-Path $env:USERPROFILE 'Downloads'),
                   (Join-Path $env:USERPROFILE '.cache\huggingface\hub'))) {
    if (-not $d) { continue }
    if (-not (Test-Path $d)) { continue }
    $f = Get-ChildItem $d -Recurse -Filter $GgufName -File -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($f) { $Gguf = $f.FullName; break }
  }

  if ($Gguf) { Ok "모델 파일 찾음 — $Gguf" }
  else {
    Work '번역 모델 내려받기 (5.8GB, 최초 1회만)'
    if (-not (Has 'hf')) { uv tool install -q huggingface_hub }
    $Models = Join-Path $Here 'models'
    New-Item -ItemType Directory -Force -Path $Models | Out-Null
    # 5.8GB 를 받는 동안 회선이 한 번 끊기는 건 드문 일이 아니다. 한 번
    # 끊겼다고 여기서 죽으면 사용자는 처음부터 다시 실행해야 한다. hf 가
    # 이어받기를 해 주므로 받은 만큼은 남는다 — 될 때까지 다시 부른다.
    #
    # 성공 판정은 종료 코드가 아니라 파일이 생겼는지로 한다. 받다 만 것은
    # .incomplete 라 이 검사에 걸리지 않는다.
    $f = $null
    for ($n = 1; $n -le 20; $n++) {
      hf download $HfRepo --include "*Q6_K*" --local-dir $Models
      $f = Get-ChildItem $Models -Recurse -Filter $GgufName -File -ErrorAction SilentlyContinue |
           Select-Object -First 1
      if ($f) { break }
      Work "연결이 끊겼습니다 — 이어받기 재시도 ($n/20)"
      Start-Sleep -Seconds 5
    }
    if (-not $f) { Die '모델 내려받기 실패 — 다시 실행하면 받은 곳부터 이어받습니다' }
    $Gguf = $f.FullName
  }
}

# ── 6. poppler (선택) ────────────────────────────────────────────────────
if (Has 'pdffonts') { Ok 'poppler (폰트 점검)' }
else { Work 'poppler 없음 — 폰트 점검만 꺼집니다 (선택 사항)' }

# ── 7. 모델 등록 (최초 1회) ──────────────────────────────────────────────
if ($Gguf) {
  Work '모델 등록 (1~2분)'
  & "$Venv\python.exe" -c @"
from pathlib import Path
from pdfko import runner
from pdfko.paths import out_base
w = out_base(); w.mkdir(parents=True, exist_ok=True)
srv = runner.Server(w, '$ModelTag'); srv.start_ollama()
runner.ensure_model(w, Path(r'$Gguf').resolve(), '$ModelTag', srv.op)
"@
  if ($LASTEXITCODE -ne 0) { Die '모델 등록 실패' }
  Ok '모델 등록 완료'
}

# ── 8. 번역 화면 ─────────────────────────────────────────────────────────
Write-Host ''
Bold '▶ 번역 화면을 엽니다'
Write-Host '  브라우저가 자동으로 열립니다. PDF 를 끌어다 놓으세요.'
Write-Host '  창을 닫아도 번역은 계속됩니다. 끝내려면 이 창에서 Ctrl+C.'
Write-Host ''

& "$Venv\pdfko-web.exe"
Read-Host "`n엔터를 누르면 닫힙니다"
