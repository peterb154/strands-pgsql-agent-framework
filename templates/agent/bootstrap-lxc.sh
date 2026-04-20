#!/usr/bin/env bash
# bootstrap-lxc.sh — host prep for a fresh Debian/Ubuntu LXC.
#
# Run ONCE after creating a new Proxmox LXC (or any Debian/Ubuntu host).
# Idempotent: safe to re-run. Edit in place if your agent has additional
# host needs — this file is yours now.
#
# Usage:
#   bash bootstrap-lxc.sh
#
# What it does:
#   - preflight: verify Docker-compatible LXC features
#   - install docker engine + compose plugin
#   - configure docker log rotation
#   - install baseline tools (git, curl, jq, ca-certificates)
#   - systemd unit that auto-starts any /opt/*/docker-compose.yml on reboot
#
# What it explicitly does NOT do:
#   - install chromium, postgres, python — those live in containers
#   - stamp/install an agent — that's install.sh's job
#   - configure env vars — that's .env's job

set -euo pipefail

log() { echo "[bootstrap-lxc] $*"; }

# ---------------------------------------------------------------------------
# preflight: LXC features
# ---------------------------------------------------------------------------
# Docker needs nesting and keyctl to work correctly inside a Proxmox LXC.
# On a bare-metal host or VM, these are implicit and the check passes trivially.
check_lxc_features() {
    if [ ! -f /proc/1/environ ]; then
        return 0  # unusual env; skip
    fi
    # Proxmox LXC containers expose container=lxc in /proc/1/environ
    if ! grep -qa 'container=lxc' /proc/1/environ 2>/dev/null; then
        return 0  # not an LXC, nothing to check
    fi
    # Cheap heuristic: if /dev/kmsg is missing write access or keyctl missing,
    # Docker overlay and cgroup issues will appear. Best signal is a loopback
    # keyctl() syscall, but testing that requires building a binary. Instead,
    # check for the two capabilities that manifest as visible features in the
    # container's /proc.
    if [ ! -r /proc/keys ]; then
        log "WARNING: /proc/keys not readable — LXC may be missing keyctl=1 feature."
        log "  On the Proxmox host, run: pct set <CTID> --features nesting=1,keyctl=1"
        log "  Then restart the LXC."
    fi
}

# ---------------------------------------------------------------------------
# docker install
# ---------------------------------------------------------------------------
install_docker() {
    if command -v docker >/dev/null 2>&1; then
        log "docker already installed: $(docker --version)"
        return 0
    fi
    log "installing docker via get.docker.com..."
    curl -fsSL https://get.docker.com | sh
}

# ---------------------------------------------------------------------------
# docker daemon config: log rotation so logs don't fill the disk
# ---------------------------------------------------------------------------
configure_docker_daemon() {
    mkdir -p /etc/docker
    if [ -f /etc/docker/daemon.json ] && grep -q '"log-driver"' /etc/docker/daemon.json; then
        log "daemon.json already has log-driver config"
        return 0
    fi
    log "writing /etc/docker/daemon.json (log rotation)"
    cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
JSON
    systemctl restart docker || true
}

# ---------------------------------------------------------------------------
# baseline tools
# ---------------------------------------------------------------------------
install_tools() {
    log "ensuring baseline tools..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -qq -y ca-certificates curl git jq
}

# ---------------------------------------------------------------------------
# systemd unit: auto-start /opt/*/docker-compose.yml stacks on reboot
# ---------------------------------------------------------------------------
install_autostart_unit() {
    local unit=/etc/systemd/system/strands-agents.service
    if [ -f "$unit" ]; then
        log "strands-agents.service already installed"
        return 0
    fi
    log "installing systemd unit: strands-agents.service"
    cat > "$unit" <<'EOF'
[Unit]
Description=Start all strands-pg stacks in /opt on boot
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/bin/bash -c 'for d in /opt/*/; do [ -f "$d/docker-compose.yml" ] && (cd "$d" && /usr/bin/docker compose up -d); done'
ExecStop=/bin/bash -c 'for d in /opt/*/; do [ -f "$d/docker-compose.yml" ] && (cd "$d" && /usr/bin/docker compose down); done'

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable strands-agents.service
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "bootstrap-lxc.sh must run as root" >&2
        exit 1
    fi

    check_lxc_features
    install_tools
    install_docker
    configure_docker_daemon
    install_autostart_unit

    log "done. next steps:"
    log "  cd $(dirname "$(readlink -f "$0")")"
    log "  cp .env.example .env  # edit AWS keys, model ID, etc."
    log "  docker compose up -d --build"
}

main "$@"
