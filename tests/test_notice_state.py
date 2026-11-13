import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('notice_state',ROOT/'bin/notice_state.py');notice=importlib.util.module_from_spec(spec);spec.loader.exec_module(notice)

class DurableReports(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.state=Path(self.tmp.name)/'state';self.state.mkdir(mode=0o700)
 def tearDown(self):self.tmp.cleanup()
 def rows(self):
  with notice.database(self.state) as con:return con.execute('SELECT kind,reference,message,state FROM notices').fetchall()
 def test_delivery_failure_retains_report_after_progress_is_committed(self):
  notice.record(self.state,'os-installed','22:4','dpkg.cursor','22:4','Upgraded package')
  self.assertEqual(notice.progress(self.state,'dpkg.cursor'),'22:4')
  def unavailable(*_):raise OSError('offline')
  self.assertEqual(notice.flush(self.state,'unused',sender=unavailable),(0,False))
  self.assertEqual(self.rows(),[('os-installed','22:4','Upgraded package','pending')])
 def test_transaction_failure_advances_neither_cursor_nor_outbox(self):
  (self.state/'dpkg.cursor').write_text('22:3')
  with notice.database(self.state) as con:con.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON progress BEGIN SELECT RAISE(ABORT,'fault injection'); END")
  with self.assertRaises(sqlite3.IntegrityError):notice.record(self.state,'os-installed','22:4','dpkg.cursor','22:4','Upgraded package')
  self.assertEqual(notice.progress(self.state,'dpkg.cursor'),'22:3');self.assertEqual(self.rows(),[])
 def test_retry_delivers_original_message_once_with_same_semantic_reference(self):
  notice.record(self.state,'os-installed','22:4','dpkg.cursor','22:4','Original report')
  notice.record(self.state,'os-installed','22:4','dpkg.cursor','22:4','Changed retry text must not replace report')
  delivered=[]
  self.assertEqual(notice.flush(self.state,'unused',sender=lambda _,message:delivered.append(message)),(1,True))
  self.assertEqual(notice.flush(self.state,'unused',sender=lambda _,message:delivered.append(message)),(0,True))
  self.assertEqual(delivered,['Original report']);self.assertEqual(self.rows()[0][3],'sent')
 def test_copy_truncated_package_log_is_not_permanently_skipped(self):
  p=self.state/'log';p.write_text('2026-01-01 00:00:00 install old:amd64 1\n2026-01-01 00:00:01 install another:amd64 1\n')
  notice.record(self.state,'-','-','dpkg.cursor',str(p.stat().st_ino)+':2','')
  p.write_text('2026-01-02 00:00:00 upgrade example:amd64 1 2\n')
  self.assertIn('example:amd64',notice.capture(self.state,p));self.assertTrue((self.state/'dpkg.cursor.next').read_text().endswith(':1'))
 def test_unfinished_log_line_is_acknowledged_only_after_newline(self):
  p=self.state/'log';p.write_text('2026-01-01 00:00:00 install old:amd64 1\n2026-01-01 00:00:01 upgrade example:amd64 1 2')
  first=notice.capture(self.state,p);self.assertNotIn('example:amd64',first)
  cursor=(self.state/'dpkg.cursor.next').read_text();notice.record(self.state,'os-installed',cursor,'dpkg.cursor',cursor,first)
  with p.open('a') as out:out.write('\n')
  self.assertIn('example:amd64',notice.capture(self.state,p))
 def test_failure_can_be_queued_without_changing_package_progress(self):
  (self.state/'dpkg.cursor').write_text('22:3')
  notice.record(self.state,'os-failed','capture-failed','-','','Package report capture failed')
  self.assertEqual(notice.progress(self.state,'dpkg.cursor'),'22:3');self.assertEqual(self.rows()[0][3],'pending')
 def test_legacy_digest_migrates_to_database_without_losing_pending_report(self):
  old=hashlib.sha256(b'old').hexdigest();new=hashlib.sha256(b'new').hexdigest()
  (self.state/'remaining-apt.digest').write_text(old)
  self.assertEqual(notice.progress(self.state,'remaining-apt.digest'),old)
  notice.record(self.state,'remaining-updates',new,'remaining-apt.digest',new,'Awaiting review')
  self.assertEqual(notice.progress(self.state,'remaining-apt.digest'),new);self.assertEqual(self.rows()[0][3],'pending')
 def test_crashed_dispatcher_lease_expires_without_deleting_report(self):
  import time
  notice.record(self.state,'os-installed','22:4','dpkg.cursor','22:4','Original report')
  with notice.database(self.state) as con:con.execute('UPDATE notices SET lease=?',(time.time()+60,))
  delivered=[];self.assertEqual(notice.flush(self.state,'unused',sender=lambda *args:delivered.append(args)),(0,False))
  self.assertEqual(delivered,[])
  with notice.database(self.state) as con:con.execute('UPDATE notices SET lease=0')
  self.assertEqual(notice.flush(self.state,'unused',sender=lambda *args:delivered.append(args)),(1,True))
 def test_private_webhook_validation_precedes_any_network_request(self):
  p=self.state/'webhook';p.write_text('https://'+'discord.com'+'/api/webhooks/123456789012345678/'+'a'*60);p.chmod(0o644)
  with patch.object(notice.urllib.request,'build_opener') as network:
   with self.assertRaises(ValueError):notice.send(p,'test')
   network.assert_not_called()
 def test_transient_delivery_retries_without_relaxing_tls_or_exposing_url(self):
  p=self.state/'webhook';p.write_text('https://'+'discord.com'+'/api/webhooks/123456789012345678/'+'a'*60);p.chmod(0o600)
  class Opener:
   calls=0
   def open(self,*args,**kwargs):self.calls+=1;raise notice.urllib.error.URLError('fixture offline')
  opener=Opener()
  with patch.object(notice.urllib.request,'build_opener',return_value=opener),patch.object(notice.time,'sleep') as pause:
   with self.assertRaises(ValueError) as error:notice.send(p,'Report')
   self.assertEqual(opener.calls,4);self.assertEqual(pause.call_count,3)
   self.assertNotIn('https:',str(error.exception))

 def test_send_keeps_credential_out_of_arguments_and_disables_mentions(self):
  p=self.state/'webhook';p.write_text('https://'+'discord.com'+'/api/webhooks/123456789012345678/'+'a'*60);p.chmod(0o600)
  class Response:
   status=200
   def __enter__(self):return self
   def __exit__(self,*_):pass
   def read(self,_):return b'{"id":"123456789012345679"}'
  class Opener:
   def open(self,request,timeout):
    body=json.loads(request.data);self_outer.assertEqual(body['allowed_mentions'],{'parse':[]});self_outer.assertEqual(timeout,15)
    self_outer.assertTrue(request.full_url.endswith('?wait=true'));return Response()
  self_outer=self
  with patch.object(notice.urllib.request,'build_opener',return_value=Opener()):notice.send(p,'@everyone safe content')

class OSIntegration(unittest.TestCase):
 def run_os(self,failed=False,missing_log=False):
  tmp=tempfile.TemporaryDirectory();root=Path(tmp.name);log=root/'dpkg.log';log.write_text('')
  source=(ROOT/'bin/vps-auto-updater').read_text().replace('/var/log/dpkg.log',str(log)).replace('/run/lock/modelmlab-updater-os.lock',str(root/'lock'))
  script=root/'updater';script.write_text(source)
  # The helper stays a sibling of the sourced script, exactly as the installed layout.
  import shutil
  shutil.copy2(ROOT/'bin/notice_state.py',root/'notice_state.py')
  script_text='''hostname(){ printf test-vps; }
date(){ printf '2026-11-12T15:00:00Z'; }
flock(){ return 0; }
sha256sum(){ python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+"  -")'; }
apt-get(){ return 0; }
apt(){ printf 'Listing...\\nexample/test 2 amd64 [upgradable from: 1]\\n'; }
unattended-upgrade(){ printf '2026-11-12 15:00:00 upgrade example:amd64 1 2\\n' >> "$TESTDIR/dpkg.log"; return "$APT_RESULT"; }
source "$TESTDIR/updater"
initialize_state
'''
  if missing_log:script_text+='rm "$TESTDIR/dpkg.log"\nunattended-upgrade(){ return 0; }\n'
  script_text+='run_os_updates\nexit "$?"\n'
  env=dict(os.environ,TESTDIR=str(root),APT_RESULT='42' if failed else '0',MONITOR_UPDATER_STATE_DIR=str(root/'state'),MONITOR_UPDATER_LOG_DIR=str(root/'logs'),MONITOR_UPDATER_WEBHOOK_FILE=str(root/'no-webhook'))
  result=subprocess.run(['/bin/bash'],input=script_text,text=True,capture_output=True,env=env,timeout=15)
  with notice.database(root/'state') as con:rows=con.execute('SELECT kind,message,state FROM notices ORDER BY created').fetchall();progress=dict(con.execute('SELECT key,value FROM progress'))
  tmp.cleanup();return result,rows,progress
 def test_retry_only_command_never_runs_package_or_container_commands(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);state=root/'state';state.mkdir(mode=0o700)
   notice.record(state,'os-installed','22:4','dpkg.cursor','22:4','Queued report')
   tools=root/'tools';tools.mkdir()
   for name in ['apt-get','apt','unattended-upgrade','docker']:
    tool=tools/name;tool.write_text('#!/bin/sh\necho unexpected >> "$TESTDIR/unsafe-command"\nexit 99\n');tool.chmod(0o755)
   env=dict(os.environ,TESTDIR=str(root),PATH=str(tools)+':'+os.environ['PATH'],MONITOR_UPDATER_STATE_DIR=str(state),MONITOR_UPDATER_LOG_DIR=str(root/'logs'),MONITOR_UPDATER_WEBHOOK_FILE=str(root/'absent-webhook'))
   # Use a temporary lock path for an unprivileged development Mac.
   import shutil
   script=root/'retry';script.write_text((ROOT/'bin/vps-auto-updater').read_text().replace('/run/lock/modelmlab-updater-os.lock',str(root/'lock')))
   shutil.copy2(ROOT/'bin/notice_state.py',root/'notice_state.py')
   # flock is a Linux requirement; the real command is exercised in Linux validation.
   if __import__('platform').system()!='Linux':
    tool=tools/'flock';tool.write_text('#!/bin/sh\nexit 0\n');tool.chmod(0o755)
   result=subprocess.run(['/bin/bash',str(script),'notices'],capture_output=True,text=True,env=env,timeout=15)
   self.assertEqual(result.returncode,75,result.stderr);self.assertFalse((root/'unsafe-command').exists())
   with notice.database(state) as con:self.assertEqual(con.execute('SELECT state FROM notices').fetchone()[0],'pending')

 def test_actual_shell_flow_queues_reports_when_discord_is_unavailable(self):
  result,rows,progress=self.run_os();self.assertEqual(result.returncode,75,result.stderr)
  self.assertEqual([row[0] for row in rows],['os-installed','remaining-updates']);self.assertTrue(all(row[2]=='pending' for row in rows))
  self.assertIn('example:amd64',rows[0][1]);self.assertIn('dpkg.cursor',progress);self.assertIn('remaining-apt.digest',progress)
 def test_actual_updater_failure_keeps_real_exit_and_failure_report(self):
  result,rows,progress=self.run_os(failed=True);self.assertEqual(result.returncode,42,result.stderr)
  self.assertEqual(rows[0][0],'os-failed');self.assertIn('Exit status: 42',rows[0][1]);self.assertEqual(rows[0][2],'pending')
 def test_missing_log_queues_diagnostic_without_faking_cursor_progress(self):
  result,rows,progress=self.run_os(missing_log=True);self.assertEqual(result.returncode,75,result.stderr)
  self.assertEqual(rows[0][0],'os-failed');self.assertNotIn('dpkg.cursor',progress)
