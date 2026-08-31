from __future__ import annotations

import logging
from typing import Any

from wsgidav.dc.base_dc import BaseDomainController

logger = logging.getLogger(__name__)


class ImmichApiKeyDC(BaseDomainController):
    """Accepts any non-empty Basic Auth password as an Immich API key.

    The password is *not* validated here. WebDAV Basic Auth re-sends
    credentials on every request, so a dedicated check would mean an Immich
    round-trip per request; instead the key is stashed on `environ` and the
    first real Immich call (listing an album, fetching an asset) is what
    validates it -- those calls carry the key anyway. A key Immich rejects
    surfaces as `ImmichAuthError`, which the resource layer turns back into a
    `401` credential challenge (see `webdav/resources.py`).

    This class still exists so wsgidav's HTTPAuthenticator issues the initial
    `401 WWW-Authenticate: Basic` that tells a client to send credentials at
    all, and so a blank password is rejected up front.
    """

    def get_domain_realm(self, path_info: str, environ: dict[str, Any]) -> str:
        return "immich-webdav"

    def require_authentication(self, realm: str, environ: dict[str, Any]) -> bool:
        return True

    def supports_http_digest_auth(self) -> bool:
        # Digest would require knowing the plaintext password up front to
        # build the challenge response -- incompatible with treating it as an
        # opaque bearer credential. Basic over TLS is the only usable mode.
        return False

    def basic_auth_user(
        self, realm: str, user_name: str, password: str, environ: dict[str, Any]
    ) -> bool:
        if not password:
            return False
        environ["immich_webdav.api_key"] = password
        return True
