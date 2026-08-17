"""번역 실행기 — 구간 분할, 이어하기, 서버 수명 관리.

## 왜 구간으로 쪼개나

BabelDOC 은 문서를 끝까지 처리해야 PDF 를 내놓는다. 500쪽을 한 번에 돌리다
중간에 죽으면 몇 시간을 쓰고도 산출물이 0이다. 구간마다 PDF 를 떨어뜨리면
죽어도 거기까지는 남고, 다시 실행하면 완료된 구간을 건너뛴다.

## 캐시가 세 겹이라는 함정

  1. 구간 `.done` 표식      — 이 모듈이 관리
  2. 프록시 SQLite 캐시      — 성공한 문단만 저장 (실패는 저장하지 않는다)
  3. BabelDOC 자체 캐시      — 우리가 통제할 수 없다

**3번을 지우지 않으면 2번을 고쳐도 소용이 없다.** BabelDOC 이 자기 캐시에서
바로 꺼내 쓰면서 프록시를 통째로 우회하기 때문이다. 검증 규칙을 바꿨다면
반드시 `clear_engine_cache()` 를 부를 것.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ENGINE_CACHE = Path.home() / ".cache" / "babeldoc"


@dataclass
class Chunk:
    first: int
    last: int
    outdir: Path

    @property
    def name(self) -> str:
        return f"{self.first}-{self.last}"

    @property
    def done(self) -> bool:
        return (self.outdir / ".done").exists()

    def mark_done(self) -> None:
        (self.outdir / ".done").touch()

    def pdf(self) -> Path | None:
        # 최신 파일을 고른다. 알파벳순으로 뽑으면 원본으로 돌린 옛 결과가
        # 청소본 결과를 이긴다 (RLbook… < cleaned…).
        f = sorted(self.outdir.glob("*.mono.pdf"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
        return f[0] if f else None


def plan_chunks(first: int, last: int, size: int, root: Path) -> list[Chunk]:
    if size < 1:
        raise ValueError(f"구간 크기는 1 이상이어야 한다 (받은 값: {size})")
    out, s = [], first
    while s <= last:
        e = min(s + size - 1, last)
        out.append(Chunk(s, e, root / "parts" / f"{s}-{e}"))
        s = e + 1
    return out


def clear_engine_cache() -> None:
    """BabelDOC 자체 캐시를 지운다.

    검증 규칙이나 프롬프트를 바꾼 뒤에는 반드시 호출해야 한다. 안 그러면
    엔진이 옛 결과를 그대로 꺼내 쓰면서 우리 미들웨어를 건너뛴다.
    """
    for p in ENGINE_CACHE.glob("cache.v1.db*"):
        p.unlink(missing_ok=True)


# 모델 저장소는 **사용자 공용**이다. 작업 폴더 안에 두면 책마다 저장소가
# 새로 생겨서, `--gguf` 로 한 번 등록해도 다음 책에서는 없는 모델이 된다.
# 그때 엔진은 영어를 그대로 내놓고 성공을 보고한다. 게다가 6GB 짜리 사본이
# 책 수만큼 쌓인다. 실측으로 첫 사용자가 정확히 이 함정에 빠졌다.
MODEL_STORE = Path(
    os.environ.get("PDFKO_MODELS", Path.home() / ".pdfko" / "ollama"))


# 이 프로세스가 띄운 서버들. 어떤 경로로 끝나든 stop_all() 로 정리한다.
_LIVE: list["Server"] = []


class Server:
    """추론 서버와 미들웨어 프록시의 수명을 관리한다.

    프로세스를 띄운 뒤 **기동 시각을 확인**한다. 재시작했다고 믿었는데 실은
    옛 프로세스가 살아 있어서 수정이 반영되지 않는 일이 실제로 있었다.
    """

    def __init__(self, workdir: Path, model: str,
                 ollama_port: int = 11500, proxy_port: int = 8100):
        self.work = workdir
        self.model = model
        self.op = ollama_port
        self.pp = proxy_port
        self.glyphmap: Path | None = None   # 깨진 합자 사전 (cli 가 채운다)
        self.user_sig: str = ""             # 용어집·프롬프트 지문 (cli 가 채운다)
        self._procs: list[subprocess.Popen] = []
        _LIVE.append(self)

    def expected_rules(self) -> str:
        """이 서버가 띄울 프록시가 가져야 할 규칙 지문.

        합자 사전이나 용어집을 붙이면 자식의 지문이 부모와 달라진다. 부모의
        `proxy.RULES` 를 그대로 비교하면 방금 띄운 프록시조차 낡았다고 판정해
        60초를 기다린 뒤 죽는다.
        """
        from . import glyphmap as _g
        from . import proxy as _p
        gm = _g.load(self.glyphmap) if (self.glyphmap and self.glyphmap.exists()) else {}
        # 부모 환경에 GLYPHMAP/USER_RULES 가 남아 있으면 `_p.RULES` 가 그걸
        # 물고 있으므로, 없는 경우에도 빈 값으로 **다시 계산**해야 한다.
        return _p._rules_fingerprint(gm, self.user_sig)

    @staticmethod
    def signature(*paths: Path | None) -> str:
        """용어집·프롬프트 파일 내용의 지문. 바뀌면 캐시가 무효화되어야 한다."""
        import hashlib
        h = hashlib.sha256()
        for p in paths:
            h.update(b"\x00")
            if p:
                try:
                    h.update(Path(p).read_bytes())
                except OSError:
                    h.update(str(p).encode())
        return h.hexdigest()[:12]

    # ---------------------------------------------------------------- 상태
    def _alive(self, url: str) -> bool:
        try:
            import urllib.request
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:
            return False

    def ollama_up(self) -> bool:
        return self._alive(f"http://127.0.0.1:{self.op}/api/tags")

    def identify(self, port: int) -> dict | None:
        """그 포트에 있는 것이 **pdfko 프록시**인가. 맞으면 /health 응답.

        모양을 확인해야 한다. 포트가 열려 있다는 것만으로는 우리 것인지 알 수
        없고, 남의 서버를 우리 것으로 착각하면 죽이게 된다.
        """
        import json as _json
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=3) as r:
                d = _json.load(r)
        except Exception:
            return None
        need = ("ok", "rules", "upstream", "cache_db", "log_dir")
        return d if isinstance(d, dict) and all(k in d for k in need) else None

    def proxy_up(self) -> bool:
        """살아 있고, 우리 것이고, **지금 소스와 같은 규칙**을 쓰고,
        **이 작업 디렉터리**를 보고 있는 프록시인가.

        규칙 지문: uvicorn 은 --reload 없이 뜨면 기동 시점의 코드를 계속 쓴다.
        예전 구현은 /health 가 응답하기만 하면 재사용해서, 소스를 고쳐도 몇
        시간 묵은 프로세스가 계속 서비스했다.

        작업 디렉터리: 이걸 확인하지 않으면 **다른 실행의 프록시를 빼앗는다.**
        실측으로 두 번역을 동시에 돌렸더니 나중 것이 먼저 것의 프록시를 죽였고,
        먼저 것은 합자 사전 없는 프록시로 넘어가 한국어 본문에 `↵` 15개가
        박혔다. 그러고도 "파손 0쪽"이라고 보고했다.
        """
        d = self.identify(self.pp)
        if d is None:
            return False
        # 간결 모드가 켜진 채로 남은 프록시를 물려받으면 **책 전체가 간결
        # 모드로 번역된다.** 복구 중에 프로세스가 죽으면 되돌리기가 실행되지
        # 않아 그 상태가 남는데, MODE 는 규칙 지문에 없어서 지문만으로는
        # 구별되지 않는다.
        return (d.get("rules") == self.expected_rules()
                and d.get("cache_db") == str(self.work / "cache" / "trans.db")
                and not d.get("concise", False))

    def _port_is_free(self, port: int) -> bool:
        import socket
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _free_port(self, start: int, tries: int = 40) -> int:
        """남의 것을 죽이지 않고 비어 있는 포트를 찾는다.

        예전에는 포트를 쥔 프로세스를 무조건 SIGTERM 했다. 우리 것인지 보지도
        않았다. 8100 은 흔한 개발 포트라, 이 도구를 처음 받아 실행한 사람이
        **무관한 서버를 잃는다.** 실측으로 그렇게 됐다.
        """
        import socket
        for p in range(start, start + tries):
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", p))
                    return p
                except OSError:
                    continue
        raise RuntimeError(f"빈 포트를 찾지 못했다 ({start}~{start + tries})")

    # ---------------------------------------------------------------- 기동
    def start_ollama(self) -> None:
        if self.ollama_up():
            return
        # 부모 환경을 물려준다. 예전에는 PATH/HOME 만 넘겼는데, 그러면
        # CUDA_VISIBLE_DEVICES·LD_LIBRARY_PATH 가 사라져 ollama 가 조용히
        # CPU 로 떨어진다. 500쪽이 4시간에서 며칠이 된다. 우리가 정하는 값은
        # 아래에서 덮어쓰므로 낡은 OLLAMA_* 가 새어 들 걱정은 없다.
        env = {
            **os.environ,
            "OLLAMA_HOST": f"127.0.0.1:{self.op}",
            "OLLAMA_MODELS": str(MODEL_STORE),
            # 슬롯 수는 KV 캐시를 늘려 **레이어를 GPU 밖으로 밀어낼 수 있다.**
            # 8 슬롯이 12GB 카드에서 7B 모델의 전 레이어를 GPU 에 유지하는 값이다.
            # 로그의 `offloaded N/M layers` 가 N==M 인지 반드시 확인할 것.
            "OLLAMA_NUM_PARALLEL": "8",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_KV_CACHE_TYPE": "q8_0",
            "OLLAMA_KEEP_ALIVE": "24h",
            "OLLAMA_FLASH_ATTENTION": "1",
        }
        (self.work / "logs").mkdir(parents=True, exist_ok=True)
        log = open(self.work / "logs" / "ollama.log", "ab")
        self._procs.append(subprocess.Popen(
            ["ollama", "serve"], env=env, stdout=log, stderr=log,
            start_new_session=True))
        for _ in range(60):
            if self.ollama_up():
                return
            time.sleep(1)
        raise RuntimeError("추론 서버가 뜨지 않았다 (logs/ollama.log 확인)")

    def model_ready(self) -> bool:
        """이 모델이 추론 서버에 등록돼 있는가.

        없으면 번역 엔진은 **영어 페이지를 그대로 내놓고 종료 코드 0** 을
        돌려준다. 500쪽이면 서너 시간을 버린 뒤에야 안다. 시작 전에 3초로
        확인할 수 있는 것을 나중에 비싸게 알아낼 이유가 없다.
        """
        import json as _json
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.op}/api/tags", timeout=5) as r:
                names = [m.get("name", "") for m in _json.load(r).get("models", [])]
        except Exception:
            return False
        want = self.model.split(":")[0]
        return any(n.split(":")[0] == want for n in names)

    def start_proxy(self, python: str) -> None:
        if self.proxy_up():
            return                 # 우리 것이고 규칙·작업폴더까지 같다 — 이어 쓴다
        # 그 포트에 뭔가 있다면 남의 것이다(다른 실행의 프록시이거나 아예 다른
        # 서버). 죽이지 않고 비켜 간다.
        if self.identify(self.pp) is not None or not self._port_is_free(self.pp):
            self.pp = self._free_port(self.pp + 1)
        env = dict(os.environ)
        env.update({
            "UPSTREAM": f"http://127.0.0.1:{self.op}/v1",
            "MODEL": self.model,
            "CACHE_DB": str(self.work / "cache" / "trans.db"),
            "LOG_DIR": str(self.work / "logs"),
        })
        if self.glyphmap and self.glyphmap.exists():
            env["GLYPHMAP"] = str(self.glyphmap)
        else:
            env.pop("GLYPHMAP", None)   # 부모 환경에 남은 낡은 사전을 물려주지 않는다
        env["USER_RULES"] = self.user_sig
        log = open(self.work / "logs" / "proxy.log", "ab")
        self._procs.append(subprocess.Popen(
            [python, "-m", "uvicorn", "pdfko.proxy:app",
             "--host", "127.0.0.1", "--port", str(self.pp),
             "--log-level", "warning"],
            env=env, stdout=log, stderr=log, start_new_session=True))
        for _ in range(60):
            if self.proxy_up():
                return
            time.sleep(1)
        raise RuntimeError("프록시가 뜨지 않았다 (logs/proxy.log 확인)")

    def proxy_log_dir(self) -> Path | None:
        """지금 도는 프록시가 실제로 로그를 쌓는 곳. 재사용되면 남의 폴더다."""
        import json as _json
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.pp}/health", timeout=3) as r:
                d = _json.load(r).get("log_dir")
            return Path(d) if d else None
        except Exception:
            return None

    def stop(self) -> None:
        for p in self._procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        for p in self._procs:
            try:
                p.wait(timeout=5)        # 좀비를 남기지 않는다
            except Exception:
                pass
        self._procs.clear()
        if self in _LIVE:
            _LIVE.remove(self)


def stop_all() -> None:
    """이 프로세스가 띄운 서버를 전부 내린다.

    예전에는 `stop()` 호출이 **정상 종료 경로 한 곳뿐**이었다. 구간 실패,
    병합 실패, Ctrl-C, 그리고 웹은 아예 어느 경로에서도 부르지 않았다.
    그 결과 실측으로 12~18시간 묵은 프록시 세 개가 떠 있었고, 그것들이
    `--fresh` 를 무력화하고(삭제된 DB 의 inode 를 계속 쥔다) 포트 창을
    잠식했다. 종료 경로가 몇 개든 여기 한 번만 걸면 된다.
    """
    for s in list(_LIVE):
        try:
            s.stop()
        except Exception:
            pass


def ensure_model(work: Path, gguf: Path, tag: str, ollama_port: int) -> None:
    """GGUF 를 ollama 에 등록한다. 채팅 템플릿을 명시해야 한다.

    ollama 가 GGUF 메타데이터의 chat_template 을 못 읽는 경우가 있다.
    그러면 모델이 구조 없는 생 텍스트를 받아 **완전한 횡설수설**을 출력한다.
    `ollama show <tag> --template` 이 `{{ .Prompt }}` 만 보이면 이 증상이다.
    """
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"127.0.0.1:{ollama_port}"
    env["OLLAMA_MODELS"] = str(MODEL_STORE)
    have = subprocess.run(["ollama", "list"], env=env,
                          capture_output=True, text=True).stdout
    if tag in have:
        return
    mf = work / "Modelfile"
    mf.write_text(TEMPLATE_HY_MT2.format(gguf=gguf), encoding="utf-8")
    subprocess.run(["ollama", "create", tag, "-f", str(mf)], env=env, check=True)


# Hy-MT2 계열의 채팅 형식.
#   system 있음: <|startoftext|>{system}<|extra_4|>{user}<|extra_0|>
#   system 없음: <|startoftext|>{user}<|extra_0|>
TEMPLATE_HY_MT2 = '''FROM {gguf}

TEMPLATE """{{{{ if .System }}}}<|startoftext|>{{{{ .System }}}}<|extra_4|>{{{{ .Prompt }}}}<|extra_0|>{{{{ else }}}}<|startoftext|>{{{{ .Prompt }}}}<|extra_0|>{{{{ end }}}}{{{{ .Response }}}}"""

PARAMETER stop "<|eos|>"
PARAMETER stop "<|endoftext|>"
PARAMETER stop "<|startoftext|>"
PARAMETER num_ctx 4096
PARAMETER temperature 0.2
PARAMETER top_p 0.6
PARAMETER top_k 20
PARAMETER repeat_penalty 1.05
'''


def translate_chunk(chunk: Chunk, src: Path, work: Path, *,
                    model: str, proxy_port: int, glossary: Path | None,
                    prompt_file: Path | None, lang_out: str = "ko-KR",
                    qps: int = 30, workers: int = 16,
                    extra: list[str] | None = None) -> bool:
    """구간 하나를 번역한다. 성공하면 True.

    lang_out 이 'ko' 면 안 된다. BabelDOC 의 폰트 선택이
    `if "KR" in lang_code.upper()` 이라서 'KO' 에는 걸리지 않고 라틴 전용
    폰트로 떨어진다. 반드시 'ko-KR'.
    """
    chunk.outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "babeldoc", "--files", str(src),
        "--pages", chunk.name,
        "--lang-in", "en", "--lang-out", lang_out,
        "--openai", "--openai-model", model,
        "--openai-base-url", f"http://127.0.0.1:{proxy_port}/v1",
        "--openai-api-key", "sk-local",
        "--no-auto-extract-glossary",
        "--primary-font-family", "serif",
        "--watermark-output-mode", "no_watermark",
        "--only-include-translated-page",
        "--qps", str(qps), "--pool-max-workers", str(workers),
        "--working-dir", str(work / "work"),
        "--output", str(chunk.outdir),
    ]
    if glossary:
        cmd += ["--glossary-files", str(glossary)]
    if prompt_file and prompt_file.exists():
        cmd += ["--custom-system-prompt", prompt_file.read_text(encoding="utf-8")]
    if extra:
        cmd += extra
    # NOTE: --max-pages-per-part 는 쓰지 않는다. 번역 시작 직후
    # "Error in part 0" 로 즉시 죽는 문서가 있다.

    # 실행 전 산출물 목록을 찍어둔다. 이전 실행이 남긴 파일을 보고 성공으로
    # 착각하면 실패한 구간에 .done 이 찍혀 영영 건너뛰게 된다.
    before = {p.name: p.stat().st_mtime for p in chunk.outdir.glob("*.mono.pdf")}
    with (work / "logs" / f"part_{chunk.name}.log").open("ab") as log:
        r = subprocess.run(cmd, stdout=log, stderr=log)
    if r.returncode != 0:
        return False
    f = chunk.pdf()
    if f is None:
        return False
    if before.get(f.name, -1) >= f.stat().st_mtime:
        return False                      # 새로 만들어진 파일이 아니다
    chunk.mark_done()
    return True


def merge(chunks: list[Chunk], out: Path) -> int:
    """구간들을 하나로 합친다. 페이지 수를 돌려준다.

    구간이 하나라도 비어 있으면 **합치지 않고 멈춘다.** 조용히 건너뛰면
    이후 페이지 번호가 어긋나서, 검사기가 엉뚱한 원본 페이지와 비교하고
    자동 복구가 멀쩡한 번역 위에 엉뚱한 원문을 덮어쓴다.
    """
    import pymupdf
    missing = [c.name for c in chunks if not c.pdf()]
    if missing:
        raise RuntimeError(
            f"구간 PDF 가 없다: {', '.join(missing)} — "
            f"그대로 합치면 쪽 번호가 어긋나 검사·복구가 망가진다")
    doc = pymupdf.open()
    short = []
    for c in chunks:
        f = c.pdf()
        if f:
            with pymupdf.open(f) as s:
                # 구간 파일이 **있기만** 하면 통과시키면 안 된다. 엔진이 일부
                # 페이지만 내놓는 경우가 있고, 그러면 이후 쪽번호가 통째로
                # 밀린다. 실측: 3쪽 구간이 2쪽만 나온 6쪽 문서에서 마지막
                # 페이지가 사라지고, 자동 복구가 3~5쪽에 **엉뚱한 원본**을
                # 끼워 넣었다. 그러고도 경고 한 줄 없었다.
                want = c.last - c.first + 1
                if s.page_count != want:
                    short.append(f"{c.name}({s.page_count}/{want}쪽)")
                doc.insert_pdf(s)
    n = doc.page_count
    if n == 0:
        doc.close()
        raise RuntimeError("합칠 구간이 하나도 없다 — 먼저 번역을 돌려야 한다")
    if short:
        doc.close()
        raise RuntimeError(
            f"구간의 쪽수가 모자란다: {', '.join(short)} — 그대로 합치면 "
            f"쪽 번호가 어긋난다. 해당 구간의 .done 을 지우고 다시 실행할 것")
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return n


def cleanup_work(work: Path) -> None:
    shutil.rmtree(work / "work", ignore_errors=True)
    # 페이지 복구 중간물도 함께 지운다. 남겨 두면 다음 실행이 같은 번호의
    # **다른 페이지** 결과를 집어 들 수 있다.
    shutil.rmtree(work / "repair", ignore_errors=True)
