"""Remote MCP with OAuth and delegated Afterglow MCP providers."""
from .afterglow import AfterglowMcp, create_afterglow_plugin
from .remote import RemoteMcp, create_remote_plugin

__all__ = ["AfterglowMcp", "RemoteMcp", "create_afterglow_plugin", "create_remote_plugin"]
