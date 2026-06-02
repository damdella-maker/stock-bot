import os
import telebot
import requests
import time
import threading
from flask import Flask, request
from datetime import datetime
import schedule

# Configuration
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# Stockage simple
subscribers = set()

# ===== COMMANDES SIMPLES =====
@bot.message_handler(commands=['start'])
def start(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, """🚀 *Bot Trading Pro - Actif*

*Commandes disponibles :*
📊 /analyse TICKER - Analyse une action
💥 /explosive - Top hausses du jour
📈 /marché - Indices boursiers
📋 /aide - Toutes les commandes

✅ Bot connecté et prêt !""", parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def aide(message):
    bot.reply_to(message, """
📚 *Commandes :*
/analyse AAPL
/explosive
/marché
/start
""", parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Utilisation : /analyse AAPL")
            return
        ticker = parts[1].upper()
        
        # Message d'attente
        bot.send_chat_action(message.chat.id, 'typing')
        
        # Données Finnhub
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        data = requests.get(url).json()
        
        if 'c' not in data or data['c'] == 0:
            bot.reply_to(message, f"❌ Action {ticker} non trouvée")
            return
        
        price = data['c']
        previous = data.get('pc', price)
        change = ((price - previous) / previous) * 100 if previous else 0
        
        # Message simple
        emoji = "🟢" if change > 0 else "🔴"
        msg = f"""
📊 *Analyse de {ticker}*
━━━━━━━━━━━━━━━━━━
💰 Prix actuel : ${price:.2f}
{emoji} Variation : {change:+.2f}%
📈 Plus haut : ${data.get('h', 'N/A')}
📉 Plus bas : ${data.get('l', 'N/A')}
━━━━━━━━━━━━━━━━━━
        """
        bot.reply_to(message, msg, parse_mode='Markdown')
        
    except Exception as e:
        bot.reply_to(message, f"❌ Erreur : {str(e)}")

@bot.message_handler(commands=['marché'])
def marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    indices = {'SPY': 'S&P 500', 'QQQ': 'Nasdaq', 'DIA': 'Dow Jones'}
    msg = "📈 *Indices Boursiers*\n\n"
    
    for ticker, name in indices.items():
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
            data = requests.get(url).json()
            if 'c' in data and data['c'] > 0:
                price = data['c']
                previous = data.get('pc', price)
                change = ((price - previous) / previous) * 100 if previous else 0
                emoji = "🟢" if change > 0 else "🔴"
                msg += f"{emoji} *{name}* : ${price:.2f} ({change:+.2f}%)\n"
        except:
            pass
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    stocks = ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX', 'AMZN', 'PLTR']
    results = []
    
    for ticker in stocks:
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
            data = requests.get(url).json()
            if 'c' in data and data['c'] > 0:
                price = data['c']
                previous = data.get('pc', price)
                change = ((price - previous) / previous) * 100 if previous else 0
                if change > 0:
                    results.append((ticker, price, change))
        except:
            pass
        time.sleep(0.3)
    
    results.sort(key=lambda x: x[2], reverse=True)
    
    if not results:
        bot.reply_to(message, "Aucune hausse détectée pour le moment.")
        return
    
    msg = "💥 *TOP HAUSSES DU JOUR*\n\n"
    for i, (ticker, price, change) in enumerate(results[:5], 1):
        emoji = "🔥" if change > 5 else "🟢"
        msg += f"{i}. {emoji} *{ticker}* : +{change:.1f}% (${price:.2f})\n"
    
    msg += "\n━━━━━━━━━━━━━━━━━━\n⚠️ Faites vos propres recherches"
    bot.reply_to(message, msg, parse_mode='Markdown')

# ===== WEBHOOK =====
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'ok', 200
    return 'bad request', 400

# ===== DÉMARRAGE =====
if __name__ == '__main__':
    print("🤖 Démarrage du bot...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"✅ Bot connecté sur {WEBHOOK_URL}")
    app.run(host='0.0.0.0', port=PORT)
