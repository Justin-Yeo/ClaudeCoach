"""ORM models package.

Importing this package registers every model with `Base.metadata` so Alembic's
autogenerate sees them. `alembic/env.py` imports `from app import models` to
trigger this.
"""

from app.models.invite_code import InviteCode
from app.models.oauth_state import OAuthState
from app.models.run import Run
from app.models.user import User

__all__ = ["InviteCode", "OAuthState", "Run", "User"]
