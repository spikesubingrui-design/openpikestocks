"""Stateless cloud entry; stock data, password hashes and sessions stay on Mac."""
import asyncio
import base64
import hmac
import os
import secrets
from urllib.parse import unquote
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

DOMAIN='stocks.example.com'
MAX_BODY=256*1024
MAX_RESPONSE=8*1024*1024

def safe_path(path):
    value=unquote(path)
    return value.startswith('/') and not value.startswith('//') and '\\' not in value and '%' not in value and all(p not in ('.','..') for p in value.split('/'))

def create_relay(token=None):
    token=token or os.environ.get('OPENPIKE_RELAY_TOKEN','')
    if len(token)<32: raise RuntimeError('Relay credential is not configured')
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.bridge=None;app.state.pending={};app.state.send_lock=asyncio.Lock();app.state.bridge_lock=asyncio.Lock()

    @app.get('/_health')
    def health(): return {'service':'OpenPike Stocks relay','bridge_connected':app.state.bridge is not None}

    @app.websocket('/_bridge')
    async def bridge(ws:WebSocket):
        if not hmac.compare_digest(ws.headers.get('authorization',''),'Bearer '+token):
            await ws.close(code=1008);return
        async with app.state.bridge_lock:
            if app.state.bridge is not None:
                await ws.close(code=1013);return
            await ws.accept();app.state.bridge=ws
        try:
            while True:
                data=await ws.receive_json()
                if not isinstance(data,dict):continue
                future=app.state.pending.get(data.get('id'))
                if future is not None and not future.done():future.set_result(data)
        except (WebSocketDisconnect,ValueError): pass
        finally:
            if app.state.bridge is ws:app.state.bridge=None
            for future in tuple(app.state.pending.values()):
                if not future.done():future.set_exception(ConnectionError('Host disconnected'))

    @app.api_route('/{path:path}',methods=['GET','HEAD','POST'])
    async def proxy(path:str,request:Request):
        if request.url.hostname!=DOMAIN:raise HTTPException(400,'访问域名不正确')
        if not safe_path('/'+path):raise HTTPException(400,'请求路径无效')
        if app.state.bridge is None:
            return JSONResponse({'detail':'主机尚未连接，请确认电脑开机和远程转发配置','code':'HOST_OFFLINE'},status_code=503,headers={'Cache-Control':'no-store'})
        if len(app.state.pending)>=24:raise HTTPException(429,'请求较多，请稍后重试')
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>MAX_BODY:raise HTTPException(413,'提交内容过大')
        rid=secrets.token_hex(16);future=asyncio.get_running_loop().create_future()
        app.state.pending[rid]=future
        headers={k:request.headers[k] for k in ('cookie','origin','content-type') if k in request.headers}
        message={'id':rid,'method':request.method,'path':'/'+path,'query':request.url.query,
                 'headers':headers,'body':base64.b64encode(body).decode()}
        try:
            async with app.state.send_lock:
                await app.state.bridge.send_json(message)
            data=await asyncio.wait_for(future,25)
            content=base64.b64decode(data.get('body',''),validate=True)
            if len(content)>MAX_RESPONSE:raise HTTPException(413,'内容较大，请在主机查看')
            allowed={'content-type','set-cookie','location','cache-control','content-security-policy','x-content-type-options','referrer-policy'}
            reply_headers={k:v for k,v in data.get('headers',{}).items() if k.lower() in allowed}
            reply_headers['Cache-Control']='no-store'
            return Response(content,status_code=int(data['status']),headers=reply_headers)
        except asyncio.TimeoutError:
            raise HTTPException(504,'主机响应超时；登记操作请先核对结果，不要重复提交')
        except (ConnectionError,RuntimeError,AttributeError):
            raise HTTPException(503,'主机连接中断，请稍后重试')
        finally:app.state.pending.pop(rid,None)
    return app
