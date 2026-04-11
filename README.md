# Bybit 레버리지 선물 자동매매 봇

기술적 분석과 거시경제 분석을 결합한 BTC/ETH 선물 자동매매 프로그램입니다.

---

## 전략 개요

```
2분마다 반복
  ↓
[1] 기술적 분석 (1시간봉)         → LONG / SHORT / NEUTRAL
[2] 추세 필터  (4시간봉 EMA50)    → LONG / SHORT
[3] 거시경제 분석
    - Fear & Greed Index
    - CryptoPanic 뉴스
    - Claude AI 종합 판단         → LONG / SHORT / NEUTRAL
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
| Claude AI 판단 | LONG 또는 NEUTRAL | SHORT 또는 NEUTRAL |
| Fear & Greed | 80 미만 | 20 초과 |

---

## 리스크 관리

| 항목 | 기본값 |
|------|--------|
| 레버리지 | 5x ~ 10x (신호 강도에 따라 자동 결정) |
| 손절 (SL) | 진입가 ±2.5% (마크 프라이스 기준) |
| 1차 익절 (TP1) | 진입가 ±5.0% (RR = 1:2) |
| 포지션 사이징 | 계좌 자본의 2% 손실 기준 자동 계산 |
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

## 필요 패키지

```bash
pip install pybit anthropic requests pandas numpy
```

---

## API 키 발급

| API 키 | 발급 경로 |
|--------|----------|
| Bybit API Key / Secret | Bybit → 계정 → API 관리 → 키 생성 (선물 거래 권한 필요) |
| Anthropic API Key | [console.anthropic.com](https://console.anthropic.com) |
| CryptoPanic API Key | [cryptopanic.com/developers/api](https://cryptopanic.com/developers/api) (무료) |

---

## 설정 방법

`bybit_autotrading.py` 상단의 사용자 설정 항목을 수정합니다.

```python
BYBIT_API_KEY       = "YOUR_BYBIT_API_KEY"
BYBIT_SECRET_KEY    = "YOUR_BYBIT_SECRET_KEY"
ANTHROPIC_API_KEY   = "YOUR_ANTHROPIC_API_KEY"
CRYPTOPANIC_API_KEY = "YOUR_CRYPTOPANIC_API_KEY"

SYMBOLS        = ["BTCUSDT", "ETHUSDT"]  # 거래 심볼
LEVERAGE_MIN   = 5                        # 최소 레버리지
LEVERAGE_MAX   = 10                       # 최대 레버리지
RISK_PER_TRADE = 0.02                     # 거래당 최대 손실 비율 (2%)
LOOP_SEC       = 120                      # 봇 반복 주기 (초)
TESTNET        = False                    # True = 테스트넷 사용
```

---

## 실행

```bash
# 테스트넷으로 먼저 실행 권장 (TESTNET = True 설정 후)
python bybit_autotrading.py
```

---

## 주의사항

- 실거래 전 반드시 `TESTNET = True`로 충분히 테스트하세요.
- 봇 재시작 시 기존 포지션의 분할 청산 상태가 초기화됩니다.
- **API 키를 코드에 직접 입력한 후 GitHub에 올리지 마세요.**
- 암호화폐 선물 거래는 높은 레버리지로 인해 원금 손실 위험이 있습니다.

---

## 로그

실행 중 모든 분석 결과와 주문 내역은 `bybit_trade_log.txt`에 자동 기록됩니다.
