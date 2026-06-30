"""Router package: re-exports each router instance for clean registration."""
from .admin import router as admin_router
from .auth import router as auth_router
from .chat import router as chat_router
from .health import router as health_router
from .knowledge import router as knowledge_router
from .user import router as user_router

__all__ = [
    "admin_router",
    "auth_router",
    "chat_router",
    "health_router",
    "knowledge_router",
    "user_router",
]
