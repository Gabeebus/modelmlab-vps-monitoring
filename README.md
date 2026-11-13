# VPS monitoring for a Pangolin stack

A Bash watchdog plus APT security-update and Traefik image-update jobs. It targets a Linux VPS with containers named `traefik`, `gerbil`, and `pangolin`. No open-source license has been selected.

Watchdog notifications use the same bounded TLS sender as update reports, validate a Discord message receipt, disable mentions and keep the webhook value out of process arguments.

The watchdog checks Docker, Tailscale, SSH, CrowdSec, its firewall bouncer, failed systemd units, UFW, memory, root-disk space, CPU load, reboot requirements, Traefik ping, and configured HTTPS paths. It sends change/recovery notifications and a daily report to your own Discord webhook. Probe deadlines, a state lock, and stable resource-alert fingerprints limit stalled runs and repeated alerts.

## Update policy

Compose keeps `latest`. The Traefik job pulls that reference, compares immutable image IDs, and validates the replacement. Automatic deployment stays within the running major/minor release series; a different release series or downgrade is held for review. A new local image can be downloaded without being deployed. `latest` alone does not automatically update running containers.

Before replacement, the job retains the previous image under a local recovery tag and writes a service-only Compose recovery override. Failed replacement or health validation triggers recovery to that image. Recovery must pass the image-ID and proxy checks before being reported successful. Failed image IDs are held to avoid repeated deployment attempts. Pangolin and Gerbil remain release-notification-only; this package does not coordinate their migrations.

The OS job runs `apt-get update` and `unattended-upgrade`. It installs what your existing unattended-upgrades policy permits and never reboots automatically. Changing apt repositories or upgrade policy is outside this package.

## Requirements

Use a supported Ubuntu/Debian Linux host, Bash 4.3+, Python 3.9+, Docker with Compose v2, curl, jq, GNU coreutils, util-linux/flock, APT/dpkg, the unattended-upgrades package with reviewed origin rules, UFW, Tailscale and CrowdSec with its firewall bouncer. Missing monitored services are reported as failures. Commands such as GNU `stat`, `free`, `nproc` and `dpkg` are Linux-specific. The included offline checks can also run on a development Mac; that is not a Linux installation test.

Your existing Traefik configuration must enable ping. Set `MONITOR_TRAEFIK_CONFIG` to its path **inside the Traefik container**. No ping port is published by this package. The configured HTTPS host must already have valid TLS and return HTTP 200 at every configured path through the VPS loopback listener. Requests use that host for SNI/certificate validation with `--resolve` to `127.0.0.1`; they do not test public DNS, an external client path, or all UDP tunnels. Do not make a private application public to satisfy a health check; choose an appropriate existing health route.

## Review and offline checks

From the repository directory:

```bash
bash install.sh --check
```

The 50 included checks mock Docker, curl and delivery. They do not pull images, change running containers, send notifications or prove a live production upgrade works.

## Install on a clean host

Review the scripts and units first. Installation requires the dependencies above to be installed already.

```bash
sudo bash install.sh --install
```

This copies files and reloads systemd. It does not start jobs or enable timers. It refuses legacy monitoring units and an existing package installation to avoid duplicate jobs. This first installer is not an upgrade/migration tool and is not an atomic package manager; a filesystem error can leave partially copied files for operator review. Do not run it on the already-configured source VPS.

Copy and edit the configuration locally:

```bash
sudo install -m 0600 /etc/modelmlab-monitoring/monitoring.env.example /etc/modelmlab-monitoring/monitoring.env
sudoedit /etc/modelmlab-monitoring/monitoring.env
```

Set a real `MONITOR_HTTPS_HOST`, the paths it serves, your Compose file, Traefik's in-container config path, and your report timezone. The default example paths are `/ /privacy/ /terms/`; replace them if your health target differs. Set each webhook-file path to a root-only file containing your own Discord webhook URL. Create/edit those files locally using your credential-management process; do not paste values into commands, commit them, or send them to an AI. This repository contains no webhook URLs or credentials.

A read-only preview, which sends no notification and writes no watchdog state:

```bash
sudo env MONITOR_HTTPS_HOST=monitor.example.com MONITOR_HTTPS_PATHS="/" MONITOR_TRAEFIK_CONFIG=/etc/traefik/traefik_config.yml /usr/local/libexec/modelmlab-monitoring/vps-watchdog preview
```

Replace the example host/path before running. Check that every expected probe passes. Preview includes system metadata; keep its output private.

When the configuration, destinations and preview are reviewed, enable the watchdog:

```bash
sudo systemctl enable --now modelmlab-vps-watchdog.timer
```

The next check can send a real message to the configured destination. Enable update timers only when you accept the OS/Traefik update policy:

```bash
sudo systemctl enable --now modelmlab-vps-os.timer modelmlab-vps-containers.timer
```

The sample update schedules are 04:10 and 04:40 in the server's system timezone. The report timezone is configured separately. Review or override the timer schedules before enabling them. These commands enable persistent production behavior; they are not read-only tests.

## Recovery and troubleshooting

Start with service status and the private updater log directory `/var/log/modelmlab-monitoring/updater`. A oneshot job being inactive after completion is normal; inspect `Result` and `ExecMainStatus`. A successful command exit alone is not proof every monitored component is healthy; inspect the preview/report too.

Recovery references and overrides are retained under `/var/lib/modelmlab-monitoring/updater/backups`. After an automatic recovery, a plain manual `docker compose up` against the unchanged `latest` base can recreate the failed image. Review the exact recovery override and quarantined image before making manual changes. Do not delete recovery images until you accept the working replacement and have an independent backup. Logs/recovery tags are not automatically pruned.

A failed `unattended-upgrades.service` can refer to the shutdown helper, not the actual package updater. Inspect its unit, the main upgrade log and the separate shutdown-helper log before diagnosing package-update failure. Do not clear a failed state and call it a root-cause repair.

OS reports now use a private SQLite outbox. Each report and its cursor/digest commit together; an unconfirmed send remains pending. Existing flat-file cursor/digest values are used as a migration baseline, then the database becomes authoritative. A failed capture never advances the committed cursor. Exit 75 means a reporting/state problem or pending delivery; it does not by itself mean package installation failed. The original package-update failure exit is preserved. Reports retry on the next OS run. You can retry only queued reports, without running apt or Docker, with `sudo /usr/local/libexec/modelmlab-monitoring/vps-auto-updater notices`; use the OS unit's same environment if paths were customized. No extra timer is enabled. Keep `report-outbox.sqlite` with updater state/backups. Delivery is at-least-once: a lost acknowledgement after an accepted send can produce a duplicate. Monitoring notices retain Discord's message-length limit; the private updater log remains the detailed record. The webhook URL is read inside Python instead of appearing in command arguments, and mentions are disabled.

## Optional real Docker recovery trial

On a reviewed Linux Docker host that already has a running container named `traefik`:

```bash
sudo python3 tests/disposable_recovery.py --run
```

This creates an isolated Compose project from the locally available image and a deliberately invalid disposable image entrypoint. It checks baseline health, observes failure, restores the exact previous image, compares production container IDs/images and removes test Docker resources. No images are downloaded or ports published; no Discord messages are sent. It requires Docker's local image-tag/create/commit permissions and about 128 MiB per running test container. It leaves a private result file under `/root`. It does not run the production updater or prove this adapted draft has been deployed.

## Validation and release scope

The release candidate has 50 offline checks, all passed as Linux root on Debian 13 with networking disabled. These checks exercise mocked failure paths and delivery contracts. The same executable watchdog was tested with actual Linux probes, a private trusted TLS test destination, a deliberate broken/restored HTTPS route, notification failure/retry, unchanged-alert suppression and daily-report deduplication. Unconfigured lab services correctly remained unhealthy.

The actual OS updater ran on Debian 13 under an explicit Debian security-only unattended-upgrades policy: 10 packages changed, no reboot occurred, both reports were acknowledged and no reports remained pending. Retrying the empty outbox sent nothing. The test policy was process-local; configure your own unattended-upgrades policy before enabling the OS timer. Ubuntu has not received this live trial.

The unchanged container updater was exercised against a real isolated Pangolin/Gerbil/Traefik stack with trusted lab TLS: normal patch replacement, deliberately failed replacement, recovery to the exact previous image, route validation and quarantine of the failed candidate all passed. Pangolin and Gerbil containers were unchanged. This validates the tested recovery mechanism, not compatibility with every future image or database migration.

Fresh-host installation evidence must match the release file manifest. Historical installation passes on earlier revisions are separate evidence. Installation leaves all timers disabled and requires no webhook credential. Review the exact candidate and installation receipt before release.

These trials do not certify production configuration, external DNS/client access, public certificate issuance, remote UDP tunnels, or all monitored services healthy. This package monitors an existing stack; it does not provision Pangolin, enroll Tailscale, install CrowdSec, or issue certificates. Production deployment still requires an operator-reviewed preview, backup, origin policy and notification destination.

The source originals were owner-exported installed scripts; provenance is recorded in `PROVENANCE.json`. Trivy monitoring exists separately and is excluded. No production configuration, private history, keys, actual webhook values or runtime state belongs in this repository. No open-source license has been selected.
