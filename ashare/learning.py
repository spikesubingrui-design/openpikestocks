import random
import statistics
from datetime import timedelta
from .calendar import now,parse_time,trading_age
from .config import WEIGHTS,VERSION

def max_drawdown(values):
    if not values: return None
    peak=values[0];dd=0.
    for v in values:
        peak=max(peak,v)
        if peak>0: dd=max(dd,1-v/peak)
    return dd

def lower_mean_ci(values):
    if len(values)<100: return None
    rng=random.Random(1909)
    estimates=sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(2000))
    return estimates[50]

class Learning:
    def __init__(self,store): self.store=store

    def weekly(self,t):
        week=f'{t.isocalendar().year}-{t.isocalendar().week:02d}'
        if self.store.get('learning_week',week): return
        if sum(c['status']=='FORWARD_ONLY' for c in self.store.all('challenger'))>=6:
            self.store.put('learning_week',week,{'week':week,'count':0,'reason':'已有6个候选正在前向验证，避免无限增加并行试验'})
            return
        variants=[('relative','volume'),('industry','structure'),('liquidity','risk')]
        for i,(a,b) in enumerate(variants):
            weights=dict(WEIGHTS);weights[a]+=5;weights[b]-=5
            version=f'challenger-{week}-{i+1}'
            self.store.put('challenger',version,{'version':version,'created':t.isoformat(),'weights':weights,'status':'FORWARD_ONLY',
                'hypothesis':f'{a}权重+5，{b}权重-5；所有买点、2倍盈亏比和5%失效距离不变'})
            self.store.put('cash','paper:'+version,{'balance':100000.,'initial':100000.})
        self.store.put('learning_week',week,{'week':week,'count':3,'created':t.isoformat()})

    def validation(self,candidate):
        created=parse_time(candidate['created'])
        days=sorted(r['day'] for r in self.store.all('observed_day') if r['day']>str(created.date()))
        # Fifteen observed trading days are excluded between frozen proposal
        # and holdout. Only genuinely captured days count toward 60 days.
        holdout=days[15:]
        since=holdout[0] if holdout else '9999-12-31'
        account='paper:'+candidate['version']
        with self.store.lock:
            rows=[dict(r) for r in self.store.db.execute('SELECT * FROM closed WHERE account=? AND substr(bought,1,10)>=?',(account,since))]
            # Partial exits count as ONE completed entry, only after its lot
            # has fully exited. Do not inflate the 100-trade gate.
            grouped={}
            for r in rows: grouped.setdefault(r['signal'],[]).append(r)
            complete=[]
            for sig,rs in grouped.items():
                open_lot=self.store.db.execute('SELECT 1 FROM lots WHERE account=? AND signal=? AND remaining>0',(account,sig)).fetchone()
                if not open_lot:
                    n=sum(r['quantity'] for r in rs)
                    complete.append({'net':sum(r['net_return']*r['quantity'] for r in rs)/n,
                                     'gross':sum(r['gross_return']*r['quantity'] for r in rs)/n,
                                     'pnl':sum(r['pnl'] for r in rs),'code':rs[0]['code'],'sold':max(r['sold'] for r in rs)[:10]})
        metrics={'observed_holdout_days':len(holdout),'completed_trades':len(complete),'expectancy_ci_lower':None,
                 'double_cost_expectancy':None,'candidate_drawdown':None,'baseline_drawdown':None,'concentration':None}
        reasons=[]
        if len(holdout)<60: reasons.append('时间隔离后实测交易日不足60日')
        if len(complete)<100: reasons.append('前向完整交易不足100笔')
        if len(complete)>=100:
            metrics['expectancy_ci_lower']=lower_mean_ci([r['net'] for r in complete])
            # Double BOTH sides' original fee/slippage difference from gross.
            metrics['double_cost_expectancy']=statistics.mean(r['gross']+2*(r['net']-r['gross']) for r in complete)
            if metrics['expectancy_ci_lower']<=0: reasons.append('扣费期望收益置信下限未大于0')
            if metrics['double_cost_expectancy']<=0: reasons.append('双倍成本压力测试未通过')
            gains=sum(max(0,r['pnl']) for r in complete)
            concentration=[]
            for field in ('code','sold'):
                buckets={}
                for r in complete: buckets[r[field]]=buckets.get(r[field],0)+max(0,r['pnl'])
                concentration.extend(buckets.values())
            metrics['concentration']=max(concentration)/gains if gains>0 else 1.
            if metrics['concentration']>.20: reasons.append('收益过度依赖单一股票或交易日')
        curve=self.store.all('equity:'+account)
        baseline=self.store.all('equity:paper')
        a={r['day']:r['equity'] for r in curve if r['day']>=since and r.get('final')}
        b={r['day']:r['equity'] for r in baseline if r['day']>=since and r.get('final')}
        common=sorted(set(a)&set(b))
        if len(common)<60: reasons.append('可比较的收盘权益曲线不足60日')
        else:
            metrics['candidate_drawdown']=max_drawdown([a[d] for d in common])
            metrics['baseline_drawdown']=max_drawdown([b[d] for d in common])
            if metrics['candidate_drawdown']>metrics['baseline_drawdown']: reasons.append('回撤劣于固定首版')
            if a[common[-1]]/a[common[0]]<=b[common[-1]]/b[common[0]]: reasons.append('收益未超过固定首版')
        return {'version':candidate['version'],'metrics':metrics,'eligible':not reasons,'reasons':reasons}

    def status(self):
        active=self.store.get('state','strategy')
        candidates=self.store.all('challenger')
        return {'active':active,'candidates':[{**c,'validation':self.validation(c)} for c in candidates],
                'win_rate':None,'notice':'前向验证期间不展示未经验证的胜率'}

    def promote(self):
        passed=[(c,self.validation(c)) for c in self.store.all('challenger') if c['status']=='FORWARD_ONLY']
        passed=[p for p in passed if p[1]['eligible']]
        if not passed: return
        c,v=max(passed,key=lambda p:p[1]['metrics']['expectancy_ci_lower'])
        old=self.store.get('state','strategy')
        if old['version']==c['version']: return
        history=old.get('history',[])+[{'previous':old,'changed_at':now().isoformat(),'validation':v}]
        self.store.put('state','strategy',{'version':c['version'],'weights':c['weights'],'history':history})
        c['status']='PROMOTED';self.store.put('challenger',c['version'],c)
        self.store.event('promotion:'+c['version'],'STRATEGY_CHANGE','',{'previous':old['version'],'version':c['version'],'validation':v})

    def rollback(self,request_id):
        from .store import dumps
        def action(db):
            state=self.store.get('state','strategy')
            if not state.get('history'): raise ValueError('没有可回滚版本')
            previous=state['history'][-1]['previous']
            stamp=now().isoformat()
            db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',('state','strategy',dumps(previous),stamp))
            event={'strategy_version':previous['version'],'version':previous['version'],'reason':'手工回滚'}
            db.execute('INSERT OR IGNORE INTO events(event_key,kind,code,created,body) VALUES(?,?,?,?,?)',
                       ('rollback:'+request_id,'STRATEGY_CHANGE','',stamp,dumps(event)))
            return previous
        return self.store.once(request_id,{'operation':'rollback'},action)
