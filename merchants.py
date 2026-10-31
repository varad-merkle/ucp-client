"""The stores the chat can shop at, and how to reach each one over UCP.

merchants.json lists each store's base URL and the categories it sells. UCP gives
a store no way to declare what it sells, so the categories are ours to maintain.
On startup each store's /.well-known/ucp gives its MCP endpoint, protocol version
and capabilities.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("ucp-client")

REGISTRY_PATH = Path(__file__).parent / "merchants.json"

SEARCH = "dev.ucp.shopping.catalog.search"
CART = "dev.ucp.shopping.cart"
CHECKOUT = "dev.ucp.shopping.checkout"

# UCP recommends caching a business profile for at least 60 seconds, or its own max-age if longer.
MIN_PROFILE_TTL = 60


@dataclass
class Merchant:
    id: str
    name: str
    url: str
    categories: list[str]
    logo: str | None = None
    version: str | None = None
    endpoint: str | None = None
    capabilities: set[str] = field(default_factory=set)
    error: str | None = None
    profile_cache: dict = field(default_factory=lambda: {"profile": None, "expires": 0.0})

    def supports(self, capability: str) -> bool:
        return not self.error and capability in self.capabilities

    async def profile(self) -> dict:
        cache = self.profile_cache
        if cache["profile"] and time.monotonic() < cache["expires"]:
            return cache["profile"]
        profile, max_age = await fetch_profile(f"{self.url}/.well-known/ucp")
        cache.update(profile=profile, expires=time.monotonic() + max(MIN_PROFILE_TTL, max_age))
        return profile

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "url": self.url, "categories": self.categories,
                "version": self.version, "endpoint": self.endpoint,
                "capabilities": sorted(self.capabilities), "error": self.error}


async def fetch_profile(url: str) -> tuple[dict, int]:
    """A UCP profile and its Cache-Control max-age. UCP: never follow redirects for profiles."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        response = await http.get(url, headers={"Accept": "application/json"})
    if response.is_redirect:
        raise RuntimeError(f"{url} redirects to {response.headers.get('location')}; "
                           "register the final URL in merchants.json")
    response.raise_for_status()
    max_age = re.search(r"max-age=(\d+)", response.headers.get("cache-control", ""))
    return response.json(), int(max_age[1]) if max_age else 0


def mcp_endpoint(profile: dict, version: str) -> str:
    services = profile.get("ucp", {}).get("services", {}).get("dev.ucp.shopping", [])
    for service in services:
        if service.get("transport") == "mcp" and service.get("endpoint") and service.get("version", version) == version:
            return service["endpoint"].rstrip("/")
    raise RuntimeError(f"no MCP endpoint for version {version} (transports: {[s.get('transport') for s in services]})")


class Registry:
    def __init__(self) -> None:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        self.merchants = {
            m["id"]: Merchant(id=m["id"], name=m["name"], url=m["url"].rstrip("/"),
                              categories=[c.strip().lower() for c in m["categories"]],
                              logo=m.get("logo"))
            for m in data["merchants"]
        }
        self.categories = sorted({c for m in self.merchants.values() for c in m.categories})

    def all(self) -> list[Merchant]:
        return list(self.merchants.values())

    def get(self, merchant_id: str | None) -> Merchant | None:
        return self.merchants.get(merchant_id or "")

    def selling(self, categories: list[str]) -> list[Merchant]:
        """Merchants selling any of these categories, most matching categories first."""
        wanted = set(categories)
        matches = [(len(wanted & set(m.categories)), m) for m in self.merchants.values()]
        return [m for count, m in sorted(matches, key=lambda pair: -pair[0]) if count]

    async def discover_all(self, platform_profile: dict) -> None:
        await asyncio.gather(*(self.discover(m, platform_profile) for m in self.merchants.values()))

    async def discover(self, merchant: Merchant, platform_profile: dict) -> None:
        ours = platform_profile["ucp"]["version"]
        try:
            profile = await merchant.profile()
            version = profile["ucp"]["version"]
            if version != ours and ours in (profile["ucp"].get("supported_versions") or {}):
                # UCP: use the business's profile for our version, and check it says so.
                profile, _ = await fetch_profile(profile["ucp"]["supported_versions"][ours])
                if profile["ucp"]["version"] != ours:
                    raise RuntimeError(f"its {ours} profile declares version {profile['ucp']['version']}")
                version = ours
            elif version != ours:
                raise RuntimeError(f"speaks UCP {version}, we speak {ours}")
            merchant.version = version
            merchant.endpoint = mcp_endpoint(profile, version)
            merchant.capabilities = set(profile["ucp"].get("capabilities") or {}) & set(platform_profile["ucp"]["capabilities"])
            merchant.error = None
        except Exception as exc:  # one store being down or incompatible must not stop the others
            merchant.error = str(exc) or type(exc).__name__
        if merchant.error:
            logger.warning(f"Store {merchant.name}: unavailable ({merchant.error})")
        else:
            logger.info(f"Store {merchant.name}: UCP {merchant.version}, {merchant.endpoint}")
