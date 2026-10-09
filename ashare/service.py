"""Manage only the independent A-share LaunchAgent."""
import argparse
import json
import os
import plistlib
import socket
import subprocess
from urllib.request import build_opener,ProxyHandler
from pathlib import Path
from .config import ROOT,DATA,PORT

LABEL='local.ashare.monitor'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'

def command(args):
    r=subprocess.run(args,text=True,capture_output=True)
    if r.returncode: raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout

def install():
    exe=DATA/'venv/bin/python'
    if not exe.exists(): raise RuntimeError('股票独立依赖环境尚未安装')
    target=f'gui/{os.getuid()}/{LABEL}'
    loaded=subprocess.run(['launchctl','print',target],capture_output=True).returncode==0
    if loaded:
        if not PLIST.exists() or plistlib.loads(PLIST.read_bytes()).get('WorkingDirectory')!=str(ROOT):
            raise RuntimeError('已有同名服务不属于本工程，停止安装')
        return {'installed':True,'already_running':True,'url':f'http://127.0.0.1:{PORT}'}
    with socket.socket() as s:
        # Match uvicorn's reuse behavior: recent closed connections must not be
        # mistaken for another listening service during an immediate restart.
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try: s.bind(('127.0.0.1',PORT))
        except OSError: raise RuntimeError('端口8765已被占用，未替换或停止任何服务')
    DATA.mkdir(parents=True,exist_ok=True);PLIST.parent.mkdir(parents=True,exist_ok=True)
    body={'Label':LABEL,'ProgramArguments':[str(exe),'-m','uvicorn','ashare.app:app','--host','127.0.0.1','--port',str(PORT),'--no-access-log'],
          'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
          'StandardOutPath':str(DATA/'service.log'),'StandardErrorPath':str(DATA/'service.error.log'),
          'EnvironmentVariables':{'PYTHONUNBUFFERED':'1'}}
    PLIST.write_bytes(plistlib.dumps(body));PLIST.chmod(0o600)
    command(['launchctl','bootstrap',f'gui/{os.getuid()}',str(PLIST)])
    return {'installed':True,'url':f'http://127.0.0.1:{PORT}','plist':str(PLIST)}

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['install','status','stop','restart']);args=p.parse_args()
    if args.action=='install': result=install()
    elif args.action=='stop':
        command(['launchctl','bootout',f'gui/{os.getuid()}/{LABEL}']);result={'stopped':True}
    elif args.action=='restart':
        command(['launchctl','kickstart','-k',f'gui/{os.getuid()}/{LABEL}']);result={'restarted':True}
    else:
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{PORT}/api/health',timeout=10) as r: result=json.load(r)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
