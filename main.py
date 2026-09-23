import websocket
import json
import requests
from collections import deque
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import random

# ==================== [설정 영역] ====================
FINNHUB_TOKEN = os.environ.get("FINNHUB_API_KEY", "dapr4a9r01qqnrhtp010dapr4a9r01qqnrhtp01g")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8926579638:AAGQvhDFLu7mPpM_Jdov4VxQsTYDoVz0TnE")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8818075368")

MIN_PRICE = 0.1
MAX_PRICE = 7.0        
SURGE_RATIO = 2.5      
VOL_MULTIPLIER = 3.0   
ALERT_COOLDOWN = 120   

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
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[경고] 텔레그램 토큰 또는 Chat ID가 설정되지 않았습니다!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
        if res.status_code != 200:
            print(f"[텔레그램 에러] 응답 코드: {res.status_code}, 내용: {res.text}")
    except Exception as e:
        print(f"알림 전송 실패: {e}")

# ==================== [초효율 스마트 스크리너 (500개 캡 + 잡동사니 차단)] ====================
def fetch_smart_watchlist():
    print(f"[{time.strftime('%H:%M:%S')}] 🔍 [스마트 스크리너] 500개 샘플링 및 잡동사니 차단 필터 가동...")
    
    if not FINNHUB_TOKEN:
        print("[에러] FINNHUB_API_KEY 환경 변수가 설정되지 않았습니다!")
        return ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU"]

    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_TOKEN}"
    try:
        res = requests.get(url, timeout=15)
        symbols_data = res.json()
        if isinstance(symbols_data, dict) and 'error' in symbols_data:
            print(f"[핀허브 API 에러]: {symbols_data['error']}")
            return ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU"]
    except Exception as e:
        print(f"심볼 조회 중 예외 발생: {e}")
        return ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU"]

    # 1. 시장 전체 무작위 셔플 (A~Z 편향 방지)
    random.shuffle(symbols_data)

    prioritized_list = []  # 7대 테마 알짜 종목
    general_list = []      # 일반 안전 소형주

    to_ts = int(time.time())
    from_ts = to_ts - (30 * 86400)

    scanned_count = 0
    max_scan_limit = 500  # 밴 위험 없는 가장 안전한 500개 한계선 설정

    for item in symbols_data:
        if scanned_count >= max_scan_limit:
            break
            
        ticker = item.get('symbol')
        description = item.get('description', '').lower()
        
        # 기본 티커 검증
        if not ticker or '.' in ticker or '^' in ticker or len(ticker) > 5:
            continue
            
        # [잡동사니 원천 차단 필터] ETF, 스팩, 펀드, 우선주, 워런트 등 이름 기반 제외
        exclude_keywords = ['etf', 'fund', 'trust', 'index', 'acquisition', 'blank check', 'preferred', 'warrant', 'notes']
        if any(keyword in description for keyword in exclude_keywords):
            continue  
            
        scanned_count += 1
        
        try:
            # [1단계] 가격 필터 우선 검증 (비용 절약: 범위 안 맞으면 즉시 탈락)
            quote_url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}"
            q_res = requests.get(quote_url, timeout=2).json()
            price = q_res.get('c', 0)
            time.sleep(0.05) # 밴 방지 미세 딜레이
            
            if not price or not (MIN_PRICE <= price < MAX_PRICE):
                continue
                
            # [2단계] 30일 폭등(설거지) 이력 검증
            candle_url = f"https://finnhub.io/api/v1/stock/candle?symbol={ticker}&resolution=D&from={from_ts}&to={to_ts}&token={FINNHUB_TOKEN}"
            c_res = requests.get(candle_url, timeout=2).json()
            time.sleep(0.05)
            
            if c_res.get('s') == 'ok':
                lows = c_res.get('l', [])
                highs = c_res.get('h', [])
                if lows and highs:
                    min_low = min(lows)
                    max_high = max(highs)
                    if min_low > 0 and (max_high - min_low) / min_low >= 0.5:
                        continue  # 30일 내 50% 이상 급등 이력 있으면 탈락

            # [3단계] 테마 분류 (7대 테마 vs 일반 소형주)
            profile_url = f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={FINNHUB_TOKEN}"
            p_res = requests.get(profile_url, timeout=2).json()
            industry = p_res.get('finnhubIndustry', '')
            time.sleep(0.05)
            
            is_favorite = any(sec.lower() in industry.lower() for sec in FAVORITE_SECTORS)
            
            if is_favorite:
                prioritized_list.append(ticker)
            else:
                general_list.append(ticker)
                
            # 50개가 채워지면 탐색 조기 종료
            if len(prioritized_list) + len(general_list) >= 50:
                break
                
        except Exception:
            continue

    # 우선순위 테마 먼저 담고, 모자란 자리는 일반 안전 소형주로 채워서 50개 완성
    combined = prioritized_list + general_list
    final_50 = combined[:50]
    
    # 예비 방어벽
    if len(final_50) < 5:
        final_50 = ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU", "DNN", "UAMY"]

    print(f"[{time.strftime('%H:%M:%S')}] ✨ 최종 선별된 50선 완료 (총 검사한 순수 주식 수: {scanned_count}개): {final_50}")
    
    list_msg = f"📋 *[스마트 와치리스트 50선 (잡동사니 차단 완료)]*\n" + ", ".join(final_50)
    send_telegram(list_msg)
    
    return final_50

# ==================== [웹소켓 실시간 감시 엔진] ====================
def on_message(ws, message):
    try:
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
                                        f"🚨 *[핵심 바닥권 소형주 폭등 감지!]*\n"
                                        f"• 종목: *{ticker}*\n"
                                        f"• 현재가: *${price:.2f}* (1분간 +{change_pct:.1f}%)\n"
                                        f"• 거래량: *{vol_1m:,}주* (평소 대비 *{vol_spike_ratio:.1f}배* 폭증 🔥)\n"
                                        f"👉 [야후 차트보기](https://finance.yahoo.com/quote/{ticker})"
                                    )
                                    print(f"[{time.strftime('%H:%M:%S')}] {ticker} 급등 감지!")
                                    send_telegram(alert_msg)
                                    last_alert_time[ticker] = now
    except Exception as e:
        print(f"메시지 처리 중 에러: {e}")

def on_open(ws):
    print(f"⚡ 실시간 웹소켓 감시 시작완료!")
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

# ==================== [HTTP 가짜 서버 (Render 생존용)] ====================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Quant Bot is running!")
        
    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

if __name__ == "__main__":
    print("🚀 퀀트 모니터링 시스템 부팅 중...")
    
    # 1. 렌더 생존용 서버 구동
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    # 2. 500개 샘플 기반 스마트 스크리너 구동 및 텔레그램 전송
    initial_list = fetch_smart_watchlist()
    with watchlist_lock:
        active_watch_list = initial_list
        for t in active_watch_list:
            history_data[t] = deque()
            last_alert_time[t] = 0

    # 3. 메인 웹소켓 실행
    run_websocket()
