import asyncio
import logging
import statistics
import time
from datetime import timedelta
from .calendar import now,session,parse_time,previous_day,bar_complete
from .config import SEEDS,MAX_WATCH,VERSION,eligible
from .providers import Providers
from .strategy import evaluate,completed_daily,ret
from .ledger import Ledger
from .learning import Learning
from .archive import Archive
import hashlib
from .store import dumps

log=logging.getLogger('ashare')

class Engine:
    def __init__(self,store):
        self.store=store;self.providers=Providers();self.ledger=Ledger(store);self.learning=Learning(store)
        self.archive=Archive(store)
        self.tasks=[];self.ranking=[];self.started=now().isoformat();self.focus=store.get('state','market_scan',{}).get('focus') or list(SEEDS)
        for c,n in SEEDS.items():
            if not store.get('instrument',c): store.put('instrument',c,{'code':c,'name':n,'seed':True,'industry':None,'classification_source':None})

    def fault(self,key,detail):
        old=self.store.get('fault',key)
        if not old or not old['active']:
            self.store.event('fault:'+key+':'+now().isoformat(),'DATA_FAULT','',{'component':key,'detail':detail})
        self.store.put('fault',key,{'component':key,'active':True,'detail':detail,'at':now().isoformat()})

    def recover(self,key):
        old=self.store.get('fault',key)
        if old and old['active']: self.store.put('fault',key,{**old,'active':False,'recovered':now().isoformat()})

    def start(self):
        self.tasks=[asyncio.create_task(self.quote_loop()),asyncio.create_task(self.market_loop()),
                    asyncio.create_task(self.history_loop()),asyncio.create_task(self.minute_loop()),asyncio.create_task(self.research_loop()),asyncio.create_task(self.financial_loop())]

    def refresh_manual_focus(self):
        held=[h['code'] for h in self.ledger.holdings()]
        manual=[c for c in self.store.get('state','manual_watchlist',[]) if
                (ins:=self.store.get('instrument',c)) and eligible(c,ins['name'])]
        self.focus=list(dict.fromkeys(held+list(SEEDS)+manual+self.focus))[:MAX_WATCH]

    async def stop(self):
        for t in self.tasks: t.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.providers.client.aclose()

    async def quote_loop(self):
        first=True;fetched_focus=()
        while True:
            start=time.monotonic();t=now();state=session(t)
            try:
                closing=state=='CLOSED' and t.hour==15 and t.minute==0
                if state=='OPEN' or first or closing or tuple(self.focus)!=fetched_focus:
                    (a,ae),(b,be)=await self.providers.quotes(self.focus)
                    for code in self.focus:
                        qa,qb=a.get(code),b.get(code)
                        def fresh(q): return q and -2<=(now()-parse_time(q['stamp'])).total_seconds()<=15
                        q=qa if fresh(qa) else qb if fresh(qb) else qa or qb
                        if not q: continue
                        q['cross_source']=qb if q is qa else qa
                        q['conflict']=False
                        if fresh(qa) and fresh(qb):
                            q['conflict']=abs(qa['price']-qb['price'])>max(.011,qa['price']*.001) or abs(qa['previous']-qb['previous'])>.011
                        # Only the explicitly supported Sina state code can
                        # confirm normal trading. Fresh volume/open additionally
                        # prevents yesterday's or zero-session snapshots passing.
                        q['status']='NORMAL' if qb and qb.get('status')=='NORMAL' and fresh(qb) and q['open']>0 and q['volume']>0 else qb.get('status','UNKNOWN') if qb else 'UNKNOWN'
                        q['network_errors']={'腾讯':ae,'新浪':be}
                        self.store.quote(q,record_history=self.archive.storage()['ready'])
                        if state=='OPEN':
                            if not fresh(q): self.fault('quote_freshness:'+code,'报价超过15秒，已阻断新买')
                            else: self.recover('quote_freshness:'+code)
                    if not a and not b: self.fault('quotes',f'腾讯：{ae}；新浪：{be}')
                    else: self.recover('quotes')
                    fetched_focus=tuple(self.focus)
                    if state=='OPEN':
                        if any(fresh(q) for q in a.values()) or any(fresh(q) for q in b.values()):
                            self.store.put('observed_day',str(t.date()),{'day':str(t.date()),'last_observed':now().isoformat()})
                        self.evaluate_all(now())
                        self.ledger.exits(now())
                        self.capture_equity(now())
                    elif closing:
                        self.evaluate_all(now());self.capture_equity(now())
                self.store.put('state','service',{'started':self.started,'last_cycle':now().isoformat(),'session':state,'focus':self.focus,'poll_seconds':5})
                first=False
                self.recover('quote_cycle')
            except asyncio.CancelledError: raise
            except Exception as e:
                log.exception('quote cycle');self.fault('quote_cycle',str(e)[:250])
            await asyncio.sleep(max(.1,(5 if state=='OPEN' else 30)-(time.monotonic()-start)))

    async def market_loop(self):
        while True:
            start=time.monotonic()
            try:
                rows,url=await self.providers.node('hs_a');valid=[r for r in rows if eligible(r['code'],r['name'])]
                for r in valid:
                    old=self.store.get('instrument',r['code'],{})
                    self.store.put('instrument',r['code'],{**old,'code':r['code'],'name':r['name'],'seed':r['code'] in SEEDS,
                                   'market_quote':r,'universe_observed':now().isoformat(),'universe_source':url})
                # Refresh excluded status too, so a previously normal name
                # becoming ST does not survive from the old universe.
                for r in rows:
                    if self.store.get('instrument',r['code']) and not eligible(r['code'],r['name']):
                        old=self.store.get('instrument',r['code']);self.store.put('instrument',r['code'],{**old,'name':r['name'],'excluded':True})
                held=[h['code'] for h in self.ledger.holdings()]
                dynamic=sorted(valid,key=lambda r:(r.get('changepercent',0)>0,r.get('amount',0)*max(0,r.get('changepercent',0))),reverse=True)
                manual=[c for c in self.store.get('state','manual_watchlist',[]) if
                        (ins:=self.store.get('instrument',c)) and eligible(c,ins['name'])]
                self.focus=list(dict.fromkeys(held+list(SEEDS)+manual+[r['code'] for r in dynamic]))[:MAX_WATCH]
                self.store.put('state','market_scan',{'at':now().isoformat(),'total':len(rows),'eligible':len(valid),'focus':self.focus,'source':url,'duration_seconds':time.monotonic()-start})
                self.recover('market_scan')
                if not self.store.get('state','industry_members') or self.store.get('state','industry_members')['day']!=str(now().date()) or self.store.get('state','industry_members').get('schema')!='东方财富财报行业分类':
                    await self.refresh_industries()
            except asyncio.CancelledError: raise
            except Exception as e: log.exception('market scan');self.fault('market_scan',str(e)[:250])
            await asyncio.sleep(max(1,300-(time.monotonic()-start)))

    async def refresh_industries(self):
        try:
            catalog=await self.providers.financial_industries()
            if not catalog: raise ValueError('财报行业分类为空')
            for group in catalog:
                group['members']=[c for c in group['members'] if (ins:=self.store.get('instrument',c)) and eligible(c,ins['name']) and ins.get('universe_observed','')[:10]==str(now().date())]
            mapped={c for r in catalog for c in r['members']}
            for ins in self.store.all('instrument'):
                if ins['code'] not in mapped and ins.get('industry','') and not ins['industry'].startswith('em-report:'):
                    self.store.put('instrument',ins['code'],{**ins,'industry':None,'classification_source':None})
            for industry in catalog:
                self.store.put('industry',industry['node'],industry)
                for c in industry['members']:
                    ins=self.store.get('instrument',c)
                    if ins: self.store.put('instrument',c,{**ins,'industry':industry['node'],
                        'classification_source':industry['member_source_url'],'classification_verified_at':industry['verified_at']})
            self.store.put('state','industry_members',{'day':str(now().date()),'count':len(catalog),
                'schema':'东方财富财报行业分类','members':sum(len(r['members']) for r in catalog),'at':now().isoformat()})
            self.recover('industry_catalog')
        except Exception as e:
            # Preserve the last known taxonomy. Never clear classifications or
            # claim full coverage after a truncated response.
            self.fault('industry_catalog',str(e)[:180])

    def history_candidates(self,t):
        industries={self.store.get('instrument',c,{}).get('industry') for c in self.focus}
        members=[c for k in sorted(k for k in industries if k) for c in self.store.get('industry',k,{}).get('members',[])]
        remaining=[r['code'] for r in self.store.all('instrument') if eligible(r['code'],r['name'])]
        codes=list(dict.fromkeys(self.focus+['000300']+members+remaining));pending=[];due=[]
        for c in codes:
            cache=self.store.get('history_fetch',c,{})
            needs=cache.get('day')!=str(t.date()) or cache.get('provider_revision')!='daily-v2' or cache.get('amount_missing',True)
            if needs:
                pending.append(c);retry=cache.get('retry_after') or self.store.get('history_error',c,{}).get('retry_after')
                if not retry or parse_time(retry)<=t: due.append(c)
        return codes,pending,due

    async def history_loop(self):
        while True:
            pending=[]
            try:
                if not self.archive.storage()['ready']:
                    self.fault('storage','磁盘剩余低于128MiB，已暂停全市场补采并阻断新买')
                    await asyncio.sleep(30);continue
                self.recover('storage')
                codes,pending,due=self.history_candidates(now())
                async def fetch(c):
                    try:
                        bars=await self.providers.daily(c)
                        if not bars: raise ValueError('日线为空')
                        self.store.save_bars(c,'day',bars)
                        closed=completed_daily(bars,now());missing=len(closed)<20 or any(b.get('amount') is None for b in closed[-20:])
                        self.store.put('history_fetch',c,{'code':c,'day':str(now().date()),'at':now().isoformat(),'count':len(bars),
                            'provider_revision':'daily-v2','amount_missing':missing,
                            'retry_after':(now()+timedelta(minutes=15)).isoformat() if missing else None})
                        self.store.put('history_error',c,{'code':c,'active':False,'recovered':now().isoformat()})
                        self.recover('daily:'+c)
                    except Exception as e:
                        old=self.store.get('history_error',c,{})
                        attempts=old.get('attempts',0)+1;wait=min(1800,30*2**min(attempts-1,6))
                        self.store.put('history_error',c,{'code':c,'active':True,'at':now().isoformat(),'error':str(e)[:180],
                            'attempts':attempts,'retry_after':(now()+timedelta(seconds=wait)).isoformat()})
                        if c in self.focus: self.fault('daily:'+c,str(e)[:150])
                await asyncio.gather(*(fetch(c) for c in due[:48]))
                _,remaining,due_after=self.history_candidates(now())
                self.store.put('state','history',{'at':now().isoformat(),'pending':len(remaining),'ready_to_fetch':len(due_after),
                    'backoff':len(remaining)-len(due_after),'total':len(codes),'provider_revision':'daily-v2'})
                self.evaluate_all(now())
                self.recover('history')
            except asyncio.CancelledError: raise
            except Exception as e: log.exception('history');self.fault('history',str(e)[:150])
            await asyncio.sleep(5 if pending else 60)

    async def financial_loop(self):
        while True:
            try:
                if not self.archive.storage()['ready']:
                    await asyncio.sleep(30);continue
                pending=[]
                for c in self.focus:
                    old=self.store.get('financial',c);err=self.store.get('financial_error',c,{})
                    if err.get('retry_after') and parse_time(err['retry_after'])>now(): continue
                    if not old or old['observed_at'][:10]!=str(now().date()): pending.append(c)
                async def fetch(c):
                    try:
                        r=await self.providers.financial_report(c)
                        r['id']='financial:'+c+':'+hashlib.sha256(dumps(r).encode()).hexdigest()[:20]
                        self.store.put('financial_evidence',r['id'],r);self.store.put('financial',c,r)
                        self.recover('financial:'+c)
                    except Exception as e:
                        self.store.put('financial_error',c,{'code':c,'error':str(e)[:180],
                            'retry_after':(now()+timedelta(minutes=5)).isoformat()})
                        self.fault('financial:'+c,str(e)[:150])
                await asyncio.gather(*(fetch(c) for c in pending[:8]))
                self.store.put('state','financial_collection',{'at':now().isoformat(),'focus':len(self.focus),
                    'available':sum(self.store.get('financial',c) is not None for c in self.focus),'queued':len(pending)})
                self.evaluate_all(now());self.archive.sync()
                self.recover('financial_collection')
            except asyncio.CancelledError: raise
            except Exception as e: log.exception('financial');self.fault('financial_collection',str(e)[:180])
            await asyncio.sleep(5 if pending else 60)

    async def minute_loop(self):
        while True:
            try:
                if not self.archive.storage()['ready']:
                    await asyncio.sleep(30);continue
                async def fetch(c):
                    try:
                        bars=await self.providers.minutes(c);self.store.save_bars(c,'5m',bars)
                        self.store.put('minute_fetch',c,{'code':c,'at':now().isoformat(),'count':len(bars)})
                        self.recover('minutes:'+c)
                    except Exception as e: self.fault('minutes:'+c,str(e)[:150])
                await asyncio.gather(*(fetch(c) for c in self.focus))
                self.recover('minutes')
            except asyncio.CancelledError: raise
            except Exception as e: log.exception('minutes');self.fault('minutes',str(e)[:150])
            # Fetch shortly after each new 5m boundary, not every quote tick.
            t=now();wait=300-(t.minute%5*60+t.second)+5
            await asyncio.sleep(max(5,wait) if session(t)=='OPEN' else 300)

    def industry_metrics(self,key,t):
        industry=self.store.get('industry',key) if key else None
        if not industry: return None
        members=industry['members'];valid=[];benchmark=completed_daily(self.store.bars('000300','day'),t)
        expected=previous_day(t.date())
        for c in members:
            bars=completed_daily(self.store.bars(c,'day'),t)
            if len(bars)>=21 and expected and bars[-1]['stamp']==str(expected): valid.append(bars)
        coverage=len(valid)/len(members) if members else 0
        result={'name':industry['name'],'node':key,'members':len(members),'effective':len(valid),'coverage':coverage,
                'verified_at':industry['verified_at'],'source_url':industry['member_source_url'],'valid':False,
                'schema':industry.get('schema','新浪行业'),'reporting_period':industry.get('reporting_period')}
        fresh=industry['verified_at'][:10]==str(t.date())
        if len(valid)>=5 and coverage>=.8 and len(benchmark)>=6 and benchmark[-1]['stamp']==str(expected) and fresh:
            r5=statistics.mean(ret(b,5) for b in valid)
            result.update(valid=True,return5=r5,return10=statistics.mean(ret(b,10) for b in valid),relative5=r5-ret(benchmark,5),
                          breadth=statistics.mean(b[-1]['close']>statistics.mean(r['close'] for r in b[-20:]) for b in valid))
        return result

    def research(self,code,t):
        rows=[r for r in self.store.all('research') if r['code']==code and parse_time(r['observed_at'])<=t and parse_time(r['published_at'])<=t]
        record=max(rows,key=lambda r:r['observed_at']) if rows else None
        financial=self.store.get('financial',code)
        if financial and parse_time(financial['observed_at'])<=t and parse_time(financial['published_at'])<=t:
            if not record:
                record={'id':financial['id'],'code':code,'published_at':financial['published_at'],
                    'observed_at':financial['observed_at'],'valid_until':(parse_time(financial['observed_at'])+timedelta(days=1)).isoformat(),
                    'sources':financial['sources'],'facts':{**financial['facts'],'risk_review_complete':False,'catalyst_review_complete':False},
                    'summary':'自动采集已公开利润表和同报告期现金流，公告风险范围尚待核查。','announcement_ids':[]}
            else:
                record={**record,'facts':dict(record['facts']),'sources':list(dict.fromkeys(record['sources']+financial['sources']))}
                period=record['facts'].get('reporting_period') or ''
                comparable=period.replace('H1','-06-30').replace('FY','-12-31')
                if not comparable or financial['facts']['reporting_period']>=comparable:
                    for k,v in financial['facts'].items():
                        if v is not None and (record['facts'].get(k) is None or financial['facts']['reporting_period']>comparable): record['facts'][k]=v
            record['financial_evidence_id']=financial['id'];record['financial_conflicts']=financial['conflicts']
            record['financial_published_at']=financial['published_at'];record['financial_observed_at']=financial['observed_at']
        if not record: return None
        calendar=self.store.get('earnings_calendar',code)
        if calendar and parse_time(calendar['observed_at'])<=t and (t-parse_time(calendar['observed_at'])).total_seconds()<86400:
            record={**record,'facts':{**record['facts']},'sources':list(dict.fromkeys(record['sources']+[calendar['source_url']])),
                'earnings_calendar':calendar}
            if calendar['earnings_next3'] or record['facts'].get('earnings_next3') is None:
                record['facts']['earnings_next3']=calendar['earnings_next3']
        current=self.store.get('announcements',code,{'rows':[]})['rows'];known=set(record.get('announcement_ids',[]))
        unseen=[r for r in current if r['id'] not in known and r['published_at'][:10]>=record['observed_at'][:10]]
        if unseen: record={**record,'facts':{**record['facts'],'risk_review_complete':False,'catalyst_review_complete':False},'new_announcements_unreviewed':True}
        return record

    def evaluate_all(self,t):
        state=self.store.get('state','strategy');industry_cache={};rankings=[];contexts=[]
        for c in self.focus:
            ins=self.store.get('instrument',c,{})
            if not ins: continue
            key=ins.get('industry')
            if key not in industry_cache: industry_cache[key]=self.industry_metrics(key,t)
            context=(c,ins['name'],self.store.bars(c,'day'),self.store.bars(c,'5m'),self.store.get('quote',c),industry_cache[key],self.research(c,t),t)
            contexts.append(context)
            row=evaluate(*context,weights=state['weights'],version=state['version'])
            if not self.archive.storage()['ready']:
                row['qualified']=False;row['reasons'].append('磁盘空间不足，档案持久化能力尚未恢复')
            if ins.get('universe_observed','')[:10]!=str(t.date()):
                row['qualified']=False;row['reasons'].append('当日上市及风险状态尚未刷新')
            rankings.append(row)
        rankings.sort(key=lambda r:(r['score']['total'],r['net_rr'] or -1,r['metrics'].get('amount20') or 0,r['code']),reverse=True)
        self.ranking=rankings
        self.store.put('state','rankings',{'at':t.isoformat(),'strategy_version':state['version'],'rows':rankings})
        qualified=[r for r in rankings if r['qualified']]
        top=qualified[0] if qualified else None
        previous=self.store.get('state','active_signal')
        if previous and (not top or previous['setup_id']!=top['setup_id']):
            self.store.event('invalid:'+previous['setup_id'],'INVALIDATED',previous['code'],{'reason':'原买点不再满足全部条件','previous':previous,'at':t.isoformat()})
        if top:
            self.store.event('buy:'+top['setup_id'],'BUY',top['code'],top)
            self.ledger.paper_entry(top,t,'paper' if state['version']==VERSION else 'paper:'+state['version'])
        self.store.put('state','active_signal',top)
        if self.archive.storage()['ready']:
            self.archive.capture(state,rankings,contexts,t,
                signal_key=top['setup_id'] if top and (not previous or previous['setup_id']!=top['setup_id']) else None)
        # Always preserve an unchanged fixed baseline for candidate comparison,
        # including after an eventual promotion.
        if state['version']!=VERSION:
            base=[evaluate(*ctx) for ctx in contexts]
            good=[r for r in base if r['qualified']]
            if good: self.ledger.paper_entry(max(good,key=lambda r:(r['score']['total'],r['net_rr'],r['metrics'].get('amount20',0))),t,'paper')
        if session(t)=='OPEN':
            for candidate in self.store.all('challenger'):
                if candidate['status']!='FORWARD_ONLY': continue
                # Avoid unbounded active simulations; weekly records remain,
                # mature failed variants are retained for audit but retired.
                if (t-parse_time(candidate['created'])).days>240:
                    candidate['status']='RETIRED';self.store.put('challenger',candidate['version'],candidate);continue
                good=[evaluate(*ctx,weights=candidate['weights'],version=candidate['version']) for ctx in contexts]
                good=[r for r in good if r['qualified'] and self.store.get('instrument',r['code'],{}).get('universe_observed','')[:10]==str(t.date())]
                if good: self.ledger.paper_entry(max(good,key=lambda r:(r['score']['total'],r['net_rr'],r['metrics'].get('amount20',0))),t,'paper:'+candidate['version'])

    def capture_equity(self,t):
        with self.store.lock:
            accounts=[r['key'] for r in self.store.db.execute('SELECT key FROM objects WHERE kind="cash"')]
        for account in accounts:
            cash=self.store.get('cash',account)
            holdings=self.ledger.holdings(account,t)
            all_fresh=all(h['quote'] and abs((t-parse_time(h['quote']['stamp'])).total_seconds())<=15 for h in holdings)
            if not all_fresh: continue
            equity=cash['balance']+sum(h['market_value'] for h in holdings)
            self.store.put('equity:'+account,str(t.date()),{'day':str(t.date()),'equity':equity,'at':t.isoformat(),'final':t.hour==15 and t.minute==0})

    async def research_loop(self):
        while True:
            t=now()
            try:
                if not self.archive.storage()['ready']:
                    await asyncio.sleep(30);continue
                self.learning.weekly(t)
                for c in self.focus:
                    stamp=self.store.get('announcement_fetch',c,{})
                    if stamp and (t-parse_time(stamp['at'])).total_seconds()<1800: continue
                    try:
                        rows=await self.providers.announcements(c)
                        old=self.store.get('announcements',c,{'rows':[]});known={r['id'] for r in old['rows']}
                        self.store.put('announcements',c,{'code':c,'rows':rows,'at':now().isoformat()})
                        self.store.put('announcement_fetch',c,{'code':c,'at':now().isoformat()})
                        for r in rows:
                            if r['id'] not in known and r['published_at'][:10]==str(t.date()):
                                self.store.event('announcement:'+r['id'],'ANNOUNCEMENT',c,r)
                        self.recover('announcements:'+c)
                    except Exception as e: self.fault('announcements:'+c,str(e)[:150])
                priority=list(dict.fromkeys([r['code'] for r in self.ranking]+self.focus))
                for c in [c for c in priority if self.store.get('earnings_calendar',c,{}).get('observed_at','')[:10]!=str(t.date())][:8]:
                    error=self.store.get('earnings_calendar_error',c,{})
                    if error.get('retry_after') and parse_time(error['retry_after'])>t: continue
                    try:self.store.put('earnings_calendar',c,await self.providers.earnings_calendar(c))
                    except Exception as e:self.store.put('earnings_calendar_error',c,{'code':c,'detail':str(e)[:160],'retry_after':(t+timedelta(minutes=30)).isoformat()})
                documents=0
                for c in priority:
                    for announcement in self.store.get('announcements',c,{'rows':[]})['rows']:
                        if documents>=3: break
                        if announcement['published_at'][:10]<str(t.date()-timedelta(days=45)): continue
                        if self.store.get('announcement_document',announcement['id']) or self.store.get('document_error',announcement['id']): continue
                        try:self.store.put('announcement_document',announcement['id'],await self.providers.disclosure(c,announcement))
                        except Exception as e:self.store.put('document_error',announcement['id'],{'id':announcement['id'],'detail':str(e)[:160]})
                        documents+=1
                    if documents>=3: break
                self.learning.promote();self.archive.sync()
                self.recover('research')
            except asyncio.CancelledError: raise
            except Exception as e: log.exception('research');self.fault('research',str(e)[:150])
            await asyncio.sleep(60)

    def health(self):
        t=now();service=self.store.get('state','service',{})
        cycle=service.get('last_cycle');age=(t-parse_time(cycle)).total_seconds() if cycle else None
        return {'status':'RUNNING' if age is not None and age<45 else 'STARTING_OR_STALLED','time':t.isoformat(),
                'session':session(t),'strategy':self.store.get('state','strategy'),'service':service,
                'quote_ages':{c:round((t-parse_time(q['stamp'])).total_seconds(),2) if (q:=self.store.get('quote',c)) else None for c in self.focus},
                'market_scan':self.store.get('state','market_scan'),'history':self.store.get('state','history'),
                'financial_collection':self.store.get('state','financial_collection'),'industry_collection':self.store.get('state','industry_members'),
                'storage':self.archive.storage(),
                'faults':[r for r in self.store.all('fault') if r['active']],
                'capabilities':{'broker_orders':False,'paper_enabled':True,'quote_poll_seconds':5,'market_scan_seconds':300,'quote_max_age_seconds':15}}
