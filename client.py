import logging
import httpx
import sys
from fastapi import HTTPException
from mcp import Client
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from mcp_types import TextContent
from ucp_sdk.models.schemas.shopping.catalog_lookup import LookupRequest, GetProductRequest
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest
from ucp_sdk.models.schemas.shopping.cart_create_request import CartCreateRequest
from ucp_sdk.models.schemas.shopping.cart_update_request import CartUpdateRequest
from ucp_sdk.models.schemas.shopping.checkout_create_request import CheckoutCreateRequest
from ucp_sdk.models.schemas.shopping.checkout_update_request import CheckoutUpdateRequest
from ucp_sdk.models.schemas.shopping.checkout_complete_request import CheckoutCompleteRequest
from ucp_sdk.models.schemas.shopping.types.line_item_create_request import LineItemCreateRequest
import json
import uuid
import config
from pyngrok import ngrok
from dotenv import load_dotenv
from pydantic import BaseModel
from contextlib import asynccontextmanager


load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class QueryMCP(BaseModel):
    query: str

class CartItemRequest(BaseModel):
    id:str
    quantity:int


class CartContextRequest(BaseModel):
    address_country: str | None = None
    address_region: str | None = None
    postal_code: str | None = None

# class CreateCartRequest(BaseModel):
#     items:list[CartItemRequest]
#     context: CartContextRequest | None = None

class GetCartRequest(BaseModel):
     id:str

class MCPUpdateCartRequest(BaseModel):
    id: str
    cart: CartUpdateRequest

class CancelCartRequest(BaseModel):
    id: str

# Checkout request models
class CheckoutCreateMCPRequest(CheckoutCreateRequest):
    line_items: list[LineItemCreateRequest] | None = None
    cart_id: str | None = None

class GetCheckoutRequest(BaseModel):
    id: str

class MCPUpdateCheckoutRequest(BaseModel):
    id: str
    checkout: CheckoutUpdateRequest

class MCPCompleteCheckoutRequest(BaseModel):
    id: str
    checkout: CheckoutCompleteRequest

class CancelCheckoutRequest(BaseModel):
    id: str

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

BUSINESS_BASE_URL = "https://www.pier1.com"
# SERVER_URL = "https://108puzzles.com"
SERVER_URL = "http://localhost:8000"
# ngrok_tunnel = ngrok.connect(addr="7000")
# public_url = ngrok_tunnel.public_url
MCP_CLIENT_URL: str | None = None
public_url: str | None = None
mcp_metadata: dict = {}


async def fetch_business_profile(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/.well-known/ucp"
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MCP_CLIENT_URL, public_url, mcp_metadata

    # 1. start the tunnel
    ngrok_tunnel = ngrok.connect(addr="7000")
    public_url = ngrok_tunnel.public_url
    logger.info(f"Ngrok Tunnel active at: {public_url}")

    # 2. build your own platform metadata
    mcp_metadata = config.get_mcp_metadata(dynamic_url=public_url)

    # 3. fetch the business's profile and find its MCP endpoint
    profile = await fetch_business_profile(BUSINESS_BASE_URL)
    MCP_CLIENT_URL = config.extract_mcp_endpoint(profile)
    logger.info(f"Discovered MCP endpoint: {MCP_CLIENT_URL}")

    yield   # ← server now starts accepting requests

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)

@app.get("/profile.json")
async def get_agent_profile(response: Response):
    response.headers["Cache-Control"] = "public, max-age=3600"
    with config.PROFILE_PATH.open(encoding="utf-8") as f:
        template = f.read()

    profile = json.loads(template)
    response.body = profile
    return response.body

@app.get("/profile")
async def get_agent_profile(request: Request):
    """Return this agent's UCP profile."""
    logger.info(f"Profile fetched by: {request.client.host}")
    logger.info(f"User-Agent: {request.headers.get('user-agent')}")
    with config.PROFILE_PATH.open(encoding="utf-8") as f:
        template = f.read()
    profile = json.loads(template)
    return profile

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
            # port =
            response = await client.post(
                f"{SERVER_URL}/catalog/search",
                json=json_body,
                headers=headers,
                timeout=30.0
            )

            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)

            return response.json()
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach merchant {merchant_id}: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/api/mcp/catalog/search/")
async def search_products_mcp(query: SearchRequest):
    """ Search products via an MCP client """
    search_args = mcp_metadata
    search_args["catalog"] = {
        "query": query.query
    }
    print(search_args)
    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            search_response = await mcp_client.call_tool("search_catalog", search_args)
            for block in search_response.content:
                if isinstance(block, TextContent):
                    print(block)
                    search_response_dict = json.loads(block.text)
                    if search_response_dict.get("products"):
                        return search_response_dict.get("products")
            return None
    except Exception as exc:
        logger.exception("MCP catalog search failed")
        raise HTTPException(status_code=502, detail=f"UCP discovery failed: {exc}") from exc

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
    lookup_args = mcp_metadata
    # Extract unique product IDs for lookup
    unique_product_ids = set(request.ids)
    lookup_args["catalog"] = {
        "ids": unique_product_ids
    }
    async with Client(MCP_CLIENT_URL) as mcp_client:
        lookup_response = await mcp_client.call_tool("lookup_catalog", lookup_args)
        for block in lookup_response.content:
            if isinstance(block, TextContent):
                print(block)
                lookup_response_dict = json.loads(block.text)
                products = lookup_response_dict.get("products")
                messages = lookup_response_dict.get("messages")
                if len(messages) > 0 and len(products) == 0:
                    print("No products found. Ensure entered IDs are valid.")
                    return messages
                elif len(products) > 0 and len(messages) == 0:
                    return products
                else:
                    print("Product search returned partial results. Some IDs are invalid.")
                    for message in messages:
                        print(f"Type: {message.get('type')}, Code: {message.get('code')}, Content: {message.get('message')}")
                    return {
                        "products": products,
                        "messages": messages
                    }
        return None

@app.post("/api/mcp/catalog/product")
async def get_product_detail(request: GetProductRequest):
    pid = request.id
    get_product_args = mcp_metadata
    get_product_args["catalog"] = {
        "id": pid
    }

    async with Client(MCP_CLIENT_URL) as mcp_client:
        get_product_response = await mcp_client.call_tool("get_product", get_product_args)
        for block in get_product_response.content:
            if isinstance(block, TextContent):
                print(block)
                product_detail_response_dict = json.loads(block.text)
                product = product_detail_response_dict.get("product")
                messages = product_detail_response_dict.get("messages")
                if len(messages) > 0 and len(product) == 0:
                    print("No products found. Ensure entered IDs are valid.")
                    return messages
                elif len(product) > 0 and len(messages) == 0:
                    return product_detail_response_dict.get("product")
        return None

@app.post("/api/mcp/cart/create")
async def create_cart_mcp(request: CartCreateRequest):
    """
    Create a new cart via an MCP client.
    Expects a list of Variant IDs and quantities.
    """

    cart_args=dict(mcp_metadata)

    cart_args["cart"]=request.model_dump(mode="json",exclude_none=True)
    print("cart_args", cart_args)

    try:
            async with Client(MCP_CLIENT_URL) as mcp_client:

                cart_response=await mcp_client.call_tool("create_cart",cart_args)

                for block in cart_response.content:
                    if isinstance(block,TextContent):
                        return json.loads(block.text)
                return {"error": "No valid response from MCP server"}

    except Exception as exc:
            logger.exception("MCP cart creation failed")
            raise HTTPException(status_code=502, detail=f"Cart creation failed: {exc}") from exc


@app.post("/api/mcp/cart/get")
async def get_cart_mcp(request:GetCartRequest):
        """
        Fetch an existing cart via an MCP client.
        Expects a valid Cart ID.
        """
        cart_args=dict(mcp_metadata)

        cart_args["id"]=request.id
        logger.info(f"Fetching cart with args: {json.dumps(cart_args, indent=2)}")

        try:
            async with Client(MCP_CLIENT_URL) as mcp_client:

                cart_response = await mcp_client.call_tool("get_cart", cart_args)

            for block in cart_response.content:
                if isinstance(block, TextContent):
                    cart_response_dict = json.loads(block.text)
                    return cart_response_dict

            return {"error": "No valid response from MCP server"}

        except Exception as exc:
            logger.exception("MCP cart fetch failed")
            raise HTTPException(status_code=502, detail=f"Cart fetch failed: {exc}") from exc


@app.post("/api/mcp/cart/update")
async def update_cart_mcp(request:MCPUpdateCartRequest):
        """
    Update an existing cart via an MCP client.
    Note: The line_items array acts as a full replacement of the cart's contents.
    """
        cart_args=dict(mcp_metadata)
        cart_args["id"] = request.id
        cart_args["cart"] = request.cart.model_dump(mode="json", exclude_none=True)
        logger.info(f"Updating cart with validated SDK args: {json.dumps(cart_args, indent=2)}")

        try:
            async with Client(MCP_CLIENT_URL) as mcp_client:
                cart_response = await mcp_client.call_tool("update_cart", cart_args)

                for block in cart_response.content:
                    if isinstance(block, TextContent):
                        return json.loads(block.text)

                return {"error": "No valid response from MCP server"}

        except Exception as exc:
                logger.exception("MCP cart update failed")
                raise HTTPException(status_code=502, detail=f"Cart update failed: {exc}") from exc


@app.post("/api/mcp/cart/cancel")
async def cancel_cart_mcp(request: CancelCartRequest):
    """
    Cancel an existing cart via an MCP client."""
    cart_args = dict(mcp_metadata)
    cart_args["id"] = request.id
    cart_args["meta"] = dict(cart_args.get("meta", {}))
    cart_args["meta"]["idempotency-key"] = str(uuid.uuid4())

    logger.info(f"Cancelling cart with args: {json.dumps(cart_args, indent=2)}")

    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            cart_response = await mcp_client.call_tool("cancel_cart", cart_args)

            for block in cart_response.content:
                if isinstance(block, TextContent):
                    cart_response_dict = json.loads(block.text)
                    if cart_response_dict.get("ucp", {}).get("status") == "error":
                        msgs = cart_response_dict.get("messages", [])
                        detail = "; ".join(m.get("content", "") for m in msgs) or "Cart cancellation failed"
                        raise HTTPException(status_code=422, detail=detail)
                    logger.info(f"Cart cancelled: {request.id}")
                    return cart_response_dict

            return {"error": "No valid response from MCP server"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("MCP cart cancellation failed")
        raise HTTPException(status_code=502, detail=f"Cart cancellation failed: {exc}") from exc


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
        checkout.setdefault("line_items", [])

    checkout_args = dict(mcp_metadata)
    checkout_args["checkout"] = checkout
    logger.info(f"Creating checkout with args: {json.dumps(checkout_args, indent=2)}")

    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            checkout_response = await mcp_client.call_tool("create_checkout", checkout_args)

            for block in checkout_response.content:
                if isinstance(block, TextContent):
                    return json.loads(block.text)

            return {"error": "No valid response from MCP server"}

    except Exception as exc:
        logger.exception("MCP checkout creation failed")
        raise HTTPException(status_code=502, detail=f"Checkout creation failed: {exc}") from exc


@app.post("/api/mcp/checkout/get")
async def get_checkout_mcp(request: GetCheckoutRequest):
    """
    Fetch an existing checkout via an MCP client.
    Expects a valid Checkout ID.
    """
    checkout_args = dict(mcp_metadata)
    checkout_args["id"] = request.id
    logger.info(f"Fetching checkout with args: {json.dumps(checkout_args, indent=2)}")

    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            checkout_response = await mcp_client.call_tool("get_checkout", checkout_args)

        for block in checkout_response.content:
            if isinstance(block, TextContent):
                return json.loads(block.text)

        return {"error": "No valid response from MCP server"}

    except Exception as exc:
        logger.exception("MCP checkout fetch failed")
        raise HTTPException(status_code=502, detail=f"Checkout fetch failed: {exc}") from exc


@app.post("/api/mcp/checkout/update")
async def update_checkout_mcp(request: MCPUpdateCheckoutRequest):
    """
    Update an existing checkout via an MCP client.
    Note: The checkout payload is a full replacement of the session state.
    """
    checkout_args = dict(mcp_metadata)
    checkout_args["id"] = request.id
    checkout_args["checkout"] = request.checkout.model_dump(mode="json", exclude_none=True)
    logger.info(f"Updating checkout with args: {json.dumps(checkout_args, indent=2)}")

    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            checkout_response = await mcp_client.call_tool("update_checkout", checkout_args)

            for block in checkout_response.content:
                if isinstance(block, TextContent):
                    return json.loads(block.text)

            return {"error": "No valid response from MCP server"}

    except Exception as exc:
        logger.exception("MCP checkout update failed")
        raise HTTPException(status_code=502, detail=f"Checkout update failed: {exc}") from exc


@app.post("/api/mcp/checkout/complete")
async def complete_checkout_mcp(request: MCPCompleteCheckoutRequest):
    """
    Complete a checkout (place the order) via an MCP client.
    Expects a valid Checkout ID and payment details.
    """
    checkout_args = dict(mcp_metadata)
    checkout_args["id"] = request.id
    checkout_args["checkout"] = request.checkout.model_dump(mode="json", exclude_none=True)
    checkout_args["meta"] = dict(checkout_args.get("meta", {}))
    checkout_args["meta"]["idempotency-key"] = str(uuid.uuid4())
    logger.info(f"Completing checkout with args: {json.dumps(checkout_args, indent=2)}")

    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            checkout_response = await mcp_client.call_tool("complete_checkout", checkout_args)

            for block in checkout_response.content:
                if isinstance(block, TextContent):
                    return json.loads(block.text)

            return {"error": "No valid response from MCP server"}

    except Exception as exc:
        logger.exception("MCP checkout completion failed")
        raise HTTPException(status_code=502, detail=f"Checkout completion failed: {exc}") from exc


@app.post("/api/mcp/checkout/cancel")
async def cancel_checkout_mcp(request: CancelCheckoutRequest):
    """
    Cancel an existing checkout via an MCP client.
    """
    checkout_args = dict(mcp_metadata)
    checkout_args["id"] = request.id
    checkout_args["meta"] = dict(checkout_args.get("meta", {}))
    checkout_args["meta"]["idempotency-key"] = str(uuid.uuid4())
    logger.info(f"Cancelling checkout with args: {json.dumps(checkout_args, indent=2)}")

    try:
        async with Client(MCP_CLIENT_URL) as mcp_client:
            checkout_response = await mcp_client.call_tool("cancel_checkout", checkout_args)

            for block in checkout_response.content:
                if isinstance(block, TextContent):
                    checkout_response_dict = json.loads(block.text)
                    if checkout_response_dict.get("ucp", {}).get("status") == "error":
                        msgs = checkout_response_dict.get("messages", [])
                        detail = "; ".join(m.get("content", "") for m in msgs) or "Checkout cancellation failed"
                        raise HTTPException(status_code=422, detail=detail)
                    logger.info(f"Checkout cancelled: {request.id}")
                    return checkout_response_dict

            return {"error": "No valid response from MCP server"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("MCP checkout cancellation failed")
        raise HTTPException(status_code=502, detail=f"Checkout cancellation failed: {exc}") from exc


def get_headers() -> dict[str, str]:
    """Generate necessary headers for UCP requests."""
    headers = {
        "idempotency-key": str(uuid.uuid4()),
        "request-id": str(uuid.uuid4()),
        "UCP-Agent": f'profile="{public_url}/profile"; version={config.get_version()}'
    }
    return headers


def main() -> int:
    """Legacy main function - kept for compatibility."""
    logger.info("Use 'uvicorn ucp_client:app' to start the server instead")
    return 0


if __name__ == '__main__':
    sys.exit(main())