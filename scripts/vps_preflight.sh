#!/usr/bin/env bash
# =============================================================================
# vps_preflight.sh — VPS-side pre-flight & software-install payload
# Runs on VPS_Host (Ubuntu). Called by vps_preflight.ps1 via SSH stdin pipe.
#
# Outputs structured KEY=VALUE lines to stdout.
# Section markers allow the local launcher to split output into evidence files:
#   01-preflight.txt  (### SECTION: PREFLIGHT ###)
#   02-install.txt    (### SECTION: INSTALL ###)
#
# Exit codes:
#   0 = all assertions pass
#   1 = pre-flight assertion failure (port conflict, hardware, OS)
#   2 = software-install assertion failure
#
# Requirements: R1.1, R1.2, R1.3, R1.4, R1.5, R2.1, R2.2, R2.3, R2.4, R2.5, R2.6
# =============================================================================
set -euo pipefail

# --- Helpers -----------------------------------------------------------------

fail_preflight() {
  echo "PREFLIGHT_RESULT=FAIL"
  echo "PREFLIGHT_FAIL_REASON=$1"
  exit 1
}

fail_install() {
  echo "INSTALL_RESULT=FAIL"
  echo "INSTALL_FAIL_REASON=$1"
  exit 2
}

# =============================================================================
# SECTION: PREFLIGHT (R1.1 – R1.5)
# =============================================================================
echo "### SECTION: PREFLIGHT ###"
echo "PREFLIGHT_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- R1.1: Hardware assertions -----------------------------------------------

# CPU cores
CPU_CORES=$(nproc)
echo "CPU_CORES=${CPU_CORES}"
if [ "${CPU_CORES}" -lt 4 ]; then
  fail_preflight "CPU cores ${CPU_CORES} < 4"
fi

# Total RAM (MiB)
RAM_TOTAL_MIB=$(free -m | awk '/^Mem:/ {print $2}')
echo "RAM_TOTAL_MIB=${RAM_TOTAL_MIB}"
if [ "${RAM_TOTAL_MIB}" -lt 7500 ]; then
  fail_preflight "RAM ${RAM_TOTAL_MIB} MiB < 7500 MiB"
fi

# Free disk on / (GiB)
DISK_FREE_GIB=$(df -BG / | awk 'NR==2 {gsub(/G/,"",$4); print $4}')
echo "DISK_FREE_GIB=${DISK_FREE_GIB}"
if [ "${DISK_FREE_GIB}" -lt 20 ]; then
  fail_preflight "Disk free ${DISK_FREE_GIB} GiB < 20 GiB"
fi

# --- R1.2: OS assertion -------------------------------------------------------

if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_VERSION="${VERSION_ID:-0}"
else
  OS_ID=$(lsb_release -is 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "unknown")
  OS_VERSION=$(lsb_release -rs 2>/dev/null || echo "0")
fi

echo "OS_ID=${OS_ID}"
echo "OS_VERSION=${OS_VERSION}"

if [ "${OS_ID}" != "ubuntu" ]; then
  fail_preflight "OS is '${OS_ID}', expected 'ubuntu'"
fi

# Compare version: major.minor >= 22.04
OS_MAJOR=$(echo "${OS_VERSION}" | cut -d. -f1)
OS_MINOR=$(echo "${OS_VERSION}" | cut -d. -f2)
if [ "${OS_MAJOR}" -lt 22 ]; then
  fail_preflight "Ubuntu version ${OS_VERSION} < 22.04"
elif [ "${OS_MAJOR}" -eq 22 ] && [ "${OS_MINOR}" -lt 4 ]; then
  fail_preflight "Ubuntu version ${OS_VERSION} < 22.04"
fi

# --- R1.3 / R1.4: Port availability ------------------------------------------

REQUIRED_PORTS="80 443 3000 5432 8082 8200 8090 8501 7233"
echo "REQUIRED_PORTS=${REQUIRED_PORTS}"

PORT_CONFLICT=0
for PORT in ${REQUIRED_PORTS}; do
  # ss -ltnp: listening TCP, numeric, show process
  LISTENER=$(ss -ltnp "sport = :${PORT}" 2>/dev/null | grep -v "^State" || true)
  if [ -n "${LISTENER}" ]; then
    # Extract PID and process name
    PID_INFO=$(echo "${LISTENER}" | grep -oP 'pid=\K[0-9]+' | head -1 || echo "unknown")
    PROC_NAME=$(echo "${LISTENER}" | grep -oP 'users:\(\("\K[^"]+' | head -1 || echo "unknown")
    echo "PORT_CONFLICT_${PORT}=OCCUPIED"
    echo "PORT_CONFLICT_${PORT}_PID=${PID_INFO}"
    echo "PORT_CONFLICT_${PORT}_PROCESS=${PROC_NAME}"
    PORT_CONFLICT=1
  else
    echo "PORT_${PORT}=FREE"
  fi
done

if [ "${PORT_CONFLICT}" -eq 1 ]; then
  fail_preflight "One or more required ports are occupied (see PORT_CONFLICT_* lines above)"
fi

# --- R1.5: Capture raw outputs for evidence -----------------------------------

echo "RAW_NPROC=$(nproc)"
echo "RAW_FREE_M<<EOF"
free -m
echo "EOF"
echo "RAW_DF_H<<EOF"
df -h /
echo "EOF"

if command -v lsb_release &>/dev/null; then
  echo "RAW_LSB_RELEASE<<EOF"
  lsb_release -a 2>/dev/null
  echo "EOF"
else
  echo "RAW_OS_RELEASE<<EOF"
  cat /etc/os-release
  echo "EOF"
fi

echo "RAW_SS_LTNP<<EOF"
ss -ltnp
echo "EOF"

echo "PREFLIGHT_RESULT=PASS"
echo "PREFLIGHT_COMPLETED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# =============================================================================
# SECTION: INSTALL (R2.1 – R2.6)
# =============================================================================
echo "### SECTION: INSTALL ###"
echo "INSTALL_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- R2.1 / R2.2: Docker Engine + Compose v2 ---------------------------------

if ! command -v docker &>/dev/null; then
  echo "DOCKER_INSTALLED=false"
  echo "DOCKER_INSTALLING=true"
  # Install Docker from official repository
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg lsb-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME:-noble}") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  echo "DOCKER_INSTALLED_NOW=true"
fi

# Assert Docker version >= 27.0
DOCKER_VERSION_RAW=$(docker --version)
echo "DOCKER_VERSION_RAW=${DOCKER_VERSION_RAW}"
DOCKER_MAJOR=$(echo "${DOCKER_VERSION_RAW}" | grep -oP '\d+' | head -1)
echo "DOCKER_MAJOR=${DOCKER_MAJOR}"
if [ "${DOCKER_MAJOR}" -lt 27 ]; then
  fail_install "Docker version ${DOCKER_VERSION_RAW} — major ${DOCKER_MAJOR} < 27"
fi

# Assert Docker Compose v2 >= 2.27
# Note: Docker Compose v2 plugin versions may appear as v2.x or v5.x+ (new scheme).
# v5.x is a successor to v2.x and satisfies the >= 2.27 requirement.
COMPOSE_VERSION_RAW=$(docker compose version)
echo "COMPOSE_VERSION_RAW=${COMPOSE_VERSION_RAW}"
# Extract major.minor from version string (e.g., "v2.27" -> 2 27, "v5.1" -> 5 1)
COMPOSE_MAJOR=$(echo "${COMPOSE_VERSION_RAW}" | grep -oP 'v\K\d+' | head -1)
COMPOSE_MINOR=$(echo "${COMPOSE_VERSION_RAW}" | grep -oP 'v\d+\.\K\d+' | head -1)
echo "COMPOSE_MAJOR=${COMPOSE_MAJOR}"
echo "COMPOSE_MINOR=${COMPOSE_MINOR}"
if [ -z "${COMPOSE_MAJOR}" ] || [ -z "${COMPOSE_MINOR}" ]; then
  fail_install "Cannot parse Docker Compose version from: ${COMPOSE_VERSION_RAW}"
fi
# v5+ is the new versioning scheme for Compose v2 plugin and satisfies >= 2.27
# v2.x must have minor >= 27
if [ "${COMPOSE_MAJOR}" -lt 2 ]; then
  fail_install "Docker Compose major version ${COMPOSE_MAJOR} < 2 (full: ${COMPOSE_VERSION_RAW})"
elif [ "${COMPOSE_MAJOR}" -eq 2 ] && [ "${COMPOSE_MINOR}" -lt 27 ]; then
  fail_install "Docker Compose version 2.${COMPOSE_MINOR} < 2.27 (full: ${COMPOSE_VERSION_RAW})"
fi
echo "COMPOSE_VERSION_CHECK=PASS"

# --- R2.3: Python >= 3.12 + git -----------------------------------------------

if ! command -v python3 &>/dev/null; then
  echo "PYTHON3_INSTALLED=false"
  echo "PYTHON3_INSTALLING=true"
  apt-get update -qq
  apt-get install -y -qq python3 python3-pip python3-venv git
  echo "PYTHON3_INSTALLED_NOW=true"
elif ! command -v git &>/dev/null; then
  apt-get install -y -qq git
fi

PYTHON_VERSION_RAW=$(python3 --version)
echo "PYTHON_VERSION_RAW=${PYTHON_VERSION_RAW}"
PYTHON_MAJOR=$(echo "${PYTHON_VERSION_RAW}" | grep -oP '\d+' | sed -n '1p')
PYTHON_MINOR=$(echo "${PYTHON_VERSION_RAW}" | grep -oP '\d+' | sed -n '2p')
echo "PYTHON_MAJOR=${PYTHON_MAJOR}"
echo "PYTHON_MINOR=${PYTHON_MINOR}"
if [ "${PYTHON_MAJOR}" -ne 3 ]; then
  fail_install "Python major version ${PYTHON_MAJOR} != 3"
fi
if [ "${PYTHON_MINOR}" -lt 12 ]; then
  fail_install "Python minor version ${PYTHON_MINOR} < 12 (full: ${PYTHON_VERSION_RAW})"
fi

GIT_VERSION_RAW=$(git --version)
echo "GIT_VERSION_RAW=${GIT_VERSION_RAW}"

# --- R2.4: ufw configuration --------------------------------------------------

if ! command -v ufw &>/dev/null; then
  echo "UFW_INSTALLED=false"
  apt-get install -y -qq ufw
fi

# Configure ufw: deny inbound by default, allow 22/80/443
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow 22/tcp >/dev/null 2>&1
ufw allow 80/tcp >/dev/null 2>&1
ufw allow 443/tcp >/dev/null 2>&1
ufw --force enable >/dev/null 2>&1

UFW_STATUS=$(ufw status verbose)
echo "UFW_STATUS<<EOF"
echo "${UFW_STATUS}"
echo "EOF"

# Verify allow-list contains 22, 80, 443
UFW_OK=1
for EXPECTED_PORT in 22 80 443; do
  if ! echo "${UFW_STATUS}" | grep -qP "^\s*${EXPECTED_PORT}/tcp\s+ALLOW IN"; then
    echo "UFW_MISSING_ALLOW=${EXPECTED_PORT}/tcp"
    UFW_OK=0
  fi
done

if [ "${UFW_OK}" -eq 0 ]; then
  fail_install "ufw allow-list does not contain all required ports (22/80/443)"
fi
echo "UFW_ALLOW_22_80_443=VERIFIED"

# --- R2.5: Swap provisioning (if RAM <= 8 GiB) --------------------------------

RAM_TOTAL_MIB_RECHECK=$(free -m | awk '/^Mem:/ {print $2}')
# 8 GiB = 8192 MiB; use threshold 8192 to match "≤ 8 GiB"
if [ "${RAM_TOTAL_MIB_RECHECK}" -le 8192 ]; then
  echo "SWAP_NEEDED=true"
  if [ -f /swapfile ] && swapon --show | grep -q '/swapfile'; then
    echo "SWAP_ALREADY_ACTIVE=true"
    SWAP_SIZE=$(swapon --show=SIZE --noheadings --bytes | head -1)
    echo "SWAP_SIZE_BYTES=${SWAP_SIZE}"
  else
    echo "SWAP_PROVISIONING=true"
    # Allocate 4 GiB swap
    swapoff /swapfile 2>/dev/null || true
    rm -f /swapfile
    fallocate -l 4G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    # Persist in /etc/fstab
    if ! grep -q '/swapfile' /etc/fstab; then
      echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    echo "SWAP_PROVISIONED=true"
    echo "SWAP_SIZE_GIB=4"
  fi
else
  echo "SWAP_NEEDED=false"
fi

SWAPON_SHOW=$(swapon --show 2>/dev/null || echo "none")
echo "SWAPON_SHOW<<EOF"
echo "${SWAPON_SHOW}"
echo "EOF"

# --- R2.6: Final evidence capture ---------------------------------------------

echo "INSTALL_DOCKER_VERSION=$(docker --version)"
echo "INSTALL_COMPOSE_VERSION=$(docker compose version)"
echo "INSTALL_PYTHON_VERSION=$(python3 --version)"
echo "INSTALL_GIT_VERSION=$(git --version)"

echo "INSTALL_RESULT=PASS"
echo "INSTALL_COMPLETED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
