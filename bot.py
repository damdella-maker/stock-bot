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
from datetime import datetime, timedelta
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

# Abonnés aux alertes, paramètres utilisateur
subscribers = set()
user_capital = {}
user_risk_per_trade = {}

# Liste d'actions agressives (volatiles) pour le scan
AGGRESSIVE_STOCKS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'SQ', 'ROKU', 'ZM', 'CRWD', 'MRNA', 'BIIB', 'PFE',
    'BA', 'AAL', 'CCL', 'NIO', 'PLTR', 'GME', 'AMC', 'BB', 'SPCE',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN'
]

# ----- FONCTIONS D'ANALYSE -----
def get_top_gainers():
    """Retourne la liste des actions de AGGRESSIVE_STOCKS avec une variation > 3% aujourd'hui"""
    gainers = []
    for ticker in AGGRESSIVE_STOCKS:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period='2d')
            if len(hist) >= 2:
                yesterday_close = hist['Close'].iloc[-2]
                current_price = hist['Close'].iloc[-1]
                change = (current_price - yesterday_close) / yesterday_close * 100
                if change >= 3.0:
                    gainers.append((ticker, current_price, change))
        except:
            pass
        time.sleep(0.2)  # respect API
    return gainers

def get_sentiment_score(ticker):
    """Analyse du sentiment via news Finnhub (mots-clés)"""
    try:
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        news = requests.get(url).json()
        if not news or 'error' in news:
            return 50
        scores = []
        for article in news[:5]:
            headline = article.get('headline', '')
            summary = article.get('summary', '')
            text = headline + ' ' + summary
            scores.append(sia.polarity_scores(text)['compound'])
        if scores:
            avg_score = np.mean(scores) * 100
            return max(0, min(100, avg_score + 50))
        return 50
    except:
        return 50

def get_fundamental_score(ticker):
    """Score fondamental rapide pour swing (croissance récente)"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        score = 50
        # Croissance trimestrielle
        q_earnings_growth = info.get('earningsQuarterlyGrowth', 0)
        if q_earnings_growth and q_earnings_growth > 0.2: score += 15
        elif q_earnings_growth and q_earnings_growth > 0.1: score += 8

        # Revenue growth
        rev_growth = info.get('revenueGrowth', 0)
        if rev_growth and rev_growth > 0.2: score += 10
        elif rev_growth and rev_growth > 0.1: score += 5

        # Analyst target (potentiel)
        target_mean = info.get('targetMeanPrice', 0)
        current = info.get('currentPrice', 0) or info.get('regularMarketPrice', 0)
        if target_mean and current:
            upside = (target_mean / current - 1) * 100
            if upside > 20: score += 15
            elif upside > 10: score += 10
            elif upside > 5: score += 5

        return min(score, 100)
    except:
        return 50

def get_technical_score(ticker):
    """Score technique orienté court terme (RSI, MACD, volume, support)"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return 50
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()

        score = 50
        # RSI
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if 30 <= rsi <= 50:
            score += 15  # zone d'achat
        elif rsi < 30:
            score += 10  # survente
        elif rsi > 70:
            score -= 10  # surachat

        # MACD
        macd = ta.trend.MACD(close)
        if macd.macd().iloc[-1] > macd.macd_signal().iloc[-1]:
            score += 15

        # Volume explosion
        avg_vol = volume.iloc[-20:].mean()
        if volume.iloc[-1] > 1.5 * avg_vol:
            score += 15

        # Force de tendance (ADX)
        adx = ta.trend.ADXIndicator(df['High'].squeeze(), df['Low'].squeeze(), close, window=14)
        if adx.adx().iloc[-1] > 25:
            score += 10

        # Prix au-dessus EMA10
        ema10 = ta.trend.EMAIndicator(close, window=10).ema_indicator().iloc[-1]
        if close.iloc[-1] > ema10:
            score += 10

        return min(score, 100)
    except:
        return 50

def get_momentum_score(ticker):
    """Momentum récent (5 jours, accélération)"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if len(df) < 10: return 50
        close = df['Close'].squeeze()
        score = 50
        # Performance sur 5 jours
        change_5d = (close.iloc[-1] / close.iloc[-5] - 1) * 100
        if change_5d > 10: score += 25
        elif change_5d > 5: score += 15
        elif change_5d > 2: score += 5

        # Volume récent
        avg_vol_5d = df['Volume'].iloc[-5:].mean()
        if df['Volume'].iloc[-1] > 1.2 * avg_vol_5d: score += 10

        return min(score, 100)
    except:
        return 50

def get_swing_analysis(ticker, capital=2000, risk_amount=6):
    """Analyse complète swing trading"""
    try:
        fund = get_fundamental_score(ticker)
        tech = get_technical_score(ticker)
        mom = get_momentum_score(ticker)
        sent = get_sentiment_score(ticker)

        global_score = fund * 0.20 + tech * 0.40 + mom * 0.30 + sent * 0.10

        # Prix actuel et stop/tp
        stock = yf.Ticker(ticker)
        hist = stock.history(period='5d')
        if hist.empty:
            return None
        current_price = hist['Close'].iloc[-1]

        # Calcul ATR (14j)
        df_atr = yf.download(ticker, period='1mo', progress=False)
        if not df_atr.empty:
            atr = ta.volatility.AverageTrueRange(
                high=df_atr['High'].squeeze(),
                low=df_atr['Low'].squeeze(),
                close=df_atr['Close'].squeeze(),
                window=14
            ).average_true_range().iloc[-1]
        else:
            atr = current_price * 0.03

        stop_loss = round(current_price - 1.5 * atr, 2)
        tp1 = round(current_price + 1.0 * atr, 2)  # +5% environ
        tp2 = round(current_price + 2.0 * atr, 2)  # +10% environ

        # Taille de position basée sur le risque (6€)
        if current_price > stop_loss and risk_amount > 0:
            qty = int(risk_amount / (current_price - stop_loss))
        else:
            qty = 0

        return {
            'ticker': ticker,
            'price': current_price,
            'global_score': round(global_score, 1),
            'fund': fund,
            'tech': tech,
            'mom': mom,
            'sent': sent,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2,
            'qty': qty,
            'atr': round(atr, 2)
        }
    except:
        return None

# ----- COMMANDES -----
@bot.message_handler(commands=['start'])
def start(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "🚀 Bot swing trading activé !\n/scan pour les top gainers\n/setcapital 2000\n/setrisk 6\n/aide")

@bot.message_handler(commands=['aide'])
def aide(message):
    msg = """
📚 *Commandes*
/scan - Scanner les top gainers du jour
/analyse TICKER - Analyse complète
/top - Watchlist agressive
/setcapital MONTANT - Capital total
/setrisk MONTANT - Perte max par trade (€)
/mesparams - Voir capital/risque
/subscribe - Alertes auto (8h-21h)
/unsubscribe - Désactiver alertes
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

@bot.message_handler(commands=['scan'])
def scan(message):
    bot.send_chat_action(message.chat.id, 'typing')
    capital = user_capital.get(message.chat.id, 2000)
    risk = user_risk_per_trade.get(message.chat.id, 6)

    gainers = get_top_gainers()
    if not gainers:
        bot.reply_to(message, "Aucun top gainer > 3% pour le moment.")
        return

    results = []
    for ticker, price, change in gainers:
        analysis = get_swing_analysis(ticker, capital, risk)
        if analysis:
            results.append((change, analysis))
        time.sleep(0.5)

    results.sort(key=lambda x: x[1]['global_score'], reverse=True)
    msg = "🔥 *TOP GAINERS (swing trading)*\n\n"
    for i, (_, a) in enumerate(results[:5]):
        msg += f"*{a['ticker']}* (+{a['price']:.2f}€, score {a['global_score']}/100)\n"
        msg += f"• Fond. {a['fund']} | Tech. {a['tech']} | Mom. {a['mom']} | Sent. {a['sent']}\n"
        msg += f"• Stop : {a['stop_loss']}€ | TP1 : {a['tp1']}€ | TP2 : {a['tp2']}€\n"
        msg += f"• Qté recommandée : {a['qty']} actions (risque {risk}€)\n\n"
    msg += "/analyse TICKER pour plus de détails."
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    capital = user_capital.get(message.chat.id, 2000)
    risk = user_risk_per_trade.get(message.chat.id, 6)
    a = get_swing_analysis(ticker, capital, risk)
    if not a:
        bot.reply_to(message, "Impossible d'analyser cette action.")
        return
    msg = f"📊 *{ticker}* (score global {a['global_score']}/100)\n\n"
    msg += f"💰 Prix : {a['price']}€\n"
    msg += f"📈 Fond. {a['fund']} | Tech. {a['tech']} | Mom. {a['mom']} | Sent. {a['sent']}\n\n"
    msg += f"🛑 Stop-loss : {a['stop_loss']}€ (ATR {a['atr']})\n"
    msg += f"🎯 TP1 : {a['tp1']}€ | TP2 : {a['tp2']}€\n"
    msg += f"🔢 Taille : {a['qty']} actions (risque {risk}€)\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['top'])
def top(message):
    # Utilise la liste agressive
    bot.send_chat_action(message.chat.id, 'typing')
    results = []
    for ticker in AGGRESSIVE_STOCKS[:15]:
        try:
            a = get_swing_analysis(ticker, 2000, 6)
            if a:
                results.append(a)
        except:
            pass
    results.sort(key=lambda x: x['global_score'], reverse=True)
    msg = "📈 *Watchlist agressive*\n\n"
    for a in results[:5]:
        msg += f"*{a['ticker']}* : {a['global_score']}/100\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "✅ Alertes activées (8h-21h).")

@bot.message_handler(commands=['unsubscribe'])
def unsubscribe(message):
    subscribers.discard(message.chat.id)
    bot.reply_to(message, "❌ Alertes désactivées.")

# ----- ALERTES AUTOMATIQUES -----
def alert_scan():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21:
        return
    gainers = get_top_gainers()
    for ticker, price, change in gainers:
        a = get_swing_analysis(ticker, 2000, 6)
        if a and a['global_score'] >= 75:
            msg = f"🔔 *ALERTE SWING* {ticker} (score {a['global_score']}/100)\n"
            msg += f"Prix : {a['price']}€ | Stop : {a['stop_loss']}€ | TP1 : {a['tp1']}€"
            for uid in subscribers:
                try:
                    bot.send_message(uid, msg, parse_mode='Markdown')
                except:
                    pass
        time.sleep(0.5)

def run_scheduler():
    schedule.every(15).minutes.do(alert_scan)
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

# ----- MAIN -----
if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"Bot swing sur {WEBHOOK_URL}")

    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
