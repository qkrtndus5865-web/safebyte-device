# -*- coding: utf-8 -*-
"""
Safe-Byte 라즈베리파이 5 프론트엔드 드라이버
===========================================================================
선배 저장소(seojaeohcode/safe-byte)의 백엔드 API 계약과 모바일 앱 기능을
그대로 따르되, 마트 카트 손잡이에 달리는 공용 하드웨어에 맞춰 재구성했다.

[ 전체 그림 ]
  아두이노 나노  --시리얼(USB)-->  라즈베리파이 5  --HTTP-->  백엔드(FastAPI)
       버튼/조명/부저                 카메라 + 화면              OCR + 위험도 판정

[ 한 번의 스캔 흐름 ]
  1) 나노가 버튼 눌림을 감지 → 조명(흰색) 켬 → "SHOOT" 전송
  2) 파이가 카메라로 촬영             → 상태 CAPTURING
  3) 파이가 나노에 "DONE" 전송 → 나노가 조명 끔
  4) 파이가 백엔드 POST /api/v1/analyze/image 호출 → 상태 ANALYZING
  5) 판정 결과 수신(SAFE/CAUTION/DANGER) → 상태 RESULT
  6) DANGER면 나노에 "DANGER" 전송 → 나노가 부저 0.6초

★ [ 공용 기기 원칙 — 이 파일의 가장 중요한 설계 ]
  이 기기는 마트 카트에 달린 '여러 사람이 돌아가며 쓰는' 장치다.
  앞사람의 알레르기 기준이 남아 있으면 다음 사람에게 잘못된 '안전' 판정을
  보여줄 수 있으므로, 사용자 기준은 절대 디스크에 저장하지 않는다.

    · 사용자 기준(프로필·이름·로그인 토큰)  → 메모리에만 존재하는 '세션'
    · 세션이 사라지는 경우
        - 사용 종료 버튼을 누를 때
        - 마지막 조작 후 SESSION_TIMEOUT_SEC 동안 아무 동작이 없을 때
        - 전원을 껐다 켤 때(메모리는 전원이 끊기면 지워지므로 자동)
    · 세션이 없으면 개인 기준 없이 '기본 기준'으로만 검사하고,
      화면에 개인 기준이 아니라는 사실을 계속 표시한다.
    · 판정 결과 화면에도 '어떤 기준으로 검사했는지'를 항상 함께 보여준다.
      (엉뚱한 기준으로 나온 판정을 사용자가 눈치챌 수 있어야 하므로)

  디스크에 저장하는 것은 사람과 무관한 기기 설정(OCR 모드, 테스트 문구)뿐이다.

[ 화면 구조 ]
  이 프로그램이 로컬 웹서버(기본 http://localhost:8080)를 띄우고,
  7인치 LCD의 크로미움이 그 주소에 접속해 index.html 을 띄운다.
  화면은 /state 를 주기적으로 물어보며 현재 상태를 그린다.

[ 의존성 ]
  파이썬 표준 라이브러리만으로 동작한다(http.server, urllib, json, threading).
  아래 두 개는 '있으면 실물, 없으면 목(mock)'으로 자동 전환된다.
    - pyserial   : 나노와 시리얼 통신          (없으면 화면 조작으로 대체)
    - picamera2  : 라즈베리파이 카메라 촬영     (없으면 test_label.jpg 또는 흰 이미지)

[ 실행 ]
  python safebyte_pi.py
  브라우저에서 http://localhost:8080 접속
  (라즈베리파이 전체화면: chromium-browser --kiosk http://localhost:8080)
"""

import io
import os
import json
import time
import uuid
import threading
import http.server
import urllib.error
import urllib.request
from urllib.parse import urlparse

# ═════════════════════════════════════════════════════════════════════════
# 1. 설정 — 환경이 바뀌면 여기만 고치면 된다
# ═════════════════════════════════════════════════════════════════════════
BACKEND_URL     = "http://127.0.0.1:8000"   # 백엔드 FastAPI 주소(배포 시 Ncloud 서버 주소로)
SERIAL_PORT     = "COM3"                    # 윈도우: COM3 / 라즈베리파이: /dev/ttyACM0 또는 /dev/ttyUSB0
BAUD            = 9600                      # 나노 스케치의 Serial.begin(9600) 과 반드시 동일
UI_PORT         = 8080                      # 화면(브라우저)이 접속할 로컬 포트
RESULT_HOLD_SEC = 12                        # 결과를 몇 초 보여준 뒤 대기화면으로 되돌릴지(0이면 수동)
REQUEST_TIMEOUT = 40                        # 백엔드 응답 대기 최대 시간(초)

# ★ 공용 기기 정책
SESSION_TIMEOUT_SEC = 15 * 60               # 마지막 조작 후 이 시간이 지나면 개인 기준을 지운다
SESSION_CHECK_SEC   = 20                    # 세션 만료를 몇 초마다 확인할지
#
# 세션이 없을 때는 '기본 기준'을 대신 쓰지 않는다. 그 이유:
#   1) 기준을 설정하지 않은 사람은 '전부 걱정된다'가 아니라 '가리는 게 없다'는 뜻이다.
#      알레르기 전체로 검사하면 웬만한 가공식품이 다 위험으로 나와 의미가 없다.
#   2) 헛경보가 반복되면 사용자가 경고 자체를 믿지 않게 된다(알람 피로).
#      정작 진짜 위험할 때 무시하게 되므로 안 하느니만 못하다.
#   3) CLOVA OCR은 호출당 요금이 나가는 유료 API다. 판정할 기준도 없이 부르면
#      비용만 나간다.
# 따라서 기준이 없으면 촬영·분석을 하지 않고 기준 설정을 먼저 요청한다.

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DEVICE_PATH = os.path.join(SCRIPT_DIR, "device.json")   # ★ 사람과 무관한 기기 설정만 저장

# 기기 설정 기본값 — 여기에 개인정보는 절대 넣지 않는다
DEFAULT_DEVICE = {
    "ocr_mode": "mock",                     # "mock"=무비용 테스트 / "live"=실제 CLOVA OCR(유료)
    "mock_text": "원재료명: 밀가루, 팜유, 정제염, 대두, 우유, 계란\n"
                 "알레르기 유발물질: 밀, 대두, 우유, 계란 함유\n"
                 "땅콩을 사용한 제품과 같은 제조시설에서 제조",
}

# ═════════════════════════════════════════════════════════════════════════
# 2. 알레르기 선택지 카탈로그
#    선배 앱(mobile/lib/main.dart 의 allergyChoices)과 백엔드
#    (app/services/profile_tokens.py 의 ALLERGEN_OPTIONS)를 그대로 옮긴 것.
#    화면은 이 목록을 /config 로 받아 그리므로, 항목 추가는 여기만 고치면 된다.
# ═════════════════════════════════════════════════════════════════════════
ALLERGY_CATEGORIES = ["전체", "기본", "견과/곡물", "해산물", "육류", "과채/첨가물"]

ALLERGY_CHOICES = [
    # (토큰, 표시이름, 분류, 검색어)
    ("allergy:milk",      "우유",     "기본",        "milk dairy 유제품 유청 분유 버터 치즈"),
    ("allergy:egg",       "계란",     "기본",        "egg 난류 알류 난백 난황"),
    ("allergy:soy",       "대두",     "기본",        "soy 콩 두류 두유 레시틴"),
    ("allergy:wheat",     "밀",       "견과/곡물",   "wheat gluten 소맥 밀가루 글루텐"),
    ("allergy:buckwheat", "메밀",     "견과/곡물",   "buckwheat"),
    ("allergy:peanut",    "땅콩",     "견과/곡물",   "peanut 피넛"),
    ("allergy:walnut",    "호두",     "견과/곡물",   "walnut"),
    ("allergy:pine_nut",  "잣",       "견과/곡물",   "pine nut"),
    ("allergy:tree_nut",  "견과류",   "견과/곡물",   "tree nut almond cashew 아몬드 캐슈 피스타치오"),
    ("allergy:sesame",    "참깨",     "견과/곡물",   "sesame 깨"),
    ("allergy:shrimp",    "새우",     "해산물",      "shrimp prawn 갑각류"),
    ("allergy:crab",      "게",       "해산물",      "crab 꽃게 갑각류"),
    ("allergy:shellfish", "조개류",   "해산물",      "shellfish 굴 전복 홍합 바지락"),
    ("allergy:squid",     "오징어",   "해산물",      "squid 오징어"),
    ("allergy:mackerel",  "고등어",   "해산물",      "mackerel"),
    ("allergy:fish",      "생선류",   "해산물",      "fish 어류 멸치 참치 명태"),
    ("allergy:pork",      "돼지고기", "육류",        "pork lard 돈육 돈지 포크"),
    ("allergy:beef",      "쇠고기",   "육류",        "beef 소고기 우육 비프"),
    ("allergy:chicken",   "닭고기",   "육류",        "chicken 계육 치킨"),
    ("allergy:peach",     "복숭아",   "과채/첨가물", "peach"),
    ("allergy:tomato",    "토마토",   "과채/첨가물", "tomato"),
    ("allergy:sulfite",   "아황산류", "과채/첨가물", "sulfite 이산화황 메타중아황산"),
]

# 자주 쓰는 묶음 — 한 번에 여러 개를 켜고 끄는 버튼
ALLERGY_BUNDLES = [
    ("가장 흔한 항목", ["allergy:milk", "allergy:egg", "allergy:peanut",
                        "allergy:wheat", "allergy:soy"]),
    ("해산물",         ["allergy:shrimp", "allergy:crab", "allergy:shellfish",
                        "allergy:fish", "allergy:squid"]),
    ("견과/씨앗",      ["allergy:peanut", "allergy:walnut", "allergy:pine_nut",
                        "allergy:tree_nut", "allergy:sesame"]),
]

# 기본 프로필(알레르기 개별 선택과 별개로 켜는 큰 기준)
BASE_PROFILES = [
    ("halal",   "할랄",          "돼지고기·알코올·육류 관련 성분을 검사합니다"),
    ("vegan",   "비건",          "우유·계란 등 모든 동물성 원료를 검사합니다"),
    ("allergy", "알레르기 전체", "국내 표시 대상 알레르기 유발물질 전체를 검사합니다"),
]

# 토큰 → 사람이 읽는 이름 (결과 화면에 '적용된 기준'을 적기 위해)
LABEL_OF = {t: l for t, l, _c, _s in ALLERGY_CHOICES}
LABEL_OF.update({t: l for t, l, _d in BASE_PROFILES})


def label_of(token):
    return LABEL_OF.get(token, token.replace("allergy:", ""))


def log(*args):
    """콘솔 로그(바로 보이도록 flush)."""
    print("[safebyte]", *args, flush=True)


# ═════════════════════════════════════════════════════════════════════════
# 3. 선택적 하드웨어 라이브러리 (없으면 자동으로 목 모드)
# ═════════════════════════════════════════════════════════════════════════
try:
    import serial                     # pyserial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

try:
    from picamera2 import Picamera2   # 라즈베리파이 카메라
    HAS_CAMERA = True
except ImportError:
    HAS_CAMERA = False


# ═════════════════════════════════════════════════════════════════════════
# 4. HTTP 유틸 — requests 없이 표준 urllib 만으로 백엔드 호출
# ═════════════════════════════════════════════════════════════════════════
def _send(url, method="GET", body=None, headers=None, timeout=REQUEST_TIMEOUT):
    """요청을 보내고 (상태코드, 응답바이트)를 돌려준다. 4xx/5xx도 예외 없이 반환."""
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:          # 서버가 에러 코드를 준 경우
        return exc.code, exc.read()
    except urllib.error.URLError as exc:           # 서버에 닿지도 못한 경우
        raise RuntimeError(f"백엔드에 연결하지 못했습니다: {exc.reason}") from exc


def _encode_multipart(fields, files):
    """multipart/form-data 본문을 직접 만든다.
    multipart/form-data = 파일과 일반 값을 한 요청에 같이 담는 HTTP 인코딩 방식.
    각 항목을 boundary(구분 문자열)로 나눠 이어 붙인다."""
    boundary = "----SafeByte" + uuid.uuid4().hex
    buf = bytearray()
    for name, value in fields.items():
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        buf += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content, ctype) in files.items():
        buf += f"--{boundary}\r\n".encode()
        buf += (f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n').encode()
        buf += f"Content-Type: {ctype}\r\n\r\n".encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _decode(raw):
    """응답 바이트를 JSON으로 해석(실패하면 원문을 message에 담아 반환)."""
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"message": raw.decode("utf-8", errors="ignore")}


def _error_message(payload, status):
    """FastAPI의 오류 응답(detail)에서 사람이 읽을 문장을 뽑는다."""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, list) and detail:
        return str(detail[0].get("msg", detail[0]))
    if detail:
        return str(detail)
    return f"서버 오류 ({status})"


# ═════════════════════════════════════════════════════════════════════════
# 5. 기기 설정 — 사람과 무관한 값만 파일에 남긴다
# ═════════════════════════════════════════════════════════════════════════
device = dict(DEFAULT_DEVICE)
device_lock = threading.Lock()


def load_device():
    global device
    try:
        with open(DEVICE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        merged = dict(DEFAULT_DEVICE)
        merged.update({k: v for k, v in saved.items() if k in DEFAULT_DEVICE})
        device = merged
        log(f"기기 설정 불러옴: {DEVICE_PATH}")
    except FileNotFoundError:
        log("기기 설정 파일이 없어 기본값으로 시작합니다.")
    except Exception as exc:
        log("기기 설정 불러오기 실패(기본값 사용):", exc)


def save_device():
    try:
        with open(DEVICE_PATH, "w", encoding="utf-8") as f:
            json.dump(device, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log("기기 설정 저장 실패:", exc)


# ═════════════════════════════════════════════════════════════════════════
# 6. ★ 세션 — 사용자 기준은 여기에만 있고, 디스크로 절대 나가지 않는다
# ═════════════════════════════════════════════════════════════════════════
EMPTY_SESSION = {
    "active": False,        # 개인 기준이 설정된 상태인지
    "display_name": "",
    "profiles": [],         # 개인이 고른 프로필 토큰
    "token": None,          # 백엔드 로그인 토큰
    "email": None,
    "started": 0.0,
    "last_active": 0.0,
}
session = dict(EMPTY_SESSION)
session_lock = threading.Lock()


def touch_session():
    """사용자가 뭔가 조작했음을 기록 → 만료 시계를 다시 0부터 센다."""
    with session_lock:
        if session["active"]:
            session["last_active"] = time.time()


def start_session(profiles, display_name="", token=None, email=None):
    now = time.time()
    with session_lock:
        session.update(active=True,
                       profiles=list(profiles),
                       display_name=display_name or session["display_name"],
                       token=token if token is not None else session["token"],
                       email=email if email is not None else session["email"],
                       started=session["started"] or now,
                       last_active=now)


def end_session(reason="사용 종료"):
    """개인 기준·이름·로그인 토큰을 메모리에서 지운다."""
    with session_lock:
        if not session["active"] and not session["token"]:
            return False
        session.update(EMPTY_SESSION)
    log(f"세션 종료({reason}) → 개인 기준 삭제됨")
    reset_state()                      # 화면도 대기 상태로 되돌린다
    return True


def session_snapshot():
    """화면에 내려보낼 세션 정보(토큰은 절대 포함하지 않는다)."""
    with session_lock:
        remain = 0
        if session["active"]:
            remain = max(0, int(SESSION_TIMEOUT_SEC -
                                (time.time() - session["last_active"])))
        return {
            "active": session["active"],
            "display_name": session["display_name"],
            "profiles": list(session["profiles"]),
            "email": session["email"],
            "logged_in": bool(session["token"]),
            "remain_sec": remain,
        }


def active_profiles():
    """지금 검사에 실제로 쓸 프로필 토큰 목록.
    세션이 없으면 빈 목록 → 판정하지 않는다는 뜻."""
    with session_lock:
        if session["active"] and session["profiles"]:
            return list(session["profiles"])
    return []


def session_watchdog():
    """일정 시간 아무 조작이 없으면 개인 기준을 자동으로 지우는 감시 스레드.
    앞사람이 사용 종료를 누르지 않고 떠났을 때를 대비한 안전장치다."""
    while True:
        time.sleep(SESSION_CHECK_SEC)
        with session_lock:
            expired = (session["active"] and
                       time.time() - session["last_active"] > SESSION_TIMEOUT_SEC)
        if expired:
            end_session("자동 만료")


# ═════════════════════════════════════════════════════════════════════════
# 7. 공유 상태 — 화면이 /state 로 읽어가는 현재 상황
# ═════════════════════════════════════════════════════════════════════════
IDLE_STATE = {
    # IDLE(대기) / NEED_SETUP(기준 없음) / CAPTURING(촬영) /
    # ANALYZING(분석) / RESULT(결과) / ERROR(오류)
    "status": "IDLE",
    "level": None,          # SAFE / CAUTION / DANGER
    "title": "",            # 결과 큰 제목 (예: 먹기 전 멈춰요)
    "subtitle": "",         # 결과 부제
    "summary": "",          # 백엔드가 만든 요약 문장
    "direct": [],           # 직접 함유로 분류된 검출 목록
    "facility": [],         # 제조시설 공유(교차오염)로 분류된 검출 목록
    "ocr_provider": "-",
    "cache_label": "-",
    "paid": False,
    "ocr_text": "",
    "request_id": "",
    "message": "",
    # ★ 이 판정을 '어떤 기준으로' 냈는지. 앞사람 기준이 남아 잘못 판정한 경우를
    #    사용자가 바로 알아챌 수 있도록 결과 화면에 항상 함께 표시한다.
    "applied": [],          # 사람이 읽는 기준 이름 목록
}
state = dict(IDLE_STATE, version=0, updated=time.time())
state_lock = threading.Lock()


def set_state(**kw):
    """상태를 갱신하고 version을 1 올린다.
    화면은 version이 바뀐 것만 다시 그리므로 불필요한 렌더링이 없다."""
    with state_lock:
        state.update(kw)
        state["version"] += 1
        state["updated"] = time.time()
        return state["version"]


def reset_state():
    """무조건 대기 화면으로."""
    set_state(**IDLE_STATE)


def revert_idle(expected_version):
    """결과를 잠시 보여준 뒤 대기화면으로 복귀.
    그 사이 새 스캔이 시작돼 version이 달라졌으면 아무것도 하지 않는다."""
    with state_lock:
        if state["version"] != expected_version:
            return
        state.update(IDLE_STATE)
        state["version"] += 1
        state["updated"] = time.time()


def schedule_idle(version):
    if RESULT_HOLD_SEC > 0:
        threading.Timer(RESULT_HOLD_SEC, revert_idle, args=(version,)).start()


# ═════════════════════════════════════════════════════════════════════════
# 8. 카메라
# ═════════════════════════════════════════════════════════════════════════
def _capture_real():
    """라즈베리파이 카메라 모듈로 한 장 촬영해 JPEG 바이트로 반환."""
    picam = Picamera2()
    picam.configure(picam.create_still_configuration())
    picam.start()
    time.sleep(0.6)                    # 노출·화이트밸런스가 안정될 시간
    buf = io.BytesIO()
    picam.capture_file(buf, format="jpeg")
    picam.stop()
    picam.close()
    return buf.getvalue()


def _capture_mock():
    """목 모드: 같은 폴더의 test_label.jpg 를 쓰고, 없으면 흰 이미지를 만든다."""
    test_path = os.path.join(SCRIPT_DIR, "test_label.jpg")
    if os.path.exists(test_path):
        with open(test_path, "rb") as f:
            return f.read()
    try:
        from PIL import Image          # Pillow가 있으면 정상 JPEG 생성
        buf = io.BytesIO()
        Image.new("RGB", (900, 1200), (250, 250, 250)).save(buf, format="JPEG")
        return buf.getvalue()
    except Exception:
        # Pillow도 없을 때: 최소 형태의 흰 JPEG
        return bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300"
            + "ff" * 64 +
            "ffc2000b080001000101011100ffc40014000100000000000000000000000000000009"
            "ffda0008010100000001d2cf20ffd9"
        )


def capture_image():
    return _capture_real() if HAS_CAMERA else _capture_mock()


# ═════════════════════════════════════════════════════════════════════════
# 9. 백엔드 호출 — POST /api/v1/analyze/image
# ═════════════════════════════════════════════════════════════════════════
def analyze(image_bytes, profiles):
    """찍은 사진을 백엔드로 보내 판정 결과(JSON)를 받는다."""
    with device_lock:
        ocr_mode = device["ocr_mode"]
        mock_text = device["mock_text"]
    with session_lock:
        token = session["token"]

    fields = {
        "profiles": ",".join(profiles),
        "ocr_mode": ocr_mode,
        "force_ocr": "false",
    }
    if ocr_mode == "mock":
        # 백엔드 규칙: ocr_mode=mock 이면 mock_text 가 반드시 있어야 한다
        fields["mock_text"] = mock_text
    files = {"file": ("label.jpg", image_bytes, "image/jpeg")}

    body, content_type = _encode_multipart(fields, files)
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, raw = _send(BACKEND_URL + "/api/v1/analyze/image", "POST", body, headers)
    payload = _decode(raw)
    if status < 200 or status >= 300:
        raise RuntimeError(_error_message(payload, status))
    return payload


def verdict_title(level):
    """선배 앱의 verdictTitle() 과 같은 문구를 쓴다."""
    return {"DANGER": "먹기 전 멈춰요",
            "CAUTION": "한 번 더 확인하세요"}.get(level, "현재 기준 통과")


def verdict_subtitle(level, direct_count, facility_count):
    if level == "DANGER":
        return f"직접 함유 위험 성분 {direct_count}개가 감지됐습니다."
    if level == "CAUTION":
        if facility_count and not direct_count:
            return f"제조시설 공유 표기 {facility_count}건이 확인됐습니다."
        return "제조시설 공유 또는 확인이 필요한 성분이 있습니다."
    return "선택한 기준에서 주요 위험 성분이 감지되지 않았습니다."


def extract(result):
    """백엔드 응답에서 화면에 필요한 값만 뽑는다.

    실제 응답 구조(app/services/analysis.py 기준):
      { "request_id":..., "risk": {"final_level","summary","detections":[...]},
        "ocr": {"provider","cached","cache_backend","called_paid_api",
                "text","corrected_text"} }
    detections 의 각 항목은 평평한 딕셔너리이고, 직접 함유인지 제조시설 공유인지는
    occurrence_type 이 "cross_contact" 인지로 구분한다.
    """
    risk = result.get("risk", {}) or {}
    ocr = result.get("ocr", {}) or {}
    level = risk.get("final_level", "SAFE")

    direct, facility = [], []
    for item in risk.get("detections", []) or []:
        if not isinstance(item, dict):
            continue
        row = {
            "name": item.get("canonical_name", ""),
            "category": item.get("category", ""),
            "severity": item.get("severity", ""),
            "keyword": item.get("matched_keyword", ""),
            "reason": item.get("reason", ""),
            "evidence": item.get("evidence", ""),
            "label": item.get("occurrence_label", "문맥 확인"),
        }
        if item.get("occurrence_type") == "cross_contact":
            facility.append(row)
        else:
            direct.append(row)

    return {
        "level": level,
        "title": verdict_title(level),
        "subtitle": verdict_subtitle(level, len(direct), len(facility)),
        "summary": risk.get("summary", ""),
        "direct": direct,
        "facility": facility,
        "ocr_provider": ocr.get("provider", "-"),
        "cache_label": (f"hit/{ocr.get('cache_backend', 'cache')}"
                        if ocr.get("cached") else "miss"),
        "paid": bool(ocr.get("called_paid_api")),
        "ocr_text": ocr.get("corrected_text") or ocr.get("text") or "",
        "request_id": result.get("request_id", ""),
    }


# ═════════════════════════════════════════════════════════════════════════
# 10. 로그인 — 앱 계정의 '내 기준'을 이번 세션 동안만 빌려온다
# ═════════════════════════════════════════════════════════════════════════
def backend_login(email, password):
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    status, raw = _send(BACKEND_URL + "/api/v1/auth/login", "POST", body,
                        {"Content-Type": "application/json"})
    payload = _decode(raw)
    if status < 200 or status >= 300:
        raise RuntimeError(_error_message(payload, status))
    return payload


def push_preferences():
    """장치에서 바꾼 기준을 백엔드 계정에도 저장(로그인 상태일 때만)."""
    with session_lock:
        token = session["token"]
        payload = {
            "display_name": session["display_name"],
            "profiles": list(session["profiles"]),
            "allergy_notes": "",
        }
    if not token:
        return
    body = json.dumps(payload).encode("utf-8")
    try:
        _send(BACKEND_URL + "/api/v1/me/preferences", "PUT", body,
              {"Content-Type": "application/json",
               "Authorization": f"Bearer {token}"})
    except Exception as exc:
        log("기준 동기화 실패(이번 세션에는 반영됨):", exc)


# ═════════════════════════════════════════════════════════════════════════
# 11. 시리얼(나노) 링크
# ═════════════════════════════════════════════════════════════════════════
class SerialLink:
    """나노와 주고받는 약속(프로토콜)
        나노 → 파이 : "SHOOT"   버튼이 눌렸다
        파이 → 나노 : "DONE"    촬영이 끝났으니 조명을 꺼라
        파이 → 나노 : "DANGER"  위험 판정이니 부저를 울려라
    """

    def __init__(self):
        self.ser = None
        self.lock = threading.Lock()
        if HAS_SERIAL:
            try:
                # timeout       : 읽을 게 없을 때 최대 1초만 기다린다
                # write_timeout : 상대가 받아가지 않아도 1초 뒤 포기한다.
                #                 이걸 빼면 write()가 영원히 멈춰 스캔 전체가 멈춘다.
                self.ser = serial.Serial(SERIAL_PORT, BAUD,
                                         timeout=1, write_timeout=1)
                time.sleep(2)          # 포트를 열면 나노가 리셋되므로 준비될 때까지 대기
                log(f"시리얼 연결됨: {SERIAL_PORT} @ {BAUD}")
            except Exception as exc:
                log(f"시리얼 연결 실패({exc}) → 목 모드로 진행")
                self.ser = None

    def send(self, msg):
        """한 줄 전송. 나노는 readStringUntil('\\n') 으로 받으므로 개행을 붙인다."""
        with self.lock:
            if self.ser:
                try:
                    self.ser.write((msg + "\n").encode())
                except Exception as exc:
                    log("시리얼 송신 실패:", exc)
            else:
                log(f"[목-시리얼 송신] {msg}")

    def readline(self):
        if self.ser:
            raw = self.ser.readline()
            return raw.decode(errors="ignore").strip() if raw else ""
        return ""


# ═════════════════════════════════════════════════════════════════════════
# 12. 한 번의 스캔
# ═════════════════════════════════════════════════════════════════════════
scan_lock = threading.Lock()     # 버튼 연타로 두 번 동시에 도는 것을 막는다


def trigger_scan(link):
    if not scan_lock.acquire(blocking=False):
        log("이미 스캔 중 → 무시")
        return
    try:
        touch_session()                       # 스캔도 '조작'이므로 만료 시계를 되돌린다
        profiles = active_profiles()

        # ★ 기준이 없으면 판정하지 않는다.
        #   촬영도, 유료 OCR 호출도 하지 않고 기준 설정을 먼저 요청한다.
        #   나노는 버튼을 누른 순간 조명을 켰으므로 DONE을 보내 즉시 꺼 준다.
        if not profiles:
            link.send("DONE")
            log("기준 없음 → 판정하지 않고 설정 요청")
            version = set_state(**dict(IDLE_STATE, status="NEED_SETUP"))
            schedule_idle(version)
            return

        applied = [label_of(t) for t in profiles]
        set_state(**dict(IDLE_STATE, status="CAPTURING", message="촬영 중",
                         applied=applied))
        image = capture_image()

        link.send("DONE")                     # 촬영 완료 → 나노가 조명을 끈다
        set_state(status="ANALYZING", message="분석 중")

        info = extract(analyze(image, profiles))
        if info["level"] == "DANGER":
            link.send("DANGER")               # 위험 → 나노 부저

        version = set_state(status="RESULT", message="", applied=applied, **{
            k: info[k] for k in
            ("level", "title", "subtitle", "summary", "direct", "facility",
             "ocr_provider", "cache_label", "paid", "ocr_text", "request_id")
        })
        log(f"판정 {info['level']} / 직접 {len(info['direct'])} "
            f"제조시설 {len(info['facility'])} / 기준 {','.join(applied)}")
        schedule_idle(version)

    except Exception as exc:
        log("스캔 오류:", exc)
        version = set_state(**dict(IDLE_STATE, status="ERROR", message=str(exc)))
        schedule_idle(version)
    finally:
        scan_lock.release()


def serial_loop(link):
    """나노가 보내는 SHOOT 을 계속 기다린다."""
    while True:
        try:
            line = link.readline()
        except Exception as exc:
            log("시리얼 읽기 오류:", exc)
            time.sleep(1)
            continue
        if line == "SHOOT":
            log("SHOOT 수신")
            trigger_scan(link)
        elif line:
            log("기타 수신:", line)
        if not link.ser:
            time.sleep(0.3)                   # 목 모드에서 CPU를 낭비하지 않도록


# ═════════════════════════════════════════════════════════════════════════
# 13. 화면용 로컬 웹서버
# ═════════════════════════════════════════════════════════════════════════
LINK = None      # main()에서 만들어 넣는다


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                                   # 접속 로그는 조용히

    # ── 응답 보조 ──
    def _reply(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._reply(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ── GET ──
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(SCRIPT_DIR, "index.html"), "rb") as f:
                    self._reply(200, f.read(), "text/html")
            except FileNotFoundError:
                self._reply(404, "index.html 이 같은 폴더에 없습니다.", "text/plain")

        elif path == "/state":
            with state_lock:
                snap = dict(state)
            snap["session"] = session_snapshot()   # 화면이 세션 만료를 바로 반영하도록
            self._json(200, snap)

        elif path == "/config":
            # 화면이 설정 UI를 그리는 데 필요한 모든 데이터
            with device_lock:
                dev = dict(device)
            self._json(200, {
                "device": dev,
                "session": session_snapshot(),
                "session_timeout_sec": SESSION_TIMEOUT_SEC,
                "categories": ALLERGY_CATEGORIES,
                "choices": [{"token": t, "label": l, "category": c, "search": s}
                            for t, l, c, s in ALLERGY_CHOICES],
                "bundles": [{"label": l, "tokens": t} for l, t in ALLERGY_BUNDLES],
                "base_profiles": [{"token": t, "label": l, "desc": d}
                                  for t, l, d in BASE_PROFILES],
                "hardware": {"serial": bool(LINK and LINK.ser), "camera": HAS_CAMERA},
                "backend": BACKEND_URL,
            })

        elif path == "/health":
            try:
                status, raw = _send(BACKEND_URL + "/health", timeout=5)
                self._json(200, {"ok": 200 <= status < 300, "detail": _decode(raw)})
            except Exception as exc:
                self._json(200, {"ok": False, "detail": str(exc)})

        else:
            self._json(404, {"error": "not found"})

    # ── POST ──
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/shoot":                    # 물리 버튼 없이 스캔 시작(화면 버튼/테스트용)
            threading.Thread(target=trigger_scan, args=(LINK,), daemon=True).start()
            self._json(200, {"ok": True})

        elif path == "/dismiss":                # 결과 화면을 바로 닫기
            touch_session()
            with state_lock:
                version = state["version"]
            revert_idle(version)
            self._json(200, {"ok": True})

        elif path == "/keepalive":              # 화면을 만지고 있는 동안 만료를 미룬다
            touch_session()
            self._json(200, {"ok": True, "session": session_snapshot()})

        elif path == "/session/end":            # ★ 사용 종료 — 개인 기준을 즉시 지운다
            end_session("버튼")
            self._json(200, {"ok": True, "session": session_snapshot()})

        elif path == "/settings":               # 이번 세션의 기준 설정
            payload = self._read_json()

            # 기기 설정(사람과 무관) → 파일에 저장
            with device_lock:
                if payload.get("ocr_mode") in ("mock", "live"):
                    device["ocr_mode"] = payload["ocr_mode"]
                if isinstance(payload.get("mock_text"), str):
                    device["mock_text"] = payload["mock_text"]
            save_device()

            # 사용자 기준(개인정보) → 메모리 세션에만
            profiles = payload.get("profiles")
            if isinstance(profiles, list):
                profiles = [str(x) for x in profiles]
                name = str(payload.get("display_name", "")).strip()
                if profiles:
                    start_session(profiles, display_name=name)
                    threading.Thread(target=push_preferences, daemon=True).start()
                    log(f"세션 기준 설정: {','.join(profiles)}")
                else:
                    end_session("기준 비움")

            self._json(200, {"ok": True, "session": session_snapshot(),
                             "device": dict(device)})

        elif path == "/login":                  # 앱 계정의 기준을 이번 세션에 빌려오기
            payload = self._read_json()
            try:
                result = backend_login(str(payload.get("email", "")),
                                       str(payload.get("password", "")))
                user = result.get("user", {}) or {}
                start_session(user.get("profiles") or [],
                              display_name=user.get("display_name") or "",
                              token=result.get("token"),
                              email=user.get("email"))
                log("로그인 → 계정 기준을 이번 세션에 적용")
                self._json(200, {"ok": True, "session": session_snapshot()})
            except Exception as exc:
                self._json(200, {"ok": False, "error": str(exc)})

        elif path == "/logout":                 # 로그아웃 = 세션 종료와 동일하게 취급
            end_session("로그아웃")
            self._json(200, {"ok": True, "session": session_snapshot()})

        else:
            self._json(404, {"error": "not found"})


# ═════════════════════════════════════════════════════════════════════════
# 14. 시작
# ═════════════════════════════════════════════════════════════════════════
def main():
    global LINK
    load_device()
    LINK = SerialLink()

    log(f"하드웨어  시리얼={'실물' if LINK.ser else '목'} / "
        f"카메라={'실물' if HAS_CAMERA else '목'}")
    log(f"백엔드    {BACKEND_URL}  (OCR={device['ocr_mode']})")
    log(f"공용정책  세션 없음으로 시작(기준 미설정 시 판정하지 않음) / "
        f"자동 만료 {SESSION_TIMEOUT_SEC // 60}분")

    threading.Thread(target=serial_loop, args=(LINK,), daemon=True).start()
    threading.Thread(target=session_watchdog, daemon=True).start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", UI_PORT), Handler)
    log(f"화면 서버 시작 → http://localhost:{UI_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("종료합니다.")
        server.shutdown()


if __name__ == "__main__":
    main()
