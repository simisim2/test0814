"""3단계: 자막 → Claude 가 쇼츠 후보 구간 추천.

- API 키는 shorts_editor/.env 의 ANTHROPIC_API_KEY (없으면 친절한 안내 오류). 키는 절대 로그·응답에 싣지 않는다.
- 결과는 <work>/candidates/<영상id>/candidates.json
- Claude 가 돌려준 시간은 믿지 않고, 자막 문장 경계에 맞춰 보정·검증한다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

MODEL = os.environ.get("SHORTS_CLAUDE_MODEL", "claude-opus-5-5")
MIN_LEN, MAX_LEN = 15.0, 60.0          # 쇼츠 길이 범위(초)
HARD_MIN, HARD_MAX = 10.0, 90.0        # 보정 후에도 이 범위를 벗어나면 버린다

SYSTEM = """당신은 강의 영상에서 유튜브 쇼츠(세로 짧은 영상)로 만들기 좋은 구간을 고르는 편집자입니다.
입력은 '[시작초] 문장' 형식의 강의 자막입니다.

좋은 쇼츠 구간의 기준:
- 앞뒤 맥락 없이도 이해되는 하나의 완결된 이야기(질문→답, 주장→근거, 사례→교훈)
- 첫 문장이 시선을 끈다(질문, 반전, 강한 주장, 구체적 숫자·사례)
- 핵심 메시지가 분명하고, 보는 사람에게 바로 쓸모 있거나 공감된다
- 인사, 진행 멘트, 잡담, 자료 넘기는 말로만 이뤄진 구간은 제외
- 길이는 15~60초. 시작과 끝은 반드시 자막 문장의 경계에서 자른다(문장 중간 금지)

규칙:
- start/end 는 자막에 나온 시간(초) 기준으로 적는다. end 는 마지막 문장이 끝나는 시점이며, 다음 문장의 시작 시간을 쓰면 된다.
- 서로 겹치는 구간은 고르지 않는다.
- title 은 시청자가 클릭하고 싶은 한국어 제목(25자 이내).
- reason 은 이 구간을 고른 이유 한두 문장.
- bullets 는 핵심 발언 요약 2~4줄.
- 후보는 추천 순서(가장 좋은 것부터)로 나열한다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "reason": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "start", "end", "reason", "bullets"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


class RecommendError(Exception):
    """사용자에게 그대로 보여줄 수 있는 쉬운 한국어 오류."""


# ---------- .env 읽기 (키 값은 어디에도 출력하지 않는다) ----------
def load_env(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


NO_KEY_MSG = (
    "Claude API 키가 없어요.\n"
    "1) https://console.anthropic.com → API Keys 에서 키를 만드세요.\n"
    "2) shorts_editor 폴더에 .env 파일을 만들고 한 줄로 저장하세요:\n"
    "   ANTHROPIC_API_KEY=여기에_키\n"
    "3) 서버(start.bat)를 껐다가 다시 켜세요.\n"
    "※ 키는 채팅이나 GitHub 에 붙여넣지 마세요."
)


# ---------- 프롬프트 ----------
def build_transcript_text(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.1f}] {s['text']}" for s in segments)


def build_user_prompt(segments: list[dict], count: int, hint: str) -> str:
    total = segments[-1]["end"] if segments else 0
    parts = [
        f"아래는 강의 자막입니다 (총 {total / 60:.1f}분, {len(segments)}문장).",
        f"쇼츠 후보 구간을 최대 {count}개 골라 주세요. 좋은 구간이 그보다 적으면 적게 골라도 됩니다.",
    ]
    if hint:
        parts.append(f"사용자가 원하는 방향: {hint[:200]}")
    parts += ["", "<transcript>", build_transcript_text(segments), "</transcript>"]
    return "\n".join(parts)


# ---------- 검증·보정 ----------
def snap_and_validate(raw: list[dict], segments: list[dict]) -> list[dict]:
    """모델이 준 시간을 자막 문장 경계에 맞추고, 이상한 후보는 버린다."""
    if not segments:
        return []
    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    lo, hi = starts[0], ends[-1]

    def nearest(vals: list[float], t: float) -> float:
        return min(vals, key=lambda x: abs(x - t))

    out: list[dict] = []
    for c in raw:
        try:
            s = nearest(starts, float(c["start"]))
            e = nearest(ends, float(c["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        s, e = max(lo, s), min(hi, e)
        length = e - s
        if length < HARD_MIN or length > HARD_MAX:
            continue
        # 이미 고른 후보와 절반 이상 겹치면 중복으로 본다
        dup = False
        for o in out:
            overlap = min(e, o["end"]) - max(s, o["start"])
            if overlap > 0.5 * min(length, o["end"] - o["start"]):
                dup = True
                break
        if dup:
            continue
        bullets = [str(b).strip() for b in (c.get("bullets") or []) if str(b).strip()][:5]
        out.append({
            "title": str(c.get("title", "")).strip()[:60] or "제목 없음",
            "start": round(s, 2), "end": round(e, 2), "length": round(length, 1),
            "reason": str(c.get("reason", "")).strip(),
            "bullets": bullets,
            "warn": "" if MIN_LEN <= length <= MAX_LEN else "쇼츠 권장 길이(15~60초)를 벗어나요",
        })
    return out


# ---------- Claude 호출 ----------
def _send(system: str, user: str, schema: dict, effort: str = "high", max_tokens: int = 16000) -> dict:
    """Claude 에 구조화 출력(JSON)을 요청하고 파싱해서 돌려준다. 오류는 쉬운 한국어 RecommendError 로."""
    import anthropic

    if not has_key():
        raise RecommendError(NO_KEY_MSG)
    client = anthropic.Anthropic()
    kwargs = dict(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}],
    )
    try:
        # 안전 분류기가 거절하면 서버가 대체 모델로 이어서 처리하도록 한다.
        try:
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            resp = client.messages.create(**kwargs)   # 이 계정이 fallbacks 를 못 쓰면 그냥 호출
    except anthropic.AuthenticationError:
        raise RecommendError("API 키가 올바르지 않아요. .env 의 ANTHROPIC_API_KEY 를 다시 확인해 주세요.")
    except anthropic.PermissionDeniedError:
        raise RecommendError("이 API 키로는 사용할 수 없어요. 권한이나 결제 설정을 확인해 주세요.")
    except anthropic.NotFoundError:
        raise RecommendError(f"모델 '{MODEL}' 을 찾을 수 없어요. .env 에 SHORTS_CLAUDE_MODEL 로 다른 모델을 지정해 보세요.")
    except anthropic.RateLimitError:
        raise RecommendError("요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요.")
    except anthropic.BadRequestError as e:
        msg = str(getattr(e, "message", e))
        if "credit" in msg.lower() or "balance" in msg.lower():
            raise RecommendError("API 크레딧이 부족해요. console.anthropic.com 에서 충전해 주세요.")
        raise RecommendError(f"요청이 거절됐어요: {msg[:300]}")
    except anthropic.APIConnectionError:
        raise RecommendError("인터넷 연결을 확인해 주세요.")
    except anthropic.APIStatusError as e:
        raise RecommendError(f"Claude 서버 오류({e.status_code}). 잠시 뒤 다시 시도해 주세요.")

    if resp.stop_reason == "refusal":
        raise RecommendError("Claude 가 이 내용의 처리를 거절했어요. 다른 영상으로 시도해 보세요.")
    if resp.stop_reason == "max_tokens":
        raise RecommendError("답이 너무 길어 잘렸어요. 개수를 줄여 다시 시도해 주세요.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise RecommendError("Claude 의 답을 읽지 못했어요. 다시 시도해 주세요.")


def call_claude(segments: list[dict], count: int, hint: str) -> list[dict]:
    data = _send(SYSTEM, build_user_prompt(segments, count, hint), SCHEMA)
    try:
        return data["candidates"]
    except (KeyError, TypeError):
        raise RecommendError("Claude 의 답을 읽지 못했어요. 다시 시도해 주세요.")


# ---------- 배경음악 추천 ----------
MUSIC_SYSTEM = """당신은 유튜브 쇼츠의 배경음악을 고르는 편집자입니다.
입력은 쇼츠로 만들 구간의 말(자막)과, 사용자가 가진 음악 목록입니다.

해야 할 일:
1) 이 구간의 분위기(mood)를 한국어 한 줄로 요약한다. (예: 차분하고 따뜻한 동기부여)
2) 무료 음원 사이트에서 검색할 영어 키워드 4~6개를 준다. (예: "calm piano", "uplifting acoustic", "soft inspirational")
3) 목록에 있는 곡 중 어울리는 순서대로 최대 3곡을 고르고 이유를 한 문장으로 쓴다. 목록에 없는 이름은 절대 만들지 않는다.
   어울리는 곡이 없으면 picks 를 빈 배열로 둔다.
4) 말소리를 방해하지 않을 배경음악 크기(volume, 10~40)를 제안한다. 말이 빠르고 중요한 내용이면 낮게.

음악은 말보다 앞서지 않는 잔잔한 배경이 기본입니다."""

MUSIC_SCHEMA = {
    "type": "object",
    "properties": {
        "mood": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "volume": {"type": "integer"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mood", "keywords", "volume", "picks"],
    "additionalProperties": False,
}


def recommend_music(transcript: str, library: list[dict]) -> dict:
    """library: [{name, tags, duration}] → {mood, keywords, volume, picks:[{name, reason}]}"""
    text = transcript.strip()
    if not text:
        raise RecommendError("이 구간에는 자막이 없어요. 먼저 ③ 에서 자막을 만들어 주세요.")
    if library:
        lib = "\n".join(
            f"- {x['name']}" + (f" (태그: {x['tags']})" if x.get("tags") else "")
            + (f" [{int(x['duration'])}초]" if x.get("duration") else "")
            for x in library[:80])
    else:
        lib = "(아직 올려 둔 음악이 없음)"
    user = f"<구간 자막>\n{text[:6000]}\n</구간 자막>\n\n<내 음악 목록>\n{lib}\n</내 음악 목록>"
    data = _send(MUSIC_SYSTEM, user, MUSIC_SCHEMA, effort="medium", max_tokens=4000)
    names = {x["name"] for x in library}
    picks = [p for p in data.get("picks", []) if p.get("name") in names][:3]   # 목록에 없는 곡은 버린다
    return {
        "mood": str(data.get("mood", ""))[:120],
        "keywords": [str(k)[:40] for k in data.get("keywords", [])][:6],
        "volume": max(10, min(40, int(data.get("volume", 25) or 25))),
        "picks": [{"name": p["name"], "reason": str(p.get("reason", ""))[:200]} for p in picks],
    }


# ---------- 작업 관리 ----------
class Job:
    def __init__(self, vid: str):
        self.vid = vid
        self.state = "running"      # running | done | error
        self.stage = "Claude 에게 물어보는 중"
        self.message = ""
        self.started = time.time()

    def public(self) -> dict:
        return {"state": self.state, "stage": self.stage, "message": self.message,
                "elapsed": round(time.time() - self.started, 1), "model": MODEL}


class Recommender:
    def __init__(self, work: Path, caller: Callable = call_claude):
        self.root = work / "candidates"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.caller = caller

    def result(self, vid: str) -> Optional[dict]:
        try:
            return json.loads((self.root / vid / "candidates.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def status(self, vid: str) -> dict:
        job = self.jobs.get(vid)
        if job:
            return job.public()
        r = self.result(vid)
        if r:
            return {"state": "done", "stage": "완료", "message": "", "elapsed": 0, "model": r.get("model")}
        return {"state": "idle", "stage": "", "message": "", "elapsed": 0, "model": MODEL}

    def start(self, vid: str, segments: list[dict], count: int, hint: str) -> dict:
        if not segments:
            raise RecommendError("자막이 비어 있어요. 말이 들어 있는 영상인지 확인해 주세요.")
        if not has_key():
            raise RecommendError(NO_KEY_MSG)
        with self.lock:
            old = self.jobs.get(vid)
            if old and old.state == "running":
                raise RuntimeError("이미 추천을 받는 중입니다.")
            job = Job(vid)
            self.jobs[vid] = job
        threading.Thread(target=self._run, args=(job, segments, max(1, min(count, 10)), hint), daemon=True).start()
        return job.public()

    def _run(self, job: Job, segments: list[dict], count: int, hint: str) -> None:
        try:
            raw = self.caller(segments, count, hint)
            cands = snap_and_validate(raw, segments)
            if not cands:
                raise RecommendError("쓸 만한 후보를 찾지 못했어요. 자막이 너무 짧거나 길이 조건에 맞는 구간이 없었어요.")
            d = self.root / job.vid
            d.mkdir(parents=True, exist_ok=True)
            data = {"video_id": job.vid, "model": MODEL, "created": int(time.time()), "hint": hint, "candidates": cands}
            tmp = d / "candidates.json.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(d / "candidates.json")
            job.state, job.stage = "done", "완료"
        except RecommendError as e:
            job.state, job.stage, job.message = "error", "실패", str(e)
        except Exception as e:  # noqa: BLE001
            job.state, job.stage, job.message = "error", "실패", f"예상치 못한 오류: {type(e).__name__}"
