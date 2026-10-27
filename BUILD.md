# 실행 파일(exe) 만들기

설치 없이 더블클릭으로 바로 실행되는 Windows용 폴더를 만든다. `dist/중주연습 1.2/`
폴더 전체(exe + `_internal`)를 통째로 옮기면 다른 PC에서도 그대로 실행된다(약 160MB).

## 준비

```
pip install -r requirements.txt pyinstaller
```

## 빌드

```
python -m PyInstaller --noconfirm "중주연습 1.2.spec"
```

처음부터 spec을 다시 만들 때:

```
python -m PyInstaller --name "중주연습 1.2" --windowed --onedir ^
  --icon "assets/ensemble.ico" --add-data "assets/ensemble.png;assets" --collect-data verovio --noconfirm app.py
```

## 주의

- `--collect-data verovio` 필수 — 악보 조판용 글꼴 리소스(`verovio/data`, 약 6MB)를 같이 묶는다.
- verovio는 리소스 경로에 한글이 있으면 악보를 못 읽는다(폴더 이름이 한글이라
  걸렸음, 2026-10-07). `score_view._ascii_resource_dir()`가 이럴 때 리소스를 임시 폴더
  (`%TEMP%\GenerateScore\verovio_data_…`)로 한 번 복사해 쓴다.
- 기록·설정·오류 로그(`generate_history.json`, `generate_settings.json`, `generate_error.log`)는
  `_internal`이 아니라 **exe 바로 옆**에 생긴다(포터블 앱).
- `--noconfirm`은 같은 이름의 `dist/…` 폴더를 통째로 지우고 다시 만든다 — exe 옆 기록·설정 파일도 지워지니 미리 복사해 둘 것.
- 창(windowed) 빌드라 콘솔이 없다 — 악보 열기·합성이 실패하면 `generate_error.log`를 본다.
- 아이콘: `python make_icon.py`로 `assets/ensemble.ico`(exe)·`assets/ensemble.png`(창)를 만든다(2026-10-08, "중주연습 1.2").

## Mac(.app) 빌드 (2026-10-27)

빌드하는 Mac의 CPU용으로 나온다 — 인텔 Mac에서 만들면 x86_64(애플실리콘 Mac에서도 Rosetta 2로 실행됨),
애플실리콘 Mac에서 만들면 arm64(네이티브). PyInstaller는 다른 CPU용으로 교차 빌드하지 못한다.

준비(관리자 암호 없이 — 시스템 `/usr/bin/python3` 3.9는 Tk가 오래돼 쓰지 않는다):

```
python3 -m pip install --user uv
~/Library/Python/3.9/bin/uv python install 3.12
~/Library/Python/3.9/bin/uv venv -p 3.12 .venv-mac
~/Library/Python/3.9/bin/uv pip install -p .venv-mac -r requirements.txt pyinstaller
.venv-mac/bin/python make_icon.py          # assets/ensemble.icns
```

빌드(Windows용 `dist/`를 지우지 않도록 따로 둔다):

```
.venv-mac/bin/python -m PyInstaller --noconfirm --distpath dist-mac --workpath build-mac "중주연습 1.3 mac.spec"
```

결과: `dist-mac/중주연습 1.3.app`(약 130MB). 주의:

- 번들 안 실행 파일 이름은 영문(`ensemble`)이어야 한다 — 한글이면 codesign 검증이 깨진다. 앱 이름은 한글 그대로.
- 기록·설정·오류 로그는 `~/Library/Application Support/중주연습/`에 생긴다(.app 안에 쓰면 서명이 깨짐).
- 마이크 권한 문구(`NSMicrophoneUsageDescription`)는 spec의 `info_plist`에 있다 — 빼면 소리 지연 자동 맞추기가 무음만 받는다.
- 임시(ad-hoc) 서명이라 다른 Mac에서 내려받으면 "확인되지 않은 개발자" 경고가 뜬다 — 우클릭 → 열기,
  또는 `xattr -cr "중주연습 1.3.app"`. 정식 배포는 Apple Developer 계정으로 서명·공증이 필요하다.
  macOS 15(Sequoia)부터는 우클릭 → 열기로도 안 열리고, 한 번 열어 본 뒤 시스템 설정 → 개인정보 보호 및 보안 →
  맨 아래 "그래도 열기"를 눌러야 한다.

### 애플실리콘(arm64)용 — GitHub Actions (2026-10-27)

인텔 Mac에서는 arm64를 만들 수 없어 GitHub의 애플실리콘 Mac 서버에서 빌드한다:
`.github/workflows/build-mac-arm64.yml`(macos-14, 단위 테스트 → 아이콘 → PyInstaller → arm64·서명 확인 → zip).
main에 올리면 자동으로, 또는 저장소 Actions 탭 → "Mac 애플실리콘 빌드" → Run workflow. 끝나면(약 5~10분) 그 실행
화면 아래 Artifacts의 `중주연습-mac-arm64`를 내려받아 압축을 풀면 `중주연습 1.3.app`. 올리는 파일은 `.gitignore`가
정한다(악보·결과·설정·로그·빌드 폴더 제외). 비공개 저장소는 Mac 빌드 시간이 무료 한도에서 10배로 계산된다
(무료 2000분 → Mac 약 200분, 빌드 한 번 약 10분).
