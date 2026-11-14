"""쇼츠 편집기 — 1단계: 영상 불러오기 + 재생 + 구간 표시.

실행: python run.py  (Windows는 start.bat 더블클릭)
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from recommend import MODEL, RecommendError, Recommender, has_key, load_env
from transcribe import DEFAULT_MODEL, MODELS, Transcriber

BASE = Path(__file__).resolve().parent
load_env(BASE / ".env")  # ANTHROPIC_API_KEY 등 (값은 화면·로그에 내보내지 않는다)
STATIC = BASE / "static"
WORK = Path(os.environ.get("SHORTS_WORK_DIR", BASE / "work")).expanduser()
WORK.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = WORK / "config.json"
REGISTRY_FILE = WORK / "videos.json"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}
SCAN_DEPTH = 2
MAX_SCAN = 400

app = FastAPI(title="쇼츠 편집기")


# ---------- 저장소(설정·영상 목록) ----------
def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def default_scan_dirs() -> list[str]:
    home = Path.home()
    cands = [home / "Videos", home / "Downloads", home / "Desktop", home / "Movies"]
    return [str(p) for p in cands if p.is_dir()]


def get_config() -> dict:
    cfg = _load(CONFIG_FILE, {})
    cfg.setdefault("scan_dirs", default_scan_dirs())
    return cfg


registry: dict[str, str] = _load(REGISTRY_FILE, {})  # id -> 절대경로


def video_id(path: Path) -> str:
    # 경로를 URL에 직접 쓰지 않는다. ([4기], 한글, 공백 문제 방지)
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def register(path: Path) -> str:
    vid = video_id(path)
    if registry.get(vid) != str(path):
        registry[vid] = str(path)
        _save(REGISTRY_FILE, registry)
    return vid


# ---------- 경로 처리 ----------
def clean_path(raw: str) -> Path:
    """붙여넣은 경로 정리: 따옴표·file://·환경변수·~ 처리."""
    s = (raw or "").strip().strip('"').strip("'").strip()
    s = re.sub(r"^file:///?", "", s)
    s = os.path.expandvars(s)
    return Path(s).expanduser()


def explain_missing(p: Path) -> str:
    msg = f"파일을 찾을 수 없습니다: {p}"
    parent = p.parent
    if parent.is_dir():
        names = [x.name for x in parent.iterdir() if x.suffix.lower() in VIDEO_EXT]
        close = difflib.get_close_matches(p.name, names, n=3, cutoff=0.4)
        if close:
            msg += "\n혹시 이 파일인가요? → " + " / ".join(close)
        else:
            msg += "\n폴더는 있지만 같은 이름의 영상이 없습니다. 파일 이름을 확인해 주세요."
    else:
        msg += "\n폴더 자체가 없습니다. 다른 PC에서 만든 경로이거나 파일이 이동됐을 수 있습니다."
    return msg


def probe(path: Path) -> dict:
    info = {"duration": None, "width": None, "height": None}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        data = json.loads(out.stdout or "{}")
        info["duration"] = float(data.get("format", {}).get("duration") or 0) or None
        for st in data.get("streams", []):
            if st.get("codec_type") == "video":
                info["width"], info["height"] = st.get("width"), st.get("height")
                info["codec"] = st.get("codec_name")
                break
    except FileNotFoundError:
        info["warning"] = "ffprobe가 없어 길이를 읽지 못했습니다. ffmpeg 설치가 필요합니다."
    except Exception as e:  # noqa: BLE001
        info["warning"] = f"영상 정보를 읽지 못했습니다: {e}"
    return info


def describe(path: Path, with_probe: bool = False) -> dict:
    st = path.stat()
    d = {
        "id": register(path),
        "name": path.name,
        "folder": str(path.parent),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }
    if with_probe:
        d.update(probe(path))
    return d


# ---------- API ----------
@app.get("/api/config")
def api_config():
    cfg = get_config()
    return {"work_dir": str(WORK), "scan_dirs": cfg["scan_dirs"]}


@app.get("/api/videos")
def api_videos():
    """내 PC의 영상 폴더를 훑어 최신순으로 보여준다."""
    found: list[dict] = []
    for d in get_config()["scan_dirs"]:
        root = Path(d)
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - base_depth >= SCAN_DEPTH:
                dirnames[:] = []
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for fn in filenames:
                if Path(fn).suffix.lower() in VIDEO_EXT:
                    try:
                        found.append(describe(Path(dirpath) / fn))
                    except OSError:
                        pass
            if len(found) >= MAX_SCAN:
                break
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return {"videos": found[:MAX_SCAN]}


class OpenReq(BaseModel):
    path: str


@app.post("/api/open")
def api_open(req: OpenReq):
    p = clean_path(req.path)
    if not p.is_file():
        raise HTTPException(404, explain_missing(p))
    if p.suffix.lower() not in VIDEO_EXT:
        raise HTTPException(400, f"영상 파일이 아닙니다 ({p.suffix or '확장자 없음'}). 지원: {', '.join(sorted(VIDEO_EXT))}")
    return describe(p, with_probe=True)


@app.get("/api/video/{vid}")
def api_video(vid: str):
    path = registry.get(vid)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "영상을 찾을 수 없습니다. 목록에서 다시 선택해 주세요.")
    return describe(Path(path), with_probe=True)


class FolderReq(BaseModel):
    path: str


@app.post("/api/folders")
def api_add_folder(req: FolderReq):
    p = clean_path(req.path)
    if not p.is_dir():
        raise HTTPException(404, f"폴더를 찾을 수 없습니다: {p}")
    cfg = get_config()
    if str(p) not in cfg["scan_dirs"]:
        cfg["scan_dirs"].append(str(p))
        _save(CONFIG_FILE, cfg)
    return {"scan_dirs": cfg["scan_dirs"]}


@app.delete("/api/folders")
def api_remove_folder(path: str):
    cfg = get_config()
    cfg["scan_dirs"] = [d for d in cfg["scan_dirs"] if d != path]
    _save(CONFIG_FILE, cfg)
    return {"scan_dirs": cfg["scan_dirs"]}


# ---------- 2단계: 자막 ----------
transcriber = Transcriber(WORK)


def _video_path(vid: str) -> Path:
    path = registry.get(vid)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "영상을 찾을 수 없습니다. 목록에서 다시 선택해 주세요.")
    return Path(path)


class TranscribeReq(BaseModel):
    id: str
    model: str = DEFAULT_MODEL
    start: float = 0.0
    end: Optional[float] = None


@app.get("/api/transcribe/models")
def api_models():
    return {"models": [{"name": k, "label": v} for k, v in MODELS.items()], "default": DEFAULT_MODEL}


@app.post("/api/transcribe")
def api_transcribe(req: TranscribeReq):
    video = _video_path(req.id)
    try:
        return transcriber.start(req.id, video, req.model, req.start, req.end)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/transcribe/{vid}")
def api_transcribe_status(vid: str):
    return transcriber.status(vid)


@app.post("/api/transcribe/{vid}/cancel")
def api_transcribe_cancel(vid: str):
    return transcriber.cancel(vid)


@app.get("/api/transcript/{vid}")
def api_transcript(vid: str):
    r = transcriber.result(vid)
    if not r:
        raise HTTPException(404, "아직 자막이 없습니다.")
    return r


# ---------- 3단계: Claude 추천 ----------
recommender = Recommender(WORK)


class RecommendReq(BaseModel):
    id: str
    count: int = 5
    hint: str = ""


@app.get("/api/recommend/info")
def api_recommend_info():
    return {"has_key": has_key(), "model": MODEL}


@app.post("/api/recommend")
def api_recommend(req: RecommendReq):
    _video_path(req.id)
    tr = transcriber.result(req.id)
    if not tr:
        raise HTTPException(400, "먼저 ③ 에서 자막을 만들어 주세요.")
    try:
        return recommender.start(req.id, tr["segments"], req.count, req.hint.strip())
    except RecommendError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/recommend/{vid}")
def api_recommend_status(vid: str):
    return recommender.status(vid)


@app.get("/api/candidates/{vid}")
def api_candidates(vid: str):
    r = recommender.result(vid)
    if not r:
        raise HTTPException(404, "아직 추천 결과가 없습니다.")
    return r


@app.get("/media/{vid}")
def media(vid: str):
    """FileResponse가 Range 요청(구간 탐색)을 처리한다."""
    path = registry.get(vid)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    return FileResponse(path)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
