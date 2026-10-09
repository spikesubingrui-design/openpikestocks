import asyncio
import httpx
from ashare.providers import Providers

def test_sina_universe_mixed_numeric_types_are_normalized():
    async def run():
        p=Providers();await p.client.aclose()
        def handler(request):
            if 'StockCount' in str(request.url): return httpx.Response(200,json='1')
            return httpx.Response(200,json=[{'code':'002675','name':'东诚药业','amount':'1000000','changepercent':'7.68','volume':'1000'}])
        p.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rows,_=await p.node('hs_a')
        assert rows[0]['changepercent']==7.68 and rows[0]['amount']==1000000.
        await p.client.aclose()
    asyncio.run(run())

def test_announcement_primary_failure_uses_public_fallback():
    async def run():
        p=Providers();await p.client.aclose()
        def handler(request):
            if 'eastmoney' in str(request.url): return httpx.Response(567,text='blocked')
            html="2026-10-09&nbsp;<a target='_blank' href='/corp/view/vCB_AllBulletinDetail.php?stockid=002675&id=12633289'>东诚药业：关于回购公司股份进展情况的公告</a><br>"
            return httpx.Response(200,content=html.encode('gb18030'))
        p.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rows=await p.announcements('002675')
        assert rows[0]['id']=='sina:12633289' and not rows[0]['reviewed']
        assert rows[0]['source_url'].startswith('https://money.finance.sina.com.cn/')
        await p.client.aclose()
    asyncio.run(run())
