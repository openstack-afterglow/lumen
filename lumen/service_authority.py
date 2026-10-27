"""Lumen UI labels and action projection, evaluated by operator oslo.policy."""

READER_CAPABILITIES = frozenset({"lumen-inventory_reader", "lumen-history_reader"})
SERVICE_CAPABILITIES = READER_CAPABILITIES | {
    "lumen-chat_user", "lumen-images_user", "lumen-audio_user", "lumen-tools_user",
    "lumen-assets_editor", "lumen-agents_editor", "lumen-mcp_editor",
    "lumen-keys_editor", "lumen-history_editor", "lumen-resources_admin",
}

# A tuple is an OR of permissible capabilities; multiple requested scopes are AND.
SCOPE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "models:read": ("lumen-inventory_reader",),
    "usage:read": ("lumen-inventory_reader",),
    "compat:completions:write": ("lumen-chat_user",),
    "compat:images:write": ("lumen-images_user",),
    "compat:audio:write": ("lumen-audio_user",),
    "compat:realtime:write": ("lumen-audio_user",),
    "compat:batches:read": ("lumen-history_reader",),
    "compat:batches:write": ("lumen-chat_user", "lumen-images_user", "lumen-audio_user"),
    "compat:files:read": ("lumen-inventory_reader",),
    "compat:files:write": ("lumen-assets_editor",),
    "compat:files:delete": ("lumen-resources_admin",),
    "native:conversations:read": ("lumen-history_reader",),
    "native:conversations:write": ("lumen-chat_user",),
    "native:conversations:delete": ("lumen-history_editor",),
    "native:runs:read": ("lumen-history_reader",),
    "native:runs:write": ("lumen-chat_user",),
    "native:images:write": ("lumen-images_user",),
    "native:audio:write": ("lumen-audio_user",),
    "native:realtime:write": ("lumen-audio_user",),
    "native:assets:read": ("lumen-inventory_reader",),
    "native:assets:write": ("lumen-assets_editor",),
    "native:assets:delete": ("lumen-resources_admin",),
    "native:batches:read": ("lumen-history_reader",),
    "native:batches:write": ("lumen-chat_user", "lumen-images_user", "lumen-audio_user"),
    "native:extensions:read": ("lumen-inventory_reader",),
    "native:extensions:write": ("lumen-assets_editor",),
    "native:extensions:delete": ("lumen-resources_admin",),
    "native:tools:execute": ("lumen-tools_user",),
    "native:memory:read": ("lumen-history_reader",),
    "native:memory:write": ("lumen-history_editor",),
    "native:memory:delete": ("lumen-history_editor",),
    "native:agents:use": ("lumen-tools_user",),
    "native:agents:read": ("lumen-inventory_reader",),
    "native:agents:write": ("lumen-agents_editor",),
    "native:agents:delete": ("lumen-resources_admin",),
    "native:mcp:write": ("lumen-mcp_editor",),
    "native:keys:read": ("lumen-keys_editor",),
    "native:keys:write": ("lumen-keys_editor",),
    "native:keys:delete": ("lumen-resources_admin",),
}


def capabilities(principal: dict) -> frozenset[str]:
    """UI labels only, never synthetic Keystone roles or a second action gate."""
    scopes = allowed_scopes(principal)
    return frozenset(leaves[0] for scope, leaves in SCOPE_CAPABILITIES.items() if len(leaves) == 1 and scope in scopes)


def allowed_scopes(principal: dict) -> frozenset[str]:
    from lumen.policy import authorize

    return frozenset(scope for scope in SCOPE_CAPABILITIES if authorize(scope, principal))
