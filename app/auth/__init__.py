"""Authentication and role-based access control for the TRINETRA API.

Two rules this package exists to enforce, both of which the service lacked:

1. **No anonymous access to camera data.** ``GET /api/v1/cameras`` returns
   ``stream_url``, which the schema documents as a credential-bearing secret.

2. **Department scope is decided by the server.** The caller's department comes
   from their token and is injected into the SQL predicate. A client-supplied
   ``department_id`` is a filter, never a boundary - omitting it must not widen
   what a user can see.
"""

from app.auth.dependencies import (
    Principal,
    get_current_principal,
    get_audited_connection,
    require_role,
)
from app.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

__all__ = [
    "Principal",
    "get_current_principal",
    "get_audited_connection",
    "require_role",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "hash_password",
    "verify_password",
]
