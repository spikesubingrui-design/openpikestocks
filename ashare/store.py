import hashlib
import gzip
import json
import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from .calendar import now
from .config import VERSION

def dumps(value): return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',',':'))

class Store:
    def __init__(self, path):
        path=Path(path)
        self.path=path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS objects(kind TEXT, key TEXT, body TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS bars(code TEXT, frame TEXT, stamp TEXT, body TEXT NOT NULL, PRIMARY KEY(code,frame,stamp));
            CREATE TABLE IF NOT EXISTS bar_chunks(code TEXT, frame TEXT, payload BLOB NOT NULL, PRIMARY KEY(code,frame));
            CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY, code TEXT, stamp TEXT, observed TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS quote_time ON quotes(observed);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, kind TEXT, code TEXT, created TEXT, body TEXT, ack TEXT, claim TEXT, lease TEXT);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, body_hash TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, account TEXT, code TEXT, bought TEXT, quantity INTEGER, remaining INTEGER, price REAL, stop REAL, target REAL, version TEXT, signal TEXT, buy_cost REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, account TEXT, code TEXT, side TEXT, stamp TEXT, quantity INTEGER, price REAL, cost REAL, body TEXT);
            CREATE TABLE IF NOT EXISTS closed(id INTEGER PRIMARY KEY, account TEXT, code TEXT, bought TEXT, sold TEXT, quantity INTEGER, net_return REAL, gross_return REAL, pnl REAL, version TEXT, signal TEXT);
            ''')
            if 'buy_cost' not in {r[1] for r in self.db.execute('PRAGMA table_info(lots)')}:
                self.db.execute('ALTER TABLE lots ADD COLUMN buy_cost REAL DEFAULT 0')
            self.db.commit()
        if self.get('state','strategy') is None: self.put('state','strategy',{'version':VERSION,'weights':None,'history':[]})
        if self.get('cash','paper') is None: self.put('cash','paper',{'balance':100000.,'initial':100000.})

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback(); raise

    def put(self, kind, key, body):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',(kind,key,dumps(body),now().isoformat()))

    def get(self, kind, key, default=None):
        with self.lock:
            r = self.db.execute('SELECT body FROM objects WHERE kind=? AND key=?',(kind,key)).fetchone()
            return json.loads(r[0]) if r else default

    def all(self, kind):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM objects WHERE kind=? ORDER BY key',(kind,))]

    def save_bars(self, code, frame, bars):
        with self.transaction() as db:
            merged={b['stamp']:b for b in self.bars(code,frame)}
            merged.update({b['stamp']:b for b in bars})
            payload=gzip.compress(dumps([merged[k] for k in sorted(merged)]).encode(),mtime=0)
            db.execute('INSERT OR REPLACE INTO bar_chunks VALUES(?,?,?)',(code,frame,payload))
            db.execute('DELETE FROM bars WHERE code=? AND frame=?',(code,frame))

    def bars(self, code, frame):
        with self.lock:
            chunk=self.db.execute('SELECT payload FROM bar_chunks WHERE code=? AND frame=?',(code,frame)).fetchone()
            merged={b['stamp']:b for b in json.loads(gzip.decompress(chunk[0]))} if chunk else {}
            merged.update({b['stamp']:b for b in [json.loads(r[0]) for r in self.db.execute('SELECT body FROM bars WHERE code=? AND frame=? ORDER BY stamp',(code,frame))]})
            return [merged[k] for k in sorted(merged)]

    def quote(self, body,record_history=True):
        self.put('quote',body['code'],body)
        if not record_history: return
        with self.transaction() as db:
            db.execute('INSERT INTO quotes(code,stamp,observed,body) VALUES(?,?,?,?)',(body['code'],body['stamp'],body['observed'],dumps(body)))

    def event(self, key, kind, code, body):
        body = {'strategy_version':body.get('strategy_version',VERSION), **body}
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO events(event_key,kind,code,created,body) VALUES(?,?,?,?,?)',(key,kind,code,now().isoformat(),dumps(body)))
            r = db.execute('SELECT id FROM events WHERE event_key=?',(key,)).fetchone()
            return r[0]

    def events(self, pending=False):
        with self.lock:
            sql = 'SELECT * FROM events' + (' WHERE ack IS NULL' if pending else '') + ' ORDER BY id DESC LIMIT 200'
            return [{**dict(r),'body':json.loads(r['body'])} for r in self.db.execute(sql)]

    def once(self, request_id, body, action):
        fingerprint = hashlib.sha256(dumps(body).encode()).hexdigest()
        with self.transaction() as db:
            r = db.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            if r:
                if r['body_hash'] != fingerprint: raise ValueError('相同登记编号对应不同内容')
                return json.loads(r['result'])
            result = action(db)
            db.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,fingerprint,dumps(result)))
            return result
