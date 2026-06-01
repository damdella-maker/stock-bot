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

# ----- CONFIGURATION -----
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID')  # optionnel
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://your-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ----- DONNÉES UTILISATEURS (stockées en mémoire) -----
subscribers = set()          # chat_ids abonnés aux alertes
user_capital = {}            # capital par utilisateur
user_risk_per_trade = {}     # risque max par trade (€)

# ----- FONCTIONS D'ANALYSE -----
def get_fundamental_score(ticker):
    """Score fondamental basé sur les données yfinance"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        score = 50

        # Croissance du CA
        rev_growth = info.get('revenueGrowth', 0)
        if rev_growth and rev_growth > 0.2: score += 15
        elif rev_growth and rev_growth > 0.1: score += 8

        # ROE
        roe = info.get('returnOnEquity', 0)
        if roe and roe > 0.2: score += 12
        elif roe and roe > 0.1: score += 6

        # Marge nette
        margins = info.get('profitMargins', 0)
        if margins and margins > 0.15: score += 10
        elif margins and margins > 0.05: score += 5

        # Ratio dette/equity
        de = info.get('debtToEquity', 100)
        if de and de < 50: score += 10
        elif de and de < 100: score += 5

        # PEG ratio
        peg = info.get('pegRatio', None)
        if peg and peg < 1.5: score += 8
        elif peg and peg < 2.5: score += 4

        return min(score, 100)
    except:
        return 50

def get_technical_score(ticker):
    """Score technique basé sur RSI, MACD, EMA"""
    try:
        df = yf.download(ticker, period='3mo', progress=False)
        if df.empty: return 50
        close = df['Close'].squeeze()
        if close.empty: return 50

        score = 50

        # RSI
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if 40 <= rsi <= 60: score += 12
        elif 30 <= rsi <= 70: score += 8
        elif rsi < 30: score += 10  # survendu
        elif rsi > 70: score -= 5   # suracheté

        # MACD
        macd = ta.trend.MACD(close)
        macd_line = macd.macd().iloc[-1]
        signal_line = macd.macd_signal().iloc[-1]
        if macd_line > signal_line: score += 10

        # Tendance EMA 20/50
        ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator().iloc[-1]
        current = close.iloc[-1]
        if current > ema20 > ema50: score += 15
        elif current > ema50: score += 5

        return min(score, 100)
    except:
        return 50

def get_momentum_score(ticker):
    """Score momentum : variation récente, volume"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return 50
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()

        score = 50
        # Variation sur 1 semaine
        if len(close) >= 5:
            change_5d = (close.iloc[-1] / close.iloc[-5] - 1) * 100
            if change_5d > 5: score += 20
            elif change_5d > 2: score += 10

        # Volume récent vs moyenne
        if len(volume) >= 20:
            avg_vol = volume.iloc[-20:].mean()
            last_vol = volume.iloc[-1]
            if last_vol > 1.5 * avg_vol: score += 10

        return min(score, 100)
    except:
        return 50

def get_global_score(ticker):
    """Score global pondéré"""
    fund = get_fundamental_score(ticker)
    tech = get_technical_score(ticker)
    mom = get_momentum_score(ticker)
    # Pondérations
    global_score = fund * 0.40 + tech * 0.30 + mom * 0.20  # le reste pour sentiment plus tard
    return round(global_score, 1), {'fundamental': fund, 'technical': tech, 'momentum': mom}

def get_risk_params(ticker, capital, risk_amount):
    """Calcule stop-loss, take-profit, taille de position"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return None
        close = df['Close'].squeeze()
        atr = ta.volatility.AverageTrueRange(high=df['High'].squeeze(),
                                             low=df['Low'].squeeze(),
                                             close=close, window=14).average_true_range().iloc[-1]
        current_price = close.iloc[-1]
        # Stop-loss basé sur 2 ATR en dessous
        stop_loss = current_price - 2 * atr
        # Take profits
        tp1 = current_price + 1.5 * atr
        tp2 = current_price + 3 * atr
        # Taille de position : risque / (entrée - stop) -> nombre d'actions
        if current_price > stop_loss and risk_amount > 0:
            qty = int(risk_amount / (current_price - stop_loss))
        else:
            qty = 0
        return {
            'current_price': current_price,
            'atr': round(atr, 2),
            'stop_loss': round(stop_loss, 2),
            'tp1': round(tp1, 2),
            'tp2': round(tp2, 2),
            'quantity': qty
        }
    except:
        return None

# ----- COMMANDES TELEGRAM -----
@bot.message_handler(commands=['start'])
def start(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "🚀 Bot professionnel activé !\nUtilisez /aide pour voir les commandes.\nVous êtes maintenant abonné aux alertes automatiques (8h-21h, toutes les 15 min).")

@bot.message_handler(commands=['aide'])
def aide(message):
    msg = """
📚 *Commandes disponibles*
/analyse TICKER - Analyse complète (score, stop-loss)
/top - Top opportunités du moment
/marché - Indices en direct
/subscribe - S'abonner aux alertes automatiques
/unsubscribe - Se désabonner
/setcapital MONTANT - Définir votre capital (ex: /setcapital 2000)
/setrisk MONTANT - Risque max par trade en € (ex: /setrisk 6)
/mesparams - Voir capital et risque actuels
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "✅ Vous êtes abonné aux alertes automatiques (8h-21h, toutes les 15 min).")

@bot.message_handler(commands=['unsubscribe'])
def unsubscribe(message):
    subscribers.discard(message.chat.id)
    bot.reply_to(message, "❌ Vous êtes désabonné des alertes.")

@bot.message_handler(commands=['setcapital'])
def set_capital(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "❌ Utilisation : /setcapital 2000")
            return
        capital = float(parts[1])
        user_capital[message.chat.id] = capital
        bot.reply_to(message, f"✅ Capital défini à {capital}€")
    except:
        bot.reply_to(message, "❌ Montant invalide.")

@bot.message_handler(commands=['setrisk'])
def set_risk(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "❌ Utilisation : /setrisk 6")
            return
        risk = float(parts[1])
        user_risk_per_trade[message.chat.id] = risk
        bot.reply_to(message, f"✅ Risque max par trade défini à {risk}€")
    except:
        bot.reply_to(message, "❌ Montant invalide.")

@bot.message_handler(commands=['mesparams'])
def show_params(message):
    cap = user_capital.get(message.chat.id, 'Non défini')
    risk = user_risk_per_trade.get(message.chat.id, 'Non défini')
    bot.reply_to(message, f"💰 Capital : {cap}€\n⚠️ Risque/trade : {risk}€")

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Utilisation : /analyse TICKER\nEx : /analyse AAPL")
            return
        ticker = parts[1].upper()
        bot.send_chat_action(message.chat.id, 'typing')

        # Scores
        global_score, details = get_global_score(ticker)
        fund = details['fundamental']
        tech = details['technical']
        mom = details['momentum']

        # Prix et variation
        stock = yf.Ticker(ticker)
        hist = stock.history(period='5d')
        if hist.empty:
            bot.reply_to(message, "❌ Données indisponibles.")
            return
        current_price = hist['Close'].iloc[-1]
        prev_close = hist['Close'].iloc[-2] if len(hist) >= 2 else current_price
        change = (current_price - prev_close) / prev_close * 100

        # Paramètres de risque
        cap = user_capital.get(message.chat.id, 2000)
        risk_amount = user_risk_per_trade.get(message.chat.id, 6)
        risk_info = get_risk_params(ticker, cap, risk_amount)

        # Message
        msg = f"📊 *ANALYSE {ticker}*\n"
        msg += f"💰 Prix : ${current_price:.2f} ({change:+.2f}%)\n\n"
        msg += f"⭐ *Score global : {global_score}/100*\n"
        msg += f"• Fondamental : {fund}/100\n"
        msg += f"• Technique : {tech}/100\n"
        msg += f"• Momentum : {mom}/100\n\n"

        if risk_info:
            msg += "🛡️ *Gestion du risque*\n"
            msg += f"• Stop-loss : ${risk_info['stop_loss']}\n"
            msg += f"• Take-profit 1 : ${risk_info['tp1']}\n"
            msg += f"• Take-profit 2 : ${risk_info['tp2']}\n"
            msg += f"• Taille de position : {risk_info['quantity']} actions (risque {risk_amount}€)\n"
        else:
            msg += "⚠️ Impossible de calculer les niveaux de risque.\n"

        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Erreur : {str(e)}")

@bot.message_handler(commands=['top'])
def top(message):
    bot.send_chat_action(message.chat.id, 'typing')
    watchlist = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'JPM', 'V', 'WMT']
    results = []
    for ticker in watchlist:
        try:
            score, _ = get_global_score(ticker)
            stock = yf.Ticker(ticker)
            hist = stock.history(period='2d')
            if hist.empty: continue
            price = hist['Close'].iloc[-1]
            prev = hist['Close'].iloc[-2] if len(hist) >= 2 else price
            change = (price - prev) / prev * 100
            results.append((ticker, score, price, change))
        except:
            pass
        time.sleep(0.3)
    results.sort(key=lambda x: x[1], reverse=True)
    msg = "🏆 *TOP OPPORTUNITÉS*\n\n"
    for i, (ticker, score, price, change) in enumerate(results[:5], 1):
        emoji = "🟢" if change > 0 else "🔴"
        msg += f"{i}. {emoji} *{ticker}* : {score}/100\n   ${price:.2f} ({change:+.1f}%)\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marché'])
def marche(message):
    indices = {'SPY': 'S&P 500', 'QQQ': 'Nasdaq', 'DIA': 'Dow Jones'}
    msg = "📈 *MARCHÉ*\n\n"
    for ticker, name in indices.items():
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period='2d')
            if hist.empty: continue
            price = hist['Close'].iloc[-1]
            prev = hist['Close'].iloc[-2] if len(hist) >= 2 else price
            change = (price - prev) / prev * 100
            emoji = "🟢" if change > 0 else "🔴"
            msg += f"{emoji} *{name}* : ${price:.2f} ({change:+.1f}%)\n"
        except:
            pass
    bot.reply_to(message, msg, parse_mode='Markdown')

# ----- SYSTÈME DE NOTIFICATIONS AUTOMATIQUES -----
def send_alerts():
    """Scanne la watchlist et envoie une alerte si score > 80"""
    if not subscribers:
        return
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21:
        return  # hors horaires
    watchlist = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA']
    opportunities = []
    for ticker in watchlist:
        try:
            score, _ = get_global_score(ticker)
            if score >= 80:
                opportunities.append(ticker)
        except:
            pass
        time.sleep(0.3)
    if opportunities:
        msg = "🔔 *ALERTE OPPORTUNITÉS*\n\n"
        for t in opportunities:
            msg += f"⭐ {t} (score ≥ 80)\n"
        msg += "\nFaites /analyse pour plus de détails."
        for chat_id in subscribers:
            try:
                bot.send_message(chat_id, msg, parse_mode='Markdown')
            except:
                pass

def keep_alive():
    """Ping l'URL du service pour éviter l'endormissement de Render"""
    while True:
        time.sleep(600)  # 10 minutes
        try:
            requests.get(WEBHOOK_URL)
        except:
            pass

def run_scheduler():
    schedule.every(15).minutes.do(send_alerts)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ----- FLASK ROUTE -----
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'ok', 200
    return 'bad request', 400

# ----- DÉMARRAGE -----
if __name__ == '__main__':
    # Suppression ancien webhook et mise en place
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"🤖 Bot prêt sur {WEBHOOK_URL}")

    # Thread du scheduler
    threading.Thread(target=run_scheduler, daemon=True).start()
    # Thread keep-alive
    threading.Thread(target=keep_alive, daemon=True).start()
    # Lancement Flask
    app.run(host='0.0.0.0', port=PORT)
