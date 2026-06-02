import os
import telebot
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import schedule
import time
import threading
import io
import csv
from datetime import datetime
from flask import Flask, request
from telebot import types
import sqlite3

# ===== CONFIGURATION =====
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ===== BASE DE DONNÉES =====
DB_PATH = 'trading_bot.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, ticker TEXT, buy_price REAL, quantity INTEGER,
        stop_loss REAL, take_profit1 REAL, take_profit2 REAL,
        highest_price REAL, status TEXT DEFAULT 'open', date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, ticker TEXT, buy_price REAL, sell_price REAL,
        quantity INTEGER, profit_loss REAL, profit_loss_pct REAL, close_date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, ticker TEXT, target_price REAL,
        active INTEGER DEFAULT 1, date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (
        chat_id INTEGER PRIMARY KEY
    )''')
    conn.commit()
    conn.close()

init_db()

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    conn.close()

def db_fetchall(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

def db_fetchone(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    row = c.fetchone()
    conn.close()
    return row

def add_subscriber(chat_id):
    db_execute('INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)', (chat_id,))

def get_all_subscribers():
    rows = db_fetchall('SELECT chat_id FROM subscribers')
    return [r[0] for r in rows]

# ===== WATCHLIST NETTOYÉE =====
WATCHLIST = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'ROKU', 'ZM', 'CRWD', 'PLTR', 'GME', 'AMC',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN', 'NIO', 'XPEV', 'LI',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'BABA', 'BIDU', 'JD',
    'MSTR', 'MRNA', 'PFE', 'BA', 'CCL', 'AAL', 'SPCE', 'NKLA',
    'SNDL', 'TLRY', 'ACB', 'CGC', 'FCEL', 'PLUG', 'QS', 'CHPT',
    'DDOG', 'SNOW', 'MDB', 'ZS', 'NET', 'FSLY', 'U', 'DASH',
    'ABNB', 'CVNA', 'W', 'F', 'T', 'VZ', 'DIS', 'PYPL',
    'SHOP', 'TWLO', 'DOCU', 'PTON', 'BYND', 'DKNG', 'PINS', 'SNAP'
]

# ===== OUTILS =====
def is_valid_ticker(ticker):
    """Vérifie si un ticker est valide"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        if not info or 'regularMarketPrice' not in info or info.get('regularMarketPrice') is None:
            return False
        
        # Vérifier qu'il y a des données récentes
        hist = stock.history(period='5d')
        if hist.empty or len(hist) < 2:
            return False
        
        return True
    except:
        return False

def get_stock_info(ticker):
    """Récupère le nom complet et l'ISIN"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        name = info.get('longName', info.get('shortName', ticker))
        isin = info.get('isin', 'N/A')
        return name, isin
    except:
        return ticker, 'N/A'

def get_current_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period='1d')
        if not hist.empty:
            return hist['Close'].iloc[-1]
    except:
        pass
    return None

def get_analysis(ticker):
    """Analyse technique complète avec vérification de validité"""
    try:
        # Vérifier d'abord si le ticker est valide
        if not is_valid_ticker(ticker):
            return None
        
        df = yf.download(ticker, period='3mo', progress=False)
        if df.empty or len(df) < 20:
            return None
        
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()
        current_price = close.iloc[-1]
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1])) if loss.iloc[-1] != 0 else 50
        
        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        
        # MA
        ma20 = close.rolling(20).mean().iloc[-1]
        ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else ma20
        
        # Volume
        avg_vol = volume.iloc[-20:].mean()
        vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 1
        
        # ATR
        high = df['High'].squeeze()
        low = df['Low'].squeeze()
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        
        # Score 0-100
        score = 50
        if 30 <= rsi <= 50: score += 15
        elif rsi < 30: score += 10
        if macd_line.iloc[-1] > signal_line.iloc[-1]: score += 10
        if current_price > ma20: score += 10
        if current_price > ma50: score += 5
        if vol_ratio > 1.5: score += 10
        
        score = min(100, max(0, score))
        
        stop_loss = round(current_price - 2 * atr, 2)
        tp1 = round(current_price + 2 * atr, 2)
        tp2 = round(current_price + 4 * atr, 2)
        
        name, isin = get_stock_info(ticker)
        
        return {
            'ticker': ticker,
            'name': name,
            'isin': isin,
            'price': round(current_price, 2),
            'rsi': round(rsi, 1),
            'macd': round(macd_line.iloc[-1], 3),
            'macd_signal': round(signal_line.iloc[-1], 3),
            'ma20': round(ma20, 2),
            'ma50': round(ma50, 2),
            'atr': round(atr, 2),
            'vol_ratio': round(vol_ratio, 1),
            'score': score,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2
        }
    except Exception as e:
        print(f"Erreur analyse {ticker}: {e}")
        return None

def send_alert_to_all(message, parse='Markdown'):
    """Envoie un message à tous les abonnés"""
    subscribers = get_all_subscribers()
    for chat_id in subscribers:
        try:
            bot.send_message(chat_id, message, parse_mode=parse)
        except Exception as e:
            print(f"Erreur envoi à {chat_id}: {e}")

# ===== COMMANDES =====
@bot.message_handler(commands=['start'])
def cmd_start(message):
    add_subscriber(message.chat.id)
    msg = """🚀 *TRADER PRO BOT*

✅ Vous recevrez les alertes automatiques !

*Commandes :*
📊 /analyse TICKER - Analyse complète + ISIN
💥 /explosive - Top 5 opportunités
🔍 /scan - Détection breakouts
📈 /marché - Indices en direct
💼 /portfolio - Positions + P&L
📋 /historique - Trades fermés
📊 /stats - Performance
🔔 /alerte TICKER PRIX
📁 /import_csv - Import Trading212
📚 /aide - Guide complet"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """📚 *GUIDE COMPLET*

*/analyse TICKER* → RSI, MACD, MA, Stop/TP, ISIN
*/explosive* → Top 5 meilleurs scores
*/scan* → Breakouts volume ×3
*/marché* → S&P500, Nasdaq, Dow, VIX
*/portfolio* → Positions ouvertes
*/vendre TICKER* → Fermer une position
*/historique* → 10 derniers trades
*/stats* → Win rate, P&L, best/worst
*/alerte TICKER PRIX* → Alerte prix
*/import_csv* → Import CSV Trading212

🔔 Alertes auto : breakouts score ≥ 85 toutes les 15 min"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    
    bot.send_chat_action(message.chat.id, 'typing')
    a = get_analysis(ticker)
    
    if not a:
        bot.reply_to(message, f"❌ Données indisponibles pour {ticker}\nVérifiez le ticker ou réessayez plus tard.")
        return
    
    msg = f"""📊 *{a['name']} ({a['ticker']})*
🔖 ISIN : `{a['isin']}`

⭐ Score : {a['score']}/100
💰 Prix : ${a['price']}

📈 *Indicateurs :*
• RSI : {a['rsi']}
• MACD : {a['macd']} | Signal : {a['macd_signal']}
• MA20 : ${a['ma20']} | MA50 : ${a['ma50']}
• Volume : {a['vol_ratio']}x moyenne

🛡️ *Gestion du risque :*
• 🛑 Stop-Loss : ${a['stop_loss']}
• 🎯 Take-Profit 1 : ${a['tp1']}
• 🎯 Take-Profit 2 : ${a['tp2']}"""
    
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("🛒 1", callback_data=f"buy_{ticker}_{a['price']}_1"),
        types.InlineKeyboardButton("🛒 5", callback_data=f"buy_{ticker}_{a['price']}_5"),
        types.InlineKeyboardButton("🛒 10", callback_data=f"buy_{ticker}_{a['price']}_10"),
        types.InlineKeyboardButton("❌ Pass", callback_data="pass")
    )
    
    bot.reply_to(message, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy(call):
    parts = call.data.split('_')
    ticker = parts[1]
    price = float(parts[2])
    qty = int(parts[3])
    
    bot.answer_callback_query(call.id, f"Achat {qty} {ticker}")
    msg = bot.send_message(call.message.chat.id,
        f"🛒 *Achat {ticker}*\nQté : {qty}\nPrix indicatif : ${price:.2f}\n\nQuel est votre prix d'achat exact ?",
        parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_buy, ticker, qty)

@bot.callback_query_handler(func=lambda call: call.data == 'pass')
def handle_pass(call):
    bot.answer_callback_query(call.id, "Opportunité ignorée")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

def process_buy(message, ticker, qty):
    try:
        buy_price = float(message.text.replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Prix invalide.")
        return
    
    a = get_analysis(ticker)
    stop = a['stop_loss'] if a else round(buy_price * 0.95, 2)
    tp1 = a['tp1'] if a else round(buy_price * 1.10, 2)
    tp2 = a['tp2'] if a else round(buy_price * 1.20, 2)
    
    db_execute('''INSERT INTO positions (chat_id, ticker, buy_price, quantity, stop_loss, take_profit1, take_profit2, highest_price, date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (message.chat.id, ticker, buy_price, qty, stop, tp1, tp2, buy_price, datetime.now().isoformat()))
    
    bot.reply_to(message, f"✅ *{ticker}* acheté : {qty} × ${buy_price:.2f}\n🛑 Stop ${stop}\n🎯 TP1 ${tp1}\n🎯 TP2 ${tp2}", parse_mode='Markdown')

@bot.message_handler(commands=['scan'])
def cmd_scan(message):
    bot.send_chat_action(message.chat.id, 'typing')
    breakouts = []
    
    for t in WATCHLIST:
        try:
            if not is_valid_ticker(t):
                continue
            
            df = yf.download(t, period='5d', progress=False)
            if len(df) < 2:
                continue
            
            close = df['Close'].squeeze()
            vol = df['Volume'].squeeze()
            avg_vol = vol.iloc[:-1].mean()
            
            if avg_vol > 0 and vol.iloc[-1] > 3 * avg_vol and close.iloc[-1] > close.iloc[-2]:
                a = get_analysis(t)
                if a:
                    name, isin = get_stock_info(t)
                    breakouts.append({
                        'ticker': t, 'name': name, 'isin': isin,
                        'price': close.iloc[-1], 'vol_ratio': round(vol.iloc[-1]/avg_vol, 1),
                        'score': a['score'], 'stop_loss': a['stop_loss'], 'tp1': a['tp1']
                    })
        except:
            pass
        time.sleep(0.1)
    
    if not breakouts:
        bot.reply_to(message, "🔍 Aucun breakout détecté pour le moment.")
        return
    
    breakouts.sort(key=lambda x: x['score'], reverse=True)
    
    msg = "🔍 *BREAKOUTS DÉTECTÉS*\n\n"
    for b in breakouts[:5]:
        msg += f"🚀 *{b['name']} ({b['ticker']})*\n"
        msg += f"   🔖 ISIN : `{b['isin']}`\n"
        msg += f"   ⭐ Score : {b['score']}/100\n"
        msg += f"   📊 Vol ×{b['vol_ratio']} | 💰 ${b['price']:.2f}\n"
        msg += f"   🛑 Stop : ${b['stop_loss']} | 🎯 TP1 : ${b['tp1']}\n\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    results = []
    for t in WATCHLIST:
        a = get_analysis(t)
        if a and a['score'] >= 60:
            results.append(a)
        time.sleep(0.1)
    results.sort(key=lambda x: x['score'], reverse=True)
    
    if not results:
        bot.reply_to(message, "Aucune opportunité forte détectée.")
        return
    
    msg = "💥 *TOP OPPORTUNITÉS*\n\n"
    for i, a in enumerate(results[:5], 1):
        msg += f"*{i}. {a['name']} ({a['ticker']})*\n"
        msg += f"   🔖 ISIN : `{a['isin']}`\n"
        msg += f"   ⭐ Score : {a['score']}/100 | 💰 ${a['price']}\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marché'])
def cmd_marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    indices = {'^GSPC': 'S&P 500', '^IXIC': 'Nasdaq', '^DJI': 'Dow Jones', '^VIX': 'VIX'}
    msg = "📈 *INDICES EN DIRECT*\n\n"
    for t, name in indices.items():
        try:
            stock = yf.Ticker(t)
            hist = stock.history(period='2d')
            if len(hist) >= 2:
                curr = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                chg = ((curr - prev) / prev) * 100
                emoji = "🟢" if chg > 0 else "🔴"
                msg += f"{emoji} *{name}* : {curr:,.2f} ({chg:+.2f}%)\n"
        except:
            pass
    msg += f"\n🕐 {datetime.now().strftime('%H:%M:%S')}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    rows = db_fetchall('SELECT * FROM positions WHERE chat_id = ? AND status = ?', (message.chat.id, 'open'))
    if not rows:
        bot.reply_to(message, "📭 Votre portefeuille est vide.\n\n/analyse TICKER pour commencer")
        return
    
    msg = "💼 *VOTRE PORTEFEUILLE*\n\n"
    total_inv = 0
    total_cur = 0
    for r in rows:
        ticker = r[2]
        buy = r[3]
        qty = r[4]
        curr = get_current_price(ticker) or buy
        inv = buy * qty
        val = curr * qty
        pnl = val - inv
        pnl_pct = ((curr / buy) - 1) * 100
        total_inv += inv
        total_cur += val
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg += f"{emoji} *{ticker}* ×{qty}\n"
        msg += f"   Achat : ${buy:.2f} | Actuel : ${curr:.2f}\n"
        msg += f"   P&L : ${pnl:.2f} ({pnl_pct:+.1f}%)\n\n"
    
    total_pnl = total_cur - total_inv
    total_pnl_pct = ((total_cur / total_inv) - 1) * 100 if total_inv > 0 else 0
    msg += f"━━━━━━━━━━━━━━━━\n💰 *Total :* ${total_cur:.2f}\n📈 *P&L :* ${total_pnl:.2f} ({total_pnl_pct:+.1f}%)"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /vendre TICKER\nExemple : /vendre AAPL")
        return
    
    row = db_fetchone('SELECT * FROM positions WHERE chat_id = ? AND ticker = ? AND status = ?', (message.chat.id, ticker, 'open'))
    if not row:
        bot.reply_to(message, f"❌ Aucune position ouverte pour {ticker}")
        return
    
    curr = get_current_price(ticker) or row[3]
    pnl = (curr - row[3]) * row[4]
    pnl_pct = ((curr / row[3]) - 1) * 100
    
    db_execute('INSERT INTO trades (chat_id, ticker, buy_price, sell_price, quantity, profit_loss, profit_loss_pct, close_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
               (message.chat.id, ticker, row[3], curr, row[4], pnl, pnl_pct, datetime.now().isoformat()))
    db_execute('UPDATE positions SET status = ? WHERE id = ?', ('closed', row[0]))
    
    emoji = "🟢" if pnl >= 0 else "🔴"
    bot.reply_to(message, f"✅ *{ticker}* vendu avec succès\n{emoji} P&L : ${pnl:.2f} ({pnl_pct:+.1f}%)", parse_mode='Markdown')

@bot.message_handler(commands=['historique'])
def cmd_historique(message):
    rows = db_fetchall('SELECT * FROM trades WHERE chat_id = ? ORDER BY close_date DESC LIMIT 10', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📋 Aucun trade fermé pour le moment.")
        return
    
    msg = "📋 *10 DERNIERS TRADES*\n\n"
    for r in rows:
        emoji = "🟢" if r[6] >= 0 else "🔴"
        msg += f"{emoji} *{r[2]}* ×{r[5]}\n"
        msg += f"   Achat : ${r[3]:.2f} → Vente : ${r[4]:.2f}\n"
        msg += f"   P&L : ${r[6]:.2f} ({r[7]:+.1f}%)\n"
        msg += f"   📅 {r[8][:10]}\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    rows = db_fetchall('SELECT profit_loss, profit_loss_pct FROM trades WHERE chat_id = ?', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📊 Pas encore de statistiques.\nFermez des trades pour en générer.")
        return
    
    total_trades = len(rows)
    wins = sum(1 for r in rows if r[0] > 0)
    total_pnl = sum(r[0] for r in rows)
    best = max(r[0] for r in rows)
    worst = min(r[0] for r in rows)
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
    
    msg = f"""📊 *STATISTIQUES DE TRADING*

📈 Nombre de trades : {total_trades}
✅ Win rate : {win_rate:.1f}%
💰 P&L Total : ${total_pnl:.2f}
📊 P&L Moyen : ${avg_pnl:.2f}
🏆 Meilleur trade : ${best:.2f}
💀 Pire trade : ${worst:.2f}"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['alerte'])
def cmd_alerte(message):
    try:
        parts = message.text.split()
        ticker = parts[1].upper()
        target = float(parts[2])
    except:
        bot.reply_to(message, "❌ /alerte TICKER PRIX\nExemple : /alerte AAPL 200")
        return
    
    db_execute('INSERT INTO alerts (chat_id, ticker, target_price, date) VALUES (?, ?, ?, ?)', (message.chat.id, ticker, target, datetime.now().isoformat()))
    bot.reply_to(message, f"🔔 Alerte créée : *{ticker}* à ${target:.2f}", parse_mode='Markdown')

@bot.message_handler(commands=['alertes'])
def cmd_alertes(message):
    rows = db_fetchall('SELECT * FROM alerts WHERE chat_id = ? AND active = 1', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📢 Aucune alerte active.")
        return
    
    msg = "📢 *ALERTES ACTIVES*\n\n"
    for r in rows:
        curr = get_current_price(r[2])
        curr_str = f" (Actuel : ${curr:.2f})" if curr else ""
        msg += f"🔔 *{r[2]}* → ${r[3]:.2f}{curr_str}\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['import_csv'])
def cmd_import_csv(message):
    msg = bot.reply_to(message, "📁 *Import CSV Trading212*\n\nEnvoyez votre fichier CSV.\n\n*Format accepté :*\n`Action,Quantité,Prix`\n`AAPL,10,150.50`", parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_csv)

def process_csv(message):
    content = message.text
    if message.document:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        content = downloaded.decode('utf-8')
    
    if not content:
        bot.reply_to(message, "❌ Contenu vide.")
        return
    
    try:
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for row in reader:
            ticker = row.get('Action', row.get('Ticker', row.get('Symbol', '')))
            qty = int(float(row.get('Quantité', row.get('Quantity', 0))))
            price = float(row.get('Prix', row.get('Prix d\'achat', row.get('Price', 0))))
            if ticker and qty > 0 and price > 0:
                db_execute('''INSERT INTO positions (chat_id, ticker, buy_price, quantity, stop_loss, take_profit1, take_profit2, highest_price, date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (message.chat.id, ticker.upper(), price, qty, round(price*0.95,2), round(price*1.10,2), round(price*1.20,2), price, datetime.now().isoformat()))
                count += 1
        bot.reply_to(message, f"✅ *{count} positions importées avec succès !*\n\n/portfolio pour les voir.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Erreur lors de l'import : {str(e)}\n\nVérifiez le format : Action,Quantité,Prix")

# ===== SURVEILLANCE AUTOMATIQUE =====
def auto_scan_breakouts():
    """Scan automatique toutes les 15 minutes et alerte si score ≥ 85"""
    print(f"[{datetime.now().strftime('%H:%M')}] Auto-scan...")
    exceptional = []
    
    for t in WATCHLIST[:30]:
        try:
            if not is_valid_ticker(t):
                continue
            
            df = yf.download(t, period='5d', progress=False)
            if len(df) < 2:
                continue
            
            close = df['Close'].squeeze()
            vol = df['Volume'].squeeze()
            avg_vol = vol.iloc[:-1].mean()
            
            if avg_vol > 0 and vol.iloc[-1] > 3 * avg_vol and close.iloc[-1] > close.iloc[-2]:
                a = get_analysis(t)
                if a and a['score'] >= 85:
                    name, isin = get_stock_info(t)
                    exceptional.append({
                        'ticker': t, 'name': name, 'isin': isin,
                        'price': a['price'], 'score': a['score'],
                        'stop_loss': a['stop_loss'], 'tp1': a['tp1']
                    })
        except:
            pass
        time.sleep(0.1)
    
    if exceptional:
        msg = "🔔 *ALERTE AUTO - BREAKOUTS EXCEPTIONNELS*\n\n"
        for b in exceptional[:3]:
            msg += f"🔥 *{b['name']} ({b['ticker']})*\n"
            msg += f"   🔖 ISIN : `{b['isin']}`\n"
            msg += f"   ⭐ Score : {b['score']}/100\n"
            msg += f"   💰 ${b['price']:.2f}\n"
            msg += f"   🛑 Stop : ${b['stop_loss']} | 🎯 TP1 : ${b['tp1']}\n\n"
        msg += "📊 /analyse TICKER pour plus de détails"
        send_alert_to_all(msg)

def monitor_positions():
    """Surveille les positions ouvertes toutes les 10 minutes"""
    rows = db_fetchall("SELECT * FROM positions WHERE status = 'open'")
    for r in rows:
        curr = get_current_price(r[2])
        if not curr:
            continue
        
        # Trailing stop
        if curr > r[8]:
            db_execute('UPDATE positions SET highest_price = ? WHERE id = ?', (curr, r[0]))
        if curr >= r[3] * 1.05:
            new_stop = round(curr * 0.97, 2)
            if new_stop > r[5]:
                db_execute('UPDATE positions SET stop_loss = ? WHERE id = ?', (new_stop, r[0]))
        
        # Alertes
        if curr <= r[5]:
            try:
                bot.send_message(r[1], f"🚨 *STOP-LOSS ATTEINT*\n\n📊 {r[2]}\n💰 Prix : ${curr:.2f}\n🛑 Stop : ${r[5]}\n\n⚡ /vendre {r[2]}", parse_mode='Markdown')
            except: pass
        elif curr >= r[7]:
            try:
                bot.send_message(r[1], f"🎯 *TAKE-PROFIT 2 ATTEINT !*\n\n📊 {r[2]}\n💰 Prix : ${curr:.2f}\n📈 Gain : +{((curr/r[3])-1)*100:.1f}%\n\n💡 /vendre {r[2]}", parse_mode='Markdown')
            except: pass
        elif curr >= r[6]:
            try:
                bot.send_message(r[1], f"🎯 *TAKE-PROFIT 1 ATTEINT !*\n\n📊 {r[2]}\n💰 Prix : ${curr:.2f}\n📈 Gain : +{((curr/r[3])-1)*100:.1f}%\n\n💡 Pensez à sécuriser vos gains", parse_mode='Markdown')
            except: pass

def check_alerts():
    """Vérifie les alertes prix toutes les 5 minutes"""
    rows = db_fetchall("SELECT * FROM alerts WHERE active = 1")
    for r in rows:
        curr = get_current_price(r[2])
        if curr and curr >= r[3]:
            try:
                bot.send_message(r[1], f"🔔 *ALERTE PRIX ATTEINTE !*\n\n📊 {r[2]}\n💰 Prix actuel : ${curr:.2f}\n🎯 Cible : ${r[3]:.2f}\n\nL'alerte va être désactivée.", parse_mode='Markdown')
            except: pass
            db_execute('UPDATE alerts SET active = 0 WHERE id = ?', (r[0],))

def run_scheduler():
    """Planificateur de tâches"""
    schedule.every(15).minutes.do(auto_scan_breakouts)
    schedule.every(10).minutes.do(monitor_positions)
    schedule.every(5).minutes.do(check_alerts)
    while True:
        schedule.run_pending()
        time.sleep(10)

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
    return "🤖 Bot Trader Pro - Opérationnel", 200

# ===== DÉMARRAGE =====
if __name__ == '__main__':
    print("🤖 Démarrage du Bot Trader Pro...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"✅ Bot connecté sur {WEBHOOK_URL}")
    print("📊 Base de données SQLite initialisée")
    print("🔍 Auto-scan des breakouts toutes les 15 minutes")
    print("🛡️ Surveillance des positions toutes les 10 minutes")
    print("🔔 Vérification des alertes toutes les 5 minutes")
    print("✅ Prêt à trader !")
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.
