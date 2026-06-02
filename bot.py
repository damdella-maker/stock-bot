import os, telebot, requests, yfinance as yf, pandas as pd, numpy as np, pytz
import schedule, time, threading, json
from datetime import datetime, timedelta
from flask import Flask, request
import ta, nltk
from nltk.sentiment import SentimentIntensityAnalyzer
nltk.download('vader_lexicon', quiet=True)
import praw

# ----- CONFIGURATION -----
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://your-service.onrender.com')
REDDIT_CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID', '')
REDDIT_CLIENT_SECRET = os.environ.get('REDDIT_CLIENT_SECRET', '')
REDDIT_USER_AGENT = os.environ.get('REDDIT_USER_AGENT', 'stockbot')
TWITTER_BEARER = os.environ.get('TWITTER_BEARER_TOKEN', '')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
sia = SentimentIntensityAnalyzer()

# Reddit (optionnel)
reddit = None
if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
    reddit = praw.Reddit(client_id=REDDIT_CLIENT_ID,
                         client_secret=REDDIT_CLIENT_SECRET,
                         user_agent=REDDIT_USER_AGENT)

subscribers = set()
user_capital = {}
user_risk_per_trade = {}
# Portefeuille utilisateur : {chat_id: [{'ticker': str, 'buy_price': float, 'qty': int, 'stop': float, 'tp1': float, 'tp2': float, 'date': str}]}
portfolios = {}

# ----- LISTE ULTRA-VOLATILE -----
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
def get_price_change(ticker):
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

def get_technical_score(ticker):
    """Score technique (0-100) basé sur RSI, MACD, Volume, EMA10"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return None
        close = df['Close'].squeeze()
        vol = df['Volume'].squeeze()
        score = 0
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if 40 <= rsi <= 50: score += 25
        elif 30 <= rsi < 40: score += 20
        elif rsi < 30: score += 15
        macd = ta.trend.MACD(close)
        if macd.macd().iloc[-1] > macd.macd_signal().iloc[-1]:
            score += 20
        avg_vol = vol.iloc[-20:].mean()
        if vol.iloc[-1] > 1.5 * avg_vol:
            score += 20
        ema10 = ta.trend.EMAIndicator(close, window=10).ema_indicator().iloc[-1]
        if close.iloc[-1] > ema10:
            score += 15
        return min(score, 100), rsi, vol.iloc[-1]/avg_vol if avg_vol>0 else 1, close.iloc[-1]
    except:
        return None, None, None, None

def get_sentiment(ticker):
    """Sentiment combiné Reddit + Twitter + Finnhub"""
    scores = []
    sources = []
    # Finnhub
    try:
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        news = requests.get(url).json()
        if news and 'error' not in news:
            for article in news[:3]:
                text = article.get('headline', '') + ' ' + article.get('summary', '')
                scores.append(sia.polarity_scores(text)['compound'])
            sources.append('Finnhub')
    except: pass
    # Reddit
    if reddit:
        try:
            for submission in reddit.subreddit('wallstreetbets').search(ticker, limit=5):
                text = submission.title + ' ' + submission.selftext
                scores.append(sia.polarity_scores(text)['compound'])
            sources.append('Reddit')
        except: pass
    # Twitter (simple recherche via Bearer)
    if TWITTER_BEARER:
        try:
            headers = {'Authorization': f'Bearer {TWITTER_BEARER}'}
            resp = requests.get(f'https://api.twitter.com/2/tweets/search/recent?query={ticker}&max_results=10', headers=headers).json()
            if 'data' in resp:
                for tweet in resp['data']:
                    scores.append(sia.polarity_scores(tweet['text'])['compound'])
            sources.append('Twitter')
        except: pass
    if scores:
        avg = np.mean(scores)*50+50
        return max(0, min(100, avg)), sources
    return 50, ['Aucune']

# ----- COMMANDES TELEGRAM -----
@bot.message_handler(commands=['start'])
def start(message):
    subscribers.add(message.chat.id)
    bot.reply_to(message, "🤖 *Trader Pro* activé.\n\n/analyse TICKER\n/explosive\n/portfolio\n/aide", parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    score, rsi, vol_ratio, price = get_technical_score(ticker)
    if not score: bot.reply_to(message, "Données indisponibles."); return
    sent, sources = get_sentiment(ticker)
    msg = f"📊 *{ticker}* | Score {score}/100\n"
    msg += f"`[{'█'*(score//10)}{'░'*(10-score//10)}]`\n\n"
    msg += f"• RSI : {rsi:.1f}\n• Volume x{vol_ratio:.1f}\n• Sentiment : {sent:.0f}/100\n"
    msg += f"📡 Sources : {', '.join(sources)}\n\n"
    # Stop/TP
    atr = price*0.03  # approximatif
    stop = round(price - atr, 2)
    tp1 = round(price + 2*atr, 2)
    tp2 = round(price + 4*atr, 2)
    msg += f"🛑 Stop conseillé : {stop}€ | TP1 : {tp1}€ | TP2 : {tp2}€\n"
    # Boutons Buy/Pass
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Buy", callback_data=f"buy_{ticker}_{price}"),
               telebot.types.InlineKeyboardButton("❌ Pass", callback_data="pass"))
    bot.send_message(message.chat.id, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    if call.data.startswith('buy_'):
        parts = call.data.split('_')
        ticker = parts[1]
        price = float(parts[2])
        bot.answer_callback_query(call.id, "Entrez le prix d'achat exact")
        msg = bot.send_message(call.message.chat.id, f"🛒 Vous achetez *{ticker}* (~{price}€).\nQuel est votre prix d'achat exact ?", parse_mode='Markdown')
        bot.register_next_step_handler(msg, process_buy, ticker)
    elif call.data == 'pass':
        bot.answer_callback_query(call.id, "Vous avez ignoré cette action.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

def process_buy(message, ticker):
    try:
        buy_price = float(message.text)
    except:
        bot.reply_to(message, "Prix invalide."); return
    chat_id = message.chat.id
    if chat_id not in portfolios: portfolios[chat_id] = []
    # Analyse rapide pour stop/TP
    score, rsi, vol_ratio, _ = get_technical_score(ticker) or (50,50,1, buy_price)
    atr = buy_price*0.03
    stop = round(buy_price - atr, 2)
    tp1 = round(buy_price + 2*atr, 2)
    tp2 = round(buy_price + 4*atr, 2)
    risk = user_risk_per_trade.get(chat_id, 6)
    if buy_price > stop:
        qty = max(1, int(risk/(buy_price-stop)))
    else:
        qty = 1
    portfolios[chat_id].append({
        'ticker': ticker,
        'buy_price': buy_price,
        'qty': qty,
        'stop': stop,
        'tp1': tp1,
        'tp2': tp2,
        'date': datetime.now().isoformat()
    })
    bot.reply_to(message, f"✅ *{ticker}* acheté {qty} actions à {buy_price}€.\nStop : {stop}€ | TP1 : {tp1}€ | TP2 : {tp2}€\nSurveillance intensive activée.", parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def portfolio(message):
    chat_id = message.chat.id
    if chat_id not in portfolios or not portfolios[chat_id]:
        bot.reply_to(message, "Portefeuille vide.")
        return
    msg = "💼 *Portefeuille*\n\n"
    for pos in portfolios[chat_id]:
        curr, _ = get_price_change(pos['ticker'])
        if curr:
            gain = (curr - pos['buy_price']) * pos['qty']
            msg += f"*{pos['ticker']}* : {pos['qty']}x{pos['buy_price']}€ → {curr}€ ({gain:+.1f}€)\n"
        else:
            msg += f"*{pos['ticker']}* : données indispo\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def vendre(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "/vendre TICKER"); return
    chat_id = message.chat.id
    if chat_id in portfolios:
        portfolios[chat_id] = [p for p in portfolios[chat_id] if p['ticker'] != ticker]
        bot.reply_to(message, f"Position {ticker} supprimée.")
    else:
        bot.reply_to(message, "Aucune position trouvée.")

@bot.message_handler(commands=['explosive'])
def explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    changes = []
    for ticker in VOLATILE_STOCKS:
        curr, chg = get_price_change(ticker)
        if chg is not None and chg > 0:
            changes.append((ticker, curr, chg))
        time.sleep(0.1)
    changes.sort(key=lambda x: x[2], reverse=True)
    if not changes:
        bot.reply_to(message, "Aucune hausse détectée.")
        return
    msg = "💥 *TOP HAUSSES DU JOUR*\n\n"
    for ticker, price, change in changes[:5]:
        score, _, _, _ = get_technical_score(ticker) or (0,0,0,0)
        msg += f"🔥 *{ticker}* +{change:.1f}%  Score {score}/100\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

# ----- SURVEILLANCE INTENSIVE -----
def monitor_positions():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21: return
    for chat_id, positions in portfolios.items():
        for pos in positions[:]:
            curr, _ = get_price_change(pos['ticker'])
            if not curr: continue
            if curr <= pos['stop'] or (curr/pos['buy_price']-1)*100 <= -5:
                # Alerte critique avec bouton Vendre
                msg = f"🚨 *ALERTE CRITIQUE* {pos['ticker']} a atteint {curr}€ (stop {pos['stop']}€).\nVendre maintenant ?"
                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(telebot.types.InlineKeyboardButton("🔴 Vendre", callback_data=f"sell_{pos['ticker']}"))
                bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)

def run_monitor():
    schedule.every(10).minutes.do(monitor_positions)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ----- DÉMARRAGE -----
if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    threading.Thread(target=run_monitor, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
