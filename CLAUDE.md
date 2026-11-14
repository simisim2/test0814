# 프로젝트: 쇼츠 메이커 (강의 영상 → 쇼츠 제작 로컬 웹앱)

자세한 기획은 `SHORTS_EDITOR_CONTEXT.md`, 실행·이어서 작업하는 법은 `PC_SETUP.md` 참고.
`index.html`(루트)은 이 프로젝트와 무관한 기존 파일이니 건드리지 말 것.

## 현재 상태
- 1단계 완료 (`shorts_editor/`): 영상 불러오기(경로 붙여넣기·폴더 스캔), 재생, 구간 표시/저장(브라우저 localStorage)
- 2단계 구현됨 (`shorts_editor/transcribe.py`): ffmpeg 로 소리 추출 → faster-whisper(CPU, int8) → `work/transcripts/<영상id>/transcript.json`. 백그라운드 스레드, 진행률 조회, 취소. UI는 왼쪽 "③ 자막 만들기"·오른쪽 "자막" 목록(클릭하면 해당 시점으로 이동)
- 다음: 3단계 Claude 추천 → 4단계 구간 편집 서버 저장 → 5단계 카드 렌더

## 구조
- `shorts_editor/app.py` FastAPI 서버 (영상은 경로 대신 id로 `/media/{id}` 스트리밍, Range 지원)
- `shorts_editor/static/index.html` 단일 페이지 UI (반응형: PC 3단 / 폰 하단 탭)
- `shorts_editor/run.py` 실행(0.0.0.0:8765), `start.bat` Windows 실행

## 규칙
- 사용자는 Windows PC + 폰(같은 와이파이)에서 씀. 한국어로 쉽게 설명할 것(비개발자).
- 경로에 `[ ]`, 한글, 공백이 들어간다. subprocess 는 항상 인자 리스트로, 글로빙·URL 직결 금지.
- 파일명은 DOM에 textContent/createElement 로만 넣을 것(XSS 방지).
- 이 PC는 프로젝트가 OneDrive 안에 있다. 그래서 `start.bat` 은 가상환경과 작업 데이터(`SHORTS_WORK_DIR`)를 `%LOCALAPPDATA%\shorts_editor\` (C드라이브 로컬)에 둔다. `.venv`/`work` 를 OneDrive 폴더 안에 만들지 말 것.
- faster-whisper 에 파일 경로를 넘기지 말고 numpy 배열로 넘길 것 (PyAV 19 와 충돌: `open() got an unexpected keyword argument 'metadata_errors'`).
- 작업 데이터는 `work/`(git 제외). API 키는 `.env`에 두고 절대 커밋·채팅에 붙이지 말 것.
- 단계마다 실행해서 확인하고 다음으로. 테스트는 2~3분 짧은 영상 사용.
