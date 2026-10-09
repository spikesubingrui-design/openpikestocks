"""Read-only overview of the existing strategy and real holding exit rules."""
from datetime import timedelta
from .calendar import parse_time, session, trading_age
from .strategy import quote_issues


def quote_deadline(stamp, t):
    deadline = parse_time(stamp) + timedelta(seconds=15)
    end = t.replace(hour=11 if t.hour < 12 else 15, minute=30 if t.hour < 12 else 0, second=0, microsecond=0)
    return min(deadline, end) if session(t) == 'OPEN' else deadline


def exit_reason(lot, price, t):
    if lot.get('stop') is not None and price <= lot['stop']:
        return '跌破原入场失效位置', 0
    age = trading_age(lot['bought'][:10], str(t.date()))
    if lot.get('target') is not None and price >= lot['target']:
        return '到达第一压力', 2
    if age is not None and age + 1 >= 15:
        return '达到15个交易日持有期限（买入日计第1日）', 1
    return None, 3


def overview(rankings, holdings, t):
    """Never infer a buy from score, nor reuse old EXIT events as current advice."""
    rows = rankings.get('rows', [])
    qualified = []
    for row in rows:
        if not row.get('qualified') or session(t) != 'OPEN':
            continue
        age = (t - parse_time(row['evaluated_at'])).total_seconds()
        if not 0 <= age <= 15 or quote_issues(row.get('quote'), t):
            continue
        qualified.append(row)
    key = lambda r: (r['score']['total'], r.get('net_rr') or 0, (r.get('quote') or {}).get('amount') or 0)
    qualified.sort(key=key, reverse=True)
    current = qualified[0] if qualified else None
    watch = max(rows, key=key) if rows else None
    valid_until = quote_deadline(min(parse_time(current['evaluated_at']), parse_time(current['quote']['stamp'])).isoformat(), t) if current else None
    buy = {'state': 'BUY' if current else 'WAIT', 'current': current, 'watch': watch,
           'valid_until': valid_until.isoformat() if valid_until else None,
           'qualified_count': len(qualified),
           'reason': '全部买入条件已通过，当前合格第一名' if current else
                     ('当前没有满足全部条件的买点' if session(t) == 'OPEN' else '当前非连续竞价时段，等待开市后重新核验')}
    real = [h for h in holdings if h['account'] == 'real']
    alerts = []
    for h in real:
        q = h.get('quote')
        if not q:
            continue
        age = (t - parse_time(q['stamp'])).total_seconds()
        reliable = -2 <= age <= 15 and q.get('status') == 'NORMAL' and not q.get('conflict')
        for lot in h['lots']:
            reason, priority = exit_reason(lot, q['price'], t)
            if not reason:
                continue
            sellable = lot['remaining'] if lot['bought'][:10] < str(t.date()) else 0
            blockers = []
            if session(t) != 'OPEN': blockers.append('非连续竞价时段，仅显示最后快照')
            if not reliable: blockers.append('报价已过期、来源冲突或交易状态待核实')
            if not sellable: blockers.append('T+1限制：该批次今日不可卖')
            if q.get('bid', 0) <= 0 or q.get('bid_size', 0) <= 0: blockers.append('买一无可成交数量，等待有效盘口')
            alerts.append({'code': h['code'], 'name': q.get('name', h['code']), 'lot_id': lot['id'],
                           'reason': reason, 'priority': priority, 'actionable': not blockers,
                           'blockers': blockers, 'quantity': lot['remaining'], 'sellable': sellable,
                           'stop': lot.get('stop'), 'target': lot.get('target'), 'reference': q.get('bid'),
                           'quote_at': q['stamp'], 'source': q.get('source'),
                           'valid_until': quote_deadline(q['stamp'], t).isoformat()})
    alerts.sort(key=lambda a: (not a['actionable'], a['priority'], a['code'], a['lot_id']))
    return {'at': t.isoformat(), 'buy': buy,
            'sell': {'state': 'EXIT' if any(a['actionable'] for a in alerts) else 'BLOCKED' if alerts else 'MONITOR' if real else 'NO_HOLDINGS',
                     'holding_count': len(real), 'alerts': alerts}}
