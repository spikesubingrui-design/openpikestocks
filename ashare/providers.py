import asyncio
import json
import re
import math
import html
from html.parser import HTMLParser
from urllib.parse import urlencode
from datetime import datetime, timedelta
import httpx
from .calendar import now, bar_complete,trading_age
from .config import TZ, symbol, eligible

SINA_NODE = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'

def num(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else 0.
    except (ValueError, TypeError): return 0.

def finite(v):
    if v is None or v in ('','--','-'): return None
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None

class DisclosureText(HTMLParser):
    def __init__(self): super().__init__();self.depth=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag=='div' and (self.depth or dict(attrs).get('id')=='content'): self.depth+=1
    def handle_endtag(self,tag):
        if tag=='div' and self.depth: self.depth-=1
    def handle_data(self,data):
        if self.depth and data.strip(): self.parts.append(data.strip())

def parse_tencent(text, observed):
    result = {}
    for sym, raw in re.findall(r'v_(\w+)="([^"]*)"',text):
        f = raw.split('~')
        if len(f) < 50 or not re.fullmatch(r'\d{14}',f[30]): continue
        stamp = datetime.strptime(f[30],'%Y%m%d%H%M%S').replace(tzinfo=TZ).isoformat()
        q = {'code':f[2],'name':f[1],'price':num(f[3]),'previous':num(f[4]),'open':num(f[5]),
             'volume':num(f[6])*100,'amount':num(f[37])*10000,'turnover':num(f[38]),
             'bid':num(f[9]),'bid_size':num(f[10])*100,'ask':num(f[19]),'ask_size':num(f[20])*100,
             'high':num(f[33]),'low':num(f[34]),'upper':num(f[47]),'lower':num(f[48]),
             'stamp':stamp,'observed':observed.isoformat(),'source':'腾讯','source_url':'https://qt.gtimg.cn/q='+sym,
             'depth':{'bid':[[num(f[9+i*2]),num(f[10+i*2])*100] for i in range(5)],
                      'ask':[[num(f[19+i*2]),num(f[20+i*2])*100] for i in range(5)]},
             'status':'UNKNOWN'}
        result[f[2]]=q
    return result

def parse_sina(text, observed):
    result={}
    for sym, raw in re.findall(r'hq_str_(\w+)="([^"]*)"',text):
        f=raw.split(',')
        if len(f)<33: continue
        try: stamp=datetime.fromisoformat(f[30]+'T'+f[31]).replace(tzinfo=TZ).isoformat()
        except ValueError: continue
        q={'code':sym[2:],'name':f[0],'price':num(f[3]),'previous':num(f[2]),'open':num(f[1]),
           'high':num(f[4]),'low':num(f[5]),'bid':num(f[6]),'ask':num(f[7]),
           'volume':num(f[8]),'amount':num(f[9]),'bid_size':num(f[10]),'ask_size':num(f[20]),
           'stamp':stamp,'observed':observed.isoformat(),'source':'新浪','source_url':'https://hq.sinajs.cn/list='+sym,
           'upper':round(num(f[2])*1.1+1e-8,2),'lower':round(num(f[2])*.9+1e-8,2),
           'status':'NORMAL' if f[32]=='00' and num(f[1])>0 else 'UNKNOWN',
           'depth':{'bid':[[num(f[11+i*2]),num(f[10+i*2])] for i in range(5)],
                    'ask':[[num(f[21+i*2]),num(f[20+i*2])] for i in range(5)]}}
        result[q['code']]=q
    return result

class Providers:
    def __init__(self):
        self.client=httpx.AsyncClient(timeout=12,headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.sina.com.cn'},limits=httpx.Limits(max_connections=8))
        self.semaphore=asyncio.Semaphore(4)

    async def text(self,url,encoding=None):
        async with self.semaphore:
            r=await self.client.get(url);r.raise_for_status()
            return r.content.decode(encoding or 'utf8',errors='replace')

    async def quotes(self,codes):
        syms=','.join(symbol(c) for c in codes)
        async def read(url,parser):
            try:
                r=await self.client.get(url,timeout=4);r.raise_for_status()
                s=r.content.decode('gb18030',errors='replace')
                return parser(s,now()),None
            except Exception as e: return {},type(e).__name__+': '+str(e)[:120]
        a,b=await asyncio.gather(read('https://qt.gtimg.cn/q='+syms,parse_tencent),read('https://hq.sinajs.cn/list='+syms,parse_sina))
        return a,b

    async def daily(self,code):
        sym='sh000300' if code=='000300' else symbol(code);t=now()
        # Public current Tencent endpoint also supplies actual amount (万元).
        # The legacy endpoint has WAF failures for many symbols and lacks it.
        params={'_var':'kline_dayqfq','param':f'{sym},day,{t.year-1}-01-01,{t.year+1}-12-31,640,qfq'}
        url='https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?'+urlencode(params)
        try:
            text=await self.text(url);doc=json.loads(text[text.find('{'):])
            data=doc['data'][sym];raw=data.get('qfqday',data.get('day',[]))
            if not raw: raise ValueError('新版日线为空')
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            url=f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,180,qfq'
            doc=json.loads(await self.text(url));data=doc['data'][sym];raw=data.get('qfqday',data.get('day',[]))
        bars=[]
        for r in raw[-200:]:
            value=finite(r[8]) if len(r)>8 else None
            bars.append({'stamp':r[0],'open':num(r[1]),'close':num(r[2]),'high':num(r[3]),'low':num(r[4]),
                'volume':num(r[5])*(1 if code=='000300' else 100),'amount':value*10000 if value is not None else None,
                'source':'腾讯前复权','source_url':url,'amount_source_url':url if value is not None else None,
                'observed':t.isoformat(),'complete':r[0]<str(t.date()),'adjustment':'qfq','provider_revision':'daily-v2'})
        if any(b['amount'] is None for b in bars[-21:]):
            secid=('1' if code.startswith('6') or code=='000300' else '0')+'.'+code
            eu=('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid='+secid+
                '&klt=101&fqt=0&beg=20250101&end=20500101&lmt=200&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61')
            try:
                edoc=json.loads(await self.text(eu));amounts={r.split(',')[0]:finite(r.split(',')[6]) for r in edoc['data']['klines']}
                for b in bars:
                    if b['amount'] is None and amounts.get(b['stamp']) is not None:
                        b['amount']=amounts[b['stamp']];b['amount_source_url']=eu
            except (httpx.HTTPError,ValueError,TypeError,KeyError): pass
        if not bars: raise ValueError('两个日线接口均未返回有效数据')
        return bars

    async def financial_report(self,code):
        t=now();sources=[]
        async def report(name):
            url='https://datacenter-web.eastmoney.com/api/data/v1/get?'+urlencode({
                'reportName':name,'columns':'ALL','filter':f'(SECURITY_CODE="{code}")',
                'pageSize':8,'pageNumber':1,'sortColumns':'REPORT_DATE','sortTypes':-1})
            doc=json.loads(await self.text(url));rows=doc['result']['data']
            # Null/malformed publication dates are not point-in-time evidence.
            rows=[r for r in rows if r.get('SECURITY_CODE')==code and r.get('NOTICE_DATE') and
                  datetime.fromisoformat(r['NOTICE_DATE']).replace(tzinfo=TZ)<=t]
            sources.append(url);return rows
        incomes,cash=await asyncio.gather(report('RPT_DMSK_FN_INCOME'),report('RPT_DMSK_FN_CASHFLOW'))
        if not incomes: raise ValueError('没有当时已公开的利润表')
        inc=max(incomes,key=lambda r:r['REPORT_DATE']);period=inc['REPORT_DATE'][:10]
        cf=next((r for r in cash if r['REPORT_DATE'][:10]==period),None)
        annual=next((r for r in sorted(incomes,key=lambda r:r['REPORT_DATE'],reverse=True) if r['REPORT_DATE'][5:10]=='12-31'),None)
        facts={'net_profit':finite(inc.get('PARENT_NETPROFIT')),'profit_growth':None,
               'operating_cashflow':finite(cf.get('NETCASH_OPERATE')) if cf else None,'reporting_period':period,
               'annual_revenue':finite(annual.get('TOTAL_OPERATE_INCOME')) if annual else None}
        growth=finite(inc.get('PARENT_NETPROFIT_RATIO'))
        if growth is not None: facts['profit_growth']=growth/100
        # Independent numerical cross-check, not a synthetic cash-flow proxy.
        cross_url=f'https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022?paperCode={symbol(code)}&source=gjzb&type=0'
        checks={};conflicts=[]
        try:
            doc=json.loads(await self.text(cross_url));r=doc['result']['data']['report_list'].get(period.replace('-',''))
            if r and datetime.strptime(r['publish_date'],'%Y%m%d').replace(tzinfo=TZ)<=t:
                fields={v['item_field']:v for v in r['data'] if v.get('item_field')}
                p=fields.get('PARENETP',{});checks={'net_profit':finite(p.get('item_value')),'profit_growth':finite(p.get('item_tongbi'))}
                for key,v in checks.items():
                    if v is not None and facts[key] is not None and abs(v-facts[key])>max(.002 if key=='profit_growth' else 1,abs(v)*.002): conflicts.append(key)
                sources.append(cross_url)
        except (httpx.HTTPError,ValueError,KeyError,TypeError): pass
        pub=max([inc['NOTICE_DATE']]+([cf['NOTICE_DATE']] if cf else []))
        return {'code':code,'facts':facts,'published_at':datetime.fromisoformat(pub).replace(tzinfo=TZ).isoformat(),
                'observed_at':t.isoformat(),'sources':sources,'units':'CNY元 / 同比小数比例',
                'cross_check':checks,'conflicts':conflicts,'raw_income':inc,'raw_cashflow':cf,'raw_annual':annual}

    async def financial_industries(self):
        t=now()
        # Whole-market classification from one consistent reported dataset.
        # No mixing Sina's older broad categories with a second taxonomy.
        period=f'{t.year}-06-30' if t.month>=9 else f'{t.year-1}-12-31' if t.month>=5 else f'{t.year-1}-06-30'
        base='https://datacenter-web.eastmoney.com/api/data/v1/get?'
        common={'reportName':'RPT_DMSK_FN_INCOME','columns':'SECURITY_CODE,SECURITY_NAME_ABBR,INDUSTRY_CODE,INDUSTRY_NAME,NOTICE_DATE,REPORT_DATE',
                'filter':f"(REPORT_DATE='{period}')",'pageSize':500,'sortColumns':'SECURITY_CODE','sortTypes':1}
        async def page(n):
            url=base+urlencode({**common,'pageNumber':n});doc=json.loads(await self.text(url))
            return doc['result'],url
        first,url=await page(1);pages=await asyncio.gather(*(page(n) for n in range(2,first['pages']+1)))
        rows=first['data']+[r for d,u in pages for r in d['data']]
        if len({r['SECURITY_CODE'] for r in rows})<first['count']*.98: raise ValueError('财报行业全市场分页覆盖不足')
        groups={}
        for r in rows:
            if not eligible(r['SECURITY_CODE'],r['SECURITY_NAME_ABBR']) or not r.get('INDUSTRY_CODE') or not r.get('INDUSTRY_NAME'): continue
            if not r.get('NOTICE_DATE') or datetime.fromisoformat(r['NOTICE_DATE']).replace(tzinfo=TZ)>t: continue
            key='em-report:'+str(r['INDUSTRY_CODE'])
            g=groups.setdefault(key,{'node':key,'name':r['INDUSTRY_NAME'],'members':[],
                'verified_at':t.isoformat(),'member_source_url':url,'schema':'东方财富财报行业分类','reporting_period':period})
            g['members'].append(r['SECURITY_CODE'])
        return [{**g,'members':sorted(set(g['members']))} for g in groups.values()]

    async def earnings_calendar(self,code):
        t=now();url='https://datacenter-web.eastmoney.com/api/data/v1/get?'+urlencode({
            'reportName':'RPT_PUBLIC_BS_APPOIN','columns':'ALL','filter':f'(SECURITY_CODE="{code}")',
            'pageSize':8,'pageNumber':1,'sortColumns':'REPORT_DATE','sortTypes':-1})
        doc=json.loads(await self.text(url));rows=doc['result']['data']
        public=[r for r in rows if r.get('SECURITY_CODE')==code and r.get('EITIME') and
                datetime.fromisoformat(r['EITIME']).replace(tzinfo=TZ)<=t]
        upcoming=[r for r in public if r.get('APPOINT_PUBLISH_DATE') and not r.get('ACTUAL_PUBLISH_DATE')]
        if not upcoming: raise ValueError('未找到已公开的待披露预约，不能据此断言无财报风险')
        scheduled=min(upcoming,key=lambda r:r['APPOINT_PUBLISH_DATE'])
        day=datetime.fromisoformat(scheduled['APPOINT_PUBLISH_DATE']).date()
        if day<t.date(): raise ValueError('待披露预约日期已过，需人工核查')
        age=trading_age(t.date(),day)
        return {'code':code,'scheduled_date':str(day),'reporting_period':scheduled['REPORT_DATE'][:10],
            'earnings_next3':0<=age<=3,'published_at':datetime.fromisoformat(scheduled['EITIME']).replace(tzinfo=TZ).isoformat(),
            'observed_at':t.isoformat(),'source_url':url,'raw':scheduled,'notice':'预约日可能变更；仅补充日程，不代表减持或冻结已核查'}

    async def disclosure(self,code,announcement):
        url=announcement['source_url']
        if not url.startswith('https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?'):
            raise ValueError('该来源需人工读取公告附件，不能把网页壳当全文')
        raw=await self.text(url,'gb18030');parser=DisclosureText();parser.feed(raw)
        content='\n'.join(parser.parts)
        if len(content)<80: raise ValueError('公告正文为空或过短')
        return {'id':announcement['id'],'code':code,'title':announcement['title'],'published_at':announcement['published_at'],
            'observed_at':now().isoformat(),'source_url':url,'text':content,'reviewed':False}

    async def minutes(self,code):
        url=f'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData?symbol={symbol(code)}&scale=5&ma=no&datalen=1023'
        s=await self.text(url);start=s.find('[{');end=s.rfind(']')
        if start<0: raise ValueError('分钟接口未返回有效数据')
        raw=json.loads(s[start:end+1]);t=now()
        return [{'stamp':r['day'],'open':num(r['open']),'close':num(r['close']),'high':num(r['high']),
                 'low':num(r['low']),'volume':num(r['volume']),'amount':num(r['amount']) if 'amount' in r else None,
                 'source':'新浪','source_url':url,'observed':t.isoformat(),'complete':bar_complete(r['day'],t)} for r in raw]

    async def node(self,node):
        url=SINA_NODE+'getHQNodeStockCount?node='+node
        count=int(json.loads(await self.text(url)))
        async def page(p):
            u=SINA_NODE+f'getHQNodeData?page={p}&num=80&sort=symbol&asc=1&node={node}&symbol='
            v=json.loads(await self.text(u));
            if not isinstance(v,list): raise ValueError('股票列表响应无效')
            for r in v:
                for k in ('trade','pricechange','changepercent','buy','sell','settlement','open','high','low','volume','amount','turnoverratio'):
                    if k in r: r[k]=num(r[k])
            return v
        pages=await asyncio.gather(*(page(p) for p in range(1,(count+79)//80+1)))
        rows=[r for page in pages for r in page]
        # A truncated market response must not silently replace the universe.
        if count and len(rows)<count*.98: raise ValueError('股票列表覆盖不足')
        return rows,url

    async def industries(self):
        url='https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php'
        s=await self.text(url,'gb18030')
        payload=json.loads(s[s.find('{'):s.rfind('}')+1])
        return [{'node':k,'name':v.split(',')[1],'source_url':url} for k,v in payload.items()]

    async def announcements(self,code):
        url=f'https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=30&page_index=1&ann_type=A&stock_list={code}'
        try:
            doc=json.loads(await self.text(url));rows=doc['data']['list']
            return [{'id':r['art_code'],'title':r['title'],'published_at':r['notice_date'],
                     'source_url':'https://data.eastmoney.com/notices/detail/'+code+'/'+r['art_code']+'.html',
                     'observed':now().isoformat(),'reviewed':False,'source':'东方财富'} for r in rows]
        except (httpx.HTTPError,ValueError,TypeError,KeyError):
            # Alternate public company-disclosure list. No WAF/captcha bypass.
            fallback='https://money.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/'+code+'.phtml'
            s=await self.text(fallback,'gb18030')
            rows=[]
            for day,href,title in re.findall(r'(\d{4}-\d{2}-\d{2})&nbsp;\s*<a[^>]*href=[\x27\"]([^\x27\"]*vCB_AllBulletinDetail[^\x27\"]*)[\x27\"][^>]*>(.*?)</a>',s,re.S):
                id=re.search(r'[?&]id=(\d+)',href)
                if id: rows.append({'id':'sina:'+id[1],'title':html.unescape(re.sub('<[^>]+>','',title)),
                     'published_at':day+'T00:00:00+08:00','source_url':'https://money.finance.sina.com.cn'+html.unescape(href),
                     'observed':now().isoformat(),'reviewed':False,'source':'新浪公司公告列表'})
            if not rows: raise ValueError('两个公告来源均未返回有效列表')
            return rows
