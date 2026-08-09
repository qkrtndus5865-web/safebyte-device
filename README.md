# Safe-Byte 하드웨어 (장치 측 코드)

2026 한이음 드림업 · 팀 청시울 · **Safe-Byte**
마트 카트 손잡이에 다는 식품 라벨 알레르겐 스캐너의 **장치 쪽 코드**입니다.

백엔드(FastAPI)와 모바일 앱(Flutter)은 별도 저장소
[`seojaeohcode/safe-byte`](https://github.com/seojaeohcode/safe-byte) 에 있고,
이 저장소는 그 백엔드에 붙는 **라즈베리파이 5 + 아두이노 나노** 코드입니다.

---

## 1. 전체 구조

```
 ┌──────────────┐   시리얼(USB)   ┌──────────────┐   HTTP    ┌──────────────┐
 │ 아두이노 나노 │ ◄────────────► │ 라즈베리파이5 │ ◄───────► │  백엔드 서버  │
 └──────────────┘                └──────────────┘           └──────────────┘
  버튼 / 조명 / 부저                카메라 + 7" 화면            OCR + 위험도 판정
```

판정(어떤 성분이 위험한지)은 **전부 백엔드에서** 합니다.
라즈베리파이는 찍고, 보내고, 보여주기만 하는 **씬 클라이언트(thin client)** 입니다.
아두이노는 판단을 하지 않고 버튼·조명·부저만 담당합니다.

### 한 번의 스캔

| 순서 | 주체 | 하는 일 |
|---|---|---|
| 1 | 나노 | 버튼 눌림 감지 → 흰색 조명 켬 → `SHOOT` 전송 |
| 2 | 파이 | 카메라 촬영 (`CAPTURING`) |
| 3 | 파이 | 나노에 `DONE` 전송 → 조명 끔 |
| 4 | 파이 | `POST /api/v1/analyze/image` 호출 (`ANALYZING`) |
| 5 | 파이 | 판정 표시 (`RESULT`: SAFE / CAUTION / DANGER) |
| 6 | 파이 | DANGER면 나노에 `DANGER` 전송 → 부저 0.6초 |

---

## 2. 파일

| 파일 | 설명 |
|---|---|
| `safebyte_nano/safebyte_nano.ino` | 아두이노 나노 펌웨어. 버튼 디바운스, 네오픽셀 조명, 부저 |
| `safebyte_pi.py` | 라즈베리파이 드라이버. 시리얼 + 카메라 + 백엔드 호출 + 로컬 웹서버 |
| `index.html` | 7인치 LCD에 띄우는 화면 (순수 HTML/CSS/JS) |
| `배선_연결도.md` | 전원선·신호선·GND 배선 정리 |
| `device.json` | 실행 중 생성되는 기기 설정. **개인정보 없음** (git 제외) |

---

## 3. 시리얼 프로토콜

나노와 파이가 주고받는 약속. 양쪽 다 **9600 baud**, 줄 끝에 `\n`.

| 방향 | 메시지 | 뜻 |
|---|---|---|
| 나노 → 파이 | `SHOOT` | 버튼이 눌렸다 |
| 파이 → 나노 | `DONE` | 촬영 끝났으니 조명 꺼라 |
| 파이 → 나노 | `DANGER` | 위험 판정이니 부저 울려라 |

나노는 `Serial.readStringUntil('\n')`, 파이는 pyserial `readline()` 으로 받습니다.

---

## 4. ★ 공용 기기 정책

**이 기기는 여러 사람이 돌아가며 쓰는 카트에 달립니다.**
앞사람의 알레르기 기준이 남아 있으면 다음 사람에게 잘못된 "안전" 판정을 보여줄 수 있습니다.
그래서 다음 원칙을 코드로 강제합니다.

1. **사용자 기준은 디스크에 저장하지 않는다.**
   프로필·이름·로그인 토큰은 메모리 세션에만 존재합니다.
   디스크에 남는 `device.json` 에는 OCR 모드와 테스트 문구만 들어갑니다.

2. **세션이 사라지는 경우 3가지**
   - 사용 종료 버튼 (상단 배지 또는 설정 화면)
   - 마지막 조작 후 `SESSION_TIMEOUT_SEC`(기본 15분) 무동작 → 자동 삭제
   - 전원 차단 (메모리라 자동)

3. **기준이 없으면 판정하지 않는다.**
   기준을 설정하지 않은 사람은 "전부 걱정된다"가 아니라 "가리는 게 없다"는 뜻입니다.
   알레르기 전체를 기본값으로 넣으면 웬만한 가공식품이 다 위험으로 떠서
   경고가 무의미해지고(알람 피로), 유료 OCR 비용만 나갑니다.
   → 촬영·분석을 하지 않고 `NEED_SETUP` 화면으로 기준 설정을 요청합니다.

4. **결과 화면에 '적용된 기준'을 항상 표시한다.**
   잘못된 기준으로 나온 판정을 사용자가 눈치챌 수 있어야 합니다.

---

## 5. 실행

### 지금(부품 없이, 윈도우에서)

```bash
python safebyte_pi.py
```

브라우저에서 `http://localhost:8080` 접속.
`pyserial`/`picamera2`가 없으면 자동으로 목(mock) 모드로 동작합니다.
물리 버튼 대신 **스페이스바** 또는 **대기화면 더블클릭**으로 스캔을 시작할 수 있습니다.

### 라즈베리파이 5에서

```bash
sudo apt install python3-picamera2
pip install pyserial
python3 safebyte_pi.py
chromium-browser --kiosk http://localhost:8080
```

### 바꿔야 하는 설정 (`safebyte_pi.py` 상단)

| 값 | 지금 | 파이에서 |
|---|---|---|
| `BACKEND_URL` | `http://127.0.0.1:8000` | Ncloud 서버 주소 |
| `SERIAL_PORT` | `COM3` (윈도우) | `/dev/ttyACM0` 또는 `/dev/ttyUSB0` |
| OCR 모드 | `mock` (무료) | **`live`** (실제 CLOVA) ← 시연 전 필수 |

---

## 6. 백엔드 API 계약

이 코드가 실제로 쓰는 엔드포인트는 두 개입니다.

### `POST /api/v1/analyze/image` (인증 불필요)

form-data:

| 필드 | 값 |
|---|---|
| `file` | JPEG 이미지 |
| `profiles` | `halal,allergy:peanut,allergy:shrimp` 처럼 쉼표로 연결 |
| `ocr_mode` | `live` 또는 `mock` |
| `mock_text` | `ocr_mode=mock` 일 때 **필수** |
| `force_ocr` | `false` (캐시 무시 여부) |

응답에서 쓰는 부분:

```jsonc
{
  "request_id": "...",
  "risk": {
    "final_level": "DANGER",          // SAFE / CAUTION / DANGER
    "summary": "직접 함유 주의 성분: 우유, 밀/글루텐 ...",
    "detections": [                    // ★ 평평한 리스트 (그룹 딕셔너리 아님)
      {
        "canonical_name": "우유",
        "matched_keyword": "유청",
        "occurrence_type": "direct",   // direct / cross_contact / unknown
        "occurrence_label": "직접 함유",
        "reason": "우유는 국내 식품 알레르기 유발물질 표시 대상입니다.",
        "evidence": "...정제염, 유청분말, 대두유... 알레르기 유발물질: 밀, 대두, 우유 함유",
        "severity": "danger"
      }
    ]
  },
  "ocr": {
    "provider": "clova",               // clova / mock / none
    "cached": false,                   // 같은 사진의 OCR 결과 재사용 여부
    "cache_backend": "redis",          // redis / sqlite
    "called_paid_api": true,           // 이번 요청이 실제로 과금됐는지
    "corrected_text": "원재료명: ..."
  }
}
```

`occurrence_type == "cross_contact"` 인 항목은 **제조시설 공유**(직접 함유 아님)로 분리해
화면에서 따로 보여줍니다.

### `POST /api/v1/auth/login`

응답의 `user.profiles` 를 그대로 세션에 넣어 **로그인 즉시 필터가 적용**됩니다.
별도로 `/api/v1/me` 를 부를 필요가 없습니다.

---

## 7. 화면(로컬 웹서버) 엔드포인트

`safebyte_pi.py` 가 여는 경로입니다. 화면은 `/state` 를 0.4초마다 폴링합니다.

| 경로 | 메서드 | 하는 일 |
|---|---|---|
| `/` | GET | `index.html` 전송 |
| `/state` | GET | 현재 상태 + 세션 정보 |
| `/config` | GET | 알레르기 선택지 카탈로그, 기기 설정, 하드웨어 상태 |
| `/health` | GET | 백엔드 연결 확인 |
| `/shoot` | POST | 물리 버튼 없이 스캔 시작 |
| `/dismiss` | POST | 결과 화면 닫기 |
| `/settings` | POST | 이번 세션의 기준 저장 |
| `/login` `/logout` | POST | 계정 기준 가져오기 / 세션 종료 |
| `/session/end` | POST | ★ 사용 종료 (개인 기준 즉시 삭제) |
| `/keepalive` | POST | 화면 조작 중 세션 만료 연기 |

---

## 8. 남은 작업

- [ ] `NUM_PIXELS` — 네오픽셀 LED 실제 개수로 수정 (`safebyte_nano.ino`)
- [ ] 7인치 LCD 모델 확정 → DSI/HDMI, 터치 배선 (`배선_연결도.md` 의 ⚠️)
- [ ] `SESSION_TIMEOUT_SEC` 값 확정 (기본 15분)
- [ ] BLE 프로필 전송 — 설계서 방식. 카트 화면에서 타이핑하지 않아도 되게
- [ ] 계정에 기준이 비어 있는 사용자 전용 안내 문구
