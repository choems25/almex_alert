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

# ==================== [429 방어벽 포함 안전 API 요청 함수] ====================
def safe_get(url, max_retries=3):
    """핀허브 429 에러(Rate Limit) 방어를 위한 안전 요청 래퍼"""
    for attempt in range(max_retries):
        try:
            res = requests.get(url, timeout=3)
            if res.status_code == 429:
                print(f"[⚠️ 경고] 핀허브 API 호출 제한(429) 도달! 10초간 숨 고르기 중...")
                time.sleep(10)
                continue
            return res
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(2)
    return None

# ==================== [스마트 스크리너 (장외주식/ETF/스팩 완벽 차단 + 35개 미만 시 1분 대기)] ====================
def fetch_smart_watchlist():
    max_attempts = 3  # 최대 재시도 횟수
    final_50 = []
    
    for attempt in range(1, max_attempts + 1):
        print(f"[{time.strftime('%H:%M:%S')}] 🔍 [스마트 스크리너] 시도 #{attempt} (500개 샘플링 및 잡주/장외주식 차단 가동)...")
        
        if not FINNHUB_TOKEN:
            print("[에러] FINNHUB_API_KEY 환경 변수가 설정되지 않았습니다!")
            return ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU"]

        url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_TOKEN}"
        try:
            res = safe_get(url)
            if not res:
                time.sleep(10)
                continue
            symbols_data = res.json()
            if isinstance(symbols_data, dict) and 'error' in symbols_data:
                print(f"[핀허브 API 에러]: {symbols_data['error']}")
                time.sleep(10)
                continue
        except Exception as e:
            print(f"심볼 조회 중 예외 발생: {e}")
            time.sleep(10)
            continue

        # 1. 시장 전체 무작위 셔플
        random.shuffle(symbols_data)

        prioritized_list = []
        general_list = []

        to_ts = int(time.time())
        from_ts = to_ts - (30 * 86400)

        scanned_count = 0
        max_scan_limit = 500

        for item in symbols_data:
            if scanned_count >= max_scan_limit:
                break
                
            ticker = item.get('symbol')
            description = item.get('description', '').lower()
            
            if not ticker or '.' in ticker or '^' in ticker or len(ticker) > 5:
                continue
                
            # [잡동사니 + 장외주식(OTC) 완벽 차단 필터]
            exclude_keywords = [
                'etf', 'fund', 'trust', 'index', 'acquisition', 'blank check', 
                'preferred', 'warrant', 'notes', 'otc', 'pink', 'over-the-counter'
            ]
            if any(keyword in description for keyword in exclude_keywords):
                continue  
                
            scanned_count += 1
            
            try:
                # [1단계] 가격 필터
                quote_url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_TOKEN}"
                q_res = safe_get(quote_url)
                if not q_res:
                    continue
                price = q_res.json().get('c', 0)
                time.sleep(0.06)
                
                if not price or not (MIN_PRICE <= price < MAX_PRICE):
                    continue
                    
                # [2단계] 30일 폭등(설거지) 이력 검증
                candle_url = f"https://finnhub.io/api/v1/stock/candle?symbol={ticker}&resolution=D&from={from_ts}&to={to_ts}&token={FINNHUB_TOKEN}"
                c_res = safe_get(candle_url)
                if c_res:
                    c_data = c_res.json()
                    if c_data.get('s') == 'ok':
                        lows = c_data.get('l', [])
                        highs = c_data.get('h', [])
                        if lows and highs:
                            min_low = min(lows)
                            max_high = max(highs)
                            if min_low > 0 and (max_high - min_low) / min_low >= 0.5:
                                continue
                time.sleep(0.06)

                # [3단계] 테마 분류
                profile_url = f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={FINNHUB_TOKEN}"
                p_res = safe_get(profile_url)
                if p_res:
                    industry = p_res.json().get('finnhubIndustry', '')
                    is_favorite = any(sec.lower() in industry.lower() for sec in FAVORITE_SECTORS)
                    
                    if is_favorite:
                        prioritized_list.append(ticker)
                    else:
                        general_list.append(ticker)
                time.sleep(0.06)
                
                if len(prioritized_list) + len(general_list) >= 50:
                    break
                    
            except Exception:
                continue

        combined = prioritized_list + general_list
        final_50 = combined[:50]
        
        # 🎯 조건 체크: 35개 이상 모았는가?
        if len(final_50) >= 35:
            print(f"[{time.strftime('%H:%M:%S')}] ✨ 충분한 우량 종목 확보 성공! ({len(final_50)}개 수집 완료)")
            break
        else:
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️ 수집된 종목이 {len(final_50)}개로 너무 적습니다 (35개 미만).")
            if attempt < max_attempts:
                print(f"[{time.strftime('%H:%M:%S')}] ⏱️ 핀허브 보호 및 재정비를 위해 **1분간 안전하게 휴식** 후 처음부터 다시 시도합니다...")
                time.sleep(60) # 1분 대기
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 🚨 최대 재시도 횟수({max_attempts}회) 도달. 현재 확보된 {len(final_50)}개로 모니터링을 시작합니다.")

    # 최후의 방어벽
    if len(final_50) < 5:
        final_50 = ["IPDN", "BTG", "LNG", "URG", "UEC", "ASM", "NOG", "AAAU", "DNN", "UAMY"]

    print(f"[{time.strftime('%H:%M:%S')}] 📋 최종 확정된 감시 대상: {final_50}")
    
    list_msg = f"📋 *[스마트 와치리스트 확정 완료 (장외주식 제외)]*\n" + ", ".join(final_50)
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
    
    # 2. 스마트 스크리너 구동 (장외주식 차단 및 35개 미만 시 1분 대기 재시도 포함)
    initial_list = fetch_smart_watchlist()
    with watchlist_lock:
        active_watch_list = initial_list
        for t in active_watch_list:
            history_data[t] = deque()
            last_alert_time[t] = 0

    # 3. 메인 웹소켓 실행
    run_websocket()
