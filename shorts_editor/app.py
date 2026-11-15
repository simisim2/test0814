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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from music import MusicError, MusicLib
from recommend import MODEL, RecommendError, Recommender, has_key, load_env, recommend_music
from render import LAYOUTS, THEMES, TRANSITIONS, RenderError, Renderer, clean_design, list_fonts, preview_png
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


# ---------- 4단계: 구간(쇼츠) 서버 저장 ----------
CLIPS_DIR = WORK / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
MAX_CLIPS = 200
CLIP_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


class Clip(BaseModel):
    id: str
    start: float
    end: float
    title: str = ""
    note: str = ""
    # 5단계 디자인 설정 (값 검증은 render.clean_design 에서)
    kicker: str = ""
    lines: list[str] = []
    footer: str = ""
    layout: str = "card"
    theme: str = "midnight"
    accent: str = ""
    subs: bool = True
    crop_x: int = 50
    font: str = ""
    sub_font: str = ""
    # 이어붙이기·전환·음악·페이드 (검증은 render.clean_design)
    extra: list[dict] = []
    trans: str = "fade"
    trans_dur: float = 0.5
    music: str = ""
    music_vol: int = 25
    music_fade_in: float = 1.0
    music_fade_out: float = 2.0
    music_start: float = 0.0
    duck: bool = True
    mute_orig: bool = False
    vfade: float = 0.0


class ClipsReq(BaseModel):
    clips: list[Clip]


def _clips_file(vid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", vid):       # 파일 경로로 쓰이므로 id 형식을 엄격히 확인
        raise HTTPException(400, "잘못된 영상 id 입니다.")
    return CLIPS_DIR / f"{vid}.json"


@app.get("/api/clips/{vid}")
def api_get_clips(vid: str):
    return _load(_clips_file(vid), {"clips": []})


@app.put("/api/clips/{vid}")
def api_put_clips(vid: str, req: ClipsReq):
    f = _clips_file(vid)
    if len(req.clips) > MAX_CLIPS:
        raise HTTPException(400, f"구간은 최대 {MAX_CLIPS}개까지 저장할 수 있어요.")
    seen, clean = set(), []
    for c in req.clips:
        if not (c.start == c.start and c.end == c.end) or c.start < 0 or c.end <= c.start:   # NaN·음수·역순 방지
            raise HTTPException(400, f"구간이 올바르지 않아요 (시작 {c.start}, 끝 {c.end}). 끝이 시작보다 뒤여야 해요.")
        if not CLIP_ID_RE.fullmatch(c.id):
            raise HTTPException(400, "구간 id 형식이 올바르지 않아요.")
        if c.id in seen:
            raise HTTPException(400, "구간 id 가 중복됐어요.")
        seen.add(c.id)
        item = {"id": c.id, "start": round(c.start, 2), "end": round(c.end, 2),
                "title": c.title.strip()[:80], "note": c.note.strip()[:500]}
        item.update(clean_design(c.model_dump()))
        clean.append(item)
    clean.sort(key=lambda x: x["start"])
    tmp = f.with_suffix(".json.tmp")
    _save(tmp, {"clips": clean})
    tmp.replace(f)
    return {"clips": clean}


# ---------- 5단계: 렌더 / 미리보기 / 글꼴 ----------
renderer = Renderer(WORK)
PREVIEW_DIR = WORK / "preview"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def _find_clip(vid: str, clip_id: str) -> dict:
    if not CLIP_ID_RE.fullmatch(clip_id):
        raise HTTPException(400, "구간 id 형식이 올바르지 않아요.")
    for c in _load(_clips_file(vid), {"clips": []})["clips"]:
        if c["id"] == clip_id:
            return c
    raise HTTPException(404, "저장된 구간을 찾을 수 없어요. 먼저 '구간 저장'을 눌러 주세요.")


def _segments(vid: str) -> list[dict]:
    tr = transcriber.result(vid)
    return tr["segments"] if tr else []


@app.get("/api/render/options")
def api_render_options():
    return {
        "layouts": [{"id": k, "label": v} for k, v in LAYOUTS.items()],
        "themes": [{"id": k, "label": v["label"], "bg": list(v["bg"]), "accent": v["accent"], "text": v["text"]}
                   for k, v in THEMES.items()],
        "fonts": [{"id": f["id"], "label": f["label"], "recommended": f["recommended"], "mine": f["mine"]}
                  for f in list_fonts()],
        "fonts_dir": str(BASE / "fonts"),
        "transitions": [{"id": k, "label": v} for k, v in TRANSITIONS.items()],
    }


def _resolve_parts(vid: str, clip: dict) -> list[dict]:
    """메인 구간 + 이어붙일 구간들을 (영상 파일, 시작, 끝, 자막) 목록으로 푼다."""
    parts = [{"video": _video_path(vid), "start": clip["start"], "end": clip["end"], "segments": _segments(vid)}]
    for i, ref in enumerate(clean_design(clip)["extra"], start=1):
        f = _clips_file(ref["vid"])
        c = next((x for x in _load(f, {"clips": []})["clips"] if x["id"] == ref["id"]), None)
        if not c:
            raise HTTPException(404, f"이어붙인 {i}번째 구간을 찾을 수 없어요. 삭제됐다면 목록에서 빼 주세요.")
        parts.append({"video": _video_path(ref["vid"]), "start": c["start"], "end": c["end"],
                      "segments": _segments(ref["vid"])})
    return parts


def _music_path(design: dict) -> Optional[Path]:
    if not design["music"]:
        return None
    try:
        return music.path(design["music"])
    except MusicError as e:
        raise HTTPException(404, str(e))


@app.get("/api/render/preview/{vid}/{clip_id}")
def api_render_preview(vid: str, clip_id: str):
    clip = _find_clip(vid, clip_id)
    part = _resolve_parts(vid, clip)[0]
    out = PREVIEW_DIR / f"{vid}_{clip_id}.png"
    try:
        preview_png(part, clean_design(clip), out)
    except RenderError as e:
        raise HTTPException(500, str(e))
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/render/{vid}/{clip_id}")
def api_render_start(vid: str, clip_id: str):
    clip = _find_clip(vid, clip_id)
    parts = _resolve_parts(vid, clip)
    mpath = _music_path(clean_design(clip))
    try:
        return renderer.start(vid, clip_id, parts, clip, mpath)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/render/{vid}/{clip_id}")
def api_render_status(vid: str, clip_id: str):
    _clips_file(vid)
    if not CLIP_ID_RE.fullmatch(clip_id):
        raise HTTPException(400, "구간 id 형식이 올바르지 않아요.")
    return renderer.status(vid, clip_id)


@app.post("/api/render/{vid}/{clip_id}/cancel")
def api_render_cancel(vid: str, clip_id: str):
    _clips_file(vid)
    if not CLIP_ID_RE.fullmatch(clip_id):
        raise HTTPException(400, "구간 id 형식이 올바르지 않아요.")
    return renderer.cancel(vid, clip_id)


@app.get("/api/out/{vid}/{clip_id}")
def api_out(vid: str, clip_id: str, download: int = 0):
    _clips_file(vid)
    if not CLIP_ID_RE.fullmatch(clip_id):
        raise HTTPException(400, "구간 id 형식이 올바르지 않아요.")
    p = renderer.out_path(vid, clip_id)
    if not p.is_file():
        raise HTTPException(404, "아직 만들어진 쇼츠가 없어요.")
    name = f"shorts_{clip_id}.mp4" if download else None
    return FileResponse(p, media_type="video/mp4", filename=name, headers={"Cache-Control": "no-store"})


# ---------- 배경음악 · 구간 모아보기 ----------
music = MusicLib(WORK)


@app.get("/api/all_clips")
def api_all_clips():
    """이어붙일 구간을 고를 수 있도록, 모든 영상의 저장된 구간을 모아 보여준다."""
    out = []
    for f in sorted(CLIPS_DIR.glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{12}", f.stem):
            continue
        path = registry.get(f.stem)
        name = Path(path).name if path else f.stem
        for c in _load(f, {"clips": []})["clips"]:
            out.append({"vid": f.stem, "video": name, "id": c["id"], "start": c["start"], "end": c["end"],
                        "title": c.get("title", "")})
    return {"clips": out}


@app.get("/api/music")
def api_music_list():
    return {"music": music.list(), "has_key": has_key()}


@app.post("/api/music/upload")
async def api_music_upload(request: Request, name: str):
    try:
        return await music.save_stream(name, request.stream())
    except MusicError as e:
        raise HTTPException(400, str(e))


class UrlReq(BaseModel):
    url: str


@app.post("/api/music/import")
def api_music_import(req: UrlReq):
    try:
        return music.import_url(req.url)
    except MusicError as e:
        raise HTTPException(400, str(e))


class TagsReq(BaseModel):
    tags: str = ""


@app.put("/api/music/{name}/tags")
def api_music_tags(name: str, req: TagsReq):
    try:
        music.set_tags(name, req.tags)
    except MusicError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.delete("/api/music/{name}")
def api_music_delete(name: str):
    try:
        music.delete(name)
    except MusicError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/music/file/{name}")
def api_music_file(name: str):
    try:
        return FileResponse(music.path(name))
    except MusicError as e:
        raise HTTPException(404, str(e))


class MusicRecReq(BaseModel):
    id: str
    clip_id: str


@app.post("/api/music/recommend")
def api_music_recommend(req: MusicRecReq):
    clip = _find_clip(req.id, req.clip_id)
    parts = _resolve_parts(req.id, clip)
    text = "\n".join(sg["text"] for p in parts for sg in p["segments"]
                     if sg["end"] > p["start"] and sg["start"] < p["end"])
    try:
        return recommend_music(text, music.list())
    except RecommendError as e:
        raise HTTPException(400, str(e))


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
