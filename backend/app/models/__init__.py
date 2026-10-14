"""Model registry. Importing this package registers every table on `Base.metadata`,
which is what `alembic revision --autogenerate` compares against.
"""

from app.models.ai_message_run import AIMessageRun, AIRunStatus
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.human_handoff import (
    ACTIVE_HANDOFF_PREDICATE,
    ACTIVE_HANDOFF_STATUSES,
    REASON_PRIORITY,
    HandoffReason,
    HandoffStatus,
    HumanHandoff,
)
from app.models.internal_notification import (
    InternalNotification,
    NotificationStatus,
    NotificationType,
)
from app.models.knowledge_base import (
    EMBEDDING_DIMENSION,
    KnowledgeBase,
    KnowledgeSourceType,
)
from app.models.lead import Lead, LeadSource, LeadStatus
from app.models.membership import ROLE_RANK, Role, TenantMembership
from app.models.meta_connection import (
    MetaConnection,
    MetaConnectionStatus,
    MetaProvider,
)
from app.models.meta_oauth_state import MetaOAuthState
from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType
from app.models.session import Session
from app.models.tenant import Tenant, TenantStatus
from app.models.types import Email
from app.models.user import User

__all__ = [
    "ACTIVE_HANDOFF_PREDICATE",
    "ACTIVE_HANDOFF_STATUSES",
    "EMBEDDING_DIMENSION",
    "REASON_PRIORITY",
    "ROLE_RANK",
    "AIMessageRun",
    "AIRunStatus",
    "Channel",
    "Conversation",
    "Email",
    "HandoffReason",
    "HandoffStatus",
    "HumanHandoff",
    "InternalNotification",
    "KnowledgeBase",
    "KnowledgeSourceType",
    "Lead",
    "LeadSource",
    "LeadStatus",
    "Message",
    "MessageDelivery",
    "MessageDirection",
    "MetaConnection",
    "MetaConnectionStatus",
    "MetaOAuthState",
    "MetaProvider",
    "MetaWebhookEvent",
    "MetaWebhookEventStatus",
    "NotificationStatus",
    "NotificationType",
    "Role",
    "ScheduledTask",
    "Session",
    "Tenant",
    "TenantMembership",
    "TenantStatus",
    "TaskStatus",
    "TaskType",
    "TimestampMixin",
    "User",
    "UUIDPrimaryKeyMixin",
]
