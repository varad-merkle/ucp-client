"""UCP identity linking (dev.ucp.common.identity_linking): shop as the user, not as a guest.

Without a link every store call is a guest call, exactly as before. With one, the
store's own access token goes on its MCP calls as `Authorization: Bearer`, and the
store can answer with that customer's saved addresses, orders and member prices.

Two ways to link, both from the UCP spec (2026-08-25, common/identity-linking):

1. Accelerated IdP Flow (preferred). Every store here lists Shop accounts
   (accounts.shop.app) as a trusted provider, so the user signs in to Shop once and
   each store is then linked without another redirect:
       Shop access token --RFC 8693 token exchange, audience=store issuer--> JWT grant
       JWT grant --RFC 7523 jwt-bearer at the store's token endpoint--> store token
   Stores do not issue refresh tokens here; an expired store token is chained again.

2. Direct OAuth with one store, used when chaining is refused (e.g. the store needs
   the user to accept terms) or no Shop link exists.

Both browser flows are OAuth 2.0 Authorization Code with PKCE S256, a single-use
`state` bound to the logged-in user, and the RFC 9207 `iss` check. Authorization
servers are found with RFC 9728 (protected resource) then RFC 8414 metadata, falling
back to OIDC discovery only on a 404, and the issuer must match byte for byte.

Tokens never reach the browser: they are encrypted with IDENTITY_TOKEN_KEY (Fernet)
in the identity_link table, one row per (user, target). The target is "shop" or a
store id. Unlinking revokes at the authorization server (RFC 7009) before deleting.

Switched on by .env: IDENTITY_TOKEN_KEY plus the Dev Dashboard client
(SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET). Shopify recognises that one client at
Shop accounts and at every store (shopify.dev/docs/agents/profiles/buyer-linked-tokens);
a per-store pair (SHOPIFY_CLIENT_ID_<STORE>) can override it for one store.

Shopify specifics followed here: the token exchange `audience` is the store's domain
(e.g. weareallbirds.myshopify.com), buyer-linked store tokens are 60-minute JWTs that
are not refreshed (we chain again), and linked calls to a store carry Shopify-Buyer-IP.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

import db

logger = logging.getLogger("ucp-client")

CAPABILITY = "dev.ucp.common.identity_linking"
SHOP = "shop"
SHOP_ISSUER = "https://accounts.shop.app"
# Request at Shop every scope that will be redeemed at a store (Shopify docs).
DEFAULT_SHOP_SCOPES = "openid email dev.ucp.shopping.checkout:manage"
DEFAULT_STORE_SCOPES = "openid dev.ucp.shopping.checkout:manage"

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"

STATE_TTL = 600            # seconds a started link may take to come back
REFRESH_MARGIN = 60        # refresh this many seconds before a token expires
METADATA_TTL = 3600        # cache for discovered server metadata
CHAIN_RETRY_AFTER = 600    # after a refused chain, wait this long before trying again

# The user a store call is made for, and their IP (Shopify-Buyer-IP on linked calls).
# Set by the chat/checkout endpoints; call_mcp reads them.
acting_user: ContextVar[str | None] = ContextVar("acting_user", default=None)
acting_buyer_ip: ContextVar[str | None] = ContextVar("acting_buyer_ip", default=None)

# Tests swap in an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None


class IdentityError(Exception):
    """A link step failed. `code` is the OAuth error code when the server gave one."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ settings


def _env(name: str) -> str | None:
    return (os.environ.get(name) or "").strip() or None


def store_credentials(store_id: str) -> tuple[str, str] | None:
    """(client_id, client_secret) for a store: the per-store pair, else the shared one."""
    suffix = re.sub(r"\W", "_", store_id).upper()
    client_id = _env(f"SHOPIFY_CLIENT_ID_{suffix}") or _env("SHOPIFY_CLIENT_ID")
    secret = _env(f"SHOPIFY_CLIENT_SECRET_{suffix}") or _env("SHOPIFY_CLIENT_SECRET")
    return (client_id, secret) if client_id and secret else None


def _fernet() -> Fernet | None:
    key = _env("IDENTITY_TOKEN_KEY")
    if not key:
        return None
    try:
        return Fernet(key)
    except ValueError:
        return None


def enabled(merchants) -> bool:
    """On when tokens can be stored safely and at least one store has credentials."""
    return _fernet() is not None and any(store_credentials(m.id) for m in merchants)


def redirect_uri(public_url: str | None) -> str:
    return _env("IDENTITY_REDIRECT_URI") or f"{public_url}/api/identity/callback"


def shop_client_id(public_url: str | None) -> str:
    """The Shop accounts client: an explicit one, else the Dev Dashboard client (which
    Shop recognises), else our Client ID Metadata Document URL as a public client."""
    return (_env("SHOP_ACCOUNTS_CLIENT_ID") or _env("SHOPIFY_CLIENT_ID")
            or f"{public_url}/oauth/client.json")


def _shop_secret() -> str | None:
    if _env("SHOP_ACCOUNTS_CLIENT_ID"):
        return _env("SHOP_ACCOUNTS_CLIENT_SECRET")
    return _env("SHOPIFY_CLIENT_SECRET") if _env("SHOPIFY_CLIENT_ID") else None


def _shop_scopes() -> str:
    return _env("SHOP_ACCOUNTS_SCOPES") or DEFAULT_SHOP_SCOPES


def chain_audience(merchant) -> str:
    """The token exchange audience for a store: its Shopify domain, as Shopify documents
    (the host of its MCP endpoint). IDENTITY_AUDIENCE=issuer sends the AS issuer instead,
    as the generic UCP spec words it."""
    return urlsplit(merchant.endpoint or merchant.url).netloc


def client_metadata(public_url: str | None) -> dict:
    """Our Client ID Metadata Document: who we are, for servers that accept a URL as client_id."""
    return {
        "client_id": shop_client_id(public_url),
        "client_name": "UCP Shopping Assistant",
        "client_uri": public_url,
        "redirect_uris": [redirect_uri(public_url)],
        "grant_types": ["authorization_code", "refresh_token", TOKEN_EXCHANGE],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": _shop_scopes(),
    }


# ----------------------------------------------------------------- storage


def init_identity() -> None:
    """Create the link table. Call after auth.init_auth(), which creates app_user."""
    with db.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS identity_link (
                username       TEXT NOT NULL REFERENCES app_user(username) ON DELETE CASCADE,
                target         TEXT NOT NULL,          -- 'shop' or a store id
                via            TEXT NOT NULL,          -- 'oauth' (browser flow) or 'chained'
                issuer         TEXT NOT NULL,          -- authorization server that issued the token
                access_token   TEXT NOT NULL,          -- Fernet ciphertext
                refresh_token  TEXT,                   -- Fernet ciphertext
                expires_at     TIMESTAMPTZ,
                scope          TEXT,
                account        TEXT,                   -- display label, e.g. the Shop email
                linked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (username, target)
            )
        """)
    key = _env("IDENTITY_TOKEN_KEY")
    if key and not _fernet():
        logger.error("IDENTITY_TOKEN_KEY is not a valid Fernet key: identity linking is off")


def _encrypt(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, AttributeError):
        return None  # key changed or removed: treat as not linked


def _load(username: str, target: str) -> dict | None:
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM identity_link WHERE username = %s AND target = %s", (username, target)
        ).fetchone()


def _links(username: str) -> list[dict]:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM identity_link WHERE username = %s", (username,)).fetchall()


def _jwt_expiry(token: str) -> datetime | None:
    """The `exp` of a JWT, read without verifying it: only used to know when to renew.
    Shopify's buyer-linked tokens are JWTs and come back without expires_in."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def _save(username: str, target: str, via: str, issuer: str, tokens: dict, account: str | None = None) -> None:
    expires_in = tokens.get("expires_in")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)) if expires_in
                  else _jwt_expiry(tokens["access_token"]))
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO identity_link
                   (username, target, via, issuer, access_token, refresh_token, expires_at, scope, account)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (username, target) DO UPDATE SET
                   via = EXCLUDED.via, issuer = EXCLUDED.issuer,
                   access_token = EXCLUDED.access_token,
                   -- a refresh response may omit the refresh token: keep the one we have
                   refresh_token = COALESCE(EXCLUDED.refresh_token, identity_link.refresh_token),
                   expires_at = EXCLUDED.expires_at, scope = EXCLUDED.scope,
                   account = COALESCE(EXCLUDED.account, identity_link.account)""",
            (username, target, via, issuer, _encrypt(tokens["access_token"]),
             _encrypt(tokens.get("refresh_token")), expires_at, tokens.get("scope"), account),
        )


def _delete(username: str, target: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM identity_link WHERE username = %s AND target = %s", (username, target))


def invalidate(username: str, target: str) -> None:
    """Mark a token as expired (the store rejected it), so the next use refreshes or re-chains."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE identity_link SET expires_at = to_timestamp(0) WHERE username = %s AND target = %s",
            (username, target),
        )


# --------------------------------------------------------------- discovery

_metadata_cache: dict[str, tuple[float, dict]] = {}


def _client() -> httpx.AsyncClient:
    # Never follow redirects: metadata and token endpoints are trusted only at the URL asked for.
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False, transport=transport)


def _well_known(url: str, suffix: str) -> str:
    """RFC 8414 §3.1 / RFC 9728 §3.1: the well-known segment goes between host and path."""
    parts = urlsplit(url)
    path = parts.path if parts.path != "/" else ""
    return f"{parts.scheme}://{parts.netloc}/.well-known/{suffix}{path}"


async def _get_json(url: str) -> tuple[int, dict | None]:
    try:
        async with _client() as http:
            response = await http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise IdentityError(f"cannot reach {url}: {exc}") from exc
    if response.is_success:
        try:
            return response.status_code, response.json()
        except ValueError as exc:
            raise IdentityError(f"{url} did not return JSON") from exc
    return response.status_code, None


async def authorization_server(issuer: str) -> dict:
    """The authorization server's metadata, per the UCP discovery rules."""
    cached = _metadata_cache.get(issuer)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    url = _well_known(issuer, "oauth-authorization-server")
    status, metadata = await _get_json(url)
    if status == 404:
        # OIDC fallback is allowed on 404 only; anything else must abort.
        url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        status, metadata = await _get_json(url)
    if metadata is None:
        raise IdentityError(f"authorization server metadata for {issuer} failed with HTTP {status}")
    if metadata.get("issuer") != issuer:
        raise IdentityError(f"issuer mismatch at {url}: expected {issuer!r}, got {metadata.get('issuer')!r}")

    _metadata_cache[issuer] = (time.monotonic() + METADATA_TTL, metadata)
    return metadata


async def store_issuer(store_url: str) -> str:
    """The issuer protecting a store (RFC 9728), or the store's own origin when it publishes none."""
    url = _well_known(store_url, "oauth-protected-resource")
    status, metadata = await _get_json(url)
    if status == 404:
        parts = urlsplit(store_url)
        return f"{parts.scheme}://{parts.netloc}"
    if metadata is None:
        raise IdentityError(f"protected resource metadata for {store_url} failed with HTTP {status}")
    servers = metadata.get("authorization_servers") or []
    if not servers:
        raise IdentityError(f"{url} lists no authorization_servers")
    return servers[0]


async def _linking_config(merchant) -> dict:
    """The store's identity_linking config block from its UCP profile ({} if none)."""
    entries = ((await merchant.profile()).get("ucp", {}).get("capabilities") or {}).get(CAPABILITY) or []
    return (entries[0].get("config") or {}) if entries else {}


def derived_scopes(config: dict, negotiated: set[str]) -> str:
    """UCP Scope Derivation: config.scopes limited to capabilities we negotiated with the store."""
    scopes = [s for s in (config.get("scopes") or {}) if s.split(":", 1)[0] in negotiated]
    return " ".join(sorted(scopes))


def _store_scope(username: str, merchant, config: dict) -> str:
    """What to ask a store for: the UCP derived set, scopes a 403 told us we lack, and
    SHOPIFY_STORE_SCOPES (default: what Shopify documents for buyer-linked tokens)."""
    wanted = set(derived_scopes(config, merchant.capabilities).split())
    wanted |= _needed_scopes[(username, merchant.id)]
    wanted |= set((_env("SHOPIFY_STORE_SCOPES") or DEFAULT_STORE_SCOPES).split())
    return " ".join(sorted(wanted))


def _trusts_shop(config: dict, own_issuer: str) -> bool:
    for entries in (config.get("providers") or {}).values():
        for entry in entries or []:
            auth_url = (entry.get("auth_url") or "").rstrip("/")
            # A provider pointing back at the store's own server must be ignored.
            if entry.get("type") == "oauth2" and auth_url == SHOP_ISSUER and auth_url != own_issuer.rstrip("/"):
                return True
    return False


# ----------------------------------------------------------- token endpoint


def _client_auth(target: str, metadata: dict, data: dict) -> httpx.BasicAuth | None:
    """Authenticate as our client, with the strongest method both sides support.

    Both are confidential-client secrets, so client_secret_post (what Shopify's docs
    show) is used when offered, else client_secret_basic. Shop accounts without any
    secret falls back to a public client ("none") whose proof is PKCE.
    """
    methods = metadata.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if target == SHOP:
        client_id = shop_client_id(_public_url)
        secret = _shop_secret()
        if not secret:
            if "none" not in methods:
                raise IdentityError("Shop accounts needs a client secret (SHOP_ACCOUNTS_CLIENT_SECRET)")
            data["client_id"] = client_id
            return None
    else:
        credentials = store_credentials(target)
        if not credentials:
            raise IdentityError(f"no client credentials configured for {target}")
        client_id, secret = credentials
    if "client_secret_post" in methods:
        data.update(client_id=client_id, client_secret=secret)
        return None
    if "client_secret_basic" in methods:
        # RFC 6749 §2.3.1: form-encode both parts before Basic encoding.
        return httpx.BasicAuth(quote_plus(client_id), quote_plus(secret))
    raise IdentityError(f"no supported client authentication method among {methods}")


async def _token_request(target: str, metadata: dict, data: dict) -> dict:
    auth = _client_auth(target, metadata, data)
    try:
        async with _client() as http:
            response = await http.post(
                metadata["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        raise IdentityError(f"token endpoint unreachable: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.is_success or "access_token" not in body:
        code = body.get("error")
        # error_description is for people, never parsed (UCP); it is only logged.
        raise IdentityError(f"{code or f'HTTP {response.status_code}'}: {body.get('error_description', '')}".strip(), code)
    return body


# -------------------------------------------------------------- link flows


@dataclass
class _Pending:
    username: str
    target: str
    issuer: str
    verifier: str
    redirect_uri: str
    created: float = field(default_factory=time.monotonic)


_pending: dict[str, _Pending] = {}
_public_url: str | None = None   # set by client.py at startup
_chain_failures: dict[tuple[str, str], tuple[float, str]] = {}
_needed_scopes: dict[tuple[str, str], set[str]] = defaultdict(set)
_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def set_public_url(url: str | None) -> None:
    global _public_url
    _public_url = url


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def start_link(username: str, target: str, registry) -> str:
    """Begin a browser link to Shop accounts or one store; returns the URL to send the browser to."""
    for state in [s for s, p in _pending.items() if time.monotonic() - p.created > STATE_TTL]:
        del _pending[state]

    if target == SHOP:
        issuer = SHOP_ISSUER
        client_id = shop_client_id(_public_url)
        scope = _shop_scopes()
    else:
        merchant = registry.get(target)
        if not merchant or not merchant.supports(CAPABILITY):
            raise IdentityError(f"{target} does not offer identity linking")
        credentials = store_credentials(target)
        if not credentials:
            raise IdentityError(f"no client credentials configured for {target}")
        client_id = credentials[0]
        issuer = await store_issuer(merchant.url)
        scope = _store_scope(username, merchant, await _linking_config(merchant))

    metadata = await authorization_server(issuer)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    callback = redirect_uri(_public_url)
    _pending[state] = _Pending(username, target, issuer, verifier, callback)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    endpoint = metadata["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"


async def finish_link(state: str | None, code: str | None, iss: str | None,
                      error: str | None, registry) -> tuple[str, str]:
    """Complete a browser link from the callback. Returns (username, target)."""
    pending = _pending.pop(state or "", None)   # single use, whatever happens next
    if not pending or time.monotonic() - pending.created > STATE_TTL:
        raise IdentityError("this link request is unknown or has expired; start again")
    if error:
        raise IdentityError(f"the sign-in was not completed ({error})", error)
    if not code:
        raise IdentityError("the authorization server returned no code")

    metadata = await authorization_server(pending.issuer)
    # RFC 9207: a server that sends iss must send the one we discovered.
    if iss is None and metadata.get("authorization_response_iss_parameter_supported"):
        raise IdentityError("the authorization response is missing iss")
    if iss is not None and iss != pending.issuer:
        raise IdentityError(f"issuer mismatch in the authorization response ({iss!r})")

    tokens = await _token_request(pending.target, metadata, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "code_verifier": pending.verifier,
    })
    account = await _account_label(metadata, tokens["access_token"]) if pending.target == SHOP else None
    _save(pending.username, pending.target, "oauth", pending.issuer, tokens, account)
    _needed_scopes.pop((pending.username, pending.target), None)
    logger.info(f"Identity: {pending.username} linked {pending.target}")

    if pending.target == SHOP:
        await chain_all(pending.username, registry)
    else:
        _chain_failures.pop((pending.username, pending.target), None)
    return pending.username, pending.target


async def _account_label(metadata: dict, access_token: str) -> str | None:
    """The Shop account's email, to show which account is linked. Optional."""
    endpoint = metadata.get("userinfo_endpoint")
    if not endpoint:
        return None
    try:
        async with _client() as http:
            response = await http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
        return response.json().get("email") if response.is_success else None
    except (httpx.HTTPError, ValueError):
        return None


async def chain(username: str, merchant) -> bool:
    """Link one store from the user's Shop link (Accelerated IdP Flow). True on success."""
    key = (username, merchant.id)
    try:
        shop_token = await access_token(username, SHOP)
        if not shop_token:
            return False
        issuer = await store_issuer(merchant.url)
        store_metadata = await authorization_server(issuer)
        config = await _linking_config(merchant)
        if not _trusts_shop(config, issuer):
            raise IdentityError("the store does not list Shop accounts as a provider")
        if JWT_BEARER not in (store_metadata.get("grant_types_supported") or []):
            raise IdentityError("the store does not accept JWT authorization grants")

        exchanged = await _token_request(SHOP, await authorization_server(SHOP_ISSUER), {
            "grant_type": TOKEN_EXCHANGE,
            "subject_token": shop_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "audience": issuer if _env("IDENTITY_AUDIENCE") == "issuer" else chain_audience(merchant),
            "requested_token_type": JWT_TOKEN_TYPE,
        })
        if exchanged.get("issued_token_type") != JWT_TOKEN_TYPE:
            raise IdentityError(f"Shop accounts issued {exchanged.get('issued_token_type')!r}, not a JWT grant")

        data = {"grant_type": JWT_BEARER, "assertion": exchanged["access_token"]}
        scope = _store_scope(username, merchant, config)
        if scope:
            data["scope"] = scope
        tokens = await _token_request(merchant.id, store_metadata, data)
        _save(username, merchant.id, "chained", issuer, tokens)
        _chain_failures.pop(key, None)
        _needed_scopes.pop(key, None)
        logger.info(f"Identity: {username} linked {merchant.name} through Shop accounts")
        return True
    except IdentityError as exc:
        # invalid_grant means the store wants the user in the browser: direct OAuth is the fallback.
        _chain_failures[key] = (time.monotonic(), str(exc))
        logger.warning(f"Identity: chaining {merchant.name} for {username} failed: {exc}")
        return False


async def chain_all(username: str, registry) -> None:
    targets = [m for m in registry.all() if linkable(m)]
    await asyncio.gather(*(chain(username, m) for m in targets))


def linkable(merchant) -> bool:
    return merchant.supports(CAPABILITY) and store_credentials(merchant.id) is not None


# ------------------------------------------------------------- token use


def _expired(row: dict) -> bool:
    expires = row.get("expires_at")
    return expires is not None and expires - timedelta(seconds=REFRESH_MARGIN) <= datetime.now(timezone.utc)


async def access_token(username: str, target: str) -> str | None:
    """A usable access token for this link, refreshed or re-chained if it has expired."""
    if _fernet() is None:
        return None
    row = _load(username, target)
    if not row:
        return None
    if not _expired(row):
        return _decrypt(row["access_token"])

    async with _locks[(username, target)]:
        row = _load(username, target)          # another request may have refreshed it meanwhile
        if not row:
            return None
        if not _expired(row):
            return _decrypt(row["access_token"])
        refresh = _decrypt(row["refresh_token"])
        if refresh:
            try:
                metadata = await authorization_server(row["issuer"])
                tokens = await _token_request(target, metadata, {"grant_type": "refresh_token", "refresh_token": refresh})
                _save(username, target, row["via"], row["issuer"], tokens)
                return tokens["access_token"]
            except IdentityError as exc:
                logger.warning(f"Identity: refreshing {target} for {username} failed: {exc}")
                if exc.code not in ("invalid_grant", "invalid_client"):
                    return None             # temporary: keep the link and try next time
        elif row["via"] == "chained":
            _delete(username, target)       # chained tokens are renewed by chaining again
            return None
        _delete(username, target)
        logger.info(f"Identity: {username}'s {target} link expired and was removed; link again")
        return None


async def bearer_for(username: str | None, merchant) -> str | None:
    """The store token to send on this user's calls to this store, or None for a guest call."""
    if not username or not linkable(merchant) or _fernet() is None:
        return None
    token = await access_token(username, merchant.id)
    if token:
        return token
    failure = _chain_failures.get((username, merchant.id))
    if failure and time.monotonic() - failure[0] < CHAIN_RETRY_AFTER:
        return None
    if _load(username, SHOP) and await chain(username, merchant):
        return await access_token(username, merchant.id)
    return None


def note_insufficient_scope(username: str, target: str, challenge: str | None) -> None:
    """A store answered 403 insufficient_scope: ask for its `scope` on the next link (RFC 6750 §3)."""
    match = re.search(r'scope="([^"]*)"', challenge or "")
    if match:
        _needed_scopes[(username, target)].update(match[1].split())


# ------------------------------------------------------------------ unlink


async def _revoke(target: str, issuer: str, token: str | None, hint: str) -> None:
    if not token:
        return
    try:
        metadata = await authorization_server(issuer)
        endpoint = metadata.get("revocation_endpoint")
        if not endpoint:
            return
        data = {"token": token, "token_type_hint": hint}
        auth = _client_auth(target, metadata, data)
        async with _client() as http:
            response = await http.post(endpoint, data=data, auth=auth)
        if not response.is_success:
            logger.warning(f"Identity: revoking {target} returned HTTP {response.status_code}")
    except (IdentityError, httpx.HTTPError) as exc:
        logger.warning(f"Identity: revoking {target} failed: {exc}")


async def unlink(username: str, target: str) -> None:
    """Revoke the link's tokens at its server (RFC 7009), then forget it.

    Unlinking Shop also unlinks the stores chained from it: revocation does not
    propagate down the chain, so each layer is revoked on its own.
    """
    targets = [target]
    if target == SHOP:
        targets += [row["target"] for row in _links(username) if row["via"] == "chained"]
    for name in targets:
        row = _load(username, name)
        if not row:
            continue
        await _revoke(name, row["issuer"], _decrypt(row["refresh_token"]), "refresh_token")
        await _revoke(name, row["issuer"], _decrypt(row["access_token"]), "access_token")
        _delete(username, name)
        _chain_failures.pop((username, name), None)
        logger.info(f"Identity: {username} unlinked {name}")


# ------------------------------------------------------------------ status


def status(username: str, registry) -> dict:
    """What the frontend shows: whether linking is on, the Shop link, and each store."""
    is_enabled = enabled(registry.all())
    links = {row["target"]: row for row in _links(username)} if is_enabled else {}
    shop = links.get(SHOP)
    stores = []
    for merchant in registry.all():
        link = links.get(merchant.id)
        failure = _chain_failures.get((username, merchant.id))
        stores.append({
            "id": merchant.id,
            "name": merchant.name,
            "supported": merchant.supports(CAPABILITY),
            "configured": store_credentials(merchant.id) is not None,
            "linked": link is not None,
            "via": link["via"] if link else None,
            "error": failure[1] if failure and not link else None,
        })
    return {
        "enabled": is_enabled,
        "shop": {"linked": shop is not None, "account": shop["account"] if shop else None},
        "stores": stores,
    }
