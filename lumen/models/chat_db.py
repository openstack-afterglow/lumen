"""빌트인 AI 채팅 SQLAlchemy ORM 모델 (마이그레이션 026_chat_builtin.sql 대응).

프로바이더/모델 카탈로그, 대화/메시지, 사용량 원장, 사용자 지갑(크레딧·월 쿼터).
모든 사용자 리소스(chat_conversations 등)는 project_id + user_id 를 보유해 소유권 검증 대상.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    BIGINT,
    BOOLEAN,
    CHAR,
    INT,
    JSON,
    TEXT,
    VARCHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lumen.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class LlmProvider(Base):
    __tablename__ = "llm_providers"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    # litellm custom_llm_provider (openai|anthropic|gemini|vertex_ai|azure|bedrock|ollama|...)
    provider_type: Mapped[str] = mapped_column(VARCHAR(40), nullable=False, default="openai")
    api_base: Mapped[str | None] = mapped_column(VARCHAR(255))
    # AES-256-GCM(k3s_kubeconfig_encryption_key, 도메인 llm_provider_key) 암호화 상태로 저장
    encrypted_api_key: Mapped[str | None] = mapped_column(TEXT)
    # Optional environment variable name used only when no database key exists.
    api_key_env: Mapped[str | None] = mapped_column(VARCHAR(128))
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    margin_multiplier: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, default=Decimal("1.0"))
    # models.dev provider key. Display names are never used for catalog matching.
    models_dev_provider_id: Mapped[str | None] = mapped_column(VARCHAR(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    models: Mapped[list["LlmModel"]] = relationship("LlmModel", back_populates="provider", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("name", name="uq_llm_providers_name"),)


class LlmModel(Base):
    __tablename__ = "llm_models"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(BIGINT, ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False)
    model_name: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    display_name: Mapped[str | None] = mapped_column(VARCHAR(150))
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    # 대화 제목 자동 요약에 쓸 모델. 앱 레벨에서 최대 1개만 True 로 유지(set_title_model).
    is_title_model: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    # 채팅 완료 후 사용자 메모리 자동 추출에 쓸 초소형 모델. 최대 1개만 True(set_memory_model).
    is_memory_model: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    # 미지정 시 litellm 내장 단가 사용 (override용). 토큰당 USD 단가.
    input_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    output_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    # models.dev catalog mapping and the immutable provenance of the last import.
    models_dev_model_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    price_source: Mapped[str | None] = mapped_column(VARCHAR(20))
    price_metadata: Mapped[dict | None] = mapped_column(JSON)
    # 능력(vision/reasoning/tool_call/attachment/modalities/reasoning_options/context_limit) 저장.
    # None 이면 런타임 litellm 판별로 fallback. capability_source: override|models_dev|None.
    capabilities: Mapped[dict | None] = mapped_column(JSON)
    capability_source: Mapped[str | None] = mapped_column(VARCHAR(20))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    provider: Mapped["LlmProvider"] = relationship("LlmProvider", back_populates="models")

    __table_args__ = (
        UniqueConstraint("provider_id", "model_name", name="uq_llm_models_provider_model"),
        Index("idx_llm_models_active", "is_active"),
    )


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    # AES-256-GCM(도메인 chat_content) 암호문. 첫 메시지 요약 제목도 채팅 내용이라 암호화. TEXT(암호문 길이).
    title: Mapped[str | None] = mapped_column(TEXT)
    model_name: Mapped[str | None] = mapped_column(VARCHAR(190))
    # 버전 트리에서 현재 보이는 리프 메시지. 렌더 경로 = active_leaf → parent 역추적.
    active_leaf_id: Mapped[int | None] = mapped_column(BIGINT)
    # 분기(fork) 출처: 이 대화가 어느 대화의 어느 메시지에서 갈라졌는지(감사·표시용).
    parent_conversation_id: Mapped[str | None] = mapped_column(CHAR(36))
    forked_from_message_id: Mapped[int | None] = mapped_column(BIGINT)
    # 소속 프로젝트(chat_workspaces). OpenStack project_id 와 별개 개념. 완료 시 워크스페이스 지침 주입.
    workspace_id: Mapped[int | None] = mapped_column(BIGINT)
    code_workspace_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_code_workspaces.id", ondelete="SET NULL")
    )
    lifecycle_status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_chat_conversations_owner", "project_id", "user_id"),
        Index("idx_chat_conversations_user_updated", "user_id", "updated_at"),  # 사용자별 목록(프로젝트 무관)
        Index("idx_chat_conversations_updated", "updated_at"),
        Index("idx_chat_conversations_code_workspace", "code_workspace_id"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)  # system | user | assistant | tool
    # 버전 트리 부모 메시지 id. 같은 parent_id 를 공유하는 형제 = 재생성 버전들. 루트는 NULL.
    parent_id: Mapped[int | None] = mapped_column(BIGINT)
    # AES-256-GCM(도메인 chat_content) 암호문 저장. 읽을 때 decrypt_chat_content 로 복호화.
    content: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    # 툴 호출 기록(JSON)을 직렬화 후 암호화한 문자열. 평문 JSON 컬럼이 아님(암호화 도입).
    tool_calls: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    # 답변 출처(citations) JSON([{url,title,snippet}])을 직렬화 후 암호화한 문자열. content 와 동일 도메인.
    citations: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    # 추론(thinking) 텍스트를 암호화한 문자열(JSON 아님). 재로딩 시 추론 과정 표시용.
    reasoning: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    # 첨부(이미지) 참조 JSON([{key,mime,name}])을 암호화한 문자열. 표시·현재턴 vision content 재구성용.
    attachments: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    parts: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    parts_version: Mapped[int | None] = mapped_column(INT)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="complete")

    token_prompt: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    token_completion: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    # 재생성 시 어떤 모델로 생성했는지(형제 버전 구분·표시용). 미지정 시 대화 기본 모델.
    model_name: Mapped[str | None] = mapped_column(VARCHAR(190))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(DATETIME(fsp=6), "mysql").with_variant(DATETIME(fsp=6), "mariadb"),
        nullable=False,
        default=_now,
    )
    # Browser-local wall-clock representation of the same `created_at` instant. Legacy rows stay NULL.
    created_at_local: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False).with_variant(DATETIME(fsp=6), "mysql").with_variant(DATETIME(fsp=6), "mariadb")
    )
    created_timezone: Mapped[str | None] = mapped_column(VARCHAR(64))

    conversation: Mapped["ChatConversation"] = relationship("ChatConversation", back_populates="messages")

    __table_args__ = (
        Index("idx_chat_messages_conversation", "conversation_id", "created_at"),
        Index("idx_chat_messages_conversation_id", "conversation_id", "id"),
        Index("idx_chat_messages_parent", "parent_id"),
    )


class ChatUsageLog(Base):
    __tablename__ = "chat_usage_logs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(CHAR(36))  # 대화 삭제 후에도 원장 보존 — FK 없음
    model_name: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    provider: Mapped[str | None] = mapped_column(VARCHAR(100))
    prompt_tokens: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    raw_cost: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False, default=Decimal("0"))
    credited_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    source: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="web")  # web | api
    api_key_id: Mapped[int | None] = mapped_column(BIGINT)
    event_id: Mapped[str | None] = mapped_column(VARCHAR(64), unique=True)
    pricing_status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="legacy")
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON)
    run_id: Mapped[str | None] = mapped_column(CHAR(36), unique=True)
    usage_components: Mapped[dict | None] = mapped_column(JSON)
    provider_reported_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        Index("idx_chat_usage_project_created", "project_id", "created_at"),
        Index("idx_chat_usage_user_created", "user_id", "created_at"),
        Index("idx_chat_usage_api_key_created", "api_key_id", "created_at"),
    )


class UserWallet(Base):
    __tablename__ = "user_wallets"

    user_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    balance_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    max_quota_monthly: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    used_quota_this_month: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    reserved_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    quota_period_start: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ChatMcpServer(Base):
    """Remote MCP data source. User sources are public or OAuth; admin sources may use static headers."""

    __tablename__ = "chat_mcp_servers"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="user")
    owner_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    owner_project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    transport: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="http")  # HTTPS streamable HTTP only
    url: Mapped[str | None] = mapped_column(VARCHAR(500))
    command: Mapped[str | None] = mapped_column(VARCHAR(500))  # deprecated(stdio 제거) — 컬럼만 유지
    headers: Mapped[dict | None] = mapped_column(
        JSON
    )  # deprecated plaintext column; new admin headers use encrypted_headers.
    # Administrator-managed static headers for explicitly approved nonstandard authentication.
    encrypted_headers: Mapped[str | None] = mapped_column(TEXT)
    # none|oauth|admin. User sources can only be none or OAuth.
    auth_mode: Mapped[str] = mapped_column(VARCHAR(12), nullable=False, default="none")
    oauth_scopes: Mapped[list | None] = mapped_column(JSON)
    oauth_client_id: Mapped[str | None] = mapped_column(VARCHAR(512))
    encrypted_oauth_client_secret: Mapped[str | None] = mapped_column(TEXT)
    tool_effect_overrides: Mapped[dict | None] = mapped_column(JSON)
    config_version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    source_package_id: Mapped[str | None] = mapped_column(CHAR(36))
    source_package_version: Mapped[str | None] = mapped_column(VARCHAR(64))
    managed_by_package: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    load_policy: Mapped[str] = mapped_column(
        VARCHAR(16), nullable=False, default="on_demand", server_default="on_demand"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_mcp_scope", "scope"),
        Index("idx_chat_mcp_owner", "owner_user_id"),
        Index("idx_chat_mcp_servers_project", "project_id"),
    )


class ChatMcpOAuthConnection(Base):
    """Per-user/project OAuth token bundle for a remote MCP server."""

    __tablename__ = "chat_mcp_oauth_connections"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    mcp_server_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    owner_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    encrypted_tokens: Mapped[str] = mapped_column(TEXT, nullable=False)
    credential_version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    status: Mapped[str] = mapped_column(VARCHAR(12), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    server_config_version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("mcp_server_id", "owner_user_id", "owner_project_id", name="uq_chat_mcp_oauth_connection"),
        Index("idx_chat_mcp_oauth_connection_owner", "owner_user_id", "owner_project_id", "status"),
    )


class ChatMcpOAuthRequest(Base):
    """Short-lived, single-use server-held OAuth state and PKCE verifier."""

    __tablename__ = "chat_mcp_oauth_requests"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    state_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    mcp_server_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    owner_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(TEXT, nullable=False)
    server_config_version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    status: Mapped[str] = mapped_column(VARCHAR(12), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (Index("idx_chat_mcp_oauth_request_expiry", "expires_at", "status"),)


class ChatApiKey(Base):
    """외부 API 키 — 사용자가 OpenAI/Anthropic 호환 /v1 엔드포인트에 접속할 때 사용.

    키 평문은 발급 시 1회만 반환하고 저장하지 않는다. key_hash 는 SHA-256(전체 키) hex 로,
    인증 시 요청 키를 해시해 조회 후 hmac.compare_digest 로 타이밍 안전 비교한다.
    사용량은 chat_usage_logs.api_key_id 로 이 id 를 참조(FK 없음 — 폐기 후에도 원장 보존).
    """

    __tablename__ = "chat_api_keys"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    owner_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False, default="")
    key_prefix: Mapped[str] = mapped_column(VARCHAR(24), nullable=False)  # 표시용(시크릿 아님)
    key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)  # SHA-256 hex
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    owner_monthly_credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    admin_monthly_credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_chat_api_keys_hash"),
        CheckConstraint(
            "owner_monthly_credit_limit IS NULL OR owner_monthly_credit_limit > 0",
            name="chk_chat_api_keys_owner_monthly_credit_limit",
        ),
        CheckConstraint(
            "admin_monthly_credit_limit IS NULL OR admin_monthly_credit_limit > 0",
            name="chk_chat_api_keys_admin_monthly_credit_limit",
        ),
        Index("idx_chat_api_keys_owner", "owner_user_id", "owner_project_id"),
    )


class ChatCustomTool(Base):
    """커스텀 HTTP 툴 (백엔드가 SSRF 가드 후 대리 호출). scope: global|user."""

    __tablename__ = "chat_custom_tools"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="user")
    owner_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    owner_project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)  # 영숫자/언더스코어
    description: Mapped[str] = mapped_column(VARCHAR(500), nullable=False)
    method: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="GET")  # GET|POST
    url: Mapped[str] = mapped_column(VARCHAR(500), nullable=False)
    params_schema: Mapped[dict | None] = mapped_column(JSON)
    timeout_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=10)
    effect: Mapped[str] = mapped_column(VARCHAR(30), nullable=False, default="external_mutation")
    config_version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    source_package_id: Mapped[str | None] = mapped_column(CHAR(36))
    source_package_version: Mapped[str | None] = mapped_column(VARCHAR(64))
    managed_by_package: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    load_policy: Mapped[str] = mapped_column(
        VARCHAR(16), nullable=False, default="on_demand", server_default="on_demand"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_tool_scope", "scope"),
        Index("idx_chat_tool_owner", "owner_user_id"),
        Index("idx_chat_custom_tools_project", "project_id"),
    )


class ChatSkill(Base):
    """빌트인 AI 채팅 스킬 (Claude Agent Skills 계열) — 선택 시 system 프리앰블에 주입.

    MCP/커스텀툴과 같은 확장 체계(scope: global|user)이나, 실행 대신 지침(instructions,
    SKILL.md 본문)을 대화 컨텍스트에 주입한다. 채팅별로 명시 선택된 스킬만 주입된다(opt-in).
    instructions 는 AES-256-GCM(chat_content 도메인) 암호문으로 저장한다.
    """

    __tablename__ = "chat_skills"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="user")
    owner_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    owner_project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    description: Mapped[str | None] = mapped_column(VARCHAR(500))
    instructions: Mapped[str] = mapped_column(TEXT, nullable=False)  # 암호문(chat_content 도메인)
    slug: Mapped[str | None] = mapped_column(VARCHAR(100))
    argument_hint: Mapped[str | None] = mapped_column(VARCHAR(500))
    user_invocable: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    model_invocable: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    source_package_id: Mapped[str | None] = mapped_column(CHAR(36))
    source_package_version: Mapped[str | None] = mapped_column(VARCHAR(64))
    managed_by_package: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_skill_scope", "scope"),
        Index("idx_chat_skill_owner", "owner_user_id"),
        Index("idx_chat_skills_project", "project_id"),
        UniqueConstraint("project_id", "slug", name="uq_chat_skills_project_slug"),
    )


class ChatAgent(Base):
    """사용자 정의 에이전트 — 프롬프트(instructions) + 모델 + 파라미터 + MCP/툴 묶음.

    visibility='private'(소유자만) | 'public'(허브 공개 — 검색·복제 가능, 수정은 소유자만).
    instructions 는 AES-256-GCM(chat_content 도메인) 암호문으로 저장한다.
    mcp_ids/tool_ids 는 ChatMcpServer/ChatCustomTool id 목록(JSON) — 에이전트에 바인딩할 확장.
    """

    __tablename__ = "chat_agents"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    description: Mapped[str | None] = mapped_column(VARCHAR(500))
    avatar: Mapped[str | None] = mapped_column(VARCHAR(500))  # 이모지 또는 URL
    # AES-256-GCM(chat_content) 암호문. 시스템 프롬프트로 주입되는 지침.
    instructions: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    model_name: Mapped[str | None] = mapped_column(VARCHAR(190))
    params: Mapped[dict | None] = mapped_column(JSON)  # {temperature, max_tokens, ...}
    mcp_ids: Mapped[list | None] = mapped_column(JSON)  # 바인딩된 ChatMcpServer id 목록
    tool_ids: Mapped[list | None] = mapped_column(JSON)  # 바인딩된 ChatCustomTool id 목록
    skill_ids: Mapped[list | None] = mapped_column(JSON)
    visibility: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="private")  # private|public
    role: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="general")
    execution_policy: Mapped[dict | None] = mapped_column(JSON)
    delegable_agent_ids: Mapped[list | None] = mapped_column(JSON)
    # 허브 복제 출처(감사·표시용): 이 에이전트가 어떤 공개 에이전트에서 복제됐는지.
    cloned_from_id: Mapped[int | None] = mapped_column(BIGINT)
    clone_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)  # 허브 인기 지표
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    source_package_id: Mapped[str | None] = mapped_column(CHAR(36))
    source_package_version: Mapped[str | None] = mapped_column(VARCHAR(64))
    managed_by_package: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_agents_owner", "owner_user_id"),
        Index("idx_chat_agents_visibility", "visibility"),
        Index("idx_chat_agents_project", "project_id"),
        Index("idx_chat_agents_project_active", "project_id", "is_active"),
    )


class ChatWorkspace(Base):
    """채팅 프로젝트(OpenAI/Claude식) — 대화 그룹 + 공통 지침. OpenStack project_id 와 별개 개념.

    소유자(user_id)만 CRUD. instructions 는 chat_content 암호문. 완료 시 소속 대화에 system 주입.
    """

    __tablename__ = "chat_workspaces"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    description: Mapped[str | None] = mapped_column(VARCHAR(500))
    # AES-256-GCM(chat_content) 암호문. 프로젝트 공통 지침(system 주입).
    instructions: Mapped[str | None] = mapped_column(MEDIUMTEXT)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (Index("idx_chat_workspaces_owner", "owner_user_id"),)


class ChatMemory(Base):
    """Encrypted memory source of truth, scoped for fail-closed semantic hydration."""

    __tablename__ = "chat_memories"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    workspace_id: Mapped[int | None] = mapped_column(BIGINT)
    scope: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="account")
    category: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="general")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="active")
    extraction_status: Mapped[str | None] = mapped_column(VARCHAR(20))
    # AES-256-GCM(chat_content) 암호문. 기억할 사실(예: "사용자는 Python 을 선호").
    content: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint(
            "(scope = 'account' AND project_id IS NULL AND workspace_id IS NULL) "
            "OR (scope = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL) "
            "OR (scope = 'workspace' AND project_id IS NOT NULL AND workspace_id IS NOT NULL)",
            name="chk_chat_memory_scope",
        ),
        Index("idx_chat_memories_user", "user_id"),
        Index("idx_chat_memories_scope", "user_id", "project_id", "workspace_id", "status", "is_active"),
    )


class ChatMemoryOwnerLock(Base):
    """Serializes automatic memory deltas for one user and project."""

    __tablename__ = "chat_memory_owner_locks"

    user_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


