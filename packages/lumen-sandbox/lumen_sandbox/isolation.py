"""Linux-only bubblewrap, tmpfs and cgroup-v2 workload manager."""

import asyncio
import os
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path

from .preflight import verify_cgroup_delegation

LIMIT = 65536
MAX_ARTIFACT = 5 * 1024 * 1024


class Workload:
    def __init__(self, *, root: Path, workspace_root: Path, uid: int, gid: int, memory_bytes: int, workspace_bytes: int,
                 cpu_quota: int, pids_limit: int, cgroup_parent: Path):
        if (os.geteuid() != 0 or uid <= 0 or gid <= 0 or workspace_bytes <= 0
                or workspace_bytes > memory_bytes // 4 or cpu_quota <= 0 or pids_limit < 2):
            raise ValueError("root manager, non-root workload and positive bounded cpu/memory/pids/workspace required")
        self.root = root
        self.workspace = workspace_root
        self.snapshots = root / "snapshots"
        self.uid, self.gid = uid, gid
        self.memory_bytes, self.workspace_bytes = memory_bytes, workspace_bytes
        self.cpu_quota, self.pids_limit = cpu_quota, pids_limit
        self.cgroup_parent = cgroup_parent
        self.proc: asyncio.subprocess.Process | None = None
        self.cancel_requested = False
        if self.workspace.is_relative_to(root) or self.workspace.parent.stat().st_uid != 0 or not self.workspace.parent.stat().st_mode & stat.S_IXOTH:
            raise ValueError("workspace mountpoint must be outside private state under searchable root-owned parent")
        verify_cgroup_delegation(cgroup_parent)
        self.group = cgroup_parent / ("lumen-" + uuid.uuid4().hex)
        self.group.mkdir(mode=0o700)
        mounted_here = False
        try:
            for name, value in (("memory.max", memory_bytes), ("memory.swap.max", 0), ("pids.max", pids_limit), ("cpu.max", f"{cpu_quota} 100000")):
                (self.group / name).write_text(str(value))
            self.workspace.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(self.workspace.lstat().st_mode):
                raise ValueError("workspace mountpoint must not be a symlink")
            self.snapshots.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.workspace.is_mount():
                if (root / "journal.sqlite").exists():
                    raise ValueError("journal exists but prior workspace mount is lost; replace resource")
                if any(self.workspace.iterdir()):
                    raise ValueError("unmounted workspace contains untrusted data")
                subprocess.run(["/usr/bin/mount", "-t", "tmpfs", "-o", f"size={workspace_bytes},mode=0700,uid={uid},gid={gid},nosuid,nodev,noexec", "tmpfs", str(self.workspace)], check=True)
                mounted_here = True
            mount = next((line for line in Path("/proc/self/mountinfo").read_text().splitlines()
                          if line.split(" - ")[0].split()[4] == str(self.workspace)), None)
            if mount is None or mount.split(" - ")[1].split()[0] != "tmpfs":
                raise ValueError("workspace must be an actual tmpfs mount")
            info = self.workspace.stat()
            volume = os.statvfs(self.workspace)
            if info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o700 or volume.f_blocks * volume.f_frsize > workspace_bytes + volume.f_frsize:
                raise ValueError("workspace tmpfs ownership or size invalid")
            self.runtime_mounts = ["--ro-bind", "/usr", "/usr"]
            node_runtime = Path("/opt/node")
            if node_runtime.is_dir():
                self.runtime_mounts.extend(("--ro-bind", str(node_runtime), str(node_runtime)))
            for name in ("/bin", "/lib", "/lib64"):
                path = Path(name)
                if path.is_symlink():
                    if not path.resolve().is_relative_to("/usr"):
                        raise ValueError("runtime symlink must point into read-only /usr")
                    self.runtime_mounts.extend(("--symlink", os.readlink(path), name))
                elif path.is_dir():
                    self.runtime_mounts.extend(("--ro-bind", name, name))
        except BaseException:
            if mounted_here:
                subprocess.run(["/usr/bin/umount", str(self.workspace)], check=True)
            self.group.rmdir()
            raise

    def kill(self) -> None:
        self.cancel_requested = True
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        (self.group / "cgroup.kill").write_text("1")

    async def quiesce(self) -> None:
        """Stop surviving grandchildren before reading the writable workspace."""
        (self.group / "cgroup.kill").write_text("1")
        for _ in range(100):
            if "populated 0" in (self.group / "cgroup.events").read_text():
                return
            await asyncio.sleep(0.01)
        raise RuntimeError("workload descendants did not terminate")

    def _before_exec(self) -> None:
        os.setsid()
        (self.group / "cgroup.procs").write_text(str(os.getpid()))
        os.setgroups([])
        os.setgid(self.gid)
        os.setuid(self.uid)
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "no_new_privs failed")

    async def execute(self, eid: str, language: str, source: str, timeout: float, *, capture_artifacts: bool = True) -> tuple[str, dict, list[tuple[str, str, int]]]:
        suffix, interpreter = {"python": ("py", "/usr/bin/python3.12"), "javascript": ("js", "/usr/bin/node"), "shell": ("sh", "/bin/sh")}[language]
        filename = f".lumen-{eid}.{suffix}"
        path = self.workspace / filename
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(os.dup(fd), "wb", closefd=True) as output:
                output.write(source.encode("utf-8"))
            os.fchown(fd, self.uid, self.gid)
        finally:
            os.close(fd)
        argv = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                *self.runtime_mounts, "--proc", "/proc", "--dev", "/dev",
                "--bind", str(self.workspace), "/work", "--chdir", "/work", "--", interpreter, "/work/" + filename]
        start = time.monotonic()
        stdout, stderr = bytearray(), bytearray()
        overflow = asyncio.Event()
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "HOME": "/work", "TMPDIR": "/work", "LANG": "C.UTF-8"}, preexec_fn=self._before_exec)
            if self.cancel_requested:
                self.kill()
            async def drain(stream: asyncio.StreamReader, buffer: bytearray) -> None:
                while data := await stream.read(8192):
                    buffer.extend(data[:max(0, LIMIT + 1 - len(buffer))])
                    if len(buffer) > LIMIT:
                        overflow.set()
                        self.kill()
                        break
            out_task = asyncio.create_task(drain(self.proc.stdout, stdout))
            err_task = asyncio.create_task(drain(self.proc.stderr, stderr))
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except TimeoutError:
                self.kill()
                await self.proc.wait()
                state = "timed_out"
            else:
                state = "output_limit_exceeded" if overflow.is_set() else ("cancelled" if self.cancel_requested else ("succeeded" if self.proc.returncode == 0 else "failed"))
            await asyncio.gather(out_task, err_task)
            if overflow.is_set():
                state = "output_limit_exceeded"
            if self.cancel_requested and state != "output_limit_exceeded" and state != "timed_out":
                state = "cancelled"
            await self.quiesce()
            artifacts = self.snapshot(eid) if capture_artifacts and state not in ("cancelled", "timed_out", "output_limit_exceeded") else []
            return state, {"stdout": stdout[:LIMIT].decode("utf-8", "ignore"), "stderr": stderr[:LIMIT].decode("utf-8", "ignore"), "exit_code": self.proc.returncode, "duration_seconds": time.monotonic() - start, "artifacts": [{"id": a, "name": n, "size": s} for a, n, s in artifacts]}, artifacts
        finally:
            path.unlink(missing_ok=True)
            self.proc = None
            self.cancel_requested = False

    def snapshot(self, eid: str) -> list[tuple[str, str, int]]:
        """Reject unsafe entries, then copy via no-follow dirfds after the child has exited."""
        result = []
        target = self.snapshots / eid
        target.mkdir(mode=0o700)
        def visit(directory: Path, rel: str = "") -> None:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith(".lumen-") and not rel:
                        continue
                    name = f"{rel}/{entry.name}" if rel else entry.name
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        visit(Path(entry.path), name)
                    elif stat.S_ISREG(mode):
                        if len(result) >= 20 or entry.stat(follow_symlinks=False).st_size > MAX_ARTIFACT:
                            raise ValueError("artifact count or size exceeded")
                        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                            try:
                                info = os.fstat(fd)
                                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT:
                                    raise ValueError("unsafe artifact")
                                aid = str(uuid.uuid4())
                                with open(target / aid, "xb") as output:
                                    remaining = info.st_size
                                    while remaining:
                                        chunk = os.read(fd, min(65536, remaining))
                                        if not chunk:
                                            raise ValueError("artifact changed during snapshot")
                                        output.write(chunk)
                                        remaining -= len(chunk)
                                    output.flush()
                                    os.fchmod(output.fileno(), 0o400)
                                    os.fsync(output.fileno())
                                result.append((aid, name, info.st_size))
                            finally:
                                os.close(fd)
                        finally:
                            os.close(parent)
                    else:
                        raise ValueError("symlink or special artifact rejected")
        try:
            visit(self.workspace)
            for directory in (target, self.snapshots):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return result
        except BaseException:
            for partial in target.iterdir():
                partial.unlink()
            target.rmdir()
            raise

    def close(self) -> None:
        self.kill()
        self.group.rmdir()
        # Preserve the tmpfs across manager restarts; the resource owner must
        # unmount it when permanently deleting this sandbox generation.
