"""Immutable, local point-in-time strategy, evidence and decision archive."""
import gzip
import hashlib
import json
import shutil
from pathlib import Path
from .calendar import now,session
from .config import WEIGHTS,COST,TICK,VERSION
from .store import dumps

class Archive:
    def __init__(self,store):
        self.store=store;self.root=store.path.parent/'strategy_archive';self.root.mkdir(parents=True,exist_ok=True)
        self.code={name:(Path(__file__).parent/name).read_text() for name in
                   ['strategy.py','providers.py','config.py','learning.py','ledger.py','engine.py','calendar.py',
                    'store.py','app.py','archive.py','actions.py']}
        self.previous_signature=None
        readme=self.root/'README.md'
        if not readme.exists(): readme.write_text('''# OpenPike Stocks 策略档案

这里统一保存策略、判定、输入、研究证据和事件。版本和判定文件不可覆盖。

- versions/：规则、权重、费用、硬条件、实现哈希与源码快照。
- decisions/日期/：每分钟观察决策JSON，及配对inputs.json.gz完整输入快照。
- evidence/：公开时间、核验时间、原始财报与来源、人工公告复核记录。
- events/：每个买点、退出、失效、公告和故障的原始事件。

SQLite中的真实/纸面成交、完整交易和验证结果通过策略版本及setup_id关联；真实成交只有手工登记。旧记录迁入并不代表当时源码已保存，manifest.observed_at是首次归档时间。
''')

    def write(self,relative,body,compressed=False):
        path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
        content=json.dumps(body,ensure_ascii=False,allow_nan=False,indent=None if compressed else 2).encode()
        if compressed: content=gzip.compress(content,mtime=0)
        if path.exists(): return path
        temp=path.with_suffix(path.suffix+'.tmp');temp.write_bytes(content);temp.replace(path)
        return path

    def manifest(self,state):
        weights=state.get('weights') or WEIGHTS
        policy={'version':state['version'],'weights':weights,'cost_each_side':COST,'tick':TICK,
            'buy_conditions':{'minimum_score':80,'required_score_completeness':100,'max_quote_age_seconds':15,
                'volume_breakout':1.5,'retest_band':.005,'max_stop_distance':.05,'minimum_net_rr':2,
                'nearest_pressure_only':True,'completed_minutes_only':True,'daily_closed_only':True,'top_one_only':True},
            'holdings':{'T_plus_1':True,'max_holding_trading_days':15,'switch_on_rank_change':False},
            'implementation_sha256':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in self.code.items()}}
        id=hashlib.sha256(dumps(policy).encode()).hexdigest()
        if not self.store.get('strategy_manifest',id):
            body={'id':id,'observed_at':now().isoformat(),'policy':policy,'source_snapshot':self.code,
                  'hypothesis':state.get('hypothesis','固定首版研究规则'),'status_at_archive':state.get('status','ACTIVE'),
                  'record_created_at':state.get('created'),'notice':'首次归档时间不追溯为旧版本创建时间；评分不是胜率'}
            path=self.write(f'versions/{id}.json',body)
            self.store.put('strategy_manifest',id,{'id':id,'version':state['version'],'observed_at':body['observed_at'],
                            'weights':weights,'hypothesis':body['hypothesis'],'path':str(path)})
        return id

    def capture(self,state,rows,contexts,t,signal_key=None):
        mid=self.manifest(state)
        signature=dumps([mid,[(r['code'],r['score']['total'],r['score']['completeness'],r['qualified'],r.get('research_id')) for r in rows]])
        if session(t)!='OPEN' and signature==self.previous_signature and not signal_key: return
        self.previous_signature=signature
        id=t.strftime('%Y%m%dT%H%M')+'-'+mid[:12]
        if signal_key: id+='-signal-'+hashlib.sha256(signal_key.encode()).hexdigest()[:12]
        if self.store.get('decision_archive',id): return
        folder='decisions/'+str(t.date())
        top=next((r for r in rows if r['qualified']),None)
        record={'id':id,'at':t.isoformat(),'session':session(t),'manifest_id':mid,'strategy_version':state['version'],
                'conclusion':'BUY' if top else 'WAIT','selected_code':top['code'] if top else None,'rows':rows,
                'outcome_link':'SQLite trades/closed按version、signal(setup_id)关联','input_file':id+'.inputs.json.gz'}
        inputs=[]
        for c in contexts:
            item={'code':c[0],'name':c[1],'quote':c[4],'industry':c[5],'research':c[6],'decision_at':t.isoformat()}
            for key,bars in [('daily',c[2]),('minutes',c[3])]:
                refs=[];observations=[]
                for start in range(0,len(bars),128):
                    block=bars[start:start+128]
                    # Repeated fetch timestamps are snapshot metadata; price,
                    # volume and source revisions remain content-addressed.
                    normalized=[{k:v for k,v in b.items() if k not in ('observed','observed_at')} for b in block]
                    digest=hashlib.sha256(dumps(normalized).encode()).hexdigest()
                    self.write(f'inputs/{digest}.json.gz',normalized,True);refs.append(digest)
                    observations.extend([{k:b[k] for k in ('observed','observed_at') if k in b} for b in block])
                item[key+'_refs']=refs;item[key+'_observations']=observations
            inputs.append(item)
        ip=self.write(f'{folder}/{id}.inputs.json.gz',inputs,True)
        path=self.write(f'{folder}/{id}.json',record)
        self.store.put('decision_archive',id,{'id':id,'at':record['at'],'manifest_id':mid,'strategy_version':state['version'],
            'conclusion':record['conclusion'],'selected_code':record['selected_code'],'candidate_count':len(rows),
            'max_completeness':max((r['score']['completeness'] for r in rows),default=0),'path':str(path),'input_path':str(ip)})

    def inputs(self,metadata):
        rows=json.loads(gzip.decompress(Path(metadata['input_path']).read_bytes()))
        for item in rows:
            for key in ('daily','minutes'):
                if key+'_refs' not in item: continue # Earlier self-contained snapshots.
                bars=[]
                for digest in item[key+'_refs']:
                    bars.extend(json.loads(gzip.decompress((self.root/'inputs'/f'{digest}.json.gz').read_bytes())))
                item[key]=[{**b,**obs} for b,obs in zip(bars,item[key+'_observations'],strict=True)]
        return rows

    def sync(self):
        if not self.storage()['ready']: return
        state=self.store.get('state','strategy');self.manifest(state)
        from .learning import Learning
        for r in self.store.all('challenger'):
            mid=self.manifest(r);validation=Learning(self.store).validation(r)
            key=hashlib.sha256(dumps([str(now().date()),mid,validation,r['status']]).encode()).hexdigest()
            self.write(f'validation/{key}.json',{'id':key,'at':now().isoformat(),'manifest_id':mid,
                'candidate':r,'validation':validation,'notice':'未通过全部门槛不会自动采用，也不展示胜率'})
        for kind in ('research','financial_evidence','announcement_document'):
            for r in self.store.all(kind):
                key=kind+':'+r['id']
                if self.store.get('archived_evidence',key): continue
                id=hashlib.sha256(key.encode()).hexdigest();path=self.write(f'evidence/{id}.json',r)
                self.store.put('archived_evidence',key,{'id':r['id'],'kind':kind,'code':r['code'],'path':str(path),'at':now().isoformat()})
        for r in self.store.all('earnings_calendar'):
            key='earnings_calendar:'+hashlib.sha256(dumps(r).encode()).hexdigest()
            if not self.store.get('archived_evidence',key):
                path=self.write(f'evidence/{key.split(":")[1]}.json',r)
                self.store.put('archived_evidence',key,{'id':key,'kind':'earnings_calendar','code':r['code'],'path':str(path),'at':now().isoformat()})
        cursor=self.store.get('state','archive_event_cursor',{'id':0})['id']
        with self.store.lock:
            events=[dict(r) for r in self.store.db.execute('SELECT * FROM events WHERE id>? ORDER BY id',(cursor,))]
        for e in events:
            e['body']=json.loads(e['body']);self.write(f'events/{e["id"]:09d}.json',e)
            self.store.put('state','archive_event_cursor',{'id':e['id']})
        # Immutable fills and completed outcomes include account/version/setup
        # links. Mutable holdings remain in SQLite rather than being relabeled.
        for table in ('trades','closed'):
            cursor=self.store.get('state','archive_'+table+'_cursor',{'id':0})['id']
            with self.store.lock:
                records=[dict(r) for r in self.store.db.execute(f'SELECT * FROM {table} WHERE id>? ORDER BY id',(cursor,))]
            for record in records:
                if 'body' in record: record['body']=json.loads(record['body'])
                self.write(f'outcomes/{table}/{record["id"]:09d}.json',record)
                self.store.put('state','archive_'+table+'_cursor',{'id':record['id']})

    def storage(self):
        free=shutil.disk_usage(self.root).free
        return {'free_bytes':free,'minimum_free_bytes':128*1024*1024,'ready':free>=128*1024*1024,
                'notice':'低于128MiB暂停补采、阻断新买并保留已有档案；不会删除个人文件'}

    def summary(self,limit=20):
        with self.store.lock:
            def recent(kind): return [json.loads(r[0]) for r in self.store.db.execute('SELECT body FROM objects WHERE kind=? ORDER BY updated DESC LIMIT ?',(kind,limit))]
            counts={k:self.store.db.execute('SELECT count(*) FROM objects WHERE kind=?',(k,)).fetchone()[0] for k in ('strategy_manifest','decision_archive','archived_evidence')}
        return {'root':str(self.root),'versions':recent('strategy_manifest'),'decisions':recent('decision_archive'),
                'evidence':recent('archived_evidence'),'counts':counts,'at':now().isoformat()}
