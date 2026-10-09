import math
import statistics
from datetime import timedelta
from .calendar import parse_time, previous_day, bar_complete, session, trading_age
from .config import WEIGHTS, COST, TICK, VERSION, eligible

def linear(value, low, high, maximum):
    if value is None or not math.isfinite(value): return 0.
    return min(1.,max(0.,(value-low)/(high-low)))*maximum

def score(metrics, weights=None):
    parts = {}
    def part(key, specs):
        points = sum(linear(metrics.get(m),lo,hi,pts) for m,lo,hi,pts in specs)
        present = sum(pts for m,lo,hi,pts in specs if metrics.get(m) is not None)
        parts[key]={'points':round(points,4),'maximum':WEIGHTS[key],'available':present,
                    'inputs':{m:metrics.get(m) for m,_,_,_ in specs}}
    part('relative',[('relative5',-.03,.03,10),('relative10',-.05,.05,10)])
    part('industry',[('industry5',-.02,.02,10),('breadth',0,1,5)])
    part('structure',[('efficiency10',0,.5,10),('drawdown10',.1,0,5)])
    part('volume',[('breakout_volume',1,2.5,10),('retest_volume',1,.5,5)])
    amount=metrics.get('amount20')
    parts['liquidity']={'points':round((linear(math.log(amount),math.log(5e7),math.log(5e8),5) if amount and amount>0 else 0)+linear(metrics.get('spread'),.005,.001,5),4),
                        'maximum':10,'available':5*(amount is not None)+5*(metrics.get('spread') is not None),
                        'inputs':{'amount20':amount,'spread':metrics.get('spread')}}
    part('fundamentals',[('profit_growth',0,.5,5),('cash_profit',0,1,5),('catalyst',0,1,5)])
    part('risk',[('return20',.30,.10,5),('risk_clear',0,1,5)])
    if weights:
        for k,p in parts.items():
            factor=weights[k]/WEIGHTS[k]
            p['points']=round(p['points']*factor,4);p['available']*=factor;p['maximum']=weights[k]
    return {'total':round(sum(p['points'] for p in parts.values()),2),
            'completeness':round(sum(p['available'] for p in parts.values()),2),'parts':parts}

def completed_daily(rows,t):
    # Ignore today's daily bar even after the bell until next trading session:
    # strategy decisions consistently reference the prior closed session.
    return [b for b in rows if b['stamp']<str(t.date()) and b['complete'] and
            (not b.get('observed') or parse_time(b['observed'])<=t)]

def ret(rows,n): return rows[-1]['close']/rows[-n-1]['close']-1 if len(rows)>n and rows[-n-1]['close']>0 else None

def breakout(rows):
    hits=[]
    for i in range(max(20,len(rows)-3),len(rows)):
        before=rows[i-20:i]
        volume=statistics.mean(b['volume'] for b in before)
        level=max(b['high'] for b in before)
        if volume>0 and rows[i]['volume']>=1.5*volume and rows[i]['close']>level:
            hits.append({'date':rows[i]['stamp'],'level':level,'volume_ratio':rows[i]['volume']/volume})
    return hits[-1] if hits else None

def retest_pair(minutes,level,t):
    rows=[b for b in minutes if b['stamp'][:10]==str(t.date()) and bar_complete(b['stamp'],t) and b['complete']]
    found=None
    for i in range(3,len(rows)-1):
        b,n=rows[i],rows[i+1]
        # Confirmation must be the immediately following completed interval.
        if parse_time(n['stamp'])-parse_time(b['stamp'])!=timedelta(minutes=5): continue
        if level*.995<=b['low']<=level*1.005 and rows[i-1]['close']>level and n['close']>max(b['high'],level):
            vol=statistics.mean(v['volume'] for v in rows[i-3:i])
            found={'retest':b,'confirm':n,'volume_ratio':b['volume']/vol if vol>0 else None}
    return found

def pivots(rows):
    # A high is confirmed only by two later completed bars and is never taken
    # from an unfinished/future bar. Plateaus do not fabricate multiple highs.
    return [b['high'] for i,b in enumerate(rows) if 2<=i<len(rows)-2 and
            b['high']>max(r['high'] for r in rows[i-2:i]) and
            b['high']>=max(r['high'] for r in rows[i+1:i+3])]

def quote_issues(q,t):
    if not q: return ['尚无报价']
    age=(t-parse_time(q['stamp'])).total_seconds()
    reasons=[]
    if not -2<=age<=15: reasons.append('报价超过15秒或时间异常')
    if q.get('status')!='NORMAL': reasons.append('停牌或交易状态未知')
    if q.get('conflict'): reasons.append('行情源关键字段冲突')
    if q.get('upper',0)<=0 or q.get('lower',0)<=0: reasons.append('涨跌停范围未知')
    if q.get('ask',0)<=0 or q.get('ask_size',0)<100: reasons.append('卖一无可成交数量／封板')
    if q.get('upper',0)>0 and q.get('ask',0)>=q['upper']-.005: reasons.append('涨停附近不提示新买')
    return reasons

def research_metrics(research,t):
    values={'profit_growth':None,'cash_profit':None,'catalyst':None,'risk_clear':None}
    if not research: return values,False
    if parse_time(research['published_at'])>t or parse_time(research['observed_at'])>t: return values,False
    facts=research.get('facts',{})
    positive=facts.get('net_profit')
    if positive is not None:
        values['profit_growth']=facts.get('profit_growth') if positive>0 else 0.
        if facts.get('operating_cashflow') is not None:
            values['cash_profit']=facts['operating_cashflow']/positive if positive>0 else 0.
    fresh=parse_time(research['valid_until'])>=t
    # Completed scope review is explicit, and expires. An LLM narrative alone
    # never implies absence of an announcement or changes a research score.
    if fresh and facts.get('catalyst_review_complete'):
        values['catalyst']=0.
        for event in facts.get('catalysts',[]):
            days=trading_age(event['published_at'][:10],str(t.date()))
            if days is not None and 0<=days<=20 and parse_time(event['published_at'])<=t:
                if event['kind']=='order' and event['ratio']>=.10 or event['kind']=='commissioned_capacity' and event['ratio']>=.20:
                    values['catalyst']=1.
    if fresh and facts.get('risk_review_complete'):
        values['risk_clear']=float(not any(facts.get(k,True) for k in ('active_reduction','controller_freeze','earnings_next3')))
    complete=fresh and values['catalyst'] is not None and values['risk_clear'] is not None and all(values[k] is not None for k in ('profit_growth','cash_profit'))
    return values,complete

METRIC_WEIGHTS={'relative5':10,'relative10':10,'industry5':10,'breadth':5,'efficiency10':10,'drawdown10':5,
    'breakout_volume':10,'retest_volume':5,'amount20':5,'spread':5,'profit_growth':5,'cash_profit':5,'catalyst':5,'risk_clear':5,'return20':5}

def data_coverage(metrics,ds,minutes,industry,research,t):
    gaps=[];waiting=0;missing=0
    for key,weight in METRIC_WEIGHTS.items():
        if metrics.get(key) is not None: continue
        status='missing';source='日线采集';reason='日线不足或尚未采集'
        if key in ('relative5','relative10','industry5','breadth'):
            source='行业成员日线 / 沪深300'
            reason='行业分类未核实' if not industry else f"行业有效成员{industry.get('effective',0)}/{industry.get('members',0)}，覆盖{industry.get('coverage',0):.0%}，要求至少5家及80%"
        elif key=='breakout_volume' and len(ds)>=23:
            status='condition_wait';source='已收盘日线';reason='数据已到，最近三日没有符合规则的放量突破'
        elif key=='retest_volume':
            source='完成五分钟线'
            finished=[b for b in minutes if b['stamp'][:10]==str(t.date()) and b.get('complete') and bar_complete(b['stamp'],t)]
            if len(finished)>=4:
                status='condition_wait';reason='分钟数据已到，尚未出现合格回踩与后一根确认'
            else:reason='当日完成分钟线不足四根，等待采集或开盘后形成'
        elif key=='spread':
            source='实时双边盘口';reason='尚无有效买一卖一报价，可能封板或状态未知'
        elif key in ('profit_growth','cash_profit'):
            source='公开利润表 / 同期现金流量表';reason='净利润、同比或同期经营现金流缺项，或来源冲突'
        elif key in ('catalyst','risk_clear'):
            source='公司公告全文 / 披露日历'
            reason='最近20交易日订单及实际投产范围尚未审阅' if key=='catalyst' else '未结束减持、控股股东冻结及未来三交易日披露日程尚未完整核查'
        if status=='missing': missing+=weight
        else:waiting+=weight
        gaps.append({'metric':key,'weight':weight,'status':status,'reason':reason,'source':source})
    return {'material_completeness':100-missing,'missing_material_weight':missing,
            'condition_waiting_weight':waiting,'gaps':gaps,
            'notice':'资料覆盖不等于评分输入齐全，更不等于可以买入；未触发条件不会被补成得分'}

def evaluate(code,name,daily,minutes,q,industry,research,t,weights=None,version=VERSION):
    ds=completed_daily(daily,t);m={};reasons=[]
    if not eligible(code,name): reasons.append('不在沪深主板正常上市范围')
    if session(t)!='OPEN': reasons.append('当前非连续竞价时段')
    reasons.extend(quote_issues(q,t))
    if len(ds)<121: reasons.append('已收盘日线不足121根')
    expected=previous_day(t.date())
    if not expected or not ds or ds[-1]['stamp']!=str(expected): reasons.append('日线未覆盖最近已收盘交易日')
    if len(ds)>20:
        closes=[b['close'] for b in ds[-11:]]
        travel=sum(abs(b-a) for a,b in zip(closes,closes[1:]))
        m['efficiency10']=max(0,closes[-1]-closes[0])/travel if travel else 0.
        peak=closes[0];dd=0.
        for c in closes:
            peak=max(peak,c);dd=max(dd,1-c/peak)
        m['drawdown10']=dd;m['return20']=ret(ds,20)
        amounts=[b.get('amount') for b in ds[-20:]]
        if all(a is not None and a>0 for a in amounts): m['amount20']=statistics.median(amounts)
        if ds[-1]['close']<=statistics.mean(b['close'] for b in ds[-20:]): reasons.append('昨日收盘未站上20日均线')
    if industry and industry.get('valid'):
        r5,r10=ret(ds,5),ret(ds,10)
        if r5 is not None: m['relative5']=r5-industry['return5']
        if r10 is not None: m['relative10']=r10-industry['return10']
        m.update(industry5=industry['relative5'],breadth=industry['breadth'])
    else: reasons.append('行业成员数据不足或分类未核实')
    hit=breakout(ds)
    pair=None
    if hit:
        m['breakout_volume']=hit['volume_ratio'];pair=retest_pair(minutes,hit['level'],t)
    else: reasons.append('最近三日无合格放量收盘突破')
    if pair: m['retest_volume']=pair['volume_ratio']
    else: reasons.append('完成五分钟线尚未确认回踩买点')
    finished=[b for b in minutes if b['stamp'][:10]==str(t.date()) and b['complete'] and bar_complete(b['stamp'],t)]
    # A historical confirmed setup cannot stay actionable after a new complete
    # bar changes the context, or when the minute source stops publishing.
    if pair and (not finished or pair['confirm']['stamp']!=finished[-1]['stamp'] or t-parse_time(pair['confirm']['stamp'])>timedelta(minutes=5,seconds=3)): reasons.append('回踩确认已过期，等待新确认')
    if session(t)=='OPEN':
        elapsed=(t.hour*60+t.minute)-(9*60+30 if t.hour<12 else 13*60)
        expected_bar=t.replace(second=0,microsecond=0)-timedelta(minutes=t.minute%5)
        if t.second<3: expected_bar-=timedelta(minutes=5)
        if elapsed>=5 and (not finished or parse_time(finished[-1]['stamp'])<expected_bar): reasons.append('分钟数据未覆盖最近完成线')
    if q and q['bid']>0 and q['ask']>0: m['spread']=(q['ask']-q['bid'])/((q['ask']+q['bid'])/2)
    rm,rc=research_metrics(research,t)
    if research and research.get('financial_conflicts'):
        rm['profit_growth']=None;rm['cash_profit']=None;rc=False;reasons.append('财务来源关键字段冲突，等待核验')
    m.update(rm)
    if not rc: reasons.append('财报／公告关键证据待核实或已过期')
    if rm['risk_clear']==0: reasons.append('减持、控股股东冻结或财报窗口风险')
    if research and any(research.get('facts',{}).get(k) is True for k in ('active_reduction','controller_freeze','earnings_next3')):
        reasons.append('已确认的减持、控股股东冻结或财报窗口风险')
    sc=score(m,weights)
    if sc['total']<80: reasons.append('研究评分未达到80分')
    if sc['completeness']<100: reasons.append('关键评分数据不完整')
    entry=q.get('ask') if q else None;stop=None;target=None;rr=None
    if pair and entry and entry>0:
        stop=round(math.floor((pair['retest']['low']-TICK+1e-7)/TICK)*TICK,2)
        if stop<=0 or stop>=entry or (entry-stop)/entry>.05: reasons.append('失效距离不在0至5%内')
        levels=pivots(ds[-120:])+pivots(finished)
        if q.get('upper',0)>0: levels.append(q['upper'])
        above=[p for p in levels if p>entry+.005]
        target=min(above) if above else None
        if target:
            risk=entry*(1+COST)-stop*(1-COST)
            profit=target*(1-COST)-entry*(1+COST)
            rr=profit/risk if risk>0 else None
        if rr is None or rr<2: reasons.append('最近压力扣费后盈亏比不足2倍')
    setup=f'{version}:{code}:{hit["date"]}:{pair["retest"]["stamp"]}' if hit and pair else None
    return {'code':code,'name':name,'strategy_version':version,'evaluated_at':t.isoformat(),
            'quote':q,'score':sc,'coverage':data_coverage(m,ds,minutes,industry,research,t),'metrics':m,'industry':industry,'research_id':research.get('id') if research else None,
            'qualified':not reasons,'reasons':list(dict.fromkeys(reasons)),
            'entry':entry,'stop':stop,'target':target,'net_rr':round(rr,4) if rr is not None else None,
            'breakout':hit,'retest':pair,'setup_id':setup,
            'local_detection_delay_seconds':(t-parse_time(pair['confirm']['stamp'])).total_seconds() if pair else None,
            'daily_asof':ds[-1]['stamp'] if ds else None,'minute_asof':finished[-1]['stamp'] if finished else None,
            'data_sources':list(dict.fromkeys(b['source'] for b in ds[-1:]+finished[-1:]))}
