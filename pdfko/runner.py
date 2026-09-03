"""번역 실행기 — 구간 분할, 이어하기, 서버 수명 관리.

## 왜 구간으로 쪼개나

BabelDOC 은 문서를 끝까지 처리해야 PDF 를 내놓는다. 500쪽을 한 번에 돌리다
중간에 죽으면 몇 시간을 쓰고도 산출물이 0이다. 구간마다 PDF 를 떨어뜨리면
죽어도 거기까지는 남고, 다시 실행하면 완료된 구간을 건너뛴다.

## 캐시가 세 겹이라는 함정

  1. 구간 `.done` 표식      — 이 모듈이 관리
  2. 프록시 SQLite 캐시      — 성공한 문단만 저장 (실패는 저장하지 않는다)
  3. BabelDOC 자체 캐시      — 우리가 통제할 수 없다

**1번은 설정 지문을 같이 적는다.** 그냥 "끝났다"만 남기면, pdfko 를 고쳐
놓고 같은 파일을 다시 돌렸을 때 옛 결과가 조용히 그대로 나온다. 실측으로
겪었다 — 줄 분리 배율을 새로 넣고 재실행했더니 `1-23 건너뜀 (완료됨)` 만
찍히고 옛 PDF 가 나왔다. `settings_stamp` 가 그 지문이다.

**3번을 우회하지 않으면 2번을 고쳐도 소용이 없다.** BabelDOC 이 자기 캐시에서
바로 꺼내 쓰면서 프록시를 통째로 지나치기 때문이다. 그래서 엔진을 부를 때마다
`--ignore-cache` 를 준다. 예전에는 그 파일을 지웠는데, 계정에 하나뿐이라
동시에 도는 다른 실행의 DB 까지 날렸다.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Chunk:
    first: int
    last: int
    outdir: Path

    @property
    def name(self) -> str:
        return f"{self.first}-{self.last}"

    def done(self, stamp: str = "") -> bool:
        """끝났고, **그때와 같은 설정인가**.

        표식이 "끝났다"만 기록하면 함정이 된다. pdfko 를 고쳐 놓고 같은
        파일을 다시 돌려도 옛 결과가 조용히 그대로 나온다 — 실측으로
        겪었다. 줄 분리 배율을 새로 넣고 재실행했더니 `1-23 건너뜀
        (완료됨)` 만 찍히고 옛 PDF 가 나왔다.

        무슨 설정으로 만든 것인지 같이 보면, 이어하기는 살고 함정은 사라진다.
        """
        f = self.outdir / ".done"
        try:
            return f.read_text(encoding="utf-8").strip() == stamp
        except OSError:
            return False

    def mark_done(self, stamp: str = "") -> None:
        (self.outdir / ".done").write_text(stamp, encoding="utf-8")

    def pdf(self) -> Path | None:
        # 최신 파일을 고른다. 알파벳순으로 뽑으면 원본으로 돌린 옛 결과가
        # 청소본 결과를 이긴다 (RLbook… < cleaned…).
        f = sorted(self.outdir.glob("*.mono.pdf"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
        return f[0] if f else None


# 실행마다 달라지지만 번역 결과는 바꾸지 않는 값들. 표식에 넣으면
# 이어하기가 죽는다 — 미들웨어 포트는 실행마다 새로 잡히므로, 중단된
# 번역을 이어가려 해도 매번 표식이 어긋나 처음부터 다시 돈다.
_VOLATILE = {"--openai-base-url", "--working-dir", "--output", "--pages",
             "--qps", "--pool-max-workers", "--files"}


def settings_stamp(cmd: list[str]) -> str:
    """번역 방식의 지문. 이게 바뀌면 끝난 구간도 다시 번역한다.

    pdfko 가 번역을 바꾸는 길은 둘이다 — babeldoc 에 넘기는 인자와, 프록시의
    검증·수리 규칙. 인자만 보면 규칙을 고쳐 놓고 다시 돌렸을 때 옛 결과가
    조용히 그대로 나온다. 규칙 쪽은 프록시가 자기 캐시를 무효화하려고 이미
    지문을 들고 있으니 그것을 같이 쓴다.
    """
    import hashlib
    from . import proxy
    keep, skip = [proxy._rules_fingerprint()], False
    for a in cmd:
        if skip:
            skip = False
            continue
        if a in _VOLATILE:
            skip = True
            continue
        keep.append(a)
    return hashlib.sha256("\x00".join(keep).encode()).hexdigest()[:16]


def looks_like_slides(doc) -> bool:
    """발표자료인가 — 쪽이 가로로 넓은가.

    babeldoc 의 `--split-short-lines` 를 켤지 정하는 데 쓴다. 발표자료에서는
    켜는 것이 옳다. 화살표·목록 항목이 원래 각자 한 줄인데, 켜지 않으면
    한 덩어리로 합쳐져 `입력↓` 처럼 붙어 버린다.

    교재에 켜면 정반대가 된다. 실측(548쪽 교재)으로 121쪽은 44줄 **전부**가
    분리 대상이었다 — 이어지는 문단이 줄마다 토막나 문맥 없이 번역된다.

    가르는 신호는 쪽 모양이다. 실측: 발표자료 1.78, 교재 0.78. 내용이 아니라
    문서 형식의 성질이라 흔들리지 않는다.
    """
    try:
        r = doc[0].rect
        return bool(r.width > r.height)
    except Exception:
        return False


def split_factor(src) -> list[str]:
    """줄 분리 배율 — 발표자료면 높이고, 교재면 babeldoc 기본값에 맡긴다.

    배율은 **쪽 중앙값 줄 너비에 대한 비율**이다. 이보다 좁은 줄만 따로 뗀다.

    기본 0.8 은 아주 짧은 줄만 떼어 목록 항목 대부분을 놓친다. 실측(마지막
    쪽, 중앙값 199pt · 문턱 160pt):

        135pt  분리됨  '1. Agent != LLM'
        199pt  안 됨   '2. Agent != Tool Calling'
        484pt  안 됨   '3. Agent 의 핵심은 ...'

    3.0 이면 다섯 항목이 모두 제자리를 찾는다. 교재에 쓰면 정반대가 된다 —
    실측(548쪽 교재)으로 121쪽은 44줄 **전부**가 분리 대상이었다.
    """
    import pymupdf
    try:
        with pymupdf.open(src) as d:
            if not looks_like_slides(d):
                return []
    except Exception:
        return []
    return ["--short-line-split-factor", "3.0"]


def plan_chunks(first: int, last: int, size: int, root: Path) -> list[Chunk]:
    if size < 1:
        raise ValueError(f"구간 크기는 1 이상이어야 한다 (받은 값: {size})")
    out, s = [], first
    while s <= last:
        e = min(s + size - 1, last)
        out.append(Chunk(s, e, root / "parts" / f"{s}-{e}"))
        s = e + 1
    return out


# 모델 저장소는 **ollama 자신의 기본 위치**를 쓴다.
#
# 우리만의 폴더를 쓰면 저장소가 갈라진다. 사용자가 `ollama serve` 를 이미
# 띄워 놨으면 그 서버는 자기 저장소(`~/.ollama/models`)를 보는데, 우리가
# 나중에 서버를 띄우면 우리 폴더를 보면서 "모델이 없다" 고 한다. 사용자가
# 다시 등록하면 **똑같은 6GB 가 한 벌 더 생긴다.** 실측으로 이 컴퓨터에
# 같은 blob 해시를 가진 저장소가 두 개, 11.6GB 쌓여 있었다.
#
# 예전에는 작업 폴더 안(`<work>/models/ollama`)이라 책마다 한 벌씩 생겼다.
MODEL_STORE = Path(
    os.environ.get("PDFKO_MODELS", Path.home() / ".ollama" / "models"))


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
        self.vocab: Path | None = None      # 원본 어휘 (cli 가 채운다)
        self.columns: Path | None = None    # 도식 열 간격 (cli 가 채운다)
        self.user_sig: str = ""             # 추가 지시문 지문 (cli 가 채운다)
        self.borrowed = False               # 이미 떠 있던 ollama 를 빌려 쓰는가
        self._procs: list[subprocess.Popen] = []
        _LIVE.append(self)

    def expected_rules(self) -> str:
        """이 서버가 띄울 프록시가 가져야 할 규칙 지문.

        합자 사전이나 추가 지시문을 붙이면 자식의 지문이 부모와 달라진다. 부모의
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
        """추가 지시문 파일 내용의 지문. 바뀌면 캐시가 무효화되어야 한다."""
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
    def model_store(self) -> Path | None:
        """지금 그 포트에서 도는 서버가 실제로 쓰는 저장소.

        우리가 띄운 서버가 아니면 우리가 정한 값과 다를 수 있다. 그걸
        모른 척하면 "등록은 한 번이면 된다"는 약속이 조용히 깨진다.
        """
        import re as _re
        pid = self._ollama_pid()
        if pid is None:
            return None
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
        except OSError:
            return None
        m = _re.search(r"OLLAMA_MODELS=([^\x00]+)", env)
        return Path(m.group(1)) if m else Path.home() / ".ollama" / "models"

    def _ollama_pid(self) -> int | None:
        import subprocess as _sp
        out = _sp.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if f":{self.op} " in line and "pid=" in line:
                try:
                    return int(line.split("pid=")[1].split(",")[0])
                except (IndexError, ValueError):
                    return None
        return None

    def start_ollama(self) -> None:
        if self.ollama_up():
            self.borrowed = True      # 우리가 띄운 게 아니다 — 저장소도 남의 것
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

    def drop_own_proxy(self) -> bool:
        """이 작업 폴더의 프록시가 떠 있으면 내린다. 우리 것만 건드린다.

        `stop_all()` 은 **이 프로세스가 띄운** 것만 안다. 앞선 실행이
        SIGKILL·크래시로 죽으면 프록시가 고아로 남는데, 그것이 삭제된 캐시
        파일의 inode 를 계속 쥐고 있어서 `--fresh` 가 조용히 아무 일도 하지
        않게 된다. cache_db 가 우리 것일 때만 내린다.
        """
        d = self.identify(self.pp)
        if not d or d.get("cache_db") != str(self.work / "cache" / "trans.db"):
            return False
        pid = d.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        for _ in range(20):
            if self._port_is_free(self.pp):
                return True
            time.sleep(0.5)
        return True

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
        if self.vocab and self.vocab.exists():
            env["PDFKO_VOCAB"] = str(self.vocab)
        else:
            env.pop("PDFKO_VOCAB", None)
        if self.columns and self.columns.exists():
            env["PDFKO_COLUMNS"] = str(self.columns)
        else:
            env.pop("PDFKO_COLUMNS", None)
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
    # `OLLAMA_MODELS` 를 여기서 정해 봐야 소용없다. `ollama create` 와
    # `ollama list` 는 둘 다 **서버 쪽** 동작이라 클라이언트 환경변수를 보지
    # 않는다. 실측으로 없는 경로를 줘도 서버 저장소의 목록이 그대로 나왔다.
    # 등록은 지금 그 포트에서 도는 서버의 저장소로 들어간다.
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"127.0.0.1:{ollama_port}"
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


def babeldoc_cmd(src: Path, work: Path, pages: str, outdir: Path, *,
                 model: str, proxy_port: int, prompt_file: Path | None,
                 lang_out: str = "ko-KR", qps: int = 30, workers: int = 8,
                 extra: list[str] | None = None) -> list[str]:
    """엔진에 넘길 명령줄. `settings_stamp` 가 이 결과를 지문으로 삼는다.

    조립을 한 곳에 모아 두는 이유는 지문 때문이다. 호출부가 "이 설정으로
    끝난 구간인가"를 물으려면 실제로 쓸 명령줄과 **같은 것**을 볼 수
    있어야 한다. 두 벌로 나뉘면 한쪽만 고쳐져 지문이 거짓말을 한다.
    """
    cmd = [
        "babeldoc", "--files", str(src),
        "--pages", pages,
        "--lang-in", "en", "--lang-out", lang_out,
        "--openai", "--openai-model", model,
        "--openai-base-url", f"http://127.0.0.1:{proxy_port}/v1",
        "--openai-api-key", "sk-local",
        # 엔진 캐시를 **쓰지 않는다**. 예전에는 실행마다 남의 것까지 통째로
        # 지웠다 — 이 캐시는 `~/.cache/babeldoc` 하나뿐이라, 두 번역을 같이
        # 돌리면 나중 것이 먼저 것의 살아 있는 DB 를 지운다. 실측으로 다른
        # 실행이 `(deleted)` 핸들을 쥔 채 도는 것을 관찰했고, WAL 짝이 어긋나
        # `database disk image is malformed` 로 가는 길이다. 우리 미들웨어를
        # 우회하는 것만 막으면 되므로 이 실행만 안 쓰면 충분하다.
        "--ignore-cache",
        "--no-auto-extract-glossary",
        # 기본값 5 는 `Yes`(3자)·`No`(2자) 같은 도식 라벨을 통째로 건너뛴다.
        # 번역본에 영어가 그대로 남고, 그림혼재로 잡힌다. 짧은 것도 보낸다 —
        # 프록시가 산문 여부를 따로 보므로 쓰레기까지 번역하지는 않는다.
        "--min-text-length", "1",
        "--split-short-lines",
        *split_factor(src),
        "--primary-font-family", "serif",
        "--watermark-output-mode", "no_watermark",
        "--only-include-translated-page",
        # 동시 요청 수는 슬롯 수(OLLAMA_NUM_PARALLEL=8)에 맞춘다. 더 보낸다고
        # 빨라지지 않는다 — 추론만 재면 4개 91, **8개 122**, 16개 112,
        # 24개 89 tok/s 다. 슬롯보다 많이 보내면 KV 캐시 경쟁만 늘어난다.
        #
        # 다만 종단간 효과는 그만큼 크지 않다. 4쪽 구간을 번갈아 재니
        # 8개 203.9·210.0초, 16개 210.6·213.3초 — 2.4% 다. 엔진 기동과
        # 조판 해석 같은 고정 비용이 추론 차이를 희석한다. 그래도 두 쌍 모두
        # 8개가 빨랐고 공짜라서 맞춘다.
        "--qps", str(qps), "--pool-max-workers", str(workers),
        "--working-dir", str(work / "work"),
        "--output", str(outdir),
    ]
    if prompt_file and prompt_file.exists():
        cmd += ["--custom-system-prompt", prompt_file.read_text(encoding="utf-8")]
    if extra:
        cmd += extra
    return cmd


def translate_chunk(chunk: Chunk, src: Path, work: Path, *,
                    model: str, proxy_port: int,
                    prompt_file: Path | None, lang_out: str = "ko-KR",
                    qps: int = 30, workers: int = 8,
                    extra: list[str] | None = None) -> bool:
    """구간 하나를 번역한다. 성공하면 True.

    lang_out 이 'ko' 면 안 된다. BabelDOC 의 폰트 선택이
    `if "KR" in lang_code.upper()` 이라서 'KO' 에는 걸리지 않고 라틴 전용
    폰트로 떨어진다. 반드시 'ko-KR'.
    """
    chunk.outdir.mkdir(parents=True, exist_ok=True)
    cmd = babeldoc_cmd(src, work, chunk.name, chunk.outdir, model=model,
                       proxy_port=proxy_port, prompt_file=prompt_file,
                       lang_out=lang_out, qps=qps, workers=workers,
                       extra=extra)
    # NOTE: --max-pages-per-part 는 쓰지 않는다. 번역 시작 직후
    # "Error in part 0" 로 즉시 죽는 문서가 있다.

    # 실행 전 산출물 목록을 찍어둔다. 이전 실행이 남긴 파일을 보고 성공으로
    # 착각하면 실패한 구간에 .done 이 찍혀 영영 건너뛰게 된다.
    before = {p.name: p.stat().st_mtime for p in chunk.outdir.glob("*.mono.pdf")}
    # 실행마다 새로 쓴다. 이어 붙이면 512KB 까지 쌓여서, 문제가 생겼을 때
    # 어느 실행의 기록인지 찾을 수 없다. 직전 것은 .1 로 남긴다.
    lp = work / "logs" / f"part_{chunk.name}.log"
    if lp.exists():
        lp.replace(lp.with_suffix(".log.1"))
    with lp.open("wb") as log:
        r = subprocess.run(cmd, stdout=log, stderr=log)
    if r.returncode != 0:
        return False
    f = chunk.pdf()
    if f is None:
        return False
    if before.get(f.name, -1) >= f.stat().st_mtime:
        return False                      # 새로 만들어진 파일이 아니다
    # 파일이 생겼다고 번역이 된 것은 아니다. 상류가 죽어 있으면 프록시가
    # 원문을 그대로 돌려주고 엔진은 성공한다. 여기서 안 보면 그 구간에
    # `.done` 이 찍혀 **다시 실행해도 영영 건너뛴다.** 합친 뒤에 알아차려도
    # 고칠 방법이 `--fresh` 로 전부 다시 돌리는 것뿐이다.
    from . import qa
    judged, empty = qa.coverage(str(f))
    if judged and len(empty) == judged:
        return False
    chunk.mark_done(settings_stamp(cmd))
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
            try:
                s = pymupdf.open(f)
            except RuntimeError as e:
                # 어느 구간인지, 무엇을 하면 되는지 말한다. 예전에는
                # `'/dev/null' is no file` 같은 mupdf 원문만 나왔다.
                raise RuntimeError(
                    f"{c.name} 구간 PDF 를 읽을 수 없습니다 ({f.name}): {e}\n"
                    f"  {c.outdir}/.done 을 지우고 다시 실행하세요") from e
            with s:
                # 구간 파일이 **있기만** 하면 통과시키면 안 된다. 엔진이 일부
                # 페이지만 내놓는 경우가 있고, 그러면 이후 쪽번호가 통째로
                # 밀린다. 실측: 3쪽 구간이 2쪽만 나온 6쪽 문서에서 마지막
                # 페이지가 사라지고, 자동 복구가 3~5쪽에 **엉뚱한 원본**을
                # 끼워 넣었다. 그러고도 경고 한 줄 없었다.
                want = c.last - c.first + 1
                if s.page_count != want:
                    short.append(f"{c.name}({s.page_count}/{want}쪽)")
                # 쪽수만 봐서는 부족하다. 파일이 중간에서 잘리면 pymupdf 가
                # 페이지 트리를 복원해 **쪽수는 맞고 내용은 빈** 문서를
                # 내놓는다. 실측으로 절반이 잘린 구간이 빈 페이지 3장으로
                # 병합됐고, 뒤쪽 검사도 전부 통과했다 — qa.coverage 는 글자가
                # 적은 쪽을 판정 보류로 넘기고 qa.inspect_page 도 마찬가지다.
                # 빈 책이 "완료 · 파손 0쪽"으로 나간다.
                elif all(len(s[i].get_text().strip()) < 20
                         for i in range(s.page_count)):
                    short.append(f"{c.name}(전 쪽이 비었습니다 — 파일이 잘렸습니다)")
                doc.insert_pdf(s)
    n = doc.page_count
    if n == 0:
        doc.close()
        raise RuntimeError("합칠 구간이 하나도 없다 — 먼저 번역을 돌려야 한다")
    if short:
        doc.close()
        raise RuntimeError(
            f"구간이 온전하지 않습니다: {', '.join(short)}\n"
            f"  그대로 합치면 쪽 번호가 어긋나거나 빈 페이지가 들어갑니다.\n"
            f"  해당 구간의 .done 을 지우고 다시 실행하세요")
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return n


def cleanup_work(work: Path) -> None:
    shutil.rmtree(work / "work", ignore_errors=True)
    # 페이지 복구 중간물도 함께 지운다. 남겨 두면 다음 실행이 같은 번호의
    # **다른 페이지** 결과를 집어 들 수 있다.
    shutil.rmtree(work / "repair", ignore_errors=True)
