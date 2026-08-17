"""브라우저에서 쓰는 화면. 파일을 올리고 버튼 한 번이면 끝난다.

    pdfko-web            →  http://127.0.0.1:8000

## 왜 필요한가

CLI 는 인자를 외워야 하고, 몇 시간짜리 작업의 진행을 보려면 로그를 따라다녀야
한다. 교재 한 권 번역하려는 사람에게 그건 과하다.

## 설계

작업은 길다(500쪽에 3~5시간). 그래서 요청을 붙잡고 있지 않고 **백그라운드
스레드로 돌린 뒤 진행 상황만 폴링**한다. 브라우저를 닫아도 작업은 계속되고,
다시 열면 이어서 보인다.

한 번에 한 작업만 받는다. GPU 가 하나뿐이라 동시에 두 개를 돌리면 둘 다 느려질
뿐이다. 이미 도는 중이면 그 작업 상태를 보여준다.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# 임포트만으로 홈에 폴더를 만들지 않는다. 필요할 때 만든다.
ROOT = Path.home() / "pdfko-작업"


@dataclass
class Job:
    name: str
    src: Path
    work: Path
    out: Path | None = None
    report: Path | None = None
    stage: str = "대기"
    detail: str = ""
    pct: int = 0
    done: bool = False
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    log: list[str] = field(default_factory=list)

    def say(self, stage: str, detail: str = "", pct: int | None = None) -> None:
        self.stage = stage
        self.detail = detail
        if pct is not None:
            self.pct = pct
        self.log.append(f"{time.strftime('%H:%M:%S')}  {stage} {detail}".rstrip())
        del self.log[:-200]

    @property
    def elapsed(self) -> str:
        s = int((self.finished or time.time()) - self.started)
        return f"{s // 3600}시간 {s % 3600 // 60}분" if s >= 3600 else f"{s // 60}분 {s % 60}초"


app = FastAPI(title="pdfko")
JOB: Job | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------- 작업 실행
def _run(job: Job, pages: str, glossary: Path | None) -> None:
    from . import clipscan, glyphmap, qa, recover, runner, terms
    import pymupdf

    try:
        job.say("사전 점검", job.src.name, 2)
        with pymupdf.open(job.src) as d:
            total = d.page_count
        first, last = 1, total
        if pages.strip():
            m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", pages)
            if not m:
                raise ValueError(f"쪽 범위 형식이 잘못됐습니다: {pages!r} (예: 13-502)")
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) else lo   # `7` = 7쪽 한 장
            if lo > total:
                raise ValueError(f"시작 쪽이 범위를 벗어났습니다: {lo} (전체 {total}쪽)")
            first, last = max(1, lo), min(total, hi)
            if first > last:
                raise ValueError(f"번역할 쪽이 없습니다: {pages} (전체 {total}쪽)")
        offset = first - 1
        job.say("사전 점검", f"{total}쪽 중 {first}-{last} 번역", 4)

        src = job.src
        scans = clipscan.scan(src, first, last)
        unreadable = [r.page for r in scans if r.error]
        if unreadable:
            job.log.append(f"{len(unreadable)}쪽은 내용을 읽지 못해 숨은 글자 "
                           f"검사를 건너뛰었습니다")
        hidden = [r for r in scans if r.hidden >= 40]
        if hidden:
            job.say("원본 청소", f"가려진 글자가 있는 {len(hidden)}쪽 처리 중", 6)
            cleaned = job.work / "cleaned.pdf"
            touched, rolled, lost = clipscan.clean(
                src, cleaned, pages=[r.page for r in hidden], min_hidden=40)
            if touched:
                src = cleaned
                job.say("원본 청소", f"{len(touched)}쪽 청소 완료", 8)
            # 잃은 낱말을 반드시 알린다. 조용히 넘기면 사용자는 본문 일부가
            # 사라진 것을 영영 모른다. CLI 는 알리는데 여기만 빠져 있었다.
            for pg, words in sorted(lost.items()):
                job.log.append(f"청소 중 {pg}쪽에서 잃은 낱말: {', '.join(words)}")

        # 깨진 합자 사전. 이게 없으면 한국어 본문에 `↵` 가 박힌 채로 나온다.
        # CLI 에만 있고 웹에는 없었다 — 같은 문서를 CLI 로 돌리면 0개,
        # 웹으로 돌리면 6개가 나왔다. 화면의 끌어다 놓기가 더 나쁜 결과를
        # 내놓고 있었던 셈이다.
        # 손상 판정에 걸지 않고 항상 만든다. 표본 임계값에 못 미치는 짧은
        # 구간에서 사전이 빠져 `↵` 가 그대로 나오는 일이 있었다.
        job.say("사전 점검", "손상된 합자를 찾는 중", 9)
        gm_path = None
        gm = glyphmap.build_table(src)   # 구간이 아니라 문서 전체에서
        if gm:
            gm_path = job.work / "glyphmap.json"
            glyphmap.save(gm, gm_path)
            job.log.append(f"손상된 합자 {len(gm)}쌍을 원본에서 찾았다")

        job.say("서버 기동", "추론 서버를 켜는 중", 10)
        srv = runner.Server(job.work, "hy-mt2-7b")
        srv.glyphmap = gm_path
        srv.start_ollama()
        if not srv.model_ready():
            raise RuntimeError(
                "모델 'hy-mt2-7b' 이 추론 서버에 없습니다. 터미널에서 "
                "`pdfko <파일> --gguf <모델.gguf>` 로 한 번 등록해 주세요.")

        # 용어 통일은 **프록시를 띄우기 전에** 끝내야 한다. user_sig 는
        # start_proxy 가 자식에게 넘길 때만 읽히므로, 나중에 넣으면 이미 뜬
        # 프록시에는 반영되지 않는다. 그러면 용어집이 캐시 키에 안 들어가고,
        # 다시 돌렸을 때 옛 번역이 그대로 나온다. 용어 선별은 추론 서버만
        # 있으면 되므로 여기서 해도 된다.
        if not glossary:
            job.say("용어 통일", "이 문서의 용어를 찾는 중", 11)
            cand = terms.extract(src, first, last)
            cand = terms.keep_terms(cand, port=srv.op, model="hy-mt2-7b")
            picked = terms.decide(cand, port=srv.op, model="hy-mt2-7b",
                                  via_proxy=False)
            if picked:
                gpath = job.work / "용어집.csv"
                terms.write_csv(gpath, cand, picked)
                glossary = gpath
                job.log.append(f"{len(picked)}개 용어의 역어를 고정했다: "
                               + ", ".join(f"{k}→{v}"
                                           for k, v in list(picked.items())[:5]))

        srv.user_sig = runner.Server.signature(glossary)
        srv.start_proxy(__import__("sys").executable)
        # 엔진 캐시는 우리 미들웨어를 통째로 우회한다. 지우지 않으면 검증도
        # 합자 복구도 폭 검사도 거치지 않은 옛 결과가 그대로 나온다.
        # 실측: 작업 폴더를 지우고 같은 파일을 다시 올렸더니 미들웨어 호출
        # 0회로 22초 만에 "완료"됐다. CLI 는 지우는데 여기만 빠져 있었다.
        runner.clear_engine_cache()

        chunks = runner.plan_chunks(first, last, 40, job.work)
        if not chunks:
            raise ValueError(f"번역할 쪽이 없습니다: {first}-{last}")
        for i, c in enumerate(chunks, 1):
            pct = 10 + int(75 * (i - 1) / len(chunks))
            if c.done:
                job.say("번역", f"{c.name} 건너뜀 (완료됨)", pct)
                continue
            job.say("번역", f"{c.name}  ({i}/{len(chunks)} 구간)", pct)
            if not runner.translate_chunk(c, src, job.work, model="hy-mt2-7b",
                                          proxy_port=srv.pp, glossary=glossary,
                                          prompt_file=None):
                raise RuntimeError(
                    f"{c.name} 구간 번역 실패 — logs/part_{c.name}.log 를 확인하세요")

        job.say("병합", "", 88)
        out = job.work / f"{job.src.stem}_한국어.pdf"
        n = runner.merge(chunks, out)

        job.say("파손 검사", f"{n}쪽 확인 중", 92)
        verdicts = qa.scan(str(src), str(out), offset=offset)
        with pymupdf.open(out) as d:
            for v in verdicts:
                mx = qa.mixed_language_figures(d[v.page - 1])
                if mx:
                    v.reasons.append(f"그림혼재{mx}")
        severe = [v for v in verdicts
                  if v.overlap > 0.15 or v.outside > 0.10 or v.collision > 0.10]

        # 복구에 들어가기 **전에** 결과물을 등록한다. 예전에는 복구가 끝난 뒤에
        # 등록해서, 마지막 단계에서 예외가 나면 몇 시간 번역한 결과가 통째로
        # 사라지고 내려받기도 보고서도 없이 "실패"만 남았다.
        job.out = out
        recs = []
        if severe:
            job.say("자동 복구", f"{len(severe)}쪽 되살리는 중", 96)
            try:
                recs = recover.repair_pages(
                    out, src, severe, offset, src, job.work,
                    model="hy-mt2-7b", proxy_port=srv.pp, glossary=glossary,
                    on_step=lambda p, what: job.say(
                        "자동 복구", f"{p}쪽 {what}" if p else what, 96))
            except Exception as e:
                job.log.append(f"자동 복구 실패({type(e).__name__}: {e}) — "
                               f"번역본은 그대로 내려받을 수 있습니다")

        rep = job.work / "품질보고서.md"
        recover.write_report(rep, verdicts, recs, offset)
        job.out, job.report = out, rep
        # 엔진 중간 산출물은 쪽수에 비례해 쌓인다. 결과와 보고서는 남기고
        # work/ 만 지운다. 사용자가 만든 번역본을 지우지는 않는다.
        runner.cleanup_work(job.work)
        bad = len([v for v in verdicts if v.broken])
        job.finished = time.time()
        job.say("완료", f"{n}쪽 · 파손 {bad}쪽 · 소요 {job.elapsed}", 100)
        job.done = True
    except Exception as e:
        job.error = f"{type(e).__name__}: {e}"
        job.log.append(traceback.format_exc()[-1500:])
        job.finished = time.time()
        job.say("실패", job.error, 100)
        job.done = True
    finally:
        # 웹은 오래 살아 있다. 작업이 끝날 때마다 서버를 내리지 않으면
        # 프록시가 작업마다 하나씩 쌓인다 — 실측으로 12~18시간 묵은 고아
        # 세 개가 떠 있었고, 포트 창(8101~8140)을 잠식하고 있었다.
        runner.stop_all()


# ---------------------------------------------------------------- 엔드포인트
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.post("/start")
async def start(file: UploadFile = File(...), pages: str = Form(""),
                glossary: UploadFile | None = File(None)):
    global JOB
    with _lock:
        if JOB and not JOB.done:
            return JSONResponse({"error": "이미 작업이 돌고 있다"}, status_code=409)
        # 파일명은 이름 부분만 취하고, 디렉터리 이름으로 쓸 stem 도 따로 검사한다.
        # `...pdf` 는 stem 이 '..' 이 되어 홈 디렉터리로 탈출했고, 널바이트는 500 을 냈다.
        safe = Path(file.filename or "").name
        if not safe or safe in (".", "..") or "\x00" in safe:
            safe = "input.pdf"
        stem = Path(safe).stem
        if not stem or stem in (".", "..") or "/" in stem or "\x00" in stem:
            stem = "문서"
        # 같은 문서는 같은 폴더를 쓴다. 시각을 붙이면 매번 새 폴더가 생겨
        # 구간 .done 표식을 못 찾고 늘 처음부터 다시 번역하게 된다.
        work = ROOT / stem
        # 하위 폴더를 여기서 다 만든다. runner 는 서버를 새로 띄울 때만
        # logs/ 를 만드는데, ollama 가 이미 떠 있으면 그 경로를 타지 않아
        # part 로그를 열다가 죽는다 — 정상 상태에서 100% 실패했다.
        if work.resolve().parent != ROOT.resolve():
            return JSONResponse({"error": "파일명이 올바르지 않습니다"}, status_code=400)
        for d in ("parts", "logs", "cache", "work", "models"):
            (work / d).mkdir(parents=True, exist_ok=True)
        # 업로드 파일명은 신뢰할 수 없다. `../../.bashrc` 같은 이름으로
        # 홈 디렉터리 밖에 쓸 수 있었다. 파일명만 취한다.
        src = work / safe
        with src.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        # 같은 파일명으로 **다른 문서**를 올리면 구간 .done 표식이 남아 있어
        # 번역을 통째로 건너뛰고 이전 문서의 번역을 내놓았다. 완료로 보고하면서.
        # 내용 해시가 다르면 이전 산출물을 버린다.
        import hashlib as _h
        digest = _h.sha256(src.read_bytes()).hexdigest()[:16]
        stamp = work / "source.sha256"
        if stamp.exists() and stamp.read_text().strip() != digest:
            shutil.rmtree(work / "parts", ignore_errors=True)
            (work / "parts").mkdir(parents=True, exist_ok=True)
        stamp.write_text(digest)
        gl = None
        if glossary is not None and glossary.filename:
            gl = work / "glossary.csv"
            with gl.open("wb") as f:
                shutil.copyfileobj(glossary.file, f)
            # 헤더가 틀리면 번역 엔진이 조용히 무시하고 그냥 진행한다.
            # 사용자는 용어집이 적용된 줄 알고 배포하게 된다.
            import csv as _csv
            with gl.open(encoding="utf-8-sig", errors="replace") as f:
                cols = {c.strip() for c in next(_csv.reader(f), [])}
            if not {"source", "target"} <= cols:
                return JSONResponse(
                    {"error": "용어집 첫 줄은 source,target 이어야 합니다"},
                    status_code=400)
        JOB = Job(name=src.name, src=src, work=work)
        threading.Thread(target=_run, args=(JOB, pages, gl), daemon=True).start()
    return {"ok": True}


@app.get("/status")
async def status():
    if JOB is None:
        return {"idle": True}
    return {
        "idle": False, "name": JOB.name, "stage": JOB.stage,
        "detail": JOB.detail, "pct": JOB.pct, "done": JOB.done,
        "error": JOB.error, "elapsed": JOB.elapsed,
        "log": JOB.log[-14:],
        "has_out": bool(JOB.out and JOB.out.exists()),
        "has_report": bool(JOB.report and JOB.report.exists()),
    }


@app.get("/download")
async def download():
    if not (JOB and JOB.out and JOB.out.exists()):
        return JSONResponse({"error": "결과가 아직 없다"}, status_code=404)
    return FileResponse(JOB.out, filename=JOB.out.name,
                        media_type="application/pdf")


@app.get("/report")
async def report():
    if not (JOB and JOB.report and JOB.report.exists()):
        return JSONResponse({"error": "보고서가 없다"}, status_code=404)
    return FileResponse(JOB.report, filename=JOB.report.name,
                        media_type="text/markdown")


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><link rel="icon" href="data:,">
<title>pdfko — 영문 교재·논문 한국어 번역</title>
<style>
:root{--bg:#faf9f7;--fg:#1c1b19;--mut:#6b665e;--line:#e3e0da;--acc:#2f6f4e;--err:#b02a1f;--onacc:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#171614;--fg:#eceae6;--mut:#9b958a;--line:#302e2a;--acc:#6fbf92;--err:#ff9c8f;--onacc:#0f2419}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:48px 20px 80px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.3px}
.sub{color:var(--mut);margin:0 0 32px}
.card{border:1px solid var(--line);border-radius:14px;padding:22px;background:transparent}
.drop{border:2px dashed var(--line);border-radius:14px;padding:44px 20px;text-align:center;
 cursor:pointer;transition:.15s}
.drop:hover,.drop.on{border-color:var(--acc);background:color-mix(in srgb,var(--acc) 7%,transparent)}
.drop strong{display:block;font-size:17px;margin-bottom:4px}
.drop span{color:var(--mut);font-size:14px}
input[type=file]{display:none}
.row{display:flex;gap:12px;align-items:center;margin-top:18px;flex-wrap:wrap}
label.f{font-size:14px;color:var(--mut);min-width:78px}
input[type=text]{flex:1;min-width:150px;padding:9px 12px;border:1px solid var(--line);
 border-radius:9px;background:transparent;color:var(--fg);font:inherit;font-size:14px}
button{padding:12px 24px;border:0;border-radius:10px;background:var(--acc);color:var(--onacc);
 font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.45;cursor:default}
.bar{height:8px;border-radius:99px;background:var(--line);overflow:hidden;margin:18px 0 10px}
.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .4s}
.bar.bad>i{background:var(--err)}
.stage{font-weight:600}
.detail,.elapsed{color:var(--mut);font-size:14px}
pre{background:color-mix(in srgb,var(--fg) 5%,transparent);border-radius:10px;padding:14px;
 font-size:12.5px;line-height:1.6;max-height:230px;overflow:auto;margin:16px 0 0;white-space:pre-wrap}
.dl{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
.dl a{padding:11px 18px;border:1px solid var(--line);border-radius:10px;text-decoration:none;
 color:var(--fg);font-size:14px;font-weight:600}
.dl a.p{background:var(--acc);color:var(--onacc);border-color:var(--acc)}
.err{color:var(--err);font-size:14px;margin-top:10px}
.note{color:var(--mut);font-size:13px;margin-top:26px;line-height:1.75}
.hide{display:none}
</style>
<div class="wrap">
<h1>영문 교재와 논문을 한국어로</h1>
<p class="sub">레이아웃·수식·그림을 그대로 두고 글자만 번역합니다. 전부 이 컴퓨터에서 처리됩니다.</p>

<div class="card" id="setup">
  <div class="drop" id="drop">
    <strong id="fname">PDF 를 여기에 놓으세요</strong>
    <span>클릭해서 고를 수도 있습니다</span>
  </div>
  <input type="file" id="file" accept=".pdf">
  <div class="row">
    <label class="f" for="pages">쪽 범위</label>
    <input type="text" id="pages" placeholder="예: 13-502 (비우면 전체)">
  </div>
  <div class="row">
    <label class="f" for="glo">용어집</label>
    <input type="file" id="glo" accept=".csv" style="display:block;font-size:13px">
  </div>
  <div class="row" style="justify-content:flex-end">
    <button id="go" disabled>번역 시작</button>
  </div>
</div>

<div class="card hide" id="run">
  <div class="stage" id="stage">준비 중</div>
  <div class="detail" id="detail"></div>
  <div class="bar"><i id="fill"></i></div>
  <div class="elapsed" id="elapsed"></div>
  <div class="err hide" id="err"></div>
  <div class="dl hide" id="dl">
    <a class="p" href="/download">번역본 내려받기</a>
    <a href="/report">품질 보고서</a>
  </div>
  <pre id="log"></pre>
</div>

<p class="note">500쪽 교재는 3~5시간쯤 걸립니다. 이 창을 닫아도 작업은 계속되고,
다시 열면 진행 상황이 그대로 보입니다.<br>
글자가 겹쳐 읽을 수 없게 된 페이지는 자동으로 찾아 되살리고, 그러지 못한 페이지는
보고서에 남깁니다.</p>
</div>
<script>
const $=s=>document.querySelector(s);
let picked=null;
$('#drop').onclick=()=>$('#file').click();
$('#file').onchange=e=>pick(e.target.files[0]);
$('#drop').ondragover=e=>{e.preventDefault();$('#drop').classList.add('on')};
$('#drop').ondragleave=()=>$('#drop').classList.remove('on');
$('#drop').ondrop=e=>{e.preventDefault();$('#drop').classList.remove('on');pick(e.dataTransfer.files[0])};
function pick(f){if(!f)return;picked=f;$('#fname').textContent=f.name;$('#go').disabled=false}
$('#go').onclick=async()=>{
  if(!picked)return;
  $('#go').disabled=true;
  const fd=new FormData();
  fd.append('file',picked);
  fd.append('pages',$('#pages').value.trim());
  if($('#glo').files[0])fd.append('glossary',$('#glo').files[0]);
  const r=await fetch('/start',{method:'POST',body:fd});
  if(!r.ok){const j=await r.json();alert(j.error||'시작하지 못했습니다');$('#go').disabled=false;return}
  /* 이전 작업의 오류 문구와 내려받기 단추를 즉시 지운다. poll() 이 2초 뒤에
     바로잡아 주긴 하지만, 그 사이 사용자는 새 작업 화면에서 지난 실패 메시지를
     보게 된다. */
  $('#err').textContent='';$('#err').classList.add('hide');
  $('#dl').classList.add('hide');$('#log').textContent='';
  $('#stage').textContent='준비 중';$('#detail').textContent='';
  $('#elapsed').textContent='';$('#fill').style.width='0%';
  document.querySelector('.bar').classList.remove('bad');
  $('#setup').classList.add('hide');$('#run').classList.remove('hide');
};
async function poll(){
  try{
    const s=await(await fetch('/status')).json();
    if(!s.idle){
      $('#setup').classList.toggle('hide',!s.done);$('#run').classList.remove('hide');
      $('#stage').textContent=s.stage;
      $('#detail').textContent=s.detail;
      $('#fill').style.width=s.pct+'%';
      $('#elapsed').textContent='경과 '+s.elapsed;
      $('#log').textContent=(s.log||[]).join('\\n');
      $('#err').textContent=s.error||'';$('#err').classList.toggle('hide',!s.error);
      $('#dl').classList.toggle('hide',!s.has_out);
      document.querySelector('.bar').classList.toggle('bad',!!s.error);
      if(s.done)$('#go').disabled=!picked;
    }
  }catch(e){}
  setTimeout(poll,2000);
}
poll();
</script></html>"""


def main() -> int:
    import uvicorn
    ROOT.mkdir(parents=True, exist_ok=True)
    print("  pdfko 웹 화면:  http://127.0.0.1:8000")
    print("  작업 폴더:      ", ROOT)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
    return 0
