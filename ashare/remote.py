"""Private remote access. The stock service remains bound to loopback."""
import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from urllib.parse import unquote
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from .config import DATA

DOMAIN = 'stocks.example.com'
ORIGIN = 'https://' + DOMAIN
COOKIE = '__Host-openpike_session'
SESSION_SECONDS = 12 * 3600
STATIC = Path(__file__).parent / 'static'
BODY_LIMIT = 256 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024


class Access:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.executescript('''
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS failures(at REAL NOT NULL);
        ''')
        for key in ('relay_token', 'setup_token'):
            if self.get(key) is None:
                self.set(key, secrets.token_urlsafe(32))

    def get(self, key):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def status(self):
        return {'domain': DOMAIN, 'url': ORIGIN, 'password_configured': bool(self.get('password')),
                'session_hours': 12, 'cloud_status': self.get('cloud_status') or 'PENDING_DEPLOYMENT',
                'bridge_connected': bool(self.get('bridge_connected'))}

    def configure(self, password, setup_token):
        if not isinstance(setup_token, str) or not hmac.compare_digest(setup_token, self.get('setup_token')):
            raise HTTPException(403, '设置页面已过期，请重新打开')
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise HTTPException(422, '密码须为12至256个字符')
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64*1024*1024)
        with self.lock, self.db:
            if not hmac.compare_digest(setup_token,self.get('setup_token')):
                raise HTTPException(403,'设置页面已过期，请重新打开')
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                            ('password', json.dumps({'salt': salt.hex(), 'digest': digest.hex()})))
            self.db.execute('DELETE FROM sessions')
            self.db.execute('DELETE FROM failures')
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                            ('setup_token', json.dumps(secrets.token_urlsafe(32))))
        return self.status()

    def login(self, password, clock=None):
        clock = time.time() if clock is None else clock
        # One lock covers checking and recording failures; concurrent guesses
        # cannot race past the global personal-account throttle.
        with self.lock, self.db:
            self.db.execute('DELETE FROM failures WHERE at < ?', (clock - 300,))
            if self.db.execute('SELECT COUNT(*) FROM failures').fetchone()[0] >= 5:
                raise HTTPException(429, '尝试过于频繁，请5分钟后重试')
            saved = self.get('password')
            if not saved:
                raise HTTPException(503, '请先在主机软件的“远程访问”页面设置密码')
            valid = False
            if isinstance(password, str) and len(password) <= 256:
                digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(saved['salt']),
                                        n=32768, r=8, p=1, maxmem=64*1024*1024)
                valid = hmac.compare_digest(digest.hex(), saved['digest'])
            if not valid:
                self.db.execute('INSERT INTO failures VALUES (?)', (clock,))
                # Commit before raising: failed guesses must survive restarts.
                self.db.commit()
                raise HTTPException(401, '密码不正确，请重新输入')
            token = secrets.token_urlsafe(32)
            self.db.execute('DELETE FROM sessions WHERE expires <= ?', (clock,))
            self.db.execute('INSERT INTO sessions VALUES (?,?)',
                            (hashlib.sha256(token.encode()).hexdigest(), clock + SESSION_SECONDS))
            self.db.execute('DELETE FROM failures')
            return token

    def valid(self, token, clock=None):
        if not token or len(token) > 128:
            return False
        clock = time.time() if clock is None else clock
        with self.lock:
            row = self.db.execute('SELECT expires FROM sessions WHERE token_hash=?',
                                  (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return bool(row and row[0] > clock)

    def logout(self, token):
        with self.lock, self.db:
            self.db.execute('DELETE FROM sessions WHERE token_hash=?',
                            (hashlib.sha256((token or '').encode()).hexdigest(),))


def create_gateway(path=None, transport=None):
    access = Access(path or DATA/'remote/access.sqlite3')

    @asynccontextmanager
    async def life(app):
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8765', transport=transport,
                                     trust_env=False, follow_redirects=False, timeout=20) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=life, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.access = access

    @app.middleware('http')
    async def guard(request, call_next):
        token = request.headers.get('x-openpike-relay', '')
        if request.url.hostname != DOMAIN or not hmac.compare_digest(token, access.get('relay_token')):
            return JSONResponse({'detail': '拒绝非授权转发'}, status_code=403)
        if request.method not in ('GET', 'HEAD') and request.headers.get('origin') != ORIGIN:
            return JSONResponse({'detail': '拒绝跨站修改'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/login')
    def login_page():
        return FileResponse(STATIC/'remote-login.html')

    @app.get('/auth/assets/{asset}')
    def asset(asset: str):
        files = {'remote.css': STATIC/'remote.css', 'remote-login.js': STATIC/'remote-login.js',
                 'remote-ui.js': STATIC/'remote-ui.js',
                 'logo.png': STATIC/'brand/openpike-stocks-logo.png'}
        if asset not in files:
            raise HTTPException(404)
        return FileResponse(files[asset])

    @app.post('/auth/login')
    async def login(request: Request):
        body = await limited_body(request)
        try:
            data = json.loads(body)
            password = data.get('password') if isinstance(data, dict) else None
        except (ValueError, UnicodeError):
            raise HTTPException(422, '登录信息格式不正确')
        token = await asyncio.to_thread(access.login, password)
        response = JSONResponse({'logged_in': True, 'expires_in': SESSION_SECONDS})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True,
                            httponly=True, samesite='strict', path='/')
        return response

    @app.post('/auth/logout')
    def logout(request: Request):
        access.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'logged_out': True})
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return response

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST'])
    async def proxy(path: str, request: Request):
        decoded=unquote(path)
        if '\\' in decoded or '%' in decoded or any(p in ('.','..') for p in decoded.split('/')) or decoded.startswith('/'):
            raise HTTPException(400,'请求路径无效')
        # Setup and notification-consumer control are local-only, even after
        # login. Raw API docs and absolute workspace paths aren't public setup.
        if path.startswith(('api/remote/', 'remote-access', '_bridge')):
            raise HTTPException(403, '此功能只能在主机软件使用')
        if path.startswith('api/events/') or path in ('openapi.json', 'docs', 'redoc'):
            raise HTTPException(403, '此接口仅供本机服务使用')
        if not access.valid(request.cookies.get(COOKIE)):
            if path.startswith('api/'):
                return JSONResponse({'detail': '请先登录', 'code': 'LOGIN_REQUIRED'}, status_code=401)
            return RedirectResponse('/login', status_code=303)
        body = await limited_body(request)
        headers = {'origin': 'http://127.0.0.1:8765'}
        if 'content-type' in request.headers:
            headers['content-type'] = request.headers['content-type']
        try:
            upstream = await app.state.client.request(request.method, '/' + path,
                params=request.query_params.multi_items(), headers=headers, content=body)
        except httpx.RequestError:
            raise HTTPException(503, '主机行情服务暂时无法连接，请稍后重试')
        if len(upstream.content) > RESPONSE_LIMIT:
            raise HTTPException(413, '内容较大，请在主机软件中查看')
        content = upstream.content
        if path == '' and upstream.status_code == 200:
            content=content.replace(b'<head>',b'<head><script defer src="/auth/assets/remote-ui.js"></script>')
            content=content.replace(b'<a class="remote-settings" href="/remote-access">'+ '远程访问'.encode()+b'</a>',
                                    b'<button id="remote-logout" type="button">'+ '退出登录'.encode()+b'</button>')
        if path == 'api/health' and upstream.status_code == 200:
            data = upstream.json();data.pop('workspace_root', None)
            content = json.dumps(data, ensure_ascii=False).encode()
        return Response(content, status_code=upstream.status_code,
                        headers={'Content-Type': upstream.headers.get('content-type', 'application/octet-stream')})
    return app


async def limited_body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BODY_LIMIT:
            raise HTTPException(413, '提交内容过大')
    return bytes(body)
