import requests
import logging
import httpx
import uuid
import config
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest, SearchResponse

# try:
#     resp = requests.get("https://moneywise-elective-anthem.ngrok-free.dev/profile")
#     print(f"Local Status: {resp.status_code}")
#     print(f"Local Response:\n{resp.json()}")
#
#     resp2 = requests.post(
#         "https://moneywise-elective-anthem.ngrok-free.dev/api/search",
#         json={"query": "puzzle"},
#         headers={"Content-Type": "application/json"},
#     )
#     print(f"Search response: {resp2.status_code}")
#     print(f"Local Response:\n{resp2.json()}")
# except Exception as e:
#     print(f"Local Error: {e}")

def get_headers() -> dict[str, str]:
    """Generate necessary headers for UCP requests"""
    headers = {"idempotency-key": str(uuid.uuid4()), "request-id": str(uuid.uuid4()),
               "UCP-Agent": f'profile="{public_url}/profile"; version={config.get_version()}'}
    return headers

public_url = "https://moneywise-elective-anthem.ngrok-free.dev"
SERVER_URL = "https://demo.ucphub.ai"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)
logger.info(f"Public Client URL: {public_url}")

client = httpx.Client(base_url=SERVER_URL)

# config.get_profile_data()

try:
    # Discovery
    url = "/.well-known/ucp"
    response = client.get(url)
    if response.status_code != 200:
        logger.error("Discovery failed: %s", response.text)

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


except Exception:
    logger.exception("An unexpected error occurred:")

finally:
    client.close()
