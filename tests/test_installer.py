"""Exercise the real installer prerequisite gate without reaching system mutations."""
import os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid()==0,'Installer gate requires a disposable Linux root test')
class InstallerPrerequisites(unittest.TestCase):
 def test_missing_os_updater_dependency_refuses_before_service_or_file_operations(self):
  for missing in ['apt-get','unattended-upgrade']:
   with self.subTest(missing=missing),tempfile.TemporaryDirectory() as d:
    root=Path(d);bin=root/'bin';bin.mkdir();marker=root/'service-probe'
    tools='bash curl jq timeout flock python3 docker systemctl ufw tailscale cscli apt apt-get dpkg unattended-upgrade dirname uname'.split()
    for name in tools:
     if name==missing:continue
     target=shutil.which(name if name in {'bash','dirname','uname'} else 'true');self.assertIsNotNone(target)
     (bin/name).symlink_to(target)
    (bin/'systemctl').unlink();(bin/'systemctl').write_text('#!/bin/sh\nprintf called > "$PROBE_MARKER"\nexit 1\n');(bin/'systemctl').chmod(0o755)
    env={'PATH':str(bin),'HOME':d,'PROBE_MARKER':str(marker),'LC_ALL':'C'}
    r=subprocess.run([shutil.which('bash'),str(ROOT/'install.sh'),'--install'],capture_output=True,text=True,env=env,timeout=10)
    self.assertNotEqual(r.returncode,0);self.assertIn('Missing dependency: '+missing,r.stderr);self.assertFalse(marker.exists())
