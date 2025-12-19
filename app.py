import json
import requests
import xml.etree.ElementTree as ET
import html
import time
from datetime import datetime, timedelta

# Backend: Flask Application
from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from pycoingecko import CoinGeckoAPI
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = '69493c769071310bece8c5bf01dffb3a6fdc6b521b91fd5a' # Cryptographically secure secret key

# Flask-Login Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

import os
cg = CoinGeckoAPI()
# Production: Use persistent disk mount if available (e.g. Render)
DATABASE = os.environ.get('DATABASE_URL', 'portfolio.db')

# --- User Class ---
class User(UserMixin):
    def __init__(self, id, name, phone, password_hash, currency='USD'):
        self.id = id
        self.name = name
        self.phone = phone
        self.password_hash = password_hash
        self.currency = currency

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    user_data = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    if user_data:
        # Handle potential None or missing key safely
        currency = 'USD'
        try:
             # Check if key exists and value is not None
             if 'currency' in user_data.keys() and user_data['currency']:
                 currency = user_data['currency']
        except:
             pass
        return User(user_data['id'], user_data['name'], user_data['phone'], user_data['password_hash'], currency)
    return None

# --- Database Helpers ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# --- Global News Cache ---
news_cache = {
    'items': [],
    'last_updated': 0
}

# --- Global Market Explorer Cache ---
market_explorer_cache = {
    'data': {}, # symbol: data
    'last_updated': 0
}

def get_market_news():
    global news_cache
    # Cache for 10 minutes (600 seconds)
    if time.time() - news_cache['last_updated'] < 600 and news_cache['items']:
        return news_cache['items']
    
    url = "https://cointelegraph.com/rss"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            # Use a more robust way to find items (handling potential namespaces)
            import re
            content = response.text
            # Simple regex to find items if ET fails or to be safe
            items_raw = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)
            
            parsed_items = []
            for item_content in items_raw[:5]:
                title_match = re.search(r'<title>(.*?)</title>', item_content, re.DOTALL)
                link_match = re.search(r'<link>(.*?)</link>', item_content, re.DOTALL)
                date_match = re.search(r'<pubDate>(.*?)</pubDate>', item_content, re.DOTALL)
                
                if title_match and link_match:
                    title = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', title_match.group(1)).strip()
                    link = link_match.group(1).strip()
                    date = date_match.group(1).strip() if date_match else ""
                    
                    parsed_items.append({
                        'title': html.unescape(title),
                        'link': link,
                        'date': date
                    })
            
            if parsed_items:
                news_cache['items'] = parsed_items
                news_cache['last_updated'] = time.time()
                return parsed_items
    except Exception as e:
        print(f"News fetch error: {e}")
    return news_cache['items']

def init_db():
    conn = get_db_connection()
    # Create Users Table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            currency TEXT DEFAULT 'USD'
        )
    ''')
    # Create Portfolio Table with User ID
    conn.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            avg_buy_price REAL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Check if avg_buy_price column exists (Migration for existing users)
    cursor = conn.execute("PRAGMA table_info(portfolio)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'avg_buy_price' not in columns:
        print("Migrating DB: Adding avg_buy_price to portfolio")
        conn.execute('ALTER TABLE portfolio ADD COLUMN avg_buy_price REAL DEFAULT 0')
    
    # Check if currency column exists in users
    cursor = conn.execute("PRAGMA table_info(users)")
    user_cols = [row[1] for row in cursor.fetchall()]
    if 'currency' not in user_cols:
        print("Migrating DB: Adding currency to users")
        conn.execute("ALTER TABLE users ADD COLUMN currency TEXT DEFAULT 'USD'")
    
    # Create Transactions Table (History)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            type TEXT NOT NULL, -- 'BUY' or 'SELL'
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Create Alerts Table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            target_price REAL NOT NULL,
            condition TEXT NOT NULL, -- 'ABOVE' or 'BELOW'
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Initialize DB on startup
init_db()

# --- Auth Routes ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        phone = request.form['phone']
        password = request.form['password']
        hashed_password = generate_password_hash(password)
        
        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO users (name, phone, password_hash) VALUES (?, ?, ?)', (name, phone, hashed_password))
            conn.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Phone number already registered.', 'error')
        finally:
            conn.close()
            
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name']
        password = request.form['password']
        
        conn = get_db_connection()
        user_data = conn.execute('SELECT * FROM users WHERE name = ?', (name,)).fetchone()
        conn.close()
        
        if user_data and check_password_hash(user_data['password_hash'], password):
            # Pass currency if it exists safely
            currency = 'USD'
            try:
                 if 'currency' in user_data.keys() and user_data['currency']:
                     currency = user_data['currency']
            except:
                 pass
                 
            user = User(user_data['id'], user_data['name'], user_data['phone'], user_data['password_hash'], currency)
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Invalid name or password.', 'error')
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- Globals ---
# Fetch top coins for autocomplete (Cached on startup)
SUPPORTED_COINS = []
try:
    # Fetch top 250 coins by market cap
    coins_list = cg.get_coins_markets(vs_currency='usd', order='market_cap_desc', per_page=250, page=1)
    SUPPORTED_COINS = [{'id': c['id'], 'symbol': c['symbol'], 'name': c['name']} for c in coins_list]
    # Create lookup maps for speed
    COIN_ID_MAP = {c['id']: c['id'] for c in SUPPORTED_COINS}
    COIN_SYMBOL_MAP = {c['symbol']: c['id'] for c in SUPPORTED_COINS}
    COIN_NAME_MAP = {c['name'].lower(): c['id'] for c in SUPPORTED_COINS}
    print(f"Loaded {len(SUPPORTED_COINS)} coins for autocomplete.")
except Exception as e:
    print(f"Error loading coin list: {e}")

CURRENCY_SYMBOLS = {
    'usd': '$', 'eur': '€', 'gbp': '£', 'inr': '₹', 'jpy': '¥', 'aud': 'A$', 'cad': 'C$'
}

# --- App Routes ---
@app.route('/')
@login_required
def index():
    # Safely handle currency with defaults
    user_currency = getattr(current_user, 'currency', 'USD')
    if not user_currency: user_currency = 'USD'
    user_currency = user_currency.lower()
    
    currency_symbol = CURRENCY_SYMBOLS.get(user_currency, user_currency.upper() + ' ')

    conn = get_db_connection()
    portfolio_items = conn.execute('SELECT * FROM portfolio WHERE user_id = ?', (current_user.id,)).fetchall()
    conn.close()

    portfolio_data = []
    total_value = 0
    total_cost = 0
    
    # helper to resolve symbol -> id
    def resolve_coingecko_id(search_term):
        search_term = search_term.lower().strip()
        # Use pre-computed maps for O(1) lookup
        return COIN_ID_MAP.get(search_term) or \
               COIN_SYMBOL_MAP.get(search_term) or \
               COIN_NAME_MAP.get(search_term) or \
               search_term
    
    # Resolve IDs for all items
    resolved_ids_map = {} # db_symbol -> api_id
    api_ids = []
    
    if portfolio_items:
        for item in portfolio_items:
            db_symbol = item['symbol']
            api_id = resolve_coingecko_id(db_symbol)
            resolved_ids_map[db_symbol] = api_id
            api_ids.append(api_id)
            
        api_ids = list(set(api_ids))

    market_data_map = {}
    if api_ids:
        try:
            # Fetch detailed data including 7d sparkline in USER CURRENCY
            data = cg.get_coins_markets(vs_currency=user_currency, ids=api_ids, sparkline=True, price_change_percentage='24h')
            # Map by API ID
            market_data_map = {item['id']: item for item in data}
        except Exception as e:
            print(f"API Error: {e}")

    for item in portfolio_items:
        db_symbol = item['symbol']
        api_id = resolved_ids_map.get(db_symbol, db_symbol)
        
        quantity = item['quantity']
        avg_buy_price = item['avg_buy_price'] 
        
        coin_data = market_data_map.get(api_id)
        
        # Fallback lookups (just in case API returns something slightly diff or we failed resolution?)
        if not coin_data:
             for k, v in market_data_map.items():
                 if v['symbol'] == db_symbol or v['id'] == db_symbol:
                     coin_data = v
                     break

        price = 0
        change_24h = 0
        sparkline = []
        name = db_symbol.capitalize()
        image = ''
        
        if coin_data:
            price = coin_data.get('current_price', 0)
            change_24h = coin_data.get('price_change_percentage_24h', 0)
            name = coin_data.get('name', name)
            image = coin_data.get('image', '')
            if 'sparkline_in_7d' in coin_data:
                sparkline = coin_data['sparkline_in_7d'].get('price', [])
        
        value = quantity * price
        
        # Cost Basis Issue:
        # We will assume cost basis is in same currency for simplicity in this MVP 
        # or we just hide P/L if currency doesn't match?
        # Let's display it as is.
        cost = quantity * avg_buy_price
        
        total_value += value
        total_cost += cost

        # Calculate P/L
        pl_amount = value - cost
        pl_percent = (pl_amount / cost * 100) if cost > 0 else 0

        portfolio_data.append({
            'id': item['id'],
            'symbol': db_symbol,
            'name': name,
            'image': image,
            'quantity': quantity,
            'price': price,
            'value': value,
            'avg_buy_price': avg_buy_price,
            'pl_amount': pl_amount,
            'pl_percent': pl_percent,
            'change_24h': change_24h,
            'sparkline': sparkline
        })

    # Portfolio Stats
    total_pl = total_value - total_cost
    total_roi = (total_pl / total_cost * 100) if total_cost > 0 else 0

    # Fetch news
    news = get_market_news()

    return render_template('index.html', 
                           portfolio=portfolio_data, 
                           total_value=total_value,
                           total_pl=total_pl,
                           total_roi=total_roi,
                           username=current_user.name,
                           portfolio_json=json.dumps(portfolio_data),
                           all_coins=SUPPORTED_COINS,
                           currency_symbol=currency_symbol,
                           current_currency=user_currency.upper(),
                           news=news)

@app.route('/set_currency', methods=['GET', 'POST'])
@login_required
def set_currency():
    if request.method == 'POST':
        new_currency = request.form.get('currency', 'USD')
        conn = get_db_connection()
        conn.execute('UPDATE users SET currency = ? WHERE id = ?', (new_currency, current_user.id))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/add', methods=['POST'])
@login_required
def add_coin():
    symbol = request.form['symbol'].lower()
    quantity = float(request.form['quantity'])
    trade_type = request.form.get('trade_type', 'BUY').upper()
    # Optional price, default to 0
    trade_price = float(request.form.get('buy_price') or 0)

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check current holding
    cursor.execute('SELECT id, quantity, avg_buy_price FROM portfolio WHERE symbol = ? AND user_id = ?', (symbol, current_user.id))
    result = cursor.fetchone()
    
    if trade_type == 'BUY':
        # 1. Log Transaction
        cursor.execute('''
            INSERT INTO transactions (user_id, symbol, type, quantity, price)
            VALUES (?, ?, 'BUY', ?, ?)
        ''', (current_user.id, symbol, quantity, trade_price))
        
        if result:
            # Existing coin: Update weighted average
            curr_qty = result['quantity']
            curr_avg = result['avg_buy_price']
            
            total_cost_existing = curr_qty * curr_avg
            total_cost_new = quantity * trade_price
            
            new_quantity = curr_qty + quantity
            new_avg_price = (total_cost_existing + total_cost_new) / new_quantity if new_quantity > 0 else 0
            
            cursor.execute('UPDATE portfolio SET quantity = ?, avg_buy_price = ? WHERE symbol = ? AND user_id = ?', 
                           (new_quantity, new_avg_price, symbol, current_user.id))
        else:
            # New coin
            cursor.execute('INSERT INTO portfolio (user_id, symbol, quantity, avg_buy_price) VALUES (?, ?, ?, ?)', 
                           (current_user.id, symbol, quantity, trade_price))
        flash(f'Successfully bought {quantity} {symbol.upper()}', 'success')

    elif trade_type == 'SELL':
        if not result or result['quantity'] < quantity:
            flash(f'Error: Insufficient {symbol.upper()} quantity to sell.', 'error')
        else:
            # 1. Log Transaction
            cursor.execute('''
                INSERT INTO transactions (user_id, symbol, type, quantity, price)
                VALUES (?, ?, 'SELL', ?, ?)
            ''', (current_user.id, symbol, quantity, trade_price))
            
            new_quantity = result['quantity'] - quantity
            if new_quantity <= 0:
                cursor.execute('DELETE FROM portfolio WHERE id = ?', (result['id'],))
                flash(f'Successfully sold all {symbol.upper()}', 'success')
            else:
                # Keep same avg_buy_price (cost basis doesn't change on sell)
                cursor.execute('UPDATE portfolio SET quantity = ? WHERE id = ?', (new_quantity, result['id']))
                flash(f'Successfully sold {quantity} {symbol.upper()}', 'success')
                
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete/<int:id>')
@login_required
def delete_coin(id):
    conn = get_db_connection()
    # Fetch item before deleting to log it
    item = conn.execute('SELECT * FROM portfolio WHERE id = ? AND user_id = ?', (id, current_user.id)).fetchone()
    
    if item:
        # Log as SELL (assuming selling at current market price roughly, OR just 0 if unknown, 
        # but better to log the exit. Since we don't have price here easily without fetching,
        # we will fetch price or just log the quantity and symbol.
        # Fetching price makes it slow but accurate.
        try:
            # Quick fetch for accuracy
            price_data = cg.get_price(ids=item['symbol'], vs_currencies='usd') # Default to USD for base recording
            # Wait, our transactions table stores 'price'. 
            # If we want to support multi-currency properly later, we should store currency in transactions.
            # For now, we store in USD (or whatever base is). 
            # Simplification: Store 0 or try fetch.
            current_price = price_data.get(item['symbol'], {}).get('usd', 0)
        except:
            current_price = 0

        conn.execute('''
            INSERT INTO transactions (user_id, symbol, type, quantity, price)
            VALUES (?, ?, 'SELL', ?, ?)
        ''', (current_user.id, item['symbol'], item['quantity'], current_price))

        conn.execute('DELETE FROM portfolio WHERE id = ? AND user_id = ?', (id, current_user.id))
    
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/history')
@login_required
def history():
    user_currency = current_user.currency.lower()
    currency_symbol = CURRENCY_SYMBOLS.get(user_currency, user_currency.upper() + ' ')
    
    conn = get_db_connection()
    transactions = conn.execute('SELECT * FROM transactions WHERE user_id = ? ORDER BY date DESC', (current_user.id,)).fetchall()
    conn.close()
    
    return render_template('history.html', 
                           transactions=transactions,
                           currency_symbol=currency_symbol)

@app.route('/export/history')
@login_required
def export_history():
    import io
    import csv
    from flask import Response

    conn = get_db_connection()
    transactions = conn.execute('SELECT * FROM transactions WHERE user_id = ? ORDER BY date DESC', (current_user.id,)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Symbol', 'Type', 'Quantity', 'Price'])
    
    for tx in transactions:
        writer.writerow([tx['date'], tx['symbol'].upper(), tx['type'], tx['quantity'], tx['price']])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=crypto_history.csv"}
    )

@app.route('/api/dashboard_data')
@login_required
def dashboard_data():
    user_currency = getattr(current_user, 'currency', 'USD').lower()
    currency_symbol = CURRENCY_SYMBOLS.get(user_currency, user_currency.upper() + ' ')
    
    conn = get_db_connection()
    portfolio_items = conn.execute('SELECT * FROM portfolio WHERE user_id = ?', (current_user.id,)).fetchall()
    conn.close()
    
    portfolio_data = []
    total_value = 0
    total_cost = 0
    notifications = []
    
    # helper to resolve symbol -> id (Duplicated for now or move to global if possible, but local is fine)
    def resolve_coingecko_id(search_term):
        search_term = search_term.lower().strip()
        for c in SUPPORTED_COINS:
            if c['id'] == search_term: return c['id']
        for c in SUPPORTED_COINS:
            if c['symbol'] == search_term: return c['id']
        return search_term

    if portfolio_items:
        # Resolve IDs
        resolved_ids_map = {}
        api_ids = []
        for item in portfolio_items:
            db_symbol = item['symbol']
            api_id = resolve_coingecko_id(db_symbol)
            resolved_ids_map[db_symbol] = api_id
            api_ids.append(api_id)
        
        api_ids = list(set(api_ids))

        try:
            # Fetch current prices including 24h change
            data = cg.get_price(ids=api_ids, vs_currencies=user_currency, include_24hr_change='true')
            
            for item in portfolio_items:
                db_symbol = item['symbol']
                api_id = resolved_ids_map.get(db_symbol, db_symbol)
                
                qty = item['quantity']
                avg_buy = item['avg_buy_price']
                
                price_info = data.get(api_id, {})
                current_price = price_info.get(user_currency, 0)
                change_24h = price_info.get(f"{user_currency}_24h_change", 0)
                
                value = qty * current_price
                cost = qty * avg_buy
                pl = value - cost
                roi = (pl / cost * 100) if cost > 0 else 0
                
                total_value += value
                total_cost += cost
                
                # Notification Message (e.g. BTC: $90k (+2%))
                if current_price:
                    sign = "+" if change_24h >= 0 else ""
                    msg = f"{db_symbol.upper()}: {currency_symbol}{current_price:,.2f} ({sign}{change_24h:.2f}%)"
                    notifications.append(msg)
                
                portfolio_data.append({
                    'symbol': db_symbol,
                    'name': db_symbol.upper(), # Simplified for API, ideally full name maps exist or stored
                    'price': current_price,
                    'value': value,
                    'pl': pl,
                    'roi': roi,
                    'change_24h': change_24h
                })
                
        except Exception as e:
            print(f"Dashboard API Error: {e}")
            
    total_pl = total_value - total_cost
    total_roi = (total_pl / total_cost * 100) if total_cost > 0 else 0
    
    return json.dumps({
        'total_value': total_value,
        'total_pl': total_pl,
        'total_roi': total_roi,
        'portfolio': portfolio_data,
        'notifications': notifications,
        'currency_symbol': currency_symbol
    })

@app.route('/api/market_explorer')
@login_required
def market_explorer():
    global market_explorer_cache
    user_currency = getattr(current_user, 'currency', 'USD').lower()
    
    # Cache for 5 minutes (300 seconds)
    cache_key = user_currency
    if time.time() - market_explorer_cache['last_updated'] < 300 and cache_key in market_explorer_cache['data']:
        return json.dumps(market_explorer_cache['data'][cache_key])
    
    try:
        # Fetch Top 50 coins with sparklines
        data = cg.get_coins_markets(vs_currency=user_currency, order='market_cap_desc', per_page=50, page=1, sparkline=True)
        market_explorer_cache['data'][cache_key] = data
        market_explorer_cache['last_updated'] = time.time()
        return json.dumps(data)
    except Exception as e:
        print(f"Market Explorer API Error: {e}")
        return json.dumps([])

if __name__ == '__main__':
    print("Starting Flask Server...")
    print("Open your browser and go to: http://127.0.0.1:5000")
    app.run(debug=True)
