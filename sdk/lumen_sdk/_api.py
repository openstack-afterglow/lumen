"""Transport-neutral Lumen v1 route mixin."""

from __future__ import annotations

from urllib.parse import quote


def _segment(value: object) -> str:
    return quote(str(value), safe="")


def _query(**kwargs: object) -> dict | None:
    """Drop unset kwargs and return a query-param dict, or ``None`` if empty."""
    filtered = {key: value for key, value in kwargs.items() if value is not None}
    return filtered or None


class _LumenApiMixin:
    def conversations(self, **query):
        return self._json_request("GET", "/v1/conversations", params=_query(**query))

    def create_conversation(self, **attrs):
        return self._json_request("POST", "/v1/conversations", body=attrs)

    def search_conversations(self, **query):
        return self._json_request("GET", "/v1/conversations/search", params=_query(**query))

    def get_conversation(self, conversation_id):
        return self._json_request("GET", f"/v1/conversations/{_segment(conversation_id)}")

    def delete_conversation(self, conversation_id):
        return self._json_request("DELETE", f"/v1/conversations/{_segment(conversation_id)}")

    def set_conversation_workspace(self, conversation_id, **attrs):
        return self._json_request("PATCH", f"/v1/conversations/{_segment(conversation_id)}/workspace", body=attrs)

    def list_messages(self, conversation_id, **query):
        return self._json_request(
            "GET", f"/v1/conversations/{_segment(conversation_id)}/messages", params=_query(**query)
        )

    def set_active_leaf(self, conversation_id, **attrs):
        return self._json_request("PATCH", f"/v1/conversations/{_segment(conversation_id)}/active-leaf", body=attrs)

    def fork_conversation(self, conversation_id, **attrs):
        return self._json_request("POST", f"/v1/conversations/{_segment(conversation_id)}/fork", body=attrs)

    # -- Completions & Runs ---------------------------------------------

    def create_completion(self, conversation_id, idempotency_key: str | None = None, **attrs):
        ik = idempotency_key or attrs.pop("idempotency_key", None)
        headers = {"Idempotency-Key": ik} if ik else None
        return self._json_request(
            "POST",
            f"/v1/conversations/{_segment(conversation_id)}/completions",
            body=attrs,
            headers=headers,
        )

    def regenerate_message(self, conversation_id, message_id, idempotency_key: str | None = None, **attrs):
        ik = idempotency_key or attrs.pop("idempotency_key", None)
        headers = {"Idempotency-Key": ik} if ik else None
        return self._json_request(
            "POST",
            f"/v1/conversations/{_segment(conversation_id)}/messages/{_segment(message_id)}/regenerate",
            body=attrs,
            headers=headers,
        )

    def retry_run(self, conversation_id, run_id, idempotency_key: str | None = None, **attrs):
        ik = idempotency_key or attrs.pop("idempotency_key", None)
        headers = {"Idempotency-Key": ik} if ik else None
        return self._json_request(
            "POST",
            f"/v1/conversations/{_segment(conversation_id)}/runs/{_segment(run_id)}/retry",
            body=attrs,
            headers=headers,
        )

    def temp_completion(self, idempotency_key: str | None = None, **attrs):
        ik = idempotency_key or attrs.pop("idempotency_key", None)
        headers = {"Idempotency-Key": ik} if ik else None
        return self._json_request("POST", "/v1/temp-completions", body=attrs, headers=headers)

    def run_events(
        self,
        run_id,
        after_seq: int | None = None,
        last_event_id: str | None = None,
        **query,
    ):
        seq = after_seq if after_seq is not None else query.pop("after_seq", None)
        lei = last_event_id or query.pop("last_event_id", None)
        params = _query(after_seq=seq, **query)
        headers = {"Last-Event-ID": str(lei)} if lei is not None else None
        return self._stream_request("GET", f"/v1/runs/{_segment(run_id)}/events", params=params, headers=headers)

    def list_conversation_runs(self, conversation_id, **query):
        return self._json_request("GET", f"/v1/conversations/{_segment(conversation_id)}/runs", params=_query(**query))

    def get_temp_thread(self, temp_thread_id):
        return self._json_request("GET", f"/v1/temp-threads/{_segment(temp_thread_id)}")

    def active_runs(self, **query):
        return self._json_request("GET", "/v1/runs", params=_query(**query))

    def get_run(self, run_id):
        return self._json_request("GET", f"/v1/runs/{_segment(run_id)}")

    def resolve_tool_approval(self, run_id, call_id, **attrs):
        return self._json_request("POST", f"/v1/runs/{_segment(run_id)}/approvals/{_segment(call_id)}", body=attrs)

    def resolve_run_interaction(self, run_id, interaction_id, **attrs):
        return self._json_request(
            "POST",
            f"/v1/runs/{_segment(run_id)}/interactions/{_segment(interaction_id)}",
            body=attrs,
        )

    def cancel_run(self, run_id):
        return self._json_request("POST", f"/v1/runs/{_segment(run_id)}/cancel")

    # -- Models & Capabilities ------------------------------------------

    def discovery(self):
        return self._json_request("GET", "/v1/")

    def models(self):
        return self._json_request("GET", "/v1/models")

    def chat_models(self):
        return self._json_request("GET", "/v1/chat/models")

    def capabilities(self, **query):
        return self._json_request("GET", "/v1/capabilities", params=_query(**query))

    # -- Agents ---------------------------------------------------------

    def agents(self, **query):
        return self._json_request("GET", "/v1/agents", params=_query(**query))

    def create_agent(self, **attrs):
        return self._json_request("POST", "/v1/agents", body=attrs)

    def agent_hub(self, **query):
        return self._json_request("GET", "/v1/agents/hub", params=_query(**query))

    def get_agent(self, agent_id):
        return self._json_request("GET", f"/v1/agents/{_segment(agent_id)}")

    def update_agent(self, agent_id, **attrs):
        return self._json_request("PATCH", f"/v1/agents/{_segment(agent_id)}", body=attrs)

    def delete_agent(self, agent_id):
        return self._json_request("DELETE", f"/v1/agents/{_segment(agent_id)}")

    def clone_agent(self, agent_id):
        return self._json_request("POST", f"/v1/agents/{_segment(agent_id)}/clone")

    # -- Workspaces -----------------------------------------------------

    def workspaces(self, **query):
        return self._json_request("GET", "/v1/workspaces", params=_query(**query))

    def create_workspace(self, **attrs):
        return self._json_request("POST", "/v1/workspaces", body=attrs)

    def get_workspace(self, workspace_id):
        return self._json_request("GET", f"/v1/workspaces/{_segment(workspace_id)}")

    def update_workspace(self, workspace_id, **attrs):
        return self._json_request("PATCH", f"/v1/workspaces/{_segment(workspace_id)}", body=attrs)

    def delete_workspace(self, workspace_id):
        return self._json_request("DELETE", f"/v1/workspaces/{_segment(workspace_id)}")

    # -- Memories -------------------------------------------------------

    def memories(self, **query):
        return self._json_request("GET", "/v1/memories", params=_query(**query))

    def create_memory(self, **attrs):
        return self._json_request("POST", "/v1/memories", body=attrs)

    def search_memories(self, **attrs):
        return self._json_request("POST", "/v1/memories/search", body=attrs)

    def update_memory(self, memory_id, **attrs):
        return self._json_request("PATCH", f"/v1/memories/{_segment(memory_id)}", body=attrs)

    def delete_memory(self, memory_id):
        return self._json_request("DELETE", f"/v1/memories/{_segment(memory_id)}")

    # -- Code Workspaces & Git Credentials ------------------------------

    def git_credentials(self, **query):
        return self._json_request("GET", "/v1/git-credentials", params=_query(**query))

    def create_git_credential(self, **attrs):
        return self._json_request("POST", "/v1/git-credentials", body=attrs)

    def update_git_credential(self, credential_id, **attrs):
        return self._json_request("PUT", f"/v1/git-credentials/{_segment(credential_id)}", body=attrs)

    def delete_git_credential(self, credential_id):
        return self._json_request("DELETE", f"/v1/git-credentials/{_segment(credential_id)}")

    def code_workspaces(self, **query):
        return self._json_request("GET", "/v1/code-workspaces", params=_query(**query))

    def create_code_workspace(self, idempotency_key: str | None = None, **attrs):
        ik = idempotency_key or attrs.pop("idempotency_key", None)
        headers = {"Idempotency-Key": ik} if ik else None
        return self._json_request("POST", "/v1/code-workspaces", body=attrs, headers=headers)

    def get_code_workspace(self, workspace_id):
        return self._json_request("GET", f"/v1/code-workspaces/{_segment(workspace_id)}")

    def delete_code_workspace(self, workspace_id):
        return self._json_request("DELETE", f"/v1/code-workspaces/{_segment(workspace_id)}")

    def set_conversation_code_workspace(self, conversation_id, **attrs):
        return self._json_request("PUT", f"/v1/conversations/{_segment(conversation_id)}/code-workspace", body=attrs)

    # -- Assets ---------------------------------------------------------

    def upload_asset(self, file=None, files=None):
        if files is None and file is not None:
            files = {"file": file}
        return self._json_request("POST", "/v1/assets", files=files)

    def get_asset(self, asset_id):
        return self._json_request("GET", f"/v1/assets/{_segment(asset_id)}")

    def download_asset(self, asset_id, allow_redirects: bool = False):
        response = self.request(
            f"/v1/assets/{_segment(asset_id)}/download", "GET", allow_redirects=allow_redirects, raise_exc=True
        )
        if not allow_redirects or response.status_code in (301, 302, 303, 307, 308):
            return response.headers.get("Location")
        return response.content

    def delete_asset(self, asset_id):
        return self._json_request("DELETE", f"/v1/assets/{_segment(asset_id)}")

    # -- Extensions, MCP & Custom Tools ---------------------------------

    def mcp_servers(self, **query):
        return self._json_request("GET", "/v1/mcp-servers", params=_query(**query))

    def create_mcp_server(self, **attrs):
        return self._json_request("POST", "/v1/mcp-servers", body=attrs)

    def update_mcp_server(self, item_id, **attrs):
        return self._json_request("PATCH", f"/v1/mcp-servers/{_segment(item_id)}", body=attrs)

    def delete_mcp_server(self, item_id):
        return self._json_request("DELETE", f"/v1/mcp-servers/{_segment(item_id)}")

    def mcp_server_oauth_status(self, item_id):
        return self._json_request("GET", f"/v1/mcp-servers/{_segment(item_id)}/oauth")

    def start_mcp_server_oauth(self, item_id, **attrs):
        return self._json_request("POST", f"/v1/mcp-servers/{_segment(item_id)}/oauth/start", body=attrs)

    def disconnect_mcp_server_oauth(self, item_id):
        return self._json_request("DELETE", f"/v1/mcp-servers/{_segment(item_id)}/oauth")

    def custom_tools(self, **query):
        return self._json_request("GET", "/v1/custom-tools", params=_query(**query))

    def create_custom_tool(self, **attrs):
        return self._json_request("POST", "/v1/custom-tools", body=attrs)

    def update_custom_tool(self, item_id, **attrs):
        return self._json_request("PATCH", f"/v1/custom-tools/{_segment(item_id)}", body=attrs)

    def delete_custom_tool(self, item_id):
        return self._json_request("DELETE", f"/v1/custom-tools/{_segment(item_id)}")

    def skills(self, **query):
        return self._json_request("GET", "/v1/skills", params=_query(**query))

    def create_skill(self, **attrs):
        return self._json_request("POST", "/v1/skills", body=attrs)

    def update_skill(self, item_id, **attrs):
        return self._json_request("PATCH", f"/v1/skills/{_segment(item_id)}", body=attrs)

    def delete_skill(self, item_id):
        return self._json_request("DELETE", f"/v1/skills/{_segment(item_id)}")

    def admin_mcp_servers(self, **query):
        return self._json_request("GET", "/v1/admin/mcp-servers", params=_query(**query))

    def admin_create_mcp_server(self, **attrs):
        return self._json_request("POST", "/v1/admin/mcp-servers", body=attrs)

    def admin_update_mcp_server(self, item_id, **attrs):
        return self._json_request("PATCH", f"/v1/admin/mcp-servers/{_segment(item_id)}", body=attrs)

    def admin_delete_mcp_server(self, item_id):
        return self._json_request("DELETE", f"/v1/admin/mcp-servers/{_segment(item_id)}")

    def admin_custom_tools(self, **query):
        return self._json_request("GET", "/v1/admin/custom-tools", params=_query(**query))

    def admin_create_custom_tool(self, **attrs):
        return self._json_request("POST", "/v1/admin/custom-tools", body=attrs)

    def admin_update_custom_tool(self, item_id, **attrs):
        return self._json_request("PATCH", f"/v1/admin/custom-tools/{_segment(item_id)}", body=attrs)

    def admin_delete_custom_tool(self, item_id):
        return self._json_request("DELETE", f"/v1/admin/custom-tools/{_segment(item_id)}")

    def admin_skills(self, **query):
        return self._json_request("GET", "/v1/admin/skills", params=_query(**query))

    def admin_create_skill(self, **attrs):
        return self._json_request("POST", "/v1/admin/skills", body=attrs)

    def admin_update_skill(self, item_id, **attrs):
        return self._json_request("PATCH", f"/v1/admin/skills/{_segment(item_id)}", body=attrs)

    def admin_delete_skill(self, item_id):
        return self._json_request("DELETE", f"/v1/admin/skills/{_segment(item_id)}")

    # -- Usage & API Keys -----------------------------------------------

    def usage(self, **query):
        return self._json_request("GET", "/v1/usage", params=_query(**query))

    def usage_timeseries(self, **query):
        return self._json_request("GET", "/v1/usage/timeseries", params=_query(**query))

    def usage_keys(self, **query):
        return self._json_request("GET", "/v1/usage/keys", params=_query(**query))

    def usage_records(self, **query):
        return self._json_request("GET", "/v1/usage/records", params=_query(**query))

    def api_keys(self, **query):
        return self._json_request("GET", "/v1/api-keys", params=_query(**query))

    def create_api_key(self, **attrs):
        return self._json_request("POST", "/v1/api-keys", body=attrs)

    def revoke_api_key(self, key_id):
        return self._json_request("DELETE", f"/v1/api-keys/{_segment(key_id)}")

    # -- Admin Providers, Models & Stats --------------------------------

    def admin_providers(self, **query):
        return self._json_request("GET", "/v1/admin/providers", params=_query(**query))

    def admin_create_provider(self, **attrs):
        return self._json_request("POST", "/v1/admin/providers", body=attrs)

    def admin_update_provider(self, provider_id, **attrs):
        return self._json_request("PATCH", f"/v1/admin/providers/{_segment(provider_id)}", body=attrs)

    def admin_delete_provider(self, provider_id):
        return self._json_request("DELETE", f"/v1/admin/providers/{_segment(provider_id)}")

    def admin_provider_available_models(self, provider_id):
        return self._json_request("GET", f"/v1/admin/providers/{_segment(provider_id)}/available-models")

    def admin_models(self, **query):
        return self._json_request("GET", "/v1/admin/models", params=_query(**query))

    def admin_create_model(self, **attrs):
        return self._json_request("POST", "/v1/admin/models", body=attrs)

    def admin_update_model(self, model_id, **attrs):
        return self._json_request("PATCH", f"/v1/admin/models/{_segment(model_id)}", body=attrs)

    def admin_delete_model(self, model_id):
        return self._json_request("DELETE", f"/v1/admin/models/{_segment(model_id)}")

    def admin_models_dev_providers(self, **query):
        return self._json_request("GET", "/v1/admin/models/pricing/models-dev/providers", params=_query(**query))

    def admin_get_models_dev_provider(self, models_dev_provider_id, **query):
        return self._json_request(
            "GET",
            f"/v1/admin/models/pricing/models-dev/providers/{_segment(models_dev_provider_id)}",
            params=_query(**query),
        )

    def admin_import_models_dev_prices(self, **attrs):
        return self._json_request("POST", "/v1/admin/models/pricing/models-dev/import", body=attrs)

    def admin_set_title_model(self, **attrs):
        return self._json_request("PUT", "/v1/admin/title-model", body=attrs)

    def admin_set_memory_model(self, **attrs):
        return self._json_request("PUT", "/v1/admin/memory-model", body=attrs)

    def admin_stats(self, **query):
        return self._json_request("GET", "/v1/admin/stats", params=_query(**query))

    def admin_stats_timeseries(self, **query):
        return self._json_request("GET", "/v1/admin/stats/timeseries", params=_query(**query))

    def admin_stats_users(self, **query):
        return self._json_request("GET", "/v1/admin/stats/users", params=_query(**query))

    # -- AI Compat Endpoints --------------------------------------------

    def openai_chat_completions(self, **attrs):
        if attrs.get("stream") is True:
            return self._stream_request("POST", "/v1/chat/completions", body=attrs)
        return self._json_request("POST", "/v1/chat/completions", body=attrs)

    def anthropic_messages(self, **attrs):
        if attrs.get("stream") is True:
            return self._stream_request("POST", "/v1/messages", body=attrs)
        return self._json_request("POST", "/v1/messages", body=attrs)
