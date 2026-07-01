#!/usr/bin/env python3
import argparse
import grp
import os
import pwd
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

MountEntry = dict[str, Any]
FstabItem = dict[str, Any]

FSTAB_PATH = Path("/etc/fstab")
CRED_DIR = Path("/etc/samba/credentials")

def main() -> None:
  parser = argparse.ArgumentParser(description="Set up persistent Samba/CIFS mounts from a YAML config.")
  parser.add_argument("--config", default="samba.yml", help="Path to samba.yml (default: ./samba.yml)")
  parser.add_argument("--dry-run", action="store_true", help="Show what would happen, change nothing")
  parser.add_argument("--no-mount", action="store_true", help="Update fstab/credentials but don't mount now")
  parser.add_argument("--remove", metavar="NAME", help="Remove a single named mount entry from fstab and unmount it")
  parser.add_argument("--status", action="store_true", help="Show current mount status and exit")
  args = parser.parse_args()
  check_root()
  check_dependencies()
  entries = load_config(args.config)
  if args.status:
    show_status(entries)
    return
  if args.remove:
    mp_entry = next((e for e in entries if e["name"] == args.remove), None)
    if mp_entry is None:
      sys.exit(
        f"ERROR: '{args.remove}' not found in {args.config} - the fstab "
        "line is only matched by //server/share, so it must still be "
        "in the config to know what to remove."
      )
    if not args.dry_run:
      subprocess.run(["umount", mp_entry["mount_point"]], capture_output=True, check=False)
      cred_path = CRED_DIR / args.remove
      if cred_path.exists():
        cred_path.unlink()
        log(f"Removed credentials file: {cred_path}")
    update_fstab([{"device_spec": device_spec_for(mp_entry)}], args.dry_run, remove_only=True)
    if not args.dry_run:
      subprocess.run(["systemctl", "daemon-reload"], check=False)
    log(f"Removed mount entry '{args.remove}' from fstab.")
    return
  entries = [e for e in entries if e.get("enabled", True)]
  if not entries:
    log("No enabled mount entries found - nothing to do.")
    return
  items = []
  for entry in entries:
    ensure_mount_point(entry, args.dry_run)
    cred_path = write_credentials_file(entry, args.dry_run)
    items.append({
      "device_spec": device_spec_for(entry),
      "line": build_fstab_line(entry, cred_path),
    })
  update_fstab(items, args.dry_run)
  if not args.no_mount:
    mount_all(args.dry_run)
  if not args.dry_run:
    log("Done. These mounts will now also come up automatically after a reboot")
    log("(via /etc/fstab + _netdev + x-systemd.automount).")

def check_root() -> None:
  if os.geteuid() != 0:
    sys.exit(
      "ERROR: this script must be run as root (it writes to /etc/fstab "
      "and /etc/samba/credentials). Try: sudo python3 samba_mount_manager.py ..."
    )

def check_dependencies() -> None:
  if shutil.which("mount.cifs") is None:
    log(
      "WARNING: mount.cifs not found. Install cifs-utils, e.g.:\n"
      "    sudo apt install cifs-utils      # Debian/Ubuntu\n"
      "    sudo dnf install cifs-utils      # Fedora/RHEL"
    )

def log(msg: str) -> None:
  print(f"[samba-mount-manager] {msg}")

def load_config(path: str) -> list[MountEntry]:
  path = Path(path)
  if not path.exists():
    sys.exit(f"ERROR: config file not found: {path}")
  with open(path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
  mounts = data.get("mounts", [])
  if not mounts:
    sys.exit("ERROR: no 'mounts' entries found in config file")
  base_required = {"name", "server", "share", "mount_point"}
  names_seen = set()
  for m in mounts:
    required = set(base_required)
    if not m.get("guest", False):
      required |= {"username", "password"}
    missing = required - m.keys()
    if missing:
      sys.exit(f"ERROR: mount entry {m.get('name', '?')} missing keys: {missing}")
    if m["name"] in names_seen:
      sys.exit(f"ERROR: duplicate 'name' in config: {m['name']}")
    names_seen.add(m["name"])
  return mounts

def show_status(entries: list[MountEntry]) -> None:
  log("Current mount status:")
  mounts_output = subprocess.run(["findmnt", "-t", "cifs"], capture_output=True, text=True, check=False)
  active = mounts_output.stdout
  for e in entries:
    mp = e["mount_point"]
    state = "MOUNTED" if mp in active else "not mounted"
    print(f"  {e['name']:20s} -> {mp:30s} [{state}]")

def device_spec_for(entry: MountEntry) -> str:
  server = entry["server"]
  share = entry["share"].strip("/")
  return f"//{server}/{share}"

def update_fstab(items: list[FstabItem], dry_run: bool, remove_only: bool = False) -> None:
  original_lines = FSTAB_PATH.read_text(encoding="utf-8").splitlines()
  new_lines = original_lines
  for item in items:
    new_lines, removed = strip_existing_definition(new_lines, item["device_spec"])
    if removed:
      log(f"Found existing /etc/fstab entry for {item['device_spec']} - replacing it")
  # Trim trailing blank lines before appending fresh entries
  while new_lines and new_lines[-1].strip() == "":
    new_lines.pop()
  if not remove_only:
    for item in items:
      new_lines.append("")
      new_lines.append(item["line"])
  new_content = "\n".join(new_lines) + "\n"
  if dry_run:
    log("[dry-run] would write the following /etc/fstab content:")
    print("=" * 180)
    print(new_content)
    print("=" * 180)
    return
  backup_fstab()
  tmp_path = FSTAB_PATH.with_suffix(".tmp")
  tmp_path.write_text(data=new_content, encoding="utf-8")
  os.chmod(tmp_path, 0o644)
  tmp_path.replace(FSTAB_PATH)
  log("Updated /etc/fstab")

def strip_existing_definition(lines: list[str], device_spec: str) -> tuple[list[str], bool]:
  result = []
  i = 0
  n = len(lines)
  removed = False
  while i < n:
    line = lines[i]
    fields = line.strip().split()
    if fields and fields[0] == device_spec:
      removed = True
      i += 1
      if i < n and lines[i].strip() == "":
        i += 1  # also drop the one blank line right after it
      continue
    result.append(line)
    i += 1
  return result, removed

def backup_fstab() -> Path:
  ts = datetime.now().strftime("%Y%m%d-%H%M%S")
  backup_path = FSTAB_PATH.with_suffix(f".bak.{ts}")
  shutil.copy2(FSTAB_PATH, backup_path)
  log(f"Backed up /etc/fstab to {backup_path}")
  return backup_path

def ensure_mount_point(entry: MountEntry, dry_run: bool) -> None:
  mp = Path(entry["mount_point"])
  if mp.exists() and not mp.is_dir():
    sys.exit(f"ERROR: mount point {mp} exists and is not a directory")
  if dry_run:
    if not mp.exists():
      log(f"[dry-run] would create mount point dir: {mp}")
    return
  mp.mkdir(parents=True, exist_ok=True)
  log(f"Ensured mount point exists: {mp}")

def write_credentials_file(entry: MountEntry, dry_run: bool) -> Path | None:
  if entry.get("guest", False):
    return None
  CRED_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
  cred_path = CRED_DIR / entry["name"]
  lines = [
    f"username={entry['username']}",
    f"password={entry['password']}",
  ]
  domain = entry.get("domain")
  if domain:
    lines.append(f"domain={domain}")
  content = "\n".join(lines) + "\n"
  if dry_run:
    log(f"[dry-run] would write credentials file: {cred_path} (mode 600)")
    return cred_path
  cred_path.write_text(content)
  os.chmod(cred_path, 0o600)
  os.chown(cred_path, 0, 0)  # root:root
  log(f"Wrote credentials file: {cred_path}")
  return cred_path

def build_fstab_line(entry: MountEntry, cred_path: Path | None) -> str:
  device_spec = device_spec_for(entry)
  mount_point = entry["mount_point"]
  vers = entry.get("vers", "3.0")
  opts = []
  if entry.get("guest", False):
    opts.append("sec=none")
  else:
    opts.append(f"credentials={cred_path}")
  opts += [
    f"vers={vers}",
    "_netdev",   # wait for network before attempting mount
    "nofail",    # don't block boot if the share is unreachable
    "x-systemd.automount",   # mount on first access, retry lazily
    "x-systemd.mount-timeout=30",
  ]
  uid = resolve_uid(entry.get("uid"))
  gid = resolve_gid(entry.get("gid"))
  if uid is not None:
    opts.append(f"uid={uid}")
  if gid is not None:
    opts.append(f"gid={gid}")
  extra = entry.get("extra_options")
  if extra:
    opts.append(extra.strip(","))
  options_str = ",".join(opts)
  return f"{device_spec}  {mount_point}  cifs  {options_str}  0  0"

def resolve_uid(value: int | str | None) -> int | None:
  if value is None or value == "":
    return None
  if isinstance(value, int):
    return value
  try:
    return pwd.getpwnam(value).pw_uid
  except KeyError:
    sys.exit(f"ERROR: no such local user: {value}")

def resolve_gid(value: int | str | None) -> int | None:
  if value is None or value == "":
    return None
  if isinstance(value, int):
    return value
  try:
    return grp.getgrnam(value).gr_gid
  except KeyError:
    sys.exit(f"ERROR: no such local group: {value}")

def mount_all(dry_run: bool) -> None:
  if dry_run:
    log("[dry-run] would run: systemctl daemon-reload && mount -a")
    return
  # Make systemd pick up the new fstab-derived mount/automount units
  subprocess.run(["systemctl", "daemon-reload"], check=False)
  result = subprocess.run(["mount", "-a"], capture_output=True, text=True, check=False)
  if result.returncode != 0:
    log(
      "WARNING: `mount -a` reported errors (some shares may be unreachable "
      "right now - that's OK, they'll retry via x-systemd.automount / on next boot):"
    )
    print(result.stderr.strip())
  else:
    log("Ran `mount -a` successfully")

if __name__ == "__main__":
  main()