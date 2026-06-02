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

TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
DB_PATH = 'trading_bot.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ticker TEXT,
        buy_price REAL, quantity INTEGER, stop_loss REAL, take_profit1 REAL,
        take_profit2 REAL, highest_price REAL, status TEXT DEFAULT 'open', date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ticker TEXT,
        buy_price REAL, sell_price REAL, quantity INTEGER, profit_loss REAL,
        profit_loss_pct REAL, close_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ticker TEXT,
        target_price REAL, active INTEGER DEFAULT 1, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)''')
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
    return [r[0] for r in db_fetchall('SELECT chat_id FROM subscribers')]

WATCHLIST = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'PLTR', 'GME', 'AMC', 'MARA', 'RIOT', 'COIN', 'NIO', 'XPEV', 'LI',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'BABA', 'BIDU', 'JD',
    'MSTR', 'MRNA', 'PFE', 'BA', 'CCL', 'AAL', 'SPCE', 'NKLA',
    'SNDL', 'TLRY', 'ACB', 'CGC', 'FCEL', 'PLUG', 'QS', 'CHPT'
]

def get_stock_info(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return info.get('longName', ticker), info.get('isin', 'N/A')
    except:
        return ticker, 'N/A'

def get_current_price(ticker):
    try:
        hist = yf.Ticker(ticker).history(period='1d')
        return hist['Close'].iloc[-1] if not hist.empty else None
    except:
        return None

def get_analysis(ticker):
    try:
        df = yf.download(ticker, period='3mo', progress=False)
        if df.empty or len(df) < 20: return None
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()
        current = close.iloc[-1]
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] != 0 else 50
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9).mean()
        ma20 = close.rolling(20).mean().iloc[-1]
        ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else ma20
        avg_vol = volume.iloc[-20:].mean()
        vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 1
        high = df['High'].squeeze()
        low = df['Low'].squeeze()
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        score = 50
        if 30 <= rsi <= 50: score += 15
        if macd.iloc[-1] > signal.iloc[-1]: score += 10
        if current > ma20: score += 10
        if current > ma50: score += 5
        if vol_ratio > 1.5: score += 10
        score = min(100, max(0, score))
        name, isin = get_stock_info(ticker)
        return {'ticker': ticker, 'name': name, 'isin': isin, 'price': round(current, 2),
                'rsi': round(rsi, 1), 'macd': round(macd.iloc[-1], 3), 'macd_signal': round(signal.iloc[-1], 3),
                'ma20': round(ma20, 2), 'ma50': round(ma50, 2), 'atr': round(atr, 2),
                'vol_ratio': round(vol_ratio, 1), 'score': score,
                'stop_loss': round(current - 2*atr, 2), 'tp1': round(current + 2*atr, 2), 'tp2': round(current + 4*atr, 2)}
    except:
        return None

def send_alert_to_all(msg):
    for cid in get_all_subscribers():
        try: bot.send_message(cid, msg, parse_mode='Markdown')
        except: pass

@bot.message_handler(commands=['start'])
def cmd_start(message):
    add_subscriber(message.chat.id)
    bot.reply_to(message, "🚀 *Bot actif !*\n/analyse AAPL | /scan | /explosive | /aide", parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    bot.reply_to(message, "/analyse TICKER | /scan | /explosive | /marche | /portfolio | /vendre TICKER | /historique | /stats | /alerte TICKER PRIX | /import_csv", parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    try: ticker = message.text.split()[1].upper()
    except: bot.reply_to(message, "❌ /analyse AAPL"); return
    a = get_analysis(ticker)
    if not a: bot.reply_to(message, "❌ Donnees indisponibles"); return
    msg = f"📊 *{a['name']} ({a['ticker']})*\n🔖 ISIN: `{a['isin']}`\n⭐ Score: {a['score']}/100\n💰 ${a['price']}\n📈 RSI: {a['rsi']} | MACD: {a['macd']}\n🛑 Stop: ${a['stop_loss']} | TP1: ${a['tp1']}"
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(types.InlineKeyboardButton("🛒 1", callback_data=f"buy_{ticker}_{a['price']}_1"),
               types.InlineKeyboardButton("🛒 5", callback_data=f"buy_{ticker}_{a['price']}_5"),
               types.InlineKeyboardButton("🛒 10", callback_data=f"buy_{ticker}_{a['price']}_10"),
               types.InlineKeyboardButton("❌ Pass", callback_data="pass"))
    bot.reply_to(message, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy(call):
    _, ticker, price, qty = call.data.split('_')
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, f"🛒 Prix d'achat exact pour {ticker} ?", parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_buy, ticker, int(qty))

@bot.callback_query_handler(func=lambda call: call.data == 'pass')
def handle_pass(call):
    bot.answer_callback_query(call.id, "Ignore")

def process_buy(message, ticker, qty):
    try: price = float(message.text.replace(',', '.'))
    except: bot.reply_to(message, "❌ Invalide"); return
    a = get_analysis(ticker)
    stop = a['stop_loss'] if a else round(price*0.95, 2)
    tp1 = a['tp1'] if a else round(price*1.10, 2)
    tp2 = a['tp2'] if a else round(price*1.20, 2)
    db_execute('INSERT INTO positions (chat_id, ticker, buy_price, quantity, stop_loss, take_profit1, take_profit2, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?)',
               (message.chat.id, ticker, price, qty, stop, tp1, tp2, price, datetime.now().isoformat()))
    bot.reply_to(message, f"✅ *{ticker}* x{qty} a ${price:.2f}", parse_mode='Markdown')

@bot.message_handler(commands=['scan'])
def cmd_scan(message):
    bot.send_chat_action(message.chat.id, 'typing')
    breakouts = []
    for t in WATCHLIST:
        try:
            df = yf.download(t, period='5d', progress=False)
            if len(df) < 2: continue
            c, v = df['Close'].squeeze(), df['Volume'].squeeze()
            avg = v.iloc[:-1].mean()
            if avg > 0 and v.iloc[-1] > 3*avg and c.iloc[-1] > c.iloc[-2]:
                a = get_analysis(t)
                if a:
                    name, isin = get_stock_info(t)
                    breakouts.append({'ticker': t, 'name': name, 'isin': isin, 'price': c.iloc[-1], 'score': a['score'], 'stop': a['stop_loss'], 'tp1': a['tp1']})
        except: pass
        time.sleep(0.1)
    if not breakouts: bot.reply_to(message, "Aucun breakout"); return
    breakouts.sort(key=lambda x: x['score'], reverse=True)
    msg = "🔍 *BREAKOUTS*\n\n"
    for b in breakouts[:5]:
        msg += f"🚀 *{b['name']} ({b['ticker']})*\n🔖 `{b['isin']}`\n⭐ {b['score']}/100 | 💰 ${b['price']:.2f}\n🛑 Stop ${b['stop']}\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    res = [a for t in WATCHLIST if (a := get_analysis(t)) and a['score'] >= 60]
    res.sort(key=lambda x: x['score'], reverse=True)
    if not res: bot.reply_to(message, "Aucune"); return
    msg = "💥 *TOP*\n\n"
    for a in res[:5]: msg += f"*{a['name']}* ⭐{a['score']}/100 💰${a['price']}\n🔖 `{a['isin']}`\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marche'])
def cmd_marche(message):
    msg = "📈 *MARCHE*\n\n"
    for t, n in {'^GSPC': 'S&P500', '^IXIC': 'Nasdaq', '^DJI': 'Dow', '^VIX': 'VIX'}.items():
        try:
            h = yf.Ticker(t).history(period='2d')
            if len(h) >= 2:
                c, p = h['Close'].iloc[-1], h['Close'].iloc[-2]
                chg = (c-p)/p*100
                msg += f"{'🟢' if chg>0 else '🔴'} *{n}* {c:,.2f} ({chg:+.2f}%)\n"
        except: pass
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    rows = db_fetchall("SELECT * FROM positions WHERE chat_id=? AND status='open'", (message.chat.id,))
    if not rows: bot.reply_to(message, "📭 Vide"); return
    msg, ti, tc = "💼 *PORTEFEUILLE*\n\n", 0, 0
    for r in rows:
        curr = get_current_price(r[2]) or r[3]
        pnl = (curr - r[3]) * r[4]
        ti += r[3]*r[4]; tc += curr*r[4]
        msg += f"{'🟢' if pnl>=0 else '🔴'} *{r[2]}* x{r[4]}\n   ${r[3]:.2f} → ${curr:.2f} | ${pnl:.2f}\n\n"
    msg += f"━━━━━━\n💰 Total: ${tc:.2f} | P&L: ${tc-ti:.2f}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try: ticker = message.text.split()[1].upper()
    except: bot.reply_to(message, "❌ /vendre AAPL"); return
    row = db_fetchone("SELECT * FROM positions WHERE chat_id=? AND ticker=? AND status='open'", (message.chat.id, ticker))
    if not row: bot.reply_to(message, "❌ Pas de position"); return
    curr = get_current_price(ticker) or row[3]
    pnl = (curr - row[3]) * row[4]
    db_execute('INSERT INTO trades (chat_id, ticker, buy_price, sell_price, quantity, profit_loss, profit_loss_pct, close_date) VALUES (?,?,?,?,?,?,?,?)',
               (message.chat.id, ticker, row[3], curr, row[4], pnl, (curr/row[3]-1)*100, datetime.now().isoformat()))
    db_execute('UPDATE positions SET status=? WHERE id=?', ('closed', row[0]))
    bot.reply_to(message, f"✅ *{ticker}* vendu\n{'🟢' if pnl>=0 else '🔴'} P&L: ${pnl:.2f}", parse_mode='Markdown')

@bot.message_handler(commands=['historique'])
def cmd_historique(message):
    rows = db_fetchall('SELECT * FROM trades WHERE chat_id=? ORDER BY close_date DESC LIMIT 10', (message.chat.id,))
    if not rows: bot.reply_to(message, "📋 Vide"); return
    msg = "📋 *HISTORIQUE*\n\n"
    for r in rows: msg += f"{'🟢' if r[6]>=0 else '🔴'} *{r[2]}* x{r[5]} | ${r[6]:.2f} ({r[7]:+.1f}%)\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    rows = db_fetchall('SELECT profit_loss FROM trades WHERE chat_id=?', (message.chat.id,))
    if not rows: bot.reply_to(message, "📊 Pas de stats"); return
    wins = sum(1 for r in rows if r[0] > 0)
    total = sum(r[0] for r in rows)
    msg = f"📊 *STATS*\n📈 Trades: {len(rows)}\n✅ Win rate: {wins/len(rows)*100:.1f}%\n💰 P&L: ${total:.2f}\n🏆 Best: ${max(r[0] for r in rows):.2f}\n💀 Worst: ${min(r[0] for r in rows):.2f}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['alerte'])
def cmd_alerte(message):
    try:
        _, ticker, price = message.text.split()
        db_execute('INSERT INTO alerts (chat_id, ticker, target_price, date) VALUES (?,?,?,?)', (message.chat.id, ticker.upper(), float(price), datetime.now().isoformat()))
        bot.reply_to(message, f"🔔 Alerte {ticker.upper()} a ${price}")
    except: bot.reply_to(message, "❌ /alerte AAPL 200")

@bot.message_handler(commands=['import_csv'])
def cmd_import_csv(message):
    msg = bot.reply_to(message, "📁 Envoyez le CSV (Action,Quantite,Prix)")
    bot.register_next_step_handler(msg, lambda m: process_csv(m))

def process_csv(message):
    content = message.document and bot.download_file(bot.get_file(message.document.file_id).file_path).decode('utf-8') or message.text
    if not content: bot.reply_to(message, "❌ Vide"); return
    try:
        count = 0
        for row in csv.DictReader(io.StringIO(content)):
            t, q, p = row.get('Action', row.get('Ticker', '')), int(float(row.get('Quantite', row.get('Quantity', 0)))), float(row.get('Prix', row.get('Price', 0)))
            if t and q > 0 and p > 0:
                db_execute('INSERT INTO positions (chat_id, ticker, buy_price, quantity, stop_loss, take_profit1, take_profit2, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?)',
                           (message.chat.id, t.upper(), p, q, round(p*.95,2), round(p*1.1,2), round(p*1.2,2), p, datetime.now().isoformat()))
                count += 1
        bot.reply_to(message, f"✅ {count} positions importees")
    except Exception as e: bot.reply_to(message, f"❌ {e}")

def auto_scan():
    for t in WATCHLIST[:20]:
        try:
            df = yf.download(t, period='5d', progress=False)
            if len(df) < 2: continue
            c, v = df['Close'].squeeze(), df['Volume'].squeeze()
            if v.iloc[:-1].mean() > 0 and v.iloc[-1] > 3*v.iloc[:-1].mean() and c.iloc[-1] > c.iloc[-2]:
                a = get_analysis(t)
                if a and a['score'] >= 85:
                    n, i = get_stock_info(t)
                    send_alert_to_all(f"🔔 *BREAKOUT*\n{n} ({t})\n🔖 `{i}`\n⭐ {a['score']}/100\n💰 ${a['price']:.2f}")
        except: pass
        time.sleep(0.1)

def monitor():
    for r in db_fetchall("SELECT * FROM positions WHERE status='open'"):
        curr = get_current_price(r[2])
        if not curr: continue
        if curr > r[8]: db_execute('UPDATE positions SET highest_price=? WHERE id=?', (curr, r[0]))
        if curr >= r[3]*1.05:
            ns = round(curr*.97, 2)
            if ns > r[5]: db_execute('UPDATE positions SET stop_loss=? WHERE id=?', (ns, r[0]))
        if curr <= r[5]:
            try: bot.send_message(r[1], f"🚨 *STOP* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass

def run_scheduler():
    schedule.every(15).minutes.do(auto_scan)
    schedule.every(10).minutes.do(monitor)
    while True: schedule.run_pending(); time.sleep(10)

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
        return 'ok', 200
    return 'bad request', 400

@app.route('/')
def home():
    return "Bot OK", 200

if __name__ == '__main__':
    print("Demarrage...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"Bot sur {WEBHOOK_URL}")
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
