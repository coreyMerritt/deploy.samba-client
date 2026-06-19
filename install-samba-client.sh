#!/usr/bin/env bash

set -euo pipefail

# Verify tools
yq --version 1>/dev/null

# Vars
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_path="${script_dir}/config.yml"

# Functions
function fnGetVar() {
  var_name="$1"
  yq -r ".${var_name}" "$config_path"
}

function fnGetList() {
  var_name="$1"
  yq -r ".${var_name} | join(\" \")" "$config_path"
}

# Load vars
SAMBA_LOCAL_CREDS_PATH="$(fnGetVar local.creds_path)"
SAMBA_SERVER_USERNAME="$(fnGetVar server.username)"
SAMBA_SERVER_PASSWORD="$(fnGetVar server.password)"
SAMBA_SERVER_ADDRESS="$(fnGetVar server.address)"
SAMBA_MOUNT_POINTS="$(fnGetList server.mount_points)"

# Drop creds on our client
cat > "$SAMBA_LOCAL_CREDS_PATH" <<EOF
username=$SAMBA_SERVER_USERNAME
password=$SAMBA_SERVER_PASSWORD
EOF
chmod 600 "$SAMBA_LOCAL_CREDS_PATH"

# Add Mounts
for MOUNT_POINT in $SAMBA_MOUNT_POINTS; do
  MOUNT_NAME="$(basename "$MOUNT_POINT")"
  sudo mkdir -p "$MOUNT_POINT"
  sudo sed -i "\|//${SAMBA_SERVER_ADDRESS}/${MOUNT_NAME}|d" "/etc/fstab"
  FSTAB_LINE="//${SAMBA_SERVER_ADDRESS}/${MOUNT_NAME} ${MOUNT_POINT} cifs credentials=${SAMBA_LOCAL_CREDS_PATH},_netdev,vers=3.1.1,uid=$(id -u),gid=$(id -g),file_mode=0775,dir_mode=0775 0 0"
  echo "$FSTAB_LINE" | sudo tee -a "/etc/fstab" > /dev/null
  unlink "$HOME/${MOUNT_NAME}"
  ln -s "/mnt/${MOUNT_NAME}" "$HOME/${MOUNT_NAME}"
done

# Apply changes
sudo systemctl daemon-reload
sudo mount -a
