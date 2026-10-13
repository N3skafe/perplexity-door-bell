# Perplexity 완료 알림

Perplexity 데스크톱 앱에서 응답이 끝나면 소리와 팝업으로 알려 주는 Windows 프로그램입니다. 라이선스는 [MIT](LICENSE)입니다.

Python을 설치하지 않아도 됩니다. 릴리스의 `PerplexityDoorBell.exe`만 받아 실행하면 됩니다.

## 필요한 것

- Windows 10 또는 Windows 11
- [Perplexity 데스크톱 앱](https://www.perplexity.ai/download)

화면 언어가 한국어인 Perplexity에서 동작을 확인했습니다.

## 받는 방법

1. [릴리스](https://github.com/N3skafe/perplexity-door-bell/releases/latest)에서 `PerplexityDoorBell.exe`를 받습니다.
2. 원하는 폴더에 두고 두 번 클릭합니다.
3. Windows가 게시자를 확인할 수 없다는 창을 띄우면, 서명이 없는 파일이라서 그렇습니다. 내용을 확인한 뒤 실행을 선택합니다.

처음 실행하면 exe 옆에 `config.json`과 `sounds\done.wav`가 만들어집니다. 언어, 소리, 팝업 설정은 그 파일에 저장되고 다음에 켜도 유지됩니다.

## 쓰는 방법

실행하면 감시가 바로 시작됩니다.

- **언어**: 한국어, English, 中文, 日本語. 이 프로그램 화면의 언어입니다.
- **찾아보기**: 알림에 쓸 wav 파일을 고릅니다.
- **들어보기**: 고른 소리를 미리 듣습니다.
- **끝났을 때 팝업 표시**: 완료 팝업을 켜거나 끕니다.

Perplexity를 끄고 다시 켜도, 프로세스 이름과 창 제목, 입력란으로 새 창을 다시 찾습니다.

## 알림이 나올 때

화면에 `응답 중지(Esc)` 버튼이 보였다가 1초 동안 사라지면 한 번 알립니다. 정상으로 끝난 경우, 사용자가 취소한 경우, 오류로 멈춘 경우가 모두 이 알림에 포함됩니다. 같은 생성에는 소리가 한 번만 납니다.

## 알림이 나오지 않을 때

- Perplexity를 끈 경우
- 창을 읽지 못한 경우
- 창이 최소화되어 있는 동안
- 최소화되어 있던 그 질문이, 창을 다시 연 직후에 이미 끝나 있는 경우

최소화 중에는 앱이 켜져 있는지만 알 수 있습니다. 답을 만드는 중인지, 끝났는지는 창을 펼친 뒤에야 구분합니다.

## 읽지 않는 것

답변 본문, 입력란에 쓴 글, 로그인 정보는 읽지 않습니다. 네트워크로 보내지도 않습니다. 클릭이나 키 입력으로 Perplexity 창을 조작하지 않습니다.

## 직접 빌드

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm doorbell.spec
```

결과 파일은 `dist\PerplexityDoorBell.exe`입니다.
