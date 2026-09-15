import logging
from urllib import response

import httpx
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ucp_sdk.models.schemas.shopping.catalog_lookup import Product
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest, SearchResponse
import json
import uuid
import config

SERVER_URL = "http://localhost:8000"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)

@app.get("/profile")
async def get_agent_profile():
    with config.PROFILE_PATH.open(encoding="utf-8") as f:
        template = f.read()

    profile = json.loads(template)
    return profile


def get_headers() -> dict[str, str]:
    """Generate necessary headers for UCP requests"""
    headers = {"idempotency-key": str(uuid.uuid4()), "request-id": str(uuid.uuid4()),
               "UCP-Agent": f'profile="http://localhost:7000/profile"; version={config.get_version()}'}
    return headers

def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    logger = logging.getLogger(__name__)

    client = httpx.Client(base_url=SERVER_URL)

    # config.get_profile_data()

    try:
        # Discovery
        url = "/.well-known/ucp"
        response = client.get(url)
        if response.status_code != 200:
            logger.error("Discovery failed: %s", response.text)
            return 1

        discovery_data = response.json()
        ucp_data = discovery_data.get("ucp")

        capabilities = []
        for capability in ucp_data.get("capabilities").values():
            capabilities.extend(capability)

        logger.info("Merchant supports %d capabilities", len(capabilities))

        for capability in capabilities:
            logger.info("%s", capability)

        # Product Lookup and search

        headers = get_headers()
        url = "/catalog/search"

        logger.info(headers)

        search_payload = SearchRequest(
            query="gadget"
        )

        json_body = search_payload.model_dump(
            mode="json", by_alias=True,exclude_none=True
        )

        response = client.post(
            url,
            json=json_body,
            headers=headers
        )

        if response.status_code != 200:
            logger.error("Search failed: %s", response.text)
        else:
            logger.info(f"Search succeeded. Response type: {type(response)}\n Response: {response}")
            response_data = response.json()
            logger.info(f"response - {response_data}")
            # logger.info(f"Found {len(products)} products")

            # search_response = SearchResponse(
            #     products=[Product(**p) for p in products]
            # )

            # Use the products
            # for product in search_response.products:
            #     print(f"{product.title}: {product.id}")

        return 0

    except Exception:
        logger.exception("An unexpected error occurred:")
        return 1

    finally:
        client.close()

if __name__ == '__main__':
    sys.exit(main())