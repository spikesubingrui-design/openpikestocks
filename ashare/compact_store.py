"""Offline, backed-up lossless migration of the stock database only."""
import gzip
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from .config import DATA
from .store import Store,dumps

def compact(path):
    path=path.resolve();s=Store(path)
    s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');s.db.close()
    backup=path.with_name('pre-compression-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3.gz')
    with path.open('rb') as src,gzip.open(backup,'wb') as dst:
        while block:=src.read(1024*1024): dst.write(block)
    with path.open('rb') as src: original=hashlib.file_digest(src,'sha256').hexdigest()
    with gzip.open(backup,'rb') as stream:
        assert hashlib.file_digest(stream,'sha256').hexdigest()==original,'备份核验失败'
    s=Store(path);before=[]
    pairs=s.db.execute('SELECT DISTINCT code,frame FROM bars').fetchall()
    for code,frame in pairs:
        rows=s.bars(code,frame);digest=hashlib.sha256(dumps(rows).encode()).hexdigest()
        s.save_bars(code,frame,rows)
        assert hashlib.sha256(dumps(s.bars(code,frame)).encode()).hexdigest()==digest
        before.append({'code':code,'frame':frame,'count':len(rows),'sha256':digest})
    s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    compacted=path.with_suffix('.compact.sqlite3')
    s.db.execute('VACUUM INTO ?', (str(compacted),));s.db.close()
    c=sqlite3.connect(compacted);assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok';c.close()
    check=Store(compacted)
    for item in before:
        rows=check.bars(item['code'],item['frame'])
        assert len(rows)==item['count'] and hashlib.sha256(dumps(rows).encode()).hexdigest()==item['sha256']
    check.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');check.db.close()
    os.replace(compacted,path)
    report={'backup':str(backup),'backup_sha256_uncompressed':original,'verified_series':len(before),
            'verified_bars':sum(r['count'] for r in before),'new_bytes':path.stat().st_size,'series':before}
    path.with_name('compression-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='series'},ensure_ascii=False))

if __name__=='__main__':
    # Invoke only after stopping local.ashare.monitor.
    compact(DATA/'monitor.sqlite3')
