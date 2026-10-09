"""Outbound-only WSS bridge. Never exposes a Mac listening port publicly."""
import asyncio
import base64
import json
import logging
import signal
import httpx
import uvicorn
from .config import DATA
from .remote import Access,DOMAIN,create_gateway,BODY_LIMIT,RESPONSE_LIMIT
from .cloud_relay import safe_path

log=logging.getLogger('openpike.remote')

async def forward(message,client,token):
    rid=message.get('id')
    path=message.get('path','');method=message.get('method')
    if not safe_path(path) or method not in ('GET','HEAD','POST'):
        return {'id':rid,'status':400,'headers':{},'body':''}
    try:
        body=base64.b64decode(message.get('body',''),validate=True)
        if len(body)>BODY_LIMIT:raise ValueError('too large')
        query=message.get('query','')
        if len(query)>8192 or any(c in query for c in ('\r','\n','#')):raise ValueError('query')
        headers={k:v for k,v in message.get('headers',{}).items() if k in ('cookie','origin','content-type')}
        headers.update({'host':DOMAIN,'x-openpike-relay':token})
        result=await client.request(method,path+('?' + query if query else ''),headers=headers,content=body)
        if len(result.content)>RESPONSE_LIMIT:raise ValueError('response too large')
        allowed=('content-type','set-cookie','location','cache-control','content-security-policy','x-content-type-options','referrer-policy')
        return {'id':rid,'status':result.status_code,'headers':{k:v for k,v in result.headers.items() if k in allowed},
                'body':base64.b64encode(result.content).decode()}
    except (ValueError,httpx.RequestError):
        return {'id':rid,'status':503,'headers':{'content-type':'application/json'},
                'body':base64.b64encode(json.dumps({'detail':'主机请求未完成，请核对状态后重试'},ensure_ascii=False).encode()).decode()}

async def connect_loop(access):
    from websockets.asyncio.client import connect
    delay=5
    while True:
        if not access.get('relay_enabled'):
            await asyncio.sleep(15);continue
        tasks=set();send_lock=asyncio.Lock()
        try:
            async with connect('wss://'+DOMAIN+'/_bridge',additional_headers={'Authorization':'Bearer '+access.get('relay_token')},
                               max_size=12*1024*1024,ping_interval=20,ping_timeout=20,open_timeout=10,proxy=None) as ws:
                access.set('bridge_connected',True);delay=5
                async with httpx.AsyncClient(base_url='http://127.0.0.1:8766',trust_env=False,timeout=22) as client:
                    async def reply(message):
                        result=await forward(message,client,access.get('relay_token'))
                        async with send_lock:await ws.send(json.dumps(result))
                    async for raw in ws:
                        message=json.loads(raw)
                        if not isinstance(message,dict):continue
                        if len(tasks)>=24:raise ValueError('queue capacity exceeded')
                        task=asyncio.create_task(reply(message));tasks.add(task);task.add_done_callback(tasks.discard)
        except asyncio.CancelledError:raise
        except Exception:
            log.warning('Remote bridge unavailable; stock collection continues')
        finally:
            access.set('bridge_connected',False)
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        await asyncio.sleep(delay);delay=min(delay*2,60)

async def run():
    app=create_gateway();access=app.state.access
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8766,access_log=False,log_level='warning'))
    connector=asyncio.create_task(connect_loop(access))
    try:await server.serve()
    finally:
        connector.cancel();await asyncio.gather(connector,return_exceptions=True)

if __name__=='__main__':asyncio.run(run())
