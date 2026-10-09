"""Manage only the outbound stock bridge LaunchAgent."""
import argparse
import json
import os
import plistlib
import socket
import subprocess
from pathlib import Path
from .config import ROOT,DATA
from .remote import Access

LABEL='local.ashare.remote'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'

def install():
    arguments=[str(DATA/'venv/bin/python'),'-m','ashare.remote_client']
    target=f'gui/{os.getuid()}/{LABEL}'
    if subprocess.run(['launchctl','print',target],capture_output=True).returncode==0:
        if not PLIST.exists():raise RuntimeError('同名远程服务来源未知，未替换')
        old=plistlib.loads(PLIST.read_bytes())
        if old.get('WorkingDirectory')!=str(ROOT) or old.get('ProgramArguments')!=arguments:
            raise RuntimeError('同名远程服务不属于当前股票工程，未替换')
        return {'installed':True,'already_running':True}
    if not Path(arguments[0]).is_file():raise RuntimeError('股票独立依赖环境不可用')
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try:s.bind(('127.0.0.1',8766))
        except OSError:raise RuntimeError('端口8766已被占用，未替换其他服务')
    directory=DATA/'remote';directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
    Access(directory/'access.sqlite3')
    body={'Label':LABEL,'ProgramArguments':arguments,'WorkingDirectory':str(ROOT),
          'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,'Umask':63,
          'StandardOutPath':str(directory/'bridge.log'),'StandardErrorPath':str(directory/'bridge.error.log'),
          'EnvironmentVariables':{'PYTHONUNBUFFERED':'1'}}
    PLIST.parent.mkdir(parents=True,exist_ok=True);PLIST.write_bytes(plistlib.dumps(body));PLIST.chmod(0o600)
    result=subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(PLIST)],capture_output=True,text=True)
    if result.returncode:raise RuntimeError('远程服务启动失败；行情服务未受影响')
    return {'installed':True,'outbound_enabled':False,'domain':'stocks.example.com'}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','status']);args=parser.parse_args()
    result=install() if args.action=='install' else Access(DATA/'remote/access.sqlite3').status()
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
