#!/bin/bash

SUDO=${SUDO:-"sudo"}

CEPHADM=${CEPHADM:-"$HOME/cephtest/cephadm"}

if ! [ -x "$CEPHADM" ]; then
    echo "cephadm not found. Please set \$CEPHADM"
    exit 1
fi

echo "Using cephadm: $CEPHADM"

echo "=== ENVIRONMENT VARIABLE DEBUG ==="
echo "BASE_IMAGE='$BASE_IMAGE'"
echo "TARGET_IMAGE='$TARGET_IMAGE'"  
echo "BASE_IMAGE_NAME='$BASE_IMAGE_NAME'"
echo "TARGET_IMAGE_NAME='$TARGET_IMAGE_NAME'"
echo "TARGET_VERSION='$TARGET_VERSION'"
echo "=== END DEBUG ==="

echo "PRE-UPGRADE STATE:"
$SUDO $CEPHADM shell -- ceph version
$SUDO $CEPHADM shell -- ceph orch ps
$SUDO $CEPHADM shell -- ceph -s

echo "Starting upgrade from $BASE_IMAGE_NAME to $TARGET_IMAGE_NAME..."
$SUDO $CEPHADM shell -- ceph orch upgrade start --image "$TARGET_IMAGE"
sleep 30

echo "Starting upgrade monitoring..."
# Find the monitor script in the workunit directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_SCRIPT="$SCRIPT_DIR/upgrade_monitor.sh"

if [ ! -x "$MONITOR_SCRIPT" ]; then
    echo "ERROR: Monitor script not found or not executable: $MONITOR_SCRIPT"
    exit 1
fi

"$MONITOR_SCRIPT" "$TARGET_VERSION" "upgrade" "$BASE_IMAGE_NAME" "$TARGET_IMAGE_NAME" "2400" "$CEPHADM" "$SUDO"

echo "POST-UPGRADE STATE:"
$SUDO $CEPHADM shell -- ceph version
$SUDO $CEPHADM shell -- ceph orch ps
$SUDO $CEPHADM shell -- ceph -s
