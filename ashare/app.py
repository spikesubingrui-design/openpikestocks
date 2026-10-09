import json
import asyncio
import gzip
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .calendar import now,parse_time,session,SOURCE,trading_age,bar_complete
from .config import DATA, VERSION, ROOT
from .actions import overview
from .store import Store,dumps
from .engine import Engine
from .remote import Access,limited_body
from .advisor import Advisor

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)

class Trade(Input):
    request_id:str=Field(min_length=8,max_length=160)
    code:str=Field(pattern=r'^\d{6}$')
    side:Literal['BUY','SELL']
    quantity:int=Field(gt=0,le=100000000)
    price:float=Field(gt=0,le=100000)
    stamp:str
    stop:float|None=Field(default=None,gt=0)
    target:float|None=Field(default=None,gt=0)
    cost:float|None=Field(default=None,ge=0)

    @field_validator('stamp')
    @classmethod
    def timestamp(cls,v): return parse_time(v).isoformat()

class Catalyst(Input):
    kind:Literal['order','commissioned_capacity']
    ratio:float=Field(ge=0,le=100)
    published_at:str
    source_url:str
    @field_validator('source_url')
    @classmethod
    def source(cls,v):
        u=urlparse(v)
        if u.scheme not in ('https','http') or not u.netloc: raise ValueError('来源须为完整网页地址')
        return v

class Facts(Input):
    net_profit:float|None=None
    profit_growth:float|None=None
    operating_cashflow:float|None=None
    reporting_period:str|None=None
    annual_revenue:float|None=Field(default=None,gt=0)
    catalyst_review_complete:bool=False
    catalysts:list[Catalyst]=Field(default_factory=list,max_length=30)
    risk_review_complete:bool=False
    active_reduction:bool|None=None
    controller_freeze:bool|None=None
    earnings_next3:bool|None=None
    @model_validator(mode='after')
    def risk_scope(self):
        if self.risk_review_complete and any(getattr(self,k) is None for k in ('active_reduction','controller_freeze','earnings_next3')):
            raise ValueError('完整风险核查须明确减持、冻结、未来三交易日财报状态')
        return self

class Research(Input):
    request_id:str=Field(min_length=8,max_length=160)
    code:str=Field(pattern=r'^\d{6}$')
    published_at:str
    observed_at:str
    valid_until:str
    sources:list[str]=Field(min_length=1,max_length=30)
    summary:str=Field(min_length=1,max_length=12000)
    facts:Facts
    analyst:str='当前聊天研究'
    @model_validator(mode='after')
    def evidence(self):
        t=now()
        pub,obs,end=map(parse_time,(self.published_at,self.observed_at,self.valid_until))
        if pub>obs or obs>t or end<obs: raise ValueError('公开、核验和有效截止时间顺序无效')
        # Announcement-clear claims expire within one calendar day so new
        # notices cannot leave a weeks-old clean-risk flag actionable.
        if (self.facts.risk_review_complete or self.facts.catalyst_review_complete) and end-obs>timedelta(days=1):
            raise ValueError('公告与风险核查有效期最多24小时')
        for s in self.sources:
            if urlparse(s).scheme not in ('http','https') or not urlparse(s).netloc: raise ValueError('来源地址无效')
        for c in self.facts.catalysts:
            if parse_time(c.published_at)>obs: raise ValueError('催化公开时间不得晚于核验时间')
            if c.source_url not in self.sources: raise ValueError('催化证据须列入来源清单')
        return self

class Ack(Input):
    token:str
    disposition:Literal['notified','silent']
    explanation:str=Field(max_length=2000)

class Rollback(Input):
    request_id:str=Field(min_length=8)

class AdvisorQuestion(Input):
    request_id:str=Field(min_length=8,max_length=160)
    question:str=Field(min_length=1,max_length=5000)

class AdvisorConfirmation(Input):
    request_id:str=Field(min_length=8,max_length=160)
    token:str=Field(min_length=16,max_length=160)
    accept:bool

def create_app(path=None,collect=True):
    store=Store(path or DATA/'monitor.sqlite3');engine=Engine(store)
    @asynccontextmanager
    async def life(app):
        if collect: engine.start()
        try: yield
        finally:
            await app.state.advisor.close()
            if collect: await engine.stop()
            else: await engine.providers.client.aclose()
    app=FastAPI(title='OpenPike Stocks',version=VERSION,lifespan=life)
    app.state.store=store;app.state.engine=engine
    remote=Access(store.path.parent/'remote/access.sqlite3')

    @app.middleware('http')
    async def local_only(request:Request,call_next):
        if request.url.hostname not in ('127.0.0.1','localhost','testserver'):
            return JSONResponse({'detail':'仅允许本机访问'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            if origin and origin not in ('http://127.0.0.1:8765','http://localhost:8765'):
                return JSONResponse({'detail':'拒绝跨站登记'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        ancestors = "'self'" if request.url.path == '/advisor' and request.query_params.get('embedded') == '1' else "'none'"
        response.headers['Content-Security-Policy']=f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors {ancestors}; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,exc): return JSONResponse({'detail':str(exc)},status_code=422)

    @app.exception_handler(Exception)
    async def backend_failure(request,exc):
        return JSONResponse({'detail':'后台处理异常，请稍后刷新；采集故障详情见健康状态。','code':'BACKEND_ERROR'},
                            status_code=500,headers={'Cache-Control':'no-store'})

    def ranking():
        value=store.get('state','rankings',{'at':None,'strategy_version':VERSION,'rows':[]})
        t=now()
        for r in value['rows']:
            if session(t)!='OPEN' or (t-parse_time(r['evaluated_at'])).total_seconds()>15 or not engine.archive.storage()['ready']:
                r['qualified']=False
                r['reasons']=list(dict.fromkeys(r['reasons']+['非交易时段或检测结果已过期']))
        return value

    advisor=Advisor(store.path.parent/'advisor',store,engine,ranking,
                    lambda:overview(ranking(),engine.ledger.holdings(),now()))
    app.state.advisor=advisor

    def advisor_origin(request):
        if request.headers.get('origin') not in ('http://127.0.0.1:8765','http://localhost:8765'):
            raise HTTPException(403,'请从软件内的AI顾问页面操作')

    @app.get('/advisor')
    def advisor_page(): return FileResponse(Path(__file__).parent/'static/advisor.html')
    @app.get('/api/advisor/status')
    def advisor_status(): return advisor.status()
    @app.get('/api/advisor/conversations')
    def advisor_conversations(): return {'rows':advisor.conversations()}
    @app.post('/api/advisor/conversations')
    def advisor_create(request:Request):
        advisor_origin(request); return advisor.create()
    @app.get('/api/advisor/conversations/{conversation}')
    def advisor_read(conversation:str): return advisor.read(conversation)
    @app.post('/api/advisor/conversations/{conversation}/messages')
    async def advisor_message(conversation:str,body:AdvisorQuestion,request:Request):
        advisor_origin(request); return advisor.submit(conversation,body.request_id,body.question)
    @app.post('/api/advisor/conversations/{conversation}/stop')
    async def advisor_stop(conversation:str,request:Request):
        advisor_origin(request); return await advisor.stop(conversation)
    @app.post('/api/advisor/proposals/{identifier}/confirm')
    def advisor_confirm(identifier:str,body:AdvisorConfirmation,request:Request):
        advisor_origin(request); return advisor.confirm(identifier,body.token,body.accept,body.request_id)
    @app.get('/api/advisor/notes')
    def advisor_notes(): return {'rows':store.all('advisor_note')}

    @app.get('/api/health')
    def health(): return {**engine.health(),'calendar_source':SOURCE,'calendar_verified_year':2026,
                          'service_name':'OpenPike Stocks','desktop_protocol':1,'workspace_root':str(ROOT)}
    @app.get('/remote-access')
    def remote_access(): return FileResponse(Path(__file__).parent/'static/remote-access.html')
    @app.get('/api/remote/status')
    def remote_status(): return {**remote.status(),'setup_token':remote.get('setup_token')}
    @app.post('/api/remote/password')
    async def remote_password(request:Request):
        if request.headers.get('origin') not in ('http://127.0.0.1:8765','http://localhost:8765'):
            raise HTTPException(403,'请在主机软件的远程访问页面设置密码')
        try: body=json.loads(await limited_body(request))
        except (ValueError,UnicodeError): raise HTTPException(422,'设置内容格式不正确')
        if not isinstance(body,dict) or set(body)-{'password','setup_token'}:
            raise HTTPException(422,'设置内容格式不正确')
        return await asyncio.to_thread(remote.configure,body.get('password'),body.get('setup_token'))
    @app.get('/api/quotes')
    def quotes(): return {'at':now().isoformat(),'session':session(now()),'rows':store.all('quote')}
    @app.get('/api/bars/{code}')
    def bars(code:str,frame:Literal['5m','day']='5m'):
        if not store.get('instrument',code): raise HTTPException(404,'未知股票代码')
        t=now();rows=[]
        for b in store.bars(code,frame):
            observed=b.get('observed_at') or b.get('observed')
            if observed and parse_time(observed)>t: continue
            complete=bar_complete(b['stamp'],t) if frame=='5m' else parse_time(b['stamp']).date()<t.date()
            rows.append({**b,'complete':bool(b.get('complete',True) and complete)})
        return {'code':code,'frame':frame,'at':t.isoformat(),'strategy_version':VERSION,
                'adjustment':'前复权' if frame=='day' else '未复权','rows':rows[-1023:]}
    @app.get('/api/rankings')
    def rankings(): return ranking()
    @app.get('/api/signals')
    def signals():
        r=ranking();return {'at':now().isoformat(),'current':next((x for x in r['rows'] if x['qualified']),None),'events':store.events()}
    @app.get('/api/actions')
    def actions(): return overview(ranking(),engine.ledger.holdings(),now())
    @app.get('/api/holdings')
    def holdings(): return engine.ledger.summary()
    @app.get('/api/evidence')
    def evidence(code:str|None=None):
        rows=store.all('research');ann=store.all('announcements')
        return {'research':[r for r in rows if code is None or r['code']==code],
                'announcements':[r for r in ann if code is None or r['code']==code],
                'industries':store.all('industry') if code is None else [],'calendar_source':SOURCE}
    @app.get('/api/research')
    def research(): return {'rows':store.all('research')}
    @app.get('/api/research/queue')
    def queue():
        rows=ranking()['rows'];priority=[]
        for r in rows:
            record=engine.research(r['code'],now())
            if record and record.get('analyst') and record.get('analyst')!='自动采集' and (now()-parse_time(record['observed_at'])).total_seconds()<1800 and not record.get('new_announcements_unreviewed'):
                continue
            if not record or parse_time(record['valid_until'])<now() or not record['facts'].get('risk_review_complete'):
                priority.append({'code':r['code'],'name':r['name'],'score':r['score']['total'],'missing':r['reasons'],
                                 'announcements':store.get('announcements',r['code'],{}),'latest_research':record,
                                 'earnings_calendar':store.get('earnings_calendar',r['code']),
                                 'documents':[{'id':d['id'],'title':d['title'],'source_url':d['source_url'],'reviewed':d['reviewed']} for d in store.all('announcement_document') if d['code']==r['code']]})
        return {'at':now().isoformat(),'rows':priority[:12]}
    @app.get('/api/data-coverage')
    def coverage():
        rows=ranking()['rows'];missing={}
        for r in rows:
            for g in r.get('coverage',{}).get('gaps',[]):
                if g['status']=='missing': missing[g['metric']]=missing.get(g['metric'],0)+1
        return {'at':now().isoformat(),'collection':engine.health(),'missing_by_metric':missing,
            'rows':[{'code':r['code'],'name':r['name'],'score_input_completeness':r['score']['completeness'],
                     'coverage':r.get('coverage')} for r in rows]}
    @app.get('/api/archive')
    def archive(): return engine.archive.summary()
    @app.get('/api/archive/versions/{id}')
    def archived_version(id:str):
        record=store.get('strategy_manifest',id)
        if not record: raise HTTPException(404,'策略档案不存在')
        return json.loads(Path(record['path']).read_text())
    @app.get('/api/archive/decisions/{id}')
    def archived_decision(id:str):
        record=store.get('decision_archive',id)
        if not record: raise HTTPException(404,'观察档案不存在')
        return json.loads(Path(record['path']).read_text())
    @app.get('/api/archive/decisions/{id}/inputs')
    def archived_inputs(id:str):
        record=store.get('decision_archive',id)
        if not record: raise HTTPException(404,'观察档案不存在')
        return engine.archive.inputs(record)
    @app.get('/api/financial')
    def financial(code:str|None=None):
        return {'at':now().isoformat(),'rows':[r for r in store.all('financial') if code is None or r['code']==code]}
    @app.get('/api/learning')
    def learning(): return engine.learning.status()
    @app.get('/api/disclosures/{id}')
    def disclosure(id:str):
        record=store.get('announcement_document',id)
        if not record: raise HTTPException(404,'公告正文尚未补采')
        return record
    @app.get('/api/events')
    def events(pending:bool=False): return {'at':now().isoformat(),'rows':store.events(pending)}
    @app.post('/api/events/claim')
    def claim():
        t=now();token=secrets.token_urlsafe(24);lease=(t+timedelta(minutes=3)).isoformat()
        with store.transaction() as db:
            rs=db.execute('SELECT * FROM events WHERE ack IS NULL AND (lease IS NULL OR lease<?) ORDER BY id LIMIT 20',(t.isoformat(),)).fetchall()
            for r in rs: db.execute('UPDATE events SET claim=?,lease=? WHERE id=?',(token,lease,r['id']))
        # Read the claimed rows directly: the dashboard's newest-200 window
        # must not hide old pending events and leave the FIFO consumer stuck.
        return {'token':token,'lease_until':lease,'rows':[
            {**dict(r),'body':json.loads(r['body']),'claim':token,'lease':lease} for r in rs]}
    @app.post('/api/events/{event_id}/ack')
    def ack(event_id:int,body:Ack):
        with store.transaction() as db:
            r=db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
            if not r: raise HTTPException(404,'事件不存在')
            if r['ack']: return {'acknowledged':True,'ack':json.loads(r['ack'])}
            if r['claim']!=body.token or not r['lease'] or parse_time(r['lease'])<now(): raise HTTPException(409,'事件领取已过期或令牌不匹配')
            data={'at':now().isoformat(),'disposition':body.disposition,'explanation':body.explanation,
                  'chat_delay_seconds':(now()-parse_time(r['created'])).total_seconds()}
            db.execute('UPDATE events SET ack=? WHERE id=?',(dumps(data),event_id))
        return {'acknowledged':True,'ack':data}
    @app.post('/api/trades')
    def trade(body:Trade): return engine.ledger.register(body.model_dump(exclude_none=True))
    @app.post('/api/opening-balances')
    def opening(body:Trade):
        if body.side!='BUY': raise ValueError('期初持仓只登记已有买入')
        return engine.ledger.register(body.model_dump(exclude_none=True),opening=True)
    @app.post('/api/research')
    def add_research(body:Research):
        if not store.get('instrument',body.code): raise ValueError('未知股票代码')
        data=body.model_dump();data['id']=body.request_id
        def action(db):
            snapshot=[r['id'] for r in store.get('announcements',body.code,{'rows':[]})['rows']]
            db.execute('INSERT INTO objects VALUES(?,?,?,?)',('research',body.request_id,dumps({**data,'announcement_ids':snapshot}),now().isoformat()))
            return {'registered':True,'id':body.request_id}
        return store.once(body.request_id,data,action)
    @app.post('/api/learning/rollback')
    def rollback(body:Rollback): return engine.learning.rollback(body.request_id)
    @app.get('/api/dashboard')
    def dashboard():
        r=ranking();ledger=engine.ledger.summary();t=now()
        return {'health':engine.health(),'rankings':r,'ledger':ledger,'actions':overview(r,ledger['holdings'],t),
                             'events':store.events()[:30],'research':[r for c in engine.focus if (r:=engine.research(c,now()))],
                             'archive':engine.archive.summary(12),'learning':{'active':store.get('state','strategy'),'candidates':store.all('challenger')}}
    @app.get('/')
    def index(): return FileResponse(Path(__file__).parent/'static/index.html')
    app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
    return app

app=create_app()
