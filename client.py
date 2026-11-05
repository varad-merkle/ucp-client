"""UCP client (platform) for the stores in merchants.json, and the backend of ucp-frontend.

The stores fetch our UCP profile from our public URL: PUBLIC_URL in production (e.g. the
Heroku app's address), or else an ngrok tunnel opened on startup for local development.
On startup it then reads each store's /.well-known/ucp to find its MCP endpoint
(merchants.py). Every store call goes through call_mcp(), which adds meta.ucp-agent (and
an idempotency key where UCP requires one) and logs the call.

Logins, chat sessions and identity links in progress are kept in PostgreSQL, so any
number of workers or dynos can serve a user, and a restart or deploy loses nothing.

The exchange log and the direct /api/mcp/... store endpoints are development tools: they
exist only with DEV_ENDPOINTS=1, and then need a login too. The log holds buyers' personal
details and is only recorded while they are on.

Each user logs in with an account from the app_user table (auth.py) and gets a token. The
endpoints marked (auth) below need it as `Authorization: Bearer <token>`, and act on
the buyer profile belonging to that user - their own name, email and market
preferences are what reach the stores over UCP.

    GET  /profile.json                our UCP platform profile
    POST /api/login                   log in, returns a token
    POST /api/logout                  (auth) invalidate the token
    GET  /api/me                      (auth) who the token belongs to
    GET  /api/profile                 (auth) get this user's buyer profile
    PATCH /api/profile                (auth) update this user's buyer or market preferences
    POST /api/chat                    (auth) one chat turn (agent.py + shop.py)
    POST /api/checkout/details        (auth) the checkout card's delivery details form
    POST /api/checkout/start          (auth) a cart card's Checkout button, for one store
    GET  /api/identity                (auth) identity linking status: Shop account and each store
    POST /api/identity/link           (auth) start linking Shop accounts or one store; returns the URL
    GET  /api/identity/callback       where the authorization server sends the browser back
    DELETE /api/identity/{target}     (auth) unlink (revokes the tokens first)
    GET  /oauth/client.json           our OAuth Client ID Metadata Document
    GET  /api/health                  status of this client and the stores
    GET  /api/merchants               the stores, their categories and what discovery found
    GET  /api/exchanges               (dev, auth) recent store calls (exchange log)
    DELETE /api/exchanges             (dev, auth)
    POST /api/mcp/catalog/...         (dev, auth) direct catalog, cart and checkout calls to one
    POST /api/mcp/cart/...            store, chosen with ?merchant=<id> (default pier1)
    POST /api/mcp/checkout/...

Run locally:  uv run python client.py
Production:   Heroku runs the Procfile (uvicorn on $PORT, $WEB_CONCURRENCY workers); set
              PUBLIC_URL and the other config vars listed in .env.example.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import urlencode

import httpx
import httpx2
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import TextContent
from pydantic import BaseModel

# pyngrok and ucp-sdk ship without type information (no py.typed), so there are no stubs to find.
from pyngrok import ngrok  # pyright: ignore[reportMissingTypeStubs]
from pyngrok.exception import PyngrokNgrokHTTPError  # pyright: ignore[reportMissingTypeStubs]
from ucp_sdk.models.schemas.shopping.cart_create_request import (  # pyright: ignore[reportMissingTypeStubs]
    CartCreateRequest,
)
from ucp_sdk.models.schemas.shopping.cart_update_request import (  # pyright: ignore[reportMissingTypeStubs]
    CartUpdateRequest,
)
from ucp_sdk.models.schemas.shopping.catalog_lookup import (  # pyright: ignore[reportMissingTypeStubs]
    GetProductRequest,
    LookupRequest,
)
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest  # pyright: ignore[reportMissingTypeStubs]
from ucp_sdk.models.schemas.shopping.checkout_create_request import (  # pyright: ignore[reportMissingTypeStubs]
    CheckoutCreateRequest,
)
from ucp_sdk.models.schemas.shopping.checkout_update_request import (  # pyright: ignore[reportMissingTypeStubs]
    CheckoutUpdateRequest,
)
from ucp_sdk.models.schemas.shopping.types.line_item_create_request import (  # pyright: ignore[reportMissingTypeStubs]
    LineItemCreateRequest,
)

import auth
import db
import identity
from agent import Agent
from merchants import CART, CHECKOUT, SEARCH, Merchant, Registry
from profile_store import get_profile, init_db, update_profile
from shop import JSON, Shop, fields, init_sessions, items

load_dotenv()

# INFO keeps shoppers' personal details (messages, names, emails, addresses) out of the logs;
# LOG_LEVEL=DEBUG adds them, for local debugging only.
logging.basicConfig(level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ucp-client")

# Keep the terminal readable: hide the libraries' own request-by-request logs.
for noisy in ("pyngrok", "httpx", "httpx2", "mcp"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

PORT = 7000
PROFILE_PATH = Path(__file__).parent / "profile.json"

# The store used by the direct /api/mcp/... endpoints when no ?merchant= is given.
DEFAULT_MERCHANT = "pier1"

LOOKUP = "dev.ucp.shopping.catalog.lookup"

TOOL_CAPABILITIES = {
    "search_catalog": SEARCH,
    "lookup_catalog": LOOKUP,
    "get_product": LOOKUP,
    "create_cart": CART,
    "get_cart": CART,
    "update_cart": CART,
    "cancel_cart": CART,
    "create_checkout": CHECKOUT,
    "get_checkout": CHECKOUT,
    "update_checkout": CHECKOUT,
    "complete_checkout": CHECKOUT,
    "cancel_checkout": CHECKOUT,
}

# The errors a direct store endpoint can answer with (merchant_or_404 and call_mcp),
# listed in each endpoint's OpenAPI `responses`.
STORE_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"description": "The linked store account lacks a permission this call needs"},
    404: {"description": "Unknown store"},
    502: {"description": "The store call failed"},
    503: {"description": "The store is unavailable"},
}

# The UCP spec requires meta.idempotency-key for these tools.
IDEMPOTENT_TOOLS = {"cancel_cart", "complete_checkout", "cancel_checkout"}

# Stores now and then fail a call with a passing "Internal error"; the same call
# usually works a moment later, so it is tried once more before giving up.
RETRY_ERRORS = {"internal error"}
RETRY_DELAY = 1.0  # seconds

# Set on startup: our public URL (PUBLIC_URL, or the ngrok tunnel's).
public_url: str | None = None

# The development tools (exchange log, /api/mcp/...): off unless DEV_ENDPOINTS=1.
DEV_ENDPOINTS = os.environ.get("DEV_ENDPOINTS", "").strip() == "1"

# The stores, from merchants.json; discovered on startup.
registry = Registry()

# Recent calls to the stores, newest first, served by /api/exchanges. Recorded only with
# DEV_ENDPOINTS on; held per process, which is fine for a development tool.
exchanges: list[JSON] = []
MAX_EXCHANGES = 200


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global public_url

    # 1. Prepare the database before any request is served. Accounts come first:
    #    a buyer profile belongs to a user, so app_user must exist before it. Every
    #    worker runs this, so they take turns (db.schema_lock).
    with db.schema_lock():
        auth.init_auth()
        init_db()
        identity.init_identity()
        init_sessions()
    logger.info("Database ready")

    # 2. Our public URL, where the stores fetch our profile.
    public_url, tunnel = _open_public_url()
    identity.set_public_url(public_url)
    logger.info(f"Public profile URL: {public_url}/profile.json")

    # 3. Read each store's /.well-known/ucp for its MCP endpoint, version and capabilities.
    await registry.discover_all(read_profile())
    _log_startup(local=tunnel is not None)

    yield
    if tunnel:
        ngrok.disconnect(tunnel)
    db.close_pools()


def _open_public_url() -> tuple[str | None, str | None]:
    """(public URL, ngrok tunnel to close at shutdown). PUBLIC_URL when it is set (production),
    else an ngrok tunnel to this machine (local development)."""
    configured = (os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")
    if configured:
        return configured, None
    try:
        tunnel = ngrok.connect(addr=str(PORT)).public_url
    except PyngrokNgrokHTTPError as exc:
        # Free ngrok accounts allow one tunnel, so a second client.py can't start.
        raise RuntimeError(
            "Could not open the ngrok tunnel. Is client.py already running in another terminal? "
            "In production, set PUBLIC_URL instead."
        ) from exc
    return tunnel, tunnel


def _log_startup(local: bool) -> None:
    """What the stores and settings turned out to be, once startup is done."""
    available = [m.name for m in registry.all() if not m.error]
    logger.info(f"{len(available)} of {len(registry.all())} stores available: {', '.join(available)}")
    if identity.enabled(registry.all()):
        linkable = [m.name for m in registry.all() if identity.linkable(m)]
        logger.info(f"Identity linking on for: {', '.join(linkable) or 'no store yet'}")
    else:
        logger.info("Identity linking off: set IDENTITY_TOKEN_KEY and SHOPIFY_CLIENT_ID/SECRET in .env to turn it on")
    if DEV_ENDPOINTS:
        logger.warning("DEV_ENDPOINTS=1: the exchange log and /api/mcp/... are on. Keep them off in production.")
    if local:
        logger.info(f"Ready on http://localhost:{PORT} - start the frontend and open http://localhost:5173")
    else:
        logger.info(f"Ready at {public_url}")


app = FastAPI(lifespan=lifespan)

# Browsers reach this API through the frontend's own address (Vercel rewrites /api/* to
# Heroku; locally the Vite dev server proxies it), so no other site needs to call it and
# CORS stays off. ALLOWED_ORIGINS (comma-separated) allows specific sites if ever needed.
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_headers=["Authorization", "Content-Type"],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
    )


def read_profile() -> JSON:
    with PROFILE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------- store calls


def error_message(exc: BaseException) -> str:
    # The MCP client wraps errors in exception groups; show the real one.
    while isinstance(exc, BaseExceptionGroup):
        exc = cast(BaseExceptionGroup[BaseException], exc).exceptions[0]
    return str(exc) or type(exc).__name__


def summarize(response: JSON) -> str:
    """A one-line description of a store response, for the terminal."""
    parts: list[str] = []
    if "products" in response:
        parts.append(f"{len(items(response['products']))} products")
    if response.get("product"):
        parts.append(f"product '{response['product'].get('title')}'")
    if "line_items" in response:
        # Checkouts carry a status (incomplete, ready_for_complete, ...); carts don't.
        kind = f"checkout ({response['status']})" if response.get("status") else "cart"
        parts.append(f"{kind} {response.get('id')} with {len(items(response['line_items']))} line items")
    if fields(response.get("pagination")).get("has_next_page"):
        parts.append("more pages available")
    for message in items(response.get("messages")):
        parts.append(f"{message.get('type')}: {message.get('content')}")
    return ", ".join(parts) or "empty response"


def log_exchange(merchant: Merchant, tool: str, arguments: JSON, started: float, status: int,
                 response: JSON | None = None, error: str | None = None, linked: bool = False) -> None:
    if not DEV_ENDPOINTS:  # the log holds buyers' personal details: kept only while debugging
        return
    exchanges.insert(0, {
        "id": str(uuid.uuid4()),
        "merchant": merchant.id,
        "capability": TOOL_CAPABILITIES.get(tool, "discovery"),
        "method": tool,
        "url": merchant.endpoint,
        "started_at": datetime.now(UTC).isoformat(),
        "status": status,
        "ok": error is None and fields(response).get("ucp", {}).get("status") != "error",
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "request_headers": {"UCP-Agent": f'profile="{public_url}/profile.json"',
                            # The token itself is never logged.
                            **({"Authorization": "Bearer ***"} if linked else {})},
        "request_body": arguments,
        "response_body": response,
        "error": error,
    })
    del exchanges[MAX_EXCHANGES:]


async def call_mcp(merchant: Merchant, tool: str, arguments: JSON) -> JSON:
    """
    Call one of a store's UCP MCP tools and return its JSON result.
    `arguments` are the tool's own parameters; the `meta` block is added here,
    with an idempotency key for the tools whose spec requires one.
    """
    meta: JSON = {"ucp-agent": {"profile": f"{public_url}/profile.json"}}
    if tool in IDEMPOTENT_TOOLS:
        meta["idempotency-key"] = str(uuid.uuid4())
    arguments = {"meta": meta, **arguments}

    # A linked user's calls carry that store's token (identity.py); everyone else is a guest.
    username = identity.acting_user.get()
    token = await identity.bearer_for(username, merchant)
    retries: set[str] = set()   # which one-off retries ("auth", "error") have been used

    while True:
        # The arguments can hold the buyer's name, email, phone and address: INFO names the
        # fields only, the values are logged at DEBUG (LOG_LEVEL=DEBUG, for local debugging).
        body = {k: v for k, v in arguments.items() if k != "meta"}
        logger.info(f"  -> {merchant.id}.{tool}{' (linked)' if token else ''} fields: {', '.join(body) or 'none'}")
        logger.debug(f"     {json.dumps(body)}")
        started = time.perf_counter()
        challenge: JSON = {}
        try:
            response = await _call_once(merchant, tool, arguments, token, challenge)
            break
        # Deliberately broad: every failure is logged, then retried or raised as an HTTP error.
        except Exception as exc:  # noqa: BLE001
            message = error_message(exc)
            logger.error(f"  <- {merchant.id}.{tool} failed: {message}")
            log_exchange(merchant, tool, arguments, started, challenge.get("status", 502), error=message,
                         linked=bool(token))
            token = await _recover(merchant, tool, message, challenge, token, username, retries)
    log_exchange(merchant, tool, arguments, started, 200, response, linked=bool(token))
    elapsed = round((time.perf_counter() - started) * 1000)
    outcome = "returned an error" if response.get("ucp", {}).get("status") == "error" else "OK"
    logger.info(f"  <- {merchant.id}.{tool} {outcome} in {elapsed} ms: {summarize(response)}")
    return response


async def _call_once(merchant: Merchant, tool: str, arguments: JSON, token: str | None, challenge: JSON) -> JSON:
    """One tool call. Returns the UCP response, or raises if the store did not give one."""
    async with _store_client(merchant, token, challenge) as mcp_client:
        result = await mcp_client.call_tool(tool, arguments)
    text = next((block.text for block in result.content if isinstance(block, TextContent)), "")
    # UCP returns its response in structuredContent; the text block is the same data as JSON.
    response: JSON | None
    try:
        response = result.structured_content or json.loads(text)
    except json.JSONDecodeError:
        response = None
    # Business outcomes (e.g. cart not found) still come back as a UCP response,
    # even when the tool call is flagged as an error. Anything else is a real failure.
    if not isinstance(response, dict) or (result.is_error and "ucp" not in response):
        raise RuntimeError(text or "the store returned a tool error")
    return response


async def _recover(merchant: Merchant, tool: str, message: str, challenge: JSON, token: str | None,
                   username: str | None, retries: set[str]) -> str | None:
    """After a failed call: return the token to retry with (None = as a guest), or raise.

    Each kind of retry happens at most once; `retries` records the ones already used.
    """
    status = challenge.get("status")
    if token and username and status == 401 and "auth" not in retries:   # a token means a linked user
        # identity_required: the token was rejected. Refresh or re-chain once,
        # and if that gives nothing, carry on as a guest (linking never gates).
        retries.add("auth")
        identity.invalidate(username, merchant.id)
        return await identity.bearer_for(username, merchant)
    if token and status == 422 and "auth" not in retries:
        # Shopify rejects a linked call without a usable buyer IP (e.g. 127.0.0.1
        # in local development): fall back to a guest call rather than fail.
        logger.warning(f"  {merchant.name} refused the linked call (HTTP 422); retrying as a guest")
        retries.add("auth")
        return None
    if token and username and status == 403:
        identity.note_insufficient_scope(username, merchant.id, challenge.get("www_authenticate"))
        # Documented on the endpoints as STORE_ERRORS (Sonar only reads responses written inline).
        raise HTTPException(status_code=403, detail=(  # NOSONAR
            f"{merchant.name} needs more permission for {tool}: link your {merchant.name} account again"))
    if "error" not in retries and message.strip().lower() in RETRY_ERRORS:
        retries.add("error")
        await asyncio.sleep(RETRY_DELAY)
        return token
    raise HTTPException(status_code=502, detail=f"{merchant.name} {tool} failed: {message}")  # NOSONAR


def _store_client(merchant: Merchant, token: str | None, challenge: JSON) -> Client:
    """An MCP client for one store call. Guest calls are unchanged; linked calls send the
    store's token and record a 401/403 with its WWW-Authenticate challenge (RFC 6750 §3),
    which the MCP library would otherwise reduce to a generic error."""
    if not merchant.endpoint:  # merchant_or_404 has already turned away stores discovery failed for
        raise RuntimeError(f"{merchant.name} has no MCP endpoint")
    if not token:
        return Client(merchant.endpoint)

    # Must stay async even without an await: httpx awaits an AsyncClient's event hooks.
    async def watch(response: httpx2.Response) -> None:  # NOSONAR
        if response.status_code in (401, 403, 422):
            challenge["status"] = response.status_code
            challenge["www_authenticate"] = response.headers.get("www-authenticate")

    headers = {"Authorization": f"Bearer {token}"}
    buyer_ip = identity.acting_buyer_ip.get()
    if buyer_ip:
        headers["Shopify-Buyer-IP"] = buyer_ip   # required by Shopify on Token-tier tool calls
    http = create_mcp_http_client(headers=headers)
    http.event_hooks["response"].append(watch)
    return Client(_closing(http, streamable_http_client(merchant.endpoint, http_client=http)))


_Streams = TypeVar("_Streams")


@asynccontextmanager
async def _closing(http: httpx2.AsyncClient,
                   transport: AbstractAsyncContextManager[_Streams]) -> AsyncGenerator[_Streams, None]:
    """A transport that also closes the HTTP client it was given."""
    async with http, transport as streams:
        yield streams


def merchant_or_404(merchant_id: str) -> Merchant:
    # Both errors are documented on the endpoints as STORE_ERRORS (Sonar only reads responses written inline).
    merchant = registry.get(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail=f"Unknown merchant {merchant_id}")  # NOSONAR
    if merchant.error:
        raise HTTPException(status_code=503, detail=f"{merchant.name} is unavailable: {merchant.error}")  # NOSONAR
    return merchant


async def call_catalog(merchant_id: str, tool: str, catalog: JSON) -> JSON:
    """Catalog tools take their parameters under `catalog`."""
    return await call_mcp(merchant_or_404(merchant_id), tool, {"catalog": catalog})


async def call_store(merchant_id: str, tool: str, arguments: JSON) -> JSON:
    return await call_mcp(merchant_or_404(merchant_id), tool, arguments)


def check_ucp_status(response: JSON, action: str) -> JSON:
    """
    UCP reports business outcomes (e.g. cart not found) as a normal result with
    ucp.status "error" and the reason in `messages`. Turn those into HTTP errors.
    """
    if response.get("ucp", {}).get("status") != "error":
        return response
    msgs = items(response.get("messages"))
    detail = "; ".join(m.get("content", "") for m in msgs) or f"{action} failed"
    status = 404 if any(str(m.get("code", "")).endswith("not_found") for m in msgs) else 422
    raise HTTPException(status_code=status, detail=detail)


shop = Shop(registry, call_mcp)
agent = Agent(shop)


# ------------------------------------------------------------ endpoints


@app.get("/profile.json")
async def get_agent_profile(request: Request):
    """
    Return this agent's UCP profile. UCP requires Cache-Control with `public` and a
    max-age of at least 60 seconds, and recommends a validator such as ETag.
    """
    client_host = request.client.host if request.client else "unknown"
    logger.info(f"Profile fetched by: {client_host} ({request.headers.get('user-agent')})")
    profile = read_profile()
    etag = '"' + hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:32] + '"'
    headers = {"Cache-Control": "public, max-age=3600", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(profile, headers=headers)


# ------------------------------------------------------------ login


class LoginRequest(BaseModel):
    username: str
    password: str


def current_user(authorization: str = Header(default="")) -> str:
    """The logged-in user for this request, from `Authorization: Bearer <token>`.

    Endpoints that touch a buyer profile depend on this, so the username they act
    on always comes from a verified token and never from the request body.
    """
    token = authorization.removeprefix("Bearer ").strip()
    username = auth.username_for(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="Log in to continue")
    return username


@app.post("/api/login", responses={
    401: {"description": "Wrong username or password"},
    429: {"description": "Too many failed logins; try again later"},
})
async def log_in(body: LoginRequest, request: Request):
    """Exchange an account's username and password for a token."""
    try:
        token = auth.login(body.username, body.password, request.client.host if request.client else None)
    except auth.TooManyAttemptsError:
        logger.warning("Login refused: too many failed attempts for this account or address")
        raise HTTPException(status_code=429, detail="Too many failed logins. Try again in a few minutes.",
                            headers={"Retry-After": str(auth.FAILURE_WINDOW_MINUTES * 60)}) from None
    if not token:
        # The same message either way: it must not reveal which accounts exist. The attempted
        # username is not logged: people sometimes type their password into that field.
        logger.info("Login failed")
        raise HTTPException(status_code=401, detail="Wrong username or password")
    logger.info(f"Logged in: {body.username}")
    return {"token": token, "username": body.username}


@app.post("/api/logout")
async def log_out(authorization: str = Header(default=""), username: str = Depends(current_user)):
    """Invalidate this token. The user's saved profile is untouched."""
    auth.logout(authorization.removeprefix("Bearer ").strip())
    logger.info(f"Logged out: {username}")
    return {"status": "logged out"}


@app.get("/api/me")
async def who_am_i(username: str = Depends(current_user)):
    """Who the token belongs to. Lets the frontend check a stored token on page load."""
    return {"username": username}


# ------------------------------------------------------------ buyer profile


class BuyerUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None


class MarketUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    country: str | None = None
    preferred_currency: str | None = None
    language: str | None = None
    region: str | None = None
    postal_code: str | None = None


class ProfilePatch(BaseModel):
    model_config = {"extra": "forbid"}
    buyer: BuyerUpdate | None = None
    market: MarketUpdate | None = None


@app.get("/api/profile")
async def read_buyer_profile(username: str = Depends(current_user)):
    """Return this user's buyer profile (buyer info + market preferences)."""
    return get_profile(username)


@app.patch("/api/profile", responses={422: {"description": "Unknown field or invalid value"}})
async def patch_buyer_profile(body: ProfilePatch, username: str = Depends(current_user)):
    """Partially update this user's buyer profile.

    Only explicitly provided fields are changed. An explicit null clears a field.
    Fields absent from the request body are preserved unchanged.
    """
    buyer = body.buyer.model_dump(exclude_unset=True) if body.buyer else {}
    market = body.market.model_dump(exclude_unset=True) if body.market else {}
    try:
        return update_profile(username, buyer, market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ------------------------------------------------------------ identity linking


class LinkRequest(BaseModel):
    target: str  # "shop" for Shop accounts (links every store that trusts it), or a store id


def _frontend(**params: str) -> str:
    base = (os.environ.get("FRONTEND_URL") or "http://localhost:5173").rstrip("/")
    return f"{base}/?{urlencode(params)}"


@app.get("/oauth/client.json")
async def oauth_client_metadata():
    """Our Client ID Metadata Document, used as client_id where no client is registered."""
    return JSONResponse(identity.client_metadata(public_url), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/identity")
async def identity_status(username: str = Depends(current_user)):
    return identity.status(username, registry)


@app.post("/api/identity/link", responses={
    404: {"description": "Unknown store"},
    409: {"description": "Identity linking is not configured on this server"},
    502: {"description": "The authorization server could not be used"},
})
async def identity_link(body: LinkRequest, username: str = Depends(current_user)):
    """Start a link. The frontend sends the browser to the returned URL."""
    if not identity.enabled(registry.all()):
        raise HTTPException(status_code=409, detail="Identity linking is not configured on this server")
    if body.target != identity.SHOP and not registry.get(body.target):
        raise HTTPException(status_code=404, detail=f"Unknown store {body.target}")
    try:
        return {"authorize_url": await identity.start_link(username, body.target, registry)}
    except identity.IdentityError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/identity/callback")
async def identity_callback(state: str | None = None, code: str | None = None,
                            iss: str | None = None, error: str | None = None):
    """The authorization server sends the browser here. No login token arrives with it:
    the user is the one the single-use `state` was issued to."""
    try:
        _, target = await identity.finish_link(state, code, iss, error, registry)
    except identity.IdentityError as exc:
        logger.warning(f"Identity: link failed: {exc}")
        return RedirectResponse(_frontend(identity="error", reason=str(exc)), status_code=303)
    return RedirectResponse(_frontend(identity="linked", target=target), status_code=303)


@app.delete("/api/identity/{target}")
async def identity_unlink(target: str, username: str = Depends(current_user)):
    await identity.unlink(username, target)
    return identity.status(username, registry)


# ------------------------------------------------------------ chat


def _extract_signals(request: Request) -> JSON:
    """Build a UCP signals dict from the HTTP request.

    Uses the direct client address (request.client.host) rather than X-Forwarded-For
    to avoid blindly trusting proxy headers without a configured trust relationship.
    These signals are forwarded to the store in UCP requests; they are not stored in
    the buyer profile database.

    Supported by the official UCP SignalsCreateRequest schema:
      - dev.ucp.buyer_ip
      - dev.ucp.user_agent
    """
    signals: JSON = {}
    if request.client and request.client.host:
        signals["dev.ucp.buyer_ip"] = request.client.host
    user_agent = request.headers.get("user-agent")
    if user_agent:
        signals["dev.ucp.user_agent"] = user_agent
    return signals


def _act_for(username: str, request: Request) -> None:
    """Store calls made while handling this request act for this user (identity.py)."""
    identity.acting_user.set(username)
    identity.acting_buyer_ip.set(request.client.host if request.client else None)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
async def chat_turn(body: ChatRequest, request: Request, username: str = Depends(current_user)):
    """One chat turn: the reply is a list of blocks the frontend renders."""
    logger.info(f"Chat [{username}]: message of {len(body.message)} characters")
    logger.debug(f"Chat [{username}]: {body.message!r}")
    _act_for(username, request)
    signals = _extract_signals(request)
    reply = await agent.reply(body.message, body.session_id, username, signals)
    logger.info(f"Reply: {', '.join(block['type'] for block in reply['blocks'])}")
    return reply


class CheckoutDetails(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    street_address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None


class CheckoutDetailsRequest(BaseModel):
    session_id: str
    details: CheckoutDetails


@app.post("/api/checkout/details")
async def checkout_details(body: CheckoutDetailsRequest, request: Request,
                           username: str = Depends(current_user)):
    """The checkout card's delivery details form: saved on the checkout in one update."""
    logger.info(f"Chat [{username}]: delivery details form submitted")
    _act_for(username, request)
    details = {k: (v.strip() or None) if isinstance(v, str) else v for k, v in body.details.model_dump().items()}
    signals = _extract_signals(request)
    return await agent.submit_details(body.session_id, details, username, signals)


class StartCheckoutRequest(BaseModel):
    session_id: str
    store: str


@app.post("/api/checkout/start", responses={404: {"description": "Unknown store"}})
async def start_checkout(body: StartCheckoutRequest, request: Request,
                         username: str = Depends(current_user)):
    """A cart card's Checkout button: opens the checkout for that card's store."""
    if not registry.get(body.store):
        raise HTTPException(status_code=404, detail=f"Unknown store {body.store}")
    logger.info(f"Chat [{username}]: checkout button pressed for {body.store}")
    _act_for(username, request)
    reply = await agent.start_checkout(body.session_id, body.store, username, _extract_signals(request))
    logger.info(f"Reply: {', '.join(block['type'] for block in reply['blocks'])}")
    return reply


async def probe(merchant: Merchant) -> tuple[int, int]:
    """A store's /.well-known/ucp status code and latency in ms (status 0 if unreachable)."""
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            status = (await client.get(f"{merchant.url}/.well-known/ucp")).status_code
    except httpx.HTTPError:
        status = 0
    return status, round((time.perf_counter() - started) * 1000)


@app.get("/api/health")
async def health() -> JSON:
    """Status of this client and of the stores it talks to (the sidebar shows one summary)."""
    merchants = registry.all()
    probes = await asyncio.gather(*(probe(m) for m in merchants))
    online = [m for m, (status, _) in zip(merchants, probes) if status == 200 and not m.error]
    return {
        "agent": {
            "status": "ok",
            "version": read_profile()["ucp"]["version"],
            "profile_url": f"{public_url}/profile.json",
            "merchant_url": ", ".join(m.url for m in merchants),
        },
        "merchant": {
            "reachable": bool(online),
            "status": 200 if online else 0,
            "latency_ms": max(latency for _, latency in probes),
            "name": f"{len(online)} of {len(merchants)} stores online",
        },
        # One row per store for the sidebar; the first listed category is the main one.
        "stores": [
            {
                "id": m.id,
                "name": m.name,
                "url": m.url,
                "logo": m.logo,
                "category": m.categories[0] if m.categories else None,
                "online": status == 200 and not m.error,
            }
            for m, (status, _) in zip(merchants, probes)
        ],
    }


@app.get("/api/merchants")
async def list_merchants():
    return [m.summary() for m in registry.all()]


# ------------------------------------------------------- development tools


def dev_tools(authorization: str = Header(default="")) -> str:
    """Gate for the development tools: they don't exist unless DEV_ENDPOINTS=1, and then need a login."""
    if not DEV_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Not Found")
    return current_user(authorization)


# Routes registered on `dev` are included in the app at the end of this file.
dev = APIRouter(dependencies=[Depends(dev_tools)])


@dev.get("/api/exchanges")
async def list_exchanges(limit: int = 40) -> JSON:
    return {"exchanges": exchanges[:max(1, min(limit, MAX_EXCHANGES))], "total": len(exchanges)}


@dev.delete("/api/exchanges")
async def clear_exchanges():
    exchanges.clear()
    return Response(status_code=204)


# ------------------------------------------- direct catalog, cart, checkout


class GetCartRequest(BaseModel):
    id: str


class MCPUpdateCartRequest(BaseModel):
    id: str
    cart: CartUpdateRequest


class CancelCartRequest(BaseModel):
    id: str


class CheckoutCreateMCPRequest(CheckoutCreateRequest):
    # UCP lets a checkout start from an existing cart (cart_id) instead of line items, so this
    # model deliberately makes the parent's required line_items optional.
    line_items: list[LineItemCreateRequest] | None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    cart_id: str | None = None


class GetCheckoutRequest(BaseModel):
    id: str


class MCPUpdateCheckoutRequest(BaseModel):
    id: str
    checkout: CheckoutUpdateRequest


# Used by the complete (payment) endpoint, which is switched off for now.
# class MCPCompleteCheckoutRequest(BaseModel):
#     id: str
#     checkout: CheckoutCompleteRequest


class CancelCheckoutRequest(BaseModel):
    id: str


@dev.post("/api/mcp/catalog/search/", responses=STORE_ERRORS)
async def search_products_mcp(query: SearchRequest, merchant: str = DEFAULT_MERCHANT):
    """Search products via an MCP client."""
    response = await call_catalog(merchant, "search_catalog", {"query": query.query})
    return response.get("products")


@dev.post("/api/mcp/catalog/lookup", responses=STORE_ERRORS)
async def lookup_products_mcp(request: LookupRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Lookup one or more products via an MCP client.
    Requires a list of one or more valid product IDs.
    The response from the MCP server can be one of three possible options:
        1. Success: A list of one or more products.
        2. Partial success: Not all specified products were found. Messages data indicates which products could not be found and why.
        3. Error: Not a single product specified by the product IDs could be found.
    """
    response = await call_catalog(merchant, "lookup_catalog", {"ids": sorted(set(request.ids))})
    products = items(response.get("products"))
    messages = items(response.get("messages"))
    if not products:
        logger.info("No products found. Ensure entered IDs are valid.")
        return messages
    if not messages:
        return products
    logger.info("Product search returned partial results. Some IDs are invalid.")
    for message in messages:
        logger.info(f"Type: {message.get('type')}, Code: {message.get('code')}, Content: {message.get('content')}")
    return {"products": products, "messages": messages}


@dev.post("/api/mcp/catalog/product", responses=STORE_ERRORS)
async def get_product_detail(request: GetProductRequest, merchant: str = DEFAULT_MERCHANT):
    response = await call_catalog(merchant, "get_product", {"id": request.id})
    if response.get("product"):
        return response["product"]
    logger.info("No products found. Ensure entered IDs are valid.")
    return items(response.get("messages"))


@dev.post("/api/mcp/cart/create", responses=STORE_ERRORS)
async def create_cart_mcp(request: CartCreateRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Create a new cart via an MCP client.
    Expects a list of Variant IDs and quantities.
    """
    cart = request.model_dump(mode="json", exclude_none=True)
    return check_ucp_status(await call_store(merchant, "create_cart", {"cart": cart}), "Cart creation")


@dev.post("/api/mcp/cart/get", responses=STORE_ERRORS)
async def get_cart_mcp(request: GetCartRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Fetch an existing cart via an MCP client.
    Expects a valid Cart ID.
    """
    return check_ucp_status(await call_store(merchant, "get_cart", {"id": request.id}), "Cart fetch")


@dev.post("/api/mcp/cart/update", responses=STORE_ERRORS)
async def update_cart_mcp(request: MCPUpdateCartRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Update an existing cart via an MCP client.
    UCP: the platform must send the entire cart; line_items fully replaces the cart's contents.
    """
    cart = request.cart.model_dump(mode="json", exclude_none=True)
    return check_ucp_status(await call_store(merchant, "update_cart", {"id": request.id, "cart": cart}), "Cart update")


@dev.post("/api/mcp/cart/cancel", responses=STORE_ERRORS)
async def cancel_cart_mcp(request: CancelCartRequest, merchant: str = DEFAULT_MERCHANT):
    """Cancel an existing cart via an MCP client."""
    response = check_ucp_status(await call_store(merchant, "cancel_cart", {"id": request.id}), "Cart cancellation")
    logger.info(f"Cart cancelled: {request.id}")
    return response


@dev.post("/api/mcp/checkout/create",
           responses={**STORE_ERRORS, 400: {"description": "Neither cart_id nor line_items was given"}})
async def create_checkout_mcp(request: CheckoutCreateMCPRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Create a checkout via an MCP client.
    Accepts either a cart_id (cart-to-checkout conversion) or line_items directly.
    """
    checkout = request.model_dump(mode="json", exclude_none=True)
    if not checkout.get("cart_id") and not checkout.get("line_items"):
        raise HTTPException(status_code=400, detail="Provide either cart_id or line_items")
    if checkout.get("cart_id"):
        # The store takes the items from the cart; line_items is still a required field.
        checkout.setdefault("line_items", [])
    return check_ucp_status(await call_store(merchant, "create_checkout", {"checkout": checkout}), "Checkout creation")


@dev.post("/api/mcp/checkout/get", responses=STORE_ERRORS)
async def get_checkout_mcp(request: GetCheckoutRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Fetch an existing checkout via an MCP client.
    Expects a valid Checkout ID.
    """
    return check_ucp_status(await call_store(merchant, "get_checkout", {"id": request.id}), "Checkout fetch")


@dev.post("/api/mcp/checkout/update", responses=STORE_ERRORS)
async def update_checkout_mcp(request: MCPUpdateCheckoutRequest, merchant: str = DEFAULT_MERCHANT):
    """
    Update an existing checkout via an MCP client.
    Note: The checkout payload is a full replacement of the session state.
    """
    checkout = request.checkout.model_dump(mode="json", exclude_none=True)
    return check_ucp_status(
        await call_store(merchant, "update_checkout", {"id": request.id, "checkout": checkout}), "Checkout update"
    )


# Payment is switched off for now; uncomment to bring back placing the order. Also
# uncomment MCPCompleteCheckoutRequest, and import CheckoutCompleteRequest from the
# SDK module shopping.checkout_complete_request.
# @dev.post("/api/mcp/checkout/complete")
# async def complete_checkout_mcp(request: MCPCompleteCheckoutRequest, merchant: str = DEFAULT_MERCHANT):
#     ...


@dev.post("/api/mcp/checkout/cancel", responses=STORE_ERRORS)
async def cancel_checkout_mcp(request: CancelCheckoutRequest, merchant: str = DEFAULT_MERCHANT):
    """Cancel an existing checkout via an MCP client."""
    response = check_ucp_status(await call_store(merchant, "cancel_checkout", {"id": request.id}), "Checkout cancellation")
    logger.info(f"Checkout cancelled: {request.id}")
    return response


# The development tools, registered above on the dev router (DEV_ENDPOINTS=1 only).
app.include_router(dev)


if __name__ == "__main__":
    import uvicorn

    # localhost only: ngrok and the frontend dev server both reach it locally.
    uvicorn.run("client:app", host="127.0.0.1", port=PORT)
