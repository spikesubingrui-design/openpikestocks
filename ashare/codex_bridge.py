"""Private stdio client. No raw JSON-RPC is exposed to the browser."""
import asyncio
import json
import shutil
from pathlib import Path


class CodexError(RuntimeError):
    pass


class CodexBridge:
    def __init__(self, directory, events, tools):
        self.directory = Path(directory)
        self.events, self.tools = events, tools
        self.proc = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.tool_jobs = set()
        self.account_ready = False

    async def start(self):
        async with self.start_lock:
            if self.proc and self.proc.returncode is None:
                return
            executable = shutil.which('codex') or str(Path.home()/'.local/bin/codex')
            if not Path(executable).is_file():
                raise CodexError('本机未安装 Codex，无法连接顾问。')
            self.directory.mkdir(parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            args = [executable, 'app-server', '--listen', 'stdio://']
            # Process-local overrides; never edit the user's Codex configuration.
            settings = {'mcp_servers': '{}', 'project_doc_max_bytes': '0',
                        'web_search': '"live"', 'approval_policy': '"never"',
                        'sandbox_mode': '"read-only"'}
            disabled = ('shell_tool', 'unified_exec', 'code_mode', 'code_mode_only',
                        'code_mode_host', 'apps', 'plugins', 'remote_plugin',
                        'browser_use', 'browser_use_external', 'in_app_browser',
                        'computer_use', 'image_generation', 'view_image',
                        'multi_agent', 'hooks', 'chronicle', 'goals')
            settings.update({'features.'+key: 'false' for key in disabled})
            settings['features.skip_host_skill_discovery'] = 'true'
            for key, value in settings.items():
                args += ['-c', key+'='+value]
            self.proc = await asyncio.create_subprocess_exec(*args, cwd=self.directory,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4*1024*1024)
            self.reader = asyncio.create_task(self._read())
            try:
                await self.call('initialize', {'clientInfo': {'name': 'openpike_stocks',
                    'title': 'OpenPike Stocks private advisor', 'version': '1.0.0'},
                    'capabilities': {'experimentalApi': True}})
                await self.send({'method': 'initialized', 'params': {}})
                result = await self.call('account/read', {'refreshToken': False})
                self.account_ready = result.get('account') is not None
                if not self.account_ready:
                    raise CodexError('请先在本机 Codex 登录；顾问不会读取或索取你的密码。')
            except Exception:
                await self.close()
                raise

    async def send(self, message):
        async with self.write_lock:
            if not self.proc or self.proc.returncode is not None:
                raise CodexError('Codex 连接中断，请稍后重新发送。')
            self.proc.stdin.write((json.dumps(message, ensure_ascii=False)+'\n').encode())
            await self.proc.stdin.drain()

    async def call(self, method, params, timeout=40):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({'id': identifier, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise CodexError('Codex 连接超时；本次请求没有自动重发。')
        finally:
            self.pending.pop(identifier, None)

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if 'method' in message:
                    if 'id' in message:
                        job = asyncio.create_task(self._request(message))
                        self.tool_jobs.add(job); job.add_done_callback(self.tool_jobs.discard)
                    else:
                        await self.events(message['method'], message.get('params', {}))
                elif message.get('id') in self.pending:
                    future = self.pending[message['id']]
                    if not future.done():
                        if 'error' in message:
                            future.set_exception(CodexError('Codex 未能完成请求，请检查本机登录、额度或连接后重试。'))
                        else:
                            future.set_result(message.get('result', {}))
        finally:
            self.account_ready = False
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(CodexError('Codex 连接已关闭。'))
            await self.events('bridge/closed', {})

    async def _request(self, message):
        method = message['method']
        if method == 'item/tool/call':
            try:
                value = await self.tools(message.get('params', {}))
                result = {'success': True, 'contentItems': [{'type': 'inputText',
                          'text': json.dumps(value, ensure_ascii=False, allow_nan=False)}]}
            except Exception:
                result = {'success': False, 'contentItems': [{'type': 'inputText',
                          'text': '此操作未通过白名单或数据校验；没有执行修改。'}]}
        elif method.endswith('requestApproval'):
            result = {'decision': 'cancel'}
        else:
            await self.send({'id': message['id'], 'error': {'code': -32601,
                             'message': 'Private advisor does not expose this operation'}})
            return
        await self.send({'id': message['id'], 'result': result})

    async def close(self):
        proc, reader = self.proc, self.reader
        self.proc = None; self.reader = None; self.account_ready = False
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill(); await proc.wait()
        if reader and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        for job in list(self.tool_jobs):
            job.cancel()
        await asyncio.gather(*self.tool_jobs, return_exceptions=True)
