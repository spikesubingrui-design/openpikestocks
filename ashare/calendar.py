from datetime import datetime, date, time, timedelta
from .config import TZ

SOURCE = 'https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml'
RANGES = [(1,1,1,3),(2,15,2,23),(4,4,4,6),(5,1,5,5),
          (6,19,6,21),(9,25,9,27),(10,1,10,7)]
HOLIDAYS = set()
for m,d,m2,d2 in RANGES:
    day = date(2026,m,d)
    while day <= date(2026,m2,d2):
        HOLIDAYS.add(day); day += timedelta(days=1)

def now():
    return datetime.now(TZ)

def parse_time(value):
    t = datetime.fromisoformat(value)
    return t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)

def trading_day(day):
    if isinstance(day, str): day = date.fromisoformat(day[:10])
    if day.year != 2026: return None
    return day.weekday() < 5 and day not in HOLIDAYS

def session(t):
    t = t.astimezone(TZ)
    valid = trading_day(t.date())
    if valid is None: return 'CALENDAR_UNKNOWN'
    if not valid: return 'CLOSED'
    h = t.time().replace(tzinfo=None)
    if time(9,30) <= h < time(11,30) or time(13) <= h < time(15): return 'OPEN'
    if time(11,30) <= h < time(13): return 'LUNCH'
    if time(9,15) <= h < time(9,30): return 'AUCTION'
    return 'CLOSED'

def previous_day(day):
    for i in range(1,30):
        d = day - timedelta(days=i)
        if trading_day(d): return d
    return None

def trading_age(start, end):
    a,b = date.fromisoformat(str(start)[:10]),date.fromisoformat(str(end)[:10])
    if a > b: return None
    count = 0
    while a < b:
        a += timedelta(days=1)
        v = trading_day(a)
        if v is None: return None
        count += bool(v)
    return count

def bar_complete(stamp, t):
    b = parse_time(stamp)
    # Providers label the END of the five-minute interval. Give it a
    # publication grace period; a future-labelled bar is never final.
    if trading_day(b.date()) is not True or b.second or b.minute % 5: return False
    h = b.time().replace(tzinfo=None)
    allowed = time(9,35) <= h <= time(11,30) or time(13,5) <= h <= time(15)
    return allowed and b + timedelta(seconds=3) <= t
