import websocket
import json
import requests
from collections import deque
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==================== [내 정보 입력 영역] ====================
FINNHUB_TOKEN = "dapr4a9r01qqnrhtp010dapr4a9r01qqnrhtp01g"
TELEGRAM_TOKEN = "8926579638:AAGQvhDFLu7mPpM_Jdov4VxQsTYDoVz0TnE"
CHAT_ID = "8818075368"

# AMEX $5 미만 주요 관심 종목 리스트
WATCH_LIST = ["AAAU", "BTG", "LNG", "URG", "UEC", "ASM", "NOG"]

MAX_PRICE = 5.0        # $5 미만
SURGE_RATIO = 2.5      # 1분 내 +2.5% 이상 급등 시
VOL_MULTIPLIER = 5.0   # 직전 5분 평균 대비 1분 거래량 5배 이상 폭발 시
ALERT_COOLDOWN = 120   # 한 종목당 연속 알림 방지 쿨타임 (120초)
# ============================================================

history_data = {ticker: deque() for ticker in WATCH_LIST}
last_alert_time = {ticker: 0 for ticker in WATCH_LIST}

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=2)
    except Exception as e:
        print(f"알림 전송 실패: {e}")

def on_message(ws, message):
    data = json.loads(message)
    if data.get('type') == 'trade':
        for trade in data['data']:
            ticker = trade['s']
            price = trade['p']
            volume = trade['v']
            now = time.time()
            
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
                                f"🚨 *[AMEX 1분 거래량 폭발+급등!]*\n"
                                f"• 종목: *{ticker}*\n"
                                f"• 현재가: *${price:.2f}* (1분간 +{change_pct:.1f}%)\n"
                                f"• 거래량: *{vol_1m:,}주* (평소 대비 *{vol_spike_ratio:.1f}배* 폭증 🔥)\n"
                                f"👉 [야후 차트보기](https://finance.yahoo.com/quote/{ticker})"
                            )
                            print(f"[{time.strftime('%H:%M:%S')}] {ticker} 급등 감지!")
                            send_telegram(alert_msg)
                            last_alert_time[ticker] = now

def on_open(ws):
    print("⚡ AMEX 실시간 웹소켓 감시 시작완료! (거래량 5배 + 1분 2.5% 급등 조건)")
    for ticker in WATCH_LIST:
        ws.send(json.dumps({'type': 'subscribe', 'symbol': ticker}))

def run():
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}",
        on_message=on_message,
        on_open=on_open
    )
    ws.run_forever()

# Render 포트 검사를 속이기 위한 가짜 웹서버 함수
def run_dummy_server():
    server = HTTPServer(('0.0.0.0', 10000), BaseHTTPRequestHandler)
    server.serve_forever()

if __name__ == "__main__":
    # 가짜 서버를 배경에서 실행
    threading.Thread(target=run_dummy_server, daemon=True).start()
    # 주식 감시 로직 실행
    run()
