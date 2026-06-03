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
import pytz

# --- Configuration ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
DB_PATH = 'trading_bot.db'

# --- Base de données ---
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

# --- Watchlist éprouvée (que des titres liquides et bien couverts) ---
HIGH_GROWTH_STOCKS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'PLTR', 'COIN', 'MARA', 'RIOT', 'NIO', 'LI', 'RIVN', 'LCID',
    'GME', 'AMC', 'SPCE', 'AFRM', 'SOFI', 'HOOD', 'BABA', 'JD', 'PFE',
    'UBER', 'ZM', 'CRWD', 'SNAP', 'DASH', 'ABNB', 'DKNG', 'PINS',
    'BA', 'CCL', 'AAL', 'MRNA', 'SNDL', 'TLRY', 'ACB', 'CGC'
]
HIGH_GROWTH_STOCKS = list(set(HIGH_GROWTH_STOCKS))

# --- Fonctions de données (robustes avec fallback) ---
def get_current_price_finnhub(ticker):
    """Essaie Finnhub, retourne le prix ou None."""
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        resp = requests.get(url, timeout=5).json()
        if 'c' in resp and resp['c'] > 0:
            return resp['c']
    except:
        pass
    return None

def get_yahoo_info(ticker):
    """Retourne les infos Yahoo Finance pour un ticker."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return info
    except:
        return {}

def get_fast_analysis(ticker):
    """
    Récupère le prix via yf.download (2 jours), puis les infos fondamentales via stock.info.
    Très fiable, même sans Finnhub.
    """
    try:
        # Téléchargement des 2 derniers jours (donne le prix actuel et la clôture précédente)
        df = yf.download(ticker, period='2d', progress=False)
        if df.empty or len(df) < 2:
            return None

        current_price = float(df['Close'].iloc[-1])
        previous_close = float(df['Close'].iloc[-2])
        volume = int(df['Volume'].iloc[-1]) if 'Volume' in df else 0

        # Informations supplémentaires (via stock.info, sans bloquer)
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
        except:
            info = {}

        change_pct = ((current_price - previous_close) / previous_close) * 100

        avg_volume = info.get('averageVolume', 0)
        vol_ratio = volume / avg_volume if avg_volume > 0 else 1

        # Score technique simple
        score = 50
        if change_pct > 0: score += 10
        if change_pct > 5: score += 10
        if vol_ratio > 1: score += 10
        if vol_ratio > 2: score += 10
        if volume > 1000000: score += 10
        score = min(100, score)

        # ATR estimé
        atr = current_price * 0.03
        stop_loss = round(current_price - 2 * atr, 2)
        tp1 = round(current_price + 2 * atr, 2)
        tp2 = round(current_price + 4 * atr, 2)
        tp3 = round(current_price + 6 * atr, 2)

        name = info.get('shortName', info.get('longName', ticker)) or ticker
        isin = info.get('isin', 'N/A')
        sector = info.get('sector', 'N/A')

        if score >= 80:
            pot10, pot20, pot30 = 75, 55, 35
        elif score >= 65:
            pot10, pot20, pot30 = 60, 40, 25
        elif score >= 50:
            pot10, pot20, pot30 = 45, 30, 15
        else:
            pot10, pot20, pot30 = 30, 20, 10

        return {
            'ticker': ticker, 'name': str(name)[:40], 'isin': isin, 'sector': sector,
            'price': round(current_price, 3), 'change_pct': round(change_pct, 2),
            'vol_ratio': round(vol_ratio, 1), 'volume': volume, 'score': score,
            'stop_loss': stop_loss, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'potential_10': pot10, 'potential_20': pot20, 'potential_30': pot30,
            'ma20': 0, 'macd': 0, 'macd_signal': 0, 'rsi': 0,
            'avg_volume': avg_volume, 'atr': round(atr, 2),
            'risk_reward': 2.0, 'industry': sector, 'market_cap': 0
        }
    except Exception as e:
        print(f"Erreur get_fast_analysis {ticker}: {e}")
        return None

def scan_all_movers():
    """
    Scan de toutes les actions de la watchlist.
    Utilise d'abord Finnhub si disponible, sinon Yahoo Finance groupé.
    Retourne une liste de dicts avec ticker, price, change, volume.
    """
    movers = []
    finnhub_works = False
    if FINNHUB_KEY:
        try:
            test_url = f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={FINNHUB_KEY}"
            test_resp = requests.get(test_url, timeout=5).json()
            if 'c' in test_resp and test_resp['c'] > 0:
                finnhub_works = True
        except:
            pass

    if finnhub_works:
        for ticker in HIGH_GROWTH_STOCKS:
            try:
                url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
                resp = requests.get(url, timeout=5).json()
                if 'c' not in resp or resp['c'] == 0:
                    continue
                current = resp['c']
                previous = resp.get('pc', 0)
                if previous == 0:
                    continue
                change = ((current - previous) / previous) * 100
                volume = resp.get('v', 0)
                movers.append({
                    'ticker': ticker, 'name': ticker,
                    'price': current, 'change': round(change, 2), 'volume': volume
                })
            except:
                pass
            time.sleep(0.05)
    else:
        # Fallback Yahoo Finance groupé (plus rapide et plus fiable)
        tickers = HIGH_GROWTH_STOCKS.copy()
        try:
            data = yf.download(tickers, period='2d', progress=False, group_by='ticker')
            if not data.empty:
                for ticker in tickers:
                    try:
                        if len(tickers) == 1:
                            close_series = data['Close']
                        else:
                            close_series = data[ticker]['Close'] if ticker in data.columns.levels[0] else None
                        if close_series is None or len(close_series) < 2:
                            continue
                        curr = close_series.iloc[-1]
                        prev = close_series.iloc[-2]
                        change = ((curr - prev) / prev) * 100
                        vol_series = data[ticker]['Volume'] if ticker in data.columns.levels[0] else None
                        volume = int(vol_series.iloc[-1]) if vol_series is not None and len(vol_series) > 0 else 0
                        movers.append({
                            'ticker': ticker, 'name': ticker,
                            'price': round(curr, 3), 'change': round(change, 2), 'volume': volume
                        })
                    except:
                        pass
        except Exception as e:
            print(f"Batch yfinance failed: {e}")
            # Dernier recours : un par un
            for ticker in tickers:
                try:
                    df = yf.download(ticker, period='2d', progress=False)
                    if len(df) >= 2:
                        curr = df['Close'].iloc[-1]
                        prev = df['Close'].iloc[-2]
                        change = ((curr - prev) / prev) * 100
                        volume = int(df['Volume'].iloc[-1]) if 'Volume' in df else 0
                        movers.append({
                            'ticker': ticker, 'name': ticker,
                            'price': round(curr, 3), 'change': round(change, 2), 'volume': volume
                        })
                except:
                    pass

    movers.sort(key=lambda x: x['change'], reverse=True)
    return movers

def get_company_news(ticker):
    """Récupère les 5 dernières actualités via Finnhub (7 jours)."""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={week_ago}&to={today}&token={FINNHUB_KEY}"
        resp = requests.get(url, timeout=5).json()
        if isinstance(resp, list) and len(resp) > 0:
            return resp[:5]
        return []
    except:
        return []

def analyze_sentiment(news):
    """Analyse basique du sentiment des titres d'actualités."""
    positive_words = ['profit', 'growth', 'rise', 'gain', 'buy', 'upgrade', 'beat', 'positive', 'record']
    negative_words = ['loss', 'decline', 'fall', 'drop', 'sell', 'downgrade', 'miss', 'negative', 'risk']
    pos_count = 0
    neg_count = 0
    for article in news:
        title = (article.get('headline', '') + ' ' + article.get('summary', '')).lower()
        pos_count += sum(1 for w in positive_words if w in title)
        neg_count += sum(1 for w in negative_words if w in title)
    if pos_count > neg_count:
        return "🟢 Positif"
    elif neg_count > pos_count:
        return "🔴 Négatif"
    else:
        return "🟡 Neutre"

def get_detailed_analysis(ticker):
    """Analyse enrichie : fondamentaux, consensus, actualités."""
    base = get_fast_analysis(ticker)
    if not base:
        return None

    info = get_yahoo_info(ticker)

    # Fondamentaux
    pe = info.get('trailingPE', info.get('forwardPE', 'N/A'))
    eps = info.get('trailingEps', 'N/A')
    roe = info.get('returnOnEquity', 'N/A')
    if isinstance(roe, (int, float)): roe = f"{roe*100:.1f}%"
    debt = info.get('debtToEquity', 'N/A')
    rev_growth = info.get('revenueGrowth', 'N/A')
    if isinstance(rev_growth, (int, float)): rev_growth = f"{rev_growth*100:.1f}%"
    margin = info.get('profitMargins', 'N/A')
    if isinstance(margin, (int, float)): margin = f"{margin*100:.1f}%"
    market_cap = info.get('marketCap', 0)
    div_yield = info.get('dividendYield', 'N/A')
    if isinstance(div_yield, (int, float)): div_yield = f"{div_yield*100:.2f}%"

    # Consensus analystes
    target_mean = info.get('targetMeanPrice', None)
    target_high = info.get('targetHighPrice', None)
    target_low = info.get('targetLowPrice', None)
    recommendation = info.get('recommendationKey', 'N/A')

    # Actualités
    news = get_company_news(ticker)
    sentiment = analyze_sentiment(news) if news else "N/A"

    detailed = {**base,
                'pe_ratio': pe, 'eps': eps, 'roe': roe,
                'debt_to_equity': debt, 'revenue_growth': rev_growth,
                'profit_margin': margin, 'market_cap': market_cap,
                'dividend_yield': div_yield,
                'target_mean': target_mean, 'target_high': target_high,
                'target_low': target_low, 'recommendation': recommendation,
                'news': news, 'sentiment': sentiment,
                'sector': info.get('sector', base['sector']),
                'industry': info.get('industry', base['industry']),
                'name': info.get('shortName', info.get('longName', ticker)) or base['name'],
                'isin': info.get('isin', base['isin'])}
    return detailed

def get_tomorrow_recommendations():
    """Top 5 pour demain basé sur momentum/volume."""
    recommendations = []
    for ticker in HIGH_GROWTH_STOCKS:
        try:
            info = get_yahoo_info(ticker)
            current = info.get('currentPrice', info.get('regularMarketPrice', 0))
            previous = info.get('previousClose', 0)
            if not current or not previous:
                continue
            change_today = ((current - previous) / previous) * 100
            volume = info.get('volume', 0)
            avg_volume = info.get('averageVolume', 0)
            vol_ratio = volume / avg_volume if avg_volume > 0 else 1
            score = 40
            if change_today > 0: score += 10
            if change_today > 3: score += 10
            if vol_ratio > 1: score += 10
            if vol_ratio > 2: score += 15
            if volume > 500000: score += 15
            score = min(100, max(0, score))
            stop = round(current * 0.93, 2)
            tp1 = round(current * 1.07, 2)
            tp2 = round(current * 1.15, 2)
            potential = "Très élevé" if score >= 80 else "Élevé" if score >= 65 else "Modéré" if score >= 50 else "Faible"
            name = info.get('shortName', info.get('longName', ticker))
            sector = info.get('sector', 'N/A')
            recommendations.append({
                'ticker': ticker, 'name': str(name), 'sector': sector,
                'price': round(current, 3), 'change_today': round(change_today, 2),
                'vol_ratio': round(vol_ratio, 1), 'score': score,
                'stop': stop, 'tp1': tp1, 'tp2': tp2,
                'potential': potential, 'trend': 'N/A'
            })
        except:
            pass
    recommendations.sort(key=lambda x: x['score'], reverse=True)
    return recommendations[:5]

def send_alert_to_all(msg):
    for cid in get_all_subscribers():
        try:
            bot.send_message(cid, msg, parse_mode='Markdown')
        except:
            pass

# --- Gestion du marché US ---
def is_market_open():
    try:
        tz = pytz.timezone('US/Eastern')
        now_us = datetime.now(tz)
        if now_us.weekday() >= 5:
            return False
        market_open = now_us.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_us.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now_us <= market_close
    except:
        return True

# ==================== COMMANDES TELEGRAM ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    add_subscriber(message.chat.id)
    msg = "🚀 *TRADER PRO - RÉVOLUTIONNAIRE*\n\n"
    msg += "✅ Analyse enrichie multi-sources (Yahoo, Finnhub)\n\n"
    msg += "📊 /rapide - Top gainers\n"
    msg += "📊 /scan - Scan avec votre filtre\n"
    msg += "📊 /scan20 - Potentiel 20-50%\n"
    msg += "📊 /scan50 - Potentiel >50%\n"
    msg += "📊 /allmovers - Tous les mouvements\n"
    msg += "📊 /analyse TICKER - Rapport complet enrichi\n"
    msg += "💥 /explosive - Top opportunités\n"
    msg += "📈 /marche - Indices en direct\n"
    msg += "📅 /demain - Top 5 pour demain\n"
    msg += "📝 /achat TICKER PRIX MONTANT - Achat rapide\n"
    msg += "💼 /portfolio - Positions\n"
    msg += "📋 /historique - Historique\n"
    msg += "📊 /stats - Performance\n"
    msg += "⚙️ /setrange MIN MAX - Votre filtre\n"
    msg += "📚 /aide - Guide complet\n"
    msg += "🏓 /ping - Test de vie"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = "📚 *GUIDE*\n\n"
    msg += "/rapide - Top gainers\n"
    msg += "/scan - Scan avec filtre\n"
    msg += "/scan20 - 20-50%\n"
    msg += "/scan50 - >50%\n"
    msg += "/analyse TICKER - Rapport enrichi\n"
    msg += "/demain - Top 5 demain\n"
    msg += "/achat TICKER PRIX MONTANT\n"
    msg += "/portfolio - Positions\n"
    msg += "/vendre TICKER - Vendre\n"
    msg += "/historique - Historique\n"
    msg += "/stats - Statistiques\n"
    msg += "/alerte TICKER PRIX - Alerte\n"
    msg += "/import_csv - Import CSV\n"
    msg += "/setrange MIN MAX - Filtre\n"
    msg += "/ping - Test"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['ping'])
def cmd_ping(message):
    bot.reply_to(message, "pong")

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
    if not is_market_open():
        bot.reply_to(message, "⚠️ Marché US fermé. Données de la dernière séance.")
    bot.send_chat_action(message.chat.id, 'typing')
    movers = scan_all_movers()
    if not movers:
        bot.reply_to(message, "❌ Aucune donnée. Vérifiez votre connexion ou la clé Finnhub.")
        return
    gainers = [m for m in movers if m['change'] > 0][:15]
    if not gainers:
        bot.reply_to(message, "🔴 Aucune action en hausse détectée.")
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
    if not is_market_open():
        bot.reply_to(message, "⚠️ Marché US fermé.")
    bot.send_chat_action(message.chat.id, 'typing')
    movers = scan_all_movers()
    if not movers:
        bot.reply_to(message, "❌ Aucune donnée.")
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
    if not is_market_open():
        bot.reply_to(message, "⚠️ Marché US fermé.")
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
        bot.edit_message_text(f"🔍 Aucune action au-dessus de {mn}%", chat_id=message.chat.id, message_id=wait_msg.message_id)
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
    recs = get_tomorrow_recommendations()
    if not recs:
        bot.edit_message_text("❌ Analyse impossible", chat_id=message.chat.id, message_id=wait_msg.message_id)
        return
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%d/%m/%Y')
    msg = f"🔮 *TOP 5 POUR DEMAIN - {tomorrow}*\n\n"
    for i, r in enumerate(recs, 1):
        if r['score'] >= 80: stars, reco = "⭐⭐⭐⭐⭐", "ACHAT FORT"
        elif r['score'] >= 65: stars, reco = "⭐⭐⭐⭐", "ACHAT"
        elif r['score'] >= 50: stars, reco = "⭐⭐⭐", "SURVEILLER"
        else: stars, reco = "⭐⭐", "ATTENDRE"
        msg += f"*{i}. {r['ticker']}* {stars} {reco}\n"
        msg += f"   Score: {r['score']}/100 | 💰 ${r['price']:.3f}\n"
        msg += f"   📈 +{r['change_today']}% | Vol: {r['vol_ratio']}x\n"
        msg += f"   🛑 Stop: ${r['stop']} | 🎯 TP: ${r['tp1']}\n\n"
    msg += "📊 /analyse TICKER pour le détail complet"
    bot.edit_message_text(msg, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    if not is_market_open():
        bot.reply_to(message, "⚠️ Marché fermé. Les données peuvent être celles de la veille.")
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return

    bot.send_chat_action(message.chat.id, 'typing')
    a = get_detailed_analysis(ticker)
    if not a:
        bot.reply_to(message, f"❌ Données indisponibles pour {ticker}. Vérifiez le ticker ou réessayez plus tard.")
        return

    msg = f"📊 *{a['name']} ({a['ticker']})*\n"
    msg += f"🔖 ISIN: `{a['isin']}`\n"
    msg += f"🏭 {a['sector']} | {a['industry']}\n\n"

    msg += f"⭐ *Score technique : {a['score']}/100*\n"
    msg += f"💰 Prix : ${a['price']} ({a['change_pct']:+.2f}%)\n"
    msg += f"📊 Volume : {a['vol_ratio']}x moyenne\n\n"

    msg += "📈 *Fondamentaux :*\n"
    msg += f"• PER : {a['pe_ratio']} | BPA : {a['eps']}\n"
    msg += f"• ROE : {a['roe']} | Marge nette : {a['profit_margin']}\n"
    msg += f"• Croissance CA : {a['revenue_growth']} | Dette/Equity : {a['debt_to_equity']}\n"
    msg += f"• Dividende : {a['dividend_yield']} | Cap. boursière : {a['market_cap']:,} $\n\n"

    if a['target_mean']:
        msg += "🎯 *Consensus analystes :*\n"
        msg += f"• Objectif moyen : ${a['target_mean']:.2f} (potentiel {((a['target_mean']/a['price'])-1)*100:+.1f}%)\n"
        msg += f"• Fourchette : ${a['target_low']:.2f} - ${a['target_high']:.2f}\n"
        msg += f"• Recommandation : {a['recommendation']}\n\n"

    msg += "🎲 *Potentiel estimé (analyse technique) :*\n"
    msg += f"• +10% : {a['potential_10']}% de probabilité\n"
    msg += f"• +20% : {a['potential_20']}% de probabilité\n"
    msg += f"• +30% : {a['potential_30']}% de probabilité\n\n"

    msg += "🛡️ *Gestion du risque :*\n"
    msg += f"🛑 Stop-loss : ${a['stop_loss']} (-{round((1-a['stop_loss']/a['price'])*100,1)}%)\n"
    msg += f"🎯 TP1 : ${a['tp1']} (+{round((a['tp1']/a['price']-1)*100,1)}%)\n"
    msg += f"🎯 TP2 : ${a['tp2']} (+{round((a['tp2']/a['price']-1)*100,1)}%)\n"
    msg += f"🎯 TP3 : ${a['tp3']} (+{round((a['tp3']/a['price']-1)*100,1)}%)\n\n"

    if a['news']:
        msg += f"📰 *Actualités récentes (7j) - Sentiment {a['sentiment']} :*\n"
        for i, article in enumerate(a['news'][:3], 1):
            headline = article.get('headline', 'Pas de titre')[:80]
            msg += f"  {i}. {headline}\n"
        msg += "\n"

    msg += "⚠️ *Avertissement :* Cette analyse est basée sur des données historiques et des estimations. Ne constitue pas un conseil en investissement."

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🛒 ACHETER", callback_data=f"buy_{ticker}_{a['price']}"),
        types.InlineKeyboardButton("❌ PASSER", callback_data="pass")
    )
    bot.reply_to(message, msg, parse_mode='Markdown', reply_markup=markup)

# --- Callbacks et commandes de trading (achat, vente, portfolio...) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy(call):
    _, ticker, price = call.data.split('_')
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        f"🛒 *Achat {ticker}*\nPrix: ${float(price):.3f}\n\n💰 *Montant à investir ?* (ex: 100)",
        parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_amount, ticker)

@bot.callback_query_handler(func=lambda call: call.data == 'pass')
def handle_pass(call):
    bot.answer_callback_query(call.id, "Ignoré")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

def process_amount(message, ticker):
    try:
        amount = float(message.text.replace(',', '.'))
    except:
        bot.reply_to(message, "❌ Invalide")
        return
    curr = get_current_price_finnhub(ticker) or get_yahoo_info(ticker).get('currentPrice', 0)
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
    db_execute("INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (message.chat.id, ticker, buy_price, qty, amount, stop, tp1, tp2, tp3, buy_price, datetime.now().isoformat()))
    loss = amount - (stop / buy_price * amount)
    msg = f"✅ *POSITION OUVERTE*\n📊 {ticker}\n💰 {amount:.2f}€ | ${buy_price:.3f}\n📦 {qty:.4f} actions\n🛑 Stop ${stop} (perte max {loss:.2f}€)\n🎯 TP1 ${tp1} | TP2 ${tp2} | TP3 ${tp3}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['achat'])
def cmd_achat(message):
    try:
        parts = message.text.split()
        ticker = parts[1].upper()
        buy_price = float(parts[2].replace(',', '.'))
        amount = float(parts[3].replace(',', '.'))
    except:
        bot.reply_to(message, "❌ /achat TICKER PRIX MONTANT\nEx: /achat AAPL 150 100")
        return
    qty = amount / buy_price
    a = get_fast_analysis(ticker)
    stop = a['stop_loss'] if a else round(buy_price * 0.90, 2)
    tp1 = a['tp1'] if a else round(buy_price * 1.10, 2)
    tp2 = a['tp2'] if a else round(buy_price * 1.20, 2)
    tp3 = a['tp3'] if a else round(buy_price * 1.30, 2)
    db_execute("INSERT INTO positions (chat_id, ticker, buy_price, quantity, amount, stop_loss, take_profit1, take_profit2, take_profit3, highest_price, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (message.chat.id, ticker, buy_price, qty, amount, stop, tp1, tp2, tp3, buy_price, datetime.now().isoformat()))
    curr = get_current_price_finnhub(ticker) or get_yahoo_info(ticker).get('currentPrice', 0)
    if curr:
        var = ((curr - buy_price) / buy_price) * 100
        status = "🟢" if var >= 0 else "🔴"
        msg = f"✅ *ACHAT ENREGISTRÉ*\n📊 {ticker}\n💰 {amount:.2f}€ | ${buy_price:.3f}\n📦 {qty:.4f} actions\n{status} Prix actuel: ${curr:.3f} ({var:+.2f}%)\n\n🛑 Stop: ${stop} | 🎯 TP1: ${tp1}"
    else:
        msg = f"✅ *ACHAT ENREGISTRÉ*\n📊 {ticker}\n💰 {amount:.2f}€ | ${buy_price:.3f}\n📦 {qty:.4f} actions\n\n🛑 Stop: ${stop} | 🎯 TP1: ${tp1}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] > 10][:10]
    if not gainers:
        bot.reply_to(message, "Aucune opportunité explosive détectée.")
        return
    msg = "💥 *TOP OPPORTUNITÉS EXPLOSIVES*\n\n"
    for m in gainers:
        msg += f"🔥 *{m['ticker']}* +{m['change']}% | 💰 ${m['price']:.3f}\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marche'])
def cmd_marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    msg = "📈 *MARCHÉ EN DIRECT*\n\n"
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
        bot.reply_to(message, "📭 Portefeuille vide")
        return
    msg = "💼 *PORTEFEUILLE*\n\n"
    total_inv = total_val = 0
    for r in rows:
        curr = get_current_price_finnhub(r[2]) or get_yahoo_info(r[2]).get('currentPrice', r[3])
        val = r[4] * (curr / r[3])
        pnl = val - r[5]
        total_inv += r[5]
        total_val += val
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg += f"{emoji} *{r[2]}* {r[4]:.4f} act\n   {r[5]:.2f}€ → {val:.2f}€ | {pnl:+.2f}€\n\n"
    msg += f"━━━━━━\n💰 Total: {total_val:.2f}€ | P&L: {total_val-total_inv:+.2f}€"
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
        bot.reply_to(message, "❌ Aucune position trouvée")
        return
    curr = get_current_price_finnhub(ticker) or get_yahoo_info(ticker).get('currentPrice', row[3])
    val = row[4] * (curr / row[3])
    pnl = val - row[5]
    db_execute("INSERT INTO trades (chat_id, ticker, buy_price, sell_price, quantity, amount, profit_loss, profit_loss_pct, close_date) VALUES (?,?,?,?,?,?,?,?,?)",
               (message.chat.id, ticker, row[3], curr, row[4], row[5], pnl, (curr/row[3]-1)*100, datetime.now().isoformat()))
    db_execute("UPDATE positions SET status=? WHERE id=?", ('closed', row[0]))
    bot.reply_to(message, f"✅ *{ticker}* vendu\n{'🟢' if pnl>=0 else '🔴'} P&L: {pnl:+.2f}€", parse_mode='Markdown')

@bot.message_handler(commands=['historique'])
def cmd_historique(message):
    rows = db_fetchall("SELECT * FROM trades WHERE chat_id=? ORDER BY close_date DESC LIMIT 10", (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📋 Aucun historique")
        return
    msg = "📋 *HISTORIQUE*\n\n"
    for r in rows:
        msg += f"{'🟢' if r[7]>=0 else '🔴'} *{r[2]}* {r[7]:+.1f}% | {r[6]:+.2f}€\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    rows = db_fetchall("SELECT profit_loss FROM trades WHERE chat_id=?", (message.chat.id,))
    if not rows:
        bot.reply_to(message, "📊 Pas de statistiques")
        return
    wins = sum(1 for r in rows if r[0] > 0)
    total = sum(r[0] for r in rows)
    msg = f"📊 *STATISTIQUES*\n📈 Trades: {len(rows)}\n✅ Win rate: {wins/len(rows)*100:.1f}%\n💰 P&L total: {total:.2f}€\n🏆 Meilleur: {max(r[0] for r in rows):.2f}€\n💀 Pire: {min(r[0] for r in rows):.2f}€"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['alerte'])
def cmd_alerte(message):
    try:
        _, ticker, price = message.text.split()
        db_execute("INSERT INTO alerts (chat_id, ticker, target_price, date) VALUES (?,?,?,?)",
                   (message.chat.id, ticker.upper(), float(price), datetime.now().isoformat()))
        bot.reply_to(message, f"🔔 Alerte créée : {ticker.upper()} à ${price}")
    except:
        bot.reply_to(message, "❌ /alerte AAPL 200")

@bot.message_handler(commands=['import_csv'])
def cmd_import_csv(message):
    msg = bot.reply_to(message, "📁 Envoyez votre fichier CSV (Action,Quantité,Prix)")
    bot.register_next_step_handler(msg, process_csv)

def process_csv(message):
    content = message.document and bot.download_file(bot.get_file(message.document.file_id).file_path).decode('utf-8') or message.text
    if not content:
        bot.reply_to(message, "❌ Contenu vide")
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
        bot.reply_to(message, f"✅ {count} positions importées avec succès")
    except Exception as e:
        bot.reply_to(message, f"❌ Erreur : {e}")

# ==================== SURVEILLANCE AUTOMATIQUE ====================
def auto_scan():
    if not is_market_open():
        return
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] >= 20][:5]
    if gainers:
        msg = "🔔 *ALERTE AUTO - FORTES HAUSSES*\n\n"
        for m in gainers:
            a = get_fast_analysis(m['ticker'])
            stop = a['stop_loss'] if a else round(m['price'] * 0.95, 2)
            msg += f"🔥 *{m['ticker']}* +{m['change']}%\n"
            msg += f"   💰 ${m['price']:.3f} | 🛑 Stop ${stop}\n\n"
        send_alert_to_all(msg)

def monitor():
    for r in db_fetchall("SELECT * FROM positions WHERE status='open'"):
        curr = get_current_price_finnhub(r[2]) or get_yahoo_info(r[2]).get('currentPrice', 0)
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
                bot.send_message(r[1], f"🚨 *STOP LOSS ATTEINT* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass
        elif curr >= r[8]:
            try:
                bot.send_message(r[1], f"🎯 *TP3 ATTEINT* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass
        elif curr >= r[7]:
            try:
                bot.send_message(r[1], f"🎯 *TP2 ATTEINT* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass
        elif curr >= r[6]:
            try:
                bot.send_message(r[1], f"🎯 *TP1 ATTEINT* {r[2]} ${curr:.2f}", parse_mode='Markdown')
            except: pass

def check_critical_drops():
    for r in db_fetchall("SELECT * FROM positions WHERE status='open'"):
        curr = get_current_price_finnhub(r[2]) or get_yahoo_info(r[2]).get('currentPrice', 0)
        if not curr:
            continue
        loss_pct = ((curr / r[3]) - 1) * 100
        if loss_pct <= -10:
            amount = r[5]
            current_value = r[4] * (curr / r[3])
            loss_amount = current_value - amount
            try:
                bot.send_message(r[1], f"🚨 *BAISSE CRITIQUE* {r[2]} {loss_pct:.1f}%\nPerte: {loss_amount:.2f}€", parse_mode='Markdown')
            except: pass

def hourly_top_movers():
    if not is_market_open():
        return
    now = datetime.now()
    movers = scan_all_movers()
    gainers = [m for m in movers if m['change'] > 2][:5]
    if gainers:
        msg = f"📊 *TOP MOUVEMENTS - {now.strftime('%H:%M')}*\n\n"
        for m in gainers:
            msg += f"🔥 *{m['ticker']}* +{m['change']}%\n"
        send_alert_to_all(msg)

def run_scheduler():
    schedule.every(15).minutes.do(auto_scan)
    schedule.every(10).minutes.do(monitor)
    schedule.every(5).minutes.do(check_critical_drops)
    schedule.every(60).minutes.do(hourly_top_movers)
    while True:
        schedule.run_pending()
        time.sleep(10)

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
        return 'ok', 200
    return 'bad request', 400

@app.route('/')
def home():
    return "Bot Trader Pro - Opérationnel", 200

if __name__ == '__main__':
    print("🚀 Démarrage du bot révolutionnaire...")
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL + '/webhook')
    print(f"✅ Bot connecté sur {WEBHOOK_URL}")
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
