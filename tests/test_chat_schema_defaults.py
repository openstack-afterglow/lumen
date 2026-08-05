from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from lumen.models.chat_db import ChatCustomTool, ChatMcpServer


def test_extension_load_policy_has_mysql_server_default():
    for model in (ChatMcpServer, ChatCustomTool):
        ddl = str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))
        load_policy_ddl = next(line for line in ddl.splitlines() if "load_policy" in line)

        assert "DEFAULT 'on_demand'" in load_policy_ddl
        assert "NOT NULL" in load_policy_ddl
