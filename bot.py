import os
import telebot
import requests
from datetime import datetime
import time
from flask import Flask, request

# Configuration
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))

# URL publique de votre service Render (sera définie plus tard)
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ===== FONCTIONS UTILES =====
def get_stock_data(ticker):
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB}"
    resp = requests.get(url).json()
    if 'c' in resp and resp['c'] > 0:
        return resp
    return None

# ===== COMMANDES =====
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 Bot actif en mode webhook !\n\n/analyse TICKER\n/top\n/marché\n/aide")

@bot.message_handler(commands=['aide'])
def aide(message):
    bot.reply_to(message, "📚 /analyse TICKER\n🔥 /top\n📈 /marché")

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Utilisation : /analyse AAPL")
            return
        ticker = parts[1].upper()
        data = get_stock_data(ticker)
        if not data:
            bot.reply_to(message, f"❌ Données indisponibles pour {ticker}")
            return
        price = data['c']
        previous = data.get('pc', price)
        change = ((price - previous) / previous) * 100 if previous else 0
        msg = f"""
📊 *{ticker}*
💰 Prix : ${price:.2f}
📈 Variation : {change:+.2f}%
🔼 Plus haut : ${data.get('h', 'N/A')}
🔽 Plus bas : ${data.get('l', 'N/A')}
"""
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Erreur : {str(e)}")

@bot.message_handler(commands=['top'])
def top(message):
    stocks = ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN']
    msg = "🏆 *TOP OPPORTUNITÉS*\n\n"
    for ticker in stocks:
        try:
            data = get_stock_data(ticker)
            if data:
                price = data['c']
                previous = data.get('pc', price)
                change = ((price - previous) / previous) * 100 if previous else 0
                emoji = "🟢" if change > 0 else "🔴"
                msg += f"{emoji} *{ticker}* : ${price:.2f} ({change:+.1f}%)\n"
        except:
            pass
        time.sleep(0.5)
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marché'])
def marche(message):
    indices = {'SPY': 'S&P 500', 'QQQ': 'Nasdaq', 'DIA': 'Dow Jones'}
    msg = "📈 *APERÇU DU MARCHÉ*\n\n"
    for ticker, name in indices.items():
        try:
            data = get_stock_data(ticker)
            if data:
                price = data['c']
                previous = data.get('pc', price)
                change = ((price - previous) / previous) * 100 if previous else 0
                emoji = "🟢" if change > 0 else "🔴"
                msg += f"{emoji} *{name}* : ${price:.2f} ({change:+.1f}%)\n"
        except:
            pass
        time.sleep(0.5)
    bot.reply_to(message, msg, parse_mode='Markdown')

# ===== ROUTE DU WEBHOOK =====
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'ok', 200
    return 'bad request', 400

# ===== DÉMARRAGE =====
if __name__ == "__main__":
    # Supprimer l'ancien webhook et définir le nouveau
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"🤖 Bot démarré en webhook sur {WEBHOOK_URL}")
    app.run(host='0.0.0.0', port=PORT)
