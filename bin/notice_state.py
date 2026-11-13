#!/usr/bin/env python3
"""Durable report outbox; enqueue and report progress share one SQLite transaction."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import sys

KEYS={'dpkg.cursor','remaining-apt.digest'}
KINDS={'os-installed','os-failed','remaining-updates'}

@contextmanager
def database(directory):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('State directory must not be a symlink')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if directory.stat().st_mode&0o077:raise ValueError('State directory must be private')
    path=directory/'report-outbox.sqlite'
    if path.is_symlink():raise ValueError('Outbox must not be a symlink')
    con=sqlite3.connect(path,timeout=2);os.chmod(path,0o600)
    try:
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        con.execute('''CREATE TABLE IF NOT EXISTS notices (
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,reference TEXT NOT NULL,message TEXT NOT NULL,
          created REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',lease REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,error TEXT)''');con.commit()
        with con:yield con
    finally:con.close()


def progress(directory,key):
    if key not in KEYS:raise ValueError('Unknown progress key')
    with database(directory) as con:row=con.execute('SELECT value FROM progress WHERE key=?',(key,)).fetchone()
    if row:return row[0]
    # Read the former flat-file state once as a migration baseline; the database
    # takes precedence after the first committed progress update.
    legacy=Path(directory)/key
    if legacy.is_symlink():raise ValueError('Legacy progress must not be a symlink')
    return legacy.read_text().strip() if legacy.is_file() else ''


def record(directory,kind,reference,key,value,message):
    if key not in KEYS|{'-'} or not isinstance(value,str) or len(value)>200:raise ValueError('Invalid report progress')
    if key=='dpkg.cursor' and not re.fullmatch(r'[0-9]+:[0-9]+',value):raise ValueError('Invalid package cursor')
    if key=='remaining-apt.digest' and not re.fullmatch(r'[a-f0-9]{64}',value):raise ValueError('Invalid update-list digest')
    if kind!='-' and (kind not in KINDS or not re.fullmatch(r'[A-Za-z0-9:_-]{1,200}',reference) or not message or len(message.encode())>200000):
        raise ValueError('Invalid report notice')
    with database(directory) as con:
        if kind!='-':
            ident=hashlib.sha256((kind+':'+reference).encode()).hexdigest()
            con.execute('INSERT OR IGNORE INTO notices(id,kind,reference,message,created) VALUES(?,?,?,?,?)',(ident,kind,reference,message,time.time()))
        if key!='-':con.execute('INSERT INTO progress(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))


def capture(directory,log):
    log=Path(log)
    if not log.is_file():raise ValueError('Package log unavailable; report cursor retained')
    with log.open('rb') as f:
        inode=os.fstat(f.fileno()).st_ino;raw=f.read(16000001)
    if len(raw)>16000000:raise ValueError('Package log exceeds report limit; inspect before advancing cursor')
    # An incomplete final line is not acknowledged; it may finish after this read.
    lines=raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b'\n'):lines.pop()
    cursor=progress(directory,'dpkg.cursor');start=0
    if re.fullmatch(r'[0-9]+:[0-9]+',cursor):
        old_inode,old_lines=map(int,cursor.split(':'))
        if old_inode==inode and old_lines<=len(lines):start=old_lines
    changes=[];seen=set()
    for raw_line in lines[start:]:
        parts=raw_line.decode('utf-8',errors='replace').split()
        if len(parts)<5:continue
        action=parts[2];package=parts[3]
        if action=='upgrade' and len(parts)>=6:line='• Upgraded `'+package+'`: '+parts[4]+' → '+parts[5]
        elif action in ('install','remove','purge'):line='• '+{'install':'Installed','remove':'Removed','purge':'Purged'}[action]+' `'+package+'`: '+parts[4]
        else:continue
        if line not in seen:changes.append(line);seen.add(line)
    point=str(inode)+':'+str(len(lines))
    # This tentative file is not committed progress and never suppresses a report.
    target=Path(directory)/'dpkg.cursor.next'
    fd,temp=tempfile.mkstemp(dir=directory,prefix='.cursor-')
    try:
        with os.fdopen(fd,'w') as f:os.fchmod(f.fileno(),0o600);f.write(point);f.flush();os.fsync(f.fileno())
        os.replace(temp,target)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return '\n'.join(changes)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):raise ValueError('Discord redirect refused')


def send(webhook_file,message):
    p=Path(webhook_file)
    if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077 or p.stat().st_size>1000:raise ValueError('Webhook file must be private')
    url=p.read_text().strip()
    if not re.fullmatch(r'https://discord\.com/api(?:/v10)?/webhooks/[0-9]{15,22}/[A-Za-z0-9_-]{30,150}',url):raise ValueError('Invalid Discord webhook destination')
    payload={'content':message[:1950],'allowed_mentions':{'parse':[]}}
    req=urllib.request.Request(url+'?wait=true',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','User-Agent':'ModelMLab-monitoring/1.0'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    # Preserve the original sender's three retries while bounding the retry window.
    deadline=time.monotonic()+50
    for attempt in range(4):
        delay=min(2**attempt,8)
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            with opener.open(req,timeout=min(15,remaining)) as response:
                raw=response.read(200001)
                if len(raw)>200000:raise ValueError('Discord receipt exceeded limit')
                result=json.loads(raw)
                if response.status!=200 or not re.fullmatch(r'[0-9]{15,22}',str(result.get('id',''))):raise ValueError('Discord message receipt missing')
            return
        except urllib.error.HTTPError as error:
            if error.code not in (408,429,500,502,503,504):break
            if error.code==429:
                try:delay=float(json.loads(error.read(10000)).get('retry_after',error.headers.get('Retry-After',delay)))
                except (ValueError,TypeError):pass
        except (urllib.error.URLError,TimeoutError):pass
        except Exception:break
        if not math.isfinite(delay) or delay<0:delay=1
        if attempt==3 or time.monotonic()+delay>=deadline:break
        time.sleep(delay)
    raise ValueError('Discord delivery was not confirmed; queued reports remain pending') from None


def flush(directory,webhook_file,sender=send,limit=10):
    delivered=0
    for _ in range(limit):
        now=time.time()
        with database(directory) as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute("SELECT id,message FROM notices WHERE state='pending' AND lease<=? ORDER BY created LIMIT 1",(now,)).fetchone()
            if not row:
                pending=con.execute("SELECT count(*) FROM notices WHERE state='pending'").fetchone()[0]
                return delivered,pending==0
            con.execute('UPDATE notices SET lease=? WHERE id=?',(now+60,row[0]))
        try:sender(webhook_file,row[1])
        except Exception:
            with database(directory) as con:con.execute('UPDATE notices SET lease=0,attempts=attempts+1,error=? WHERE id=?',('Delivery unconfirmed; retry retained',row[0]))
            return delivered,False
        with database(directory) as con:con.execute("UPDATE notices SET state='sent',lease=0,error=NULL WHERE id=?",(row[0],))
        delivered+=1
    # More queued reports remain; return a temporary failure instead of claiming a drain.
    with database(directory) as con:pending=con.execute("SELECT count(*) FROM notices WHERE state='pending'").fetchone()[0]
    return delivered,pending==0


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('command',choices=['progress','capture','record','flush','send']);p.add_argument('args',nargs='+');a=p.parse_args()
    try:
        if a.command=='progress':print(progress(*a.args))
        elif a.command=='capture':print(capture(*a.args))
        elif a.command=='record':record(*a.args,sys.stdin.read(200001))
        elif a.command=='send':send(a.args[0],sys.stdin.read(200001))
        else:
            count,complete=flush(*a.args);print(json.dumps({'reports_delivered':count,'queue_drained':complete}))
            if not complete:return 75
    except Exception as error:
        # No urllib exception string or credential value is printed.
        print('Report state/delivery failed; progress is not proof of delivery. Inspect private outbox.',file=sys.stderr)
        return 75
    return 0

if __name__=='__main__':sys.exit(main())
