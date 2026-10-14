"""Versioned API surface, mounted under `settings.api_prefix`.

Health lives at the server root (see `app.api.health`) so health checks stay stable
across API versions.
"""

from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.dashboard import router as dashboard_router
from app.api.follow_ups import router as follow_ups_router
from app.api.handoffs import router as handoffs_router
from app.api.knowledge_base import router as knowledge_base_router
from app.api.leads import router as leads_router
from app.api.meta import router as meta_router
from app.api.tenants import router as tenants_router
from app.api.webhooks import router as webhooks_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(tenants_router)
api_router.include_router(meta_router)
api_router.include_router(leads_router)
api_router.include_router(conversations_router)
api_router.include_router(knowledge_base_router)
api_router.include_router(follow_ups_router)
api_router.include_router(handoffs_router)
api_router.include_router(dashboard_router)
# Meta calls this itself: no SaaS session, no X-Tenant-ID. It resolves the tenant
# from the Meta source identifier, after validating Meta's own signature.
api_router.include_router(webhooks_router)
