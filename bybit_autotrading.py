"""
Bybit 레버리지 선물 자동매매 프로그램

전략: 차티스트 + 거시경제 복합 분석
────────────────────────────────────────────────
[기술적 분석 - 차티스트 관점]
  - EMA(9 / 21 / 50) 배열 및 가격 위치
  - RSI(14): 과매수/과매도 + 모멘텀
  - MACD(12/26/9): 히스토그램 방향성
  - Bollinger Bands(20, 2σ): 변동성 + 추세
  - 거래량 급증 확인
  - 멀티 타임프레임(1h 기본 + 4h 트렌드 필터)

[거시경제 분석]
  - Fear & Greed Index (alternative.me)
  - CryptoPanic 주요 뉴스 헤드라인
  - Claude API: 연준 정책, 달러 강세, 기관 동향, 규제 리스크 종합 분석

[리스크 관리]
  - 계좌 자본의 2% 리스크 기반 포지션 사이징
  - 자동 스탑로스 / 테이크프로핏 (SL 2.5%, TP 5.0% / RR=1:2)
  - 반대 신호 발생 시 포지션 청산
  - 레버리지 5~10x 동적 조절 (신호 강도 기반)
────────────────────────────────────────────────

필요 패키지:
  pip install pybit anthropic requests pandas numpy
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

import anthropic
import pandas as pd
import requests

try:
    from pybit.unified_trading import HTTP
except ImportError:
    raise ImportError("pybit 패키지가 필요합니다: pip install pybit")

# ──────────────────────────────────────────
# 사용자 설정
# ──────────────────────────────────────────
BYBIT_API_KEY       = "YOUR_BYBIT_API_KEY"
BYBIT_SECRET_KEY    = "YOUR_BYBIT_SECRET_KEY"
ANTHROPIC_API_KEY   = "YOUR_ANTHROPIC_API_KEY"   # https://console.anthropic.com
CRYPTOPANIC_API_KEY = "YOUR_CRYPTOPANIC_API_KEY"  # https://cryptopanic.com/developers/api

SYMBOLS        = ["BTCUSDT", "ETHUSDT"]   # 거래 심볼 목록 (추가/제거 가능)
LEVERAGE_MIN   = 5           # 최소 레버리지 (신호 약할 때)
LEVERAGE_MAX   = 10          # 최대 레버리지 (신호 강할 때)
TIMEFRAME      = "60"        # 기본 타임프레임 (분 단위): 60 = 1시간봉
HIGHER_TF      = "240"       # 상위 타임프레임 (4시간봉) — 트렌드 필터
RISK_PER_TRADE = 0.02        # 계좌 자본 대비 최대 허용 리스크 (2%)
MAX_MARGIN_PCT = 0.20        # 포지션당 최대 증거금 비율 (잔고의 20%)
SL_PCT         = 0.025       # 스탑로스 비율 (2.5%)
TP_RATIO       = 2.0         # 리스크:리워드 = 1:TP_RATIO → TP = SL_PCT × TP_RATIO
NEWS_COUNT     = 15          # Claude에게 전달할 뉴스 개수
LOOP_SEC       = 120         # 루프 주기 (초): 120 = 2분
TESTNET        = False       # True = 테스트넷 사용 (실거래 전 반드시 테스트)

# ──────────────────────────────────────────
# 포지션 상태 (심볼별 인메모리, 봇 재시작 시 초기화)
# entry_qty     : 진입 시 수량 (부분청산 감지용)
# partial_closed: 1차 TP(50%) 청산 완료 여부
# ──────────────────────────────────────────
position_state: dict = {sym: {"entry_qty": 0.0, "partial_closed": False} for sym in SYMBOLS}

# ──────────────────────────────────────────
# 로깅
# ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bybit_trade_log.txt", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────
# Bybit 클라이언트 초기화
# ──────────────────────────────────────────
def init_bybit() -> HTTP:
    return HTTP(
        testnet=TESTNET,
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_SECRET_KEY,
    )


# ──────────────────────────────────────────
# 캔들 데이터 수집
# ──────────────────────────────────────────
def get_klines(session: HTTP, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """Bybit에서 OHLCV 캔들 데이터 조회"""
    try:
        resp = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
        raw = resp["result"]["list"]
        df = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.astype({
            "timestamp": "int64",
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df
    except Exception as e:
        log.error(f"캔들 데이터 수집 실패: {e}")
        return pd.DataFrame()


# ──────────────────────────────────────────
# 기술적 지표 계산
# ──────────────────────────────────────────
def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_f = _ema(series, fast)
    ema_s = _ema(series, slow)
    line  = ema_f - ema_s
    sig   = _ema(line, signal)
    hist  = line - sig
    return line, sig, hist


def _bollinger(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    mid   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hi, lo, cp = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(hi - lo), (hi - cp).abs(), (lo - cp).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def get_technical_signal(df: pd.DataFrame) -> dict:
    """
    기술적 지표 종합 분석
    반환: {signal: LONG|SHORT|NEUTRAL, score: int, atr: float, close: float}
    """
    close  = df["close"]
    volume = df["volume"]

    ema9   = _ema(close, 9)
    ema21  = _ema(close, 21)
    ema50  = _ema(close, 50)
    rsi    = _rsi(close, 14)
    _, _, hist = _macd(close)
    bbu, bbm, bbl = _bollinger(close, 20)
    atr    = _atr(df, 14)
    vol_ma = volume.rolling(20).mean()

    # 최신값
    c       = close.iloc[-1]
    e9      = ema9.iloc[-1]
    e21     = ema21.iloc[-1]
    e50     = ema50.iloc[-1]
    r       = rsi.iloc[-1]
    h       = hist.iloc[-1]
    h_prev  = hist.iloc[-2]
    bbu_v   = bbu.iloc[-1]
    bbm_v   = bbm.iloc[-1]
    bbl_v   = bbl.iloc[-1]
    atr_v   = atr.iloc[-1]
    vol_v   = volume.iloc[-1]
    vol_ma_v = vol_ma.iloc[-1]

    score   = 0
    details = {}

    # ① EMA 배열 — 가중치 3
    if e9 > e21 > e50 and c > e50:
        score += 3
        details["ema"] = f"정배열 (가격 > EMA50)"
    elif e9 < e21 < e50 and c < e50:
        score -= 3
        details["ema"] = f"역배열 (가격 < EMA50)"
    elif c > e21:
        score += 1
        details["ema"] = "부분 상승 (가격 > EMA21)"
    elif c < e21:
        score -= 1
        details["ema"] = "부분 하락 (가격 < EMA21)"
    else:
        details["ema"] = "NEUTRAL"

    # ② RSI — 가중치 2
    if r >= 70:
        score -= 2
        details["rsi"] = f"과매수 {r:.1f}"
    elif r <= 30:
        score += 2
        details["rsi"] = f"과매도 {r:.1f} (반등 가능)"
    elif r >= 55:
        score += 1
        details["rsi"] = f"강세 모멘텀 {r:.1f}"
    elif r <= 45:
        score -= 1
        details["rsi"] = f"약세 모멘텀 {r:.1f}"
    else:
        details["rsi"] = f"중립 {r:.1f}"

    # ③ MACD 히스토그램 — 가중치 2
    if h > 0 and h > h_prev:
        score += 2
        details["macd"] = f"상승 확대 hist={h:.2f}"
    elif h < 0 and h < h_prev:
        score -= 2
        details["macd"] = f"하락 확대 hist={h:.2f}"
    elif h > 0:
        score += 1
        details["macd"] = f"양수 hist={h:.2f}"
    elif h < 0:
        score -= 1
        details["macd"] = f"음수 hist={h:.2f}"
    else:
        details["macd"] = "NEUTRAL"

    # ④ 볼린저 밴드 위치 — 가중치 1
    bb_range = bbu_v - bbl_v
    bb_pct   = (c - bbl_v) / (bb_range + 1e-10) * 100
    if c >= bbu_v:
        score -= 1
        details["bb"] = f"상단 돌파 (과열, {bb_pct:.0f}%)"
    elif c <= bbl_v:
        score += 1
        details["bb"] = f"하단 터치 (과매도, {bb_pct:.0f}%)"
    elif c > bbm_v:
        score += 1
        details["bb"] = f"중간 위 ({bb_pct:.0f}%)"
    else:
        score -= 1
        details["bb"] = f"중간 아래 ({bb_pct:.0f}%)"

    # ⑤ 거래량 급증 — 방향 가중치 1
    vol_ratio = vol_v / (vol_ma_v + 1e-10)
    if vol_ratio >= 1.5:
        direction = +1 if c > close.iloc[-2] else -1
        score    += direction
        details["volume"] = f"급증 {vol_ratio:.1f}x (방향: {'▲' if direction > 0 else '▼'})"
    else:
        details["volume"] = f"보통 {vol_ratio:.1f}x"

    # 최종 신호
    if score >= 5:
        signal = "LONG"
    elif score <= -5:
        signal = "SHORT"
    else:
        signal = "NEUTRAL"

    return {
        "signal": signal,
        "score": score,
        "details": details,
        "atr": atr_v,
        "close": c,
    }


def get_htf_trend(session: HTTP, symbol: str) -> str:
    """
    4시간봉 기준 추세 판단 (트렌드 필터)
    EMA50 위 → LONG, 아래 → SHORT
    """
    df = get_klines(session, symbol, HIGHER_TF, 100)
    if df.empty:
        return "NEUTRAL"
    ema50 = _ema(df["close"], 50)
    c     = df["close"].iloc[-1]
    e50   = ema50.iloc[-1]
    if c > e50:
        return "LONG"
    elif c < e50:
        return "SHORT"
    return "NEUTRAL"


# ──────────────────────────────────────────
# 거시경제 지표 수집
# ──────────────────────────────────────────
def get_fear_greed() -> dict:
    """Alternative.me Fear & Greed Index 수집"""
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        resp.raise_for_status()
        data = resp.json()["data"][0]
        return {
            "value": int(data["value"]),
            "label": data["value_classification"],
        }
    except Exception as e:
        log.warning(f"Fear & Greed 수집 실패: {e}")
        return {"value": 50, "label": "Neutral"}


def fetch_news(symbol: str, count: int = NEWS_COUNT) -> list:
    """CryptoPanic 심볼별 주요 뉴스 헤드라인 수집"""
    coin = "ETH" if "ETH" in symbol else "BTC"
    try:
        resp = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={
                "auth_token": CRYPTOPANIC_API_KEY,
                "currencies": coin,
                "kind": "news",
                "filter": "important",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return [item["title"] for item in resp.json().get("results", [])[:count]]
    except Exception as e:
        log.warning(f"뉴스 수집 실패 ({coin}): {e}")
        return []


def analyze_macro(headlines: list, fear_greed: dict, symbol: str) -> dict:
    """
    Claude API로 거시경제 + 뉴스 종합 분석
    반환: {signal: LONG|SHORT|NEUTRAL, confidence: int, reasoning: str}
    """
    coin      = "이더리움(ETH)" if "ETH" in symbol else "비트코인(BTC)"
    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_text = "\n".join(f"- {h}" for h in headlines) if headlines else "- (수집된 뉴스 없음)"

    prompt = f"""당신은 {coin} 선물 트레이딩을 위한 거시경제 분석 전문가입니다.

현재 시장 데이터:
- 분석 대상: {coin} ({symbol})
- 날짜/시간: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC
- Fear & Greed Index: {fear_greed['value']} / 100 ({fear_greed['label']})

최근 주요 {coin} 관련 뉴스:
{news_text}

아래 5가지 관점에서 {coin} 선물(롱/숏) 방향성을 종합 분석하세요.

1. 미국 연준(Fed) 통화정책 기조: 금리 인상/동결/인하 기대감
2. 달러 인덱스(DXY) 방향성: 달러 강세 = BTC 약세 압력
3. 기관 투자자 동향: ETF 자금 유출입, 대형 매수/매도 신호
4. 규제 및 지정학 리스크: 각국 정부 정책, 글로벌 이벤트
5. 시장 심리: Fear & Greed 수치 해석

분석 결과를 반드시 아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "signal": "LONG" 또는 "SHORT" 또는 "NEUTRAL",
  "confidence": 0~100,
  "reasoning": "핵심 근거 2~3줄 요약"
}}"""

    try:
        msg  = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = msg.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            sig    = result.get("signal", "NEUTRAL").upper()
            if sig not in ("LONG", "SHORT", "NEUTRAL"):
                sig = "NEUTRAL"
            return {
                "signal":     sig,
                "confidence": int(result.get("confidence", 50)),
                "reasoning":  result.get("reasoning", ""),
            }
    except Exception as e:
        log.warning(f"Claude 거시경제 분석 실패: {e}")

    return {"signal": "NEUTRAL", "confidence": 0, "reasoning": "분석 실패"}


def get_macro_signal(fear_greed: dict, headlines: list, symbol: str) -> dict:
    """Fear & Greed 바이어스 + Claude 분석 결합"""
    fgv = fear_greed["value"]

    # Fear & Greed 단독 해석
    if fgv >= 80:
        fg_bias = "SHORT"   # 극단적 탐욕 → 역추세 숏 경고
    elif fgv >= 55:
        fg_bias = "LONG"    # 탐욕 → 상승 우호
    elif fgv <= 20:
        fg_bias = "LONG"    # 극단적 공포 → 역추세 롱 기회
    elif fgv <= 45:
        fg_bias = "SHORT"   # 공포 → 하락 우호
    else:
        fg_bias = "NEUTRAL"

    claude = analyze_macro(headlines, fear_greed, symbol)

    return {
        "fg_value":   fgv,
        "fg_label":   fear_greed["label"],
        "fg_bias":    fg_bias,
        "claude_sig": claude["signal"],
        "confidence": claude["confidence"],
        "reasoning":  claude["reasoning"],
    }


# ──────────────────────────────────────────
# 포지션 관리
# ──────────────────────────────────────────
def get_position(session: HTTP, symbol: str) -> dict | None:
    """현재 오픈 포지션 조회"""
    try:
        resp = session.get_positions(category="linear", symbol=symbol)
        for pos in resp["result"]["list"]:
            if float(pos.get("size", 0)) > 0:
                return pos
        return None
    except Exception as e:
        log.error(f"포지션 조회 실패: {e}")
        return None


def get_balance(session: HTTP) -> float:
    """USDT 총 자산(equity) 조회"""
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        return float(resp["result"]["list"][0]["totalEquity"])
    except Exception as e:
        log.error(f"잔고 조회 실패: {e}")
        return 0.0


def set_isolated_margin(session: HTTP, symbol: str, leverage: int):
    """
    격리 마진(Isolated Margin) 모드 설정 + 레버리지 지정
    tradeMode=1: Isolated / tradeMode=0: Cross
    포지션이 없을 때만 전환 가능 (Bybit 정책)
    """
    try:
        session.switch_margin_mode(
            category="linear",
            symbol=symbol,
            tradeMode=1,                # 1 = Isolated Margin
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        log.info(f"격리 마진 모드 설정 완료 ({leverage}x)")
    except Exception as e:
        # 이미 격리 마진이거나 포지션 보유 중인 경우 무시
        log.debug(f"격리 마진 설정 스킵 (이미 설정됨 또는 포지션 보유): {e}")


def calc_leverage(tech_score: int, macro_confidence: int) -> int:
    """
    신호 강도에 따라 레버리지 동적 결정 (LEVERAGE_MIN ~ LEVERAGE_MAX)
    tech_score   : 기술 분석 점수 (최대 ±9)
    macro_confidence : Claude 거시 신뢰도 (0~100)
    두 지표를 0~1로 정규화해 평균 → 레버리지 매핑
    """
    tech_norm  = min(abs(tech_score) / 9.0, 1.0)          # 0.0 ~ 1.0
    macro_norm = min(macro_confidence / 100.0, 1.0)        # 0.0 ~ 1.0
    strength   = (tech_norm + macro_norm) / 2              # 0.0 ~ 1.0
    leverage   = LEVERAGE_MIN + round(strength * (LEVERAGE_MAX - LEVERAGE_MIN))
    return int(leverage)


def calc_qty(balance: float, price: float, leverage: int) -> float:
    """
    리스크 기반 포지션 사이징
    손실 허용 금액 = balance × RISK_PER_TRADE
    qty = 손실 허용액 / (진입가 × SL_PCT)
    증거금 상한 = balance × MAX_MARGIN_PCT → qty 상한 = 상한증거금 × leverage / price
    """
    risk_usdt = balance * RISK_PER_TRADE
    qty       = risk_usdt / (price * SL_PCT)
    max_qty   = (balance * MAX_MARGIN_PCT * leverage) / price
    return round(min(qty, max_qty), 3)


def _place_partial_tp(session: HTTP, symbol: str, side: str, half_qty: float, tp_price: float):
    """
    1차 TP: reduce-only 지정가 주문으로 50%만 청산
    side: 롱 포지션이면 "Sell", 숏 포지션이면 "Buy"
    """
    try:
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Limit",
            price=str(tp_price),
            qty=str(half_qty),
            reduceOnly=True,
            timeInForce="GTC",          # 체결될 때까지 유지
        )
        log.info(f"[1차 TP 주문] 50% reduce-only 지정가 | qty={half_qty} | tp={tp_price:,.1f} | orderId={resp['result'].get('orderId','')}")
    except Exception as e:
        log.error(f"1차 TP 주문 실패: {e}")


def open_long(session: HTTP, symbol: str, qty: float, price: float):
    sl      = round(price * (1 - SL_PCT), 1)
    tp1     = round(price * (1 + SL_PCT * TP_RATIO), 1)   # 1차 TP (50%)
    half    = round(qty / 2, 3)

    try:
        # 진입 주문: SL만 설정 (TP는 분할 처리)
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy",
            orderType="Market",
            qty=str(qty),
            stopLoss=str(sl),
            slTriggerBy="MarkPrice",
            reduceOnly=False,
            closeOnTrigger=False,
        )
        log.info(f"[LONG 진입] qty={qty} | SL={sl:,.1f} | orderId={resp['result'].get('orderId','')}")
    except Exception as e:
        log.error(f"LONG 주문 실패: {e}")
        return

    # 1차 TP: 50%만 지정가 매도 주문
    _place_partial_tp(session, symbol, "Sell", half, tp1)

    # 상태 기록
    position_state[symbol]["entry_qty"]      = qty
    position_state[symbol]["partial_closed"] = False
    log.info(f"[분할매도 설정] 1차 TP={tp1:,.1f} (50% = {half}개) | 나머지 {half}개는 신호 청산 대기")


def open_short(session: HTTP, symbol: str, qty: float, price: float):
    sl      = round(price * (1 + SL_PCT), 1)
    tp1     = round(price * (1 - SL_PCT * TP_RATIO), 1)   # 1차 TP (50%)
    half    = round(qty / 2, 3)

    try:
        # 진입 주문: SL만 설정 (TP는 분할 처리)
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            stopLoss=str(sl),
            slTriggerBy="MarkPrice",
            reduceOnly=False,
            closeOnTrigger=False,
        )
        log.info(f"[SHORT 진입] qty={qty} | SL={sl:,.1f} | orderId={resp['result'].get('orderId','')}")
    except Exception as e:
        log.error(f"SHORT 주문 실패: {e}")
        return

    # 1차 TP: 50%만 지정가 매수 주문
    _place_partial_tp(session, symbol, "Buy", half, tp1)

    # 상태 기록
    position_state[symbol]["entry_qty"]      = qty
    position_state[symbol]["partial_closed"] = False
    log.info(f"[분할매도 설정] 1차 TP={tp1:,.1f} (50% = {half}개) | 나머지 {half}개는 신호 청산 대기")


def close_position(session: HTTP, symbol: str, pos: dict):
    side = "Sell" if pos["side"] == "Buy" else "Buy"
    qty  = pos["size"]
    try:
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty,
            reduceOnly=True,
        )
        log.info(f"[포지션 청산] side={side} | qty={qty} | orderId={resp['result'].get('orderId','')}")
    except Exception as e:
        log.error(f"청산 실패: {e}")


# ──────────────────────────────────────────
# 메인 트레이딩 로직 (심볼별 호출)
# ──────────────────────────────────────────
def trade(session: HTTP, symbol: str):
    log.info(f"── {symbol} 분석 시작 ──")
    state = position_state[symbol]

    # ── 현재 상태 확인 ──────────────────────
    pos     = get_position(session, symbol)
    balance = get_balance(session)
    log.info(f"[{symbol}] 잔고: ${balance:.2f} USDT")

    if pos:
        side  = pos["side"]
        size  = pos["size"]
        entry = float(pos["avgPrice"])
        pnl   = float(pos.get("unrealisedPnl", 0))
        log.info(f"[{symbol}] 포지션: {side} {size} @ ${entry:,.2f} | 미실현PnL: {pnl:+.2f} USDT")

    # ── 캔들 데이터 수집 ──────────────────────
    df = get_klines(session, symbol, TIMEFRAME, 200)
    if df.empty:
        log.error(f"[{symbol}] 캔들 데이터 없음 — 스킵")
        return

    price = df["close"].iloc[-1]
    log.info(f"[{symbol}] 현재가: ${price:,.2f}")

    # ── 기술적 분석 (1시간봉) ─────────────────
    tech = get_technical_signal(df)
    log.info(
        f"[{symbol}][기술] 신호={tech['signal']} | 점수={tech['score']}/9 | "
        f"EMA={tech['details'].get('ema','')} | RSI={tech['details'].get('rsi','')} | "
        f"MACD={tech['details'].get('macd','')} | BB={tech['details'].get('bb','')} | "
        f"VOL={tech['details'].get('volume','')}"
    )

    # ── 상위 타임프레임 트렌드 (4시간봉) ────────
    htf = get_htf_trend(session, symbol)
    log.info(f"[{symbol}][4시간봉] {htf}")

    # ── 거시경제 분석 ──────────────────────────
    fg        = get_fear_greed()
    headlines = fetch_news(symbol)
    macro     = get_macro_signal(fg, headlines, symbol)
    log.info(
        f"[{symbol}][거시경제] F&G={macro['fg_value']}({macro['fg_label']}) → {macro['fg_bias']} | "
        f"Claude={macro['claude_sig']}(신뢰도 {macro['confidence']}%) | "
        f"근거: {macro['reasoning']}"
    )

    # ──────────────────────────────────────────
    # 진입 / 청산 조건 판단
    # ──────────────────────────────────────────
    want_long  = (
        tech["signal"] == "LONG"
        and htf == "LONG"
        and macro["claude_sig"] in ("LONG", "NEUTRAL")
        and macro["fg_value"] < 80
    )
    want_short = (
        tech["signal"] == "SHORT"
        and htf == "SHORT"
        and macro["claude_sig"] in ("SHORT", "NEUTRAL")
        and macro["fg_value"] > 20
    )

    # 포지션 있을 때 → 부분청산 감지 + 청산 또는 유지
    if pos:
        current_qty = float(pos["size"])
        entry_qty   = state["entry_qty"]

        # ── 1차 TP(50%) 체결 감지 ──────────────────
        if not state["partial_closed"] and entry_qty > 0:
            if current_qty <= entry_qty * 0.6:
                state["partial_closed"] = True
                log.info(
                    f"[{symbol}][1차 TP 체결] 50% 청산 완료 "
                    f"(진입qty={entry_qty} → 현재qty={current_qty}) | "
                    f"나머지 {current_qty}개 신호 대기 중"
                )
        elif entry_qty == 0:
            state["partial_closed"] = True

        # ── 나머지 포지션 청산 조건 (반대 신호) ────
        if pos["side"] == "Buy" and tech["signal"] == "SHORT" and htf == "SHORT":
            log.info(f"[{symbol}][청산] 롱 나머지 포지션 반대 신호 → 전량 청산")
            close_position(session, symbol, pos)
            state["entry_qty"]      = 0.0
            state["partial_closed"] = False
        elif pos["side"] == "Sell" and tech["signal"] == "LONG" and htf == "LONG":
            log.info(f"[{symbol}][청산] 숏 나머지 포지션 반대 신호 → 전량 청산")
            close_position(session, symbol, pos)
            state["entry_qty"]      = 0.0
            state["partial_closed"] = False
        else:
            log.info(
                f"[{symbol}] 포지션 유지 | qty={current_qty} | "
                f"1차TP {'완료' if state['partial_closed'] else '대기 중'}"
            )
        return

    # 포지션 없을 때 → 신규 진입
    if want_long:
        lev = calc_leverage(tech["score"], macro["confidence"])
        qty = calc_qty(balance, price, lev)
        log.info(f"[{symbol}][신호] LONG 진입 | qty={qty} | price=${price:,.2f} | 레버리지={lev}x")
        set_isolated_margin(session, symbol, lev)
        open_long(session, symbol, qty, price)

    elif want_short:
        lev = calc_leverage(tech["score"], macro["confidence"])
        qty = calc_qty(balance, price, lev)
        log.info(f"[{symbol}][신호] SHORT 진입 | qty={qty} | price=${price:,.2f} | 레버리지={lev}x")
        set_isolated_margin(session, symbol, lev)
        open_short(session, symbol, qty, price)

    else:
        log.info(
            f"[{symbol}] 진입 조건 미충족 — 대기 "
            f"(기술={tech['signal']}, 4h={htf}, 거시={macro['claude_sig']})"
        )


# ──────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("=== Bybit 레버리지 선물 자동매매 시작 ===")
    log.info(f"심볼={SYMBOLS} | 레버리지={LEVERAGE_MIN}~{LEVERAGE_MAX}x (동적) | SL={SL_PCT*100:.1f}% | TP={SL_PCT*TP_RATIO*100:.1f}% | 루프={LOOP_SEC}s")
    log.info(f"테스트넷: {TESTNET}")
    log.info("=" * 65)

    session = init_bybit()

    # API 연결 확인
    try:
        balance = get_balance(session)
        if balance <= 0:
            raise ValueError("잔고 0 또는 API 키 오류")
        log.info(f"Bybit API 연결 성공 | 잔고: ${balance:.2f} USDT")
    except Exception as e:
        log.error(f"Bybit 연결 실패: {e}")
        return

    while True:
        log.info("=" * 65)
        log.info(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] 사이클 시작")
        for sym in SYMBOLS:
            try:
                trade(session, sym)
            except Exception as e:
                log.error(f"[{sym}] 트레이딩 오류: {e}", exc_info=True)

        log.info(f"다음 실행까지 {LOOP_SEC}초 대기...\n")
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()


