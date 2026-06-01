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

# ----- CONFIGURATION -----
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

# Liste ultra‑volatile (200+ tickers)
VOLATILE_STOCKS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'SQ', 'ROKU', 'ZM', 'CRWD', 'MRNA', 'BIIB', 'PFE',
    'BA', 'AAL', 'CCL', 'NIO', 'PLTR', 'GME', 'AMC', 'BB', 'SPCE',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN',
    'DVAX', 'INO', 'VXRT', 'NVAX', 'BNGO', 'OCGN', 'CEMI', 'IBIO', 'CODX',
    'SNDL', 'TLRY', 'ACB', 'CGC', 'HCMC', 'EEENF', 'AABB', 'ILUS',
    'HMBL', 'TBEV', 'KAVL', 'GTEH', 'HYSR',
    'BABA', 'JD', 'BIDU', 'XPEV', 'LI', 'BILI', 'TME',
    'BBBY', 'KOSS', 'EXPR', 'NAKD', 'CLOV', 'WISH', 'MVIS',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'U', 'PATH', 'PLUG',
    'FCEL', 'BLDP', 'QS', 'CHPT', 'GOEV', 'FSR', 'NKLA',
    'RIDE', 'WKHS', 'HYLN', 'FSLY', 'NET', 'DDOG',
    'ZS', 'SNOW', 'MDB', 'ESTC', 'FROG',
    'RSX', 'GUSH', 'DRIP', 'UCO', 'SCO',
    'MSTR', 'SI', 'HIVE', 'BTBT', 'CAN', 'EQOS'
]
VOLATILE_STOCKS = list(set(VOLATILE_STOCKS))

# ----- OUTILS D'ANALYSE -----
def get_24h_change(ticker):
    """Retourne la variation sur 24 heures et le prix actuel"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period='5d')  # assez pour calculer 24h
        if len(hist) >= 2:
            now = datetime.now(pytz.timezone('US/Eastern'))
            yesterday = now - timedelta(days=1)
            # Prendre le prix de clôture d'hier et le prix actuel
            close_yesterday = hist.loc[hist.index.date == yesterday.date(), 'Close']
            if not close_yesterday.empty:
                prev = close_yesterday.iloc[-1]
                curr = hist['Close'].iloc[-1]
                change = (curr - prev) / prev * 100
                return curr, change
    except:
        pass
    return None, None

def get_technical_momentum_score(ticker):
    """Score pro : 60% technique, 40% momentum"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return None
        close = df['Close'].squeeze()
        vol = df['Volume'].squeeze()

        tech_score = 0
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if 40 <= rsi <= 50: tech_score += 20
        elif 30 <= rsi < 40: tech_score += 15
        elif rsi < 30: tech_score += 10
        elif rsi > 70: tech_score -= 10

        macd = ta.trend.MACD(close)
        if macd.macd().iloc[-1] > macd.macd_signal().iloc[-1]:
            tech_score += 15

        avg_vol = vol.iloc[-20:].mean()
        if vol.iloc[-1] > 1.5 * avg_vol:
            tech_score += 15

        ema10 = ta.trend.EMAIndicator(close, window=10).ema_indicator().iloc[-1]
        if close.iloc[-1] > ema10:
            tech_score += 10

        mom_score = 0
        change_5d = (close.iloc[-1] / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0
        if change_5d > 30:
            mom_score += 25
        elif change_5d > 15:
            mom_score += 18
        elif change_5d > 5:
            mom_score += 10

        avg_vol_5d = vol.iloc[-5:].mean()
        if vol.iloc[-1] > 1.2 * avg_vol_5d:
            mom_score += 15

        total = min(tech_score + mom_score, 100)
        return {
            'score': total,
            'rsi': round(rsi, 1),
            'volume_ratio': round(vol.iloc[-1] / avg_vol, 1) if avg_vol > 0 else 1,
            'trend': 'haussière' if close.iloc[-1] > ema10 else 'neutre',
            'change_5d': round(change_5d, 1)
        }
    except:
        return None

def get_aggressive_analysis(ticker, capital=2000, risk_amount=6):
    analysis = get_technical_momentum_score(ticker)
    if not analysis: return None

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
        atr = current * 0.05

    stop_loss = round(current - 1.0 * atr, 2)
    tp1 = round(current + 2.0 * atr, 2)
    tp2 = round(current + 4.0 * atr, 2)

    if current > stop_loss and risk_amount > 0:
        qty = max(1, int(risk_amount / (current - stop_loss)))
    else:
        qty = 0

    return {
        'ticker': ticker,
        'price': current,
        'score': analysis['score'],
        'rsi': analysis['rsi'],
        'volume_ratio': analysis['volume_ratio'],
        'trend': analysis['trend'],
        'change_5d': analysis['change_5d'],
        'stop_loss': stop_loss,
        'tp1': tp1,
        'tp2': tp2,
        'qty': qty,
        'atr': round(atr, 2)
    }

# ----- RAPPORT MATINAL -----
def morning_report():
    """Envoyer le rapport quotidien à 9h (Paris)"""
    print("📋 Génération du rapport matinal...")
    if not subscribers:
        return

    # Récupérer les actions avec une hausse >30% sur 24h
    gainers = []
    for ticker in VOLATILE_STOCKS:
        curr, chg = get_24h_change(ticker)
        if curr and chg is not None and chg >= 30:
            analysis = get_technical_momentum_score(ticker)
            if analysis and analysis['score'] >= 50:  # on filtre les scores trop faibles
                gainers.append((ticker, curr, chg, analysis))
        time.sleep(0.15)  # ne pas surcharger l'API

    if not gainers:
        # Envoyer un message plus léger s'il n'y a pas de fusée
        for uid in subscribers:
            try:
                bot.send_message(uid, "☀️ *Analyse du jour*\n\nAucune action n'affiche +30% avec un score élevé ce matin.\nSurveillez les prochaines heures avec /explosive.", parse_mode='Markdown')
            except:
                pass
        return

    # Trier par score décroissant
    gainers.sort(key=lambda x: x[3]['score'], reverse=True)
    top5 = gainers[:5]

    msg = "☀️ *ANALYSE DU JOUR – HAUT POTENTIEL (>30%)*\n\n"
    msg += "_Actions ayant explosé sur 24h avec un score technique élevé_\n\n"

    for ticker, price, change, analysis in top5:
        score = analysis['score']
        bars = int(score/10)
        msg += f"🔥 *{ticker}*  |  +{change:.1f}%  |  Score {score}/100\n"
        msg += f"`[{'█'*bars}{'░'*(10-bars)}]`\n"
        a = get_aggressive_analysis(ticker, 2000, 6)  # paramètres par défaut, on peut utiliser user params plus tard
        if a:
            msg += f"🛑 Stop {a['stop_loss']}€ | 🎯 TP1 {a['tp1']}€ | TP2 {a['tp2']}€\n"
            msg += f"🔢 {a['qty']} actions (risque 6€)\n"
        msg += f"📊 Volume {analysis['volume_ratio']}x moy | RSI {analysis['rsi']} | Tendance {analysis['trend']}\n\n"

    msg += "📡 *Sources* : Yahoo Finance (cours, ATR, volumes), Finnhub (sentiment news), NLTK VADER\n"
    msg += "⚠️ Ces analyses sont des estimations basées sur des données historiques. Ne constitue pas un conseil en investissement."

    for uid in subscribers:
        try:
            bot.send_message(uid, msg, parse_mode='Markdown')
        except Exception as e:
            print(f"Erreur envoi rapport à {uid}: {e}")

# ----- COMMANDES TELEGRAM -----
@bot.message_handler(commands=['start'])
def start(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, 
        "🤖 *Trader Pro Assistant activé*\n\n"
        "Commandes essentielles :\n"
        "/explosive – Top 5 des fusées du jour\n"
        "/analyse TICKER – Analyse pro (score, stop, TP)\n"
        "/force – Meilleurs scores techniques\n"
        "/setcapital 2000 – Définir votre capital\n"
        "/setrisk 6 – Risque max par trade\n"
        "/mesparams – Voir vos paramètres\n"
        "/subscribe – Alertes automatiques (8h-21h)\n\n"
        "📅 *Rapport matinal à 9h* avec les actions à +30% et score élevé.", parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def aide(message):
    msg = """
📚 *Aide du Trader Pro*

/explosive – Top 5 des plus gros gagnants du jour
/analyse TICKER – Analyse technique pro, stop, TP, quantité
/force – Top 5 des meilleurs scores techniques (toute la liste)
/setcapital MONTANT – Capital total de votre compte
/setrisk MONTANT – Perte maximale acceptée par trade (€)
/mesparams – Voir capital et risque actuels
/subscribe – Activer les alertes automatiques (8h-21h)
/unsubscribe – Désactiver les alertes
Rapport automatique à 9h (actions >+30% avec bon score)
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['setcapital'])
def set_capital(message):
    try:
        capital = float(message.text.split()[1])
        user_capital[message.chat.id] = capital
        bot.reply_to(message, f"✅ Capital défini à {capital}€")
    except:
        bot.reply_to(message, "❌ Utilisation : /setcapital 2000")

@bot.message_handler(commands=['setrisk'])
def set_risk(message):
    try:
        risk = float(message.text.split()[1])
        user_risk_per_trade[message.chat.id] = risk
        bot.reply_to(message, f"✅ Risque max par trade = {risk}€")
    except:
        bot.reply_to(message, "❌ Utilisation : /setrisk 6")

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

    # Récupérer les plus fortes hausses du jour (méthode précédente)
    changes = []
    for ticker in VOLATILE_STOCKS:
        curr, chg = get_24h_change(ticker)  # on peut aussi utiliser daily change
        if chg and chg > 0:
            changes.append((ticker, curr, chg))
        time.sleep(0.1)
    changes.sort(key=lambda x: x[2], reverse=True)
    top5 = changes[:5]

    msg = "💥 *TOP 5 DES FUSÉES DU JOUR*\n\n"
    for ticker, price, change in top5:
        analysis = get_aggressive_analysis(ticker, capital, risk)
        if analysis:
            score = analysis['score']
            bars = int(score/10)
            msg += f"*{ticker}*  |  +{change:.1f}%  |  Score {score}/100\n"
            msg += f"`[{'█'*bars}{'░'*(10-bars)}]`\n"
            msg += f"🛑 Stop {analysis['stop_loss']}€ | 🎯 TP1 {analysis['tp1']}€ | TP2 {analysis['tp2']}€\n"
            msg += f"🔢 {analysis['qty']} actions (risque {risk}€)\n\n"
        else:
            msg += f"*{ticker}*  |  +{change:.1f}%  |  Analyse impossible\n\n"
    msg += "⚠️ Volatilité extrême. Risque de perte totale."
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse TICKER (ex: /analyse TSLA)")
        return
    cap = user_capital.get(message.chat.id, 2000)
    risk = user_risk_per_trade.get(message.chat.id, 6)
    a = get_aggressive_analysis(ticker, cap, risk)
    if not a:
        bot.reply_to(message, "❌ Analyse impossible (données manquantes).")
        return

    # Commentaire pro
    commentary = f"📈 *{ticker}* | Score Pro : {a['score']}/100\n"
    bars = int(a['score']/10)
    commentary += f"`[{'█'*bars}{'░'*(10-bars)}]`\n\n"
    if a['score'] >= 80:
        commentary += "🔥 *Force : Achat agressif*\n"
    elif a['score'] >= 65:
        commentary += "🟢 *Force : Achat dynamique*\n"
    elif a['score'] >= 50:
        commentary += "🟡 *Force : Neutre / Attendre*\n"
    else:
        commentary += "🔴 *Force : Éviter / Vente*\n"

    commentary += f"▸ Volume {a['volume_ratio']}x la moyenne, RSI à {a['rsi']}, "
    if a['trend'] == 'haussière':
        commentary += "tendance haussière."
    else:
        commentary += "tendance neutre/baissière."
    if a['change_5d'] > 20:
        commentary += f"\n📊 Performance 5 jours : +{a['change_5d']}%"

    msg = commentary + "\n"
    msg += f"💰 Prix actuel : {a['price']}€\n"
    msg += f"🛑 Stop‑loss : {a['stop_loss']}€ (ATR {a['atr']})\n"
    msg += f"🎯 Take‑profit 1 : {a['tp1']}€ (+{round((a['tp1']/a['price']-1)*100,1)}%)\n"
    msg += f"🎯 Take‑profit 2 : {a['tp2']}€ (+{round((a['tp2']/a['price']-1)*100,1)}%)\n"
    msg += f"🔢 Taille recommandée : {a['qty']} actions (risque {risk}€)"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['force'])
def force(message):
    bot.send_chat_action(message.chat.id, 'typing')
    results = []
    for ticker in VOLATILE_STOCKS[:30]:
        analysis = get_technical_momentum_score(ticker)
        if analysis:
            results.append((ticker, analysis['score']))
        time.sleep(0.1)
    results.sort(key=lambda x: x[1], reverse=True)
    msg = "🏆 *TOP SCORES TECHNIQUES (force)*\n\n"
    for ticker, score in results[:5]:
        bars = int(score/10)
        msg += f"*{ticker}*  Score {score}/100\n"
        msg += f"`[{'█'*bars}{'░'*(10-bars)}]`\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "✅ Vous recevrez le rapport matinal (9h) et les alertes automatiques (8h-21h).")

@bot.message_handler(commands=['unsubscribe'])
def unsubscribe(message):
    subscribers.discard(message.chat.id)
    bot.reply_to(message, "❌ Désabonné des rapports et alertes.")

# ----- PLANIFICATEUR -----
def run_scheduler():
    # Rapport quotidien à 9h heure de Paris
    schedule.every().day.at("09:00", "Europe/Paris").do(morning_report)
    # Alertes toutes les 15 minutes (8h-21h gérées dans la fonction)
    schedule.every(15).minutes.do(alert_explosive)

    while True:
        schedule.run_pending()
        time.sleep(30)

def alert_explosive():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21:
        return
    # Même logique que dans /explosive, mais seulement pour changements >20%
    top = []
    for ticker in VOLATILE_STOCKS:
        curr, chg = get_24h_change(ticker)
        if chg and chg >= 20:
            analysis = get_technical_momentum_score(ticker)
            if analysis and analysis['score'] >= 60:
                top.append((ticker, curr, chg, analysis))
        time.sleep(0.1)
    top.sort(key=lambda x: x[3]['score'], reverse=True)
    for ticker, price, change, analysis in top[:3]:
        a = get_aggressive_analysis(ticker, 2000, 6)
        if a:
            msg = f"🚀 *ALERTE FUSÉE* {ticker} +{change:.1f}%\nScore {a['score']}/100\nStop {a['stop_loss']}€ | TP1 {a['tp1']}€ | TP2 {a['tp2']}€"
            for uid in subscribers:
                try:
                    bot.send_message(uid, msg, parse_mode='Markdown')
                except:
                    pass

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
    print(f"✅ Bot Trader Pro avec rapport 9h sur {WEBHOOK_URL}")

    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
