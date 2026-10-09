import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from ashare.app import create_app
from ashare.advisor import Advisor


class FakeBridge:
    def __init__(self, directory, events, tools):
        self.events=events;self.tools=tools;self.account_ready=False;self.calls=[]
    async def start(self): self.account_ready=True
    async def close(self): self.account_ready=False
    async def call(self, method, params, timeout=40):
        self.calls.append((method,params))
        if method in ('thread/start','thread/resume'): return {'thread':{'id':'private-thread'},'model':'test-model'}
        if method=='mcpServerStatus/list': return {'data':[]}
        if method=='turn/start':
            await self.events('item/agentMessage/delta',{'threadId':'private-thread','itemId':'answer','delta':'第一段'})
            await self.events('item/completed',{'threadId':'private-thread','item':{'type':'agentMessage','id':'answer','text':'第一段完整回答'}})
            await self.events('turn/completed',{'threadId':'private-thread','turn':{'id':'turn-1','status':'completed'}})
            return {'turn':{'id':'turn-1'}}
        return {}


def replace(app):
    app.state.advisor.bridge=FakeBridge(app.state.advisor.directory/'runtime',app.state.advisor.event,app.state.advisor.tool)
    return app.state.advisor


def test_real_turn_contract_dedup_resume_and_restart(tmp_path):
    async def run():
        app=create_app(tmp_path/'stock.sqlite3',collect=False);a=replace(app);c=a.create()['id']
        result=a.submit(c,'question-request-1','为什么还没有买点？');await a.job
        view=a.read(c);assert view['turns'][0]['status']=='completed'
        assert view['turns'][0]['answer']=='第一段完整回答'
        count=len(a.bridge.calls)
        assert a.submit(c,'question-request-1','为什么还没有买点？')['id']==result['id']
        assert len(a.bridge.calls)==count
        with pytest.raises(ValueError): a.submit(c,'question-request-1','不同问题')
        a.submit(c,'question-request-2','继续分析');await a.job
        assert any(m=='thread/resume' for m,_ in a.bridge.calls)
        settings=next(p for m,p in a.bridge.calls if m=='thread/start')
        assert settings['sandbox']=='read-only' and settings['approvalPolicy']=='never'
        assert {t['name'] for t in settings['dynamicTools']}=={'stocks_snapshot','stock_details','save_change_proposal'}
        await a.close()
        b=Advisor(a.directory,app.state.store,app.state.engine,app.state.advisor.ranking,app.state.advisor.actions,FakeBridge)
        assert len(b.read(c)['turns'])==2
        await b.close();await app.state.engine.providers.client.aclose()
    asyncio.run(run())


def test_white_list_proposals_need_owner_confirmation_and_do_not_change_scores(tmp_path):
    async def run():
        app=create_app(tmp_path/'stock.sqlite3',collect=False);a=replace(app);c=a.create()['id']
        app.state.store.put('instrument','600000',{'code':'600000','name':'浦发银行'})
        original=app.state.store.get('state','strategy')
        a.current={'id':'none','conversation':c,'codex_id':'private-thread'}
        with pytest.raises(ValueError): await a.tool({'threadId':'private-thread','tool':'exec','arguments':{'cmd':'x'}})
        with pytest.raises(ValueError): await a.tool({'threadId':'other-thread','tool':'stocks_snapshot','arguments':{}})
        with pytest.raises(ValueError): a.propose(c,{'kind':'research_note','code':'600000','text':'证据不足','sources':[]})
        p=a.propose(c,{'kind':'watchlist_add','code':'600000','text':'优先观察，不构成买入','sources':[]})
        assert app.state.store.get('state','manual_watchlist',[])==[]
        row=a.read(c)['proposals'][0]
        with pytest.raises(ValueError): a.confirm(p['id'],'wrong',True,'confirmation-1')
        r=a.confirm(p['id'],row['token'],True,'confirmation-1')
        assert r==a.confirm(p['id'],row['token'],True,'confirmation-1')
        assert app.state.store.get('state','manual_watchlist')==['600000']
        assert '600000' in app.state.engine.focus
        with pytest.raises(ValueError): a.confirm(p['id'],row['token'],False,'confirmation-1')
        p=a.propose(c,{'kind':'research_note','code':'600000','text':'未完成人工公告核查','sources':['https://www.sse.com.cn/']})
        row=next(r for r in a.read(c)['proposals'] if r['id']==p['id']);a.confirm(p['id'],row['token'],True,'confirmation-2')
        assert len(app.state.store.all('advisor_note'))==1
        assert app.state.store.all('research')==[]
        assert app.state.store.get('state','strategy')==original
        app.state.engine.archive.sync()
        assert any(r['kind']=='advisor_note' for r in app.state.store.all('archived_evidence'))
        with pytest.raises(ValueError): a.propose(c,{'kind':'strategy_idea','code':'','text':'修改风险','sources':[],'risk_review_complete':True})
        a.current=None;await a.close();await app.state.engine.providers.client.aclose()
    asyncio.run(run())


def test_inherited_mcp_fails_closed(tmp_path):
    async def run():
        app=create_app(tmp_path/'stock.sqlite3',collect=False);a=replace(app);c=a.create()['id']
        old=a.bridge.call
        async def call(method,params,timeout=40):
            if method=='mcpServerStatus/list':return {'data':[{'name':'unexpected'}]}
            return await old(method,params,timeout)
        a.bridge.call=call;a.submit(c,'request-unsafe-tools','测试');await a.job
        assert a.read(c)['turns'][0]['status']=='failed'
        assert not any(m=='turn/start' for m,_ in a.bridge.calls)
        await a.close();await app.state.engine.providers.client.aclose()
    asyncio.run(run())


def test_api_origin_and_payload_validation(tmp_path):
    app=create_app(tmp_path/'stock.sqlite3',collect=False);replace(app)
    with TestClient(app) as client:
        assert client.get('/advisor').status_code==200
        assert client.post('/api/advisor/conversations').status_code==403
        assert client.post('/api/advisor/conversations',headers={'Origin':'https://evil.example'}).status_code==403
        h={'Origin':'http://127.0.0.1:8765'}
        cid=client.post('/api/advisor/conversations',headers=h).json()['id']
        assert client.post(f'/api/advisor/conversations/{cid}/messages',headers=h,json={'request_id':'request-1','question':'问题','shell':'x'}).status_code==422
        assert client.post(f'/api/advisor/conversations/{cid}/messages',headers=h,json={'request_id':'request-1','question':'x'*5001}).status_code==422
        result=client.post(f'/api/advisor/conversations/{cid}/messages',headers=h,json={'request_id':'request-1','question':'问题'})
        assert result.status_code==200
        assert client.get('/api/advisor/conversations/'+cid).status_code==200


def test_restart_marks_inflight_interrupted(tmp_path):
    async def run():
        app=create_app(tmp_path/'stock.sqlite3',collect=False);a=replace(app);c=a.create()['id']
        with a.lock:
            a.db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('unfinished',c,'request-1','fingerprint','问题','','running','2026-10-09','2026-10-09',None,None,'{}'));a.db.commit()
        await a.close()
        b=Advisor(a.directory,app.state.store,app.state.engine,a.ranking,a.actions,FakeBridge)
        assert b.read(c)['turns'][0]['status']=='interrupted'
        await b.close();await app.state.engine.providers.client.aclose()
    asyncio.run(run())


def test_disabled_catalog_is_safe_and_dynamic_calls_are_idempotent(tmp_path):
    async def run():
        app=create_app(tmp_path/'stock.sqlite3',collect=False);a=replace(app);c=a.create()['id']
        old=a.bridge.call
        async def call(method,params,timeout=40):
            if method=='mcpServerStatus/list':return {'data':[{'name':'inactive','runtimeStatus':'disabled','tools':{}}]}
            return await old(method,params,timeout)
        a.bridge.call=call;a.submit(c,'question-safe-catalog','核验');await a.job
        assert a.read(c)['turns'][0]['status']=='completed'
        a.current={'id':'none','conversation':c,'codex_id':'private-thread','codex_turn':'turn-1'}
        data={'kind':'strategy_idea','code':'','text':'未来研究权重差异，保持硬风控','sources':[]}
        params={'threadId':'private-thread','turnId':'turn-1','callId':'call-1','tool':'save_change_proposal','arguments':data}
        assert await a.tool(params)==await a.tool(params)
        assert len(a.read(c)['proposals'])==1
        with pytest.raises(ValueError):await a.tool({**params,'arguments':{**data,'text':'另一个草稿'}})
        with pytest.raises(ValueError):await a.tool({**params,'turnId':'old-turn','callId':'call-2'})
        a.current=None;await a.close();await app.state.engine.providers.client.aclose()
    asyncio.run(run())


def test_stop_and_busy_guard_preserve_user_question(tmp_path):
    async def run():
        app=create_app(tmp_path/'stock.sqlite3',collect=False);a=replace(app);c=a.create()['id'];other=a.create()['id']
        async def start():await asyncio.Event().wait()
        a.bridge.start=start;a.submit(c,'question-stopping-1','保留这个问题');await asyncio.sleep(0)
        with pytest.raises(ValueError):a.submit(other,'question-stopping-2','同时提问')
        await a.stop(c)
        assert a.current is None and a.read(c)['turns'][0]['status']=='interrupted'
        assert a.read(c)['turns'][0]['question']=='保留这个问题'
        await a.close();await app.state.engine.providers.client.aclose()
    asyncio.run(run())


def test_only_embedded_advisor_allows_same_origin_framing(tmp_path):
    app=create_app(tmp_path/'framing.sqlite3',collect=False)
    with TestClient(app) as client:
        assert "frame-ancestors 'self'" in client.get('/advisor?embedded=1').headers['content-security-policy']
        for path in ['/', '/advisor', '/advisor?embedded=0', '/remote-access?embedded=1']:
            assert "frame-ancestors 'none'" in client.get(path).headers['content-security-policy']
