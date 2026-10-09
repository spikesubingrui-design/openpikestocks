import json
import asyncio
import base64
import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from ashare.remote import Access,create_gateway,DOMAIN,ORIGIN,COOKIE
from ashare.cloud_relay import create_relay,safe_path
from ashare.remote_client import forward
from ashare.app import create_app

PASSWORD='fixture-only-password-12345'

def test_password_and_sessions_survive_restart_and_rotation(tmp_path):
    path=tmp_path/'access.db';access=Access(path)
    with pytest.raises(HTTPException):access.configure(PASSWORD,'bad-token')
    access.configure(PASSWORD,access.get('setup_token'))
    token=access.login(PASSWORD,clock=100)
    assert access.valid(token,clock=101)
    restored=Access(path)
    assert restored.valid(token,clock=101)
    assert not restored.valid(token,clock=100+12*3600)
    restored.configure(PASSWORD+'new',restored.get('setup_token'))
    assert not restored.valid(token,clock=102)
    assert PASSWORD not in path.read_bytes().decode(errors='ignore')

def test_login_throttle_persists_failures(tmp_path):
    path=tmp_path/'auth.db';access=Access(path);access.configure(PASSWORD,access.get('setup_token'))
    for _ in range(5):
        with pytest.raises(HTTPException) as e:access.login('wrong',clock=100)
        assert e.value.status_code==401
    with pytest.raises(HTTPException) as e:Access(path).login(PASSWORD,clock=101)
    assert e.value.status_code==429
    assert Access(path).login(PASSWORD,clock=401)

def test_setup_nonce_cannot_be_reused_concurrently(tmp_path):
    import threading,concurrent.futures
    access=Access(tmp_path/'concurrent.db');token=access.get('setup_token');barrier=threading.Barrier(2)
    def attempt(suffix):
        barrier.wait()
        try:access.configure(PASSWORD+suffix,token);return 200
        except HTTPException as error:return error.status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt,['one','two']))==[200,403]

def test_gateway_auth_csrf_cookie_local_setup_and_proxy(tmp_path):
    calls=[]
    def upstream(request):
        calls.append(request)
        assert request.url.host=='127.0.0.1'
        assert 'cookie' not in request.headers and 'x-openpike-relay' not in request.headers
        return httpx.Response(200,json={'workspace_root':'private/path','value':42})
    app=create_gateway(tmp_path/'gateway.db',transport=httpx.MockTransport(upstream));access=app.state.access
    headers={'host':DOMAIN,'x-openpike-relay':access.get('relay_token')}
    access.configure(PASSWORD,access.get('setup_token'))
    with TestClient(app,base_url=ORIGIN,headers=headers) as c:
        assert c.get('/api/holdings').status_code==401 and not calls
        assert c.get('/',follow_redirects=False).status_code==303
        assert c.post('/auth/login',json={'password':PASSWORD}).status_code==403
        assert c.post('/auth/login',headers={'origin':'https://evil.invalid'},json={'password':PASSWORD}).status_code==403
        response=c.post('/auth/login',headers={'origin':ORIGIN},json={'password':PASSWORD})
        assert response.status_code==200
        cookie=response.headers['set-cookie'];assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
        assert c.get('/api/holdings').status_code==200 and len(calls)==1
        assert 'workspace_root' not in c.get('/api/health').json()
        assert c.get('/api/remote/status').status_code==403
        assert c.get('/static/%2e%2e/api/remote/status').status_code==400
        assert c.post('/api/trades',json={}).status_code==403
        assert c.post('/auth/logout',headers={'origin':ORIGIN}).status_code==200
        assert c.get('/api/holdings').status_code==401
        assert c.get('/login',headers={'x-openpike-relay':'bad'}).status_code==403

def test_local_setup_requires_origin_and_one_use_nonce(tmp_path):
    app=create_app(tmp_path/'monitor.db',collect=False)
    with TestClient(app) as c:
        status=c.get('/api/remote/status').json()
        assert not status['password_configured']
        body={'password':PASSWORD,'setup_token':status['setup_token']}
        assert c.post('/api/remote/password',json=body).status_code==403
        assert c.post('/api/remote/password',headers={'origin':'http://127.0.0.1:8765'},json=body).status_code==200
        assert c.post('/api/remote/password',headers={'origin':'http://127.0.0.1:8765'},json=body).status_code==403
        assert c.get('/remote-access').status_code==200

@pytest.mark.parametrize('path',['//evil.invalid','/a/../api/remote/status','/%252e%252e/x','/a\\b','https://evil.invalid'])
def test_relay_rejects_non_relative_or_traversal_paths(path):assert not safe_path(path)

def test_relay_offline_and_bad_bridge_never_return_stock_data():
    with TestClient(create_relay('x'*40),base_url=ORIGIN) as c:
        result=c.get('/api/holdings');assert result.status_code==503 and result.json()['code']=='HOST_OFFLINE'
        assert not c.get('/_health').json()['bridge_connected']
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/_bridge'):pass

def test_bridge_can_forward_and_disconnect_resets_state():
    app=create_relay('x'*40)
    with TestClient(app,base_url=ORIGIN) as c:
        with c.websocket_connect('/_bridge',headers={'authorization':'Bearer '+'x'*40}) as ws:
            assert c.get('/_health').json()['bridge_connected']
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future=pool.submit(c.get,'/login')
                request=ws.receive_json();assert request['path']=='/login'
                ws.send_json({'id':request['id'],'status':200,'headers':{'content-type':'text/html'},'body':base64.b64encode(b'login page').decode()})
                assert future.result(timeout=3).text=='login page'
        assert not c.get('/_health').json()['bridge_connected']

def test_outbound_forward_never_contacts_arbitrary_host():
    seen=[]
    def handler(request):seen.append(request);return httpx.Response(200,text='ok')
    async def exercise():
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8766',transport=httpx.MockTransport(handler)) as client:
            bad=await forward({'id':'1','path':'//evil.invalid','method':'GET'},client,'token')
            assert bad['status']==400 and not seen
            good=await forward({'id':'2','path':'/api/holdings','method':'GET','headers':{'authorization':'untrusted'}},client,'token')
            assert good['status']==200 and seen[0].url.host=='127.0.0.1'
            assert 'authorization' not in seen[0].headers
    asyncio.run(exercise())


def test_remote_embedded_advisor_retains_auth_and_scoped_framing(tmp_path):
    app=create_gateway(tmp_path/'framing.db',transport=httpx.MockTransport(lambda request:httpx.Response(200,text='advisor')))
    access=app.state.access
    access.configure(PASSWORD,access.get('setup_token'))
    headers={'host':DOMAIN,'x-openpike-relay':access.get('relay_token')}
    with TestClient(app,base_url=ORIGIN,headers=headers) as client:
        assert client.get('/advisor?embedded=1',follow_redirects=False).status_code==303
        assert client.post('/auth/login',headers={'origin':ORIGIN},json={'password':PASSWORD}).status_code==200
        assert "frame-ancestors 'self'" in client.get('/advisor?embedded=1').headers['content-security-policy']
        assert "frame-ancestors 'none'" in client.get('/advisor').headers['content-security-policy']
        assert "frame-ancestors 'none'" in client.get('/login?embedded=1').headers['content-security-policy']
