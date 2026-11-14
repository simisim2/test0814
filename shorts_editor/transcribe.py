"""2단계: 자막 생성 (ffmpeg 로 소리 추출 → faster-whisper 로 받아쓰기).

- 백그라운드 스레드에서 돌고, 진행률을 조회할 수 있고, 중간에 취소할 수 있다.
- 결과는 <work>/transcripts/<영상id>/transcript.json  ([{start, end, text}])
- 경로는 항상 subprocess 인자 리스트로 전달한다. ([4기], 한글, 공백 안전)
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

MODELS = {
    "small": "빠름 (정확도 보통)",
    "medium": "정확 (느림)",
    "large-v3": "고정밀 (매우 느림, 메모리 많이 사용)",
}
DEFAULT_MODEL = "small"


def _read_wav(path: Path):
    """16kHz 모노 wav 를 직접 읽는다. (faster-whisper 가 쓰는 PyAV 의 버전 충돌을 피함)"""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class Job:
    def __init__(self, vid: str, model: str, start: float, end: Optional[float]):
        self.vid, self.model, self.start, self.end = vid, model, start, end
        self.state = "running"      # running | done | error | cancelled
        self.stage = "준비 중"
        self.progress = 0.0         # 0~1
        self.message = ""
        self.count = 0
        self.started = time.time()
        self.cancel = threading.Event()
        self.proc: Optional[subprocess.Popen] = None

    def public(self) -> dict:
        return {
            "state": self.state, "stage": self.stage, "progress": round(self.progress, 4),
            "message": self.message, "count": self.count, "model": self.model,
            "elapsed": round(time.time() - self.started, 1),
        }


class Transcriber:
    def __init__(self, work: Path):
        self.root = work / "transcripts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self._models: dict[str, object] = {}

    # ---- 조회 ----
    def _dir(self, vid: str) -> Path:
        return self.root / vid

    def result(self, vid: str) -> Optional[dict]:
        f = self._dir(vid) / "transcript.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None

    def status(self, vid: str) -> dict:
        job = self.jobs.get(vid)
        if job:
            return job.public()
        r = self.result(vid)
        if r:
            return {"state": "done", "stage": "완료", "progress": 1.0, "message": "",
                    "count": len(r["segments"]), "model": r.get("model")}
        return {"state": "idle", "stage": "", "progress": 0.0, "message": "", "count": 0}

    # ---- 시작/취소 ----
    def start(self, vid: str, video: Path, model: str, start: float = 0.0, end: Optional[float] = None) -> dict:
        if model not in MODELS:
            raise ValueError(f"지원하지 않는 모델입니다: {model}")
        if end is not None and end <= start:
            raise ValueError("분석 구간의 끝이 시작보다 뒤여야 합니다.")
        with self.lock:
            old = self.jobs.get(vid)
            if old and old.state == "running":
                raise RuntimeError("이미 자막을 만드는 중입니다.")
            job = Job(vid, model, max(0.0, start), end)
            self.jobs[vid] = job
        threading.Thread(target=self._run, args=(job, video), daemon=True).start()
        return job.public()

    def cancel(self, vid: str) -> dict:
        job = self.jobs.get(vid)
        if job and job.state == "running":
            job.cancel.set()
            if job.proc and job.proc.poll() is None:
                job.proc.kill()
            job.stage = "취소하는 중"
        return self.status(vid)

    # ---- 실제 작업 ----
    def _get_model(self, name: str, job: Job):
        if name not in self._models:
            job.stage = f"모델 준비 중 ({name}) — 처음 한 번은 다운로드로 오래 걸릴 수 있어요"
            from faster_whisper import WhisperModel  # 늦게 불러와 서버 시작을 빠르게
            self._models[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return self._models[name]

    def _extract_audio(self, job: Job, video: Path, wav: Path) -> None:
        job.stage = "소리 추출 중"
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if job.start > 0:
            cmd += ["-ss", str(job.start)]
        cmd += ["-i", str(video)]
        if job.end is not None:
            cmd += ["-t", str(job.end - job.start)]
        cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, err = job.proc.communicate()
        if job.cancel.is_set():
            return
        if job.proc.returncode != 0:
            msg = err.decode("utf-8", "replace").strip()[-400:]
            raise RuntimeError(f"소리 추출에 실패했습니다. {msg}")

    def _run(self, job: Job, video: Path) -> None:
        d = self._dir(job.vid)
        d.mkdir(parents=True, exist_ok=True)
        wav = d / "audio.wav"
        try:
            try:
                self._extract_audio(job, video, wav)
            except FileNotFoundError:
                raise RuntimeError("ffmpeg 가 없습니다. 'winget install Gyan.FFmpeg' 후 start.bat 을 다시 실행하세요.")
            if job.cancel.is_set():
                job.state, job.stage = "cancelled", "취소됨"
                return

            model = self._get_model(job.model, job)
            if job.cancel.is_set():
                job.state, job.stage = "cancelled", "취소됨"
                return

            job.stage = "받아쓰는 중"
            segs_iter, info = model.transcribe(
                _read_wav(wav), language="ko", vad_filter=True, beam_size=1,
                condition_on_previous_text=False,
            )
            total = float(info.duration) or 1.0
            out = []
            for s in segs_iter:
                if job.cancel.is_set():
                    job.state, job.stage = "cancelled", "취소됨"
                    return
                text = s.text.strip()
                if text:
                    out.append({"start": round(s.start + job.start, 2), "end": round(s.end + job.start, 2), "text": text})
                job.count = len(out)
                job.progress = min(0.99, s.end / total)

            data = {"video_id": job.vid, "model": job.model, "range": [job.start, job.end],
                    "created": int(time.time()), "segments": out}
            tmp = d / "transcript.json.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(d / "transcript.json")
            job.progress, job.state, job.stage = 1.0, "done", "완료"
        except Exception as e:  # noqa: BLE001
            job.state, job.stage, job.message = "error", "실패", str(e)
        finally:
            try:
                wav.unlink(missing_ok=True)   # 큰 임시 파일은 바로 지운다
            except OSError:
                pass
