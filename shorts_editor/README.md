# 쇼츠 메이커 (1단계)

강의 영상에서 쇼츠 구간을 뽑는 로컬 웹앱. 1단계: 영상 불러오기 · 재생 · 구간 표시/저장.

## 실행 (Windows)
1. Python 설치 (설치 시 "Add python.exe to PATH" 체크)
2. `winget install Gyan.FFmpeg` (영상 길이 읽기용, 이후 단계에서 필수)
3. `start.bat` 더블클릭 → 브라우저가 열림
4. 폰: 같은 와이파이에서 창에 표시된 `http://PC주소:8765` 접속

## 실행 (그 외)
```
pip install -r requirements.txt && python run.py
```

## 주의
- 같은 네트워크의 누구나 접속할 수 있으니 공용 와이파이에서는 쓰지 마세요.
- 작업 데이터는 `work/` 폴더에 저장됩니다 (git 제외).
