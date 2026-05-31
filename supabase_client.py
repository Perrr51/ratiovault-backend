"""Supabase service_role client singleton.

Authorized importers (see `tests/test_supabase_client.py::test_service_import_is_restricted`):
    - routers/webhooks.py
    - routers/checkout.py
    - routers/portal.py
    - routers/internal.py
"""

from functools import lru_cache

from supabase import Client, create_client

from config import settings


@lru_cache(maxsize=1)
def get_supabase_service() -> Client:
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
