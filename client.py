import logging
import httpx
import sys

from mcp import Client
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from mcp_types import TextContent
from ucp_sdk.models.schemas.shopping.catalog_lookup import LookupRequest, GetProductRequest
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest
import json
import uuid
import config
from pyngrok import ngrok
from load_dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

MCP_CLIENT_URL = "https://p1-dev.myshopify.com/api/ucp/mcp"

class QueryMCP(BaseModel):
    query: str

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
public_url = ngrok.connect(addr="7000")
mcp_metadata = config.get_mcp_metadata()

app = FastAPI()

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
    async with Client(MCP_CLIENT_URL) as mcp_client:
        search_response = await mcp_client.call_tool("search_catalog", search_args)
        for block in search_response.content:
            if isinstance(block, TextContent):
                print(block)
                search_response_dict = json.loads(block.text)
                if search_response_dict.get("products"):
                    return search_response_dict.get("products")
        return None

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


def get_headers() -> dict[str, str]:
    """Generate necessary headers for UCP requests."""
    headers = {
        "idempotency-key": str(uuid.uuid4()),
        "request-id": str(uuid.uuid4()),
        "UCP-Agent": f'profile="https://moneywise-elective-anthem.ngrok-free.dev/profile"; version={config.get_version()}'
    }
    return headers


def main() -> int:
    """Legacy main function - kept for compatibility."""
    logger.info("Use 'uvicorn ucp_client:app' to start the server instead")
    return 0


if __name__ == '__main__':
    sys.exit(main())
