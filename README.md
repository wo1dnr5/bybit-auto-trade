# Bybit 레버리지 선물 자동매매 봇

기술적 분석과 거시경제 분석을 결합한 ETH 선물 자동매매 프로그램입니다.

---

## 전략 개요

```
2분마다 반복
  ↓
[1] 기술적 분석 (1시간봉)         → LONG / SHORT / NEUTRAL
[2] 추세 필터  (4시간봉 EMA50)    → LONG / SHORT
[3] 거시경제 분석
    - Fear & Greed Index
    - CoinDesk / CoinTelegraph RSS 뉴스
    - Groq AI (Llama 3.3 70B) 종합 판단 → LONG / SHORT / NEUTRAL
  ↓
세 가지가 모두 같은 방향 → 진입
반대 신호 발생 → 청산
```

---

## 기술 지표 (점수 기반, 최대 ±9점)

| 지표 | 가중치 | LONG 조건 | SHORT 조건 |
|------|--------|-----------|------------|
| EMA 9/21/50 배열 | ±3 | 정배열 + 가격 > EMA50 | 역배열 + 가격 < EMA50 |
| RSI(14) | ±2 | 30 이하(과매도) 또는 55 이상 | 70 이상(과매수) 또는 45 이하 |
| MACD 히스토그램 | ±2 | 양수 + 상승 확대 | 음수 + 하락 확대 |
| 볼린저밴드 위치 | ±1 | 하단 터치 또는 중간 위 | 상단 돌파 또는 중간 아래 |
| 거래량 급증 (1.5x↑) | ±1 | 상승봉 + 급증 | 하락봉 + 급증 |

> **5점 이상 → LONG / -5점 이하 → SHORT / 그 사이 → NEUTRAL**

---

## 진입 조건 (전부 충족해야 진입)

| 조건 | LONG | SHORT |
|------|------|-------|
| 1시간봉 신호 | LONG | SHORT |
| 4시간봉 트렌드 | EMA50 위 | EMA50 아래 |
| Groq AI 판단 | LONG 또는 NEUTRAL | SHORT 또는 NEUTRAL |
| Fear & Greed | 80 미만 | 20 초과 |

---

## 리스크 관리

| 항목 | 설정값 |
|------|--------|
| 거래 심볼 | ETHUSDT (소액 계좌 기준) |
| 레버리지 | 3x ~ 5x (신호 강도에 따라 자동 결정) |
| 손절 (SL) | 진입가 ±2.5% (마크 프라이스 기준) |
| 1차 익절 (TP1) | 진입가 ±5.0% (RR = 1:2) |
| 포지션 사이징 | 계좌 자본의 1% 손실 기준 자동 계산 |
| 최대 증거금 | 잔고의 20% 이하 |
| 마진 모드 | 격리 마진 (Isolated) |

---

## 분할 익절 전략

```
진입 후
  ├─ 1차 TP (±5%): 수량의 50%를 지정가 주문으로 자동 청산
  └─ 나머지 50%: 반대 신호 발생 시 전량 청산
```

---

## 뉴스 분석

**CoinDesk / CoinTelegraph RSS 피드**에서 최신 뉴스를 자동 수집합니다.

- API 키 불필요, 완전 무료
- ETH 관련 기사 우선 정렬 후 상위 15개를 Groq AI에 전달
- 수집 실패 시에도 봇은 계속 실행됨

---

## 필요 패키지

```bash
pip3 install pybit groq requests pandas numpy python-dotenv
```

---

## API 키 발급

| API 키 | 발급 경로 |
|--------|----------|
| Bybit API Key / Secret | Bybit → 계정 → API 관리 → 키 생성 (선물 거래 권한 필요) |
| Groq API Key | [console.groq.com](https://console.groq.com) (무료) |

---

## 설정 방법

프로젝트 폴더에 `.env` 파일을 생성하고 아래 내용을 입력합니다.

```
BYBIT_API_KEY=발급받은키
BYBIT_SECRET_KEY=발급받은시크릿
GROQ_API_KEY=발급받은키
```

> `.env` 파일은 `.gitignore`에 포함되어 GitHub에 올라가지 않습니다.

그 외 설정은 `bybit_autotrading.py` 상단에서 수정합니다.

```python
SYMBOLS        = ["ETHUSDT"]  # 거래 심볼
LEVERAGE_MIN   = 3            # 최소 레버리지
LEVERAGE_MAX   = 5            # 최대 레버리지
RISK_PER_TRADE = 0.01         # 거래당 최대 손실 비율 (1%)
LOOP_SEC       = 120          # 봇 반복 주기 (초)
TESTNET        = False        # True = 테스트넷 사용
DRY_RUN        = False        # True = 드라이런 (실제 주문 없음)
```

---

## 실행

```bash
python3 bybit_autotrading.py
```

---

## Claude → Groq 전환 이유

초기에는 Claude API(Anthropic)를 사용했으나 아래 이유로 Groq으로 전환했습니다.

- **Claude API**: 무료 플랜 없음, 크레딧 소진 후 유료
- **Gemini API**: 한국 리전에서 무료 티어 할당량 0으로 제한됨
- **Groq API**: 한국 포함 전 세계 무료 제공, 응답 속도 빠름

Groq은 자체 LPU(Language Processing Unit) 칩 기반으로 오픈소스 모델(Llama 3.3 70B)을 무료로 호스팅합니다.

---

## 주의사항

- 실거래 전 `DRY_RUN = True`로 먼저 테스트하세요.
- 봇 재시작 시 기존 포지션의 분할 청산 상태가 초기화됩니다.
- **API 키를 코드에 직접 입력하지 마세요.** 반드시 `.env` 파일을 사용하세요.
- 암호화폐 선물 거래는 높은 레버리지로 인해 원금 손실 위험이 있습니다.
- Bybit Unified Trading 계좌에 USDT가 있어야 매매 가능합니다.

---

## 로그

실행 중 모든 분석 결과와 주문 내역은 `bybit_trade_log.txt`에 자동 기록됩니다.
