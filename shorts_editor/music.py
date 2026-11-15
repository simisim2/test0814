"""배경음악 라이브러리: 내 파일 올리기 / 인터넷 직접 링크로 가져오기 / 태그 / 삭제.

- 저장 위치: <work>/music/  (OneDrive 밖의 로컬 폴더)
- 인터넷 가져오기는 https 직접 링크(.mp3 등)만 허용하고, 내 PC·공유기 같은 내부망 주소는 막는다(SSRF 방지).
- 받은 파일은 ffprobe 로 '진짜 오디오인지' 확인한 뒤에만 남긴다.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import AsyncIterator, Optional

ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
MAX_BYTES = 60 * 1024 * 1024
CT_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
          "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/flac": ".flac",
          "audio/opus": ".opus"}


class MusicError(Exception):
    """사용자에게 그대로 보여줄 수 있는 쉬운 한국어 오류."""


def _safe_name(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name                      # 경로 부분 제거
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise MusicError(f"지원하지 않는 형식이에요 ({ext or '확장자 없음'}). 지원: {', '.join(sorted(ALLOWED_EXT))}")
    stem = Path(name).stem[:80].strip(" .") or "music"
    return stem + ext


def _ffprobe_audio(path: Path) -> Optional[float]:
    """오디오면 길이(초), 아니면 None."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_type:format=duration", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
        if not data.get("streams"):
            return None
        return float(data.get("format", {}).get("duration") or 0)
    except FileNotFoundError:
        raise MusicError("ffprobe 가 없어요. ffmpeg 설치가 필요합니다.")
    except Exception:
        return None


# ---------- 안전한 다운로드 ----------
def _check_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise MusicError("주소를 찾을 수 없어요. 링크를 다시 확인해 주세요.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise MusicError("내부 주소는 가져올 수 없어요. 인터넷에 공개된 https 링크만 쓸 수 있어요.")


def _validate_url(url: str) -> urllib.parse.ParseResult:
    u = urllib.parse.urlparse(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise MusicError("https:// 로 시작하는 직접 다운로드 링크만 가져올 수 있어요. (.mp3 파일 주소)")
    if u.port not in (None, 443):
        raise MusicError("허용되지 않는 포트예요.")
    _check_host(u.hostname)
    return u


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)                       # 넘어가는 주소도 같은 검사를 한다
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MusicLib:
    def __init__(self, work: Path):
        self.dir = work / "music"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_file = self.dir / "meta.json"

    # ---- 메타(태그·길이 캐시) ----
    def _meta(self) -> dict:
        try:
            return json.loads(self.meta_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_meta(self, m: dict) -> None:
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.meta_file)

    def list(self) -> list[dict]:
        meta, changed, out = self._meta(), False, []
        for p in sorted(self.dir.iterdir(), key=lambda x: x.name.lower()):
            if p.suffix.lower() not in ALLOWED_EXT or not p.is_file():
                continue
            m = meta.setdefault(p.name, {})
            size = p.stat().st_size
            if m.get("size") != size or "duration" not in m:
                m["size"], m["duration"] = size, _ffprobe_audio(p) or 0
                changed = True
            out.append({"name": p.name, "size": size, "duration": round(m["duration"], 1), "tags": m.get("tags", "")})
        if changed:
            self._save_meta(meta)
        return out

    def path(self, name: str) -> Path:
        p = self.dir / _safe_name(name)
        if p.parent != self.dir or not p.is_file():
            raise MusicError("배경음악 파일을 찾을 수 없어요. 다시 올려 주세요.")
        return p

    def set_tags(self, name: str, tags: str) -> None:
        p = self.path(name)
        meta = self._meta()
        meta.setdefault(p.name, {})["tags"] = tags.strip()[:100]
        self._save_meta(meta)

    def delete(self, name: str) -> None:
        p = self.path(name)
        p.unlink()
        meta = self._meta()
        meta.pop(p.name, None)
        self._save_meta(meta)

    def _unique(self, name: str) -> Path:
        p = self.dir / name
        i = 2
        while p.exists():
            p = self.dir / f"{Path(name).stem} ({i}){Path(name).suffix}"
            i += 1
        return p

    def _finish(self, tmp: Path, final: Path) -> Path:
        if _ffprobe_audio(tmp) is None:
            tmp.unlink(missing_ok=True)
            raise MusicError("오디오 파일이 아니거나 손상된 파일이에요.")
        tmp.replace(final)
        return final

    # ---- 올리기 ----
    async def save_stream(self, name: str, chunks: AsyncIterator[bytes]) -> dict:
        final = self._unique(_safe_name(name))
        tmp = final.with_suffix(final.suffix + ".part")
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for ch in chunks:
                    size += len(ch)
                    if size > MAX_BYTES:
                        raise MusicError(f"파일이 너무 커요 (최대 {MAX_BYTES // 1024 // 1024}MB).")
                    f.write(ch)
            if size == 0:
                raise MusicError("빈 파일이에요.")
            self._finish(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {"name": final.name}

    # ---- 인터넷 링크로 가져오기 ----
    def import_url(self, url: str) -> dict:
        u = _validate_url(url)
        opener = urllib.request.build_opener(_Redirect())
        req = urllib.request.Request(url.strip(), headers={"User-Agent": "shorts-editor/1.0"})
        tmp: Optional[Path] = None
        try:
            with opener.open(req, timeout=30) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                length = int(r.headers.get("Content-Length") or 0)
                if length > MAX_BYTES:
                    raise MusicError(f"파일이 너무 커요 (최대 {MAX_BYTES // 1024 // 1024}MB).")
                raw = Path(urllib.parse.unquote(urllib.parse.urlparse(r.geturl()).path)).name or "music"
                ext = Path(raw).suffix.lower()
                if ext not in ALLOWED_EXT:
                    if ctype in CT_EXT:
                        raw = Path(raw).stem + CT_EXT[ctype]
                    else:
                        raise MusicError("오디오 파일 링크가 아닌 것 같아요. 웹페이지가 아니라 .mp3 같은 파일 자체의 주소가 필요해요.")
                elif ctype.startswith("text/"):
                    raise MusicError("오디오가 아니라 웹페이지예요. 파일 자체의 주소가 필요해요.")
                final = self._unique(_safe_name(raw))
                tmp = final.with_suffix(final.suffix + ".part")
                size = 0
                with open(tmp, "wb") as f:
                    while True:
                        ch = r.read(1024 * 256)
                        if not ch:
                            break
                        size += len(ch)
                        if size > MAX_BYTES:
                            raise MusicError(f"파일이 너무 커요 (최대 {MAX_BYTES // 1024 // 1024}MB).")
                        f.write(ch)
            self._finish(tmp, final)
            return {"name": final.name, "source": u.hostname}
        except MusicError:
            if tmp:
                tmp.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as e:
            if tmp:
                tmp.unlink(missing_ok=True)
            raise MusicError(f"받아오지 못했어요 (서버 응답 {e.code}). 링크가 직접 다운로드 주소가 맞는지 확인해 주세요.")
        except (urllib.error.URLError, socket.timeout, OSError):
            if tmp:
                tmp.unlink(missing_ok=True)
            raise MusicError("받아오지 못했어요. 인터넷 연결과 링크를 확인해 주세요.")
