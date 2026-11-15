"""5단계: 구간 → 9:16 쇼츠 mp4 렌더 (+ 디자인 미리보기 1프레임).

- 글자(제목·문구)는 Pillow 로 PNG 로 그려 ffmpeg 에서 겹친다. (drawtext 이스케이프 지옥 회피)
- 자막은 ASS 파일로 만들어 ffmpeg(libass)로 입힌다.
- ffmpeg 는 작업 폴더(cwd)에서 상대 파일명으로 실행한다. (경로의 ':' '[' 한글·공백 문제 회피)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
LAYOUTS = {
    "card": "카드형 — 위 제목, 가운데 원본 영상, 아래 문구 (영상이 잘리지 않아요)",
    "fill": "세로 꽉 채우기 — 영상을 9:16 으로 잘라 가득 채워요 (가로 영상은 양옆이 잘려요)",
    "blur": "배경 흐림 + 원본 — 원본을 가운데 두고 위아래는 같은 영상을 흐리게 채워요",
}
THEMES = {
    "midnight": dict(label="미드나잇", bg=("#0f1226", "#2b1d63"), text="#ffffff", accent="#ff4d7e", box="#ffffff", boxtext="#14162b"),
    "cream":    dict(label="크림",     bg=("#fff6e5", "#ffe2b8"), text="#2a2118", accent="#e5532d", box="#2a2118", boxtext="#fff6e5"),
    "forest":   dict(label="포레스트", bg=("#0e2a22", "#1c5a45"), text="#ffffff", accent="#ffd24d", box="#ffffff", boxtext="#0e2a22"),
    "sunset":   dict(label="선셋",     bg=("#ff7a45", "#c2185b"), text="#ffffff", accent="#fff1a8", box="#ffffff", boxtext="#7a1236"),
    "mono":     dict(label="모노",     bg=("#0b0b0b", "#2a2a2a"), text="#ffffff", accent="#00e0a4", box="#ffffff", boxtext="#0b0b0b"),
}
DEFAULT_DESIGN = dict(kicker="", lines=[], footer="", layout="card", theme="midnight", accent="",
                      subs=True, crop_x=50, font="", sub_font="")

# ---------------------------------------------------------------- 글꼴
BASE = Path(__file__).resolve().parent
FONT_DIRS = [
    BASE / "fonts",                                             # 직접 넣어 둔 글꼴 (모두 후보)
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",     # 시스템 (알려진 이름만)
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
]
# (파일명 일부, 보기 좋은 이름, 추천 순위: 작을수록 쇼츠 제목에 어울림)
KNOWN = [
    ("gmarketsansbold", "G마켓 산스 Bold", 1), ("gmarketsans", "G마켓 산스", 2),
    ("pretendard-extrabold", "Pretendard ExtraBold", 3), ("pretendard-bold", "Pretendard Bold", 4),
    ("nanumsquareextrabold", "나눔스퀘어 ExtraBold", 5), ("nanumsquarebold", "나눔스퀘어 Bold", 6),
    ("blackhansans", "블랙 한 산스", 7), ("jua", "주아체", 8),
    ("notosanskr", "Noto Sans KR", 20), ("malgunbd", "맑은 고딕 Bold", 21), ("malgun.", "맑은 고딕", 30),
]
_font_cache: Optional[list[dict]] = None
_font_lock = threading.Lock()


def _has_hangul(path: Path) -> bool:
    try:
        f = ImageFont.truetype(str(path), 40)
        a = f.getmask("가힣한").getbbox()
        b = f.getmask("\ue000\ue001\ue002").getbbox()   # 비어 있는 코드 → '없는 글자' 모양
        return bool(a) and a != b
    except Exception:
        return False


def _names(path: Path) -> tuple[str, str]:
    """(글꼴 패밀리 이름, 굵기 이름)"""
    try:
        fam, style = ImageFont.truetype(str(path), 20).getname()
        return fam, style or ""
    except Exception:
        return path.stem, ""


def list_fonts(refresh: bool = False) -> list[dict]:
    """한글이 되는 글꼴 목록. 추천 순서대로 정렬하고 가장 위 하나에 recommended 표시."""
    global _font_cache
    with _font_lock:
        if _font_cache is not None and not refresh:
            return _font_cache
        found: dict[str, dict] = {}
        for i, d in enumerate(FONT_DIRS):
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.suffix.lower() not in (".ttf", ".otf"):
                    continue
                low = p.name.lower().replace(" ", "")
                hit = next((k for k in KNOWN if k[0] in low), None)
                if i > 0 and not hit:
                    continue                      # 시스템 글꼴은 알려진 것만 (수백 개 검사 방지)
                if not _has_hangul(p):
                    continue
                fid = p.stem.lower()
                if fid in found:
                    continue
                fam, style = _names(p)
                if i > 0 and style.replace(" ", "").lower() in ("thin", "extralight", "light"):
                    continue                      # 너무 얇은 굵기는 쇼츠에 안 어울려서 숨김
                label = hit[1] if hit else p.stem
                if style and style not in ("-", "") and style.lower() != "regular" and style.lower() not in label.lower():
                    label += f" {style}"
                found[fid] = dict(id=fid, path=str(p), family=fam, label=label,
                                  rank=(hit[2] if hit else 10), mine=(i == 0))
        fonts = sorted(found.values(), key=lambda f: (f["rank"], f["label"]))
        for f in fonts:
            f["recommended"] = False
        if fonts:
            fonts[0]["recommended"] = True
        _font_cache = fonts
        return fonts


def resolve_font(fid: str) -> Optional[dict]:
    fonts = list_fonts()
    for f in fonts:
        if f["id"] == fid:
            return f
    return fonts[0] if fonts else None            # 못 찾으면 추천(맨 위) 글꼴


def _pil_font(f: Optional[dict], size: int) -> ImageFont.FreeTypeFont:
    if not f:
        return ImageFont.load_default()
    font = ImageFont.truetype(f["path"], size)
    try:                                         # 가변 글꼴이면 굵게
        axes = font.get_variation_axes()
        font.set_variation_by_axes([min(max(700, a["minimum"]), a["maximum"]) if a.get("name") in (b"Weight", "Weight") else a["default"] for a in axes])
    except Exception:
        pass
    return font


# ---------------------------------------------------------------- 디자인 값 검증
def clean_design(d: dict) -> dict:
    out = dict(DEFAULT_DESIGN)
    out["kicker"] = str(d.get("kicker", ""))[:30].strip()
    out["lines"] = [str(x).strip()[:40] for x in (d.get("lines") or [])[:3] if str(x).strip()]
    out["footer"] = str(d.get("footer", ""))[:120].strip()
    out["layout"] = d.get("layout") if d.get("layout") in LAYOUTS else "card"
    out["theme"] = d.get("theme") if d.get("theme") in THEMES else "midnight"
    acc = str(d.get("accent", "")).strip()
    out["accent"] = acc if re.fullmatch(r"#[0-9a-fA-F]{6}", acc) else ""
    out["subs"] = bool(d.get("subs", True))
    try:
        out["crop_x"] = max(0, min(100, int(d.get("crop_x", 50))))
    except (TypeError, ValueError):
        out["crop_x"] = 50
    out["font"] = re.sub(r"[^0-9a-z._ -]", "", str(d.get("font", "")).lower())[:80]
    out["sub_font"] = re.sub(r"[^0-9a-z._ -]", "", str(d.get("sub_font", "")).lower())[:80]
    return out


# ---------------------------------------------------------------- 그림(오버레이) 만들기
def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _gradient(c1: str, c2: str) -> Image.Image:
    a, b = _rgb(c1), _rgb(c2)
    col = Image.new("RGB", (1, H))
    px = col.load()
    for y in range(H):
        t = y / (H - 1)
        px[0, y] = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return col.resize((W, H))


def _fit(draw: ImageDraw.ImageDraw, text: str, font_info: Optional[dict], size: int, max_w: int, min_size: int = 40):
    while size > min_size:
        f = _pil_font(font_info, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _pil_font(font_info, min_size)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur.strip())
            cur = ch
        else:
            cur += ch
    if cur.strip():
        lines.append(cur.strip())
    return lines


def make_overlays(design: dict, work: Path) -> tuple[Optional[Path], Path, Optional[int]]:
    """(배경 PNG 또는 None, 글자 오버레이 PNG, 하단 문구 박스의 윗변 y 또는 None) 를 work 폴더에 만든다."""
    th = THEMES[design["theme"]]
    accent = design["accent"] or th["accent"]
    layout = design["layout"]
    tf = resolve_font(design["font"])

    bg_path = None
    if layout == "card":
        bg = _gradient(*th["bg"])
        bg_path = work / "bg.png"
        bg.save(bg_path)

    top = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(top)
    text_col = th["text"] if layout == "card" else "#ffffff"

    if layout != "card":                         # 영상 위에 글자가 읽히도록 위쪽을 어둡게
        scrim = Image.new("RGBA", (1, 700))
        sp = scrim.load()
        for y in range(700):
            sp[0, y] = (0, 0, 0, round(170 * (1 - y / 700) ** 1.5))
        top.alpha_composite(scrim.resize((W, 700)), (0, 0))
        d = ImageDraw.Draw(top)

    y = 170
    if design["kicker"]:
        kf = _fit(d, design["kicker"], tf, 44, W - 260, 28)
        tw = d.textlength(design["kicker"], font=kf)
        pad = 28
        x0 = (W - tw) / 2 - pad
        d.rounded_rectangle([x0, y, x0 + tw + pad * 2, y + 76], radius=38, fill=accent)
        r, g, b = _rgb(accent)
        kcol = "#14162b" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"   # 밝은 포인트색 위엔 어두운 글씨
        d.text((W / 2, y + 38), design["kicker"], font=kf, fill=kcol, anchor="mm")
        y += 76 + 34

    lines = design["lines"]
    if lines:
        n = len(lines)
        size = int(min(100, 330 / (n * 1.2)))
        for i, ln in enumerate(lines):
            f = _fit(d, ln, tf, size, W - 140, 44)
            col = accent if (n >= 2 and i == n - 1) else text_col
            d.text((W / 2, y), ln, font=f, fill=col, anchor="ma",
                   stroke_width=(0 if layout == "card" else 6), stroke_fill="#000000")
            y += int(f.size * 1.22)

    footer_top = None
    if design["footer"]:
        ff = _pil_font(tf, 54)
        wrapped = _wrap(d, design["footer"], ff, W - 260)[:3]
        lh = 70
        bh = 56 + lh * len(wrapped)
        by1 = 1790 if layout == "card" else 1700
        by0 = by1 - bh
        footer_top = int(by0)
        d.rounded_rectangle([90, by0, W - 90, by1], radius=36, fill=th["box"])
        ty = by0 + 28
        for ln in wrapped:
            d.text((W / 2, ty), ln, font=ff, fill=th["boxtext"], anchor="ma")
            ty += lh

    out = work / "top.png"
    top.save(out)
    return bg_path, out, footer_top


# ---------------------------------------------------------------- 자막(ASS)
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def make_ass(segments: list[dict], start: float, end: float, family: str, layout: str, work: Path,
             footer_top: Optional[int] = None) -> Optional[Path]:
    rows = []
    for s in segments:
        a, b = max(s["start"], start), min(s["end"], end)
        if b - a < 0.15:
            continue
        text = str(s["text"]).replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()
        if text:
            rows.append(f"Dialogue: 0,{_ass_time(a - start)},{_ass_time(b - start)},Default,,0,0,0,,{text}")
    if not rows:
        return None
    margin_v = 520 if layout == "card" else 330
    if footer_top and layout != "card":
        margin_v = max(margin_v, H - footer_top + 40)   # 하단 문구 박스 바로 위
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{family},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,1,2,90,90,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    # 위 Format 은 ass 표준(…, Style, Name, MarginL, …)과 맞추기 위해 Name 열을 포함해 다시 쓴다
    head = head.replace("Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text",
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
    p = work / "subs.ass"
    p.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- ffmpeg
def build_cmd(video: Path, design: dict, start: float, dur: float, has_bg: bool, has_ass: bool,
              out_name: str, preview: bool) -> list[str]:
    layout = design["layout"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video)]
    if has_bg:
        cmd += ["-loop", "1", "-framerate", "30", "-i", "bg.png"]
    cmd += ["-loop", "1", "-framerate", "30", "-i", "top.png"]
    top_idx = 2 if has_bg else 1

    if layout == "card":
        g = (f"[0:v]scale=1080:900:force_original_aspect_ratio=decrease,setsar=1,fps=30[v];"
             f"[1:v][v]overlay=x=(W-w)/2:y=560+(900-h)/2:shortest=1[a];")
        last = "a"
    elif layout == "fill":
        cx = design["crop_x"] / 100
        g = (f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
             f"crop=1080:1920:x='(iw-ow)*{cx}':y='(ih-oh)/2',setsar=1,fps=30[a];")
        last = "a"
    else:  # blur
        g = ("[0:v]fps=30,split[s1][s2];"
             "[s1]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:4,eq=brightness=-0.12[bgv];"
             "[s2]scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1[fg];"
             "[bgv][fg]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[a];")
        last = "a"
    g += f"[{last}][{top_idx}:v]overlay=0:0:shortest=1[b];"
    g += "[b]ass=filename=subs.ass:fontsdir=.[c];" if has_ass else "[b]null[c];"
    g += "[c]format=yuv420p[out]"
    cmd += ["-filter_complex", g, "-map", "[out]"]
    if preview:
        cmd += ["-frames:v", "1", out_name]
    else:
        cmd += ["-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{dur:.3f}", out_name]
    return cmd


def prepare(video: Path, design: dict, segments: list[dict], start: float, end: float, work: Path):
    """work 폴더에 오버레이·자막·글꼴을 준비하고 (has_bg, has_ass) 를 돌려준다."""
    bg, _, footer_top = make_overlays(design, work)
    has_ass = False
    if design["subs"] and segments:
        sf = resolve_font(design["sub_font"] or design["font"])
        family = "Malgun Gothic"
        if sf:
            shutil.copy(sf["path"], work / Path(sf["path"]).name)   # libass 가 찾도록 같은 폴더에 둔다
            family = sf["family"]
        has_ass = make_ass(segments, start, end, family, design["layout"], work, footer_top) is not None
    return bg is not None, has_ass


class RenderError(Exception):
    pass


def preview_png(video: Path, design: dict, segments: list[dict], start: float, end: float, out_png: Path) -> None:
    # 자막이 실제로 나오는 순간을 보여 주려고, 구간 안 첫 문장 근처를 고른다
    t = start + min(1.5, (end - start) / 2)
    for sg in segments:
        if sg["end"] > start and sg["start"] < end:
            t = min(max(sg["start"] + 0.4, start), end - 0.5)
            break
    with tempfile.TemporaryDirectory(prefix="shorts_prev_") as tmp:
        work = Path(tmp)
        has_bg, has_ass = prepare(video, design, segments, t, min(end, t + 2.0), work)
        cmd = build_cmd(video, design, t, 2.0, has_bg, has_ass, "frame.png", preview=True)
        try:
            r = subprocess.run(cmd, cwd=work, capture_output=True, timeout=90)
        except FileNotFoundError:
            raise RenderError("ffmpeg 가 없습니다. 'winget install Gyan.FFmpeg' 후 start.bat 을 다시 실행하세요.")
        except subprocess.TimeoutExpired:
            raise RenderError("미리보기가 너무 오래 걸려 멈췄어요.")
        if r.returncode != 0 or not (work / "frame.png").exists():
            raise RenderError("미리보기를 만들지 못했어요. " + r.stderr.decode("utf-8", "replace")[-300:])
        shutil.copy(work / "frame.png", out_png)


class Job:
    def __init__(self, vid: str, clip_id: str, dur: float):
        self.vid, self.clip_id, self.dur = vid, clip_id, dur
        self.state = "running"        # running | done | error | cancelled
        self.progress = 0.0
        self.message = ""
        self.started = time.time()
        self.cancel = threading.Event()
        self.proc: Optional[subprocess.Popen] = None

    def public(self) -> dict:
        return {"state": self.state, "progress": round(self.progress, 4), "message": self.message,
                "elapsed": round(time.time() - self.started, 1)}


class Renderer:
    def __init__(self, work: Path):
        self.root = work / "out"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[tuple[str, str], Job] = {}
        self.lock = threading.Lock()

    def out_path(self, vid: str, clip_id: str) -> Path:
        return self.root / vid / f"{clip_id}.mp4"

    def status(self, vid: str, clip_id: str) -> dict:
        job = self.jobs.get((vid, clip_id))
        if job:
            return job.public()
        p = self.out_path(vid, clip_id)
        if p.is_file():
            return {"state": "done", "progress": 1.0, "message": "", "elapsed": 0}
        return {"state": "idle", "progress": 0.0, "message": "", "elapsed": 0}

    def start(self, vid: str, clip_id: str, video: Path, clip: dict, segments: list[dict]) -> dict:
        design = clean_design(clip)
        start, end = float(clip["start"]), float(clip["end"])
        with self.lock:
            old = self.jobs.get((vid, clip_id))
            if old and old.state == "running":
                raise RuntimeError("이미 이 구간을 만드는 중입니다.")
            job = Job(vid, clip_id, end - start)
            self.jobs[(vid, clip_id)] = job
        threading.Thread(target=self._run, args=(job, video, design, segments, start, end), daemon=True).start()
        return job.public()

    def cancel(self, vid: str, clip_id: str) -> dict:
        job = self.jobs.get((vid, clip_id))
        if job and job.state == "running":
            job.cancel.set()
            if job.proc and job.proc.poll() is None:
                job.proc.kill()
        return self.status(vid, clip_id)

    def _run(self, job: Job, video: Path, design: dict, segments: list[dict], start: float, end: float) -> None:
        final = self.out_path(job.vid, job.clip_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="shorts_render_") as tmp:
            work = Path(tmp)
            try:
                has_bg, has_ass = prepare(video, design, segments, start, end, work)
                cmd = build_cmd(video, design, start, end - start, has_bg, has_ass, "out.mp4", preview=False)
                err = open(work / "ffmpeg.log", "wb")
                try:
                    job.proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    raise RenderError("ffmpeg 가 없습니다. 'winget install Gyan.FFmpeg' 후 start.bat 을 다시 실행하세요.")
                for line in job.proc.stdout:               # -progress 출력: out_time_us=123456
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            job.progress = min(0.99, int(line.split("=")[1]) / 1_000_000 / max(job.dur, 0.1))
                        except ValueError:
                            pass
                job.proc.wait()
                err.close()
                if job.cancel.is_set():
                    job.state, job.message = "cancelled", "취소했어요."
                    return
                if job.proc.returncode != 0 or not (work / "out.mp4").exists():
                    tail = (work / "ffmpeg.log").read_text(encoding="utf-8", errors="replace")[-400:]
                    raise RenderError("영상을 만들지 못했어요. " + tail)
                tmp_final = final.with_suffix(".mp4.tmp")
                shutil.move(str(work / "out.mp4"), tmp_final)
                tmp_final.replace(final)
                job.progress, job.state = 1.0, "done"
            except RenderError as e:
                job.state, job.message = "error", str(e)
            except Exception as e:  # noqa: BLE001
                job.state, job.message = "error", f"예상치 못한 오류: {type(e).__name__}: {e}"
