"""Actual Docker recovery trial in a separate, private disposable project."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import sys


def run(args, check=True, limit=45):
    r = subprocess.run(args, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=limit)
    if check and r.returncode:
        raise RuntimeError('Test command failed: ' + args[0] + ': ' + r.stderr[-1200:])
    return r


def main():
    if sys.argv[1:] != ['--run'] or os.geteuid() != 0:
        raise SystemExit('Explicit opt-in required: sudo python3 tests/disposable_recovery.py --run')
    production = json.loads(run(['docker', 'inspect', 'traefik', 'gerbil', 'pangolin']).stdout)
    before = {c['Name']: (c['Id'], c['Image']) for c in production}
    old_id = next(c['Image'] for c in production if c['Name'] == '/traefik')
    token = uuid.uuid4().hex[:12]
    project = 'modelmlab-recovery-' + token
    tag_old, tag_bad = 'modelmlab-local/recovery-test:old-' + token, 'modelmlab-local/recovery-test:bad-' + token
    directory = Path(tempfile.mkdtemp(prefix=project+'-', dir='/root'))
    compose, override = directory/'compose.json', directory/'recovery.json'
    seed, baseline_ok, failure_seen, recovered = None, False, False, False
    cleanup_errors = []
    cli = ['docker', 'compose', '-p', project, '-f', str(compose)]
    result = None
    try:
        run(['docker', 'image', 'tag', old_id, tag_old])
        seed = run(['docker', 'create', '--network', 'none', '--name', project+'-seed', old_id]).stdout.strip()
        bad_id = run(['docker', 'commit', '--change', 'ENTRYPOINT ["traefik", "--modelmlab-invalid-test-option"]', seed, tag_bad], limit=90).stdout.strip()
        run(['docker', 'rm', '-v', seed]); seed = None
        definition = {'services': {'probe': {'image': tag_old, 'pull_policy': 'never',
            'command': ['--ping=true', '--entryPoints.traefik.address=:8080', '--global.checkNewVersion=false', '--global.sendAnonymousUsage=false'],
            'user': '65532:65532', 'read_only': True, 'cap_drop': ['ALL'],
            'security_opt': ['no-new-privileges:true'], 'pids_limit': 64, 'mem_limit': '128m', 'restart': 'no'}},
            'networks': {'default': {'internal': True}}}
        compose.write_text(json.dumps(definition)); compose.chmod(0o600)
        run(cli + ['config', '--quiet'])

        def container():
            return run(cli + ['ps', '-a', '-q', 'probe']).stdout.strip()

        def healthy(image_id):
            for _ in range(20):
                cid = container()
                if cid:
                    data = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
                    if data['State']['Running'] and data['Image'] == image_id:
                        if run(['docker', 'exec', cid, 'traefik', 'healthcheck', '--ping'], check=False, limit=5).returncode == 0:
                            return True
                time.sleep(1)
            return False

        run(cli + ['up', '-d', '--no-deps', '--pull', 'never', 'probe'])
        baseline_ok = healthy(old_id)
        if not baseline_ok:
            raise RuntimeError('Disposable baseline did not become healthy; production was not changed.')
        definition['services']['probe']['image'] = tag_bad
        compose.write_text(json.dumps(definition))
        run(cli + ['up', '-d', '--no-deps', '--pull', 'never', 'probe'], check=False)
        for _ in range(20):
            cid = container()
            if cid:
                data = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
                if data['Image'] == bad_id and data['State']['Status'] == 'exited' and data['State']['ExitCode'] != 0:
                    failure_seen = True
                    break
            time.sleep(1)
        if not failure_seen:
            raise RuntimeError('Deliberate replacement failure was not confirmed; trial incomplete.')
        override.write_text(json.dumps({'services': {'probe': {'image': tag_old}}})); override.chmod(0o600)
        run(cli + ['-f', str(override), 'up', '-d', '--no-deps', '--pull', 'never', 'probe'])
        recovered = healthy(old_id)
        if not recovered:
            raise RuntimeError('Disposable recovery did not pass its image and health checks.')
        after = json.loads(run(['docker', 'inspect', 'traefik', 'gerbil', 'pangolin']).stdout)
        production_unchanged = before == {c['Name']: (c['Id'], c['Image']) for c in after}
        if not production_unchanged:
            raise RuntimeError('Production changed during the trial; investigate before attributing cause.')
        result = {'status': 'passed', 'baseline_healthy': baseline_ok, 'deliberate_failure_observed': failure_seen,
                  'exact_previous_image_recovered': recovered, 'production_container_ids_and_images_unchanged': production_unchanged,
                  'ports_published': False, 'images_pulled': False, 'notifications_sent': False,
                  'scope': 'Disposable Compose replacement/recovery integration; not an execution of the production updater.'}
    finally:
        if compose.exists() and run(cli + ['down', '--timeout', '5', '--remove-orphans', '--volumes'], check=False, limit=45).returncode:
            cleanup_errors.append('disposable Compose project')
        if seed and run(['docker', 'rm', '-f', '-v', seed], check=False).returncode:
            cleanup_errors.append('disposable seed container')
        for tag in (tag_bad, tag_old):
            exists = run(['docker', 'image', 'inspect', tag], check=False).returncode == 0
            if exists and run(['docker', 'image', 'rm', tag], check=False).returncode:
                cleanup_errors.append(tag)
        if cleanup_errors:
            print('CLEANUP NEEDS ATTENTION:', ', '.join(cleanup_errors))
    if cleanup_errors:
        raise RuntimeError('Trial cleanup incomplete; do not mark complete.')
    result['disposable_docker_resources_removed'] = True
    (directory/'result.json').write_text(json.dumps(result, indent=2))
    (directory/'result.json').chmod(0o600)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
