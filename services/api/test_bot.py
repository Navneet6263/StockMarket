import sys, time, traceback
sys.stderr = sys.__stderr__
sys.stdout = sys.stderr

from dotenv import load_dotenv
load_dotenv('.env')

from pymongo import MongoClient
mc = MongoClient('mongodb://localhost:27017', serverSelectionTimeoutMS=1000)
mc.admin.command('ping')
db = mc['stock_predictor_ml']
print('[OK] DB connected')

# Test the db is not None pattern
print(f'[OK] db type: {type(db)}')
print(f'[OK] db is not None: {db is not None}')

from app.telegram_bot import get_bot
from app.signal_tracker import get_tracker
from app.stock_scanner import get_scanner

tracker = get_tracker(db=db if db is not None else None)
print(f'[OK] Tracker created, signals_coll: {tracker._signals_coll() is not None}')

bot = get_bot(db=db if db is not None else None)
print('[OK] Bot created')

# Test with 10 stocks
test_stocks = ['RELIANCE', 'TCS', 'SBIN', 'INFY', 'ITC', 'HDFCBANK', 'ICICIBANK', 'BHARTIARTL', 'KOTAKBANK', 'LT']
print(f'[..] Scanning {len(test_stocks)} stocks (batch mode)...')
start = time.time()

try:
    result = bot.scan_once(test_stocks)
    elapsed = round(time.time() - start, 1)
    print(f'[OK] Done in {elapsed}s')
    print(f'[OK] Signals sent: {len(result)}')
    for r in result:
        if 'error' in r:
            print(f'  ERROR: {r}')
        else:
            print(f'  {r.get("symbol")} {r.get("action")} Q={r.get("quality_grade")}')
    print('[OK] Check your Telegram!')
except Exception as e:
    traceback.print_exc()
    print(f'[FAIL] {e}')
