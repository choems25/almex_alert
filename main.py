import websocket
import json
import requests
from collections import deque
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
from datetime import datetime, timedelta

# ==================== [설정 영역] ====================
FINNHUB_TOKEN = os.environ.get("FINNHUB_API_KEY", "dapr4a9r01qqnrhtp010dapr4a9r01qqnrhtp01g")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8926579638:AAGQvhDFLu7mPpM_Jdov4VxQsTYDoVz0TnE")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8818075368")

MIN_PRICE = 0.1
MAX_PRICE = 7.0        # $0.1 ~ $7.0 미만
SURGE_RATIO = 2.5      # 1분 내 +2.5% 이상 급등
VOL_MULTIPLIER = 3.0   # 거래량 3배 폭증
ALERT_COOLDOWN = 120   # 쿨타임 (120초)

# 7대 핵심 폭등 섹터 키워드
FAVORITE_SECTORS = [
    "Artificial Intelligence", "Semiconductors", "Semiconductor", "Technology",
    "Biotechnology", "Pharmaceuticals", 
    "Mining", "Metals & Mining",
    "Clean Energy", "Renewable Energy", "Lithium", "Solar", "Battery",
    "Aerospace", "Space", "Satellite", "Defense",
    "Blockchain", "Fintech", "Cryptocurrency"
]

active_watch_list = []
watchlist_lock = threading.Lock()
history_data = {}
last_alert_time = {}

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=2)
    except Exception as e:
        print(f"알림 전송 실패: {e}")

# ==================== [데일리 스마트 스크리너] ====================
def fetch_smart_watchlist():
    print(f"[{time.strftime('%H:%M:%S')}] 🔍 [30일 50% 폭등 제외 + 7대 테마 우선] 스마트 스크리너 가동 중...")
    
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_TOKEN}"
    try:
        res = requests.get(url, timeout=10)
        symbols_data = res.json()
    except Exception as e:
        print(f"심볼 조회 실패: {e}")
        return ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU"]

    prioritized_list = []
    general_list = []

    # 30일 전 타임스탬프 계산
    to_ts = int(time.time())
    from_ts = to_ts - (30 * 86400)

    count = 0
    for item in symbols_data:
        ticker = item.get('symbol')
        if '.' in ticker or '^' in ticker or len(ticker) > 5:
            continue
            
        if count >= 350:  # 검토 풀 350개
            break
            
        try:
            # 1. 현재가 확인 ($0.1 ~ $7.0 미만)
            quote_url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}"
            q_res = requests.get(quote_url, timeout=2).json()
            price = q_res.get('c', 0)
            
            if not price or not (MIN_PRICE <= price < MAX_PRICE):
                time.sleep(0.04)
                continue
                
            # 2. 최근 30일간 50% 이상 폭등 이력(설거지 구간) 확인
            candle_url = f"https://finnhub.io/api/v1/stock/candle?symbol={ticker}&resolution=D&from={from_ts}&to={to_ts}&token={FINNHUB_TOKEN}"
            c_res = requests.get(candle_url, timeout=2).json()
            
            if c_res.get('s') == 'ok':
                lows = c_res.get('l', [])
                highs = c_res.get('h', [])
                if lows and highs:
                    min_low = min(lows)
                    max_high = max(highs)
                    # 30일 내 최저점 대비 최고점이 50% 이상(+50% 이상 슈팅) 올랐던 적이 있다면 제외!
                    if min_low > 0 and (max_high - min_low) / min_low >= 0.5:
                        time.sleep(0.04)
                        continue # 폭등 이력 있으므로 패스!

            # 3. 기업 프로필(섹터) 확인
            profile_url = f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={FINNHUB_TOKEN}"
            p_res = requests.get(profile_url, timeout=2).json()
            industry = p_res.get('finnhubIndustry', '')
            
            is_favorite = any(sec.lower() in industry.lower() for sec in FAVORITE_SECTORS)
            
            if is_favorite:
                prioritized_list.append(ticker)
            else:
                general_list.append(ticker)
                
            count += 1
            time.sleep(0.04)
            
            if len(prioritized_list) + len(general_list) >= 100:
                break
        except Exception:
            continue

    combined = prioritized_list + general_list
    final_50 = combined[:50]
    
    if len(final_50) < 10:
        final_50 = ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU", "DNN", "UAMY"]

    print(f"[{time.strftime('%H:%M:%S')}] ✨ 최종 선별된 50개 종목 (폭등 제외 완료): {final_50}")
    return final_50

# ==================== [웹소켓 실시간 감시 엔진] ====================
def on_message(ws, message):
    data = json.loads(message)
    if data.get('type') == 'trade':
        for trade in data['data']:
            ticker = trade['s']
            price = trade['p']
            volume = trade['v']
            now = time.time()
            
            with watchlist_lock:
                if ticker in history_data:
                    history = history_data[ticker]
                    history.append((now, price, volume))
                    
                    while history and (now - history[0][0] > 360):
                        history.popleft()
                    
                    last_1m = [item for item in history if now - item[0] <= 60]
                    prev_5m = [item for item in history if 60 < now - item[0] <= 360]
                    
                    if last_1m and prev_5m:
                        oldest_price_1m = last_1m[0][1]
                        vol_1m = sum(item[2] for item in last_1m)
                        vol_prev_5m_total = sum(item[2] for item in prev_5m)
                        vol_5m_avg_1m = (vol_prev_5m_total / 5.0) if vol_prev_5m_total > 0 else 1.0
                        
                        if price < MAX_PRICE and oldest_price_1m > 0:
                            change_pct = ((price - oldest_price_1m) / oldest_price_1m) * 100
                            vol_spike_ratio = vol_1m / vol_5m_avg_1m
                            
                            if (change_pct >= SURGE_RATIO) and (vol_spike_ratio >= VOL_MULTIPLIER) and (now - last_alert_time[ticker] > ALERT_COOLDOWN):
                                alert_msg = (
                                    f"🚨 *[핵심 테마 바닥권 소형주 폭등!]*\n"
                                    f"• 종목: *{ticker}*\n"
                                    f"• 현재가: *${price:.2f}* (1분간 +{change_pct:.1f}%)\n"
                                    f"• 거래량: *{vol_1m:,}주* (평소 대비 *{vol_spike_ratio:.1f}배* 폭증 🔥)\n"
                                    f"👉 [야후 차트보기](https://finance.yahoo.com/quote/{ticker})"
                                )
                                print(f"[{time.strftime('%H:%M:%S')}] {ticker} 급등 감지!")
                                send_telegram(alert_msg)
                                last_alert_time[ticker] = now

def on_open(ws):
    print(f"⚡ 실시간 웹소켓 감시 시작완료! ($7 미만 바닥권 7대 테마 50선)")
    with watchlist_lock:
        current_list = list(active_watch_list)
    for ticker in current_list:
        ws.send(json.dumps({'type': 'subscribe', 'symbol': ticker}))

def run_websocket():
    while True:
        try:
            ws = websocket.WebSocketApp(
                f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}",
                on_message=on_message,
                on_open=on_open
            )
            ws.run_forever()
        except Exception as e:
            print(f"웹소켓 연결 끊김, 5초 후 재연결: {e}")
            time.sleep(5)

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), BaseHTTPRequestHandler)
    server.serve_forever()

if __name__ == "__main__":
    # 1. 렌더 생존용 서버 구동
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    # 2. 초기 와치리스트 빌드 (30일 50% 폭등 제외 + 7대 테마 우선)
    initial_list = fetch_smart_watchlist()
    with watchlist_lock:
        active_watch_list = initial_list
        for t in active_watch_list:
            history_data[t] = deque()
            last_alert_time[t] = 0

    # 3. 메인 웹소켓 실행
    run_websocket()
