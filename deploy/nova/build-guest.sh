#!/bin/bash
# Build on a native Ubuntu 24.04 amd64 or arm64 runner with DIB, skopeo and qemu-img.
set -euo pipefail
umask 077

fail() { printf '%s\n' "$*" >&2; exit 1; }
role= arch= image= ubuntu_url= ubuntu_sha256= ubuntu_snapshot= docker_packages= docker_packages_sha256= output=
while (($#)); do
    (($# >= 2)) || fail "Every option requires a value"
    case "$1" in
        --role) role=$2 ;;
        --arch) arch=$2 ;;
        --image) image=$2 ;;
        --ubuntu-url) ubuntu_url=$2 ;;
        --ubuntu-sha256) ubuntu_sha256=$2 ;;
        --ubuntu-snapshot) ubuntu_snapshot=$2 ;;
        --docker-packages) docker_packages=$2 ;;
        --docker-packages-sha256) docker_packages_sha256=$2 ;;
        --output) output=$2 ;;
        *) fail "Unknown option: $1" ;;
    esac
    shift 2
done
[[ $role == api || $role == worker ]] || fail "--role must be api or worker"
[[ $arch == amd64 || $arch == arm64 ]] || fail "--arch must be amd64 or arm64"
[[ -n $image && -n $ubuntu_url && -n $ubuntu_sha256 && -n $ubuntu_snapshot && -n $docker_packages && -n $docker_packages_sha256 && -n $output ]] || fail "All pinned inputs and --output are required"
case "$(uname -m):$arch" in
    x86_64:amd64|aarch64:arm64) ;;
    *) fail "Use a native runner for the requested architecture" ;;
esac
for tool in python3 curl sha256sum dpkg-deb skopeo disk-image-create qemu-img; do
    command -v "$tool" >/dev/null || fail "Missing build tool: $tool"
done
root=$(dirname "$(readlink -f "$0")")
output=$(realpath -m "$output")
[[ $output == *.qcow2 ]] || fail "--output must end in .qcow2"
[[ ! -e $output && ! -e $output.sha256 && ! -e $output.manifest.json ]] || fail "Output already exists"
mkdir -p "$(dirname "$output")"
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
mkdir -p "$staging/packages"
python3 "$root/artifact.py" prepare --role "$role" --arch "$arch" --image "$image" \
    --ubuntu-url "$ubuntu_url" --ubuntu-sha256 "$ubuntu_sha256" \
    --ubuntu-snapshot "$ubuntu_snapshot" \
    --docker-packages "$docker_packages" --docker-packages-sha256 "$docker_packages_sha256" \
    --staging "$staging" > "$staging/packages.tsv"

fetch_verified() {
    local url=$1 expected=$2 destination=$3
    [[ $expected =~ ^[0-9a-f]{64}$ ]] || fail "Missing input SHA-256"
    curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' \
        "$url" --output "$destination"
    printf '%s  %s\n' "$expected" "$destination" | sha256sum --check --strict -
}
fetch_verified "$ubuntu_url" "$ubuntu_sha256" "$staging/ubuntu.squashfs"
while IFS=$'\t' read -r package version url expected; do
    destination="$staging/packages/$package.deb"
    fetch_verified "$url" "$expected" "$destination"
    [[ $(dpkg-deb --field "$destination" Package) == "$package" ]] || fail "Package name mismatch"
    [[ $(dpkg-deb --field "$destination" Version) == "$version" ]] || fail "Package version mismatch"
    package_arch=$(dpkg-deb --field "$destination" Architecture)
    [[ $package_arch == "$arch" || $package_arch == all ]] || fail "Package architecture mismatch"
    printf '%s  %s\n' "$expected" "$package.deb" >> "$staging/packages/SHA256SUMS"
done < "$staging/packages.tsv"

# Verify the caller's source digest, then select and verify one platform manifest.
# Skopeo verifies each downloaded config and layer against its descriptor digest.
skopeo inspect --raw "docker://$image" > "$staging/source-manifest.json"
selected=$(python3 "$root/artifact.py" select-image --image "$image" --arch "$arch" \
    --manifest "$staging/source-manifest.json")
skopeo inspect --raw "docker://$selected" > "$staging/platform-manifest.json"
skopeo copy --override-os linux --override-arch "$arch" "docker://$selected" \
    "docker-archive:$staging/image.tar:lumen-guest:preloaded"
python3 "$root/artifact.py" image-record --staging "$staging" --selected "$selected"

export ELEMENTS_PATH="$root/elements"
export DIB_RELEASE=noble DIB_LOCAL_IMAGE="$staging/ubuntu.squashfs" DIB_LUMEN_STAGING="$staging"
export DIB_LUMEN_SOURCE="$root" DIB_DEBUG_TRACE=0 DIB_CLOUD_INIT_DATASOURCES=OpenStack,ConfigDrive
export DIB_DISTRIBUTION_MIRROR="https://snapshot.ubuntu.com/ubuntu/$ubuntu_snapshot"
export DIB_APT_LOCAL_CACHE=0
# Stock DIB Ubuntu/VM/EFI elements own partitioning and firmware support on both arches.
disk-image-create --offline --no-tmpfs -a "$arch" -t qcow2 -o "${output%.qcow2}" \
    ubuntu vm block-device-efi lumen-guest
[[ -s $output ]] || fail "DIB did not emit a qcow2 image"
(cd "$(dirname "$output")" && sha256sum "$(basename "$output")" > "$(basename "$output").sha256")
python3 "$root/artifact.py" build-manifest --staging "$staging" --output "$output"
printf 'Guest artifact: %s\n' "$output"
