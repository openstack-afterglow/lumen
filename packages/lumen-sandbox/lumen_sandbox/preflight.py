"""Fail-closed host prerequisite checks, never a host execution fallback."""

import json
import os
import platform
import stat
import subprocess
from pathlib import Path


class PreflightError(RuntimeError):
    pass

def verify_cgroup_delegation(cgroup_parent: Path) -> None:
    if not {"cpu", "memory", "pids"}.issubset(set((cgroup_parent / "cgroup.controllers").read_text().split())):
        raise PreflightError("cpu/memory/pids cgroup controllers unavailable")
    if not os.access(cgroup_parent, os.W_OK):
        raise PreflightError("cgroup parent not writable")
    if not {"cpu", "memory", "pids"}.issubset(set((cgroup_parent / "cgroup.subtree_control").read_text().split())):
        raise PreflightError("cpu/memory/pids cgroup controllers not delegated to workloads")


def verify(*, uid: int, gid: int, memory_bytes: int, workspace_bytes: int, cgroup_parent: Path,
           node_major: int, node_version: str, runtime_firewall: bool = True) -> None:
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise PreflightError("Linux root manager required")
    if uid <= 0 or gid <= 0 or workspace_bytes <= 0 or workspace_bytes > memory_bytes // 4:
        raise PreflightError("unprivileged workload UID/GID and bounded workspace required")
    for path in ("/usr/bin/python3.12", "/usr/bin/node", "/bin/sh", "/usr/bin/bwrap", "/usr/bin/mount", "/usr/bin/umount", "/usr/sbin/nft"):
        if not Path(path).exists():
            raise PreflightError(f"missing required image binary: {path}")
    python_version = subprocess.run(["/usr/bin/python3.12", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    node_output = subprocess.run(["/usr/bin/node", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    if (not python_version.startswith("Python 3.12.") or not node_version.startswith(f"{node_major}.")
            or node_output != "v" + node_version):
        raise PreflightError("unexpected pinned Python or Node version")
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise PreflightError("cgroup v2 required")
    verify_cgroup_delegation(cgroup_parent)
    if runtime_firewall:
        verify_firewall()


def verify_firewall() -> None:
    """Require default-drop output with only established/related return traffic."""
    try:
        ruleset = json.loads(subprocess.run(["/usr/sbin/nft", "-j", "list", "chain", "inet", "lumen_sandbox", "output"], check=True, capture_output=True, text=True).stdout)["nftables"]
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise PreflightError("runtime nftables output policy missing") from exc
    chain = next((item["chain"] for item in ruleset if "chain" in item), None)
    if (not chain or chain.get("family") != "inet" or chain.get("table") != "lumen_sandbox"
            or chain.get("name") != "output" or chain.get("hook") != "output"
            or chain.get("type") != "filter" or chain.get("policy") != "drop"):
        raise PreflightError("guest outbound default-drop policy required")
    rules = [item["rule"] for item in ruleset if "rule" in item]
    if len(rules) != 1 or len(rules[0].get("expr", [])) != 2:
        raise PreflightError("only established return traffic may leave the guest")
    match, verdict = rules[0]["expr"]
    condition = match.get("match", {})
    right = condition.get("right")
    if isinstance(right, dict) and set(right) == {"set"}:
        right = right["set"]
    if (verdict != {"accept": None} or condition.get("op") != "in"
            or condition.get("left") != {"ct": {"key": "state"}}
            or not isinstance(right, list) or len(right) != 2
            or set(right) != {"established", "related"}):
        raise PreflightError("only established return traffic may leave the guest")


def activate_runtime_firewall() -> None:
    # The image provisions an output base chain with policy drop. Remove the
    # one-shot bootstrap exception before serving even a single request.
    subprocess.run(["/usr/sbin/nft", "-f", "-"], input=(
        "flush chain inet lumen_sandbox output\n"
        "add rule inet lumen_sandbox output ct state established,related accept\n"
    ), text=True, check=True)
    verify_firewall()

async def verify_workload_network(workload, daemon_port: int) -> None:
    """Exercise the actual namespace before any controller dispatch can run."""
    source = (
        "import fcntl, socket, struct\n"
        "interfaces = [line.split(':')[0].strip() for line in open('/proc/net/dev').read().splitlines()[2:]]\n"
        "for interface in interfaces:\n"
        "    if interface == 'lo': continue\n"
        "    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:\n"
        "        flags = struct.unpack('16sH', fcntl.ioctl(probe.fileno(), 0x8913, struct.pack('16sH', interface.encode(), 0))[:18])[1]\n"
        "    assert not flags & 1, (interface, flags)\n"
        "for family, address in [(socket.AF_INET, ('169.254.169.254', 80)), "
        "(socket.AF_INET6, ('fd00:ec2::254', 80)), "
        f"(socket.AF_INET, ('127.0.0.1', {daemon_port})), "
        f"(socket.AF_INET6, ('::1', {daemon_port})), "
        "(socket.AF_INET, ('1.1.1.1', 443))]:\n"
        "    sock = socket.socket(family, socket.SOCK_STREAM)\n"
        "    sock.settimeout(0.2)\n"
        "    try:\n"
        "        assert sock.connect_ex(address) != 0, address\n"
        "    finally:\n"
        "        sock.close()\n"
        "print('network-denied')\n"
    )
    state, detail, _ = await workload.execute("preflight-network", "python", source, 5, capture_artifacts=False)
    if state != "succeeded" or detail.get("stdout", "").strip() != "network-denied":
        raise PreflightError(f"workload namespace network isolation failed: {state}; stdout={detail.get('stdout', '')[:256]!r}; stderr={detail.get('stderr', '')[:256]!r}")

def verify_private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise PreflightError("daemon state and bootstrap parent must be root-owned 0700 directories")


def verify_bootstrap_file(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise PreflightError("bootstrap must be a root-owned 0600 regular file")
        data = os.read(fd, 4097)
        if not data or len(data) > 4096:
            raise PreflightError("bootstrap token length invalid")
        return data.strip()
    finally:
        os.close(fd)
