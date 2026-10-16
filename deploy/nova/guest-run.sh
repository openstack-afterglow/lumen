#!/bin/bash
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { printf 'Guest launcher requires root\n' >&2; exit 1; }
# Capture first: a failed validator must not be hidden by process substitution.
settings=$(python3 /usr/local/libexec/lumen-guest-settings.py)
mapfile -t fields <<< "$settings"
[[ ${#fields[@]} == 5 ]] || { printf 'Invalid guest settings\n' >&2; exit 1; }
role=${fields[0]} image=${fields[1]} image_id=${fields[2]} architecture=${fields[3]} port=${fields[4]}
# docker load has no registry access. The archive SHA-256 was verified above.
if ! docker image inspect "$image_id" >/dev/null 2>&1; then
    docker image load --input /usr/share/lumen-guest/image.tar
fi
loaded_id=$(docker image inspect --format '{{.Id}}' "$image_id")
loaded_arch=$(docker image inspect --format '{{.Architecture}}' "$image_id")
loaded_os=$(docker image inspect --format '{{.Os}}' "$image_id")
[[ $loaded_id == "$image_id" && $loaded_arch == "$architecture" && $loaded_os == linux ]] || {
    printf 'Loaded image digest/platform mismatch\n' >&2; exit 1;
}
mkdir -p -m 0700 /var/lib/lumen/identity /var/lib/lumen/drain
if [[ $role == api ]]; then
    command=(uvicorn lumen.main:app --host 0.0.0.0 --port "$port")
else
    command=(python -m lumen.worker)
fi
# Foreground docker forwards signals to the root supervisor, which drops its child
# to appuser. It has no Docker socket, no writable host root, and no pull fallback.
exec docker run --rm --name lumen-guest --pull=never --network host --read-only --user 0:0 \
    --security-opt no-new-privileges --cap-drop ALL \
    --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL \
    --mount type=bind,src=/var/lib/lumen/bootstrap,dst=/var/lib/lumen/bootstrap \
    --mount type=bind,src=/var/lib/lumen/identity,dst=/var/lib/lumen/identity \
    --mount type=bind,src=/var/lib/lumen/drain,dst=/var/lib/lumen/drain \
    --tmpfs /run/lumen:rw,noexec,nosuid,nodev,size=16m,mode=0755 \
    --tmpfs /var/lib/lumen/scratch:rw,noexec,nosuid,nodev,size=256m,mode=0700,uid=1000,gid=1000 \
    --tmpfs /var/lib/lumen/spool:rw,noexec,nosuid,nodev,size=1g,mode=0700,uid=1000,gid=1000 \
    --env "LUMEN_GUEST_IMAGE=$image" --env PYTHONDONTWRITEBYTECODE=1 \
    --env TMPDIR=/var/lib/lumen/scratch \
    --entrypoint python "$image_id" -m lumen.services.infrastructure.guest_bootstrap \
    --role "$role" -- "${command[@]}"
