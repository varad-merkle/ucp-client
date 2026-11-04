"""Identity linking (identity.py) against fake Shop accounts and store authorization servers.

The fakes follow the shapes the real servers publish (checked against accounts.shop.app
and the stores' shopify.com/authentication/<id> issuers), so the whole flow runs here:
discovery, the browser link with PKCE/state/iss, token exchange, the JWT bearer grant,
refresh, re-chaining and revocation.
"""

import asyncio
import base64
import json
import math
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import db
import identity
from conftest import PASSWORD, USER
from merchants import Merchant

AGENT = "https://agent.example"
STORE_URL = "https://store.example"
STORE_ISSUER = "https://auth.example/authentication/1"
ORDER_SCOPE = "dev.ucp.shopping.order:read"
STORE_DOMAIN = "teststore.myshopify.com"
# What Shopify documents for buyer-linked tokens, plus the scope this store gates.
STORE_SCOPES = {"openid", "dev.ucp.shopping.checkout:manage", ORDER_SCOPE}

SHOP_METADATA = {
    "issuer": identity.SHOP_ISSUER,
    "authorization_endpoint": "https://accounts.shop.app/oauth/authorize",
    "token_endpoint": "https://accounts.shop.app/oauth/token",
    "userinfo_endpoint": "https://accounts.shop.app/oauth/userinfo",
    "revocation_endpoint": "https://accounts.shop.app/oauth/revoke",
    "authorization_response_iss_parameter_supported": True,
    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
    "grant_types_supported": ["authorization_code", "refresh_token", identity.TOKEN_EXCHANGE],
}
STORE_METADATA = {
    "issuer": STORE_ISSUER,
    "authorization_endpoint": f"{STORE_ISSUER}/oauth/authorize",
    "token_endpoint": f"{STORE_ISSUER}/oauth/token",
    "revocation_endpoint": f"{STORE_ISSUER}/oauth/revoke",
    "authorization_response_iss_parameter_supported": True,
    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
    "grant_types_supported": ["authorization_code", "refresh_token", identity.JWT_BEARER],
}


class FakeServers:
    """Shop accounts + one store's authorization server. Records every request."""

    def __init__(self):
        self.requests: list[tuple[str, dict, httpx.Request]] = []
        self.counter = 0
        self.overrides: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url in self.overrides:
            return self.overrides[url]
        if request.method == "GET":
            return self.get(url)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((url, form, request))
        if url.endswith("/revoke"):
            return httpx.Response(200)
        return self.token(url, form)

    def get(self, url: str) -> httpx.Response:
        routes = {
            f"{STORE_URL}/.well-known/oauth-protected-resource":
                {"resource": STORE_URL, "authorization_servers": [STORE_ISSUER]},
            "https://auth.example/.well-known/oauth-authorization-server/authentication/1": STORE_METADATA,
            "https://accounts.shop.app/.well-known/oauth-authorization-server": SHOP_METADATA,
            "https://accounts.shop.app/oauth/userinfo": {"email": "buyer@example.com"},
        }
        return httpx.Response(200, json=routes[url]) if url in routes else httpx.Response(404)

    def token(self, url: str, form: dict) -> httpx.Response:
        self.counter += 1
        n = self.counter
        grant = form["grant_type"]
        if grant == identity.TOKEN_EXCHANGE:
            return httpx.Response(200, json={"access_token": f"jwt-grant-{n}",
                                             "issued_token_type": identity.JWT_TOKEN_TYPE,
                                             "token_type": "N_A", "expires_in": 60})
        if grant == identity.JWT_BEARER:   # stores issue no refresh token for chained links
            return httpx.Response(200, json={"access_token": f"store-chained-{n}", "token_type": "Bearer",
                                             "expires_in": 3600, "scope": form.get("scope")})
        prefix = "shop" if "shop.app" in url else "store"
        return httpx.Response(200, json={"access_token": f"{prefix}-access-{n}", "refresh_token": f"{prefix}-refresh-{n}",
                                         "token_type": "Bearer", "expires_in": 3600})

    def of(self, grant: str) -> list[tuple[str, dict, httpx.Request]]:
        return [r for r in self.requests if r[1].get("grant_type") == grant]


class FakeRegistry:
    def __init__(self, *merchants):
        self.merchants = {m.id: m for m in merchants}

    def get(self, merchant_id):
        return self.merchants.get(merchant_id or "")

    def all(self):
        return list(self.merchants.values())


def make_store() -> Merchant:
    merchant = Merchant(id="teststore", name="Test Store", url=STORE_URL, categories=[])
    merchant.endpoint = f"https://{STORE_DOMAIN}/api/ucp/mcp"
    merchant.capabilities = {identity.CAPABILITY, "dev.ucp.shopping.order", "dev.ucp.shopping.cart"}
    merchant.profile_cache = {"expires": math.inf, "profile": {"ucp": {"capabilities": {identity.CAPABILITY: [{
        "version": "2026-08-25",
        "config": {
            "providers": {"app.shop.accounts": [{"type": "oauth2", "auth_url": "https://accounts.shop.app"}]},
            # order:read is negotiated; the loyalty scope is for a capability we don't speak.
            "scopes": {ORDER_SCOPE: {}, "com.example.loyalty:points": {}},
        },
    }]}}}}
    return merchant


@pytest.fixture
def servers(monkeypatch):
    fake = FakeServers()
    monkeypatch.setattr(identity, "transport", httpx.MockTransport(fake))
    monkeypatch.setenv("IDENTITY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "store-client")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "store-secret")
    for name in ("SHOP_ACCOUNTS_CLIENT_ID", "SHOP_ACCOUNTS_CLIENT_SECRET", "SHOPIFY_STORE_SCOPES",
                 "IDENTITY_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    identity.set_public_url(AGENT)
    for cache in (identity._pending, identity._metadata_cache, identity._chain_failures, identity._needed_scopes):
        cache.clear()
    return fake


@pytest.fixture
def store():
    return make_store()


@pytest.fixture
def registry(store):
    return FakeRegistry(store)


def run(coro):
    return asyncio.run(coro)


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def link_shop(registry) -> dict:
    """Run the Shop accounts browser link to completion; returns the authorize params."""
    params = query(run(identity.start_link(USER, identity.SHOP, registry)))
    run(identity.finish_link(params["state"], "shop-code", identity.SHOP_ISSUER, None, registry))
    return params


def client_auth(form: dict) -> tuple[str, str]:
    """The client credentials sent in the form body (client_secret_post, as Shopify documents)."""
    return form.get("client_id"), form.get("client_secret")


# ------------------------------------------------------------- building blocks


def test_pkce_matches_the_rfc7636_example():
    assert identity.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    verifier, challenge = identity.pkce_pair()
    assert 43 <= len(verifier) <= 128 and identity.pkce_challenge(verifier) == challenge


def test_well_known_goes_between_host_and_path():
    assert identity._well_known(STORE_ISSUER, "oauth-authorization-server") == \
        "https://auth.example/.well-known/oauth-authorization-server/authentication/1"
    assert identity._well_known("https://accounts.shop.app", "oauth-authorization-server") == \
        "https://accounts.shop.app/.well-known/oauth-authorization-server"


def test_audience_can_follow_the_generic_spec(servers, registry, monkeypatch):
    monkeypatch.setenv("IDENTITY_AUDIENCE", "issuer")
    link_shop(registry)
    assert servers.of(identity.TOKEN_EXCHANGE)[0][1]["audience"] == STORE_ISSUER


def test_jwt_expiry_is_read_when_expires_in_is_missing():
    claims = base64.urlsafe_b64encode(json.dumps({"exp": 2_000_000_000}).encode()).decode().rstrip("=")
    assert identity._jwt_expiry(f"eyJhbGciOiJSUzI1NiJ9.{claims}.sig").timestamp() == 2_000_000_000
    assert identity._jwt_expiry("opaque-token") is None


def test_scope_derivation_keeps_only_negotiated_capabilities(store):
    config = run(identity._linking_config(store))
    assert identity.derived_scopes(config, store.capabilities) == ORDER_SCOPE


def test_off_without_a_key_or_credentials(monkeypatch, servers, registry):
    assert identity.enabled(registry.all())
    monkeypatch.delenv("IDENTITY_TOKEN_KEY")
    assert not identity.enabled(registry.all())
    monkeypatch.setenv("IDENTITY_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("SHOPIFY_CLIENT_SECRET")
    assert not identity.enabled(registry.all())


def test_per_store_credentials_win(monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "shared")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shared-secret")
    monkeypatch.setenv("SHOPIFY_CLIENT_ID_ALLBIRDS", "allbirds-id")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET_ALLBIRDS", "allbirds-secret")
    assert identity.store_credentials("allbirds") == ("allbirds-id", "allbirds-secret")
    assert identity.store_credentials("pier1") == ("shared", "shared-secret")


# --------------------------------------------------------------- discovery


def test_issuer_must_match_exactly(servers):
    servers.overrides["https://accounts.shop.app/.well-known/oauth-authorization-server"] = \
        httpx.Response(200, json={**SHOP_METADATA, "issuer": "https://accounts.shop.app/"})
    with pytest.raises(identity.IdentityError, match="issuer mismatch"):
        run(identity.authorization_server(identity.SHOP_ISSUER))


def test_only_404_falls_back_to_oidc(servers):
    rfc8414 = "https://accounts.shop.app/.well-known/oauth-authorization-server"
    servers.overrides[rfc8414] = httpx.Response(500)
    with pytest.raises(identity.IdentityError, match="HTTP 500"):
        run(identity.authorization_server(identity.SHOP_ISSUER))

    servers.overrides[rfc8414] = httpx.Response(404)
    servers.overrides["https://accounts.shop.app/.well-known/openid-configuration"] = \
        httpx.Response(200, json=SHOP_METADATA)
    assert run(identity.authorization_server(identity.SHOP_ISSUER))["issuer"] == identity.SHOP_ISSUER


def test_store_issuer_comes_from_protected_resource_metadata(servers):
    assert run(identity.store_issuer(STORE_URL)) == STORE_ISSUER


# ----------------------------------------------------- Shop link + chaining


def test_shop_link_uses_the_dev_dashboard_client_with_pkce(servers, registry):
    params = query(run(identity.start_link(USER, identity.SHOP, registry)))
    assert params["code_challenge_method"] == "S256"
    assert params["client_id"] == "store-client"            # Shop recognises the Dev Dashboard client
    assert params["redirect_uri"] == f"{AGENT}/api/identity/callback"
    assert set(params["scope"].split()) == {"openid", "email", "dev.ucp.shopping.checkout:manage"}

    run(identity.finish_link(params["state"], "shop-code", identity.SHOP_ISSUER, None, registry))
    _, form, request = servers.of("authorization_code")[0]
    assert identity.pkce_challenge(form["code_verifier"]) == params["code_challenge"]
    assert client_auth(form) == ("store-client", "store-secret")
    assert "authorization" not in request.headers


def test_without_a_shared_client_shop_uses_our_metadata_document(servers, registry, monkeypatch):
    monkeypatch.delenv("SHOPIFY_CLIENT_ID")
    monkeypatch.delenv("SHOPIFY_CLIENT_SECRET")
    params = query(run(identity.start_link(USER, identity.SHOP, registry)))
    assert params["client_id"] == f"{AGENT}/oauth/client.json"
    run(identity.finish_link(params["state"], "shop-code", identity.SHOP_ISSUER, None, registry))
    _, form, _ = servers.of("authorization_code")[0]
    assert client_auth(form) == (params["client_id"], None)    # public client: PKCE only


def test_shop_link_chains_every_store_without_a_redirect(servers, registry, store):
    link_shop(registry)

    _, exchange, request = servers.of(identity.TOKEN_EXCHANGE)[0]
    assert exchange["audience"] == STORE_DOMAIN                 # Shopify: the store's domain
    assert exchange["subject_token"].startswith("shop-access-")
    assert exchange["subject_token_type"] == identity.ACCESS_TOKEN_TYPE
    assert exchange["requested_token_type"] == identity.JWT_TOKEN_TYPE

    url, bearer, request = servers.of(identity.JWT_BEARER)[0]
    assert url == STORE_METADATA["token_endpoint"]
    assert bearer["assertion"].startswith("jwt-grant-")
    assert set(bearer["scope"].split()) == STORE_SCOPES
    assert client_auth(bearer) == ("store-client", "store-secret")

    status = identity.status(USER, registry)
    assert status["shop"] == {"linked": True, "account": "buyer@example.com"}
    assert status["stores"][0] | {"linked": True, "via": "chained"} == status["stores"][0]
    assert run(identity.bearer_for(USER, store)).startswith("store-chained-")


def test_tokens_are_encrypted_at_rest(servers, registry):
    link_shop(registry)
    with db.connect() as conn:
        rows = conn.execute("SELECT access_token, refresh_token FROM identity_link").fetchall()
    assert rows and all(not row["access_token"].startswith(("shop-", "store-")) for row in rows)


def test_refused_chain_is_reported_and_not_retried_at_once(servers, registry, store):
    servers.overrides[STORE_METADATA["token_endpoint"]] = httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "terms must be accepted"})
    link_shop(registry)
    assert "invalid_grant" in identity.status(USER, registry)["stores"][0]["error"]
    calls = len(servers.requests)
    assert run(identity.bearer_for(USER, store)) is None
    assert len(servers.requests) == calls        # no new exchange during the back-off


def test_expired_chained_token_is_chained_again(servers, registry, store):
    link_shop(registry)
    first = run(identity.bearer_for(USER, store))
    identity.invalidate(USER, store.id)
    second = run(identity.bearer_for(USER, store))
    assert second.startswith("store-chained-") and second != first
    assert len(servers.of(identity.JWT_BEARER)) == 2


# ---------------------------------------------------- callback protections


def test_state_is_single_use(servers, registry):
    params = query(run(identity.start_link(USER, identity.SHOP, registry)))
    run(identity.finish_link(params["state"], "code", identity.SHOP_ISSUER, None, registry))
    with pytest.raises(identity.IdentityError, match="unknown or has expired"):
        run(identity.finish_link(params["state"], "code", identity.SHOP_ISSUER, None, registry))


@pytest.mark.parametrize("iss", ["https://evil.example", None])
def test_wrong_or_missing_iss_is_rejected(servers, registry, iss):
    params = query(run(identity.start_link(USER, identity.SHOP, registry)))
    with pytest.raises(identity.IdentityError, match="iss"):
        run(identity.finish_link(params["state"], "code", iss, None, registry))
    assert not servers.of("authorization_code")          # the code was never redeemed


def test_declined_sign_in_stores_nothing(servers, registry):
    params = query(run(identity.start_link(USER, identity.SHOP, registry)))
    with pytest.raises(identity.IdentityError, match="access_denied"):
        run(identity.finish_link(params["state"], None, identity.SHOP_ISSUER, "access_denied", registry))
    assert not identity.status(USER, registry)["shop"]["linked"]


# ------------------------------------------------- direct store link


def test_direct_store_link_and_refresh(servers, registry, store):
    params = query(run(identity.start_link(USER, store.id, registry)))
    assert urlsplit(run(identity.start_link(USER, store.id, registry))).netloc == "auth.example"
    assert params["client_id"] == "store-client" and set(params["scope"].split()) == STORE_SCOPES
    run(identity.finish_link(params["state"], "store-code", STORE_ISSUER, None, registry))
    assert client_auth(servers.of("authorization_code")[0][1]) == ("store-client", "store-secret")

    identity.invalidate(USER, store.id)
    token = run(identity.bearer_for(USER, store))
    _, refresh, _ = servers.of("refresh_token")[0]
    assert refresh["refresh_token"].startswith("store-refresh-")
    assert token.startswith("store-access-")
    assert identity.status(USER, registry)["stores"][0]["via"] == "oauth"


def test_403_scope_is_requested_on_the_next_link(servers, registry, store):
    identity.note_insufficient_scope(USER, store.id,
                                     'Bearer error="insufficient_scope", scope="dev.ucp.shopping.order:manage"')
    params = query(run(identity.start_link(USER, store.id, registry)))
    assert set(params["scope"].split()) == STORE_SCOPES | {"dev.ucp.shopping.order:manage"}


# ------------------------------------------------------------------ unlink


def test_unlinking_shop_revokes_both_layers(servers, registry):
    link_shop(registry)
    run(identity.unlink(USER, identity.SHOP))
    revoked = {(url, form["token_type_hint"]) for url, form, _ in servers.requests if url.endswith("/revoke")}
    assert (SHOP_METADATA["revocation_endpoint"], "refresh_token") in revoked
    assert (STORE_METADATA["revocation_endpoint"], "access_token") in revoked
    status = identity.status(USER, registry)
    assert not status["shop"]["linked"] and not status["stores"][0]["linked"]


# --------------------------------------------------------------------- API


@pytest.fixture
def api():
    from client import app
    client = TestClient(app)
    token = client.post("/api/login", json={"username": USER, "password": PASSWORD}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def test_identity_endpoints_need_login():
    from client import app
    anon = TestClient(app)
    assert anon.get("/api/identity").status_code == 401
    assert anon.post("/api/identity/link", json={"target": "shop"}).status_code == 401


def test_link_is_refused_when_switched_off(api, monkeypatch):
    monkeypatch.delenv("IDENTITY_TOKEN_KEY", raising=False)
    assert api.get("/api/identity").json()["enabled"] is False
    assert api.post("/api/identity/link", json={"target": "shop"}).status_code == 409


def test_callback_with_unknown_state_goes_back_to_the_frontend(api):
    response = api.get("/api/identity/callback?state=forged&code=x", follow_redirects=False)
    assert response.status_code == 303
    assert "identity=error" in response.headers["location"]


def test_client_metadata_document_names_its_own_url():
    from client import app
    document = TestClient(app).get("/oauth/client.json").json()
    assert document["client_id"].endswith("/oauth/client.json")
    assert document["token_endpoint_auth_method"] == "none"
    assert json.dumps(document)  # plain JSON
