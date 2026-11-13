"""Offline fault-injection checks. Docker, curl and notifications are mocked."""
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = (ROOT/'bin/vps-watchdog').read_text()
UPDATER = (ROOT/'bin/vps-auto-updater').read_text()
VALIDATOR = UPDATER[UPDATER.index('valid_monitor_host() {'):UPDATER.index('readonly HOST_NAME')]
CONTAINER_FUNCTIONS = VALIDATOR + UPDATER[UPDATER.index('proxy_ready_once() {'):UPDATER.index('if [[ "${BASH_SOURCE[0]}" == "$0" ]]')]
MOCKS = r'''
set -uo pipefail
readonly OLD="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
readonly NEW="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
HOST_NAME=test-vps
MONITOR_HTTPS_HOST=monitor.example.net
MONITOR_HTTPS_PATHS="/ /privacy/ /terms/"
TRAEFIK_CONFIG=/etc/traefik/traefik_config.yml
COMPOSE_FILE="$TESTDIR/compose.yml"
STATE_DIR="$TESTDIR/state"
LOG_DIR="$TESTDIR/logs"
timestamp=test-run
mkdir -p "$STATE_DIR/backups" "$LOG_DIR"
printf 'image: docker.io/traefik:latest\n' > "$COMPOSE_FILE"
printf '%s' "$OLD" > "$TESTDIR/running"
flock() { return 0; }
sleep() { :; }
timeout() {
  while [[ "${1:-}" != docker && "${1:-}" != curl ]]; do shift; done
  "$@"
}
send_discord() {
  printf '%s\n' "$1" >> "$TESTDIR/notifications"
  [[ "$SCENARIO" != notifyfail ]]
}
discord_safe() { send_discord "$1" || true; }
latest_stable_tag() { printf 1.0.0; }
dpkg() { return 1; }
curl() {
  if [[ "$SCENARIO" == baselinefail || ( ( "$SCENARIO" == healthfail || "$SCENARIO" == rollbackfail ) && "$(cat "$TESTDIR/running")" == "$NEW" ) ]]; then return 22; fi
  printf 200
}
docker() {
  printf '%s\n' "$*" >> "$TESTDIR/docker-calls"
  if [[ "$1" == pull ]]; then
    [[ "$SCENARIO" != pullfail ]]; return
  elif [[ "$1 $2" == 'image inspect' ]]; then
    if [[ "$SCENARIO" == unchanged ]]; then printf '%s' "$OLD"; else printf '%s' "$NEW"; fi
  elif [[ "$1 $2" == 'image tag' ]]; then
    return 0
  elif [[ "$1" == inspect ]]; then
    if [[ "$*" == *'org.opencontainers.image.version'* ]]; then printf 1.0.0; else cat "$TESTDIR/running"; fi
  elif [[ "$1" == exec ]]; then
    if [[ "$*" == *version ]]; then printf 'Version: 3.5.1\n'; fi
  elif [[ "$1" == run ]]; then
    if [[ "$SCENARIO" == versionfail ]]; then return 1; fi
    if [[ "$SCENARIO" == major ]]; then printf 'Version: 4.0.0\n'; else printf 'Version: 3.5.2\n'; fi
  elif [[ "$1" == compose ]]; then
    if [[ "$*" == *'config --format json'* ]]; then
      printf '{"services":{"traefik":{"image":"docker.io/traefik:latest"}}}'
    elif [[ "$*" == *'config --quiet'* ]]; then
      return 0
    elif [[ "$*" == *'up -d'* ]]; then
      if [[ "$*" == *'.json'* ]]; then
        [[ "$SCENARIO" != rollbackfail ]] || return 1
        printf '%s' "$OLD" > "$TESTDIR/running"
      else
        printf '%s' "$NEW" > "$TESTDIR/running"
        [[ "$SCENARIO" != upfail ]] || return 1
      fi
    else return 93
    fi
  else return 94
  fi
}
'''

class MonitoringTests(unittest.TestCase):
    def run_flow(self, scenario):
        with tempfile.TemporaryDirectory() as tmp:
            functions = CONTAINER_FUNCTIONS.replace('/run/lock/modelmlab-updater-containers.lock', tmp + '/lock')
            script = MOCKS + '\n' + functions + '''\nif [[ "$SCENARIO" == quarantined ]]; then printf "%s\n" "$NEW" > "$STATE_DIR/traefik.failed-image"; fi
run_container_updates
rc=$?
printf 'RESULT=%s\\n' "$rc"
exit "$rc"
'''
            import os
            env = dict(os.environ, TESTDIR=tmp, SCENARIO=scenario)
            r = subprocess.run(['/bin/bash'], input=script, text=True, capture_output=True, env=env, timeout=10)
            read = lambda name: (Path(tmp) / name).read_text() if (Path(tmp) / name).exists() else ''
            return r, read('docker-calls'), read('running'), read('compose.yml'), read('state/traefik.failed-image'), read('notifications')

    def test_bash_syntax_both_candidates(self):
        for text in (UPDATER, WATCHDOG):
            r = subprocess.run(['/bin/bash', '-n'], input=text, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_running_change_if_latest_identical(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('unchanged')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('up -d', calls); self.assertNotIn(' run ', calls)
        self.assertIn(':latest', compose)

    def test_success_preserves_latest_and_checks_image(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('success')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(running, 'sha256:' + 'b'*64)
        self.assertIn('--pull never', calls); self.assertIn('--network none --read-only', calls)
        self.assertEqual(compose, 'image: docker.io/traefik:latest\n')
        self.assertIn('updated and verified', notices)

    def test_pull_failure_never_recreates(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('pullfail')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('up -d', calls)
        self.assertEqual(running, 'sha256:' + 'a'*64)

    def test_baseline_failure_never_pulls(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('baselinefail')
        self.assertNotEqual(r.returncode, 0); self.assertNotIn('pull ', calls)

    def test_version_probe_failure_never_recreates(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('versionfail')
        self.assertNotEqual(r.returncode, 0); self.assertNotIn('up -d', calls)

    def test_failed_up_recovers_exact_previous_image(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('upfail')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(running, 'sha256:'+'a'*64)
        self.assertEqual(quarantine.strip(), 'sha256:'+'b'*64)
        self.assertIn('RECOVERED:', r.stdout)
        self.assertNotIn('updated and verified', notices)

    def test_unhealthy_update_recovers_exact_previous_image(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('healthfail')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(running, 'sha256:'+'a'*64)
        self.assertIn('RECOVERED:', r.stdout)
        self.assertEqual(compose, 'image: docker.io/traefik:latest\n')

    def test_major_change_keeps_existing_review_policy(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('major')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('up -d', calls)
        self.assertIn('needs release review', notices)
        self.assertEqual(running, 'sha256:'+'a'*64)

    def test_notification_failure_does_not_rollback_healthy_update(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('notifyfail')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(running, 'sha256:'+'b'*64)
        self.assertNotIn('RECOVERED:', r.stdout)
        self.assertEqual(quarantine, '')

    def test_bash_errexit_orlist_regression(self):
        old = '(set -e; false; echo UNSAFE_CONTINUATION) || rc=$?'
        new = '(set -e; false; echo UNSAFE_CONTINUATION)\nrc=$?\nexit "$rc"'
        a = subprocess.run(['/bin/bash', '-c', old], capture_output=True, text=True)
        b = subprocess.run(['/bin/bash', '-c', new], capture_output=True, text=True)
        self.assertIn('UNSAFE_CONTINUATION', a.stdout)
        self.assertNotIn('UNSAFE_CONTINUATION', b.stdout)
        self.assertNotEqual(b.returncode, 0)

    def test_latest_references_not_replaced_in_transform(self):
        self.assertNotIn('sed -i', CONTAINER_FUNCTIONS)
        self.assertNotIn('latest_traefik_patch', CONTAINER_FUNCTIONS)
        self.assertIn('config --format json', CONTAINER_FUNCTIONS)

    def test_custom_traefik_config_used_by_both_probes(self):
        expected = 'traefik healthcheck "--configFile=${TRAEFIK_CONFIG}"'
        self.assertIn(expected, WATCHDOG)
        self.assertIn(expected, CONTAINER_FUNCTIONS)
        self.assertNotIn('healthcheck --ping --ping.entryPoint=web', WATCHDOG)
        self.assertNotIn('healthcheck --ping --ping.entryPoint=web', CONTAINER_FUNCTIONS)

    def test_watchdog_routes_are_tls_checked(self):
        self.assertIn('--resolve', WATCHDOG)
        self.assertNotIn('curl -sk', WATCHDOG)
        self.assertNotIn('--insecure', WATCHDOG)
        self.assertIn('check_proxy_routes\n  build_metrics', WATCHDOG)


    def test_failed_rollback_is_reported_without_false_recovery(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('rollbackfail')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('RECOVERY FAILED:', r.stdout)
        self.assertNotIn('RECOVERED:', r.stdout)
        self.assertEqual(quarantine, '')

    def test_failed_image_is_not_redeployed(self):
        r, calls, running, compose, quarantine, notices = self.run_flow('quarantined')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('up -d', calls)
        self.assertEqual(running, 'sha256:'+'a'*64)

    def route_check(self, behavior):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'watchdog'; p.write_text(WATCHDOG)
            script = 'source "$1"\n' + behavior + '\ncheck_proxy_routes\nprintf "PROBLEMS=%s\\n" "${#problems[@]}"'
            import os
            r = subprocess.run(['/bin/bash', '-c', script, 'test', str(p)], capture_output=True, text=True, timeout=10, env={**os.environ, 'MONITOR_HTTPS_HOST':'monitor.example.net'})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_functional_routes_pass_without_alerts(self):
        out = self.route_check('timeout() { return 0; }; curl() { printf 200; }')
        self.assertIn('PROBLEMS=0', out)

    def test_tls_error_cannot_pass_even_with_http_200(self):
        out = self.route_check('timeout() { return 0; }; curl() { printf 200; return 60; }')
        self.assertIn('PROBLEMS=3', out)

    def test_ping_failure_is_visible_when_routes_pass(self):
        out = self.route_check('timeout() { return 1; }; curl() { printf 200; }')
        self.assertIn('PROBLEMS=1', out)

if __name__ == '__main__':
    unittest.main(verbosity=2)
