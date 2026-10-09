import asyncio
import gzip
import hashlib
import json
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from ashare.providers import Providers
from ashare.config import TZ,WEIGHTS
from ashare.store import Store,dumps
from ashare.archive import Archive
from ashare.engine import Engine
from ashare.strategy import evaluate,data_coverage,METRIC_WEIGHTS
from ashare.compact_store import compact
from ashare.app import create_app
from ashare.tests.test_safety import setup

T=datetime(2026,10,9,10,0,5,tzinfo=TZ)

async def provider(handler):
    p=Providers();await p.client.aclose();p.client=httpx.AsyncClient(transport=httpx.MockTransport(handler));return p

def test_current_tencent_units_and_missing_amount_not_fabricated(monkeypatch):
    monkeypatch.setattr('ashare.providers.now',lambda:T)
    async def run():
        def handler(r):
            if 'push2his' in str(r.url):return httpx.Response(567)
            return httpx.Response(200,text='kline_dayqfq='+json.dumps({'data':{'sz002675':{'qfqday':[
                ['2026-10-08','10','11','12','9','123',{},'1','456',''],
                ['2026-10-09','11','11','12','10','10',{},'1','--','']]}}}))
        p=await provider(handler);bars=await p.daily('002675');await p.client.aclose()
        assert bars[0]['volume']==12300 and bars[0]['amount']==4560000
        assert bars[0]['complete'] and not bars[1]['complete']
        assert bars[1]['amount'] is None
    asyncio.run(run())

def test_financial_asof_and_same_period_cashflow(monkeypatch):
    monkeypatch.setattr('ashare.providers.now',lambda:T)
    async def run():
        def handler(r):
            if 'sina' in str(r.url):return httpx.Response(200,json={'result':{'data':{'report_list':{}}}})
            if r.url.params['reportName'].endswith('CASHFLOW'):
                rows=[{'SECURITY_CODE':'002675','NOTICE_DATE':'2026-04-28 00:00:00','REPORT_DATE':'2025-12-31','NETCASH_OPERATE':99}]
            else:
                rows=[{'SECURITY_CODE':'002675','NOTICE_DATE':'2026-10-30 00:00:00','REPORT_DATE':'2026-09-30','PARENT_NETPROFIT':999},
                {'SECURITY_CODE':'002675','NOTICE_DATE':'2026-08-28 00:00:00','REPORT_DATE':'2026-06-30','PARENT_NETPROFIT':100,'PARENT_NETPROFIT_RATIO':10},
                {'SECURITY_CODE':'002675','NOTICE_DATE':'2026-04-28 00:00:00','REPORT_DATE':'2025-12-31','TOTAL_OPERATE_INCOME':1000}]
            return httpx.Response(200,json={'result':{'data':rows}})
        p=await provider(handler);r=await p.financial_report('002675');await p.client.aclose()
        assert r['facts']['net_profit']==100 and r['facts']['profit_growth']==.1
        assert r['facts']['operating_cashflow'] is None and r['facts']['annual_revenue']==1000
        assert r['facts']['reporting_period']=='2026-06-30'
    asyncio.run(run())

def test_financial_conflict_blocks_qualified_signal(setup):
    args=list(deepcopy(setup));args[6]['financial_conflicts']=['net_profit']
    row=evaluate(*args)
    assert not row['qualified'] and row['metrics']['profit_growth'] is None
    assert row['score']['completeness']==90

def test_truncated_industry_catalog_is_rejected():
    async def run():
        p=await provider(lambda r:httpx.Response(200,json={'result':{'count':100,'pages':1,'data':[{'SECURITY_CODE':'002675'}]}}))
        with pytest.raises(ValueError,match='分页覆盖不足'):await p.financial_industries()
        await p.client.aclose()
    asyncio.run(run())

def test_calendar_no_record_does_not_clear_risk(monkeypatch):
    monkeypatch.setattr('ashare.providers.now',lambda:T)
    async def run():
        p=await provider(lambda r:httpx.Response(200,json={'result':{'data':[]}}))
        with pytest.raises(ValueError,match='不能据此'):await p.earnings_calendar('002675')
        await p.client.aclose()
    asyncio.run(run())

def test_calendar_observed_schedule_not_future_knowledge(monkeypatch):
    monkeypatch.setattr('ashare.providers.now',lambda:T)
    async def run():
        rows=[{'SECURITY_CODE':'002675','EITIME':'2026-10-08 10:00:00','APPOINT_PUBLISH_DATE':'2026-10-13 00:00:00','ACTUAL_PUBLISH_DATE':None,'REPORT_DATE':'2026-09-30'},
              {'SECURITY_CODE':'002675','EITIME':'2026-10-10 10:00:00','APPOINT_PUBLISH_DATE':'2026-10-29 00:00:00','ACTUAL_PUBLISH_DATE':None,'REPORT_DATE':'2026-09-30'}]
        p=await provider(lambda r:httpx.Response(200,json={'result':{'data':rows}}));record=await p.earnings_calendar('002675');await p.client.aclose()
        assert record['scheduled_date']=='2026-10-13' and record['earnings_next3']
    asyncio.run(run())

def test_document_excludes_scripts_and_is_unreviewed():
    async def run():
        html='<script>not evidence</script><div id="content"><p>'+('公开公告原文。'*30)+'</p></div><footer>footer</footer>'
        p=await provider(lambda r:httpx.Response(200,content=html.encode('gb18030')))
        doc=await p.disclosure('002675',{'id':'sina:1','title':'公告','source_url':'https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=1','published_at':'2026-10-09'})
        await p.client.aclose()
        assert 'script' not in doc['text'] and 'footer' not in doc['text'] and not doc['reviewed']
    asyncio.run(run())

def test_failed_history_does_not_starve_other_codes(tmp_path):
    s=Store(tmp_path/'db');e=Engine(s);e.focus=['002675'];s.put('history_error','002675',{'retry_after':(T+timedelta(minutes=5)).isoformat()})
    s.put('instrument','600519',{'code':'600519','name':'贵州茅台'})
    _,pending,due=e.history_candidates(T)
    assert '002675' in pending and '002675' not in due and '600519' in due
    asyncio.run(e.providers.client.aclose())

def test_coverage_waiting_is_not_missing_or_awarded(setup):
    row=evaluate(*setup);m=deepcopy(row['metrics']);m['breakout_volume']=None;m['retest_volume']=None
    coverage=data_coverage(m,setup[2],setup[3],setup[5],setup[6],T)
    assert sum(METRIC_WEIGHTS.values())==100
    assert coverage['material_completeness']==100 and coverage['condition_waiting_weight']==15
    args=list(deepcopy(setup));args[3][-2]['low']*=.98
    row=evaluate(*args)
    assert not row['qualified'] and row['score']['completeness']<100

def test_archive_immutable_replay_restart_and_signal_same_minute(tmp_path,setup):
    s=Store(tmp_path/'db');a=Archive(s);state=s.get('state','strategy');row=evaluate(*setup)
    a.capture(state,[row],[setup],T);m=a.summary()['decisions'][0];saved=Path(m['path']).read_bytes()
    contexts=list(deepcopy(setup));contexts[2][-1]['close']=99
    a.capture(state,[row],[tuple(contexts)],T+timedelta(seconds=10))
    assert Path(m['path']).read_bytes()==saved and a.inputs(m)[0]['daily']==setup[2]
    a.capture(state,[row],[tuple(contexts)],T+timedelta(seconds=11),signal_key='new-setup')
    assert a.summary()['counts']['decision_archive']==2
    b=Archive(Store(tmp_path/'db'));assert b.inputs(m)[0]['daily']==setup[2]
    changed={**state,'version':'candidate-test','weights':dict(WEIGHTS)}
    assert b.manifest(changed)!=m['manifest_id']

def test_archive_preserves_more_than_200_events(tmp_path):
    s=Store(tmp_path/'db');a=Archive(s)
    for i in range(205):s.event(str(i),'DATA_FAULT','',{'detail':str(i)})
    a.sync();assert len(list((a.root/'events').glob('*.json')))==205
    a.sync();assert len(list((a.root/'events').glob('*.json')))==205

def test_compressed_store_lossless_legacy_migration(tmp_path):
    s=Store(tmp_path/'db');rows=[{'stamp':'2026-10-08','close':10,'amount':None},{'stamp':'2026-10-09','close':11,'complete':False}]
    with s.transaction() as db:
        db.executemany('INSERT INTO bars VALUES(?,?,?,?)',[('002675','day',r['stamp'],dumps(r)) for r in rows])
    s.db.close();compact(tmp_path/'db');s=Store(tmp_path/'db')
    assert s.bars('002675','day')==rows
    s.save_bars('002675','day',[{**rows[-1],'close':12}])
    assert s.bars('002675','day')==[rows[0],{**rows[-1],'close':12}]
    assert list(tmp_path.glob('pre-compression-*.gz'))

def test_financial_merge_preserves_known_freeze(tmp_path):
    s=Store(tmp_path/'db');e=Engine(s)
    s.put('research','r',{'id':'r','code':'002083','published_at':'2026-08-20T00:00:00+08:00','observed_at':'2026-10-09T09:00:00+08:00','valid_until':'2026-10-09T16:00:00+08:00','sources':['https://example.org/notice'],'facts':{'reporting_period':None,'controller_freeze':True,'risk_review_complete':False}})
    s.put('financial','002083',{'id':'f','code':'002083','published_at':'2026-08-25T00:00:00+08:00','observed_at':'2026-10-09T09:30:00+08:00','sources':['https://example.org/finance'],'facts':{'reporting_period':'2026-06-30','net_profit':100},'conflicts':[]})
    r=e.research('002083',T);assert r['facts']['controller_freeze'] is True and r['facts']['risk_review_complete'] is False and r['facts']['net_profit']==100
    asyncio.run(e.providers.client.aclose())

def test_low_storage_blocks_new_buy_and_archive_unknown_id(tmp_path,setup,monkeypatch):
    app=create_app(tmp_path/'db',collect=False);s=app.state.store;e=app.state.engine;e.focus=['002675']
    s.put('instrument','002675',{'code':'002675','name':'东诚药业','universe_observed':T.isoformat()});s.save_bars('002675','day',setup[2]);s.save_bars('002675','5m',setup[3]);s.quote(setup[4])
    monkeypatch.setattr(e,'industry_metrics',lambda key,t:setup[5]);monkeypatch.setattr(e,'research',lambda code,t:setup[6])
    monkeypatch.setattr('ashare.archive.shutil.disk_usage',lambda path:SimpleNamespace(free=127*1024*1024))
    e.evaluate_all(T);assert not e.ranking[0]['qualified'] and any('磁盘空间不足' in x for x in e.ranking[0]['reasons'])
    with TestClient(app) as c: assert c.get('/api/archive/decisions/unknown').status_code==404

def test_partial_research_registration_keeps_dashboard_available(tmp_path):
    app=create_app(tmp_path/'db',collect=False);s=app.state.store
    s.put('financial','002675',{'id':'f','code':'002675','published_at':'2026-08-25T00:00:00+08:00','observed_at':'2026-10-09T09:30:00+08:00','sources':['https://example.org/finance'],'facts':{'reporting_period':'2026-06-30','net_profit':100,'operating_cashflow':120,'profit_growth':.1},'conflicts':[]})
    with TestClient(app) as c:
        r=c.post('/api/research',json={'request_id':'partial-announcement-review','code':'002675','published_at':'2026-10-09T09:00:00+08:00','observed_at':T.isoformat(),'valid_until':'2026-10-09T16:00:00+08:00','sources':['https://example.org/notice'],'summary':'仅核查本次公告，未完成全部风险范围。','facts':{'controller_freeze':True}})
        assert r.status_code==200,r.text
        d=c.get('/api/dashboard');assert d.status_code==200,d.text
        record=next(r for r in d.json()['research'] if r['code']=='002675')
        assert record['facts']['net_profit']==100 and record['facts']['reporting_period']=='2026-06-30'
        assert record['facts']['controller_freeze'] and not record['facts']['risk_review_complete']
        assert '002675' not in [r['code'] for r in c.get('/api/research/queue').json()['rows']]

def test_unexpected_backend_failure_returns_json(tmp_path,monkeypatch):
    app=create_app(tmp_path/'db',collect=False)
    def fail():raise RuntimeError('private internal details')
    monkeypatch.setattr(app.state.engine,'health',fail)
    with TestClient(app,raise_server_exceptions=False) as c:
        r=c.get('/api/health');assert r.status_code==500 and r.json()['code']=='BACKEND_ERROR'
        assert 'private internal' not in r.text

def test_industry_members_must_be_in_current_eligible_universe(tmp_path,monkeypatch):
    s=Store(tmp_path/'db');e=Engine(s);monkeypatch.setattr('ashare.engine.now',lambda:T)
    s.put('instrument','002675',{'code':'002675','name':'东诚药业','universe_observed':T.isoformat()})
    s.put('instrument','600001',{'code':'600001','name':'ST测试','universe_observed':T.isoformat()})
    s.put('instrument','600002',{'code':'600002','name':'过期状态','universe_observed':(T-timedelta(days=1)).isoformat()})
    async def catalog():return [{'node':'em-report:test','name':'测试','members':['002675','600001','600002','001381'],'verified_at':T.isoformat(),'member_source_url':'https://example.org/source'}]
    monkeypatch.setattr(e.providers,'financial_industries',catalog)
    asyncio.run(e.refresh_industries());assert s.get('industry','em-report:test')['members']==['002675']
    asyncio.run(e.providers.client.aclose())
