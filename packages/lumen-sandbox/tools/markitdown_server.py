"""Run the pinned upstream stdio server with local reads confined to /work.

The protocol, tool description and schema remain upstream-owned. Only URI access
is narrowed: file URIs use descriptor-relative, no-symlink traversal, and all
other supported schemes retain the Pod's operator-approved egress policy.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

WORKSPACE = "/work"


@contextmanager
def workspace_file(uri: str):
    """Open a regular workspace file without following any path symlinks."""
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Local conversion requires a file:///work/ URI.")
    path = PurePosixPath(unquote(parsed.path, errors="strict"))
    root = PurePosixPath(WORKSPACE)
    if not path.is_absolute() or not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Local conversion is confined to /work.")
    parts = path.relative_to(root).parts
    if not parts:
        raise ValueError("Local conversion requires a regular file.")
    directory = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Local conversion requires a regular file.")
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            yield stream, path.suffix
    finally:
        os.close(directory)


def main() -> None:
    import markitdown_mcp.__main__ as upstream

    class WorkspaceMarkItDown(upstream.MarkItDown):
        def convert_uri(self, uri: str, **kwargs):
            scheme = urlsplit(uri).scheme.lower()
            if scheme == "file":
                with workspace_file(uri) as (stream, extension):
                    return self.convert_stream(stream, file_extension=extension, **kwargs)
            if scheme not in {"http", "https", "data"}:
                raise ValueError("Only http:, https:, data:, and workspace file: URIs are supported.")
            return super().convert_uri(uri, **kwargs)

    upstream.MarkItDown = WorkspaceMarkItDown
    # Never load arbitrary installed converter plugins or serve an HTTP listener.
    os.environ["MARKITDOWN_ENABLE_PLUGINS"] = "false"
    upstream.mcp.run()


if __name__ == "__main__":
    main()
