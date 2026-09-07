# -*- coding: utf-8 -*-
"""
Safe-Byte 목(mock) 백엔드 — 테스트 전용
===========================================================================
진짜 백엔드(seojaeohcode/safe-byte, FastAPI+CLOVA+Redis)를 띄우지 않고도
safebyte_pi.py 의 화면 흐름을 끝까지(촬영→분석→결과) 시험하기 위한 최소 서버다.

safebyte_pi.py 가 부르는 엔드포인트만 흉내낸다:
  POST /api/v1/analyze/image  → mock_text 를 규칙으로 판정해 결과 JSON 반환
  GET  /health                → 살아있음 표시

판정 규칙(아주 단순):
  - 사용자가 고른 기준(profiles)에 해당하는 성분 키워드가 mock_text 에 있으면 검출.
  - "제조시설" 이 적힌 문장에 있으면 교차오염(cross_contact), 아니면 직접함유(direct).
  - 직접함유가 하나라도 있으면 DANGER, 교차오염만 있으면 CAUTION, 없으면 SAFE.

Python 3.13에서 표준 cgi 모듈이 제거됐으므로 multipart 파싱을 직접 한다(표준 라이브러리만).

실행:  python3 mock_backend.py   (기본 0.0.0.0:8000)
"""
import re
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 프로필 토큰 → (표시이름, 라벨에서 찾을 키워드들)
ALLERGENS = {
    "allergy:milk":   ("우유",   ["우유", "유청", "유당", "분유", "치즈", "버터"]),
    "allergy:egg":    ("계란",   ["계란", "난백", "난황", "알류"]),
    "allergy:soy":    ("대두",   ["대두", "콩", "두유", "레시틴"]),
    "allergy:wheat":  ("밀",     ["밀", "밀가루", "글루텐", "소맥"]),
    "allergy:peanut": ("땅콩",   ["땅콩", "피넛"]),
    "allergy:walnut": ("호두",   ["호두"]),
    "allergy:shrimp": ("새우",   ["새우"]),
    "allergy:crab":   ("게",     ["게", "꽃게"]),
    "allergy:pork":   ("돼지고기", ["돼지", "돈육", "라드"]),
    "allergy:sesame": ("참깨",   ["참깨", "깨"]),
}

REASON = "국내 식품 알레르기 유발물질 표시 대상 성분입니다."


def parse_multipart(body, boundary):
    """cgi 없이 multipart/form-data 를 직접 파싱. 파일 파트는 건너뛰고
    일반 필드(name→value)만 문자열로 돌려준다."""
    fields = {}
    delim = b"--" + boundary.encode()
    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue
        header, _, value = part.partition(b"\r\n\r\n")
        header_str = header.decode("utf-8", "ignore")
        if "filename=" in header_str:          # 파일(이미지) 파트는 무시
            continue
        m = re.search(r'name="([^"]+)"', header_str)
        if not m:
            continue
        fields[m.group(1)] = value.rstrip(b"\r\n").decode("utf-8", "ignore")
    return fields


def find_facility_span(text):
    """'제조시설'이 언급된 문장(줄)만 모아 돌려준다 → 교차오염 판정용."""
    lines = [ln for ln in text.replace("\r", "").split("\n")
             if "제조시설" in ln or "같은 시설" in ln]
    return " ".join(lines)


def analyze(text, profiles):
    profiles = set(profiles or [])
    all_mode = "allergy" in profiles  # '알레르기 전체' 기준이면 전부 검사
    facility_text = find_facility_span(text)

    direct, cross = [], []
    for token, (name, keywords) in ALLERGENS.items():
        if not all_mode and token not in profiles:
            continue
        hit = next((k for k in keywords if k in text), None)
        if not hit:
            continue
        in_facility = hit in facility_text
        row = {
            "canonical_name": name,
            "matched_keyword": hit,
            "occurrence_type": "cross_contact" if in_facility else "direct",
            "occurrence_label": "제조시설 공유" if in_facility else "직접 함유",
            "reason": REASON,
            "evidence": (facility_text if in_facility else text.split("\n")[0]),
            "severity": "caution" if in_facility else "danger",
        }
        (cross if in_facility else direct).append(row)

    if direct:
        level = "DANGER"
        summary = "직접 함유 위험 성분: " + ", ".join(d["canonical_name"] for d in direct)
    elif cross:
        level = "CAUTION"
        summary = "제조시설 공유 표기: " + ", ".join(c["canonical_name"] for c in cross)
    else:
        level = "SAFE"
        summary = "선택한 기준에서 위험 성분이 발견되지 않았습니다."

    return {
        "request_id": uuid.uuid4().hex[:12],
        "risk": {"final_level": level, "summary": summary, "detections": direct + cross},
        "ocr": {
            "provider": "mock",
            "cached": False,
            "cache_backend": "none",
            "called_paid_api": False,
            "corrected_text": text,
        },
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "mock": True})
        else:
            self._json(404, {"detail": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/v1/analyze/image"):
            self._json(404, {"detail": "not found"})
            return
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        fields = {}
        m = re.search(r"boundary=([^;]+)", ctype)
        if "multipart/form-data" in ctype and m:
            fields = parse_multipart(raw, m.group(1).strip().strip('"'))
        profiles = [p for p in fields.get("profiles", "").split(",") if p]
        text = fields.get("mock_text", "") or ""
        result = analyze(text, profiles)
        print(f"[mock] profiles={profiles} → {result['risk']['final_level']} "
              f"(검출 {len(result['risk']['detections'])}건)", flush=True)
        self._json(200, result)


if __name__ == "__main__":
    print("[mock] Safe-Byte 목 백엔드 시작 → http://0.0.0.0:8000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
