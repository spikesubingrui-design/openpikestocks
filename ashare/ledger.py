import json
import math
from datetime import date
from .calendar import now, parse_time, trading_day, trading_age
from .config import COST, VERSION, eligible
from .store import dumps
from .strategy import quote_issues
from .actions import exit_reason

class Ledger:
    def __init__(self,store): self.store=store

    def holdings(self,account=None,t=None):
        t=t or now()
        with self.store.lock:
            lots=[dict(r) for r in self.store.db.execute('SELECT * FROM lots WHERE remaining>0 ORDER BY bought,id')]
        if account: lots=[r for r in lots if r['account']==account]
        groups={}
        for lot in lots:
            key=(lot['account'],lot['code'])
            if key not in groups: groups[key]={'account':key[0],'code':key[1],'quantity':0,'sellable':0,'cost_value':0,'lots':[]}
            g=groups[key];g['quantity']+=lot['remaining'];g['cost_value']+=lot['price']*lot['remaining'];g['lots'].append(lot)
            if lot['bought'][:10]<str(t.date()): g['sellable']+=lot['remaining']
        for g in groups.values():
            g['average_price']=round(g['cost_value']/g['quantity'],4)
            q=self.store.get('quote',g['code']);g['quote']=q
            g['market_value']=q['price']*g['quantity'] if q else None
            g['unrealized']=g['market_value']-g['cost_value'] if q else None
        return list(groups.values())

    def register(self,body,opening=False,paper=False):
        account=body.get('account','real')
        if not paper and account!='real': raise ValueError('手工登记只用于真实持仓记录')
        side=body['side'];quantity=body['quantity'];price=body['price'];stamp=body['stamp']
        t=parse_time(stamp)
        if t>now() or trading_day(t.date()) is not True: raise ValueError('成交日期须为已核实交易日且不得晚于当前时间')
        if quantity<=0 or price<=0: raise ValueError('数量和价格须为正数')
        if side=='BUY' and not opening and quantity%100: raise ValueError('买入须为100股整数倍')
        instrument=self.store.get('instrument',body['code'])
        if not instrument or not eligible(body['code'],instrument['name']): raise ValueError('代码不在正常沪深主板范围')
        if body.get('stop') is not None and not 0<body['stop']<price: raise ValueError('入场失效价须低于成交价')
        if body.get('target') is not None and body['target']<=price: raise ValueError('第一压力须高于成交价')
        cost=0. if opening else float(body.get('cost',price*quantity*COST))
        if not math.isfinite(cost) or cost<0: raise ValueError('费用须为非负有限数')
        def action(db):
            cash=self.store.get('cash',account)
            if paper and cash is None: raise ValueError('纸面账户未初始化')
            if side=='BUY':
                if paper and price*quantity+cost>cash['balance']+.001: raise ValueError('纸面现金不足')
                db.execute('INSERT INTO lots(account,code,bought,quantity,remaining,price,stop,target,version,signal,buy_cost) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (account,body['code'],stamp,quantity,quantity,price,body.get('stop'),body.get('target'),body.get('version',VERSION),body.get('signal'),cost))
                if paper: cash['balance']-=price*quantity+cost
            elif side=='SELL':
                lots=db.execute('SELECT * FROM lots WHERE account=? AND code=? AND remaining>0 ORDER BY bought,id',(account,body['code'])).fetchall()
                sellable=sum(r['remaining'] for r in lots if r['bought'][:10]<stamp[:10])
                if quantity>sellable: raise ValueError('超出T+1可卖数量')
                total=sum(r['remaining'] for r in lots)
                if quantity%100 and quantity!=total: raise ValueError('零股须一次性卖出剩余全部数量')
                remaining=quantity
                for r in lots:
                    if r['bought'][:10]>=stamp[:10] or remaining==0: continue
                    n=min(remaining,r['remaining']);remaining-=n
                    db.execute('UPDATE lots SET remaining=remaining-? WHERE id=?',(n,r['id']))
                    # Entry costs are allocated from the actual recorded buy,
                    # opening balances have no invented historical fees.
                    buy_cost=r['buy_cost']/r['quantity']*n
                    gross=(price-r['price'])/r['price']
                    net=(price*n-cost*n/quantity)/(r['price']*n+buy_cost)-1
                    pnl=(price-r['price'])*n-buy_cost-cost*n/quantity
                    db.execute('INSERT INTO closed(account,code,bought,sold,quantity,net_return,gross_return,pnl,version,signal) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (account,body['code'],r['bought'],stamp,n,net,gross,pnl,r['version'],r['signal']))
                if paper: cash['balance']+=price*quantity-cost
            else: raise ValueError('成交方向须为BUY或SELL')
            db.execute('INSERT INTO trades(request_id,account,code,side,stamp,quantity,price,cost,body) VALUES(?,?,?,?,?,?,?,?,?)',
                       (body['request_id'],account,body['code'],side,stamp,quantity,price,cost,dumps(body)))
            if paper: db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('cash',account,dumps(cash),now().isoformat()))
            return {'registered':True,'request_id':body['request_id'],'account':account,'side':side,'quantity':quantity}
        return self.store.once(body['request_id'],{**body,'opening':opening},action)

    def paper_entry(self,row,t,account='paper'):
        if not row['qualified'] or quote_issues(row['quote'],t): return
        holdings=self.holdings(account,t)
        if len(holdings)>=5 or any(h['code']==row['code'] for h in holdings): return
        cash=self.store.get('cash',account)
        if not cash: return
        equity=cash['balance']+sum(h['market_value'] if h['market_value'] is not None else h['cost_value'] for h in holdings)
        budget=min(cash['balance'],equity*.20)
        ask=row['quote']['ask'];size=row['quote']['ask_size']
        qty=math.floor(min(budget/(ask*(1+COST)),size)/100)*100
        if qty<100: return
        self.register({'request_id':account+':entry:'+row['setup_id'],'account':account,'code':row['code'],
                       'side':'BUY','quantity':qty,'price':ask,'stamp':t.isoformat(),'cost':qty*ask*COST,
                       'stop':row['stop'],'target':row['target'],'version':row['strategy_version'],'signal':row['setup_id']},paper=True)

    def exits(self,t):
        for h in self.holdings(t=t):
            q=h['quote']
            if not q or q.get('status')!='NORMAL' or q.get('conflict') or not -2<=(t-parse_time(q['stamp'])).total_seconds()<=15: continue
            for lot in h['lots']:
                reason,_=exit_reason(lot,q['price'],t)
                if not reason: continue
                if h['account']=='real':
                    self.store.event(f'exit:{lot["id"]}:{reason}','EXIT',h['code'],{'reason':reason,'lot':lot,'sellable':h['sellable'],'quote':q,
                                      't_plus_one_blocked':lot['bought'][:10]>=str(t.date())})
                    continue
                if lot['bought'][:10]>=str(t.date()) or q['bid']<=0 or q['bid_size']<=0: continue
                # At a limit-down bid with no executable size no simulated fill.
                qty=min(lot['remaining'],int(q['bid_size']//100)*100)
                if lot['remaining']<100 and q['bid_size']>=lot['remaining']: qty=lot['remaining']
                if qty<=0: continue
                self.register({'request_id':f'paper-exit:{lot["id"]}:{t.isoformat()}', 'account':h['account'],'code':h['code'],
                               'side':'SELL','quantity':qty,'price':q['bid'],'stamp':t.isoformat(),'cost':qty*q['bid']*COST,
                               'reason':reason},paper=True)

    def summary(self):
        with self.store.lock:
            trades=[{**dict(r),'body':json.loads(r['body'])} for r in self.store.db.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 200')]
            closed=[dict(r) for r in self.store.db.execute('SELECT * FROM closed ORDER BY id DESC')]
        return {'holdings':self.holdings(),'cash':self.store.all('cash'),'trades':trades,'closed':closed,'win_rate':None,'notice':'胜率未通过前向验证，不展示'}
