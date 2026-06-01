#!/usr/bin/env bats
# =============================================================================
# test_vps_preflight.bats — POSIX shell unit tests for vps_preflight.sh
#
# Uses bats-core (https://github.com/bats-core/bats-core) to test the
# assertion logic in platform/scripts/vps_preflight.sh by mocking system
# commands (nproc, free, df, ss, lsb_release, docker, python3, git, ufw, etc.)
#
# Requirements validated: R1.1 (CPU, RAM, disk), R1.3 (port availability)
#
# Installation (if bats is not available):
#   git clone https://github.com/bats-core/bats-core.git /tmp/bats
#   /tmp/bats/install.sh /usr/local
#
# Usage:
#   bats platform/tests/test_vps_preflight.bats
#
# Strategy:
#   We source only the PREFLIGHT section of vps_preflight.sh by extracting
#   the relevant functions and logic into a testable helper, or we run the
#   full script with mocked commands on PATH. We use the mock-command approach:
#   create a temp bin directory with mock scripts, prepend it to PATH.
# =============================================================================

# --- Setup / Teardown --------------------------------------------------------

setup() {
  # Create a temporary directory for mock commands
  MOCK_BIN="$(mktemp -d)"
  export PATH="${MOCK_BIN}:${PATH}"

  # Script under test
  SCRIPT_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../scripts" && pwd)"
  SCRIPT_UNDER_TEST="${SCRIPT_DIR}/vps_preflight.sh"

  # We'll create a trimmed version that only runs the PREFLIGHT section
  # to avoid Docker/apt-get side effects from the INSTALL section
  PREFLIGHT_SCRIPT="$(mktemp)"
  # Extract from start to just before "### SECTION: INSTALL ###"
  sed -n '1,/^echo "### SECTION: INSTALL ###"/{ /^echo "### SECTION: INSTALL ###"/!p }' \
    "${SCRIPT_UNDER_TEST}" > "${PREFLIGHT_SCRIPT}"
  # Append a clean exit so the script doesn't fall through
  echo 'exit 0' >> "${PREFLIGHT_SCRIPT}"
  chmod +x "${PREFLIGHT_SCRIPT}"

  # Default mocks: all passing scenario
  _create_mock_nproc 4
  _create_mock_free 8192
  _create_mock_df 50
  _create_mock_ss_empty
  _create_mock_os_release "ubuntu" "24.04"
  _create_mock_date
  _create_mock_lsb_release "Ubuntu" "24.04"
}

teardown() {
  rm -rf "${MOCK_BIN}"
  rm -f "${PREFLIGHT_SCRIPT}"
}

# --- Mock Creators -----------------------------------------------------------

_create_mock_nproc() {
  local cores="${1:-4}"
  cat > "${MOCK_BIN}/nproc" <<SCRIPT
#!/bin/bash
echo "${cores}"
SCRIPT
  chmod +x "${MOCK_BIN}/nproc"
}

_create_mock_free() {
  # $1 = total RAM in MiB
  local total="${1:-8192}"
  cat > "${MOCK_BIN}/free" <<SCRIPT
#!/bin/bash
if [[ "\$1" == "-m" ]]; then
  echo "              total        used        free      shared  buff/cache   available"
  echo "Mem:          ${total}        2048        4096         128        2048        5900"
  echo "Swap:         4096           0        4096"
else
  echo "              total        used        free      shared  buff/cache   available"
  echo "Mem:       $((${total} * 1024))     2097152     4194304      131072     2097152     6041600"
  echo "Swap:      4194304           0     4194304"
fi
SCRIPT
  chmod +x "${MOCK_BIN}/free"
}

_create_mock_df() {
  # $1 = free disk in GiB
  local free_gib="${1:-50}"
  cat > "${MOCK_BIN}/df" <<SCRIPT
#!/bin/bash
if [[ "\$*" == *"-BG"* ]]; then
  echo "Filesystem      1G-blocks  Used Available Use% Mounted on"
  echo "/dev/sda1            160G   80G      ${free_gib}G  50% /"
elif [[ "\$*" == *"-h"* ]]; then
  echo "Filesystem      Size  Used Avail Use% Mounted on"
  echo "/dev/sda1       160G   80G   ${free_gib}G  50% /"
fi
SCRIPT
  chmod +x "${MOCK_BIN}/df"
}

_create_mock_ss_empty() {
  # No listeners on any port
  cat > "${MOCK_BIN}/ss" <<'SCRIPT'
#!/bin/bash
echo "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
SCRIPT
  chmod +x "${MOCK_BIN}/ss"
}

_create_mock_ss_with_port() {
  # $1 = port that is occupied
  local occupied_port="${1:-80}"
  cat > "${MOCK_BIN}/ss" <<SCRIPT
#!/bin/bash
# Parse the sport filter from arguments
FILTER="\$*"
if echo "\${FILTER}" | grep -q "sport = :${occupied_port}"; then
  echo "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
  echo "LISTEN 0      128    0.0.0.0:${occupied_port}       0.0.0.0:*     users:((\"nginx\",pid=1234,fd=6))"
else
  echo "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
fi
SCRIPT
  chmod +x "${MOCK_BIN}/ss"
}

_create_mock_os_release() {
  # $1 = OS ID, $2 = version
  local os_id="${1:-ubuntu}"
  local os_version="${2:-24.04}"

  # Create a fake /etc/os-release that the script can source
  # We override by creating a wrapper that sets the variables
  export MOCK_OS_ID="${os_id}"
  export MOCK_OS_VERSION="${os_version}"

  # The script sources /etc/os-release directly, so we need to patch the script
  # to use our mock. We'll use a sed replacement in the preflight script.
  sed -i "s|/etc/os-release|${MOCK_BIN}/os-release|g" "${PREFLIGHT_SCRIPT}"

  cat > "${MOCK_BIN}/os-release" <<SCRIPT
ID=${os_id}
VERSION_ID=${os_version}
NAME="Mock OS"
SCRIPT
}

_create_mock_lsb_release() {
  local distro="${1:-Ubuntu}"
  local version="${2:-24.04}"
  cat > "${MOCK_BIN}/lsb_release" <<SCRIPT
#!/bin/bash
case "\$1" in
  -is) echo "${distro}" ;;
  -rs) echo "${version}" ;;
  -a)
    echo "Distributor ID: ${distro}"
    echo "Release:        ${version}"
    echo "Codename:       noble"
    ;;
esac
SCRIPT
  chmod +x "${MOCK_BIN}/lsb_release"
}

_create_mock_date() {
  cat > "${MOCK_BIN}/date" <<'SCRIPT'
#!/bin/bash
echo "2025-01-15T10:00:00Z"
SCRIPT
  chmod +x "${MOCK_BIN}/date"
}

# =============================================================================
# TEST: R1.1 — CPU cores assertion
# =============================================================================

@test "R1.1: PASS when CPU cores >= 4" {
  _create_mock_nproc 4
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"CPU_CORES=4"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=PASS"* ]]
}

@test "R1.1: PASS when CPU cores = 8 (exceeds minimum)" {
  _create_mock_nproc 8
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"CPU_CORES=8"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=PASS"* ]]
}

@test "R1.1: FAIL when CPU cores < 4" {
  _create_mock_nproc 2
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"CPU_CORES=2"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
  [[ "$output" == *"PREFLIGHT_FAIL_REASON=CPU cores 2 < 4"* ]]
}

@test "R1.1: FAIL when CPU cores = 1" {
  _create_mock_nproc 1
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
}

# =============================================================================
# TEST: R1.1 — RAM assertion
# =============================================================================

@test "R1.1: PASS when RAM >= 7500 MiB" {
  _create_mock_free 8192
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"RAM_TOTAL_MIB=8192"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=PASS"* ]]
}

@test "R1.1: PASS when RAM = 7500 MiB (exact boundary)" {
  _create_mock_free 7500
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"RAM_TOTAL_MIB=7500"* ]]
}

@test "R1.1: FAIL when RAM < 7500 MiB" {
  _create_mock_free 4096
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"RAM_TOTAL_MIB=4096"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
  [[ "$output" == *"PREFLIGHT_FAIL_REASON=RAM 4096 MiB < 7500 MiB"* ]]
}

# =============================================================================
# TEST: R1.1 — Disk space assertion
# =============================================================================

@test "R1.1: PASS when disk free >= 20 GiB" {
  _create_mock_df 50
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DISK_FREE_GIB=50"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=PASS"* ]]
}

@test "R1.1: PASS when disk free = 20 GiB (exact boundary)" {
  _create_mock_df 20
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DISK_FREE_GIB=20"* ]]
}

@test "R1.1: FAIL when disk free < 20 GiB" {
  _create_mock_df 10
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"DISK_FREE_GIB=10"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
  [[ "$output" == *"PREFLIGHT_FAIL_REASON=Disk free 10 GiB < 20 GiB"* ]]
}

# =============================================================================
# TEST: R1.2 — OS family and version assertion
# =============================================================================

@test "R1.2: PASS when OS is ubuntu >= 22.04" {
  _create_mock_os_release "ubuntu" "24.04"
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OS_ID=ubuntu"* ]]
  [[ "$output" == *"OS_VERSION=24.04"* ]]
}

@test "R1.2: PASS when OS is ubuntu 22.04 (exact boundary)" {
  _create_mock_os_release "ubuntu" "22.04"
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OS_ID=ubuntu"* ]]
  [[ "$output" == *"OS_VERSION=22.04"* ]]
}

@test "R1.2: FAIL when OS is not ubuntu" {
  _create_mock_os_release "debian" "12"
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"OS_ID=debian"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
  [[ "$output" == *"PREFLIGHT_FAIL_REASON=OS is 'debian', expected 'ubuntu'"* ]]
}

@test "R1.2: FAIL when ubuntu version < 22.04" {
  _create_mock_os_release "ubuntu" "20.04"
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
  [[ "$output" == *"Ubuntu version 20.04 < 22.04"* ]]
}

# =============================================================================
# TEST: R1.3 — Port availability (all free)
# =============================================================================

@test "R1.3: PASS when all required ports are free" {
  _create_mock_ss_empty
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"PORT_80=FREE"* ]]
  [[ "$output" == *"PORT_443=FREE"* ]]
  [[ "$output" == *"PORT_3000=FREE"* ]]
  [[ "$output" == *"PORT_5432=FREE"* ]]
  [[ "$output" == *"PORT_8082=FREE"* ]]
  [[ "$output" == *"PORT_8200=FREE"* ]]
  [[ "$output" == *"PORT_8090=FREE"* ]]
  [[ "$output" == *"PORT_8501=FREE"* ]]
  [[ "$output" == *"PORT_7233=FREE"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=PASS"* ]]
}

# =============================================================================
# TEST: R1.3 / R1.4 — Port conflict detection
# =============================================================================

@test "R1.3: FAIL when port 80 is occupied" {
  _create_mock_ss_with_port 80
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PORT_CONFLICT_80=OCCUPIED"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
}

@test "R1.3: FAIL when port 5432 is occupied" {
  _create_mock_ss_with_port 5432
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PORT_CONFLICT_5432=OCCUPIED"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
}

@test "R1.3: FAIL when port 8090 is occupied" {
  _create_mock_ss_with_port 8090
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PORT_CONFLICT_8090=OCCUPIED"* ]]
  [[ "$output" == *"PREFLIGHT_RESULT=FAIL"* ]]
}

@test "R1.4: occupied port reports PID and process name" {
  _create_mock_ss_with_port 80
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PORT_CONFLICT_80_PID="* ]]
  [[ "$output" == *"PORT_CONFLICT_80_PROCESS="* ]]
}

# =============================================================================
# TEST: Combined pass scenario
# =============================================================================

@test "Full preflight PASS: 4 cores, 8 GiB RAM, 50 GiB disk, ubuntu 24.04, all ports free" {
  _create_mock_nproc 4
  _create_mock_free 8192
  _create_mock_df 50
  _create_mock_ss_empty
  _create_mock_os_release "ubuntu" "24.04"
  run bash "${PREFLIGHT_SCRIPT}"
  [ "$status" -eq 0 ]
  [[ "$output" == *"PREFLIGHT_RESULT=PASS"* ]]
  [[ "$output" == *"CPU_CORES=4"* ]]
  [[ "$output" == *"RAM_TOTAL_MIB=8192"* ]]
  [[ "$output" == *"DISK_FREE_GIB=50"* ]]
  [[ "$output" == *"OS_ID=ubuntu"* ]]
}
