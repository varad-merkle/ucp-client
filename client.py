import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from load_dotenv import load_dotenv
from mcp import Client
from mcp_types import TextContent
from pydantic import BaseModel, ConfigDict
from pyngrok import ngrok
from pyngrok.exception import PyngrokNgrokHTTPError
from ucp_sdk.models.schemas.shopping.catalog_lookup import GetProductRequest, LookupRequest
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest
from ucp_sdk.models.schemas.shopping.cart_create_request import Checkout as CartCheckoutCreateRequest
from ucp_sdk.models.schemas.shopping.checkout_complete_request import CheckoutCompleteRequest
from ucp_sdk.models.schemas.shopping.checkout_update_request import CheckoutUpdateRequest

import config
from chat import Chat

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ucp-client")

# Keep the terminal readable: hide the libraries' own request-by-request logs.
for noisy in ("pyngrok", "httpx", "httpx2", "mcp"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

PORT = 7000
STORE_NAME = "Pier 1"
STORE_URL = "https://p1-dev.myshopify.com"
STORE_CURRENCY = "USD"
MCP_CLIENT_URL = f"{STORE_URL}/api/ucp/mcp"

# Merchant configuration (running on different ports)
MERCHANTS_CONFIG = [
    {
        "id": "widgets",
        "name": "Local Tech Store",
        "description": "Premium widgets for everyday tasks",
        "url": "http://localhost:8000",
        "port": 8000
    },
    {
        "id": "gadgets",
        "name": "Gadget Emporium",
        "description": "Cutting-edge gadgets and tech accessories",
        "url": "http://localhost:8001",
        "port": 8001
    },
    {
        "id": "doohickeys",
        "name": "Doohickey Depot",
        "description": "Mysterious and wonderful doohickeys",
        "url": "http://localhost:8002",
        "port": 8002
    }
]

# SERVER_URL = "https://108puzzles.com"
SERVER_URL = "http://localhost:8000"

TOOL_CAPABILITIES = {
    "search_catalog": "dev.ucp.shopping.catalog.search",
    "lookup_catalog": "dev.ucp.shopping.catalog.lookup",
    "get_product": "dev.ucp.shopping.catalog.lookup",
    "create_checkout": "dev.ucp.shopping.checkout",
    "get_checkout": "dev.ucp.shopping.checkout",
    "update_checkout": "dev.ucp.shopping.checkout",
    "complete_checkout": "dev.ucp.shopping.checkout",
    "cancel_checkout": "dev.ucp.shopping.checkout",
}

# The store fetches our profile from this public URL before answering any call.
# It is set when the ngrok tunnel opens on startup.
profile_url = ""

# Recent calls to the store, newest first, for the frontend's protocol log.
exchanges: list[dict] = []
MAX_EXCHANGES = 200


@asynccontextmanager
async def lifespan(app: FastAPI):
    global profile_url
    try:
        tunnel = ngrok.connect(addr=str(PORT))
    except PyngrokNgrokHTTPError as exc:
        # Free ngrok accounts allow one tunnel, so a second client.py can't start.
        raise RuntimeError(
            "Could not open the ngrok tunnel. Is client.py already running in another terminal?"
        ) from exc
    profile_url = f"{tunnel.public_url}/profile.json"
    logger.info(f"Public profile URL: {profile_url}")
    logger.info(f"Store: {STORE_NAME} ({MCP_CLIENT_URL})")
    logger.info(f"Ready on http://localhost:{PORT} - start the frontend and open http://localhost:5173")
    yield
    ngrok.disconnect(tunnel.public_url)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)


def read_profile() -> dict:
    with config.PROFILE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


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
        "ok": error is None,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "request_headers": {"UCP-Agent": f'profile="{profile_url}"'},
        "request_body": arguments,
        "response_body": response,
        "error": error,
    })
    del exchanges[MAX_EXCHANGES:]


def mcp_metadata(require_idempotency_key: bool = False) -> dict:
    meta = {"ucp-agent": {"profile": profile_url}}
    if require_idempotency_key:
        meta["idempotency-key"] = str(uuid.uuid4())
    return {"meta": meta}


async def call_mcp_arguments(tool: str, arguments: dict, payload: dict) -> dict:
    """Call one UCP MCP tool and return its JSON result."""
    logger.info(f"  -> {tool} {json.dumps(payload)}")
    started = time.perf_counter()
    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            result = await mcp_client.call_tool(tool, arguments)
        text = next(block.text for block in result.content if isinstance(block, TextContent))
        response = json.loads(text)
    except Exception as exc:
        message = error_message(exc)
        logger.error(f"  <- {tool} failed: {message}")
        log_exchange(tool, arguments, started, 502, error=message)
        raise HTTPException(status_code=502, detail=f"{tool} failed: {message}")
    log_exchange(tool, arguments, started, 200, response)
    elapsed = round((time.perf_counter() - started) * 1000)
    logger.info(f"  <- {tool} OK in {elapsed} ms: {summarize(response)}")
    return response


async def call_mcp(tool: str, catalog: dict) -> dict:
    """Call a catalog UCP MCP tool and return its JSON result."""
    arguments = {**mcp_metadata(), "catalog": catalog}
    return await call_mcp_arguments(tool, arguments, catalog)


async def call_checkout_mcp(tool: str, payload: dict, require_idempotency_key: bool = False) -> dict:
    """Call a UCP Checkout MCP tool and return its JSON result."""
    arguments = {**mcp_metadata(require_idempotency_key), **payload}
    return await call_mcp_arguments(tool, arguments, payload)


async def fetch_store_profile() -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{STORE_URL}/.well-known/ucp")
        response.raise_for_status()
        return response.json()


chat = Chat(call_mcp, fetch_store_profile, STORE_NAME, STORE_CURRENCY)


@app.get("/profile.json")
async def get_agent_profile_json():
    # The store only accepts a profile served with a Cache-Control header.
    return JSONResponse(read_profile(), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/profile")
async def get_agent_profile(request: Request):
    """Return this agent's UCP profile."""
    logger.info(f"Profile fetched by: {request.client.host}")
    logger.info(f"User-Agent: {request.headers.get('user-agent')}")
    return read_profile()


# API Endpoints for Frontend
@app.get("/api/merchants")
async def list_merchants():
    """Discover available merchants."""
    return {
        "merchants": [
            {
                "id": m["id"],
                "name": m["name"],
                "description": m["description"]
            } for m in MERCHANTS_CONFIG
        ],
        "total": len(MERCHANTS_CONFIG)
    }


@app.post("/api/search")
async def search_products(search_req: SearchRequest, merchant_id: str | None = None):
    """Search products on a specific merchant."""
    # Find merchant
    merchant = next((m for m in MERCHANTS_CONFIG if m["id"] == merchant_id), None)
    if not merchant:
        raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")

    headers = get_headers()

    try:
        async with httpx.AsyncClient() as client:
            json_body = search_req.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            response = await client.post(
                f"{SERVER_URL}/catalog/search",
                json=json_body,
                headers=headers,
                timeout=30.0
            )
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach merchant {merchant_id}: {str(e)}")

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/api/mcp/catalog/search/")
async def search_products_mcp(query: SearchRequest):
    """ Search products via an MCP client """
    response = await call_mcp("search_catalog", {"query": query.query})
    return response.get("products")


@app.post("/api/mcp/catalog/lookup")
async def lookup_products_mcp(request: LookupRequest):
    """
    Lookup one or more products via an MCP client.
    Requires a list of one or more valid product IDs.
    The response from the MCP server can be one of three possible options:
        1. Success: A list of one or more products.
        2. Partial success: Not all specified products were found. Mesages data indicates which products could not be found and why.
        3. Error: Not a single product specified by the product IDs could be found.
    """
    # Extract unique product IDs for lookup
    unique_product_ids = sorted(set(request.ids))
    response = await call_mcp("lookup_catalog", {"ids": unique_product_ids})
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
    response = await call_mcp("get_product", {"id": request.id})
    if response.get("product"):
        return response["product"]
    logger.info("No products found. Ensure entered IDs are valid.")
    return response.get("messages") or []


class CheckoutFromCartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cart_id: str


@app.post("/api/mcp/checkout/create")
async def create_checkout(request: CartCheckoutCreateRequest | CheckoutFromCartRequest):
    if isinstance(request, CheckoutFromCartRequest):
        checkout = CartCheckoutCreateRequest.model_validate({
            "cart_id": request.cart_id,
            "line_items": [],
        })
    else:
        checkout = request
    payload = checkout.model_dump(mode="json", by_alias=True, exclude_none=True)
    return await call_checkout_mcp("create_checkout", {"checkout": payload})


@app.post("/api/mcp/checkout/get")
async def get_checkout(id: str = Body(..., embed=True)):
    return await call_checkout_mcp("get_checkout", {"id": id})


@app.post("/api/mcp/checkout/update")
async def update_checkout(id: str = Body(...), checkout: CheckoutUpdateRequest = Body(...)):
    checkout_payload = checkout.model_dump(mode="json", by_alias=True, exclude_none=True)
    return await call_checkout_mcp(
        "update_checkout", {"id": id, "checkout": checkout_payload}
    )


@app.post("/api/mcp/checkout/complete")
async def complete_checkout(id: str = Body(...), checkout: CheckoutCompleteRequest = Body(...)):
    checkout_payload = checkout.model_dump(mode="json", by_alias=True, exclude_none=True)
    return await call_checkout_mcp(
        "complete_checkout",
        {"id": id, "checkout": checkout_payload},
        require_idempotency_key=True,
    )


@app.post("/api/mcp/checkout/cancel")
async def cancel_checkout(id: str = Body(..., embed=True)):
    return await call_checkout_mcp(
        "cancel_checkout", {"id": id}, require_idempotency_key=True
    )


# Endpoints for the chat frontend (ucp-frontend)
class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
async def chat_turn(body: ChatRequest):
    """One chat turn: the reply is a list of blocks the frontend renders."""
    logger.info(f"Chat: {body.message!r}")
    reply = await chat.reply(body.message, body.session_id)
    logger.info(f"Reply: {', '.join(block['type'] for block in reply['blocks'])}")
    return reply


@app.get("/api/health")
async def health():
    """Status of this client and of the store it talks to."""
    store = {"reachable": False, "status": 0, "latency_ms": 0, "name": STORE_NAME}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{STORE_URL}/.well-known/ucp")
        store["status"] = response.status_code
        store["reachable"] = response.status_code == 200
    except httpx.HTTPError:
        pass
    store["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return {
        "agent": {
            "status": "ok",
            "version": read_profile()["ucp"]["version"],
            "profile_url": profile_url,
            "merchant_url": STORE_URL,
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


def get_headers() -> dict[str, str]:
    """Generate necessary headers for UCP requests."""
    headers = {
        "idempotency-key": str(uuid.uuid4()),
        "request-id": str(uuid.uuid4()),
        "UCP-Agent": f'profile="{profile_url}"; version={config.get_version()}'
    }
    return headers


if __name__ == '__main__':
    import uvicorn

    uvicorn.run("client:app", host="0.0.0.0", port=PORT)
