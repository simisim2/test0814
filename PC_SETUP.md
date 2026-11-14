# PC에서 이어서 작업하기

## A. 앱 실행해 보기 (처음 한 번)
1. **Python 설치**: https://www.python.org/downloads/ — 설치 첫 화면에서 **Add python.exe to PATH 체크**
2. **ffmpeg 설치**: 명령 프롬프트(cmd)에서 `winget install Gyan.FFmpeg` 후 창 닫기
3. **Git 설치**: https://git-scm.com/download/win (기본값으로 Next)
4. **코드 받기** (cmd):
   ```
   cd %USERPROFILE%\Documents
   git clone -b claude/image-screen-analysis-5akenl https://github.com/simisim2/test0814.git
   cd test0814\shorts_editor
   ```
   (GitHub 로그인 창이 뜨면 로그인)
5. **`start.bat` 더블클릭** → 브라우저가 `http://localhost:8765` 로 열림
6. 폰: 같은 와이파이에서 검은 창에 나온 `http://192.168.x.x:8765` 접속 (방화벽 "허용" 클릭)

## B. 체크리스트
- [ ] "내 PC 영상" 목록이 보인다
- [ ] 영상 클릭 → 재생, Space 재생/정지, 진행 바 이동
- [ ] `[4기]` 들어간 파일 경로 붙여넣기 → 열림
- [ ] I/O 로 구간 지정 → 구간 저장 → 새로고침 후에도 유지
- [ ] 폰에서 접속·재생

문제가 생기면 **검은 창 + 브라우저 화면을 캡처**해서 Claude에게 보여 주세요.

## C. PC에서 Claude Code 로 이어서 개발하기
1. Claude Code 설치: https://claude.com/claude-code (데스크톱 앱 또는 `npm i -g @anthropic-ai/claude-code`)
2. 앱에서 폴더 `...\test0814` 를 열거나, cmd에서 `cd test0814` 후 `claude`
3. 이 폴더의 `CLAUDE.md` 를 자동으로 읽어서 맥락이 이어집니다.
4. 첫 메시지 예시:
   > CLAUDE.md 와 SHORTS_EDITOR_CONTEXT.md 읽고, 2단계(자막 생성)부터 이어서 만들어줘. 먼저 내 PC에서 start.bat 으로 1단계가 잘 도는지 확인해줘.
5. PC에서 돌리면 Claude 가 직접 서버를 켜고 실제 영상으로 테스트할 수 있습니다.

## D. 변경 내용 GitHub 에 올리기 (Claude 에게 "커밋하고 푸시해줘" 하면 됨)
- 작업 브랜치: `claude/image-screen-analysis-5akenl`

## E. 2단계 이후에 필요한 것
- 2단계 자막: `pip install faster-whisper` (처음 실행 때 모델 자동 다운로드, 수백 MB). 1시간 영상은 CPU에서 수십 분 → 진행률 표시 필요
- 3단계 AI 추천: Claude API 키 (https://console.anthropic.com → API Keys). `shorts_editor/.env` 에 `ANTHROPIC_API_KEY=sk-ant-...` 저장. **채팅·GitHub 에 붙여넣지 말 것**
