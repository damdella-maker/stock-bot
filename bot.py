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

# ===== BASE DE DONNÉES SIMPLE =====
DB_PATH = 'trading_bot.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        ticker TEXT,
        buy_price REAL,
        quantity INTEGER,
        stop_loss REAL,
        take_profit1 REAL,
        take_profit2 REAL,
        highest_price REAL,
        status TEXT DEFAULT 'open',
        date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        ticker TEXT,
        buy_price REAL,
        sell_price REAL,
        quantity INTEGER,
        profit_loss REAL,
        profit_loss_pct REAL,
        close_date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        ticker TEXT,
        target_price REAL,
        active INTEGER DEFAULT 1,
        date TEXT
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

# ===== LISTE D'ACTIONS =====
WATCHLIST = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'SQ', 'ROKU', 'ZM', 'CRWD', 'PLTR', 'GME', 'AMC',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN', 'NIO', 'XPEV', 'LI',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'BABA', 'BIDU', 'JD',
    'MSTR', 'MRNA', 'PFE', 'BA', 'CCL', 'AAL', 'SPCE', 'NKLA',
    'SNDL', 'TLRY', 'ACB', 'CGC', 'FCEL', 'PLUG', 'QS', 'CHPT',
    'DDOG', 'SNOW', 'MDB', 'ZS', 'NET', 'FSLY', 'U'
]

# ===== OUTILS =====
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
    try:
        df = yf.download(ticker, period='3mo', progress=False)
        if df.empty:
            return None
        
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()
        current_price = close.iloc[-1]
        
        # RSI simple
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1])) if loss.iloc[-1] != 0 else 50
        
        # MACD simple
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
        
        # Score
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
        
        return {
            'ticker': ticker,
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

# ===== COMMANDES =====
@bot.message_handler(commands=['start'])
def cmd_start(message):
    msg = """🚀 *TRADER PRO BOT*

✅ Bot activé !

*Commandes :*
📊 /analyse TICKER - Analyse technique
💥 /explosive - Top opportunités
🔍 /scan - Détection breakouts
📈 /marché - Indices en direct
💼 /portfolio - Positions
📋 /historique - Historique
📊 /stats - Statistiques
🔔 /alerte TICKER PRIX
📢 /alertes - Alertes actives
📁 /import_csv - Import Trading212
📚 /aide - Guide complet"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """📚 *GUIDE*

*/analyse TICKER* → RSI, MACD, MA, Stop/TP
*/explosive* → Top 5 scores
*/scan* → Breakouts volume x3
*/marché* → S&P500, Nasdaq, VIX
*/portfolio* → Positions + P&L
*/vendre TICKER* → Fermer position
*/historique* → 10 derniers trades
*/stats* → Win rate, P&L total
*/alerte TICKER PRIX* → Alerte prix
*/import_csv* → Import CSV"""
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
        bot.reply_to(message, f"❌ Données indisponibles pour {ticker}")
        return
    
    msg = f"""📊 *ANALYSE {ticker}*

⭐ Score : {a['score']}/100
💰 Prix : ${a['price']}

📈 *Indicateurs :*
• RSI : {a['rsi']}
• MACD : {a['macd']} | Signal : {a['macd_signal']}
• MA20 : ${a['ma20']} | MA50 : ${a['ma50']}
• Volume : {a['vol_ratio']}x moyenne

🛡️ *Risque :*
• 🛑 Stop : ${a['stop_loss']}
• 🎯 TP1 : ${a['tp1']}
• 🎯 TP2 : ${a['tp2']}"""
    
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
    bot.answer_callback_query(call.id, "Ignoré")
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

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    results = []
    for t in WATCHLIST[:40]:
        a = get_analysis(t)
        if a and a['score'] >= 60:
            results.append(a)
        time.sleep(0.2)
    results.sort(key=lambda x: x['score'], reverse=True)
    
    if not results:
        bot.reply_to(message, "Aucune opportunité forte.")
        return
    
    msg = "💥 *TOP OPPORTUNITÉS*\n\n"
    for i, a in enumerate(results[:5], 1):
        msg += f"*{i}. {a['ticker']}* - {a['score']}/100\n"
        msg += f"   ${a['price']} | RSI {a['rsi']} | Vol {a['vol_ratio']}x\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['scan'])
def cmd_scan(message):
    bot.send_chat_action(message.chat.id, 'typing')
    breakouts = []
    for t in WATCHLIST[:30]:
        try:
            df = yf.download(t, period='5d', progress=False)
            if len(df) < 2: continue
            close = df['Close'].squeeze()
            vol = df['Volume'].squeeze()
            avg_vol = vol.iloc[:-1].mean()
            if avg_vol > 0 and vol.iloc[-1] > 3 * avg_vol and close.iloc[-1] > close.iloc[-2]:
                a = get_analysis(t)
                if a:
                    breakouts.append({'ticker': t, 'price': close.iloc[-1], 'vol_ratio': round(vol.iloc[-1]/avg_vol, 1), 'score': a['score']})
        except:
            pass
        time.sleep(0.2)
    
    if not breakouts:
        bot.reply_to(message, "Aucun breakout détecté.")
        return
    
    breakouts.sort(key=lambda x: x['score'], reverse=True)
    msg = "🔍 *BREAKOUTS*\n\n"
    for b in breakouts[:5]:
        msg += f"🚀 *{b['ticker']}* - {b['score']}/100\n   Vol ×{b['vol_ratio']} | ${b['price']:.2f}\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marché'])
def cmd_marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    indices = {'^GSPC': 'S&P 500', '^IXIC': 'Nasdaq', '^DJI': 'Dow Jones', '^VIX': 'VIX'}
    msg = "📈 *MARCHÉ*\n\n"
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
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    rows = db_fetchall('SELECT * FROM positions WHERE chat_id = ? AND status = ?', (message.chat.id, 'open'))
    if not rows:
        bot.reply_to(message, "📭 Portefeuille vide.")
        return
    
    msg = "💼 *PORTEFEUILLE*\n\n"
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
        msg += f"{emoji} *{ticker}* ×{qty}\n   Achat ${buy:.2f} | Actuel ${curr:.2f}\n   P&L ${pnl:.2f} ({pnl_pct:+.1f}%)\n\n"
    
    total_pnl = total_cur - total_inv
    msg += f"━━━━━━━━━━\n💰 Total : ${total_cur:.2f} | P&L ${total_pnl:.2f}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /vendre TICKER")
        return
    
    row = db_fetchone('SELECT * FROM positions WHERE chat_id = ? AND ticker = ? AND status = ?',
                      (message.chat.id, ticker, 'open'))
    if not row:
        bot.reply_to(message, f"❌ Pas de position {ticker}")
        return
    
    curr = get_current_price(ticker) or row[3]
    pnl = (curr - row[3]) * row[4]
    pnl_pct = ((curr / row[3]) - 1) * 100
    
    db_execute('INSERT INTO trades (chat_id, ticker, buy_price, sell_price, quantity, profit_loss, profit_loss_pct, close_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
               (message.chat.id, ticker, row[3], curr, row[4], pnl, pnl_pct, datetime.now().isoformat()))
    db_execute('UPDATE positions SET status = ? WHERE id = ?', ('closed', row[0]))
    
    emoji = "🟢" if pnl >= 0 else "🔴"
    bot.reply_to(message, f"✅ *{ticker}* vendu\n{emoji} P&L ${pnl:.2f} ({pnl_pct:+.1f}%)", parse_mode='Markdown')

@bot.message_handler(commands=['historique'])
def cmd_historique(message):
    rows = db_fetchall('SELECT * FROM trades WHERE chat_id = ? ORDER BY close_date DESC LIMIT 10', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📋 Aucun trade.")
        return
    
    msg = "📋 *HISTORIQUE*\n\n"
    for r in rows:
        emoji = "🟢" if r[6] >= 0 else "🔴"
        msg += f"{emoji} *{r[2]}* ×{r[5]}\n   ${r[3]:.2f} → ${r[4]:.2f} | {r[6]:+.1f}%\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    rows = db_fetchall('SELECT profit_loss, profit_loss_pct FROM trades WHERE chat_id = ?', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📊 Pas de stats.")
        return
    
    total_trades = len(rows)
    wins = sum(1 for r in rows if r[0] > 0)
    total_pnl = sum(r[0] for r in rows)
    best = max(r[0] for r in rows)
    worst = min(r[0] for r in rows)
    win_rate = (wins / total_trades) * 100
    
    msg = f"""📊 *STATS*

📈 Trades : {total_trades}
✅ Win rate : {win_rate:.1f}%
💰 P&L Total : ${total_pnl:.2f}
🏆 Meilleur : ${best:.2f}
💀 Pire : ${worst:.2f}"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['alerte'])
def cmd_alerte(message):
    try:
        parts = message.text.split()
        ticker = parts[1].upper()
        target = float(parts[2])
    except:
        bot.reply_to(message, "❌ /alerte AAPL 200")
        return
    db_execute('INSERT INTO alerts (chat_id, ticker, target_price, date) VALUES (?, ?, ?, ?)',
               (message.chat.id, ticker, target, datetime.now().isoformat()))
    bot.reply_to(message, f"🔔 Alerte {ticker} à ${target:.2f}")

@bot.message_handler(commands=['alertes'])
def cmd_alertes(message):
    rows = db_fetchall('SELECT * FROM alerts WHERE chat_id = ? AND active = 1', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📢 Aucune alerte.")
        return
    msg = "📢 *ALERTES*\n\n"
    for r in rows:
        curr = get_current_price(r[2])
        msg += f"🔔 *{r[2]}* → ${r[3]:.2f} (Actuel: ${curr:.2f})\n" if curr else f"🔔 *{r[2]}* → ${r[3]:.2f}\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['import_csv'])
def cmd_import_csv(message):
    msg = bot.reply_to(message, "📁 Envoyez votre CSV Trading212\n\nFormat : Action,Quantité,Prix\nAAPL,10,150.50")
    bot.register_next_step_handler(msg, process_csv)

def process_csv(message):
    content = message.text
    if message.document:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        content = downloaded.decode('utf-8')
    
    if not content:
        bot.reply_to(message, "❌ Vide.")
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
        bot.reply_to(message, f"✅ {count} positions importées !")
    except Exception as e:
        bot.reply_to(message, f"❌ Erreur : {str(e)}")

# ===== SURVEILLANCE =====
def monitor():
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
                bot.send_message(r[1], f"🚨 *STOP LOSS* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass
        elif curr >= r[7]:
            try:
                bot.send_message(r[1], f"🎯 *TP2 ATTEINT* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass
        elif curr >= r[6]:
            try:
                bot.send_message(r[1], f"🎯 *TP1 ATTEINT* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass

def check_alerts():
    rows = db_fetchall("SELECT * FROM alerts WHERE active = 1")
    for r in rows:
        curr = get_current_price(r[2])
        if curr and curr >= r[3]:
            try:
                bot.send_message(r[1], f"🔔 *ALERTE* {r[2]} a atteint ${r[3]:.2f} (${curr:.2f})", parse_mode='Markdown')
            except:
                pass
            db_execute('UPDATE alerts SET active = 0 WHERE id = ?', (r[0],))

def run_scheduler():
    schedule.every(10).minutes.do(monitor)
    schedule.every(5).minutes.do(check_alerts)
    while True:
        schedule.run_pending()
        time.sleep(10)

# ===== FLASK =====
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
    return "🤖 Bot OK", 200

# ===== START =====
if __name__ == '__main__':
    print("🤖 Démarrage...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"✅ Bot sur {WEBHOOK_URL}")
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
