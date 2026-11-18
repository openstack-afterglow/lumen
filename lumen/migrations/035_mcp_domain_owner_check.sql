-- Existing invalid domain rows fail this CHECK without any ownership backfill.
ALTER TABLE chat_mcp_servers
    ADD CONSTRAINT ck_chat_mcp_servers_domain_owner
    CHECK (scope <> 'domain' OR owner_domain_id IS NOT NULL);
