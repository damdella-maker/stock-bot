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

# ===== WATCHLIST ULTRA-ELARGIE =====
HIGH_GROWTH_STOCKS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'CRM', 'ADBE', 'INTC', 'QCOM', 'AVGO', 'COIN', 'MARA', 'RIOT', 'MSTR',
    'HIVE', 'BTBT', 'CLSK', 'HUT', 'WULF', 'CIFR', 'IREN', 'CORZ',
    'PLTR', 'AI', 'BBAI', 'SOUN', 'PATH', 'C3AI', 'BIGC', 'VERI',
    'NIO', 'XPEV', 'LI', 'RIVN', 'LCID', 'CHPT', 'PLUG', 'FCEL', 'BLDP',
    'QS', 'GOEV', 'FSR', 'NKLA', 'WKHS', 'HYLN',
    'MRNA', 'PFE', 'BNTX', 'NVAX', 'VXRT', 'INO', 'OCGN', 'BNGO', 'HOTH',
    'GME', 'AMC', 'BB', 'SPCE', 'CLOV', 'WISH', 'MVIS',
    'LASE', 'HKD', 'TOP', 'GNS', 'NUWE', 'SOPA', 'AUUD', 'GFAI',
    'KSCP', 'KALA', 'EFTR', 'CJJD', 'JXJT', 'YQ', 'CLEU', 'POL',
    'ICU', 'TTOO', 'EOSE', 'AMPX',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'SQ', 'PYPL', 'SHOP',
    'BABA', 'JD', 'BIDU', 'BILI', 'TME', 'PDD', 'DQ', 'JKS', 'CAN', 'EH',
    'MU', 'AMAT', 'LRCX', 'KLAC', 'TSM', 'MRVL',
    'SNDL', 'TLRY', 'ACB', 'CGC', 'OGI', 'CRON',
    'BA', 'CCL', 'AAL', 'UAL', 'DAL', 'M', 'W', 'CVNA',
    'SNAP', 'UBER', 'ZM', 'CRWD', 'DDOG', 'SNOW', 'MDB', 'ZS', 'NET',
    'FSLY', 'U', 'DASH', 'ABNB', 'PTON', 'BYND', 'DKNG', 'PINS',
    'LITM', 'SHPW', 'PEGY',
    'BURU', 'FOXO', 'VTAK', 'RSLS', 'GCTK', 'IMNN'
]

HIGH_GROWTH_STOCKS = list(set(HIGH_GROWTH_STOCKS))

# ===== OUTILS =====
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
            atr = current_price * 0.05
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
    except:
        return None

def scan_all_movers():
    movers = []
    for ticker in HIGH_GROWTH_STOCKS[:50]:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', 0)
            if current and previous and current > 0 and previous > 0:
                change = ((current - previous) / previous) * 100
                volume = info.get('volume', 0)
                name = info.get('shortName', info.get('longName', ticker))
                movers.append({'ticker': ticker, 'name': name, 'price': current,
                               'change': round(change, 2), 'volume': volume})
        except:
            pass
        time.sleep(0.03)
    movers.sort(key=lambda x: x['change'], reverse=True)
    return movers

def get_tomorrow_recommendations():
    recommendations = []
    for ticker in HIGH_GROWTH_STOCKS[:40]:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', 0)
            if not current or not previous or current == 0:
                continue
            change_today = ((current - previous) / previous) * 100
            volume = info.get('volume', 0)
            avg_volume = info.get('averageVolume', 0)
            vol_ratio = volume / avg_volume if avg_volume > 0 else 1
            df = yf.download(ticker, period='1mo', progress=False)
            if df.empty or len(df) < 10:
                continue
            close = df['Close'].squeeze()
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = -delta.where(delta < 0, 0).rolling(14).mean()
            rsi = 100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] != 0 else 50
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9).mean()
            ma5 = close.rolling(5).mean().iloc[-1]
            ma10 = close.rolling(10).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else ma10
            change_5d = ((close.iloc[-1] - close.iloc[-5]) / close.iloc[-5]) * 100 if len(close) >= 5 else 0
            change_10d = ((close.iloc[-1] - close.iloc[-10]) / close.iloc[-10]) * 100 if len(close) >= 10 else change_5d
            score = 40
            if change_today > 0: score += 5
            if change_today > 3: score += 5
            if change_5d > 5: score += 5
            if change_5d > 10: score += 5
            if current > ma5: score += 5
            if current > ma10: score += 5
            if current > ma20: score += 5
            if ma5 > ma10: score += 5
            if ma10 > ma20: score += 5
            if 30 <= rsi <= 50: score += 10
            elif 50 <= rsi <= 65: score += 5
            if macd_line.iloc[-1] > signal_line.iloc[-1]: score += 5
            if macd_line.iloc[-1] > 0: score += 5
            if vol_ratio > 1.5: score += 8
            elif vol_ratio > 1: score += 4
            if change_today > 0 and change_5d > 0 and change_10d > 0: score += 5
            try:
                spy = yf.Ticker('SPY')
                spy_info = spy.info
                spy_change = ((spy_info.get('currentPrice', 0) - spy_info.get('previousClose', 0)) / spy_info.get('previousClose', 1)) * 100
                if spy_change > 0: score += 3
            except:
                pass
            score = min(100, max(0, score))
            a = get_fast_analysis(ticker)
            stop = a['stop_loss'] if a else round(current * 0.93, 2)
            tp1 = a['tp1'] if a else round(current * 1.07, 2)
            tp2 = a['tp2'] if a else round(current * 1.15, 2)
            if score >= 80: potential = "Tres eleve"
            elif score >= 65: potential = "Eleve"
            elif score >= 50: potential = "Modere"
            else: potential = "Faible"
            name = info.get('shortName', info.get('longName', ticker))
            sector = info.get('sector', 'N/A')
            recommendations.append({
                'ticker': ticker, 'name': name, 'sector': sector,
                'price': round(current, 3), 'change_today': round(change_today, 2),
                'change_5d': round(change_5d, 2), 'rsi': round(rsi, 1),
                'vol_ratio': round(vol_ratio, 1), 'score': score,
                'stop': stop, 'tp1': tp1, 'tp2': tp2,
                'potential': potential, 'ma5': round(ma5, 2), 'ma10': round(ma10, 2),
                'trend': 'Haussiere' if current > ma5 > ma10 else 'Neutre' if current > ma10 else 'Baissiere'
            })
        except:
            pass
        time.sleep(0.1)
    recommendations.sort(key=lambda x: x['score'], reverse=True)
    return recommendations[:5]

def send_alert_to_all(msg):
    for cid in get_all_subscribers():
        try:
            bot.send_message(cid, msg, parse_mode='Markdown')
        except:
            pass

# ===== COMMANDES =====
@bot.message_handler(commands=['start'])
def cmd_start(message):
    add_subscriber(message.chat.id)
    msg = """
🚀 *TRADER PRO V6*

✅ *Bot actif avec 150+ actions !*

📊 /rapide - Top gainers instantane
📊 /scan - Scan filtre personnalise
📊 /scan20 - Potentiel 20-50%
📊 /scan50 - Potentiel >50%
📊 /allmovers - TOUS les mouvements
📊 /analyse TICKER - Rapport complet
💥 /explosive - Top opportunites
📈 /marche - Indices en direct
📅 /demain - Top 5 pour demain
💼 /portfolio - Positions
📋 /historique - Historique
📊 /stats - Performance
/setrange MIN MAX - Filtre
/aide - Guide complet
"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """
📚 *GUIDE*

/rapide - Top gainers
/allmovers - Tous les mouvements
/scan - Avec votre filtre
/scan20 - Potentiel 20-50%
/scan50 - Potentiel >50%
/analyse TICKER - Rapport detaille
/demain - Top 5 pour demain
/explosive - Top 5 scores
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
    movers = scan_all_movers()
    if not movers:
        bot.reply_to(message, "❌ Aucune donnee")
        return
    gainers = [m for m in movers if m['change'] > 0][:15]
    if not gainers:
        bot.reply_to(message, "🔴 Aucune action en hausse aujourd'hui")
        return
    msg = "⚡ *TOP GAINERS DU JOUR*\n\n"
    for i, m in enumerate(gainers, 1):
        emoji = "🔥" if m['change'] > 20 else "🟢"
        bar = "█" * min(10, int(abs(m['change']) / 5))
        msg += f"*{i}. {m['ticker']}* {emoji}\n"
        msg += f"   {m['name'][:40]}\n"
        msg += f"   💰 ${m['price']:.3f} | +{m['change']}%\n"
        msg += f"   `{bar}`\n"
        msg += f"   Vol: {m['volume']:,}\n\n"
    msg += "📊 /analyse TICKER pour le detail"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['allmovers'])
def cmd_allmovers(message):
    bot.send_chat_action(message.chat.id, 'typing')
    movers = scan_all_movers()
    if not movers:
        bot.reply_to(message, "❌ Aucune donnee")
        return
    gainers = [m for m in movers if m['change'] > 0][:10]
    losers = [m for m in movers if m['change'] < 0][:5]
    msg = "📊 *TOUS LES MOUVEMENTS*\n\n"
    if gainers:
        msg += "🟢 *TOP HAUSSES :*\n"
        for m in gainers:
            msg += f"• *{m['ticker']}* : +{m['change']}% (${m['price']:.3f})\n"
        msg += "\n"
    if losers:
        msg += "🔴 *TOP BAISSES :*\n"
        for m in losers:
            msg += f"• *{m['ticker']}* : {m['change']}% (${m['price']:.3f})\n"
    msg += "\n💡 /rapide pour le top gainers"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['scan', 'scan20', 'scan50'])
def cmd_scan(message):
    cmd = message.text.split()[0].lower()
    if cmd == '/scan20': mn = 20
    elif cmd == '/scan50': mn = 50
    else:
        prefs = db_fetchone('SELECT min_potential FROM subscribers WHERE chat_id=?', (message.chat.id,))
        mn = prefs[0] if prefs else 3
    bot.send_chat_action(message.chat.id, 'typing')
    wait_msg = bot.reply_to(message, "🔍 *Scan en cours...*", parse_mode='Markdown')
    all_movers = scan_all_movers()
    results = [m for m in all_movers if m['change'] >= mn]
    if not results:
        bot.edit_message_text(
            f"🔍 *Aucune action > {mn}%*\n\n💡 /allmovers pour voir tous les mouvements",
            chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown'
        )
        return
    msg = f"🔥 *ACTIONS > {mn}% - {len(results)} TROUVES*\n\n"
    for i, m in enumerate(results[:10], 1):
        a = get_fast_analysis(m['ticker'])
        score = a['score'] if a else '?'
        stop = a['stop_loss'] if a else round(m['price'] * 0.95, 2)
        tp1 = a['tp1'] if a else round(m['price'] * 1.10, 2)
        stars = "⭐" * min(5, int(score) // 20 + 1) if isinstance(score, int) else "⭐"
        emoji = "🔥" if m['change'] > 50 else "🟢" if m['change'] > 20 else "🟡"
        msg += f"*{i}. {m['ticker']}* {emoji}\n"
        msg += f"   {stars} Score: {score}/100\n"
        msg += f"   💰 ${m['price']:.3f} | +{m['change']}%\n"
        msg += f"   🛑 Stop: ${stop} | TP1: ${tp1}\n"
        msg += f"   Vol: {m['volume']:,}\n\n"
    msg += "📊 /analyse TICKER pour le rapport complet"
    bot.edit_message_text(msg, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown')

@bot.message_handler(commands=['demain'])
def cmd_demain(message):
    bot.send_chat_action(message.chat.id, 'typing')
    wait_msg = bot.reply_to(message, "🔮 *Analyse du marche pour demain...*\n\nRecherche des meilleures opportunites...", parse_mode='Markdown')
    recommendations = get_tomorrow_recommendations()
    if not recommendations:
        bot.edit_message_text("❌ *Analyse impossible*\n\nMarche ferme ou donnees indisponibles.", chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown')
        return
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%d/%m/%Y')
    msg = f"🔮 *TOP 5 ACTIONS POUR DEMAIN - {tomorrow}*\n\n"
    msg += "═" * 30 + "\n\n"
    for i, r in enumerate(recommendations, 1):
        if r['score'] >= 80: stars, emoji, reco = "⭐⭐⭐⭐⭐", "🔥", "ACHAT FORT"
        elif r['score'] >= 65: stars, emoji, reco = "⭐⭐⭐⭐", "🟢", "ACHAT"
        elif r['score'] >= 50: stars, emoji, reco = "⭐⭐⭐", "🟡", "SURVEILLER"
        else: stars, emoji, reco = "⭐⭐", "🟠", "ATTENDRE"
        bar = "█" * (r['score'] // 10) + "░" * (10 - r['score'] // 10)
        msg += f"*{i}. {r['name']} ({r['ticker']})*\n"
        msg += f"   {stars} Score : {r['score']}/100\n"
        msg += f"   `[{bar}]`\n"
        msg += f"   {emoji} *{reco}*\n\n"
        msg += f"   💰 Prix actuel : ${r['price']:.3f}\n"
        msg += f"   📈 Aujourd'hui : {r['change_today']:+.1f}%\n"
        msg += f"   📊 Sur 5 jours : {r['change_5d']:+.1f}%\n"
        msg += f"   📐 RSI : {r['rsi']} | Tendance : {r['trend']}\n"
        msg += f"   📏 Volume : {r['vol_ratio']}x moyenne\n"
        msg += f"   🏭 Secteur : {r['sector']}\n\n"
        msg += f"   🛡️ *Protection :*\n"
        msg += f"   🛑 Stop-loss : ${r['stop']} (-{round((1-r['stop']/r['price'])*100,1)}%)\n"
        msg += f"   🎯 Objectif 1 : ${r['tp1']} (+{round((r['tp1']/r['price']-1)*100,1)}%)\n"
        msg += f"   🎯 Objectif 2 : ${r['tp2']} (+{round((r['tp2']/r['price']-1)*100,1)}%)\n"
        msg += f"   🔮 Potentiel demain : *{r['potential']}*\n\n"
        msg += "─" * 30 + "\n\n"
    best = recommendations[0]
    msg += f"💡 *RESUME*\n\n"
    msg += f"🥇 Top recommandation : *{best['ticker']}* ({best['score']}/100)\n"
    msg += f"📊 {best['change_today']:+.1f}% aujourd'hui | RSI : {best['rsi']}\n"
    msg += f"🛑 Stop : ${best['stop']} | 🎯 TP : ${best['tp1']}\n\n"
    msg += "⚠️ *Avertissement :*\n"
    msg += "Ces recommandations sont basees sur une analyse technique et ne garantissent pas les performances futures.\n"
    msg += "Faites toujours vos propres recherches avant d'investir.\n\n"
    msg += "📊 */analyse TICKER* pour un rapport complet\n"
    msg += "💼 Cliquez sur */analyse TICKER* puis *ACHETER* pour investir"
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
• Volume : {a['vol_ratio']}x ({a['volume']:,})

🛡️ *Gestion du Risque :*
• 🛑 Stop-Loss : ${a['stop_loss']}
• 🎯 TP1 : ${a['tp1']} (+{round((a['tp1']/a['price']-1)*100,1)}%)
• 🎯 TP2 : ${a['tp2']} (+{round((a['tp2']/a['price']-1)*100,1)}%)
• 🎯 TP3 : ${a['tp3']} (+{round((a['tp3']/a['price']-1)*100,1)}%)

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
        f"🛒 *Achat {ticker}*\nPrix actuel : ${float(price):.3f}\n\n💰 *Quel montant investir ?* (ex: 100)",
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
        bot.reply_to(message, "❌ Invalide")
        return
    curr = get_current_price(ticker) or 0
    msg = bot.send_message(message.chat.id,
        f"💰 Montant : {amount}€\n💵 Prix actuel : ${curr:.3f}\n\n📝 *Prix d'achat exact ?*",
        parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_price, ticker, amount)

def process_price(message, ticker, amount):
    try:
        buy_price = float(message.text.replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Invalide")
        return
    qty = amount / buy_price
    a = get_fast_analysis(ticker)
    stop = a['stop_loss'] if a else round(buy_price * 0.95, 2)
    tp1 = a['tp1'] if a else round(buy_price * 1.10, 2)
    tp2 = a['tp2'] if a else round(buy_price * 1.20, 2)
    tp3 = a['tp3'] if a else round(buy_price * 1.30, 2)
    db_execute('''INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (message.chat.id, ticker, buy_price, qty, amount, stop, tp1, tp2, tp3, buy_price, datetime.now().isoformat()))
    loss = amount - (stop / buy_price * amount)
    msg = f"""✅ *POSITION OUVERTE*
📊 *{ticker}*
💰 {amount:.2f}€ | ${buy_price:.3f}
📦 {qty:.4f} actions
🛑 Stop ${stop} (perte max {loss:.2f}€)
🎯 TP1 ${tp1} | TP2 ${tp2} | TP3 ${tp3}"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] > 10][:10]
    if not gainers:
        bot.reply_to(message, "Aucune opportunite explosive")
        return
    msg = "💥 *TOP OPPORTUNITES*\n\n"
    for m in gainers:
        a = get_fast_analysis(m['ticker'])
        score = a['score'] if a else '?'
        msg += f
