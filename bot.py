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
        chat_id INTEGER PRIMARY KEY, min_potential REAL DEFAULT 5,
        max_potential REAL DEFAULT 500)''')
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

# ===== WATCHLIST OPTIMISEE TRADING212 =====
HIGH_GROWTH_STOCKS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'CRM', 'ADBE', 'INTC', 'QCOM', 'AVGO', 'COIN', 'MARA', 'RIOT', 'MSTR',
    'PLTR', 'SOUN', 'NIO', 'XPEV', 'LI', 'RIVN', 'LCID', 'CHPT', 'PLUG',
    'MRNA', 'PFE', 'GME', 'AMC', 'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX',
    'BABA', 'JD', 'BIDU', 'SNDL', 'TLRY', 'ACB', 'CGC', 'BA', 'CCL', 'AAL',
    'UAL', 'DAL', 'CVNA', 'W', 'F', 'T', 'VZ', 'DIS', 'PYPL', 'SHOP',
    'SNAP', 'UBER', 'ZM', 'CRWD', 'DDOG', 'SNOW', 'MDB', 'ZS', 'NET',
    'FSLY', 'U', 'DASH', 'ABNB', 'PTON', 'BYND', 'DKNG', 'PINS'
]
HIGH_GROWTH_STOCKS = list(set(HIGH_GROWTH_STOCKS))

# ===== OUTILS RAPIDES =====
def get_stock_info_fast(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return (info.get('longName', info.get('shortName', ticker)),
                info.get('isin', 'N/A'),
                info.get('sector', 'N/A'),
                info.get('industry', 'N/A'),
                info.get('marketCap', 0))
    except:
        return (ticker, 'N/A', 'N/A', 'N/A', 0)

def get_current_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return info.get('currentPrice', info.get('regularMarketPrice', info.get('ask', 0)))
    except:
        return None

def get_fast_analysis(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        current_price = info.get('currentPrice', info.get('regularMarketPrice', info.get('ask', 0)))
        if not current_price or current_price == 0:
            return None
        
        previous_close = info.get('previousClose', current_price)
        change_pct = ((current_price - previous_close) / previous_close) * 100
        
        volume = info.get('volume', 0)
        avg_volume = info.get('averageVolume', 0)
        vol_ratio = volume / avg_volume if avg_volume > 0 else 1
        
        df = yf.download(ticker, period='5d', progress=False)
        if df.empty or len(df) < 2:
            return None
        
        close = df['Close'].squeeze()
        
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(min(14, len(close))).mean()
        loss = -delta.where(delta < 0, 0).rolling(min(14, len(close))).mean()
        rsi = 100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] != 0 else 50
        
        ema12 = close.ewm(span=min(12, len(close))).mean()
        ema26 = close.ewm(span=min(26, len(close))).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=min(9, len(close))).mean()
        
        ma20 = close.rolling(min(20, len(close))).mean().iloc[-1]
        
        score = 50
        if rsi < 70: score += 10
        if rsi > 30: score += 5
        if macd_line.iloc[-1] > signal_line.iloc[-1]: score += 10
        if current_price > ma20: score += 10
        if vol_ratio > 1: score += 10
        if change_pct > 0: score += 5
        score = min(100, score)
        
        df_atr = yf.download(ticker, period='2wk', progress=False)
        if not df_atr.empty and len(df_atr) >= 5:
            high = df_atr['High'].squeeze()
            low = df_atr['Low'].squeeze()
            close_atr = df_atr['Close'].squeeze()
            tr = pd.concat([high - low, (high - close_atr.shift()).abs(), (low - close_atr.shift()).abs()], axis=1).max(axis=1)
            atr = tr.rolling(min(14, len(tr))).mean().iloc[-1]
        else:
            atr = current_price * 0.03
        
        stop_loss = round(current_price - 2 * atr, 2)
        tp1 = round(current_price + 2 * atr, 2)
        tp2 = round(current_price + 4 * atr, 2)
        tp3 = round(current_price + 6 * atr, 2)
        
        risk = current_price - stop_loss
        reward = tp1 - current_price
        rr = round(reward / risk, 2) if risk > 0 else 0
        
        name, isin, sector, industry, cap = get_stock_info_fast(ticker)
        
        return {
            'ticker': ticker, 'name': name, 'isin': isin, 'sector': sector,
            'industry': industry, 'market_cap': cap, 'price': round(current_price, 2),
            'change_pct': round(change_pct, 2), 'rsi': round(rsi, 1),
            'macd': round(macd_line.iloc[-1], 3), 'macd_signal': round(signal_line.iloc[-1], 3),
            'ma20': round(ma20, 2), 'vol_ratio': round(vol_ratio, 1),
            'volume': volume, 'avg_volume': avg_volume,
            'atr': round(atr, 2), 'score': score,
            'stop_loss': stop_loss, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'risk_reward': rr,
            'potential_10': min(95, max(5, 70 if score > 60 else 40)),
            'potential_20': min(85, max(5, 55 if score > 60 else 30)),
            'potential_30': min(70, max(5, 40 if score > 60 else 20))
        }
    except Exception as e:
        return None

def scan_fast(min_change=5, min_score=30):
    results = []
    for ticker in HIGH_GROWTH_STOCKS[:40]:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', current)
            if not current or current == 0: continue
            change = ((current - previous) / previous) * 100
            volume = info.get('volume', 0)
            avg_volume = info.get('averageVolume', 0)
            if avg_volume < 50000: continue
            if change >= min_change:
                a = get_fast_analysis(ticker)
                if a and a['score'] >= min_score:
                    results.append(a)
        except:
            pass
        time.sleep(0.05)
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
🚀 *TRADER PRO V5*

✅ *Bot actif !*

📊 /rapide - Top gainers instantane
📊 /scan - Scan filtre personnalise
📊 /scan20 - Potentiel 20-50%
📊 /scan50 - Potentiel >50%
📊 /analyse TICKER - Rapport complet
💥 /explosive - Top opportunites
📈 /marche - Indices en direct
💼 /portfolio - Positions
📋 /historique - Trades fermes
📊 /stats - Performance
🔔 /alerte TICKER PRIX
📁 /import_csv - Import Trading212
/setrange MIN MAX - Votre filtre
/aide - Guide complet
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """
📚 *GUIDE*

*/rapide* - Top gainers facon T212
*/scan* - Scan avec votre filtre
*/scan20* - Potentiel 20-50%
*/scan50* - Potentiel >50%
*/analyse TICKER* - Rapport detaille
*/explosive* - Top 5 scores
*/marche* - Indices + top mouvements
/setrange 10 100 - Votre filtre

🛒 Achat : entrez montant + prix exact
📦 Quantite calculee au centieme

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
        bot.reply_to(message, "❌ /setrange 10 100")

@bot.message_handler(commands=['rapide'])
def cmd_rapide(message):
    bot.send_chat_action(message.chat.id, 'typing')
    movers = []
    for ticker in HIGH_GROWTH_STOCKS[:30]:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', current)
            if current and previous and current > 0:
                change = ((current - previous) / previous) * 100
                volume = info.get('volume', 0)
                if volume > 100000:
                    movers.append({'ticker': ticker, 'name': info.get('shortName', ticker),
                                   'price': current, 'change': round(change, 2), 'volume': volume})
        except:
            pass
        time.sleep(0.03)
    
    movers.sort(key=lambda x: x['change'], reverse=True)
    
    if not movers:
        bot.reply_to(message, "Aucun mouvement detecte")
        return
    
    msg = "⚡ *TOP GAINERS DU JOUR*\n\n"
    for i, m in enumerate(movers[:10], 1):
        emoji = "🟢" if m['change'] > 0 else "🔴"
        bar = "█" * min(10, int(abs(m['change']) / 5))
        msg += f"{i}. {emoji} *{m['ticker']}*\n"
        msg += f"   ${m['price']:.2f} | {m['change']:+.1f}%\n"
        msg += f"   `{bar}` | Vol: {m['volume']:,}\n\n"
    msg += "📊 /analyse TICKER pour le detail"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['scan', 'scan20', 'scan50'])
def cmd_scan(message):
    cmd = message.text.split()[0].lower()
    if cmd == '/scan20': mn, mx = 20, 50
    elif cmd == '/scan50': mn, mx = 50, 500
    else:
        prefs = db_fetchone('SELECT min_potential, max_potential FROM subscribers WHERE chat_id=?', (message.chat.id,))
        mn, mx = prefs if prefs else (5, 500)
    
    bot.send_chat_action(message.chat.id, 'typing')
    wait_msg = bot.reply_to(message, "🔍 *Scan en cours...*", parse_mode='Markdown')
    
    results = scan_fast(mn, min_score=30)
    
    if not results:
        bot.edit_message_text(
            f"🔍 *Aucune action trouvee > {mn}%*\n\n"
            "💡 /scan20 | /scan50 | /rapide",
            chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown'
        )
        return
    
    msg = f"🔥 *SCAN > {mn}% - {len(results)} TROUVES*\n\n"
    for i, a in enumerate(results[:8], 1):
        stars = "⭐" * min(5, a['score'] // 20 + 1)
        msg += f"*{i}. {a['name']} ({a['ticker']})*\n"
        msg += f"   {stars} Score: {a['score']}/100\n"
        msg += f"   💰 ${a['price']} +{a['change_pct']}%\n"
        msg += f"   📊 Vol: {a['vol_ratio']}x | RSI: {a['rsi']}\n"
        msg += f"   🛑 Stop: ${a['stop_loss']} | TP1: ${a['tp1']}\n"
        msg += f"   🎲 Potentiel +10%: {a['potential_10']}%\n\n"
    msg += "📊 /analyse TICKER pour le rapport complet"
    
    bot.edit_message_text(msg, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    
    bot.send_chat_action(message.chat.id, 'typing')
    a = get_fast_analysis(ticker)
    
    if not a:
        bot.reply_to(message, f"❌ Donnees indisponibles pour {ticker}")
        return
    
    msg = f"""📊 *{a['name']} ({a['ticker']})*
🔖 ISIN : `{a['isin']}`
🏭 {a['sector']} | {a['industry']}

⭐ *Score Technique : {a['score']}/100*
💰 Prix : ${a['price']}
📈 Variation : {a['change_pct']:+.2f}%

📊 *Indicateurs :*
• RSI : {a['rsi']}
• MACD : {a['macd']} | Signal : {a['macd_signal']}
• MA20 : ${a['ma20']}
• Volume : {a['vol_ratio']}x moyenne ({a['volume']:,})

📏 *Volatilite :*
• ATR : ${a['atr']}

🛡️ *Gestion du Risque :*
• 🛑 Stop-Loss : ${a['stop_loss']} (-{round((1-a['stop_loss']/a['price'])*100,1)}%)
• 🎯 TP1 : ${a['tp1']} (+{round((a['tp1']/a['price']-1)*100,1)}%)
• 🎯 TP2 : ${a['tp2']} (+{round((a['tp2']/a['price']-1)*100,1)}%)
• 🎯 TP3 : ${a['tp3']} (+{round((a['tp3']/a['price']-1)*100,1)}%)
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
    a = get_fast_analysis(ticker)
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
    res = [a for t in HIGH_GROWTH_STOCKS[:30] if (a := get_fast_analysis(t)) and a['score'] >= 60]
    res.sort(key=lambda x: x['score'], reverse=True)
    if not res: bot.reply_to(message, "Aucune"); return
    msg = "💥 *TOP OPPORTUNITES*\n\n"
    for a in res[:5]:
        msg += f"*{a['name']}* ⭐{a['score']}/100 💰${a['price']}\n🔖 `{a['isin']}`\n\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marche'])
def cmd_marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    msg = "📈 *MARCHE*\n\n"
    for t, n in {'^GSPC': 'S&P500', '^IXIC': 'Nasdaq', '^DJI': 'Dow', '^VIX': 'VIX'}.items():
        try:
            h = yf.Ticker(t).history(period='2d')
            if len(h) >= 2:
                c, p = h['Close'].iloc[-1], h['Close'].iloc[-2]
                chg = (c-p)/p*100
                msg += f"{'🟢' if chg>0 else '🔴'} *{n}* {c:,.2f} ({chg:+.2f}%)\n"
        except: pass
    
    msg += "\n🔥 *TOP MOUVEMENTS*\n\n"
    movers = []
    for t in HIGH_GROWTH_STOCKS[:15]:
        try:
            stock = yf.Ticker(t)
            info = stock.info
            curr = info.get('currentPrice', 0)
            prev = info.get('previousClose', curr)
            if curr and prev:
                chg = abs((curr-prev)/prev*100)
                movers.append((t, chg))
        except: pass
        time.sleep(0.03)
    movers.sort(key=lambda x: x[1], reverse=True)
    for t, chg in movers[:5]:
        msg += f"• *{t}* : {chg:.1f}%\n"
    
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

# ===== SURVEILLANCE =====
def auto_scan():
    results = scan_fast(20, 50)
    if results:
        msg = "🔔 *ALERTE AUTO - FORT POTENTIEL*\n\n"
        for a in results[:3]:
            msg += f"🔥 *{a['name']}* +{a['change_pct']}%\n⭐ {a['score']}/100 | 💰 ${a['price']}\n🛑 Stop ${a['stop_loss']}\n\n"
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
    return "Bot Trader Pro v5 - OK", 200

if __name__ == '__main__':
    print("Demarrage Bot Trader Pro v5...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"Bot sur {WEBHOOK_URL}")
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
