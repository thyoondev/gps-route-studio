# GPS Route Studio

Mac 지도에서 경로를 만들고 USB로 연결한 iPhone 또는 Android에 테스트 위치를 전송하는 개발 도구입니다. UI는 한국어입니다.

## 기능

- 지도 경유지 편집과 핀 클러스터링
- 속도·위치 지터 조절, 일시정지·재개, 반복·왕복 재생
- iPhone XCTest 기반 위치·속도 전달 및 수신 진단
- Android 표준 모의 위치 도우미와 복구 상태 확인
- 별도 Python CLI의 GPX 경로 재생

**실험용 개발 도구입니다.** 앱이 표시하는 전송·수신 속도와 다른 앱의 기록 인정 여부는 다릅니다. Android 위치는 모의 위치로 표시되며, 탐지 표시 숨김·센서 위조·루팅 기능은 포함하지 않습니다. 다른 앱의 주행·게임·보상 기록 인정을 보장하지 않습니다.

## 준비

현재 빌드 스크립트는 **Apple Silicon Mac, macOS 14 이상**을 대상으로 합니다.

- Xcode 전체 설치 및 Command Line Tools 설정
- Python 3.13과 [uv](https://docs.astral.sh/uv/)
- Android 빌드: Java 17, Android SDK Platform 35, Build Tools 36.0.0, Platform Tools
- Android SDK 기본 경로: `~/Library/Android/sdk`
- iPhone: 개발자 모드, Mac 신뢰, 본인의 Apple 개발 서명 설정
- Android: Android 12 이상, USB 디버깅 및 Mac 연결 허용

Python 환경은 GUI가 찾는 다음 위치에 설치합니다.

```sh
uv tool install --python 3.13 'pymobiledevice3==9.31.0'
```

GUI는 `~/.local/share/uv/tools/pymobiledevice3/bin/python3`를 사용합니다. uv 저장 위치를 바꿨다면 `Studio.swift`와 `Diagnostics.swift`도 조정해야 합니다. 테스트한 `pmd-pytcp` 버전은 0.0.4입니다. 의존성 설치에는 네트워크가 필요합니다.

## 빌드

1. iPhone을 사용할 경우 `GPSReceiver.xcodeproj`를 Xcode에서 열고 **GPSReceiver와 GPSMotionUITests 두 타깃**의 Signing Team을 본인 계정으로 설정합니다. 공개본에는 Team ID가 없습니다. Bundle Identifier를 변경한다면 `diagnostic_worker.py`의 `local.gpsroute.receiver` 실행 대상도 같은 값으로 바꾸세요.
2. Android 도우미와 Mac 앱을 순서대로 빌드합니다. 현재 Mac 패키징은 Android APK도 요구합니다.

```sh
./Android/build.sh
./build-mac.sh
open 'GPS Route Studio.app'
```

Android 개발용 서명 키는 `Android/.local/`에 생성됩니다. 이 폴더는 Git에서 제외됩니다. Mac 앱은 로컬 임시 서명을 사용하며 공증된 배포 바이너리가 아닙니다. iPhone 서명 설정을 바꾼 뒤에는 Mac 앱도 다시 빌드하세요.

## 사용과 GPS 복구

1. 다른 위치 시뮬레이터를 종료합니다. GeoShift를 사용 중이었다면 Restore GPS를 누른 뒤 종료하세요.
2. USB 기기를 연결하고 Mac 앱에서 플랫폼과 기기를 선택합니다.
3. 경로·속도·지터를 설정하고 시작합니다. Android가 요청하면 개발자 옵션에서 GPS Route Bridge를 모의 위치 앱으로 지정합니다.
4. 테스트가 끝나면 **중지 · GPS 복구**를 누르고 복구 완료 응답을 확인한 뒤 케이블을 분리합니다.

연결이 끊기거나 앱이 강제 종료되면 복구가 즉시 완료되지 않을 수 있습니다. 기기를 다시 연결하고 앱의 복구 절차를 진행하세요. Android 도우미에는 연결 단절 시 정리 기능이 있지만, 복구 완료를 확인하는 것이 필요합니다. 정상 위치가 돌아오지 않으면 위치 시뮬레이터를 모두 중지하고 기기를 재시작하세요.

CLI 예제(서울시청 → 남산의 공개 지점, 실제 도로 경로가 아닌 직선 보간):

```sh
~/.local/share/uv/tools/pymobiledevice3/bin/python3 gpsroute.py \
  -p 37.5665,126.9780 -p 37.5512,126.9882 --speed 40 --jitter 3
~/.local/share/uv/tools/pymobiledevice3/bin/python3 gpsroute.py \
  --gpx example.gpx --speed 40
```

`example.gpx`는 사용자가 준비한 파일입니다. CLI는 Ctrl-C로 종료할 때 GPS 복구를 시도합니다. 기기에서 실제 위치 복귀도 확인하세요.

## 검증

기기를 연결하거나 위치를 전송하지 않는 단위 테스트:

```sh
~/.local/share/uv/tools/pymobiledevice3/bin/python3 -m unittest discover -v
```

단위 테스트는 경로 계산·프로토콜·복구 분기를 검사합니다. 모든 iOS/Android 버전의 동작, 실제 기기 연결, 제3자 앱의 기록 인정까지 검증하지는 않습니다. 새 기기에서는 짧은 테스트 경로로 시작·일시정지·복구를 별도로 확인하세요.

## 개인정보와 라이선스

설정·경로·기기 복구 정보는 실행 중 로컬에 저장될 수 있습니다. 지도는 Apple MapKit을 이용하므로 지도 표시를 위한 네트워크 통신이 발생할 수 있습니다. 실제 이동 경로, 기기 ID, 개발팀 ID, 로그, 캡처, 서명 키를 이슈나 커밋에 포함하지 마세요.

프로젝트 코드는 **GPL-3.0-or-later**로 배포합니다. [LICENSE](LICENSE)와 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하세요.
