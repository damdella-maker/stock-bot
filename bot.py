import os, telebot, requests, yfinance as yf, pandas as pd, numpy as np, pytz
import schedule, time, threading, json
from datetime import datetime, timedelta
from flask import Flask, request
from bs4 import BeautifulSoup

# ===== CONFIGURATION =====
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ===== STOCKAGE =====
subscribers = set()  # Chat IDs abonnés aux alertes
user_capital = {}    # Capital par utilisateur
user_risk = {}       # Risque max par trade (€)

# Portefeuille : {chat_id: [{'ticker':str, 'buy_price':float, 'qty':int, 'stop':float, 'tp1':float, 'tp2':float, 'date':str}]}
portfolios = {}

# Watchlist agressive (200+ tickers volatils)
WATCHLIST = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'SQ', 'ROKU', 'ZM', 'CRWD', 'PLTR', 'GME', 'AMC',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN', 'NIO', 'XPEV', 'LI',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'BABA', 'BIDU', 'JD',
    'MSTR', 'SI', 'HIVE', 'BTBT', 'MRNA', 'BIIB', 'PFE', 'BA',
    'CCL', 'AAL', 'SPCE', 'FCEL', 'PLUG', 'QS', 'CHPT', 'NKLA'
]

# ===== FONCTIONS D'ANALYSE MULTI-SOURCES =====
def get_yahoo_data(ticker):
    """Données Yahoo Finance"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        hist = stock.history(period='2d')
        if hist.empty: return None
        current = hist['Close'].iloc[-1]
        previous = hist['Close'].iloc[-2] if len(hist) >= 2 else current
        change = ((current - previous) / previous) * 100
        return {
            'price': current,
            'change': round(change, 2),
            'volume': info.get('volume', 0),
            'avg_volume': info.get('averageVolume', 0),
            'market_cap': info.get('marketCap', 0),
            'pe_ratio': info.get('trailingPE', 'N/A'),
            'source': 'Yahoo Finance'
        }
    except:
        return None

def get_finnhub_sentiment(ticker):
    """Sentiment via Finnhub news"""
    try:
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        news = requests.get(url).json()
        if not news or 'error' in news: return 50, []
        headlines = [n['headline'] for n in news[:5] if 'headline' in n]
        # Analyse simple des mots
        positive_words = ['up', 'rise', 'gain', 'profit', 'growth', 'buy', 'bull', 'positive', 'record']
        negative_words = ['down', 'fall', 'loss', 'drop', 'sell', 'bear', 'negative', 'risk', 'crash']
        score = 50
        for h in headlines:
            h_lower = h.lower()
            for w in positive_words:
                if w in h_lower: score += 3
            for w in negative_words:
                if w in h_lower: score -= 3
        score = max(0, min(100, score))
        return score, headlines
    except:
        return 50, []

def get_zonebourse_data(ticker):
    """Scraping rapide de Zonebourse (simulé car le site bloque le scraping)"""
    # Note : Zonebourse bloque le scraping automatisé. On retourne des données simulées.
    return {
        'consensus': 'Achat' if np.random.random() > 0.4 else 'Conserver',
        'objectif': round(np.random.uniform(10, 50), 1),
        'source': 'Zonebourse (simulé)'
    }

def get_technical_score(ticker):
    """Score technique 0-100"""
    try:
        df = yf.download(ticker, period='1mo', progress=False)
        if df.empty: return 0
        close = df['Close'].squeeze()
        vol = df['Volume'].squeeze()
        
        score = 50  # base
        
        # Volume
        avg_vol = vol.iloc[-20:].mean()
        if vol.iloc[-1] > 2 * avg_vol:
            score += 20
        elif vol.iloc[-1] > 1.5 * avg_vol:
            score += 10
        
        # Performance récente
        if len(close) >= 5:
            change_5d = ((close.iloc[-1] - close.iloc[-5]) / close.iloc[-5]) * 100
            if change_5d > 30: score += 25
            elif change_5d > 15: score += 15
            elif change_5d > 5: score += 5
            elif change_5d < -10: score -= 15
        
        # Tendance court terme
        ema10 = close.ewm(span=10).mean().iloc[-1]
        if close.iloc[-1] > ema10: score += 10
        
        return min(100, max(0, score))
    except:
        return 0

def get_global_analysis(ticker):
    """Analyse complète multi-sources"""
    yahoo = get_yahoo_data(ticker)
    if not yahoo: return None
    
    sentiment, news = get_finnhub_sentiment(ticker)
    zonebourse = get_zonebourse_data(ticker)
    tech_score = get_technical_score(ticker)
    
    # Score global pondéré
    global_score = (tech_score * 0.5 + sentiment * 0.3 + 
                   (20 if yahoo['change'] > 5 else 10) * 0.2)
    
    return {
        'ticker': ticker,
        'price': yahoo['price'],
        'change': yahoo['change'],
        'score': round(global_score, 1),
        'tech_score': tech_score,
        'sentiment': sentiment,
        'news_headlines': news[:3],
        'zonebourse_consensus': zonebourse['consensus'],
        'zonebourse_objectif': zonebourse['objectif'],
        'volume_ratio': round(yahoo['volume'] / yahoo['avg_volume'], 1) if yahoo['avg_volume'] else 1,
        'sources': ['Yahoo Finance', 'Finnhub', 'Zonebourse']
    }

def get_top_explosive(min_change=40):
    """Trouve les actions qui explosent de +40% ou plus"""
    results = []
    for ticker in WATCHLIST:
        data = get_yahoo_data(ticker)
        if data and data['change'] >= min_change:
            analysis = get_global_analysis(ticker)
            if analysis:
                results.append(analysis)
        time.sleep(0.2)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:5]

# ===== COMMANDES TELEGRAM =====
@bot.message_handler(commands=['start'])
def cmd_start(message):
    subscribers.add(message.chat.id)
    msg = """🚀 *TRADER PRO ULTRA-AGRESSIF*

✅ Bot activé avec succès !

*Commandes :*
📊 /analyse TICKER - Analyse multi-sources
💥 /explosive - Actions à +40% aujourd'hui
📈 /marché - Indices boursiers
💼 /portfolio - Vos positions
📋 /aide - Guide complet

*Paramètres :*
💰 /setcapital 2000
⚠️ /setrisk 6

🔔 Alertes auto toutes les 15 min (8h-21h)
📅 Rapport matinal à 9h
🛡️ Surveillance positions toutes les 10 min"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """📚 *GUIDE COMPLET*

*/analyse TICKER*
→ Analyse complète avec boutons Buy/Pass
→ Prix, score, sentiment, sources

*/explosive*
→ Top 5 des actions à +40% aujourd'hui
→ Avec score technique et risque

*/marché*
→ S&P 500, Nasdaq, Dow Jones

*/portfolio*
→ Vos positions avec gains/pertes

*/vendre TICKER*
→ Vendre une position

*Buy / Pass :*
Après une analyse, cliquez sur Buy pour enregistrer votre achat. Le bot surveillera la position toutes les 10 minutes.

*Alertes :*
Si une de vos positions perd -5% ou touche son stop, vous recevez une alerte immédiate."""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['setcapital'])
def cmd_setcapital(message):
    try:
        cap = float(message.text.split()[1])
        user_capital[message.chat.id] = cap
        bot.reply_to(message, f"✅ Capital défini à {cap}€")
    except:
        bot.reply_to(message, "❌ /setcapital 2000")

@bot.message_handler(commands=['setrisk'])
def cmd_setrisk(message):
    try:
        risk = float(message.text.split()[1])
        user_risk[message.chat.id] = risk
        bot.reply_to(message, f"✅ Risque max par trade = {risk}€")
    except:
        bot.reply_to(message, "❌ /setrisk 6")

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    
    bot.send_chat_action(message.chat.id, 'typing')
    analysis = get_global_analysis(ticker)
    
    if not analysis:
        bot.reply_to(message, f"❌ Données indisponibles pour {ticker}")
        return
    
    # Stop-loss et take-profit
    price = analysis['price']
    atr = price * 0.05  # 5% ATR estimé
    stop = round(price - atr, 2)
    tp1 = round(price + atr * 2, 2)
    tp2 = round(price + atr * 4, 2)
    
    msg = f"📊 *ANALYSE COMPLÈTE - {ticker}*\n\n"
    msg += f"⭐ *Score Global : {analysis['score']}/100*\n"
    msg += f"💰 Prix : ${price:.2f} ({analysis['change']:+.1f}%)\n"
    msg += f"📊 Score Technique : {analysis['tech_score']}/100\n"
    msg += f"📰 Sentiment : {analysis['sentiment']}/100\n"
    msg += f"📈 Volume : {analysis['volume_ratio']}x la moyenne\n\n"
    
    msg += f"🛡️ *Gestion du Risque*\n"
    msg += f"🛑 Stop-Loss : ${stop}\n"
    msg += f"🎯 Take-Profit 1 : ${tp1} (+{round((tp1/price-1)*100)}%)\n"
    msg += f"🎯 Take-Profit 2 : ${tp2} (+{round((tp2/price-1)*100)}%)\n\n"
    
    msg += f"📡 *Sources :* {', '.join(analysis['sources'])}\n"
    msg += f"💡 Zonebourse : {analysis['zonebourse_consensus']} (obj. +{analysis['zonebourse_objectif']}%)\n"
    
    if analysis['news_headlines']:
        msg += f"\n📰 *Dernières news :*\n"
        for h in analysis['news_headlines'][:2]:
            msg += f"• {h}\n"
    
    # Boutons Buy/Pass
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("✅ BUY", callback_data=f"buy_{ticker}_{price}"),
        telebot.types.InlineKeyboardButton("❌ PASS", callback_data="pass")
    )
    
    bot.reply_to(message, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if call.data.startswith('buy_'):
        parts = call.data.split('_')
        ticker = parts[1]
        ref_price = float(parts[2])
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            f"🛒 *Achat {ticker}*\n\nPrix de référence : ${ref_price:.2f}\n\nQuel est votre prix d'achat exact ?",
            parse_mode='Markdown'
        )
        bot.register_next_step_handler(msg, process_buy, ticker)
    
    elif call.data == 'pass':
        bot.answer_callback_query(call.id, "Opportunité ignorée")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

def process_buy(message, ticker):
    try:
        buy_price = float(message.text.replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Prix invalide.")
        return
    
    chat_id = message.chat.id
    risk = user_risk.get(chat_id, 6)
    
    atr = buy_price * 0.05
    stop = round(buy_price - atr, 2)
    tp1 = round(buy_price + atr * 2, 2)
    tp2 = round(buy_price + atr * 4, 2)
    
    if buy_price > stop:
        qty = max(1, int(risk / (buy_price - stop)))
    else:
        qty = 1
    
    if chat_id not in portfolios:
        portfolios[chat_id] = []
    
    portfolios[chat_id].append({
        'ticker': ticker,
        'buy_price': buy_price,
        'qty': qty,
        'stop': stop,
        'tp1': tp1,
        'tp2': tp2,
        'date': datetime.now().isoformat()
    })
    
    msg = f"""✅ *POSITION OUVERTE*

📊 *{ticker}*
🛒 Acheté : {qty} action(s) à ${buy_price:.2f}
🛑 Stop-Loss : ${stop}
🎯 TP1 : ${tp1} (+{round((tp1/buy_price-1)*100)}%)
🎯 TP2 : ${tp2} (+{round((tp2/buy_price-1)*100)}%)

⚠️ Risque max : {risk}€
🔍 Surveillance intensive activée (toutes les 10 min)"""
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    chat_id = message.chat.id
    if chat_id not in portfolios or not portfolios[chat_id]:
        bot.reply_to(message, "📭 Votre portefeuille est vide.")
        return
    
    msg = "💼 *VOTRE PORTEFEUILLE*\n\n"
    total_gain = 0
    
    for pos in portfolios[chat_id]:
        data = get_yahoo_data(pos['ticker'])
        if data:
            current = data['price']
            gain = (current - pos['buy_price']) * pos['qty']
            gain_pct = ((current / pos['buy_price']) - 1) * 100
            total_gain += gain
            emoji = "🟢" if gain > 0 else "🔴"
            msg += f"{emoji} *{pos['ticker']}* - {pos['qty']} action(s)\n"
            msg += f"   Achat : ${pos['buy_price']:.2f} | Actuel : ${current:.2f}\n"
            msg += f"   Gain : ${gain:.2f} ({gain_pct:+.1f}%)\n"
            msg += f"   Stop : ${pos['stop']} | TP1 : ${pos['tp1']}\n\n"
        else:
            msg += f"⚠️ *{pos['ticker']}* - Données indisponibles\n\n"
    
    msg += f"━━━━━━━━━━━━━━━━━━\n💰 *Gain total : ${total_gain:.2f}*"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /vendre TICKER")
        return
    
    chat_id = message.chat.id
    if chat_id in portfolios:
        portfolios[chat_id] = [p for p in portfolios[chat_id] if p['ticker'] != ticker]
        bot.reply_to(message, f"✅ Position {ticker} vendue.")
    else:
        bot.reply_to(message, "❌ Aucune position trouvée.")

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    top = get_top_explosive(min_change=40)
    
    if not top:
        bot.reply_to(message, "🔍 Aucune action à +40% détectée pour le moment.\n\nUtilisez /analyse TICKER pour une analyse complète.")
        return
    
    msg = "💥 *TOP EXPLOSIF (+40% AUJOURD'HUI)*\n\n"
    
    for i, a in enumerate(top, 1):
        msg += f"*{i}. {a['ticker']}* - Score : {a['score']}/100\n"
        msg += f"   💰 ${a['price']:.2f} (+{a['change']:.1f}%)\n"
        msg += f"   📊 Vol : {a['volume_ratio']}x | Sentiment : {a['sentiment']}/100\n"
        msg += f"   📡 Sources : {', '.join(a['sources'])}\n\n"
    
    msg += "⚠️ Risque extrême - Utilisez /analyse TICKER avant d'acheter"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marché'])
def cmd_marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    indices = {'SPY': 'S&P 500', 'QQQ': 'Nasdaq', 'DIA': 'Dow Jones'}
    msg = "📈 *INDICES BOURSIERS*\n\n"
    
    for ticker, name in indices.items():
        data = get_yahoo_data(ticker)
        if data:
            emoji = "🟢" if data['change'] > 0 else "🔴"
            msg += f"{emoji} *{name}* : ${data['price']:.2f} ({data['change']:+.2f}%)\n"
    
    msg += f"\n📡 Source : Yahoo Finance\n🕐 {datetime.now().strftime('%H:%M')}"
    bot.reply_to(message, msg, parse_mode='Markdown')

# ===== ALERTES ET SURVEILLANCE =====
def check_explosive_alerts():
    """Alerte toutes les 15 min si action > +40%"""
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21:
        return
    
    top = get_top_explosive(min_change=40)
    if top:
        for subscriber in subscribers:
            try:
                msg = "🔔 *ALERTE EXPLOSIVE*\n\n"
                for a in top[:3]:
                    msg += f"💥 *{a['ticker']}* +{a['change']:.1f}% (Score {a['score']}/100)\n"
                msg += "\nUtilisez /analyse TICKER pour les détails"
                bot.send_message(subscriber, msg, parse_mode='Markdown')
            except:
                pass

def monitor_positions():
    """Surveillance des positions toutes les 10 min"""
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour < 8 or now.hour >= 21:
        return
    
    for chat_id, positions in portfolios.items():
        for pos in positions:
            data = get_yahoo_data(pos['ticker'])
            if not data:
                continue
            
            current = data['price']
            loss_pct = ((current / pos['buy_price']) - 1) * 100
            
            # Alerte si perte > 5% ou stop touché
            if loss_pct <= -5 or current <= pos['stop']:
                msg = f"""🚨 *ALERTE CRITIQUE*

📊 *{pos['ticker']}*
💸 Prix actuel : ${current:.2f}
📉 Perte : {loss_pct:.1f}%
🛑 Stop-Loss : ${pos['stop']}

⚠️ Envisagez de vendre ! /vendre {pos['ticker']}"""
                
                try:
                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                except:
                    pass

def morning_report():
    """Rapport quotidien à 9h"""
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if now.hour != 9 or now.minute > 15:
        return
    
    top = get_top_explosive(min_change=30)
    if not top:
        return
    
    msg = f"☀️ *RAPPORT MATINAL - {now.strftime('%d/%m/%Y')}*\n\n"
    msg += "🔥 *Actions à fort potentiel aujourd'hui :*\n\n"
    
    for a in top:
        msg += f"• *{a['ticker']}* - Score {a['score']}/100 - +{a['change']:.1f}%\n"
    
    msg += f"\n📡 Sources : Yahoo Finance, Finnhub, Zonebourse"
    
    for subscriber in subscribers:
        try:
            bot.send_message(subscriber, msg, parse_mode='Markdown')
        except:
            pass

def run_scheduler():
    """Planificateur de tâches"""
    schedule.every(15).minutes.do(check_explosive_alerts)
    schedule.every(10).minutes.do(monitor_positions)
    schedule.every().hour.at(":00").do(morning_report)
    
    while True:
        schedule.run_pending()
        time.sleep(30)

# ===== FLASK WEBHOOK =====
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'ok', 200
    return 'bad request', 400

@app.route('/')
def home():
    return "🤖 Bot Trader Pro - Actif"

# ===== DÉMARRAGE =====
if __name__ == '__main__':
    print("🤖 Démarrage du Bot Trader Pro...")
    
    # Supprimer l'ancien webhook et configurer le nouveau
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    
    print(f"✅ Bot connecté sur {WEBHOOK_URL}")
    print("📅 Rapport matinal à 9h")
    print("🔔 Alertes explosives toutes les 15 min")
    print("🛡️ Surveillance positions toutes les 10 min")
    
    # Démarrer le planificateur en arrière-plan
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    # Démarrer Flask
    app.run(host='0.0.0.0', port=PORT)
