r"""JWT verification implementing the MCP SDK's TokenVerifier (PyJWT, RS256).

Validates tokens issued by the Sewa Setu portal against its JWKS.
Enforces:
- RS256 signature verification
- iss == PUBLIC_BASE_URL (exact match, no trailing slash)
- aud == canonical_url (exact string match, e.g. https://HOST/citizen/mcp)
- exp and iat valid with 10s leeway
- scope and role match the server's configured role
- sub matches ^citizen:\d{10}$ or ^officer:[\w.-]+$
- kind=program accepted on officer server only
- citizen server: rejects tokens whose iat is <= subject's sign-out cut-off
"""

import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger("mcp_common.jwt")


class PortalTokenVerifier(TokenVerifier):
    """TokenVerifier that validates RS256 JWTs issued by the Sewa Setu portal."""

    def __init__(
        self,
        public_base_url: str,
        canonical_url: str,
        role: str,
        jwks_url: str = "http://app:8000/.well-known/jwks.json",
        internal_api_url: str = "http://app:8000/internal/api/v1",
        internal_api_key: str | None = None,
        cache_ttl: float = 600.0,
        session_cache_ttl: float = 5.0,
        jwks_client: httpx.AsyncClient | None = None,
    ):
        self.public_base_url = public_base_url.rstrip("/")
        self.canonical_url = canonical_url
        self.role = role
        self.jwks_url = jwks_url
        self.internal_api_url = internal_api_url.rstrip("/")
        self.internal_api_key = internal_api_key or ""
        self.cache_ttl = cache_ttl
        self.session_cache_ttl = session_cache_ttl
        self._custom_client = jwks_client

        # JWKS cache: {kid: key_object}, last_fetched_time
        self._keys_by_kid: dict[str, Any] = {}
        self._jwks_fetched_at: float = 0.0

        # Session state cache for citizen sign-out cut-off:
        # {subject: (revoked_at_timestamp_or_none, cached_at)}
        self._session_cache: dict[str, tuple[float | None, float]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        return httpx.AsyncClient(timeout=5.0)

    async def _fetch_jwks(self, force: bool = False) -> dict[str, Any]:
        """Fetch and cache JWKS public keys. Fail closed on network error."""
        now = time.time()
        if not force and self._keys_by_kid and (now - self._jwks_fetched_at < self.cache_ttl):
            return self._keys_by_kid

        try:
            client = await self._get_client()
            should_close = (self._custom_client is None)
            try:
                resp = await client.get(self.jwks_url)
                resp.raise_for_status()
                jwks_data = resp.json()
            finally:
                if should_close:
                    await client.aclose()

            new_keys: dict[str, Any] = {}
            for jwk_dict in jwks_data.get("keys", []):
                kid = jwk_dict.get("kid")
                if kid:
                    try:
                        key_obj = jwt.algorithms.RSAAlgorithm.from_jwk(jwk_dict)
                        new_keys[kid] = key_obj
                    except Exception as e:
                        logger.warning("Failed to parse JWK kid=%s: %s", kid, e)

            if new_keys:
                self._keys_by_kid = new_keys
                self._jwks_fetched_at = now
            return self._keys_by_kid
        except Exception as e:
            logger.error("Failed to fetch JWKS from %s: %s", self.jwks_url, e)
            # Fail closed — do not accept arbitrary tokens
            return self._keys_by_kid

    async def _check_citizen_signout(self, subject: str, token_iat: int) -> bool:
        """Check if citizen has signed out.

        Returns True if token is valid (iat > revoked_at), False if rejected.
        Cached for <= session_cache_ttl (5 seconds) to avoid request flooding.
        """
        now = time.time()
        cached = self._session_cache.get(subject)
        if cached:
            revoked_epoch, cached_at = cached
            ttl = self.session_cache_ttl if revoked_epoch is not None else 1.0
            if now - cached_at < ttl:
                if revoked_epoch is not None and token_iat <= revoked_epoch:
                    return False
                return True

        # Query internal API for session state
        revoked_epoch: float | None = None
        try:
            url = f"{self.internal_api_url}/me/session-state"
            headers = {
                "X-Internal-Key": self.internal_api_key,
                "X-Acting-Subject": subject,
            }
            client = await self._get_client()
            should_close = (self._custom_client is None)
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    payload = resp.json()
                    revoked_at_str = payload.get("data", {}).get("revoked_at")
                    if revoked_at_str:
                        # ISO format parse
                        dt = datetime.fromisoformat(revoked_at_str)
                        revoked_epoch = dt.timestamp()
            finally:
                if should_close:
                    await client.aclose()

            # Store in cache
            self._session_cache[subject] = (revoked_epoch, now)
            if revoked_epoch is not None and token_iat <= revoked_epoch:
                logger.info(
                    "Token for %s rejected due to sign-out cut-off (iat=%s <= revoked=%s)",
                    subject, token_iat, revoked_epoch
                )
                return False
            return True
        except Exception as e:
            logger.warning("Failed to check session state for %s: %s", subject, e)
            # If session state check fails, accept token if cached allows, else fail closed
            if cached and cached[0] is not None and token_iat <= cached[0]:
                return False
            return True

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token and return AccessToken if valid, None otherwise."""
        if not token or not isinstance(token, str):
            return None

        # 1. Inspect unverified header for kid
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                logger.warning("Token missing kid header")
                return None
        except Exception:
            return None

        # 2. Get key from JWKS (refetch once if kid unknown)
        keys = await self._fetch_jwks()
        key = keys.get(kid)
        if not key:
            # Refetch once on unknown kid
            keys = await self._fetch_jwks(force=True)
            key = keys.get(kid)
            if not key:
                logger.warning("Unknown kid=%s after JWKS refresh", kid)
                return None

        # 3. Decode & verify signature with PyJWT (RS256, 10s leeway)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": False,  # We perform exact audience comparison below
                },
                leeway=10,
            )
        except jwt.PyJWTError as e:
            logger.info("JWT decode/validation failed: %s", e)
            return None

        # 4. Require iss == PUBLIC_BASE_URL (exact match, no trailing slash)
        token_iss = claims.get("iss", "").rstrip("/")
        if token_iss != self.public_base_url:
            logger.info("Issuer mismatch: expected %s, got %s", self.public_base_url, token_iss)
            return None

        # 5. Require aud == canonical_url (exact string match, including trailing slashes)
        token_aud = claims.get("aud")
        if token_aud != self.canonical_url:
            logger.info("Audience mismatch: expected %s, got %s", self.canonical_url, token_aud)
            return None

        # 6. Require scope and role equal to this server's role
        token_role = claims.get("role")
        token_scope = claims.get("scope", "")
        # Check role field matches
        if token_role != self.role:
            logger.info("Role mismatch: expected %s, got %s", self.role, token_role)
            return None

        # Check scope matches role (e.g. scope 'citizen' or 'officer')
        scopes = token_scope.split() if isinstance(token_scope, str) else []
        if self.role not in scopes:
            logger.info("Scope mismatch: %s not in token scopes %s", self.role, scopes)
            return None

        # 7. Validate subject format
        sub = claims.get("sub", "")
        if self.role == "citizen":
            if not re.match(r"^citizen:\d{10}$", sub):
                logger.info("Invalid citizen subject format: %s", sub)
                return None
        elif self.role == "officer":
            if not re.match(r"^officer:[\w.-]+$", sub):
                logger.info("Invalid officer subject format: %s", sub)
                return None
        else:
            return None

        # 8. Token kind: kind=program allowed on officer server ONLY
        token_kind = claims.get("kind", "access")
        if token_kind == "program":
            if self.role != "officer":
                logger.info("kind=program rejected on non-officer server: role=%s", self.role)
                return None
        elif token_kind != "access":
            logger.info("Unknown token kind: %s", token_kind)
            return None

        # 9. Citizen server only: sign-out cut-off check
        if self.role == "citizen":
            token_iat = claims.get("iat", 0)
            valid_session = await self._check_citizen_signout(sub, token_iat)
            if not valid_session:
                return None

        # All checks passed! Return AccessToken
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", ""),
            scopes=[self.role],
            expires_at=claims.get("exp"),
            resource=claims.get("aud"),
            subject=sub,
            claims=claims,
        )


def current_access_token() -> AccessToken | None:
    """Return the AccessToken from current context if authenticated, else None."""
    return get_access_token()


def current_subject() -> str | None:
    """Return the verified subject (e.g. 'citizen:9876543210' or 'officer:admin') from current context."""
    token = current_access_token()
    return token.subject if token else None
