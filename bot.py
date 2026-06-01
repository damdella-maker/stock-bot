import os
import telebot
import requests
from datetime import datetime
import time
import threading
from flask import Flask

# Configuration
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))

bot = telebot.TeleBot(TOKEN)

# Petit serveur web pour Render
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# Lancer Flask dans un thread séparé
threading.Thread(target=run_flask, daemon=True).start()

# ===== COMMANDES TELEGRAM =====
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 Bot actif !\n\nCommandes :\n/analyse TICKER\n/top\n/aide")

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
        
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB}"
        data = requests.get(url).json()
        
        if 'c' not in data or data['c'] == 0:
            bot.reply_to(message, f"❌ Action {ticker} non trouvée")
            return
        
        price = data['c']
        change = ((price - data['pc']) / data['pc']) * 100
        
        msg = f"""
📊 *{ticker}*
💰 Prix : ${price:.2f}
📈 Variation : {change:+.2f}%
🔼 Plus haut : ${data['h']:.2f}
🔽 Plus bas : ${data['l']:.2f}
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
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB}"
            data = requests.get(url).json()
            if 'c' in data and data['c'] > 0:
                change = ((data['c'] - data['pc']) / data['pc']) * 100
                emoji = "🟢" if change > 0 else "🔴"
                msg += f"{emoji} *{ticker}* : ${data['c']:.2f} ({change:+.1f}%)\n"
        except:
            pass
        time.sleep(0.5)
    
    bot.reply_to(message, msg, parse_mode='Markdown')

if __name__ == "__main__":
    print("🤖 Bot démarré !")
    while True:
        try:
            bot.polling(none_stop=True)
        except:
            time.sleep(10)
