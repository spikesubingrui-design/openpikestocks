from copy import deepcopy
from datetime import datetime,timedelta,date
import json
import pytest
from fastapi.testclient import TestClient
from ashare.config import TZ,VERSION,eligible
from ashare.calendar import trading_day,session,bar_complete
from ashare.strategy import score,evaluate,linear,pivots,quote_issues
from ashare.store import Store
from ashare.ledger import Ledger
from ashare.app import create_app
from ashare.learning import Learning
from ashare.providers import parse_tencent,parse_sina

@pytest.fixture
def setup():
    t=datetime(2026,10,9,10,0,5,tzinfo=TZ)
    days=[];d=t.date()-timedelta(days=1)
    while len(days)<121:
        if trading_day(d): days.append(str(d))
        d-=timedelta(days=1)
    days.reverse();daily=[]
    for i,day in enumerate(days):
        c=10 if i<115 else 10.1+(i-115)*.03
        daily.append({'stamp':day,'open':c-.01,'close':c,'high':c+.01,'low':c-.02,'volume':2500000 if i==120 else 1000000,
                      'amount':600000000,'source':'fixture','observed':t.isoformat(),'complete':True})
    level=daily[-2]['high']
    minutes=[]
    for i,minute in enumerate([40,45,50,55,0]):
        stamp=f'2026-10-09 {9 if i<4 else 10:02d}:{minute:02d}:00'
        low,high,close=(10.3,10.4,10.35) if i<3 else (level,level+.02,level+.01) if i==3 else (level+.01,level+.05,level+.04)
        minutes.append({'stamp':stamp,'open':close,'close':close,'low':low,'high':high,'volume':500 if i==3 else 1000,
                        'source':'fixture','complete':True})
    entry=level+.05
    q={'code':'002675','name':'东诚药业','price':entry,'previous':daily[-1]['close'],'bid':entry-.01,'ask':entry,
       'bid_size':100000,'ask_size':100000,'upper':11.5,'lower':9.,'open':10.3,'volume':1000000,
       'status':'NORMAL','stamp':t.isoformat(),'observed':t.isoformat(),'source':'fixture','conflict':False}
    industry={'valid':True,'return5':-.03,'return10':-.04,'relative5':.03,'breadth':1}
    research={'id':'fixture-research','published_at':'2026-08-22T00:00:00+08:00','observed_at':'2026-10-09T09:00:00+08:00',
              'valid_until':'2026-10-09T16:00:00+08:00','facts':{'net_profit':100.,'profit_growth':.6,'operating_cashflow':110.,
              'catalyst_review_complete':True,'catalysts':[{'kind':'order','ratio':.2,'published_at':'2026-10-08T09:00:00+08:00'}],
              'risk_review_complete':True,'active_reduction':False,'controller_freeze':False,'earnings_next3':False}}
    return ('002675','东诚药业',daily,minutes,q,industry,research,t)

def test_positive_fixture_and_determinism(setup):
    a=evaluate(*setup);b=evaluate(*deepcopy(setup))
    assert a==b
    assert a['qualified'],a['reasons']
    assert a['score']['total']==100 and a['score']['completeness']==100
    assert a['net_rr']>=2

@pytest.mark.parametrize('change,reason',[
    ('stale','报价超过'),('sealed','卖一无'),('conflict','冲突'),('unknown','状态未知'),
    ('incomplete','未确认'),('tight_pressure','盈亏比不足'),('missing','关键'),('freeze','冻结'),('future_daily','日线未覆盖')])
def test_fail_closed(setup,change,reason):
    args=list(deepcopy(setup));q=args[4];t=args[7]
    if change=='stale': q['stamp']=(t-timedelta(seconds=16)).isoformat()
    if change=='sealed': q['ask']=0;q['ask_size']=0
    if change=='conflict':q['conflict']=True
    if change=='unknown':q['status']='UNKNOWN'
    if change=='incomplete':args[3][-1]['complete']=False
    if change=='tight_pressure':q['upper']=q['ask']+.03
    if change=='missing':args[6]=None
    if change=='freeze':args[6]['facts']['controller_freeze']=True
    if change=='future_daily':args[2][-1]['stamp']='2026-10-09'
    row=evaluate(*args)
    assert not row['qualified']
    assert any(reason in r for r in row['reasons']),row['reasons']

def test_near_pressure_is_not_skipped(setup):
    args=list(deepcopy(setup))
    q=args[4];q['upper']=q['ask']+.04
    row=evaluate(*args)
    assert row['target']==q['upper'] and row['net_rr']<2 and not row['qualified']

def test_no_future_pivot_or_incomplete_confirmation(setup):
    args=list(deepcopy(setup));t=args[-1]
    future=deepcopy(args[3][-1]);future['stamp']='2026-10-09 10:05:00';future['high']=99;future['complete']=True
    args[3].append(future)
    assert evaluate(*args)['target']==evaluate(*setup)['target']
    assert not bar_complete(future['stamp'],t)
    args[-1]=t+timedelta(minutes=94)
    args[4]['stamp']=args[-1].isoformat()
    assert not evaluate(*args)['qualified']

def test_retest_low_must_stay_inside_half_percent_band(setup):
    args=list(deepcopy(setup));args[3][-2]['low']*=.98
    assert not evaluate(*args)['qualified']

def test_history_observed_after_decision_cannot_be_used(setup):
    args=list(deepcopy(setup));args[2][-1]['observed']=(args[-1]+timedelta(minutes=1)).isoformat()
    row=evaluate(*args)
    assert not row['qualified'] and any('日线未覆盖' in r for r in row['reasons'])

def test_fifteen_day_exit_includes_purchase_day(tmp_path,setup):
    s=Store(tmp_path/'db');s.put('instrument','002675',{'code':'002675','name':'东诚药业'});ledger=Ledger(s)
    ledger.register({'request_id':'fifteen-day-fixture','code':'002675','side':'BUY','quantity':100,'price':10.,'stamp':'2026-09-11T10:00:00+08:00'})
    q=deepcopy(setup[4]);s.quote(q);ledger.exits(setup[-1])
    assert s.events()[0]['kind']=='EXIT' and '15个交易日' in s.events()[0]['body']['reason']

def test_missing_scores_not_rescaled():
    s=score({'relative5':.03})
    assert s['total']==10 and s['completeness']==10
    assert sum(v['maximum'] for v in s['parts'].values())==100
    assert linear(10,0,1,5)==5 and linear(-1,0,1,5)==0
    assert score({'profit_growth':-.2,'cash_profit':-1,'drawdown10':.5})['total']==0

def test_calendar_and_board_exclusions():
    assert trading_day(date(2026,10,1)) is False
    assert trading_day(date(2026,10,10)) is False
    assert trading_day(date(2027,1,4)) is None
    assert session(datetime(2026,10,9,11,45,tzinfo=TZ))=='LUNCH'
    assert all(not eligible(c,n) for c,n in [('300001','正常'),('688001','正常'),('920001','正常'),('002001','ST测试'),('002001','退市测试')])

def test_event_dedup_survives_restart(tmp_path):
    path=tmp_path/'db';s=Store(path)
    a=s.event('unique','BUY','002675',{'reason':'test'})
    s.db.close();s=Store(path)
    assert s.event('unique','BUY','002675',{'reason':'changed'})==a
    assert len(s.events())==1

def test_manual_ledger_t1_idempotency_and_gap(tmp_path,setup):
    s=Store(tmp_path/'db');s.put('instrument','002675',{'code':'002675','name':'东诚药业'});ledger=Ledger(s)
    buy={'request_id':'buy-fixture-01','code':'002675','side':'BUY','quantity':100,'price':10.,'stamp':'2026-10-08T10:00:00+08:00','stop':9.5,'target':11.}
    ledger.register(buy);ledger.register(buy)
    assert ledger.holdings('real')[0]['quantity']==100
    with pytest.raises(ValueError):ledger.register({**buy,'price':10.2})
    sell={**buy,'request_id':'sell-fixture-01','side':'SELL','price':9.1}
    sell.pop('stop');sell.pop('target')
    with pytest.raises(ValueError,match=r'T\+1'):ledger.register(sell)
    sell['stamp']='2026-10-09T10:00:00+08:00';ledger.register(sell)
    assert ledger.holdings('real')==[]
    closed=ledger.summary()['closed'][0]
    assert closed['net_return']<-.09 # actual gap price, not invented stop fill

def test_paper_requires_executable_quotes_and_separate_real(tmp_path,setup):
    s=Store(tmp_path/'db');s.put('instrument','002675',{'code':'002675','name':'东诚药业'});ledger=Ledger(s)
    row=evaluate(*setup);t=setup[-1]
    ledger.paper_entry(row,t)
    assert ledger.holdings('paper',t) and not ledger.holdings('real',t)
    assert ledger.holdings('paper',t)[0]['sellable']==0
    ledger.paper_entry(row,t)
    assert len(ledger.holdings('paper',t)[0]['lots'])==1
    q=deepcopy(row['quote']);q['price']=1;q['bid']=0;q['bid_size']=0;s.quote(q)
    ledger.exits(t+timedelta(days=1))
    assert ledger.holdings('paper',t)[0]['quantity']>0

def test_api_event_claim_ack_and_input_validation(tmp_path):
    app=create_app(tmp_path/'db',collect=False)
    with TestClient(app) as client:
        s=app.state.store;s.event('event1','DATA_FAULT','',{'detail':'fixture'})
        claim=client.post('/api/events/claim').json()
        assert len(claim['rows'])==1
        assert client.post('/api/events/claim').json()['rows']==[]
        id=claim['rows'][0]['id']
        assert client.post(f'/api/events/{id}/ack',json={'token':'wrong','disposition':'silent','explanation':'test'}).status_code==409
        assert client.post(f'/api/events/{id}/ack',json={'token':claim['token'],'disposition':'silent','explanation':'test'}).status_code==200
        assert client.get('/api/events?pending=true').json()['rows']==[]
        assert client.post('/api/trades',json={'request_id':'invalid-price','code':'002675','side':'BUY','quantity':100,'price':0,'stamp':'2026-10-09T10:00:00+08:00'}).status_code==422
        assert client.post('/api/events/claim',headers={'Origin':'https://evil.example'}).status_code==403
        assert client.get('/').status_code==200

def test_research_cannot_override_policy_or_assert_missing_clearance(tmp_path):
    app=create_app(tmp_path/'db',collect=False)
    with TestClient(app) as c:
        r={'request_id':'research-fixture-01','code':'002675','published_at':'2026-08-22T00:00:00+08:00',
           'observed_at':'2026-10-08T16:00:00+08:00','valid_until':'2026-10-09T16:00:00+08:00','sources':['https://www.sse.com.cn'],
           'summary':'fixture','facts':{'net_profit':1,'profit_growth':.6,'operating_cashflow':1}}
        assert c.post('/api/research',json=r).status_code==200
        assert c.post('/api/research',json=r).status_code==200
        assert len(c.get('/api/research').json()['rows'])==1
        assert c.post('/api/research',json={**r,'request_id':'research-fixture-02','score':100}).status_code==422
        r['facts']['risk_review_complete']=True
        assert c.post('/api/research',json={**r,'request_id':'research-fixture-03'}).status_code==422

def test_learning_insufficient_forward_evidence_never_promotes(tmp_path,setup):
    s=Store(tmp_path/'db');l=Learning(s);l.weekly(setup[-1]);l.weekly(setup[-1])
    assert len(s.all('challenger'))==3
    for c in s.all('challenger'):
        v=l.validation(c);assert not v['eligible'];assert v['metrics']['completed_trades']==0
    l.promote();assert s.get('state','strategy')['version']==VERSION

def test_new_announcement_invalidates_old_clearance(tmp_path):
    app=create_app(tmp_path/'db',collect=False)
    record={'id':'review-test','code':'002675','published_at':'2026-08-22T00:00:00+08:00','observed_at':'2026-10-09T09:00:00+08:00',
            'valid_until':'2026-10-09T16:00:00+08:00','announcement_ids':['old'],'facts':{'risk_review_complete':True,'catalyst_review_complete':True}}
    s=app.state.store;s.put('research','review-test',record)
    s.put('announcements','002675',{'rows':[{'id':'new','published_at':'2026-10-09 00:00:00'}]})
    r=app.state.engine.research('002675',datetime(2026,10,9,10,tzinfo=TZ))
    assert not r['facts']['risk_review_complete'] and r['new_announcements_unreviewed']

def test_raw_quote_unit_normalization_and_status():
    t=datetime(2026,10,9,10,tzinfo=TZ);f=['0']*90
    for i,v in {1:'东诚药业',2:'002675',3:'13.6',4:'12.63',5:'12.61',6:'1000',9:'13.59',10:'66',19:'13.60',20:'940',30:'20261009100000',37:'43063',47:'13.89',48:'11.37'}.items():f[i]=v
    q=parse_tencent('v_sz002675="'+'~'.join(f)+'";',t)['002675']
    assert q['volume']==100000 and q['bid_size']==6600 and q['amount']==430630000
    f=['0']*33
    for i,v in {0:'东诚药业',1:'12.61',2:'12.63',3:'13.6',8:'100000',10:'6600',20:'94000',30:'2026-10-09',31:'10:00:00',32:'00'}.items():f[i]=v
    q=parse_sina('var hq_str_sz002675="'+','.join(f)+'";',t)['002675']
    assert q['volume']==100000 and q['bid_size']==6600 and q['status']=='NORMAL'

def test_chart_api_keeps_unfinished_bar_explicit(tmp_path,setup,monkeypatch):
    app=create_app(tmp_path/'chart.db',collect=False);s=app.state.store
    s.put('instrument','002675',{'code':'002675','name':'东诚药业'})
    bars=deepcopy(setup[3]);future=deepcopy(bars[-1]);future['stamp']='2026-10-09 10:05:00';bars.append(future)
    s.save_bars('002675','5m',bars)
    monkeypatch.setattr('ashare.app.now',lambda:setup[-1])
    with TestClient(app) as client:
        r=client.get('/api/bars/002675').json()
        assert r['rows'][-1]['complete'] is False
        assert r['rows'][-2]['complete'] is True
        assert r['adjustment']=='未复权'
        assert client.get('/api/bars/999999').status_code==404
        assert client.get('/api/bars/002675?frame=1m').status_code==422

def test_rollback_retry_does_not_pop_another_version(tmp_path):
    s=Store(tmp_path/'rollback.db');l=Learning(s)
    base={'version':'fixed-1.0.0','weights':{},'history':[]}
    prior={'version':'v2','weights':{},'history':[{'previous':base}]}
    latest={'version':'v3','weights':{},'history':[{'previous':prior}]}
    s.put('state','strategy',latest)
    assert l.rollback('retry-rollback-1')==prior
    assert l.rollback('retry-rollback-1')==prior
    assert s.get('state','strategy')==prior
    assert len(s.events())==1

def test_claim_oldest_events_even_when_more_than_dashboard_window(tmp_path):
    app=create_app(tmp_path/'queue.db',collect=False)
    for i in range(225): app.state.store.event(f'queue-{i}','DATA_FAULT','',{'component':'fixture'})
    with TestClient(app) as client:
        result=client.post('/api/events/claim').json()
        assert len(result['rows'])==20
        assert [r['id'] for r in result['rows']]==list(range(1,21))
        assert all(r['claim']==result['token'] for r in result['rows'])
