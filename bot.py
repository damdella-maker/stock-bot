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

@bot.message_handler(commands=['achat'])
def cmd_achat(message):
    """
    Ajoute un achat rapide : /achat TICKER PRIX MONTANT
    Exemple : /achat AAPL 150.50 100
    """
    try:
        parts = message.text.split()
        ticker = parts[1].upper()
        buy_price = float(parts[2].replace(',', '.'))
        amount = float(parts[3].replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Format : /achat TICKER PRIX MONTANT\nExemple : /achat AAPL 150.50 100")
        return
    
    # Calculs
    qty = amount / buy_price
    a = get_fast_analysis(ticker)
    stop = a['stop_loss'] if a else round(buy_price * 0.90, 2)  # Stop à -10%
    tp1 = a['tp1'] if a else round(buy_price * 1.10, 2)
    tp2 = a['tp2'] if a else round(buy_price * 1.20, 2)
    tp3 = a['tp3'] if a else round(buy_price * 1.30, 2)
    
    # Sauvegarder dans la base
    db_execute('''INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (message.chat.id, ticker, buy_price, qty, amount, stop, tp1, tp2, tp3, buy_price, datetime.now().isoformat()))
    
    # Message de confirmation
    current = get_current_price(ticker)
    if current:
        variation = ((current - buy_price) / buy_price) * 100
        status = "🟢" if variation >= 0 else "🔴"
    else:
        variation = 0
        status = "⚪"
    
    msg = f"""✅ *ACHAT ENREGISTRE*

📊 *{ticker}*
💰 Montant : {amount:.2f}€
💵 Prix d'achat : ${buy_price:.3f}
📦 Quantité : {qty:.4f} actions
{status} Prix actuel : ${current:.3f} ({variation:+.2f}%)""" if current else f"""✅ *ACHAT ENREGISTRE*

📊 *{ticker}*
💰 Montant : {amount:.2f}€
💵 Prix d'achat : ${buy_price:.3f}
📦 Quantité : {qty:.4f} actions"""

    msg += f"""

🛡️ *Protection :*
• 🛑 Stop-Loss (-10%) : ${stop}
• 💸 Perte max : {amount * 0.10:.2f}€

🎯 *Objectifs :*
• TP1 (+10%) : ${tp1} → Gain : {amount * 0.10:.2f}€
• TP2 (+20%) : ${tp2} → Gain : {amount * 0.20:.2f}€
• TP3 (+30%) : ${tp3} → Gain : {amount * 0.30:.2f}€

🔍 *Surveillance active*
⚠️ Alerte si baisse > 10%
📊 /portfolio pour suivre"""
    
    bot.reply_to(message, msg, parse_mode='Markdown')
    
# ===== WATCHLIST =====
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
    'LITM', 'SHPW', 'PEGY', 'BURU', 'FOXO', 'VTAK', 'RSLS', 'GCTK', 'IMNN'
]
HIGH_GROWTH_STOCKS = list(set(HIGH_GROWTH_STOCKS))

def check_critical_drops():
    """Vérifie les baisses de plus de 10% sur toutes les positions"""
    rows = db_fetchall("SELECT * FROM positions WHERE status='open'")
    
    for r in rows:
        curr = get_current_price(r[2])
        if not curr:
            continue
        
        # Calculer la perte en pourcentage
        loss_pct = ((curr / r[3]) - 1) * 100
        
        # Si baisse > 10%, envoyer une alerte
        if loss_pct <= -10:
            amount = r[5]
            current_value = r[4] * (curr / r[3])
            loss_amount = current_value - amount
            
            alert_msg = f"""🚨 *ALERTE BAISSE CRITIQUE*

📊 *{r[2]}*
📉 Baisse : {loss_pct:.1f}%
💰 Investi : {amount:.2f}€
💸 Valeur actuelle : {current_value:.2f}€
🔴 Perte : {loss_amount:.2f}€

🛑 Stop-Loss actuel : ${r[5]}
⚠️ *Envisagez de vendre rapidement !*
/vendre {r[2]}"""
            
            try:
                bot.send_message(r[1], alert_msg, parse_mode='Markdown')
            except:
                pass

def hourly_top_movers():
    """Envoie les tops mouvements haussiers toutes les heures"""
    now = datetime.now()
    
    # N'envoyer qu'entre 8h et 21h
    if now.hour < 8 or now.hour >= 21:
        return
    
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] > 2][:5]
    
    if not gainers:
        return
    
    msg = f"📊 *TOP MOUVEMENTS HORAIRE - {now.strftime('%H:%M')}*\n\n"
    
    for i, m in enumerate(gainers, 1):
        emoji = "🔥" if m['change'] > 10 else "🟢" if m['change'] > 5 else "🟡"
        msg += f"*{i}. {m['ticker']}* {emoji} +{m['change']}%\n"
        msg += f"   💰 ${m['price']:.3f} | Vol: {m['volume']:,}\n\n"
    
    msg += "📊 /analyse TICKER pour le detail"
    
    for subscriber in get_all_subscribers():
        try:
            bot.send_message(subscriber, msg, parse_mode='Markdown')
        except:
            pass

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
        
        current_price = info.get('currentPrice', info.get('regularMarketPrice', 0))
        if not current_price or current_price == 0:
            return None
        
        # ... reste de la fonction ...
        
    except Exception as e:
        print(f"Erreur get_fast_analysis {ticker}: {e}")
        return None

def scan_all_movers():
    movers = []
    for ticker in HIGH_GROWTH_STOCKS[:30]:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', 0)
            if current and previous and current > 0 and previous > 0:
                change = ((current - previous) / previous) * 100
                volume = info.get('volume', 0)
                name = info.get('shortName', ticker)
                movers.append({
                    'ticker': ticker, 'name': name[:30],
                    'price': current, 'change': round(change, 2), 'volume': volume
                })
        except:
            pass
    movers.sort(key=lambda x: x['change'], reverse=True)
    return movers
    
def get_tomorrow_recommendations():
    """Recommandations pour demain - version rapide"""
    recommendations = []
    
    for ticker in HIGH_GROWTH_STOCKS[:25]:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', 0)
            
            if not current or not previous or current == 0:
                continue
            
            # Variation du jour
            change_today = ((current - previous) / previous) * 100
            
            # Volume
            volume = info.get('volume', 0)
            avg_volume = info.get('averageVolume', 0)
            vol_ratio = volume / avg_volume if avg_volume > 0 else 1
            
            # Tendance 50 jours (si disponible)
            fifty_day_avg = info.get('fiftyDayAverage', 0)
            two_hundred_day_avg = info.get('twoHundredDayAverage', 0)
            
            # Score
            score = 40
            
            # Performance du jour
            if change_today > 0:
                score += 5
            if change_today > 2:
                score += 5
            if change_today > 5:
                score += 5
            if change_today > 10:
                score += 5
            
            # Volume
            if vol_ratio > 1:
                score += 5
            if vol_ratio > 1.5:
                score += 5
            if vol_ratio > 2:
                score += 5
            
            # Tendance long terme
            if fifty_day_avg > 0 and current > fifty_day_avg:
                score += 5
            if two_hundred_day_avg > 0 and current > two_hundred_day_avg:
                score += 5
            if fifty_day_avg > two_hundred_day_avg > 0:
                score += 5
            
            # Capitalisation (actions > 1B sont plus stables)
            market_cap = info.get('marketCap', 0)
            if market_cap > 1000000000:
                score += 5
            
            # Beta (volatilité)
            beta = info.get('beta', 1)
            if beta and beta > 1:
                score += 3  # Plus volatile = plus de potentiel
            
            score = min(100, max(0, score))
            
            # Stop et TP basés sur ATR estimé
            atr_pct = 0.03  # 3% par défaut
            if beta and beta > 1.5:
                atr_pct = 0.05  # Plus volatil
            elif beta and beta < 0.8:
                atr_pct = 0.02  # Moins volatil
            
            stop = round(current * (1 - atr_pct * 2), 2)
            tp1 = round(current * (1 + atr_pct * 2), 2)
            tp2 = round(current * (1 + atr_pct * 4), 2)
            
            # Potentiel
            if score >= 80:
                potential = "Tres eleve"
            elif score >= 65:
                potential = "Eleve"
            elif score >= 50:
                potential = "Modere"
            else:
                potential = "Faible"
            
            name = info.get('shortName', info.get('longName', ticker))
            sector = info.get('sector', 'N/A')
            
            # RSI estimé
            rsi = info.get('fiftyTwoWeekHigh', 0)
            if rsi > 0 and current > 0:
                rsi_est = 50 + (current / rsi - 0.7) * 100
                rsi_est = min(100, max(0, rsi_est))
            else:
                rsi_est = 50
            
            recommendations.append({
                'ticker': ticker,
                'name': name,
                'sector': sector,
                'price': round(current, 3),
                'change_today': round(change_today, 2),
                'change_5d': round(change_today, 2),
                'rsi': round(rsi_est, 1),
                'vol_ratio': round(vol_ratio, 1),
                'score': score,
                'stop': stop,
                'tp1': tp1,
                'tp2': tp2,
                'potential': potential,
                'trend': 'Haussiere' if fifty_day_avg > 0 and current > fifty_day_avg else 'Neutre',
                'ma5': 0,
                'ma10': 0
            })
        except:
            pass
    
    # Trier par score
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
    msg = "🚀 *TRADER PRO V7*\n\n"
    msg += "✅ Bot actif !\n\n"
    msg += "📊 /rapide - Top gainers\n"
    msg += "📊 /scan - Scan filtre\n"
    msg += "📊 /scan20 - Potentiel 20-50%\n"
    msg += "📊 /scan50 - Potentiel >50%\n"
    msg += "📊 /allmovers - Tous mouvements\n"
    msg += "📊 /analyse TICKER - Rapport\n"
    msg += "💥 /explosive - Top opportunites\n"
    msg += "📈 /marche - Indices\n"
    msg += "📅 /demain - Top 5 demain\n"
    msg += "📝 /achat TICKER PRIX MONTANT\n"
    msg += "💼 /portfolio - Positions\n"
    msg += "📋 /historique - Historique\n"
    msg += "📊 /stats - Performance\n"
    msg += "🔔 /alerte TICKER PRIX\n"
    msg += "📁 /import_csv - Import CSV\n"
    msg += "⚙️ /setrange MIN MAX\n"
    msg += "📚 /aide - Guide"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = "📚 *GUIDE*\n\n"
    msg += "/rapide - Top gainers\n"
    msg += "/scan - Scan avec filtre\n"
    msg += "/scan20 - 20-50%\n"
    msg += "/scan50 - >50%\n"
    msg += "/analyse TICKER - Rapport\n"
    msg += "/demain - Top 5 demain\n"
    msg += "/achat TICKER PRIX MONTANT\n"
    msg += "/portfolio - Positions\n"
    msg += "/vendre TICKER - Vendre\n"
    msg += "/historique - Historique\n"
    msg += "/stats - Statistiques\n"
    msg += "/alerte TICKER PRIX - Alerte\n"
    msg += "/import_csv - Import CSV\n"
    msg += "/setrange MIN MAX - Filtre"
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
        bot.reply_to(message, "🔴 Aucune action en hausse")
        return
    msg = "⚡ *TOP GAINERS*\n\n"
    for i, m in enumerate(gainers, 1):
        emoji = "🔥" if m['change'] > 20 else "🟢"
        bar = "█" * min(10, int(abs(m['change']) / 5))
        msg += f"*{i}. {m['ticker']}* {emoji}\n"
        msg += f"   💰 ${m['price']:.3f} | +{m['change']}%\n"
        msg += f"   `{bar}` | Vol: {m['volume']:,}\n\n"
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
    msg = "📊 *MOUVEMENTS*\n\n"
    if gainers:
        msg += "🟢 *HAUSSES :*\n"
        for m in gainers:
            msg += f"• *{m['ticker']}* : +{m['change']}%\n"
        msg += "\n"
    if losers:
        msg += "🔴 *BAISSES :*\n"
        for m in losers:
            msg += f"• *{m['ticker']}* : {m['change']}%\n"
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
    wait_msg = bot.reply_to(message, "🔍 *Scan...*", parse_mode='Markdown')
    all_movers = scan_all_movers()
    results = [m for m in all_movers if m['change'] >= mn]
    if not results:
        bot.edit_message_text(f"🔍 Aucune action > {mn}%", chat_id=message.chat.id, message_id=wait_msg.message_id)
        return
    msg = f"🔥 *ACTIONS > {mn}%*\n\n"
    for i, m in enumerate(results[:10], 1):
        a = get_fast_analysis(m['ticker'])
        score = a['score'] if a else '?'
        stop = a['stop_loss'] if a else round(m['price'] * 0.95, 2)
        msg += f"*{i}. {m['ticker']}* +{m['change']}% | Score: {score}/100\n"
        msg += f"   💰 ${m['price']:.3f} | 🛑 Stop: ${stop}\n\n"
    bot.edit_message_text(msg, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown')

@bot.message_handler(commands=['demain'])
def cmd_demain(message):
    bot.send_chat_action(message.chat.id, 'typing')
    wait_msg = bot.reply_to(message, "🔮 *Analyse pour demain...*", parse_mode='Markdown')
    
    try:
        recommendations = get_tomorrow_recommendations()
    except Exception as e:
        bot.edit_message_text(
            f"❌ Erreur lors de l'analyse : {str(e)[:50]}",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id
        )
        return
    
    if not recommendations or len(recommendations) == 0:
        bot.edit_message_text(
            "❌ *Aucune recommandation disponible*\n\n"
            "Le marche est peut-etre ferme ou les donnees sont indisponibles.\n\n"
            "💡 Essayez /rapide pour voir les mouvements du jour.",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id,
            parse_mode='Markdown'
        )
        return
    
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%d/%m/%Y')
    msg = f"🔮 *TOP 5 POUR DEMAIN - {tomorrow}*\n\n"
    msg += "═" * 25 + "\n\n"
    
    for i, r in enumerate(recommendations, 1):
        if r['score'] >= 80:
            stars, reco = "⭐⭐⭐⭐⭐", "ACHAT FORT"
        elif r['score'] >= 65:
            stars, reco = "⭐⭐⭐⭐", "ACHAT"
        elif r['score'] >= 50:
            stars, reco = "⭐⭐⭐", "SURVEILLER"
        else:
            stars, reco = "⭐⭐", "ATTENDRE"
        
        msg += f"*{i}. {r['name']} ({r['ticker']})*\n"
        msg += f"   {stars} Score: {r['score']}/100 {reco}\n"
        msg += f"   💰 ${r['price']:.3f} | 📈 +{r['change_today']}% aujourd'hui\n"
        msg += f"   📊 Vol: {r['vol_ratio']}x | RSI: {r['rsi']}\n"
        msg += f"   🛑 Stop: ${r['stop']} | 🎯 TP: ${r['tp1']}\n"
        msg += f"   🔮 Potentiel: *{r['potential']}*\n\n"
    
    msg += "⚠️ Analyse basee sur les donnees de marche actuelles."
    
    bot.edit_message_text(
        msg,
        chat_id=message.chat.id,
        message_id=wait_msg.message_id,
        parse_mode='Markdown'
    )
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
🔖 ISIN: `{a['isin']}`

⭐ Score: {a['score']}/100
💰 Prix: ${a['price']}
📈 Variation: {a['change_pct']:+.2f}%

📊 RSI: {a['rsi']} | MACD: {a['macd']}
📏 Vol: {a['vol_ratio']}x ({a['volume']:,})

🛡️ Stop: ${a['stop_loss']}
🎯 TP1: ${a['tp1']} | TP2: ${a['tp2']} | TP3: ${a['tp3']}"""
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
        f"🛒 *Achat {ticker}*\nPrix: ${float(price):.3f}\n\n💰 *Montant a investir ?* (ex: 100)",
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
        f"💰 Montant: {amount}€\n💵 Prix actuel: ${curr:.3f}\n\n📝 *Prix d'achat exact ?*",
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
    db_execute('INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (message.chat.id, ticker, buy_price, qty, amount, stop, tp1, tp2, tp3, buy_price, datetime.now().isoformat()))
    loss = amount - (stop / buy_price * amount)
    msg = f"✅ *POSITION OUVERTE*\n📊 {ticker}\n💰 {amount:.2f}€ | ${buy_price:.3f}\n📦 {qty:.4f} actions\n🛑 Stop ${stop} (perte max {loss:.2f}€)\n🎯 TP1 ${tp1} | TP2 ${tp2} | TP3 ${tp3}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] > 10][:10]
    if not gainers:
        bot.reply_to(message, "Aucune opportunite")
        return
    msg = "💥 *TOP OPPORTUNITES*\n\n"
    for m in gainers:
        msg += f"🔥 *{m['ticker']}* +{m['change']}% | 💰 ${m['price']:.3f}\n"
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
        except:
            pass
    msg += f"\n🕐 {datetime.now().strftime('%H:%M:%S')}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    rows = db_fetchall("SELECT * FROM positions WHERE chat_id=? AND status='open'", (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📭 Vide")
        return
    msg = "💼 *PORTEFEUILLE*\n\n"
    ti = tc = 0
    for r in rows:
        curr = get_current_price(r[2]) or r[3]
        val = r[4] * (curr / r[3])
        pnl = val - r[5]
        ti += r[5]
        tc += val
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg += f"{emoji} *{r[2]}* {r[4]:.4f} act\n   {r[5]:.2f}€ → {val:.2f}€ | {pnl:+.2f}€\n\n"
    msg += f"━━━━━━\n💰 Total: {tc:.2f}€ | P&L: {tc-ti:+.2f}€"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /vendre AAPL")
        return
    row = db_fetchone("SELECT * FROM positions WHERE chat_id=? AND ticker=? AND status='open'", (message.chat.id, ticker))
    if not row:
        bot.reply_to(message, "❌ Pas de position")
        return
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
    if not rows:
        bot.reply_to(message, "📋 Vide")
        return
    msg = "📋 *HISTORIQUE*\n\n"
    for r in rows:
        msg += f"{'🟢' if r[7]>=0 else '🔴'} *{r[2]}* {r[7]:+.1f}% | {r[6]:+.2f}€\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    rows = db_fetchall('SELECT profit_loss FROM trades WHERE chat_id=?', (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📊 Pas de stats")
        return
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
    except:
        bot.reply_to(message, "❌ /alerte AAPL 200")

@bot.message_handler(commands=['import_csv'])
def cmd_import_csv(message):
    msg = bot.reply_to(message, "📁 Envoyez le CSV")
    bot.register_next_step_handler(msg, process_csv)

def process_csv(message):
    content = message.document and bot.download_file(bot.get_file(message.document.file_id).file_path).decode('utf-8') or message.text
    if not content:
        bot.reply_to(message, "❌ Vide")
        return
    try:
        count = 0
        for row in csv.DictReader(io.StringIO(content)):
            t = row.get('Action', row.get('Ticker', ''))
            q = float(row.get('Quantite', row.get('Quantity', 0)))
            p = float(row.get('Prix', row.get('Price', 0)))
            if t and q > 0 and p > 0:
                amt = q * p
                db_execute("INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (message.chat.id, t.upper(), p, q, amt, round(p*.95,2), round(p*1.1,2), round(p*1.2,2), round(p*1.3,2), p, datetime.now().isoformat()))
                count += 1
        bot.reply_to(message, f"✅ {count} positions importees")
    except Exception as e:
        bot.reply_to(message, f"❌ {e}")

# ===== SURVEILLANCE =====
def auto_scan():
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] >= 20][:5]
    if gainers:
        msg = "🔔 *ALERTE AUTO*\n\n"
        for m in gainers:
            a = get_fast_analysis(m['ticker'])
            stop = a['stop_loss'] if a else round(m['price'] * 0.95, 2)
            msg += f"🔥 *{m['ticker']}* +{m['change']}%\n"
            msg += f"   💰 ${m['price']:.3f} | 🛑 Stop ${stop}\n\n"
        send_alert_to_all(msg)

def monitor():
    for r in db_fetchall("SELECT * FROM positions WHERE status='open'"):
        curr = get_current_price(r[2])
        if not curr:
            continue
        if curr > r[9]:
            db_execute('UPDATE positions SET highest_price=? WHERE id=?', (curr, r[0]))
        if curr >= r[3] * 1.05:
            ns = round(curr * 0.97, 2)
            if ns > r[5]:
                db_execute('UPDATE positions SET stop_loss=? WHERE id=?', (ns, r[0]))
        if curr <= r[5]:
            try:
                bot.send_message(r[1], f"🚨 *STOP* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass
        elif curr >= r[8]:
            try:
                bot.send_message(r[1], f"🎯 *TP3* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass
        elif curr >= r[7]:
            try:
                bot.send_message(r[1], f"🎯 *TP2* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass
        elif curr >= r[6]:
            try:
                bot.send_message(r[1], f"🎯 *TP1* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except:
                pass

def run_scheduler():
    """Planificateur de toutes les taches"""
    # Toutes les 15 minutes : scan auto
    schedule.every(15).minutes.do(auto_scan)
    
    # Toutes les 10 minutes : surveillance positions
    schedule.every(10).minutes.do(monitor)
    
    # Toutes les 5 minutes : vérification baisses critiques
    schedule.every(5).minutes.do(check_critical_drops)
    
    # Toutes les heures : top mouvements
    schedule.every(60).minutes.do(hourly_top_movers)
    
    while True:
        schedule.run_pending()
        time.sleep(10)

# ===== FLASK =====
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
        return 'ok', 200
    return 'bad request', 400

@app.route('/')
def home():
    return "Bot Trader Pro v6 - OK", 200

if __name__ == '__main__':
    print("Demarrage Bot Trader Pro v6...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"Bot sur {WEBHOOK_URL}")
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
