import os, telebot, requests, yfinance as yf, pandas as pd, numpy as np, pytz
import schedule, time, threading, json, io, csv
from datetime import datetime, timedelta
from flask import Flask, request
from telebot import types
from database import db

# ===== CONFIGURATION =====
TOKEN = os.environ.get('TELEGRAM_TOKEN')
FINNHUB_KEY = os.environ.get('FINNHUB_KEY')
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://votre-service.onrender.com')

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

subscribers = set()

# Watchlist élargie pour /scan et /explosive
WATCHLIST = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX',
    'SNAP', 'UBER', 'SQ', 'ROKU', 'ZM', 'CRWD', 'PLTR', 'GME', 'AMC',
    'RIVN', 'LCID', 'MARA', 'RIOT', 'COIN', 'NIO', 'XPEV', 'LI',
    'AFRM', 'UPST', 'SOFI', 'HOOD', 'RBLX', 'BABA', 'BIDU', 'JD',
    'MSTR', 'MRNA', 'PFE', 'BA', 'CCL', 'AAL', 'SPCE', 'NKLA',
    'SNDL', 'TLRY', 'ACB', 'CGC', 'FCEL', 'PLUG', 'QS', 'CHPT',
    'DDOG', 'SNOW', 'MDB', 'ZS', 'CRWD', 'NET', 'FSLY', 'U'
]

# ===== OUTILS D'ANALYSE TECHNIQUE =====
def get_full_analysis(ticker):
    """Analyse technique complète avec RSI, MACD, Bollinger, MA"""
    try:
        df = yf.download(ticker, period='6mo', progress=False)
        if df.empty: return None
        
        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()
        high = df['High'].squeeze()
        low = df['Low'].squeeze()
        
        current_price = close.iloc[-1]
        
        # RSI
        import ta
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        
        # MACD
        macd_ind = ta.trend.MACD(close)
        macd_line = macd_ind.macd().iloc[-1]
        macd_signal = macd_ind.macd_signal().iloc[-1]
        macd_histogram = macd_ind.macd_diff().iloc[-1]
        
        # Bandes de Bollinger
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_upper = bb.bollinger_hband().iloc[-1]
        bb_middle = bb.bollinger_mavg().iloc[-1]
        bb_lower = bb.bollinger_lband().iloc[-1]
        
        # Moyennes mobiles
        ma20 = close.rolling(20).mean().iloc[-1]
        ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else ma20
        ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else ma50
        
        # ATR pour stop/tp
        atr_ind = ta.volatility.AverageTrueRange(high, low, close, window=14)
        atr = atr_ind.average_true_range().iloc[-1]
        
        # Volume
        avg_volume = volume.iloc[-20:].mean()
        volume_ratio = volume.iloc[-1] / avg_volume if avg_volume > 0 else 1
        
        # Score technique 0-100
        score = 50
        
        # RSI scoring
        if 40 <= rsi <= 60: score += 15
        elif 30 <= rsi < 40: score += 10  # zone achat
        elif rsi < 30: score += 5  # survente
        
        # MACD scoring
        if macd_line > macd_signal: score += 10
        
        # Tendance vs MA
        if current_price > ma20: score += 5
        if current_price > ma50: score += 5
        if ma20 > ma50: score += 5
        
        # Volume scoring
        if volume_ratio > 2: score += 10
        elif volume_ratio > 1.5: score += 5
        
        # Bollinger position
        if current_price < bb_lower: score += 10  # survendu
        elif current_price > bb_upper: score -= 5  # suracheté
        
        score = min(100, max(0, score))
        
        # Stop-loss et Take-profit basés sur ATR
        stop_loss = round(current_price - 2 * atr, 2)
        tp1 = round(current_price + 2 * atr, 2)
        tp2 = round(current_price + 4 * atr, 2)
        
        return {
            'ticker': ticker,
            'price': round(current_price, 2),
            'rsi': round(rsi, 1),
            'macd_line': round(macd_line, 2),
            'macd_signal': round(macd_signal, 2),
            'macd_histogram': round(macd_histogram, 4),
            'bb_upper': round(bb_upper, 2),
            'bb_middle': round(bb_middle, 2),
            'bb_lower': round(bb_lower, 2),
            'ma20': round(ma20, 2),
            'ma50': round(ma50, 2),
            'ma200': round(ma200, 2),
            'atr': round(atr, 2),
            'volume_ratio': round(volume_ratio, 1),
            'score': score,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2
        }
    except Exception as e:
        print(f"Erreur analyse {ticker}: {e}")
        return None

def detect_breakouts():
    """Détecte les actions avec volume ×3 et hausse du prix"""
    breakouts = []
    for ticker in WATCHLIST[:50]:
        try:
            df = yf.download(ticker, period='5d', progress=False)
            if len(df) < 2: continue
            
            close = df['Close'].squeeze()
            volume = df['Volume'].squeeze()
            
            current_vol = volume.iloc[-1]
            avg_vol_5d = volume.iloc[:-1].mean()
            
            if avg_vol_5d > 0 and current_vol > 3 * avg_vol_5d:
                price_change = ((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) * 100
                if price_change > 0:
                    analysis = get_full_analysis(ticker)
                    if analysis:
                        breakouts.append({
                            'ticker': ticker,
                            'price': close.iloc[-1],
                            'volume_ratio': round(current_vol / avg_vol_5d, 1),
                            'price_change': round(price_change, 2),
                            'score': analysis['score']
                        })
        except:
            pass
        time.sleep(0.2)
    
    breakouts.sort(key=lambda x: x['score'], reverse=True)
    return breakouts[:5]

# ===== COMMANDES TELEGRAM =====
@bot.message_handler(commands=['start'])
def cmd_start(message):
    subscribers.add(message.chat.id)
    msg = """🚀 *TRADER PRO BOT*

✅ Bot professionnel activé !

*Commandes :*
📊 /analyse TICKER - Analyse technique complète
💥 /explosive - Top 5 opportunités
🔍 /scan - Détection de breakouts
📈 /marché - Indices en direct
💼 /portfolio - Vos positions
📋 /historique - Derniers trades
📊 /stats - Performance globale
🔔 /alerte TICKER PRIX - Créer une alerte
📢 /alertes - Liste des alertes
📁 /import_csv - Importer depuis Trading212
📚 /aide - Guide complet"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['aide'])
def cmd_aide(message):
    msg = """📚 *GUIDE COMPLET*

*/analyse TICKER*
→ RSI, MACD, Bollinger, MA20/50/200
→ Score /100, Stop-loss, Take-profit
→ Boutons Buy avec choix quantité

*/explosive*
→ Top 5 des meilleurs scores

*/scan*
→ Détection de breakouts (volume ×3 + hausse)

*/marché*
→ S&P500, Nasdaq, Dow Jones, VIX

*/portfolio*
→ Positions ouvertes avec P&L temps réel

*/vendre TICKER*
→ Ferme une position

*/historique*
→ 10 derniers trades fermés

*/stats*
→ Win rate, P&L total, best/worst trade

*/alerte TICKER PRIX*
→ Crée une alerte prix

*/import_csv*
→ Import positions depuis Trading212"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['analyse'])
def cmd_analyse(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /analyse AAPL")
        return
    
    bot.send_chat_action(message.chat.id, 'typing')
    analysis = get_full_analysis(ticker)
    
    if not analysis:
        bot.reply_to(message, f"❌ Données indisponibles pour {ticker}")
        return
    
    msg = f"""📊 *ANALYSE TECHNIQUE - {ticker}*

⭐ *Score : {analysis['score']}/100*
💰 Prix : ${analysis['price']}

📈 *Indicateurs :*
• RSI (14) : {analysis['rsi']}
• MACD : {analysis['macd_line']} | Signal : {analysis['macd_signal']}
• Bandes Bollinger :
  Haut : ${analysis['bb_upper']}
  Milieu : ${analysis['bb_middle']}
  Bas : ${analysis['bb_lower']}

📐 *Moyennes Mobiles :*
• MA20 : ${analysis['ma20']}
• MA50 : ${analysis['ma50']}
• MA200 : ${analysis['ma200']}

📊 *Volume :* {analysis['volume_ratio']}x la moyenne
📏 *ATR :* ${analysis['atr']}

🛡️ *Gestion du Risque :*
• 🛑 Stop-Loss : ${analysis['stop_loss']}
• 🎯 TP1 : ${analysis['tp1']} (+{round((analysis['tp1']/analysis['price']-1)*100)}%)
• 🎯 TP2 : ${analysis['tp2']} (+{round((analysis['tp2']/analysis['price']-1)*100)}%)"""
    
    # Boutons de quantité
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("🛒 1", callback_data=f"buy_{ticker}_{analysis['price']}_1"),
        types.InlineKeyboardButton("🛒 5", callback_data=f"buy_{ticker}_{analysis['price']}_5"),
        types.InlineKeyboardButton("🛒 10", callback_data=f"buy_{ticker}_{analysis['price']}_10"),
        types.InlineKeyboardButton("❌ Pass", callback_data="pass")
    )
    
    bot.reply_to(message, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy(call):
    parts = call.data.split('_')
    ticker = parts[1]
    price = float(parts[2])
    qty = int(parts[3])
    
    bot.answer_callback_query(call.id, f"Achat de {qty} {ticker} à ~${price:.2f}")
    
    msg = bot.send_message(
        call.message.chat.id,
        f"🛒 *Achat {ticker}*\n\nQuantité : {qty}\nPrix indicatif : ${price:.2f}\n\nQuel est votre prix d'achat exact ?",
        parse_mode='Markdown'
    )
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
    
    analysis = get_full_analysis(ticker)
    if analysis:
        stop = analysis['stop_loss']
        tp1 = analysis['tp1']
        tp2 = analysis['tp2']
    else:
        stop = round(buy_price * 0.95, 2)
        tp1 = round(buy_price * 1.10, 2)
        tp2 = round(buy_price * 1.20, 2)
    
    db.add_position(message.chat.id, ticker, buy_price, qty, stop, tp1, tp2)
    
    msg = f"""✅ *POSITION OUVERTE*

📊 *{ticker}*
🛒 {qty} action(s) à ${buy_price:.2f}
💰 Total : ${buy_price * qty:.2f}
🛑 Stop : ${stop}
🎯 TP1 : ${tp1}
🎯 TP2 : ${tp2}

🔍 Surveillance intensive activée"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['explosive'])
def cmd_explosive(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    results = []
    for ticker in WATCHLIST[:40]:
        analysis = get_full_analysis(ticker)
        if analysis and analysis['score'] >= 60:
            results.append(analysis)
        time.sleep(0.2)
    
    results.sort(key=lambda x: x['score'], reverse=True)
    top = results[:5]
    
    if not top:
        bot.reply_to(message, "Aucune opportunité forte détectée.")
        return
    
    msg = "💥 *TOP 5 OPPORTUNITÉS*\n\n"
    for i, a in enumerate(top, 1):
        msg += f"*{i}. {a['ticker']}* - Score {a['score']}/100\n"
        msg += f"   💰 ${a['price']} | RSI {a['rsi']} | Vol {a['volume_ratio']}x\n"
        msg += f"   🛑 Stop ${a['stop_loss']} | 🎯 TP1 ${a['tp1']}\n\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['scan'])
def cmd_scan(message):
    bot.send_chat_action(message.chat.id, 'typing')
    breakouts = detect_breakouts()
    
    if not breakouts:
        bot.reply_to(message, "🔍 Aucun breakout détecté (volume ×3 + hausse).")
        return
    
    msg = "🔍 *BREAKOUTS DÉTECTÉS*\n\n"
    for b in breakouts:
        msg += f"🚀 *{b['ticker']}* - Score {b['score']}/100\n"
        msg += f"   Vol ×{b['volume_ratio']} | +{b['price_change']}%\n"
        msg += f"   💰 ${b['price']:.2f}\n\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['marché'])
def cmd_marche(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    indices = {
        '^GSPC': 'S&P 500',
        '^IXIC': 'Nasdaq',
        '^DJI': 'Dow Jones',
        '^VIX': 'VIX'
    }
    
    msg = "📈 *INDICES EN DIRECT*\n\n"
    
    for ticker, name in indices.items():
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period='2d')
            if len(hist) >= 2:
                curr = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                change = ((curr - prev) / prev) * 100
                emoji = "🟢" if change > 0 else "🔴"
                msg += f"{emoji} *{name}* : {curr:,.2f} ({change:+.2f}%)\n"
        except:
            pass
    
    msg += f"\n🕐 {datetime.now().strftime('%H:%M:%S')}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    positions = db.get_positions(message.chat.id)
    
    if not positions:
        bot.reply_to(message, "📭 Portefeuille vide.\n\n/analyse TICKER pour commencer")
        return
    
    msg = "💼 *PORTEFEUILLE*\n\n"
    total_invested = 0
    total_current = 0
    
    for pos in positions:
        current_price = db.get_current_price(pos['ticker']) or pos['buy_price']
        invested = pos['buy_price'] * pos['quantity']
        current_value = current_price * pos['quantity']
        pnl = current_value - invested
        pnl_pct = ((current_price / pos['buy_price']) - 1) * 100
        
        total_invested += invested
        total_current += current_value
        
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg += f"{emoji} *{pos['ticker']}* ×{pos['quantity']}\n"
        msg += f"   Achat ${pos['buy_price']:.2f} | Actuel ${current_price:.2f}\n"
        msg += f"   P&L ${pnl:.2f} ({pnl_pct:+.1f}%)\n"
        msg += f"   🛑 Stop ${pos['stop_loss']} | 🎯 TP1 ${pos['take_profit1']}\n\n"
    
    total_pnl = total_current - total_invested
    total_pnl_pct = ((total_current / total_invested) - 1) * 100 if total_invested > 0 else 0
    
    msg += "━━━━━━━━━━━━━━━━\n"
    msg += f"💰 Investi : ${total_invested:.2f}\n"
    msg += f"📊 Valeur : ${total_current:.2f}\n"
    msg += f"📈 P&L Total : ${total_pnl:.2f} ({total_pnl_pct:+.1f}%)\n"
    msg += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['vendre'])
def cmd_vendre(message):
    try:
        ticker = message.text.split()[1].upper()
    except:
        bot.reply_to(message, "❌ /vendre TICKER")
        return
    
    result = db.close_position_by_ticker(message.chat.id, ticker)
    
    if result:
        emoji = "🟢" if result['profit_loss'] >= 0 else "🔴"
        msg = f"✅ *Position fermée*\n\n"
        msg += f"📊 *{ticker}*\n"
        msg += f"{emoji} P&L : ${result['profit_loss']:.2f} ({result['profit_loss_pct']:+.1f}%)\n\n"
        msg += "📋 /historique pour voir vos trades"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, f"❌ Aucune position ouverte pour {ticker}")

@bot.message_handler(commands=['historique'])
def cmd_historique(message):
    trades = db.get_history(message.chat.id)
    
    if not trades:
        bot.reply_to(message, "📋 Aucun trade fermé.")
        return
    
    msg = "📋 *10 DERNIERS TRADES*\n\n"
    for t in trades:
        emoji = "🟢" if t['profit_loss'] >= 0 else "🔴"
        msg += f"{emoji} *{t['ticker']}* ×{t['quantity']}\n"
        msg += f"   Achat ${t['buy_price']:.2f} → Vente ${t['sell_price']:.2f}\n"
        msg += f"   P&L ${t['profit_loss']:.2f} ({t['profit_loss_pct']:+.1f}%)\n"
        msg += f"   {t['close_date'][:10]}\n\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    stats = db.get_stats(message.chat.id)
    
    if stats['total_trades'] == 0:
        bot.reply_to(message, "📊 Aucune statistique disponible.")
        return
    
    msg = "📊 *STATISTIQUES DE TRADING*\n\n"
    msg += f"📈 Trades : {stats['total_trades']}\n"
    msg += f"✅ Win Rate : {stats['win_rate']}%\n"
    msg += f"💰 P&L Total : ${stats['total_pnl']:.2f}\n"
    msg += f"📊 P&L Moyen : ${stats['avg_pnl']:.2f}\n"
    msg += f"🏆 Meilleur Trade : ${stats['best_trade']:.2f}\n"
    msg += f"💀 Pire Trade : ${stats['worst_trade']:.2f}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['alerte'])
def cmd_alerte(message):
    try:
        parts = message.text.split()
        ticker = parts[1].upper()
        target_price = float(parts[2])
    except:
        bot.reply_to(message, "❌ /alerte TICKER PRIX\nExemple : /alerte AAPL 200")
        return
    
    db.add_alert(message.chat.id, ticker, target_price)
    bot.reply_to(message, f"🔔 Alerte créée : {ticker} à ${target_price:.2f}")

@bot.message_handler(commands=['alertes'])
def cmd_alertes(message):
    alerts = db.get_alerts(message.chat.id)
    
    if not alerts:
        bot.reply_to(message, "📢 Aucune alerte active.")
        return
    
    msg = "📢 *ALERTES ACTIVES*\n\n"
    for a in alerts:
        current = db.get_current_price(a['ticker'])
        current_str = f"${current:.2f}" if current else "N/A"
        msg += f"🔔 *{a['ticker']}* → ${a['target_price
