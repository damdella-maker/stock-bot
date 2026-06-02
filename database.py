import sqlite3
import json
from datetime import datetime
from threading import Lock

class Database:
    def __init__(self, db_path='trading_bot.db'):
        self.db_path = db_path
        self.lock = Lock()
        self.init_db()
    
    def init_db(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # Table positions
            c.execute('''CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                buy_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                stop_loss REAL,
                take_profit1 REAL,
                take_profit2 REAL,
                trailing_stop REAL,
                highest_price REAL,
                open_date TEXT,
                status TEXT DEFAULT 'open',
                source TEXT DEFAULT 'manual'
            )''')
            
            # Table historique
            c.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                buy_price REAL,
                sell_price REAL,
                quantity INTEGER,
                profit_loss REAL,
                profit_loss_pct REAL,
                open_date TEXT,
                close_date TEXT
            )''')
            
            # Table alertes
            c.execute('''CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                target_price REAL NOT NULL,
                alert_type TEXT DEFAULT 'price',
                active INTEGER DEFAULT 1,
                created_date TEXT
            )''')
            
            conn.commit()
            conn.close()
    
    # ----- POSITIONS -----
    def add_position(self, chat_id, ticker, buy_price, quantity, stop_loss=None, tp1=None, tp2=None, source='manual'):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''INSERT INTO positions 
                (chat_id, ticker, buy_price, quantity, stop_loss, take_profit1, take_profit2, highest_price, open_date, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (chat_id, ticker.upper(), buy_price, quantity, 
                 stop_loss or round(buy_price * 0.95, 2),
                 tp1 or round(buy_price * 1.10, 2),
                 tp2 or round(buy_price * 1.20, 2),
                 buy_price,
                 datetime.now().isoformat(),
                 source))
            conn.commit()
            conn.close()
    
    def get_positions(self, chat_id):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT * FROM positions WHERE chat_id = ? AND status = ?', (chat_id, 'open'))
            rows = c.fetchall()
            conn.close()
            return [{
                'id': r[0], 'ticker': r[2], 'buy_price': r[3], 'quantity': r[4],
                'stop_loss': r[5], 'take_profit1': r[6], 'take_profit2': r[7],
                'trailing_stop': r[8], 'highest_price': r[9], 'open_date': r[10],
                'source': r[12]
            } for r in rows]
    
    def update_highest_price(self, position_id, price):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE positions SET highest_price = MAX(highest_price, ?) WHERE id = ?', (price, position_id))
            conn.commit()
            conn.close()
    
    def update_trailing_stop(self, position_id, trailing_stop):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE positions SET trailing_stop = ? WHERE id = ?', (trailing_stop, position_id))
            conn.commit()
            conn.close()
    
    def close_position(self, position_id, sell_price):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # Récupérer la position
            c.execute('SELECT * FROM positions WHERE id = ?', (position_id,))
            pos = c.fetchone()
            if not pos:
                conn.close()
                return None
            
            # Calculer P&L
            buy_price = pos[3]
            quantity = pos[4]
            profit_loss = (sell_price - buy_price) * quantity
            profit_loss_pct = ((sell_price / buy_price) - 1) * 100
            
            # Ajouter à l'historique
            c.execute('''INSERT INTO trades 
                (chat_id, ticker, buy_price, sell_price, quantity, profit_loss, profit_loss_pct, open_date, close_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (pos[1], pos[2], buy_price, sell_price, quantity, 
                 profit_loss, profit_loss_pct, pos[10], datetime.now().isoformat()))
            
            # Marquer comme fermée
            c.execute('UPDATE positions SET status = ? WHERE id = ?', ('closed', position_id))
            
            conn.commit()
            conn.close()
            return {'ticker': pos[2], 'profit_loss': profit_loss, 'profit_loss_pct': profit_loss_pct}
    
    def close_position_by_ticker(self, chat_id, ticker):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT id FROM positions WHERE chat_id = ? AND ticker = ? AND status = ?', 
                     (chat_id, ticker.upper(), 'open'))
            row = c.fetchone()
            conn.close()
            if row:
                return self.close_position(row[0], self.get_current_price(ticker))
        return None
    
    # ----- HISTORIQUE -----
    def get_history(self, chat_id, limit=10):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT * FROM trades WHERE chat_id = ? ORDER BY close_date DESC LIMIT ?', 
                     (chat_id, limit))
            rows = c.fetchall()
            conn.close()
            return [{
                'ticker': r[2], 'buy_price': r[3], 'sell_price': r[4],
                'quantity': r[5], 'profit_loss': r[6], 'profit_loss_pct': r[7],
                'close_date': r[9]
            } for r in rows]
    
    def get_stats(self, chat_id):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            c.execute('SELECT COUNT(*), SUM(profit_loss), AVG(profit_loss_pct) FROM trades WHERE chat_id = ?', (chat_id,))
            total_trades, total_pnl, avg_pnl = c.fetchone()
            
            c.execute('SELECT COUNT(*) FROM trades WHERE chat_id = ? AND profit_loss > 0', (chat_id,))
            wins = c.fetchone()[0]
            
            c.execute('SELECT MIN(profit_loss), MAX(profit_loss) FROM trades WHERE chat_id = ?', (chat_id,))
            worst, best = c.fetchone()
            
            conn.close()
            
            win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
            
            return {
                'total_trades': total_trades or 0,
                'win_rate': round(win_rate, 1),
                'total_pnl': round(total_pnl or 0, 2),
                'avg_pnl': round(avg_pnl or 0, 2),
                'best_trade': round(best or 0, 2),
                'worst_trade': round(worst or 0, 2)
            }
    
    # ----- ALERTES -----
    def add_alert(self, chat_id, ticker, target_price):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('INSERT INTO alerts (chat_id, ticker, target_price, created_date) VALUES (?, ?, ?, ?)',
                     (chat_id, ticker.upper(), target_price, datetime.now().isoformat()))
            conn.commit()
            conn.close()
    
    def get_alerts(self, chat_id):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT * FROM alerts WHERE chat_id = ? AND active = 1', (chat_id,))
            rows = c.fetchall()
            conn.close()
            return [{'id': r[0], 'ticker': r[2], 'target_price': r[3], 'created_date': r[5]} for r in rows]
    
    def get_all_active_alerts(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT * FROM alerts WHERE active = 1')
            rows = c.fetchall()
            conn.close()
            return [{'id': r[0], 'chat_id': r[1], 'ticker': r[2], 'target_price': r[3]} for r in rows]
    
    def deactivate_alert(self, alert_id):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE alerts SET active = 0 WHERE id = ?', (alert_id,))
            conn.commit()
            conn.close()
    
    # ----- UTILITAIRES -----
    def get_current_price(self, ticker):
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            hist = stock.history(period='1d')
            if not hist.empty:
                return hist['Close'].iloc[-1]
        except:
            pass
        return None
    
    def get_all_open_positions(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT * FROM positions WHERE status = ?', ('open',))
            rows = c.fetchall()
            conn.close()
            return [{
                'id': r[0], 'chat_id': r[1], 'ticker': r[2], 'buy_price': r[3],
                'quantity': r[4], 'stop_loss': r[5], 'take_profit1': r[6],
                'take_profit2': r[7], 'highest_price': r[9]
            } for r in rows]

# Instance globale
db = Database()
