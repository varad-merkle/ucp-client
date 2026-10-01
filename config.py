import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import HTTPException
load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("config")

PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:7000")

PROFILE_PATH = Path(__file__).parent / "profile.json"

def get_profile_data():
    with PROFILE_PATH.open(encoding="utf-8") as f:
        template = f.read()
    profile = json.loads(template)
    logger.info(f"Profile data: {profile}, type: {type(profile)}")
    version = profile.get("ucp").get("version")
    logger.info(f"version: {version}")
    capabilities = profile.get("ucp").get("capabilities")
    logger.info(f"Capabilities: {capabilities}; keys: {[capabilities.keys()]}; vals: {capabilities.values()}")
    return profile

def get_version():
    profile = get_profile_data()
    return profile["ucp"]["version"]

def get_mcp_metadata(dynamic_url:str =None) -> dict:

    base_url=dynamic_url or PUBLIC_URL
    return {
        "meta": {
            "ucp-agent": {
                "profile": f"{base_url}/profile.json"
            }
        }
    }


def extract_mcp_endpoint(business_profile:dict)->str:
     services=business_profile.get("ucp",{}).get("services",{}).get("dev.ucp.shopping", [])
     for service in services:
        if service.get("transport") == "mcp" and service.get("endpoint"):
            return service["endpoint"].rstrip("/")

     available = [s.get("transport") for s in services]
     raise HTTPException(
        status_code=400,
        detail=f"Merchant does not expose an MCP transport endpoint (available: {available})",
    ) 

