/*
 * Safe-Byte 아두이노 나노 펌웨어
 * -------------------------------------------------
 * 동작 흐름:
 *   [버튼 D7 눌림] -> 네오픽셀 흰색 ON(촬영 조명) + 시리얼로 "SHOOT" 전송
 *   [Pi가 촬영 끝냄] -> "DONE" 수신 -> 네오픽셀 OFF
 *   [Pi가 위험 판정] -> "DANGER" 수신 -> 부저 D8만 울림 (LED는 관여 안 함)
 *
 * 네오픽셀은 '버튼~촬영완료' 구간에만 흰색으로 켜지고, 나머지 시간엔 항상 꺼져 있음.
 * 결과 표시는 7인치 LCD가 담당하므로 LED로 색은 내지 않음.
 *
 * 필요 라이브러리: Adafruit NeoPixel (아두이노 IDE 라이브러리 관리에서 설치)
 */

#include <Adafruit_NeoPixel.h>   // 네오픽셀 제어 라이브러리

// ── 함수 원형(미리 선언) ──
void handleButton();
void handleSerial();
void handleLightTimeout();
void handleBuzzer();
void lightOn();
void lightOff();
void startBuzzer();

// ── 핀 배정 ──
const uint8_t PIN_BUTTON = 7;    // 값 7  : 방수 푸시버튼이 꽂힌 핀 번호
const uint8_t PIN_PIXELS = 6;    // 값 6  : 네오픽셀 링 DIN이 꽂힌 핀 번호
const uint8_t PIN_BUZZER = 8;    // 값 8  : 액티브 부저(+)가 꽂힌 핀 번호

// ── 네오픽셀 설정 ──
const uint8_t NUM_PIXELS = 12;   // 값 12 : 링에 달린 LED 알 개수(실제 부품 수로 수정)
Adafruit_NeoPixel pixels(NUM_PIXELS, PIN_PIXELS, NEO_GRB + NEO_KHZ800);
                                 // pixels : 네오픽셀 12알을 제어하는 객체(도구 묶음)

// ── 버튼 디바운스용 상태 변수 ──
const unsigned long DEBOUNCE_MS = 40;  // 값 40  : 채터링 무시할 시간(밀리초)
int lastReading = HIGH;                // 값 HIGH: 직전 loop에서 읽은 버튼 raw 신호
int buttonState = HIGH;                // 값 HIGH: 디바운스 거쳐 '확정된' 버튼 상태
unsigned long lastDebounceTime = 0;    // 값 0   : 버튼 신호가 마지막으로 바뀐 시각(ms)

// ── 촬영 조명(네오픽셀) 자동 소등용 ──
bool lightOnFlag = false;                // 값 false: 지금 조명이 켜져 있는지 여부
unsigned long lightStartTime = 0;        // 값 0   : 조명을 켠 시각(ms)
const unsigned long LIGHT_MAX_MS = 4000; // 값 4000: Pi가 DONE을 안 줘도 강제로 끌 최대 시간(ms)

// ── 부저 논블로킹 제어용 ──
bool buzzerOn = false;                 // 값 false: 지금 부저가 울리는 중인지 여부
unsigned long buzzerStartTime = 0;     // 값 0   : 부저를 켠 시각(ms)
const unsigned long BUZZER_MS = 600;   // 값 600 : 부저를 울릴 시간 길이(ms) = 0.6초

void setup() {
  Serial.begin(9600);                  // 값 9600: Pi와 주고받는 통신 속도(보드레이트)
  pinMode(PIN_BUTTON, INPUT_PULLUP);   // 7번 핀: 내부 풀업 입력(안 누르면 HIGH, 누르면 LOW)
  pinMode(PIN_BUZZER, OUTPUT);         // 8번 핀: 출력
  digitalWrite(PIN_BUZZER, LOW);       // 부저 처음엔 꺼둠

  pixels.begin();                      // 네오픽셀 초기화
  pixels.clear();                      // 색 버퍼를 전부 0(꺼짐)으로
  pixels.show();                       // 실제 LED에 반영 → 전부 소등
}

void loop() {
  handleButton();        // 버튼 눌림 감지 → 조명 켜고 SHOOT 전송
  handleSerial();        // Pi가 보낸 DONE / DANGER 처리
  handleLightTimeout();  // DONE이 안 와도 일정 시간 지나면 조명 자동 끔
  handleBuzzer();        // 부저 자동 끄기(논블로킹)
}

// ── 버튼: 디바운스 후 '눌린 순간'만 잡음 ──
void handleButton() {
  int reading = digitalRead(PIN_BUTTON);  // reading : 지금 이 순간 버튼 핀 값(HIGH 또는 LOW)

  if (reading != lastReading) {
    lastDebounceTime = millis();          // 신호가 흔들린 시각 갱신
  }

  if (millis() - lastDebounceTime > DEBOUNCE_MS) {  // 40ms 넘게 안정됐으면
    if (reading != buttonState) {
      buttonState = reading;              // 확정 상태 갱신
      if (buttonState == LOW) {           // HIGH→LOW = 방금 눌린 순간
        lightOn();                        // 촬영 조명 흰색 ON
        Serial.println("SHOOT");          // Pi에 촬영 신호 전송
      }
    }
  }
  lastReading = reading;                  // 다음 비교를 위해 저장
}

// ── 시리얼: Pi가 보낸 한 줄 명령 처리 ──
void handleSerial() {
  if (Serial.available() > 0) {                     // 받은 데이터가 있으면
    String cmd = Serial.readStringUntil('\n');      // cmd : 개행 전까지 받은 문자열
    cmd.trim();                                     // 앞뒤 공백·개행 제거

    if (cmd == "DONE") {          // 촬영 완료 신호
      lightOff();                 // 조명 끔
    } else if (cmd == "DANGER") { // 위험 성분 검출 신호
      startBuzzer();              // 부저 울림(LED는 켜지 않음)
    }
  }
}

// ── 조명 켜기: 흰색 최대 밝기 ──
void lightOn() {
  for (uint8_t i = 0; i < NUM_PIXELS; i++) {
    pixels.setPixelColor(i, pixels.Color(255, 255, 255));  // 255,255,255 = 흰색 최대
  }
  pixels.show();
  lightOnFlag = true;            // 조명 켜짐 표시
  lightStartTime = millis();     // 켠 시각 기록(자동 소등 계산용)
}

// ── 조명 끄기 ──
void lightOff() {
  pixels.clear();                // 색 버퍼 전부 0
  pixels.show();                 // 실제 소등
  lightOnFlag = false;           // 조명 꺼짐 표시
}

// ── 안전장치: Pi가 DONE을 안 줘도 최대 시간 지나면 강제 소등 ──
void handleLightTimeout() {
  if (lightOnFlag && millis() - lightStartTime > LIGHT_MAX_MS) {
    lightOff();
  }
}

// ── 부저 켜기(논블로킹 시작점) ──
void startBuzzer() {
  digitalWrite(PIN_BUZZER, HIGH);  // 부저 ON
  buzzerOn = true;                 // 울리는 중 표시
  buzzerStartTime = millis();      // 켠 시각 기록
}

// ── 부저: 정해진 시간 지나면 자동 끔 ──
void handleBuzzer() {
  if (buzzerOn && millis() - buzzerStartTime > BUZZER_MS) {
    digitalWrite(PIN_BUZZER, LOW); // 부저 OFF
    buzzerOn = false;
  }
}
