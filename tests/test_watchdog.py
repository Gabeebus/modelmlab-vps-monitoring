"""Offline watchdog regression checks against the reusable draft."""
from pathlib import Path
import subprocess
import tempfile
import unittest
CANDIDATE = (Path(__file__).resolve().parents[1]/'bin/vps-watchdog').read_text()

class WatchdogFixTests(unittest.TestCase):
    def shell(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            source = CANDIDATE.replace('/var/lib/modelmlab-monitoring/watchdog', tmp + '/state')
            p = Path(tmp) / 'candidate'; p.write_text(source)
            script = 'source "$1"\nread_webhook() { printf TEST_ONLY; }\njq() { printf \'{}\'; }\nsha256sum() { python3 -c \'import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())\'; }\n' + body
            return subprocess.run(['/bin/bash', '-c', script, 'test', str(p)], capture_output=True, text=True, timeout=10)

    def assert_shell(self, body):
        r = self.shell(body); self.assertEqual(r.returncode, 0, r.stderr + r.stdout); return r.stdout

    def test_collect_checks_actual_docker_unit(self):
        for state in ('active', 'inactive'):
            with self.subTest(state=state):
                out = self.assert_shell("""
bounded() {
  case "$1:$2" in
    systemctl:is-active)
      case "$3" in
        docker) printf '%s\\n' 'STATE_VALUE' ;;
        tailscaled|ssh|crowdsec|crowdsec-firewall-bouncer) printf 'active\\n' ;;
        *) printf 'Unexpected unit: %s\\n' "$3" >&2; return 1 ;;
      esac ;;
    systemctl:--failed) : ;;
    docker:info) return 1 ;;
    ufw:status) printf 'Status: active\\n' ;;
    *) return 1 ;;
  esac
}
collect_status
printf '%s\\n' "${service_lines[@]}"
""".replace('STATE_VALUE', state))
                self.assertIn('Docker: ' + state, out)
                self.assertNotIn('docker: inactive', out)

    def test_bash_syntax(self):
        r = subprocess.run(['/bin/bash', '-n'], input=CANDIDATE, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_memory_readings_do_not_realert(self):
        self.assert_shell('problems=("Memory usage is 91%"); a=$(status_signature); problems=("Memory usage is 97%"); [[ "$a" == "$(status_signature)" ]]')

    def test_disk_readings_do_not_realert(self):
        self.assert_shell('problems=("Root filesystem usage is 86%"); a=$(status_signature); problems=("Root filesystem usage is 94%"); [[ "$a" == "$(status_signature)" ]]')

    def test_load_readings_do_not_realert(self):
        self.assert_shell('problems=("One-minute load 4.5 is more than twice the 2 CPU count"); a=$(status_signature); problems=("One-minute load 7.25 is more than twice the 2 CPU count"); [[ "$a" == "$(status_signature)" ]]')

    def test_distinct_failure_still_alerts(self):
        self.assert_shell('problems=("Memory usage is 91%"); a=$(status_signature); problems+=("SSH service is inactive"); [[ "$a" != "$(status_signature)" ]]')

    def test_problem_order_does_not_realert(self):
        self.assert_shell('problems=("SSH service is inactive" "Memory usage is 91%"); a=$(status_signature); problems=("Memory usage is 95%" "SSH service is inactive"); [[ "$a" == "$(status_signature)" ]]')

    def test_recovery_has_healthy_signature(self):
        self.assert_shell('problems=("Memory usage is 91%"); a=$(status_signature); problems=(); [[ "$(status_signature)" == healthy && "$a" != healthy ]]')

    def test_preview_does_not_write_state_or_send(self):
        out = self.assert_shell('''jq() { :; }; curl() { return 99; }; timeout() { :; }
collect_status() { problems=("Memory usage is 95%"); }
build_metrics() { :; }; daily_report() { printf 'PREVIEW_ONLY'; }
send_discord() { printf 'UNEXPECTED_SEND'; return 99; }
install() { printf 'UNEXPECTED_WRITE'; return 99; }
main preview || exit 1
[[ ! -e "$STATE_DIR" ]]''')
        self.assertEqual(out.strip(), 'PREVIEW_ONLY')

    def test_invalid_mode_no_collection(self):
        r = self.shell('jq() { :; }; curl() { :; }; collect_status() { echo UNEXPECTED; }; main typo')
        self.assertEqual(r.returncode, 2); self.assertNotIn('UNEXPECTED', r.stdout)

    def test_lock_contention_skips_probes_and_alerts(self):
        out = self.assert_shell('''jq() { :; }; curl() { :; }; timeout() { :; }; flock() { return 1; }
collect_status() { echo UNEXPECTED; }; send_discord() { echo UNEXPECTED_SEND; }
main check''')
        self.assertIn('skipped', out); self.assertNotIn('UNEXPECTED', out)

    def test_probe_timeout_arguments(self):
        out = self.assert_shell('timeout() { printf "[%s]" "$@"; }; bounded docker info')
        self.assertEqual(out, '[--signal=TERM][--kill-after=2s][5s][docker][info]')

    def test_delivery_passes_only_helper_and_credential_filename(self):
        r = self.shell('python3() { printf "[%s]" "$@" >&2; cat >/dev/null; }; send_discord "MESSAGE_NOT_AN_ARGUMENT"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('[send][/etc/modelmlab-monitoring/watchdog-webhook.url]', r.stderr)
        self.assertNotIn('MESSAGE_NOT_AN_ARGUMENT', r.stderr)
        self.assertNotIn('https://', r.stderr)

    def test_delivery_message_is_stdin_data(self):
        r = self.shell('python3() { cat; }; send_discord "@everyone literal message"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '@everyone literal message')

    def test_unconfirmed_delivery_does_not_commit_health_state(self):
        self.assert_shell("""jq() { :; }; curl() { :; }; timeout() { :; }
collect_status() { problems=(); }; check_proxy_routes() { :; }; build_metrics() { :; }
send_discord() { return 1; }
(main check); rc=$?
[[ $rc != 0 && ! -e "$STATUS_FILE" && ! -e "$SIGNATURE_FILE" && ! -e "$DAILY_FILE" ]]
""")

if __name__ == '__main__':
    unittest.main(verbosity=2)
