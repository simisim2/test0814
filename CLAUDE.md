# 프로젝트: 쇼츠 메이커 (강의 영상 → 쇼츠 제작 로컬 웹앱)

자세한 기획은 `SHORTS_EDITOR_CONTEXT.md`, 실행·이어서 작업하는 법은 `PC_SETUP.md` 참고.
`index.html`(루트)은 이 프로젝트와 무관한 기존 파일이니 건드리지 말 것.

## 현재 상태
- 1단계 완료 (`shorts_editor/`): 영상 불러오기(경로 붙여넣기·폴더 스캔), 재생, 구간 표시/저장(브라우저 localStorage)
- 2단계 구현됨 (`shorts_editor/transcribe.py`): ffmpeg 로 소리 추출 → faster-whisper(CPU, int8) → `work/transcripts/<영상id>/transcript.json`. 백그라운드 스레드, 진행률 조회, 취소. UI는 왼쪽 "③ 자막 만들기"·오른쪽 "자막" 목록(클릭하면 해당 시점으로 이동)
- 3단계 코드 완료, **실제 Claude 호출은 미검증** (`shorts_editor/recommend.py`): 자막 → Claude(기본 `claude-opus-5-5`, `.env` 의 `SHORTS_CLAUDE_MODEL` 로 변경) → JSON 스키마 구조화 출력 → 자막 문장 경계로 보정·중복/길이 검증 → `work/candidates/<id>/candidates.json`. 키 없으면 쉬운 안내 오류. UI: 왼쪽 "④ AI 쇼츠 추천", 오른쪽 "AI 추천 구간"(미리보기 / 이 구간 쓰기). 보정·파이프라인은 가짜 caller 로 테스트함. API 키가 생기면 짧은 영상(2~3분)으로 먼저 실제 호출 확인할 것.
- 4단계 완료: 구간을 서버에 저장 (`work/clips/<영상id>.json`, `GET/PUT /api/clips/{id}` 전체 교체 방식, 시작<끝·NaN·중복 id 검증, 시작순 정렬). 구간별 제목 입력, AI 추천의 "이 구간 쓰기"는 제목도 함께 저장. 1단계 때 브라우저(localStorage)에 저장한 구간은 처음 열 때 서버로 자동 이전.
- 5단계 완료 (`shorts_editor/render.py`): 구간 → 1080x1920 30fps mp4. 제작 화면 "⑤ 쇼츠 만들기"에서 선택: 배치 3종(card 카드형 / fill 세로 꽉 채우기+크롭 위치 / blur 배경흐림+원본), 자막 번인 on/off, 색 테마 5종(midnight/cream/forest/sunset/mono)+포인트 컬러, 제목·자막 글꼴(추천 표시), 라벨·제목 1~3줄·하단 문구. 디자인 설정은 구간 JSON(`work/clips`)에 함께 저장. 글자는 Pillow PNG, 자막은 ASS(libass), 합성은 ffmpeg(cwd=임시폴더, 상대 파일명). 디자인 미리보기(1프레임)·렌더 진행률·취소 지원. 결과 `work/out/<영상id>/<구간id>.mp4`. 약 0.35배속(19초 구간≈55초, CPU).
- 이어붙이기·전환·배경음악 완료: 구간 JSON 에 `extra`(이어붙일 구간 참조 [{vid,id}] 최대 8), `trans`(none/fade/fadeblack/dissolve/wipeleft/slideleft/circleopen)+`trans_dur`, `music`+`music_vol/fade_in/fade_out/start`, `duck`(말할 때 음악 줄이기=sidechaincompress), `mute_orig`, `vfade`(화면·원본소리 페이드) 저장. `render.py` 는 구간별로 완성 프레임(배경+영상+글자)을 만든 뒤 xfade/acrossfade 로 잇고, 자막은 전체 시간축(plan()의 bases)으로 ASS 생성. 완성 길이 = 합계 − (n−1)×전환. 음악은 `music.py`(work/music/): 내 파일 올리기(`/api/music/upload` raw body), https 직접링크 가져오기(`/api/music/import`, 내부망 주소·http·웹페이지 차단, 60MB 제한, ffprobe 로 오디오 검증), 태그, 삭제. AI 음악 추천 `/api/music/recommend`(recommend.py `recommend_music`: 분위기·영어 검색 키워드·내 음악 중 고르기, 목록에 없는 곡은 버림, API 키 필요·미검증). 음악 파일을 에이전트가 임의로 내려받지 말 것(저작권·다운로드는 사용자 확인).
- 글꼴: `list_fonts()` 가 project `fonts/`(직접 넣은 것, git 제외)와 시스템/사용자 글꼴 중 한글 되는 것만 찾아 G마켓 산스 Bold 등 순으로 추천. 추천 글꼴 파일을 임의로 내려받지 말 것(라이선스·다운로드는 사용자 확인).
- 테스트 시 주의: bash 에서 한글 JSON 을 curl 로 보내면 인코딩이 깨져 400 이 난다 → Python(urllib, UTF-8)로 보낼 것. 응답 코드·저장값까지 확인해야 거짓 성공을 피한다.
- 다음(후보): 6단계 프로젝트 저장/불러오기, 여러 구간·영상 이어붙이기 + 배경음악/오디오 믹스(ffmpeg concat·amix, 사용자 요청), 8단계 타임라인 UI

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
