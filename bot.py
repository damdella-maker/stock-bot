import os
import telebot
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import pytz
import schedule
import time
import threading
from datetime import datetime
from flask import Flask, request
import ta
import nltk
from nltk.sentiment import SentimentIntensityAnalyzer
nltk.download('vader_lexicon', quiet=True)

# ----- CONFIG -----
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://your-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
sia = SentimentIntensityAnalyzer()

# Abonnés et paramètres utilisateur
subscribers = set()
user_capital = {}
user_risk_per_trade = {}

# --- LISTE ULTRA-VOLATILE (200+ tickers) ---
VOLATILE_STOCKS = [
    # Technologie / croissance
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'SQ', 'ROKU', 'ZM', 'CRWD', 'MRNA', 'BIIB', 'PFE',
    'BA', 'AAL', 'CCL', 'NIO', 'PLTR', 'GME', 'AMC', 'BB', 'SPCE',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN',
    # Biotech explosives
    'DVAX', 'INO', 'VXRT', 'NVAX', 'BNGO', 'OCGN', 'CEMI', 'IBIO', 'CODX',
    # Penny stocks / small caps volatiles
    'SNDL', 'TLRY', 'ACB', 'CGC', 'HCMC', 'EEENF', 'AABB', 'ILUS',
    'HMBL', 'TBEV', 'KAVL', 'GTEH', 'HYSR',
    # ADR chinoises
    'BABA', 'JD', 'BIDU', 'NIO', 'XPEV', 'LI', 'BILI', 'TME',
    # Mèmes / Reddit
    'BBBY', 'KOSS', 'EXPR', 'NAKD', 'CLOV', 'WISH', 'MVIS',
    # Autres high beta
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'U', 'PATH', 'PLUG',
    'FCEL', 'BLDP', 'QS', 'CHPT', 'GOEV', 'FSR', 'NKLA',
    'LCID', 'RIDE', 'WKHS', 'HYLN', 'FSLY', 'NET', 'DDOG',
    'ZS', 'CRWD', 'SNOW', 'MDB', 'ESTC', 'DDOG', 'FROG',
    # Russie / énergie
    'RSX', 'GUSH', 'DRIP', 'UCO', 'SCO',
    # Crypto actions
    'MSTR', 'SI', 'HIVE', 'BTBT', 'CAN', 'EQOS'
]
# On enlève les doublons éventuels
VOLATILE_STOCKS = list(set(VOLATILE_STOCKS))

# ----- FONCTIONS D'ANALYSE (similaires mais plus agressives) -----
def get_daily_change(ticker):
    """Retourne la variation du jour en % et le prix actuel"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period='2d')
        if len(hist) >= 2:
            prev = hist['Close'].iloc[-2]
            curr = hist['Close'].iloc[-1]
            change = (curr - prev) / prev * 100
            return curr, change
    except:
        pass
    return None, None

def get_top_explosive_stocks():
    """Retourne les 5 actions avec la plus forte hausse aujourd'hui"""
    changes = []
    for ticker in VOLATILE_STOCKS:
        curr, chg = get_daily_change(ticker)
        if chg is not None and chg > 0:  # on ne prend que les hausses
            changes.append((ticker, curr, chg))
        time.sleep(0.1)  # ne pas surcharger l'API
    # Tri par pourcentage descendant
    changes.sort(key=lambda x: x[2], reverse=True)
    return changes[:5]  # top 5

def get_sentiment_score(ticker):
    try:
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        news = requests.get(url).json()
        if not news or 'error' in news: return 50
        scores = []
        for article in news[:5]:
            text = article.get('headline', '') + ' ' + article.get('summary', '')
            scores.append(sia.polarity_scores(text)['compound'])
        avg = np.mean(scores) * 100 if scores else 0
        return max(0, min(100, avg + 50))
    except:
        return 50

def get_fundamental_score(ticker):
    try:
        info = yf.Ticker(ticker).info
        score = 50
        q_eg = info.get('earningsQuarterlyGrowth', 0)
        if q_eg and q_eg > 0.2: score += 15
        rev_g = info.get('revenueGrowth', 0)
        if rev_g and rev_g > 0.2: score += 10
        target = info.get('targetMeanPrice', 0)
        curr = info.get('currentPrice', 0) or info.get('regularMarketPrice', 0)
        if target and curr:
            upside = (target/curr - 1)*100
            if upside > 30: score += 20
            elif upside > 15: score += 10
        return min(score, 100)
    except:
        return 50

def get_technical_score(ticker):
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return 50
        close = df['Close'].squeeze()
        vol = df['Volume'].squeeze()
        score = 50
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if 30 <= rsi <= 50: score += 20
        elif rsi < 30: score += 15
        elif rsi > 70: score -= 10
        macd = ta.trend.MACD(close)
        if macd.macd().iloc[-1] > macd.macd_signal().iloc[-1]: score += 15
        avg_vol = vol.iloc[-20:].mean()
        if vol.iloc[-1] > 1.5 * avg_vol: score += 15
        ema10 = ta.trend.EMAIndicator(close, window=10).ema_indicator().iloc[-1]
        if close.iloc[-1] > ema10: score += 10
        return min(score, 100)
    except:
        return 50

def get_momentum_score(ticker):
    try:
        df = yf.download(ticker, period='5d', progress=False)
        if len(df) < 5: return 50
        close = df['Close'].squeeze()
        change = (close.iloc[-1] / close.iloc[0] - 1) * 100
        score = 50
        if change > 30: score += 25
        elif change > 15: score += 15
        elif change > 5: score += 5
        return min(score, 100)
    except:
        return 50

def get_aggressive_analysis(ticker, capital=2000, risk_amount=6):
    """Analyse swing ultra-agressive (stop 1 ATR, TP1 +10%, TP2 +20%)"""
    try:
        fund = get_fundamental_score(ticker)
        tech = get_technical_score(ticker)
        mom = get_momentum_score(ticker)
        sent = get_sentiment_score(ticker)
        global_score = fund*0.15 + tech*0.40 + mom*0.30 + sent*0.15

        stock = yf.Ticker(ticker)
        hist = stock.history(period='5d')
        if hist.empty: return None
        current = hist['Close'].iloc[-1]

        df_atr = yf.download(ticker, period='1mo', progress=False)
        if not df_atr.empty:
            atr = ta.volatility.AverageTrueRange(
                high=df_atr['High'].squeeze(),
                low=df_atr['Low'].squeeze(),
                close=df_atr['Close'].squeeze(),
                window=14
            ).average_true_range().iloc[-1]
        else:
            atr = current * 0.05  # 5% par défaut

        stop_loss = round(current - 1.0 * atr, 2)
        tp1 = round(current + 2.0 * atr, 2)   # ~10% de hausse si ATR ~5%
        tp2 = round(current + 4.0 * atr, 2)   # ~20%

        # Taille de position pour risquer exactement "risk_amount"
        if current > stop_loss and risk_amount > 0:
            qty = max(1, int(risk_amount / (current - stop_loss)))
        else:
            qty = 0

        return {
            'ticker': ticker,
            'price': current,
            'global_score': round(global_score, 1),
            'fund': fund, 'tech': tech, 'mom': mom, 'sent': sent,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2,
            'qty': qty,
            'atr': round(atr, 2)
        }
    except:
        return None

# ----- COMMANDES TELEGRAM -----
@bot.message_handler(commands=['start'])
def start(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "🔥 Mode ultra-agressif activé !\n/explosive pour les fusées du jour\n/setcapital 2000\n/setrisk 6\n/aide")

@bot.message_handler(commands=['aide'])
def aide(message):
    msg = """
📚 *Commandes*
/explosive - Top 5 des plus gros gagnants du jour (tous scores)
/analyse TICKER - Analyse complète
/setcapital MONTANT
/setrisk MONTANT
/mesparams
/subscribe - Alertes automatiques (8h-21h, si gain >20%)
/unsubscribe
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['setcapital'])
def set_capital(message):
    try:
        capital = float(message.text.split()[1])
        user_capital[message.chat.id] = capital
        bot.reply_to(message, f"✅ Capital = {capital}€")
    except:
        bot.reply_to(message, "❌ /setcapital 2000")

@bot.message_handler(commands=['setrisk'])
def set_risk(message):
    try:
        risk = float(message.text.split()[1])
        user_risk_per_trade[message.chat.id] = risk
        bot.reply_to(message, f"✅ Risque max = {risk}€")
    except:
        bot.reply_to(message, "❌ /setrisk 6")

@bot.message_handler(commands=['mesparams'])
def mesparams(message):
    cap = user_capital.get(message.chat.id, 2000)
    risk = user_risk_per_trade.get(message.chat.id, 6)
    bot.reply_to(message, f"💰 Capital : {cap}€\n⚠️ Risque/trade : {risk}€")

@bot.message_handler(commands=['explosive'])
def explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    capital = user_capital.get(message.chat.id, 2000)
    risk = user_risk_per_trade.get(message.chat.id, 6)

    top_gainers = get_top_explosive_stocks()
    if not top_gainers:
        bot.reply_to(message, "Aucune hausse significative détectée.")
        return

    msg = "💥 *TOP 5 DES FUSÉES DU JOUR*\n\n"
    for ticker, price, change in top_gainers:
        analysis = get_aggressive_analysis(ticker, capital, risk)
        if analysis:
            msg += f"*{ticker}*  |  +{change:.1f}%  |  Prix {price:.2f}€\n"
            msg += f"Score global : {analysis['global_score']}/100\n"
            msg += f"Fund {analysis['fund']} | Tech {analysis['tech']} | Mom {analysis['mom']} | Sent {analysis['sent']}\n"
            msg += f"Stop : {analysis['stop_loss']}€  |  TP1 : {analysis['tp1']}€  |  TP2 : {analysis['tp2']}€\n"
            msg += f"Qté recommandée : {analysis['qty']} actions (risque {risk}€)\n\n"
        else:
            msg += f"*{ticker}*  |  +{change:.1f}%  |  Analyse impossible\n\n"
    msg += "⚠️ Ces actions sont extrêmement volatiles. Risque de perte totale."
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse TSLA")
        return
    cap = user_capital.get(message.chat.id, 2000)
    risk = user_risk_per_trade.get(message.chat.id, 6)
    a = get_aggressive_analysis(ticker, cap, risk)
    if not a:
        bot.reply_to(message, "Impossible d'analyser cette action.")
        return
    msg = f"📊 *{ticker}* (score {a['global_score']}/100)\n\n"
    msg += f"💰 {a['price']}€\n"
    msg += f"Fund {a['fund']} | Tech {a['tech']} | Mom {a['mom']} | Sent {a['sent']}\n\n"
    msg += f"🛑 Stop : {a['stop_loss']}€ (ATR {a['atr']})\n"
    msg += f"🎯 TP1 : {a['tp1']}€ | TP2 : {a['tp2']}€\n"
    msg += f"🔢 {a['qty']} actions (risque {risk}€)"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "✅ Alertes activées pour les fusées >20%.")

@bot.message_handler(commands=['unsubscribe'])
def unsubscribe(message):
    subscribers.discard(message.chat.id)
    bot.reply_to(message, "❌ Alertes désactivées.")

# ----- ALERTES AUTO -----
def alert_explosive():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21:
        return
    top = get_top_explosive_stocks()
    for ticker, price, change in top:
        if change >= 20:  # alerte seulement si +20% ou plus
            a = get_aggressive_analysis(ticker, 2000, 6)
            if a and a['global_score'] >= 60:
                msg = f"🚀 *ALERTE FUSÉE* {ticker} +{change:.1f}%\n"
                msg += f"Score {a['global_score']}/100 | Stop {a['stop_loss']}€ | TP1 {a['tp1']}€"
                for uid in subscribers:
                    try:
                        bot.send_message(uid, msg, parse_mode='Markdown')
                    except:
                        pass
        time.sleep(0.3)

def run_scheduler():
    schedule.every(15).minutes.do(alert_explosive)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ----- FLASK -----
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'ok', 200
    return 'bad request', 400

if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"Bot ultra-agressif sur {WEBHOOK_URL}")

    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
