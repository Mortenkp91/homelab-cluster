#!/usr/bin/env bash
# Give the app pods access to the persistent data under /home/mkp/homelab-persistence: the
# directories the static PVs in infrastructure/base/local/local-volume.yaml point at
# (configs/<app>, media, nvr).
#
#   sudo bootstrap/persistence-permissions.sh [base-dir]
#
# Model: the owner of the base directory (you) stays the owner of everything in it. The pods get
# read/write/delete through the group mediagroup (gid 2001), which the media pods run with (PGID,
# runAsGroup and supplementalGroups under apps/base/media; their uid 1001 is mediauser).
#   - dirs  2775 (setgid, so new entries inherit the group)
#   - files 664  (existing execute bits are kept)
#   - default ACL g::rwx, so new files stay group-writable whatever the creating process' umask is
#   - ACL entry for the owner, so you keep full access to files the pods create (those belong to
#     mediauser) and to the config directories some images chown to their own uid at start
# Re-running resets the ownership of such files to the owner again.
#
# Pods mount each directory directly, so nobody needs access to the parent (/home/mkp stays 0700).
# Safe to re-run. Run it again after restoring a backup or copying files in as another user.
set -euo pipefail

BASE=${1:-/home/mkp/homelab-persistence}
NFS_USER=mediauser
NFS_UID=1001
NFS_GROUP=mediagroup
NFS_GID=2001

die() {
  echo "error: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
command -v setfacl >/dev/null || die "setfacl not found (pacman -S acl)"

BASE=$(realpath -e -- "$BASE") || die "base directory does not exist"
# A recursive chown on the wrong directory is hard to undo: only accept a directory that looks
# like the persistence root.
[[ -d $BASE/configs ]] || die "$BASE has no configs/ directory; not the persistence root?"

OWNER=$(stat -c %U "$BASE")
[[ $OWNER != root && $OWNER != UNKNOWN ]] ||
  die "$BASE is owned by $OWNER; chown the directory itself to your user first"

# --- group -----------------------------------------------------------------
if existing=$(getent group "$NFS_GID"); then
  [[ ${existing%%:*} == "$NFS_GROUP" ]] ||
    die "gid $NFS_GID is already used by group '${existing%%:*}'"
elif getent group "$NFS_GROUP" >/dev/null; then
  die "group $NFS_GROUP exists with a gid other than $NFS_GID"
else
  groupadd --gid "$NFS_GID" "$NFS_GROUP"
  echo "created group $NFS_GROUP ($NFS_GID)"
fi

# --- user ------------------------------------------------------------------
if existing=$(getent passwd "$NFS_UID"); then
  [[ ${existing%%:*} == "$NFS_USER" ]] ||
    die "uid $NFS_UID is already used by user '${existing%%:*}'"
  [[ $(id -g "$NFS_USER") == "$NFS_GID" ]] || usermod --gid "$NFS_GID" "$NFS_USER"
elif getent passwd "$NFS_USER" >/dev/null; then
  die "user $NFS_USER exists with a uid other than $NFS_UID"
else
  useradd --uid "$NFS_UID" --gid "$NFS_GID" --no-create-home \
    --home-dir /nonexistent --shell "$(command -v nologin)" "$NFS_USER"
  echo "created user $NFS_USER ($NFS_UID)"
fi

# --- permissions -----------------------------------------------------------
echo "applying permissions under $BASE (owner $OWNER, group $NFS_GROUP) ..."
chown -R -h "$OWNER:$NFS_GROUP" "$BASE"
# X = execute/search only on directories and already-executable files.
# Also clears stale setuid/setgid/world-write bits.
chmod -R u=rwX,g=rwX,o=rX "$BASE"
find "$BASE" -type d -exec chmod g+s {} +
setfacl -R -P -m "u:$OWNER:rwX,g::rwX,m::rwX" "$BASE"
find "$BASE" -type d -exec setfacl -d -m "u::rwx,u:$OWNER:rwx,g::rwx,m::rwx,o::rx" {} +

echo "done:"
ls -ld "$BASE" "$BASE"/*/
