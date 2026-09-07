# -*- coding: utf-8 -*-
"""
Safe-Byte 라즈베리파이 5 프론트엔드 드라이버 (기기 페어링 방식)
===========================================================================
백엔드(seojaeohcode/safe-byte)가 회원제로 바뀌면서, 카트 기기는 '기기 페어링'으로 붙는다.
기기에는 사용자 비밀번호를 저장하지 않는다(공용 기기 보안).

[ 페어링 흐름 ]  (자세한 계약: 저장소 INTEGRATION.md 섹션 7)
  1) 앱이 장바구니(cart)를 만들고 '5분·1회용 페어링 코드'를 발급한다.
     (앱이 cart 를 만들 때 알레르기·할랄 기준(profiles)을 함께 정한다)
  2) 기기: POST /api/v1/devices/redeem  { code }        → 4시간짜리 기기 토큰
  3) 기기: GET  /api/v1/devices/session (기기 토큰)      → 연결된 cart 의 profiles
  4) 기기: POST /api/v1/devices/analyze (기기 토큰, 이미지) → 판정 결과(+장바구니 자동 담기)

  ★ profiles(무엇을 가릴지)는 앱이 cart 에 정한 값을 그대로 쓴다. 기기는 고르지 않고
    'session' 으로 받아 화면에 '적용된 기준'으로 보여주기만 한다.

[ 화면 ]
  이 프로그램이 로컬 웹서버(http://localhost:8080)를 띄우고, 7인치 LCD의 크로미움이 접속해
  index.html 을 띄운다. 화면은 /state 를 주기적으로 물어보며 현재 상태를 그린다.
  페어링 전에는 '페어링 코드 입력' 화면, 페어링 후에는 스캔 대기 화면을 보여준다.

[ 스캔 ]
  버튼(GPIO 직결)을 누르면 카메라로 라벨을 촬영해 백엔드로 보내고, 결과(SAFE/CAUTION/DANGER)를
  화면에 표시한다. 위험 알림은 화면 표시로만 한다(부저 없음).

[ 판정 결과에서 지켜야 할 것 — 알레르기 앱이라 중요 ]
  · occurrence_type == "cross_contact" 는 '같은 제조시설' 문구에서 잡힌 것 → 직접 함유와 구분 표시
  · quality.needs_review == true 면 라벨을 충분히 못 읽은 것 → 'SAFE(안전)'로 표시하지 않는다

[ 의존성 ]  파이썬 표준 라이브러리만으로 동작. 아래는 '있으면 실물, 없으면 목'.
  · gpiozero  : GPIO 버튼 입력   (없으면 화면 버튼/POST 로만 스캔)
  · picamera2 : 카메라 촬영       (없으면 test_label.jpg 또는 흰 이미지)

[ 실행 ]  python3 safebyte_pi.py  →  브라우저에서 http://localhost:8080
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
# 1. 설정
# ═════════════════════════════════════════════════════════════════════════
BACKEND_URL     = "http://101.79.20.37"     # 선배 백엔드(Ncloud, nginx 80). 로컬 테스트 시 http://127.0.0.1:8000
BUTTON_PIN      = 17                        # 스캔 버튼이 연결된 GPIO 핀(BCM). 버튼 반대쪽은 GND에.
UI_PORT         = 8080                      # 화면(브라우저)이 접속할 로컬 포트
RESULT_HOLD_SEC = 0                         # 결과를 몇 초 뒤 대기로 되돌릴지(0=수동, 버튼 누를 때까지 유지)
REQUEST_TIMEOUT = 40                        # 백엔드 응답 대기 최대(초)

# ★ 공용 기기 정책: 마지막 조작 후 이 시간 동안 무동작이면 페어링(기기 토큰)을 지운다.
#   서버 기기 토큰 수명은 4시간이지만, 화면 보안상 더 짧게 스스로 끊는다.
IDLE_TIMEOUT_SEC = 15 * 60
IDLE_CHECK_SEC   = 20

# profiles 토큰 → 사람이 읽는 이름 (session 의 profiles 를 화면에 한글로 표시하기 위해서만 필요)
LABELS = {
    "halal": "할랄", "vegan": "비건", "vegetarian": "베지테리언",
    "allergy": "알레르기 전체", "all": "전체 검사",
    "allergy:milk": "우유", "allergy:egg": "계란", "allergy:soy": "대두",
    "allergy:wheat": "밀", "allergy:buckwheat": "메밀", "allergy:peanut": "땅콩",
    "allergy:walnut": "호두", "allergy:pine_nut": "잣", "allergy:tree_nut": "견과류",
    "allergy:sesame": "참깨", "allergy:shrimp": "새우", "allergy:crab": "게",
    "allergy:shellfish": "조개류", "allergy:squid": "오징어", "allergy:mackerel": "고등어",
    "allergy:fish": "생선류", "allergy:pork": "돼지고기", "allergy:beef": "쇠고기",
    "allergy:chicken": "닭고기", "allergy:peach": "복숭아", "allergy:tomato": "토마토",
    "allergy:sulfite": "아황산류",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEV_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "dev_account.json")   # 있으면 "현장 테스트 연결" 버튼이 열린다


def label_of(token):
    return LABELS.get(token, token.replace("allergy:", ""))


def log(*args):
    print("[safebyte]", *args, flush=True)


# ═════════════════════════════════════════════════════════════════════════
# 2. 선택적 하드웨어 (없으면 목 모드)
# ═════════════════════════════════════════════════════════════════════════
try:
    from gpiozero import Button
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

try:
    from picamera2 import Picamera2
    HAS_CAMERA = True
except ImportError:
    HAS_CAMERA = False


# ═════════════════════════════════════════════════════════════════════════
# 3. HTTP 유틸 — requests 없이 표준 urllib 로 백엔드 호출
# ═════════════════════════════════════════════════════════════════════════
def _send(url, method="GET", body=None, headers=None, timeout=REQUEST_TIMEOUT):
    """요청을 보내고 (상태코드, 응답바이트)를 돌려준다. 4xx/5xx도 예외 없이 반환."""
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"백엔드에 연결하지 못했습니다: {exc.reason}") from exc


def _json_body(obj):
    """JSON 본문을 만든다. 한글은 UTF-8 로 인코딩(윈도우 curl 인코딩 문제와 무관)."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _encode_multipart(fields, files):
    """multipart/form-data 본문을 직접 만든다. (파일+일반값을 한 요청에 담는 인코딩)"""
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
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"message": raw.decode("utf-8", errors="ignore")}


def _error_message(payload, status):
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, list) and detail:
        return str(detail[0].get("msg", detail[0]))
    if detail:
        return str(detail)
    return f"서버 오류 ({status})"


# ═════════════════════════════════════════════════════════════════════════
# 4. ★ 페어링 세션 — 기기 토큰과 cart 기준은 여기에만, 디스크로 나가지 않는다
# ═════════════════════════════════════════════════════════════════════════
EMPTY_PAIRING = {
    "token": None,          # 기기 토큰(4시간). 디스크 저장 안 함.
    "cart_id": None,
    "cart_name": "",
    "profiles": [],         # cart 가 정한 기준(앱에서 설정). 기기는 표시만.
    "last_active": 0.0,
}
pairing = dict(EMPTY_PAIRING)
pairing_lock = threading.Lock()


def touch_pairing():
    with pairing_lock:
        if pairing["token"]:
            pairing["last_active"] = time.time()


def clear_pairing(reason="사용 종료"):
    with pairing_lock:
        had = bool(pairing["token"])
        pairing.update(EMPTY_PAIRING)
    if had:
        log(f"페어링 종료({reason}) → 기기 토큰·기준 삭제됨")
        reset_state()
    return had


def pairing_snapshot():
    """화면에 내려보낼 페어링 정보(토큰은 절대 포함하지 않는다)."""
    with pairing_lock:
        active = bool(pairing["token"])
        remain = 0
        if active:
            remain = max(0, int(IDLE_TIMEOUT_SEC - (time.time() - pairing["last_active"])))
        return {
            "active": active,
            "cart_name": pairing["cart_name"],
            "profiles": list(pairing["profiles"]),
            "remain_sec": remain,
        }


def device_redeem(code):
    """페어링 코드 → 기기 토큰. 이어서 session 을 조회해 cart 기준을 채운다."""
    status, raw = _send(BACKEND_URL + "/api/v1/devices/redeem", "POST",
                        _json_body({"code": code}), {"Content-Type": "application/json"})
    payload = _decode(raw)
    if status < 200 or status >= 300:
        raise RuntimeError(_error_message(payload, status))
    with pairing_lock:
        pairing.update(token=payload.get("token"),
                       cart_id=payload.get("cart_id"),
                       last_active=time.time())
    log("페어링 성공 → 기기 토큰 발급")
    device_session()                       # cart 기준(profiles) 채우기
    return True


def dev_self_pair(profiles=None):
    """[현장 테스트용] 앱 없이 이 기기가 스스로 카트를 만들고 페어링한다.

    기준은 보통 dev_account.json 의 profiles 를 쓴다. 다른 기준으로 한 번만
    시험해 보고 싶으면 profiles 를 실어 보내면 된다(파일 수정·재시작 불필요):
        curl -X POST localhost:8080/pair/dev -d '{"profiles":["allergy:peanut"]}'

    정식 흐름은 '앱이 카트를 만들고 연결 코드를 발급 → 기기에 코드 입력' 이다.
    앱이 아직 없어 혼자 현장 테스트를 할 때만 쓰라고, 같은 폴더에
    dev_account.json (계정·기준) 이 있을 때만 동작한다. 파일이 없으면 화면에
    버튼 자체가 나오지 않는다.
    """
    try:
        with open(DEV_ACCOUNT_FILE, encoding="utf-8") as f:
            conf = json.load(f)
    except FileNotFoundError:
        raise RuntimeError("dev_account.json 이 없어 현장 테스트 연결이 꺼져 있습니다.")

    status, raw = _send(BACKEND_URL + "/api/v1/auth/login", "POST",
                        _json_body({"email": conf["email"], "password": conf["password"]}),
                        {"Content-Type": "application/json"})
    payload = _decode(raw)
    if status < 200 or status >= 300:
        raise RuntimeError("로그인 실패: " + _error_message(payload, status))
    user_token = payload.get("token")
    auth = {"Content-Type": "application/json", "Authorization": "Bearer " + user_token}

    status, raw = _send(BACKEND_URL + "/api/v1/carts", "POST",
                        _json_body({"name": conf.get("cart_name", "현장 테스트 카트"),
                                    "profiles": list(profiles or []) or conf.get("profiles", []),
                                    "language": "ko"}), auth)
    payload = _decode(raw)
    if status < 200 or status >= 300:
        raise RuntimeError("카트 생성 실패: " + _error_message(payload, status))
    cart_id = payload.get("cart_id") or payload.get("id")

    status, raw = _send(BACKEND_URL + "/api/v1/carts/%s/pair" % cart_id, "POST", None,
                        {"Authorization": "Bearer " + user_token})
    payload = _decode(raw)
    if status < 200 or status >= 300:
        raise RuntimeError("연결 코드 발급 실패: " + _error_message(payload, status))

    log("현장 테스트 연결: 카트 생성 → 코드 발급 → 스스로 페어링")
    return device_redeem(payload.get("code"))


def device_session():
    """연결된 cart 정보(profiles)를 받아 화면 표시용으로 저장. 토큰 만료면 폐기."""
    with pairing_lock:
        token = pairing["token"]
    if not token:
        return None
    status, raw = _send(BACKEND_URL + "/api/v1/devices/session", "GET", None,
                        {"Authorization": "Bearer " + token})
    payload = _decode(raw)
    if status in (400, 401, 403):
        clear_pairing("세션 만료")
        raise RuntimeError("페어링이 만료되었습니다. 다시 연결해 주세요.")
    if status < 200 or status >= 300:
        raise RuntimeError(_error_message(payload, status))
    with pairing_lock:
        pairing.update(cart_name=payload.get("name", ""),
                       profiles=list(payload.get("profiles", [])))
    return payload


def device_analyze(jpeg):
    """촬영 이미지를 devices/analyze 로 보내 판정 결과를 받는다.
    ★ mock 옵션이 없어 항상 실제 OCR(유료·하루 50회 서버 공용)을 호출한다."""
    with pairing_lock:
        token = pairing["token"]
    if not token:
        raise RuntimeError("먼저 앱에서 카트를 연결(페어링)해 주세요.")
    files = {"file": ("label.jpg", jpeg, "image/jpeg")}
    body, content_type = _encode_multipart({}, files)
    headers = {"Content-Type": content_type, "Authorization": "Bearer " + token}
    status, raw = _send(BACKEND_URL + "/api/v1/devices/analyze", "POST", body, headers)
    payload = _decode(raw)
    if status in (400, 401, 403):
        clear_pairing("분석 중 세션 만료")
    if status < 200 or status >= 300:
        raise RuntimeError(_error_message(payload, status))
    return payload


def pairing_watchdog():
    """무동작이 오래되면 기기 토큰을 자동으로 지우는 감시 스레드."""
    while True:
        time.sleep(IDLE_CHECK_SEC)
        with pairing_lock:
            expired = (pairing["token"] and
                       time.time() - pairing["last_active"] > IDLE_TIMEOUT_SEC)
        if expired:
            clear_pairing("자동 만료")


# ═════════════════════════════════════════════════════════════════════════
# 5. 공유 상태 — 화면이 /state 로 읽어가는 현재 상황
# ═════════════════════════════════════════════════════════════════════════
IDLE_STATE = {
    # IDLE(대기) / NEED_PAIR(페어링 필요) / CAPTURING / ANALYZING / RESULT / ERROR
    "status": "IDLE",
    "level": None,          # SAFE / CAUTION / DANGER
    "needs_review": False,  # 라벨을 충분히 못 읽음 → 'SAFE(안전)'로 표시 금지
    "title": "",
    "subtitle": "",
    "summary": "",
    "direct": [],           # 직접 함유
    "facility": [],         # 제조시설 공유(교차오염)
    "ocr_provider": "-",
    "cache_label": "-",
    "paid": False,
    "ocr_text": "",
    "request_id": "",
    "message": "",
    "applied": [],          # 적용된 기준(사람이 읽는 이름)
}
state = dict(IDLE_STATE, version=0, updated=time.time())
state_lock = threading.Lock()


def set_state(**kw):
    with state_lock:
        state.update(kw)
        state["version"] += 1
        state["updated"] = time.time()
        return state["version"]


def reset_state():
    set_state(**IDLE_STATE)


def revert_idle(expected_version):
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
# 6. 카메라 — 계속 켜두고 '미리보기 스트림'을 제공한다
#    (버튼 첫 누름에 미리보기로 조준 → 두 번째 누름에 그 화면을 촬영해 분석)
# ═════════════════════════════════════════════════════════════════════════
CAMERA = None
_cam_lock = threading.Lock()


def start_camera():
    """카메라를 미리보기 모드로 열어 둔다. 촬영도 이 스트림의 현재 프레임으로 한다."""
    global CAMERA
    if not HAS_CAMERA:
        log("카메라 없음(목 모드) → 미리보기는 정지 이미지로 대체")
        return
    try:
        CAMERA = Picamera2()
        # 미리보기 해상도를 조금 높여(1640x1232) 라벨 글자 판독에 유리하게.
        CAMERA.configure(CAMERA.create_video_configuration(
            main={"size": (1640, 1232), "format": "RGB888"}))
        CAMERA.start()
        # ★ imx708(카메라 모듈 3)은 오토포커스 지원 → 연속 초점(근거리 라벨에 맞춤).
        #   AfSpeed=Fast 로 라벨을 대면 빠르게 초점을 잡게 한다.
        try:
            from libcamera import controls
            CAMERA.set_controls({
                "AfMode": controls.AfModeEnum.Continuous,
                "AfSpeed": controls.AfSpeedEnum.Fast,
                "AfRange": controls.AfRangeEnum.Macro,   # 근거리(소스통 작은 글자) 우선
            })
            log("오토포커스 켜짐 (연속·매크로 근접 초점)")
        except Exception as exc:
            log("오토포커스 설정 실패(고정 초점으로 진행):", exc)
        time.sleep(0.8)
        log("카메라 미리보기 시작 (1640x1232)")
    except Exception as exc:
        log("카메라 시작 실패:", exc)
        CAMERA = None


def _mock_frame():
    test_path = os.path.join(SCRIPT_DIR, "test_label.jpg")
    if os.path.exists(test_path):
        with open(test_path, "rb") as f:
            return f.read()
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (900, 1200), (250, 250, 250)).save(buf, format="JPEG")
        return buf.getvalue()
    except Exception:
        return bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300"
            + "ff" * 64 +
            "ffc2000b080001000101011100ffc40014000100000000000000000000000000000009"
            "ffda0008010100000001d2cf20ffd9")


def frame_jpeg(quality=80):
    """카메라의 현재 프레임을 JPEG 바이트로. 카메라가 없으면 목 이미지."""
    if CAMERA is None:
        return _mock_frame()
    try:
        from PIL import Image
        with _cam_lock:
            arr = CAMERA.capture_array()
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception as exc:
        log("프레임 캡처 실패:", exc)
        return _mock_frame()


def capture_image():
    """분석용 촬영 — 고해상도 스틸로 캡처해 작은 글자까지 판독되게 한다.
    촬영 직전 오토포커스를 한 번 더 확실히 맞춘 뒤, 미리보기보다 높은 해상도로 찍는다."""
    if CAMERA is None:
        return _mock_frame()
    try:
        from PIL import Image
        # 촬영 직전 초점 재확보(매크로 근접). 실패해도 연속 초점값으로 진행.
        try:
            from libcamera import controls
            CAMERA.set_controls({"AfTrigger": controls.AfTriggerEnum.Start})
            time.sleep(0.7)
        except Exception:
            pass
        still_config = CAMERA.create_still_configuration()
        with _cam_lock:
            arr = CAMERA.switch_mode_and_capture_array(still_config, "main")
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as exc:
        log("고화질 촬영 실패 → 미리보기 프레임으로 대체:", exc)
        return frame_jpeg(quality=90)


# ═════════════════════════════════════════════════════════════════════════
# 7. 판정 결과 해석 — 새 응답에서 화면에 필요한 값만 뽑는다
# ═════════════════════════════════════════════════════════════════════════
def verdict_title(level, needs_review):
    if needs_review:
        return "라벨을 다시 확인하세요"
    return {"DANGER": "먹기 전 멈춰요",
            "CAUTION": "한 번 더 확인하세요"}.get(level, "현재 기준 통과")


def verdict_subtitle(level, direct_count, facility_count, needs_review):
    if needs_review:
        return "라벨을 충분히 읽지 못했습니다. 다시 촬영해 주세요."
    if level == "DANGER":
        return f"직접 함유 위험 성분 {direct_count}개가 감지됐습니다."
    if level == "CAUTION":
        if facility_count and not direct_count:
            return f"제조시설 공유 표기 {facility_count}건이 확인됐습니다."
        return "제조시설 공유 또는 확인이 필요한 성분이 있습니다."
    return "선택한 기준에서 주요 위험 성분이 감지되지 않았습니다."


def extract(result):
    """백엔드 응답 → 화면 값.
    detections 는 평평한 리스트이고, occurrence_type=="cross_contact" 이면 제조시설 공유로 분리.
    quality.needs_review 가 true 면 안전으로 표시하지 않는다."""
    risk = result.get("risk", {}) or {}
    quality = result.get("quality", {}) or {}
    ocr = result.get("ocr", {}) or {}
    level = risk.get("final_level", "SAFE")
    needs_review = bool(quality.get("needs_review"))

    direct, facility = [], []
    for item in risk.get("detections", []) or []:
        if not isinstance(item, dict):
            continue
        cross = item.get("occurrence_type") == "cross_contact"
        row = {
            "name": item.get("canonical_name", ""),
            "category": item.get("category", ""),
            "severity": item.get("severity", ""),
            "keyword": item.get("matched_keyword", ""),
            "reason": item.get("reason", ""),
            "evidence": item.get("evidence", ""),
            "label": "제조시설 공유" if cross else "직접 함유",
        }
        (facility if cross else direct).append(row)

    return {
        "level": level,
        "needs_review": needs_review,
        "title": verdict_title(level, needs_review),
        "subtitle": verdict_subtitle(level, len(direct), len(facility), needs_review),
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
# 8. 한 번의 스캔
# ═════════════════════════════════════════════════════════════════════════
scan_lock = threading.Lock()


def trigger_scan():
    if not scan_lock.acquire(blocking=False):
        log("이미 스캔 중 → 무시")
        return
    try:
        touch_pairing()
        with pairing_lock:
            token = pairing["token"]
            profiles = list(pairing["profiles"])

        # 페어링이 안 됐으면 판정하지 않고 연결을 먼저 요청한다.
        if not token:
            log("페어링 없음 → 스캔하지 않고 연결 요청")
            version = set_state(**dict(IDLE_STATE, status="NEED_PAIR"))
            schedule_idle(version)
            return

        applied = [label_of(t) for t in profiles]
        with state_lock:
            cur = state["status"]

        # ★ 2단계 스캔: 첫 누름은 '미리보기(조준)', 두 번째 누름에 실제 촬영·분석.
        #   무엇을 찍는지 확인한 뒤 찍게 해서 헛스캔(유료 OCR 낭비)을 줄인다.
        if cur != "PREVIEW":
            set_state(**dict(IDLE_STATE, status="PREVIEW", applied=applied))
            return

        set_state(**dict(IDLE_STATE, status="CAPTURING", message="촬영 중", applied=applied))
        image = capture_image()

        set_state(status="ANALYZING", message="분석 중")
        info = extract(device_analyze(image))

        version = set_state(status="RESULT", message="", applied=applied, **{
            k: info[k] for k in
            ("level", "needs_review", "title", "subtitle", "summary", "direct",
             "facility", "ocr_provider", "cache_label", "paid", "ocr_text", "request_id")
        })
        log(f"판정 {info['level']} / needs_review={info['needs_review']} / "
            f"직접 {len(info['direct'])} 제조시설 {len(info['facility'])}")
        schedule_idle(version)

    except Exception as exc:
        log("스캔 오류:", exc)
        version = set_state(**dict(IDLE_STATE, status="ERROR", message=str(exc)))
        schedule_idle(version)
    finally:
        scan_lock.release()


# ═════════════════════════════════════════════════════════════════════════
# 9. GPIO 버튼
# ═════════════════════════════════════════════════════════════════════════
def setup_button():
    """스캔 버튼을 파이 GPIO 핀에 직접 연결해 감시한다.
    pull_up=True: 버튼 반대쪽을 GND에 연결(저항 불필요). 눌리는 순간 GPIO가 GND로 떨어짐.
    bounce_time: 기계식 버튼의 접점 떨림(채터링)을 무시하는 시간."""
    if not HAS_GPIO:
        log("gpiozero 없음 → 물리 버튼 없이 진행(화면 버튼/POST로만 스캔)")
        return None
    try:
        btn = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
        btn.when_pressed = lambda: threading.Thread(target=trigger_scan, daemon=True).start()
        log(f"스캔 버튼 GPIO{BUTTON_PIN} 감시 시작")
        return btn
    except Exception as exc:
        log(f"버튼 초기화 실패({exc}) → 물리 버튼 없이 진행")
        return None


# ═════════════════════════════════════════════════════════════════════════
# 10. 화면용 로컬 웹서버
# ═════════════════════════════════════════════════════════════════════════
BUTTON = None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

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
            snap["pairing"] = pairing_snapshot()
            self._json(200, snap)

        elif path == "/config":
            self._json(200, {
                "pairing": pairing_snapshot(),
                "idle_timeout_sec": IDLE_TIMEOUT_SEC,
                "labels": LABELS,
                "hardware": {"button": bool(BUTTON), "camera": HAS_CAMERA},
                "backend": BACKEND_URL,
                "dev_pair": os.path.exists(DEV_ACCOUNT_FILE),
            })

        elif path == "/health":
            try:
                status, raw = _send(BACKEND_URL + "/health", timeout=5)
                self._json(200, {"ok": 200 <= status < 300, "detail": _decode(raw)})
            except Exception as exc:
                self._json(200, {"ok": False, "detail": str(exc)})

        elif path == "/preview":
            # 카메라 미리보기 MJPEG 스트림. 화면에서 <img src="/preview"> 로 표시한다.
            try:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    frame = frame_jpeg(quality=70)
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(("Content-Length: %d\r\n\r\n" % len(frame)).encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.12)                # 약 8fps (미리보기용으로 충분)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                               # 브라우저가 스트림을 끊으면 조용히 종료

        else:
            self._json(404, {"error": "not found"})

    # ── POST ──
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/shoot":                    # 물리 버튼 없이 스캔 시작(화면 버튼/테스트용)
            threading.Thread(target=trigger_scan, daemon=True).start()
            self._json(200, {"ok": True})

        elif path == "/pair":                   # 페어링 코드 입력 → 기기 토큰 발급
            payload = self._read_json()
            code = str(payload.get("code", "")).strip()
            if not code:
                self._json(200, {"ok": False, "error": "페어링 코드를 입력해 주세요."})
                return
            try:
                device_redeem(code)
                reset_state()
                self._json(200, {"ok": True, "pairing": pairing_snapshot()})
            except Exception as exc:
                self._json(200, {"ok": False, "error": str(exc)})

        elif path == "/pair/dev":               # [현장 테스트용] 앱 없이 스스로 페어링
            payload = self._read_json()
            try:
                dev_self_pair(payload.get("profiles"))
                reset_state()
                self._json(200, {"ok": True, "pairing": pairing_snapshot()})
            except Exception as exc:
                self._json(200, {"ok": False, "error": str(exc)})

        elif path == "/dismiss":                # 결과 화면 닫기
            touch_pairing()
            with state_lock:
                version = state["version"]
            revert_idle(version)
            self._json(200, {"ok": True})

        elif path == "/keepalive":              # 화면 조작 중 만료 연기
            touch_pairing()
            self._json(200, {"ok": True, "pairing": pairing_snapshot()})

        elif path == "/session/end":            # ★ 사용 종료 — 기기 토큰 즉시 폐기
            clear_pairing("버튼")
            self._json(200, {"ok": True, "pairing": pairing_snapshot()})

        else:
            self._json(404, {"error": "not found"})


# ═════════════════════════════════════════════════════════════════════════
# 11. 시작
# ═════════════════════════════════════════════════════════════════════════
def main():
    global BUTTON
    BUTTON = setup_button()
    start_camera()                             # 미리보기 스트림용으로 카메라를 열어 둔다

    log(f"하드웨어  버튼={'GPIO'+str(BUTTON_PIN) if BUTTON else '없음(화면/POST)'} / "
        f"카메라={'실물' if HAS_CAMERA else '목'}")
    log(f"백엔드    {BACKEND_URL}  (기기 페어링 방식)")
    log(f"공용정책  페어링 없이 시작(연결 전 스캔 안 함) / 자동 만료 {IDLE_TIMEOUT_SEC // 60}분")

    threading.Thread(target=pairing_watchdog, daemon=True).start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", UI_PORT), Handler)
    log(f"화면 서버 시작 → http://localhost:{UI_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("종료합니다.")
        server.shutdown()


if __name__ == "__main__":
    main()
