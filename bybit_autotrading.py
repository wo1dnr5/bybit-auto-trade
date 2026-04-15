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
  - CoinDesk / CoinTelegraph RSS 뉴스 헤드라인 (API 키 불필요, 무료)
  - Groq API (Llama 3.3 70B): 연준 정책, 달러 강세, 기관 동향, 규제 리스크 종합 분석

[리스크 관리]
  - 잔고 전액 증거금 사용 (레버리지 3~5x 자동 결정)
  - 거래소 스탑로스 (SL 2.5%) + 봇 루프 Hard SL (3.5% 강제 청산)
  - 자동 테이크프로핏 (TP 5.0% / RR=1:2)
  - 분할 익절: 1차 TP(50%) 지정가 주문 + 나머지 50% 반대 신호 시 청산
  - 레버리지 3~5x 동적 조절 (신호 강도 기반)
  - 격리 마진(Isolated) 모드 사용
────────────────────────────────────────────────

API 키 설정:
  .env 파일에 아래 항목 입력 (코드에 직접 입력 금지)
  BYBIT_API_KEY, BYBIT_SECRET_KEY, GROQ_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

필요 패키지:
  pip install pybit groq requests pandas numpy python-dotenv
"""

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone

from groq import Groq
import pandas as pd
import requests

try:
    from pybit.unified_trading import HTTP
except ImportError:
    raise ImportError("pybit 패키지가 필요합니다: pip install pybit")

# ──────────────────────────────────────────
# 사용자 설정
# ──────────────────────────────────────────
BYBIT_API_KEY      = os.environ.get("BYBIT_API_KEY", "")      # Bybit API Key
BYBIT_SECRET_KEY   = os.environ.get("BYBIT_SECRET_KEY", "")   # Bybit Secret Key
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")       # https://console.groq.com
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")     # 텔레그램 봇 토큰
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")   # 텔레그램 Chat ID

SYMBOLS        = ["ETHUSDT"]  # 거래 심볼 목록 (소액이므로 ETH만)
LEVERAGE       = 5           # 고정 레버리지
TIMEFRAME      = "60"        # 기본 타임프레임 (분 단위): 60 = 1시간봉
HIGHER_TF      = "240"       # 상위 타임프레임 (4시간봉) — 트렌드 필터
SL_PCT         = 0.020       # 스탑로스 비율 (2.0%) — 거래소 SL 주문용
HARD_SL_PCT    = 0.030       # 하드 스탑로스 비율 (3.0%) — 봇 루프 내 강제 청산 (거래소 SL 실패 대비)
TP_RATIO       = 1.5         # 리스크:리워드 = 1:TP_RATIO → TP = SL_PCT × TP_RATIO (2.0% × 1.5 = 3.0%)
NEWS_COUNT     = 5           # Groq에게 전달할 뉴스 개수
LOOP_SEC       = 30          # 루프 주기 (초): 30 = 30초
MACRO_CACHE_SEC = 600        # 거시경제 분석 캐싱 주기 (초): 600 = 10분
TESTNET        = False       # True = 테스트넷 사용 (실거래 전 반드시 테스트)
DRY_RUN        = False       # True = 드라이런 (분석만, 실제 주문 없음)

# ──────────────────────────────────────────
# 포지션 상태 (심볼별 인메모리, 봇 재시작 시 초기화)
# entry_qty     : 진입 시 수량 (부분청산 감지용)
# partial_closed: 1차 TP(50%) 청산 완료 여부
# ──────────────────────────────────────────
position_state: dict = {sym: {"entry_qty": 0.0, "partial_closed": False} for sym in SYMBOLS}

# ──────────────────────────────────────────
# 거시경제 분석 캐시 (10분마다 갱신, Groq 토큰 절약)
# ──────────────────────────────────────────
macro_cache: dict = {sym: {"data": None, "last_updated": 0.0} for sym in SYMBOLS}

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
# 텔레그램 알림
# ──────────────────────────────────────────
def send_telegram(message: str):
    """텔레그램 메시지 전송"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        log.warning(f"텔레그램 전송 실패: {e}")


# 텔레그램 명령어용 최신 상태 캐시
bot_status: dict = {
    "balance":  None,   # 최근 잔고 (USDT)
    "price":    None,   # 최근 현재가
    "position": None,   # 최근 포지션 dict (없으면 None)
    "tech":     None,   # 최근 기술 분석 결과
    "macro":    None,   # 최근 거시경제 분석 결과
}

_last_update_id: int = 0

def check_telegram_commands():
    """텔레그램 메시지 폴링 — 명령어 수신 시 자동 응답"""
    global _last_update_id
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        resp = requests.get(url, params={"offset": _last_update_id + 1, "timeout": 1}, timeout=5)
        updates = resp.json().get("result", [])
        for update in updates:
            _last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text == "잘 작동중이야?":
                send_telegram("네.")

            elif text == "잔고":
                bal = bot_status["balance"]
                send_telegram(f"💰 잔고: ${bal:,.2f} USDT" if bal else "잔고 정보 없음")

            elif text == "현재가":
                p = bot_status["price"]
                send_telegram(f"💹 ETH 현재가: ${p:,.2f}" if p else "현재가 정보 없음")

            elif text == "포지션":
                pos = bot_status["position"]
                if not pos:
                    send_telegram("📭 현재 포지션 없음")
                else:
                    side = "롱 📈" if pos["side"] == "Buy" else "숏 📉"
                    send_telegram(
                        f"📊 포지션\n"
                        f"방향: {side}\n"
                        f"진입가: ${float(pos['avgPrice']):,.2f}\n"
                        f"수량: {pos['size']} ETH"
                    )

            elif text == "점수":
                tech = bot_status["tech"]
                if not tech:
                    send_telegram("기술 분석 정보 없음")
                else:
                    send_telegram(
                        f"📐 기술 점수\n"
                        f"신호: {tech['signal']}\n"
                        f"점수: {tech['score']}/10\n"
                        f"EMA: {tech['details'].get('ema','')}\n"
                        f"RSI: {tech['details'].get('rsi','')}\n"
                        f"MACD: {tech['details'].get('macd','')}\n"
                        f"BB: {tech['details'].get('bb','')}"
                    )

            elif text == "거시경제":
                macro = bot_status["macro"]
                if not macro:
                    send_telegram("거시경제 분석 정보 없음")
                else:
                    send_telegram(
                        f"🌍 거시경제 분석\n"
                        f"Groq 신호: {macro['claude_sig']}\n"
                        f"신뢰도: {macro['confidence']}%\n"
                        f"근거: {macro['reasoning']}"
                    )

    except Exception as e:
        log.warning(f"텔레그램 커맨드 확인 실패: {e}")


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
    bbu, _, bbl = _bollinger(close, 20)
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
    bbl_v   = bbl.iloc[-1]
    atr_v   = atr.iloc[-1]
    vol_v   = volume.iloc[-1]
    vol_ma_v = vol_ma.iloc[-1]

    score   = 0
    details = {}

    # ① EMA 배열 — 가중치 3
    if e9 > e21 > e50 and c > e50:
        score += 2
        details["ema"] = f"정배열 (가격 > EMA50)"
    elif e9 < e21 < e50 and c < e50:
        score -= 2
        details["ema"] = f"역배열 (가격 < EMA50)"
    elif c > e21:
        score += 1
        details["ema"] = "부분 상승 (가격 > EMA21)"
    elif c < e21:
        score -= 1
        details["ema"] = "부분 하락 (가격 < EMA21)"
    else:
        details["ema"] = "NEUTRAL"

    # ② RSI — 가중치 3 (LONG: 35 이하 / SHORT: 75 이상)
    if r >= 75:
        score -= 3
        details["rsi"] = f"과매수 {r:.1f} (숏 진입 구간)"
    elif r <= 35:
        score += 3
        details["rsi"] = f"과매도 {r:.1f} (롱 진입 구간)"
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

    # ④ 볼린저 밴드 위치 — 가중치 1 (LONG: 50% 이하 / SHORT: 50% 이상)
    bb_range = bbu_v - bbl_v
    bb_pct   = (c - bbl_v) / (bb_range + 1e-10) * 100
    if c >= bbu_v:
        score -= 2
        details["bb"] = f"상단 돌파 (숏 구간, {bb_pct:.0f}%)"
    elif bb_pct >= 50:
        score -= 1
        details["bb"] = f"상단 절반 이상 (숏 우호, {bb_pct:.0f}%)"
    elif c <= bbl_v:
        score += 2
        details["bb"] = f"하단 터치 (롱 구간, {bb_pct:.0f}%)"
    elif bb_pct <= 50:
        score += 1
        details["bb"] = f"하단 절반 이하 (롱 우호, {bb_pct:.0f}%)"

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
    """
    CoinDesk / CoinTelegraph RSS 피드에서 뉴스 헤드라인 수집 (API 키 불필요)
    두 소스를 합산 후 count 개 반환
    """
    coin = "ETH" if "ETH" in symbol else "BTC"
    keyword = "ethereum" if coin == "ETH" else "bitcoin"

    rss_feeds = [
        f"https://www.coindesk.com/arc/outboundfeeds/rss/?query={keyword}",
        "https://cointelegraph.com/rss",
    ]

    headlines = []
    for url in rss_feeds:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                if title:
                    headlines.append(title)
        except Exception as e:
            log.warning(f"RSS 수집 실패 ({url}): {e}")

    # 코인 키워드 포함 기사 우선, 나머지는 뒤로
    related   = [h for h in headlines if keyword in h.lower() or coin.lower() in h.lower()]
    unrelated = [h for h in headlines if h not in related]
    merged    = (related + unrelated)[:count]
    log.info(f"뉴스 수집 완료 ({coin}): {len(merged)}건")
    return merged


def analyze_macro(headlines: list, fear_greed: dict, symbol: str) -> dict:
    """
    Groq API로 거시경제 + 뉴스 종합 분석
    반환: {signal: LONG|SHORT|NEUTRAL, confidence: int, reasoning: str}
    """
    coin      = "이더리움(ETH)" if "ETH" in symbol else "비트코인(BTC)"
    client    = Groq(api_key=GROQ_API_KEY)
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
        raw   = client.chat.completions.with_raw_response.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        remaining  = raw.headers.get("x-ratelimit-remaining-tokens", "?")
        reset_time = raw.headers.get("x-ratelimit-reset-tokens", "?")
        log.info(f"[Groq 토큰] 남은 일일 토큰={remaining} | 리셋까지={reset_time}")
        msg  = raw.parse()
        text = msg.choices[0].message.content.strip()
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
        log.warning(f"Groq 거시경제 분석 실패: {e}")

    return {"signal": "NEUTRAL", "confidence": 0, "reasoning": "분석 실패"}


def get_macro_signal(fear_greed: dict, headlines: list, symbol: str) -> dict:
    """Fear & Greed 바이어스 + Groq 분석 결합"""
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

    gemini = analyze_macro(headlines, fear_greed, symbol)

    return {
        "fg_value":   fgv,
        "fg_label":   fear_greed["label"],
        "fg_bias":    fg_bias,
        "claude_sig": gemini["signal"],
        "confidence": gemini["confidence"],
        "reasoning":  gemini["reasoning"],
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


def calc_leverage() -> int:
    """고정 레버리지 반환"""
    return LEVERAGE


def calc_qty(balance: float, price: float, leverage: int) -> float:
    """
    전체 잔고 기반 포지션 사이징
    잔고 전액을 증거금으로 사용
    qty = balance × leverage / price
    """
    qty = (balance * leverage) / price
    qty = round(qty, 3)
    return max(qty, 0.01)  # ETHUSDT 최소 주문 수량 0.01 ETH 보정


def _place_partial_tp(session: HTTP, symbol: str, side: str, half_qty: float, tp_price: float):
    """
    TP: reduce-only 지정가 주문으로 전량 청산
    side: 롱 포지션이면 "Sell", 숏 포지션이면 "Buy"
    """
    if DRY_RUN:
        log.info(f"[DRY RUN][TP 주문] 100% reduce-only 지정가 | qty={half_qty} | tp={tp_price:,.1f} | 실제 주문 없음")
        return
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
        log.info(f"[TP 주문] 100% reduce-only 지정가 | qty={half_qty} | tp={tp_price:,.1f} | orderId={resp['result'].get('orderId','')}")
    except Exception as e:
        log.error(f"TP 주문 실패: {e}")


def open_long(session: HTTP, symbol: str, qty: float, price: float):
    sl      = round(price * (1 - SL_PCT), 1)
    tp1     = round(price * (1 + SL_PCT * TP_RATIO), 1)   # TP 100%

    if DRY_RUN:
        log.info(f"[DRY RUN][LONG 진입] qty={qty} | price=${price:,.2f} | SL={sl:,.1f} | TP={tp1:,.1f} | 실제 주문 없음")
        position_state[symbol]["entry_qty"]      = qty
        position_state[symbol]["partial_closed"] = False
        return

    try:
        # 진입 주문: SL만 설정 (TP는 지정가 주문으로 처리)
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
        send_telegram(
            f"📈 [LONG 진입] {symbol}\n"
            f"가격: ${price:,.2f}\n"
            f"수량: {qty}\n"
            f"손절(SL): ${sl:,.1f}\n"
            f"TP: ${tp1:,.1f}"
        )
    except Exception as e:
        log.error(f"LONG 주문 실패: {e}")
        return

    # TP: 전량 지정가 매도 주문
    _place_partial_tp(session, symbol, "Sell", qty, tp1)

    # 상태 기록
    position_state[symbol]["entry_qty"]      = qty
    position_state[symbol]["partial_closed"] = False
    log.info(f"[TP 설정] TP={tp1:,.1f} (100% = {qty}개)")


def open_short(session: HTTP, symbol: str, qty: float, price: float):
    sl      = round(price * (1 + SL_PCT), 1)
    tp1     = round(price * (1 - SL_PCT * TP_RATIO), 1)   # TP 100%

    if DRY_RUN:
        log.info(f"[DRY RUN][SHORT 진입] qty={qty} | price=${price:,.2f} | SL={sl:,.1f} | TP={tp1:,.1f} | 실제 주문 없음")
        position_state[symbol]["entry_qty"]      = qty
        position_state[symbol]["partial_closed"] = False
        return

    try:
        # 진입 주문: SL만 설정 (TP는 지정가 주문으로 처리)
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
        send_telegram(
            f"📉 [SHORT 진입] {symbol}\n"
            f"가격: ${price:,.2f}\n"
            f"수량: {qty}\n"
            f"손절(SL): ${sl:,.1f}\n"
            f"TP: ${tp1:,.1f}"
        )
    except Exception as e:
        log.error(f"SHORT 주문 실패: {e}")
        return

    # TP: 전량 지정가 매수 주문
    _place_partial_tp(session, symbol, "Buy", qty, tp1)

    # 상태 기록
    position_state[symbol]["entry_qty"]      = qty
    position_state[symbol]["partial_closed"] = False
    log.info(f"[TP 설정] TP={tp1:,.1f} (100% = {qty}개)")


def close_position(session: HTTP, symbol: str, pos: dict):
    side = "Sell" if pos["side"] == "Buy" else "Buy"
    qty  = pos["size"]
    if DRY_RUN:
        log.info(f"[DRY RUN][포지션 청산] side={side} | qty={qty} | 실제 주문 없음")
        return
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
        send_telegram(
            f"🔴 [포지션 청산] {symbol}\n"
            f"방향: {side}\n"
            f"수량: {qty}"
        )
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
        f"[{symbol}][기술] 신호={tech['signal']} | 점수={tech['score']}/10 | "
        f"EMA={tech['details'].get('ema','')} | RSI={tech['details'].get('rsi','')} | "
        f"MACD={tech['details'].get('macd','')} | BB={tech['details'].get('bb','')} | "
        f"VOL={tech['details'].get('volume','')}"
    )

    # ── 상위 타임프레임 트렌드 (4시간봉) ────────
    htf = get_htf_trend(session, symbol)
    log.info(f"[{symbol}][4시간봉] {htf}")

    # ── 거시경제 분석 (10분 캐싱) ─────────────────
    cache     = macro_cache[symbol]
    now_ts    = time.time()
    if cache["data"] is None or (now_ts - cache["last_updated"]) >= MACRO_CACHE_SEC:
        fg        = get_fear_greed()
        headlines = fetch_news(symbol)
        macro     = get_macro_signal(fg, headlines, symbol)
        cache["data"]         = macro
        cache["last_updated"] = now_ts
        log.info(f"[{symbol}][거시경제] 캐시 갱신 (다음 갱신까지 {MACRO_CACHE_SEC//60}분)")
    else:
        macro     = cache["data"]
        remaining = int(MACRO_CACHE_SEC - (now_ts - cache["last_updated"]))
        log.info(f"[{symbol}][거시경제] 캐시 사용 중 (갱신까지 {remaining}초 남음)")
    log.info(
        f"[{symbol}][거시경제] F&G={macro['fg_value']}({macro['fg_label']}) → {macro['fg_bias']} | "
        f"Groq={macro['claude_sig']}(신뢰도 {macro['confidence']}%) | "
        f"근거: {macro['reasoning']}"
    )

    # ── 텔레그램 명령어용 상태 업데이트 ──────────
    bot_status["balance"]  = balance
    bot_status["price"]    = price
    bot_status["position"] = pos
    bot_status["tech"]     = tech
    bot_status["macro"]    = macro

    # ──────────────────────────────────────────
    # 진입 / 청산 조건 판단
    # ──────────────────────────────────────────
    want_long  = (
        tech["signal"] == "LONG"
        and htf == "LONG"
        and macro["claude_sig"] in ("LONG", "NEUTRAL")
        # Fear & Greed 조건 없음 — RSI/BB 기술 신호만으로 진입
    )
    want_short = (
        tech["signal"] == "SHORT"
        and htf == "SHORT"
        and macro["claude_sig"] in ("SHORT", "NEUTRAL")
        # 숏은 Fear & Greed 조건 없음 — RSI/BB 기술 신호만으로 진입
    )

    # 포지션 있을 때 → 부분청산 감지 + 청산 또는 유지
    if pos:
        current_qty = float(pos["size"])
        entry_qty   = state["entry_qty"]

        # ── Hard Stop Loss (봇 루프 내 강제 청산) ────────
        # 거래소 SL 주문 실패/슬리피지 대비 보조 안전장치
        # 롱: 현재가가 진입가 대비 HARD_SL_PCT 이상 하락 시 즉시 청산
        # 숏: 현재가가 진입가 대비 HARD_SL_PCT 이상 상승 시 즉시 청산
        entry = float(pos["avgPrice"])
        hard_sl_hit = (
            (pos["side"] == "Buy"  and price <= entry * (1 - HARD_SL_PCT)) or
            (pos["side"] == "Sell" and price >= entry * (1 + HARD_SL_PCT))
        )
        if hard_sl_hit:
            loss_pct = abs(price - entry) / entry * 100
            log.warning(
                f"[{symbol}][HARD SL 발동] {pos['side']} | 진입가=${entry:,.2f} | 현재가=${price:,.2f} | "
                f"손실={loss_pct:.2f}% (임계={HARD_SL_PCT*100:.1f}%) → 강제 청산"
            )
            send_telegram(
                f"🚨 [HARD SL 발동] {symbol}\n"
                f"방향: {pos['side']}\n"
                f"진입가: ${entry:,.2f}\n"
                f"현재가: ${price:,.2f}\n"
                f"손실: -{loss_pct:.2f}%\n"
                f"→ 강제 전량 청산"
            )
            close_position(session, symbol, pos)
            state["entry_qty"]      = 0.0
            state["partial_closed"] = False
            return

        # ── 1차 TP(50%) 체결 감지 ──────────────────
        if not state["partial_closed"] and entry_qty > 0:
            if current_qty <= entry_qty * 0.6:
                state["partial_closed"] = True
                log.info(
                    f"[{symbol}][1차 TP 체결] 50% 청산 완료 "
                    f"(진입qty={entry_qty} → 현재qty={current_qty}) | "
                    f"나머지 {current_qty}개 신호 대기 중"
                )
                send_telegram(
                    f"✅ [1차 TP 체결] {symbol}\n"
                    f"50% 부분 청산 완료\n"
                    f"남은 수량: {current_qty}"
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
        lev = calc_leverage()
        qty = calc_qty(balance, price, lev)
        log.info(f"[{symbol}][신호] LONG 진입 | qty={qty} | price=${price:,.2f} | 레버리지={lev}x")
        set_isolated_margin(session, symbol, lev)
        open_long(session, symbol, qty, price)

    elif want_short:
        lev = calc_leverage()
        qty = calc_qty(balance, price, lev)
        log.info(f"[{symbol}][신호] SHORT 진입 | qty={qty} | price=${price:,.2f} | 레버리지={lev}x")
        set_isolated_margin(session, symbol, lev)
        open_short(session, symbol, qty, price)

    else:
        log.info(
            f"[{symbol}] 진입 조건 미충족 — 대기 "
            f"(기술={tech['signal']}, 4h={htf}, 거시={macro['claude_sig']})"  # claude_sig 키명은 dict 내부용으로 유지
        )


# ──────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("=== Bybit 레버리지 선물 자동매매 시작 ===")
    log.info(f"심볼={SYMBOLS} | 레버리지={LEVERAGE}x (고정) | SL={SL_PCT*100:.1f}% | TP={SL_PCT*TP_RATIO*100:.1f}% | 루프={LOOP_SEC}s")
    log.info(f"테스트넷: {TESTNET} | 드라이런: {DRY_RUN}")
    if DRY_RUN:
        log.info("*** DRY RUN 모드: 분석만 수행, 실제 주문 없음 ***")
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
        check_telegram_commands()
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


