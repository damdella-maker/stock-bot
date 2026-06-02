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
from datetime import datetime, timedelta
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
DB_PATH = 'trading_bot.db'

# ===== BASE DE DONNEES =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ticker TEXT,
        buy_price REAL, quantity REAL, amount REAL,
        stop_loss REAL, take_profit1 REAL, take_profit2 REAL, take_profit3 REAL,
        highest_price REAL, status TEXT DEFAULT 'open', date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ticker TEXT,
        buy_price REAL, sell_price REAL, quantity REAL, amount REAL,
        profit_loss REAL, profit_loss_pct REAL, close_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ticker TEXT,
        target_price REAL, active INTEGER DEFAULT 1, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (
        chat_id INTEGER PRIMARY KEY, min_potential REAL DEFAULT 40,
        max_potential REAL DEFAULT 200)''')
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

# ===== WATCHLIST =====
HIGH_GROWTH_STOCKS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'CRM', 'ADBE', 'INTC', 'QCOM', 'AVGO', 'COIN', 'MARA', 'RIOT', 'MSTR',
    'PLTR', 'SOUN', 'NIO', 'XPEV', 'LI', 'RIVN', 'LCID', 'CHPT', 'PLUG',
    'MRNA', 'PFE', 'GME', 'AMC', 'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX',
    'BABA', 'JD', 'BIDU', 'SNDL', 'TLRY', 'ACB', 'CGC', 'BA', 'CCL', 'AAL',
    'UAL', 'DAL', 'CVNA', 'W', 'F', 'T', 'VZ', 'DIS', 'PYPL', 'SHOP'
]
HIGH_GROWTH_STOCKS = list(set(HIGH_GROWTH_STOCKS))

# ===== OUTILS =====
def get_stock_info(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return (info.get('longName', ticker), info.get('isin', 'N/A'),
                info.get('sector', 'N/A'), info.get('industry', 'N/A'),
                info.get('marketCap', 0))
    except:
        return (ticker, 'N/A', 'N/A', 'N/A', 0)

def get_current_price(ticker):
    try:
        hist = yf.Ticker(ticker).history(period='1d')
        return hist['Close'].iloc[-1] if not hist.empty else None
    except:
        return None

def get_detailed_analysis(ticker):
    try:
        df = yf.download(ticker, period='6mo', progress=False)
        if df.empty or len(df) < 20: return None
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()
        high = df['High'].squeeze()
        low = df['Low'].squeeze()
        current = close.iloc[-1]
        chg_1d = ((current - close.iloc[-2]) / close.iloc[-2]) * 100 if len(close) >= 2 else 0
        chg_5d = ((current - close.iloc[-5]) / close.iloc[-5]) * 100 if len(close) >= 5 else 0
        chg_1m = ((current - close.iloc[-20]) / close.iloc[-20]) * 100 if len(close) >= 20 else 0
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] != 0 else 50
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        macd_hist = macd_line - signal_line
        ma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_upper = ma20 + 2 * std20
        bb_lower = ma20 - 2 * std20
        bb_pos = ((current - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1])) * 100
        ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else ma20.iloc[-1]
        ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else ma50
        avg_vol = volume.iloc[-20:].mean()
        vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 1
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr / current) * 100
        recent_high = high.iloc[-20:].max()
        recent_low = low.iloc[-20:].min()
        score = 50
        if 40 <= rsi <= 60: score += 10
        elif 30 <= rsi < 40: score += 15
        elif rsi < 30: score += 20
        if macd_line.iloc[-1] > signal_line.iloc[-1]: score += 10
        if macd_hist.iloc[-1] > macd_hist.iloc[-2]: score += 5
        if current > ma20.iloc[-1]: score += 5
        if current > ma50: score += 5
        if ma20.iloc[-1] > ma50: score += 5
        if vol_ratio > 2: score += 10
        elif vol_ratio > 1.5: score += 5
        if chg_5d > 10: score += 5
        score = min(100, max(0, score))
        stop_loss = round(current - 2 * atr, 2)
        tp1 = round(current + 2 * atr, 2)
        tp2 = round(current + 4 * atr, 2)
        tp3 = round(current + 6 * atr, 2)
        risk = current - stop_loss
        reward = tp1 - current
        rr = round(reward / risk, 2) if risk > 0 else 0
        pot_10 = min(95, max(5, 70 if score > 60 else 40))
        pot_20 = min(85, max(5, 55 if score > 60 else 30))
        pot_30 = min(70, max(5, 40 if score > 60 else 20))
        name, isin, sector, industry, cap = get_stock_info(ticker)
        return {
            'ticker': ticker, 'name': name, 'isin': isin, 'sector': sector,
            'industry': industry, 'market_cap': cap, 'price': round(current, 2),
            'change_1d': round(chg_1d, 2), 'change_5d': round(chg_5d, 2),
            'change_1m': round(chg_1m, 2), 'rsi': round(rsi, 1),
            'macd': round(macd_line.iloc[-1], 3), 'macd_signal': round(signal_line.iloc[-1], 3),
            'macd_histogram': round(macd_hist.iloc[-1], 4),
            'bb_upper': round(bb_upper.iloc[-1], 2), 'bb_lower': round(bb_lower.iloc[-1], 2),
            'bb_position': round(bb_pos, 1), 'ma20': round(ma20.iloc[-1], 2),
            'ma50': round(ma50, 2), 'ma200': round(ma200, 2),
            'vol_ratio': round(vol_ratio, 1), 'atr': round(atr, 2),
            'atr_pct': round(atr_pct, 2), 'support': round(recent_low, 2),
            'resistance': round(recent_high, 2), 'score': score,
            'stop_loss': stop_loss, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'risk_reward': rr, 'potential_10': pot_10, 'potential_20': pot_20, 'potential_30': pot_30
        }
    except Exception as e:
        print(f"Erreur analyse {ticker}: {e}")
        return None

def scan_high_potential(min_change=20, max_change=200, min_score=40):
    results = []
    for t in HIGH_GROWTH_STOCKS:
        try:
            df = yf.download(t, period='5d', progress=False)
            if len(df) < 5: continue
            close = df['Close'].squeeze()
            chg = ((close.iloc[-1] - close.iloc[-5]) / close.iloc[-5]) * 100
            if chg >= min_change:
                a = get_detailed_analysis(t)
                if a and a['score'] >= min_score:
                    results.append(a)
        except: pass
        time.sleep(0.1)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results

def send_alert_to_all(msg):
    for cid in get_all_subscribers():
        try: bot.send_message(cid, msg, parse_mode='Markdown')
        except: pass

# ===== COMMANDES =====
@bot.message_handler(commands=['start'])
def cmd_start(message):
    add_subscriber(message.chat.id)
    msg = """
🚀 *TRADER PRO V4*

✅ *Actif avec succes !*

📊 /scan - Scanner le marche
📊 /scan20 - Potentiel 20-50%
📊 /scan50 - Potentiel >50%
📊 /analyse TICKER - Rapport complet
💥 /explosive - Top opportunites
📈 /marche - Indices
💼 /portfolio - Positions
📋 /historique - Trades
📊 /stats - Performance
/setrange MIN MAX - Votre filtre
/aide - Guide complet
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """
📚 *GUIDE*

*/scan* - Toutes actions >20%
*/scan20* - Potentiel 20-50%
*/scan50* - Potentiel >50%
*/analyse TICKER* - Rapport detaille
*/explosive* - Top 5 scores
*/setrange 30 100* - Filtrer

🔔 Notifs auto toutes les 15min
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['setrange'])
def cmd_setrange(message):
    try:
        _, mn, mx = message.text.split()
        mn, mx = float(mn), float(mx)
        db_execute('UPDATE subscribers SET min_potential=?, max_potential=? WHERE chat_id=?', (mn, mx, message.chat.id))
        bot.reply_to(message, f"✅ Filtre : {mn}% - {mx}%")
    except:
        bot.reply_to(message, "❌ /setrange 30 100")

@bot.message_handler(commands=['scan', 'scan20', 'scan50'])
def cmd_scan(message):
    cmd = message.text.split()[0].lower()
    if cmd == '/scan20': mn, mx = 20, 50
    elif cmd == '/scan50': mn, mx = 50, 500
    else:
        prefs = db_fetchone('SELECT min_potential, max_potential FROM subscribers WHERE chat_id=?', (message.chat.id,))
        mn, mx = prefs if prefs else (20, 500)
    
    bot.send_chat_action(message.chat.id, 'typing')
    results = scan_high_potential(mn, mx)
    
    if not results:
        bot.reply_to(message, f"🔍 Aucune action trouvee entre {mn}% et {mx}%")
        return
    
    msg = f"🔍 *SCAN {mn}%-{mx}%*\n\n"
    for i, a in enumerate(results[:10], 1):
        msg += f"*{i}. {a['name']} ({a['ticker']})*\n"
        msg += f"   ⭐ {a['score']}/100 | 💰 ${a['price']}\n"
        msg += f"   📈 +{a['change_5d']}% sur 5j\n"
        msg += f"   🛑 Stop ${a['stop_loss']} | 🎯 TP1 ${a['tp1']}\n\n"
    
    msg += "/analyse TICKER pour le rapport complet"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    
    bot.send_chat_action(message.chat.id, 'typing')
    a = get_detailed_analysis(ticker)
    
    if not a:
        bot.reply_to(message, f"❌ Donnees indisponibles pour {ticker}")
        return
    
    msg = f"""📊 *{a['name']} ({a['ticker']})*
🔖 ISIN : `{a['isin']}`
🏭 {a['sector']} | {a['industry']}

⭐ *Score Technique : {a['score']}/100*
💰 Prix : ${a['price']}

📈 *Performance :*
• 1j : {a['change_1d']:+.2f}% | 5j : {a['change_5d']:+.2f}% | 1m : {a['change_1m']:+.2f}%

📊 *Indicateurs :*
• RSI : {a['rsi']}
• MACD : {a['macd']} | Signal : {a['macd_signal']}
• Bollinger : ${a['bb_lower']} - ${a['bb_upper']}
• MA20 : ${a['ma20']} | MA50 : ${a['ma50']}

📐 *Supports/Resistances :*
• Support : ${a['support']}
• Resistance : ${a['resistance']}

📏 *Volatilite :*
• ATR : ${a['atr']} ({a['atr_pct']}%)
• Volume : {a['vol_ratio']}x moyenne

🛡️ *Gestion du Risque :*
• 🛑 Stop-Loss : ${a['stop_loss']}
• 🎯 TP1 : ${a['tp1']} | TP2 : ${a['tp2']} | TP3 : ${a['tp3']}
• ⚖️ Ratio R/R : {a['risk_reward']}

🎲 *Potentiel Estime :*
• +10% : {a['potential_10']}%
• +20% : {a['potential_20']}%
• +30% : {a['potential_30']}%

⚠️ Estimations - Pas de garantie"""

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🛒 ACHETER", callback_data=f"buy_{ticker}_{a['price']}"),
        types.InlineKeyboardButton("❌ PASSER", callback_data="pass")
    )
    
    bot.reply_to(message, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy(call):
    _, ticker, price = call.data.split('_')
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        f"🛒 *Achat {ticker}*\nPrix actuel : ${float(price):.2f}\n\n💰 *Quel montant investir ?* (ex: 100)",
        parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_amount, ticker)

@bot.callback_query_handler(func=lambda call: call.data == 'pass')
def handle_pass(call):
    bot.answer_callback_query(call.id, "Ignore")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

def process_amount(message, ticker):
    try:
        amount = float(message.text.replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Invalide"); return
    curr = get_current_price(ticker) or 0
    msg = bot.send_message(message.chat.id,
        f"💰 Montant : {amount}€\n💵 Prix actuel : ${curr:.2f}\n\n📝 *Prix d'achat exact ?*",
        parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_price, ticker, amount)

def process_price(message, ticker, amount):
    try:
        buy_price = float(message.text.replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Invalide"); return
    qty = amount / buy_price
    a = get_detailed_analysis(ticker)
    stop = a['stop_loss'] if a else round(buy_price * 0.95, 2)
    tp1 = a['tp1'] if a else round(buy_price * 1.10, 2)
    tp2 = a['tp2'] if a else round(buy_price * 1.20, 2)
    tp3 = a['tp3'] if a else round(buy_price * 1.30, 2)
    db_execute('INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (message.chat.id, ticker, buy_price, qty, amount, stop, tp1, tp2, tp3, buy_price, datetime.now().isoformat()))
    loss = amount - (stop / buy_price * amount)
    msg = f"""✅ *POSITION OUVERTE*
📊 *{ticker}*
💰 {amount:.2f}€ | ${buy_price:.2f}
📦 {qty:.4f} actions
🛑 Stop ${stop} (perte max {loss:.2f}€)
🎯 TP1 ${tp1} | TP2 ${tp2} | TP3 ${tp3}"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    res = [a for t in HIGH_GROWTH_STOCKS if (a := get_detailed_analysis(t)) and a['score'] >= 60]
    res.sort(key=lambda x: x['score'], reverse=True)
    if not res: bot.reply_to(message, "Aucune"); return
    msg = "💥 *TOP OPPORTUNITES*\n\n"
    for a in res[:5]:
        msg += f"*{a['name']}* ⭐{a['score']}/100 💰${a['price']}\n🔖 `{a['isin']}`\n\n"
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
    msg = "💼 *PORTEFEUILLE*\n\n"; ti = tc = 0
    for r in rows:
        curr = get_current_price(r[2]) or r[3]
        val = r[4] * (curr / r[3])
        pnl = val - r[5]
        ti += r[5]; tc += val
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg += f"{emoji} *{r[2]}* {r[4]:.4f} act\n   {r[5]:.2f}€ → {val:.2f}€ | {pnl:+.2f}€\n\n"
    msg += f"━━━━━━\n💰 Total: {tc:.2f}€ | P&L: {tc-ti:+.2f}€"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try: ticker = message.text.split()[1].upper()
    except: bot.reply_to(message, "❌ /vendre AAPL"); return
    row = db_fetchone("SELECT * FROM positions WHERE chat_id=? AND ticker=? AND status='open'", (message.chat.id, ticker))
    if not row: bot.reply_to(message, "❌ Pas de position"); return
    curr = get_current_price(ticker) or row[3]
    val = row[4] * (curr / row[3])
    pnl = val - row[5]
    db_execute('INSERT INTO trades (chat_id, ticker, buy_price, sell_price, quantity, amount, profit_loss, profit_loss_pct, close_date) VALUES (?,?,?,?,?,?,?,?,?)',
               (message.chat.id, ticker, row[3], curr, row[4], row[5], pnl, (curr/row[3]-1)*100, datetime.now().isoformat()))
    db_execute('UPDATE positions SET status=? WHERE id=?', ('closed', row[0]))
    bot.reply_to(message, f"✅ *{ticker}* vendu\n{'🟢' if pnl>=0 else '🔴'} P&L: {pnl:+.2f}€", parse_mode='Markdown')

@bot.message_handler(commands=['historique'])
def cmd_historique(message):
    rows = db_fetchall('SELECT * FROM trades WHERE chat_id=? ORDER BY close_date DESC LIMIT 10', (message.chat.id,))
    if not rows: bot.reply_to(message, "📋 Vide"); return
    msg = "📋 *HISTORIQUE*\n\n"
    for r in rows: msg += f"{'🟢' if r[7]>=0 else '🔴'} *{r[2]}* {r[7]:+.1f}% | {r[6]:+.2f}€\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    rows = db_fetchall('SELECT profit_loss FROM trades WHERE chat_id=?', (message.chat.id,))
    if not rows: bot.reply_to(message, "📊 Pas de stats"); return
    wins = sum(1 for r in rows if r[0] > 0)
    total = sum(r[0] for r in rows)
    msg = f"📊 *STATS*\n📈 Trades: {len(rows)}\n✅ Win: {wins/len(rows)*100:.1f}%\n💰 P&L: {total:.2f}€\n🏆 Best: {max(r[0] for r in rows):.2f}€\n💀 Worst: {min(r[0] for r in rows):.2f}€"
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
    bot.register_next_step_handler(msg, process_csv)

def process_csv(message):
    content = message.document and bot.download_file(bot.get_file(message.document.file_id).file_path).decode('utf-8') or message.text
    if not content: bot.reply_to(message, "❌ Vide"); return
    try:
        count = 0
        for row in csv.DictReader(io.StringIO(content)):
            t = row.get('Action', row.get('Ticker', ''))
            q = float(row.get('Quantite', row.get('Quantity', 0)))
            p = float(row.get('Prix', row.get('Price', 0)))
            if t and q > 0 and p > 0:
                amt = q * p
                db_execute('INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (message.chat.id, t.upper(), p, q, amt, round(p*.95,2), round(p*1.1,2), round(p*1.2,2), round(p*1.3,2), p, datetime.now().isoformat()))
                count += 1
        bot.reply_to(message, f"✅ {count} positions importees")
    except Exception as e: bot.reply_to(message, f"❌ {e}")

# ===== SURVEILLANCE AUTO =====
def auto_scan():
    results = scan_high_potential(40, 500, 60)
    if results:
        msg = "🔔 *ALERTE AUTO - FORT POTENTIEL*\n\n"
        for a in results[:3]:
            msg += f"🔥 *{a['name']}* +{a['change_5d']}%\n⭐ {a['score']}/100 | 💰 ${a['price']}\n🛑 Stop ${a['stop_loss']}\n\n"
        send_alert_to_all(msg)

def monitor():
    for r in db_fetchall("SELECT * FROM positions WHERE status='open'"):
        curr = get_current_price(r[2])
        if not curr: continue
        if curr > r[9]: db_execute('UPDATE positions SET highest_price=? WHERE id=?', (curr, r[0]))
        if curr >= r[3] * 1.05:
            ns = round(curr * 0.97, 2)
            if ns > r[5]: db_execute('UPDATE positions SET stop_loss=? WHERE id=?', (ns, r[0]))
        if curr <= r[5]:
            try: bot.send_message(r[1], f"🚨 *STOP* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass
        elif curr >= r[8]:
            try: bot.send_message(r[1], f"🎯 *TP3* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass
        elif curr >= r[7]:
            try: bot.send_message(r[1], f"🎯 *TP2* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass
        elif curr >= r[6]:
            try: bot.send_message(r[1], f"🎯 *TP1* {r[2]} ${curr:.2f}", parse_mode='Markdown')
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
    return "Bot Trader Pro v4 - OK", 200

if __name__ == '__main__':
    print("Demarrage Bot Trader Pro v4...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"Bot sur {WEBHOOK_URL}")
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
