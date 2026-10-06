"""UCP client (platform) for the Pier 1 store, and the backend of ucp-frontend.

On startup it opens an ngrok tunnel so the store can fetch our UCP profile, reads the
store's /.well-known/ucp and finds its MCP endpoint. Every store call then goes through
call_mcp(), which adds meta.ucp-agent (and an idempotency key where UCP requires one),
logs the call and records it for the frontend's protocol log.

    GET  /profile.json                our UCP platform profile
    POST /api/chat                    one chat turn (agent.py + shop.py)
    POST /api/checkout/details        the checkout card's delivery details form
    GET  /api/health                  status of this client and the store
    GET  /api/exchanges               recent store calls (protocol log)
    DELETE /api/exchanges
    POST /api/mcp/catalog/...         direct catalog, cart and checkout calls
    POST /api/mcp/cart/...
    POST /api/mcp/checkout/...

Run:  uv run python client.py
"""

import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp import Client
from mcp_types import TextContent
from pydantic import BaseModel
from pyngrok import ngrok
from pyngrok.exception import PyngrokNgrokHTTPError
from ucp_sdk.models.schemas.shopping.cart_create_request import CartCreateRequest
from ucp_sdk.models.schemas.shopping.cart_update_request import CartUpdateRequest
from ucp_sdk.models.schemas.shopping.catalog_lookup import GetProductRequest, LookupRequest
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest
# from ucp_sdk.models.schemas.shopping.checkout_complete_request import CheckoutCompleteRequest  # payment, switched off
from ucp_sdk.models.schemas.shopping.checkout_create_request import CheckoutCreateRequest
from ucp_sdk.models.schemas.shopping.checkout_update_request import CheckoutUpdateRequest
from ucp_sdk.models.schemas.shopping.types.line_item_create_request import LineItemCreateRequest

from agent import Agent
from shop import Shop

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ucp-client")

# Keep the terminal readable: hide the libraries' own request-by-request logs.
for noisy in ("pyngrok", "httpx", "httpx2", "mcp"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

PORT = 7000
PROFILE_PATH = Path(__file__).parent / "profile.json"
STORE_NAME = "Pier 1"
STORE_CURRENCY = "USD"
STORE_COUNTRY = "US"  # prices carts in USD, like the catalog
# The business we shop from. Its MCP endpoint is read from its /.well-known/ucp on startup.
BUSINESS_BASE_URL = "https://www.pier1.com"

TOOL_CAPABILITIES = {
    "search_catalog": "dev.ucp.shopping.catalog.search",
    "lookup_catalog": "dev.ucp.shopping.catalog.lookup",
    "get_product": "dev.ucp.shopping.catalog.lookup",
    "create_cart": "dev.ucp.shopping.cart",
    "get_cart": "dev.ucp.shopping.cart",
    "update_cart": "dev.ucp.shopping.cart",
    "cancel_cart": "dev.ucp.shopping.cart",
    "create_checkout": "dev.ucp.shopping.checkout",
    "get_checkout": "dev.ucp.shopping.checkout",
    "update_checkout": "dev.ucp.shopping.checkout",
    "complete_checkout": "dev.ucp.shopping.checkout",
    "cancel_checkout": "dev.ucp.shopping.checkout",
}

# The UCP spec requires meta.idempotency-key for these tools.
IDEMPOTENT_TOOLS = {"cancel_cart", "complete_checkout", "cancel_checkout"}

# Set on startup: our public URL (ngrok) and the business's MCP endpoint.
public_url: str | None = None
MCP_CLIENT_URL: str | None = None

# Recent calls to the store, newest first, for the frontend's protocol log.
exchanges: list[dict] = []
MAX_EXCHANGES = 200

# The business profile, cached as UCP recommends (at least 60 seconds, or the
# profile's own Cache-Control max-age if longer).
MIN_PROFILE_TTL = 60
profile_cache: dict = {"profile": None, "expires": 0.0}


# ----------------------------------------------------------- discovery


async def fetch_business_profile() -> dict:
    if profile_cache["profile"] and time.monotonic() < profile_cache["expires"]:
        return profile_cache["profile"]

    # UCP: platforms must not follow redirects when fetching a business profile.
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        resp = await http.get(f"{BUSINESS_BASE_URL}/.well-known/ucp", headers={"Accept": "application/json"})
        resp.raise_for_status()
        profile = resp.json()

    max_age = re.search(r"max-age=(\d+)", resp.headers.get("cache-control", ""))
    ttl = max(MIN_PROFILE_TTL, int(max_age[1]) if max_age else 0)
    profile_cache.update(profile=profile, expires=time.monotonic() + ttl)
    return profile


def extract_mcp_endpoint(business_profile: dict) -> str:
    """Find the MCP endpoint in a business's /.well-known/ucp profile."""
    services = business_profile.get("ucp", {}).get("services", {}).get("dev.ucp.shopping", [])
    for service in services:
        if service.get("transport") == "mcp" and service.get("endpoint"):
            return service["endpoint"].rstrip("/")
    available = [s.get("transport") for s in services]
    raise RuntimeError(f"Merchant does not expose an MCP transport endpoint (available: {available})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global public_url, MCP_CLIENT_URL

    # 1. Start the tunnel, so the store can fetch our profile.
    try:
        ngrok_tunnel = ngrok.connect(addr=str(PORT))
    except PyngrokNgrokHTTPError as exc:
        # Free ngrok accounts allow one tunnel, so a second client.py can't start.
        raise RuntimeError(
            "Could not open the ngrok tunnel. Is client.py already running in another terminal?"
        ) from exc
    public_url = ngrok_tunnel.public_url
    logger.info(f"Public profile URL: {public_url}/profile.json")

    # 2. Fetch the business's profile and find its MCP endpoint.
    MCP_CLIENT_URL = extract_mcp_endpoint(await fetch_business_profile())
    logger.info(f"Store: {STORE_NAME}, discovered MCP endpoint: {MCP_CLIENT_URL}")
    logger.info(f"Ready on http://localhost:{PORT} - start the frontend and open http://localhost:5173")

    yield
    ngrok.disconnect(ngrok_tunnel.public_url)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)


def read_profile() -> dict:
    with PROFILE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------- store calls


def error_message(exc: BaseException) -> str:
    # The MCP client wraps errors in exception groups; show the real one.
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return str(exc) or type(exc).__name__


def summarize(response: dict) -> str:
    """A one-line description of a store response, for the terminal."""
    parts = []
    if "products" in response:
        parts.append(f"{len(response['products'] or [])} products")
    if response.get("product"):
        parts.append(f"product '{response['product'].get('title')}'")
    if "line_items" in response:
        # Checkouts carry a status (incomplete, ready_for_complete, ...); carts don't.
        kind = f"checkout ({response['status']})" if response.get("status") else "cart"
        parts.append(f"{kind} {response.get('id')} with {len(response['line_items'] or [])} line items")
    if (response.get("pagination") or {}).get("has_next_page"):
        parts.append("more pages available")
    for message in response.get("messages") or []:
        parts.append(f"{message.get('type')}: {message.get('content')}")
    return ", ".join(parts) or "empty response"


def log_exchange(tool: str, arguments: dict, started: float, status: int, response=None, error=None):
    exchanges.insert(0, {
        "id": str(uuid.uuid4()),
        "capability": TOOL_CAPABILITIES.get(tool, "discovery"),
        "method": tool,
        "url": MCP_CLIENT_URL,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ok": error is None and (response or {}).get("ucp", {}).get("status") != "error",
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "request_headers": {"UCP-Agent": f'profile="{public_url}/profile.json"'},
        "request_body": arguments,
        "response_body": response,
        "error": error,
    })
    del exchanges[MAX_EXCHANGES:]


async def call_mcp(tool: str, arguments: dict) -> dict:
    """
    Call one of the store's UCP MCP tools and return its JSON result.
    `arguments` are the tool's own parameters; the `meta` block is added here,
    with an idempotency key for the tools whose spec requires one.
    """
    meta: dict = {"ucp-agent": {"profile": f"{public_url}/profile.json"}}
    if tool in IDEMPOTENT_TOOLS:
        meta["idempotency-key"] = str(uuid.uuid4())
    arguments = {"meta": meta, **arguments}

    logger.info(f"  -> {tool} {json.dumps({k: v for k, v in arguments.items() if k != 'meta'})}")
    started = time.perf_counter()
    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            result = await mcp_client.call_tool(tool, arguments)
        text = next((block.text for block in result.content if isinstance(block, TextContent)), "")
        # UCP returns its response in structuredContent; the text block is the same data as JSON.
        try:
            response = result.structured_content or json.loads(text)
        except json.JSONDecodeError:
            response = None
        # Business outcomes (e.g. cart not found) still come back as a UCP response,
        # even when the tool call is flagged as an error. Anything else is a real failure.
        if not isinstance(response, dict) or (result.is_error and "ucp" not in response):
            raise RuntimeError(text or "the store returned a tool error")
    except Exception as exc:
        message = error_message(exc)
        logger.error(f"  <- {tool} failed: {message}")
        log_exchange(tool, arguments, started, 502, error=message)
        raise HTTPException(status_code=502, detail=f"{tool} failed: {message}")
    log_exchange(tool, arguments, started, 200, response)
    elapsed = round((time.perf_counter() - started) * 1000)
    outcome = "returned an error" if response.get("ucp", {}).get("status") == "error" else "OK"
    logger.info(f"  <- {tool} {outcome} in {elapsed} ms: {summarize(response)}")
    return response


async def call_catalog(tool: str, catalog: dict) -> dict:
    """Catalog tools take their parameters under `catalog`."""
    return await call_mcp(tool, {"catalog": catalog})


def check_ucp_status(response: dict, action: str) -> dict:
    """
    UCP reports business outcomes (e.g. cart not found) as a normal result with
    ucp.status "error" and the reason in `messages`. Turn those into HTTP errors.
    """
    if response.get("ucp", {}).get("status") != "error":
        return response
    msgs = response.get("messages") or []
    detail = "; ".join(m.get("content", "") for m in msgs) or f"{action} failed"
    status = 404 if any(str(m.get("code", "")).endswith("not_found") for m in msgs) else 422
    raise HTTPException(status_code=status, detail=detail)


shop = Shop(call_catalog, call_mcp, fetch_business_profile, STORE_NAME, STORE_CURRENCY, STORE_COUNTRY)
agent = Agent(shop)


# ------------------------------------------------------------ endpoints


@app.get("/profile.json")
async def get_agent_profile(request: Request):
    """
    Return this agent's UCP profile. UCP requires Cache-Control with `public` and a
    max-age of at least 60 seconds, and recommends a validator such as ETag.
    """
    logger.info(f"Profile fetched by: {request.client.host} ({request.headers.get('user-agent')})")
    profile = read_profile()
    etag = '"' + hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:32] + '"'
    headers = {"Cache-Control": "public, max-age=3600", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(profile, headers=headers)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
async def chat_turn(body: ChatRequest):
    """One chat turn: the reply is a list of blocks the frontend renders."""
    logger.info(f"Chat: {body.message!r}")
    reply = await agent.reply(body.message, body.session_id)
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
async def checkout_details(body: CheckoutDetailsRequest):
    """The checkout card's delivery details form: saved on the checkout in one update."""
    logger.info("Chat: delivery details form submitted")
    details = {k: (v.strip() or None) if isinstance(v, str) else v for k, v in body.details.model_dump().items()}
    return await agent.submit_details(body.session_id, details)


@app.get("/api/health")
async def health():
    """Status of this client and of the store it talks to."""
    store = {"reachable": False, "status": 0, "latency_ms": 0, "name": STORE_NAME}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(f"{BUSINESS_BASE_URL}/.well-known/ucp")
        store["status"] = response.status_code
        store["reachable"] = response.status_code == 200
    except httpx.HTTPError:
        pass
    store["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return {
        "agent": {
            "status": "ok",
            "version": read_profile()["ucp"]["version"],
            "profile_url": f"{public_url}/profile.json",
            "merchant_url": BUSINESS_BASE_URL,
        },
        "merchant": store,
    }


@app.get("/api/exchanges")
async def list_exchanges(limit: int = 40):
    return {"exchanges": exchanges[:max(1, min(limit, MAX_EXCHANGES))], "total": len(exchanges)}


@app.delete("/api/exchanges")
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
    # UCP lets a checkout start from an existing cart (cart_id) instead of line items.
    line_items: list[LineItemCreateRequest] | None = None
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


@app.post("/api/mcp/catalog/search/")
async def search_products_mcp(query: SearchRequest):
    """Search products via an MCP client."""
    response = await call_catalog("search_catalog", {"query": query.query})
    return response.get("products")


@app.post("/api/mcp/catalog/lookup")
async def lookup_products_mcp(request: LookupRequest):
    """
    Lookup one or more products via an MCP client.
    Requires a list of one or more valid product IDs.
    The response from the MCP server can be one of three possible options:
        1. Success: A list of one or more products.
        2. Partial success: Not all specified products were found. Messages data indicates which products could not be found and why.
        3. Error: Not a single product specified by the product IDs could be found.
    """
    response = await call_catalog("lookup_catalog", {"ids": sorted(set(request.ids))})
    products = response.get("products") or []
    messages = response.get("messages") or []
    if not products:
        logger.info("No products found. Ensure entered IDs are valid.")
        return messages
    if not messages:
        return products
    logger.info("Product search returned partial results. Some IDs are invalid.")
    for message in messages:
        logger.info(f"Type: {message.get('type')}, Code: {message.get('code')}, Content: {message.get('content')}")
    return {"products": products, "messages": messages}


@app.post("/api/mcp/catalog/product")
async def get_product_detail(request: GetProductRequest):
    response = await call_catalog("get_product", {"id": request.id})
    if response.get("product"):
        return response["product"]
    logger.info("No products found. Ensure entered IDs are valid.")
    return response.get("messages") or []


@app.post("/api/mcp/cart/create")
async def create_cart_mcp(request: CartCreateRequest):
    """
    Create a new cart via an MCP client.
    Expects a list of Variant IDs and quantities.
    """
    cart = request.model_dump(mode="json", exclude_none=True)
    return check_ucp_status(await call_mcp("create_cart", {"cart": cart}), "Cart creation")


@app.post("/api/mcp/cart/get")
async def get_cart_mcp(request: GetCartRequest):
    """
    Fetch an existing cart via an MCP client.
    Expects a valid Cart ID.
    """
    return check_ucp_status(await call_mcp("get_cart", {"id": request.id}), "Cart fetch")


@app.post("/api/mcp/cart/update")
async def update_cart_mcp(request: MCPUpdateCartRequest):
    """
    Update an existing cart via an MCP client.
    UCP: the platform must send the entire cart; line_items fully replaces the cart's contents.
    """
    cart = request.cart.model_dump(mode="json", exclude_none=True)
    return check_ucp_status(await call_mcp("update_cart", {"id": request.id, "cart": cart}), "Cart update")


@app.post("/api/mcp/cart/cancel")
async def cancel_cart_mcp(request: CancelCartRequest):
    """Cancel an existing cart via an MCP client."""
    response = check_ucp_status(await call_mcp("cancel_cart", {"id": request.id}), "Cart cancellation")
    logger.info(f"Cart cancelled: {request.id}")
    return response


@app.post("/api/mcp/checkout/create")
async def create_checkout_mcp(request: CheckoutCreateMCPRequest):
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
    return check_ucp_status(await call_mcp("create_checkout", {"checkout": checkout}), "Checkout creation")


@app.post("/api/mcp/checkout/get")
async def get_checkout_mcp(request: GetCheckoutRequest):
    """
    Fetch an existing checkout via an MCP client.
    Expects a valid Checkout ID.
    """
    return check_ucp_status(await call_mcp("get_checkout", {"id": request.id}), "Checkout fetch")


@app.post("/api/mcp/checkout/update")
async def update_checkout_mcp(request: MCPUpdateCheckoutRequest):
    """
    Update an existing checkout via an MCP client.
    Note: The checkout payload is a full replacement of the session state.
    """
    checkout = request.checkout.model_dump(mode="json", exclude_none=True)
    return check_ucp_status(
        await call_mcp("update_checkout", {"id": request.id, "checkout": checkout}), "Checkout update"
    )


# Payment is switched off for now; uncomment to bring back placing the order
# (also uncomment MCPCompleteCheckoutRequest and the CheckoutCompleteRequest import).
# @app.post("/api/mcp/checkout/complete")
# async def complete_checkout_mcp(request: MCPCompleteCheckoutRequest):
#     """
#     Complete a checkout (place the order) via an MCP client.
#     Expects a valid Checkout ID and payment details.
#     The UCP spec requires an idempotency key for complete_checkout.
#     """
#     checkout = request.checkout.model_dump(mode="json", exclude_none=True)
#     return check_ucp_status(
#         await call_mcp("complete_checkout", {"id": request.id, "checkout": checkout}),
#         "Checkout completion",
#     )


@app.post("/api/mcp/checkout/cancel")
async def cancel_checkout_mcp(request: CancelCheckoutRequest):
    """Cancel an existing checkout via an MCP client."""
    response = check_ucp_status(await call_mcp("cancel_checkout", {"id": request.id}), "Checkout cancellation")
    logger.info(f"Checkout cancelled: {request.id}")
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("client:app", host="0.0.0.0", port=PORT)
