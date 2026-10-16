"""The shopping session and the UCP actions behind the chat.

agent.py's model decides what to do; the methods here do it. Each action calls a
store's UCP tools and returns blocks for the frontend (text, notice, products,
product, capabilities, cart, checkout - see ucp-frontend/src/lib/types.ts).

Several stores: a search goes to the stores selling the categories the model picked,
every product is tagged with its store, and each store has its own cart. UCP carts
and checkouts belong to one business, so there is one checkout per store, and one
open at a time.

Payment is not enabled: checkout stops at reviewing the details.
"""

import asyncio
import html
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from merchants import CART, CHECKOUT, SEARCH, Merchant, Registry

logger = logging.getLogger("ucp-client")

PAGE_SIZE = 8  # products per page from a single store
MIN_PER_STORE = 4  # products per page from each store when several are searched
MAX_CATEGORIES = 3
MAX_SESSIONS = 500
EMPTY_STATE = {"cart_count": 0, "cart_subtotal": 0, "checkout_id": None}

# The store needs these with a shipping address before it can offer delivery.
RECIPIENT_FIELDS = ("first_name", "last_name", "phone_number")

# Hidden from the capabilities card for now. Empty this set to show them again.
HIDDEN_CAPABILITIES = {
    "dev.ucp.shopping.checkout",
    "dev.ucp.shopping.discount",
    "dev.ucp.shopping.fulfillment",
    "dev.ucp.shopping.order",
    "dev.ucp.common.identity_linking",
}

# Products returned by a store are tagged with its merchant id under this key.
STORE_KEY = "_merchant"

CallTool = Callable[[Merchant, str, dict], Awaitable[dict]]
Blocks = list[dict]


@dataclass
class Session:
    id: str
    products: list[dict] = field(default_factory=list)  # last products shown, in display order, tagged with STORE_KEY
    last_search: dict | None = None  # the request, without pagination
    next_cursors: dict[str, str] = field(default_factory=dict)  # merchant id -> next page cursor
    detail: dict | None = None  # last product shown in detail
    carts: dict[str, dict] = field(default_factory=dict)  # merchant id -> that store's last cart response
    checkout: dict | None = None  # the open checkout's last response
    checkout_store: str | None = None  # merchant id of the open checkout
    # Checkout details the shopper gave. UCP update_checkout is a full replacement,
    # so these are sent again with every update.
    buyer: dict = field(default_factory=dict)
    address: dict = field(default_factory=dict)
    delivery_option: str | None = None
    history: list[dict] = field(default_factory=list)  # earlier messages, for the model


# ------------------------------------------------------------------ blocks


def text(markdown: str) -> dict:
    return {"type": "text", "text": markdown}


def notice(tone: str, message: str) -> dict:
    return {"type": "notice", "tone": tone, "text": message}


def notices(messages: list[dict] | None) -> Blocks:
    return [notice(m.get("type", "info"), m["content"]) for m in messages or [] if m.get("content")]


def plain_text(description: dict | None) -> str | None:
    description = description or {}
    if description.get("plain"):
        return description["plain"]
    if description.get("html"):
        return html.unescape(re.sub(r"<[^>]+>", " ", description["html"])).strip()
    return None


def image_of(item: dict) -> str | None:
    return (item.get("media") or [{}])[0].get("url")


def total_of(totals: list[dict] | None, kind: str) -> int:
    return next((t["amount"] for t in totals or [] if t.get("type") == kind), 0)


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def money(price: dict) -> str:
    """A UCP price ({amount, currency}) for the model to read; not every store prices in USD."""
    if price.get("currency", "USD") == "USD":
        return dollars(price["amount"])
    return f"{price['amount'] / 100:,.2f} {price['currency']}"


def product_card(product: dict, store: str | None = None) -> dict:
    variants = product.get("variants") or []
    featured = variants[0]
    many = len(variants) > 1
    price_range = product.get("price_range") or {}
    card = {
        "id": product["id"],
        "title": product["title"],
        "brand": store,  # the frontend shows it above the title
        "image": image_of(product) or image_of(featured),
        "price": featured["price"],
        "availability": "in_stock" if featured.get("availability", {}).get("available", True) else "out_of_stock",
        "variant_count": len(variants),
        "variant_label": featured.get("title") if many else None,
        "option_name": " / ".join(o["name"] for o in product.get("options") or []) or None,
        "variants": [
            {
                "id": v["id"],
                "label": v.get("title") if many else None,
                "price": v["price"],
                "list_price": v.get("list_price"),
                "available": v.get("availability", {}).get("available", True),
            }
            for v in variants
        ],
    }
    if featured.get("list_price"):
        card["list_price"] = featured["list_price"]
    if price_range.get("min") and price_range.get("min") != price_range.get("max"):
        card["price_from"] = price_range["min"]
    return card


def product_detail(product: dict, store: str | None = None) -> dict:
    detail = product_card(product, store)
    featured = product["variants"][0]
    available = featured.get("availability", {}).get("available", True)
    description = plain_text(product.get("description"))

    # The detail card shows one option group; use the first one with a real choice.
    option = next((o for o in product.get("options") or [] if len(o.get("values") or []) > 1), {})
    selected = {s["name"]: s["label"] for s in product.get("selected") or []}

    highlights = [f"SKU: {featured['sku']}"] if featured.get("sku") else []
    if description:
        highlights.append(description)

    detail.update({
        "description": description,
        "highlights": highlights,
        "tags": product.get("tags") or [],
        "option_name": option.get("name"),
        "options": [{"label": v["label"], "available": True} for v in option.get("values") or []],
        "variant": {
            "id": featured["id"],
            "sku": featured.get("sku"),
            "label": selected.get(option.get("name"), featured.get("title")),
            "price": featured["price"],
            "list_price": featured.get("list_price"),
            "availability": "in_stock" if available else "out_of_stock",
            "purchasable": available,
        },
        "policies": [],
    })
    return {"type": "product", "product": detail}


def capabilities_block(merchant: Merchant, profile: dict) -> dict:
    ucp = profile.get("ucp") or {}
    return {
        "type": "capabilities",
        "version": ucp.get("version"),
        "business": {"name": merchant.name, "categories": merchant.categories},
        "capabilities": [
            {"name": name, "version": entries[0].get("version"), "spec": entries[0].get("spec")}
            for name, entries in sorted((ucp.get("capabilities") or {}).items())
            if entries and name not in HIDDEN_CAPABILITIES
        ],
        # Payment handlers are hidden for now; uncomment to show them again.
        "payment_handlers": [
            # {"name": name, "id": entries[0].get("id", name), "version": entries[0].get("version")}
            # for name, entries in sorted((ucp.get("payment_handlers") or {}).items())
            # if entries
        ],
    }


def line_view(line: dict) -> dict:
    """A cart or checkout line as the frontend shows it."""
    item = line["item"]
    return {
        "id": line.get("id"),
        "variant_id": item["id"],
        "title": item.get("title", ""),
        "image": item.get("image_url"),
        "price": item.get("price", 0),
        "quantity": line["quantity"],
        "line_total": total_of(line.get("totals"), "total") or item.get("price", 0) * line["quantity"],
    }


def cart_block(cart: dict | None, store: str | None = None) -> dict:
    """A UCP cart as the frontend's cart card (an empty card when there is no cart)."""
    cart = cart or {}
    return {
        "type": "cart",
        "store": store,
        "currency": cart.get("currency", "USD"),
        "subtotal": total_of(cart.get("totals"), "subtotal"),
        "lines": [line_view(line) for line in cart.get("line_items") or []],
    }


def cart_count(cart: dict | None) -> int:
    return sum(line["quantity"] for line in (cart or {}).get("line_items") or [])


def cart_state(session: "Session") -> dict:
    """The cart summary the frontend shows in the top bar and sidebar, across all stores' carts."""
    return {
        **EMPTY_STATE,
        "cart_count": sum(cart_count(cart) for cart in session.carts.values()),
        "cart_subtotal": sum(total_of(cart.get("totals"), "subtotal") for cart in session.carts.values()),
        "checkout_id": (session.checkout or {}).get("id"),
    }


def shipping_method(checkout: dict) -> dict:
    return next((m for m in (checkout.get("fulfillment") or {}).get("methods") or [] if m.get("type") == "shipping"), {})


def delivery_group(checkout: dict) -> dict:
    return (shipping_method(checkout).get("groups") or [{}])[0]


def checkout_block(checkout: dict, given: dict | None = None, store: str | None = None) -> dict:
    """
    A UCP checkout as the frontend's checkout card. `given` holds the buyer and address
    details the shopper entered, to pre-fill the card's delivery details form.
    """
    given = given or {}
    method = shipping_method(checkout)
    destinations = method.get("destinations") or [{}]
    destination = next((d for d in destinations if d.get("id") == method.get("selected_destination_id")), destinations[0])
    group = delivery_group(checkout)
    return {
        "type": "checkout",
        "checkout": {
            "store": store,
            "id": checkout["id"],
            "status": checkout.get("status", ""),
            "currency": checkout.get("currency", "USD"),
            "line_items": [line_view(line) for line in checkout.get("line_items") or []],
            "totals": checkout.get("totals") or [],
            "buyer": checkout.get("buyer") or {},
            "address": {k: v for k, v in destination.items()
                        if k in ("street_address", "address_locality", "address_region", "postal_code")},
            "shipping_options": [
                {
                    "id": option["id"],
                    "title": option.get("title", ""),
                    "description": plain_text(option.get("description")),
                    "amount": total_of(option.get("totals"), "total"),
                }
                for option in group.get("options") or []
            ],
            "selected_shipping_id": group.get("selected_option_id"),
            "instruments": [
                {"id": i.get("id", ""), "label": i.get("display", {}).get("brand") or i.get("handler_id", ""),
                 "handler": i.get("handler_id", ""), "selected": bool(i.get("selected"))}
                for i in (checkout.get("payment") or {}).get("instruments") or []
            ],
            # path (e.g. "$.buyer.phone_number") and severity let the card point a message at a form field.
            "messages": [{"type": m.get("type", "info"), "code": m.get("code"), "content": m.get("content", ""),
                          "path": m.get("path"), "severity": m.get("severity")}
                         for m in checkout.get("messages") or []],
            "links": checkout.get("links") or [],
            "details": {
                "email": given.get("email", ""),
                "first_name": given.get("first_name", ""),
                "last_name": given.get("last_name", ""),
                "phone": given.get("phone_number", ""),
                "street_address": given.get("street_address", ""),
                "city": given.get("address_locality", ""),
                "region": given.get("address_region", ""),
                "postal_code": given.get("postal_code", ""),
                "country": given.get("address_country", ""),
            },
            # Payment is switched off for now, so the store's payment page link isn't passed on.
            # "continue_url": checkout.get("continue_url"),
        },
    }


# ----------------------------------------------------------------- helpers


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def within_price(products: list[dict], price: dict | None, currency: str) -> list[dict]:
    """
    The store's price filter is loose, so check each product's lowest price again.
    Prices in another currency can't be compared, so those products are kept.
    """
    if not price:
        return products
    low, high = price.get("min", 0), price.get("max", float("inf"))
    kept = []
    for p in products:
        lowest = (p.get("price_range") or {}).get("min") or {}
        if lowest.get("currency", currency) != currency or low <= lowest.get("amount", 0) <= high:
            kept.append(p)
    return kept


def is_error(response: dict) -> bool:
    return response.get("ucp", {}).get("status") == "error"


def is_not_found(response: dict) -> bool:
    """UCP reports a missing (expired or cancelled) cart or checkout as an error with a *not_found code."""
    return is_error(response) and any(str(m.get("code", "")).endswith("not_found") for m in response.get("messages") or [])


def missing_recipient(session: Session) -> list[str]:
    """Recipient details the delivery destination still needs."""
    return [name for name in RECIPIENT_FIELDS if not session.buyer.get(name)]


# -------------------------------------------------------------------- shop


class Shop:
    def __init__(self, registry: Registry, call_tool: CallTool, currency: str | None = None, country: str | None = None):
        self.registry = registry
        self.call_tool = call_tool  # (merchant, tool, tool arguments)
        self.currency = currency
        # Sent with every cart so the store prices it like the catalog (it would
        # otherwise guess the shopper's country from their IP address).
        self.cart_context = {"address_country": country, "currency": currency}
        self.sessions: dict[str, Session] = {}

    def session(self, session_id: str | None) -> Session:
        if session_id in self.sessions:
            return self.sessions[session_id]
        if len(self.sessions) >= MAX_SESSIONS:
            self.sessions.pop(next(iter(self.sessions)))  # drop the oldest
        session = Session(id=str(uuid.uuid4()))
        self.sessions[session.id] = session
        return session

    def store_of(self, product: dict) -> Merchant:
        return self.registry.get(product[STORE_KEY])

    async def capabilities(self, store_id: str | None) -> Blocks:
        merchants = [self.registry.get(store_id)] if self.registry.get(store_id) else self.registry.all()
        blocks: Blocks = []
        for merchant in merchants:
            if merchant.error:
                blocks.append(notice("warning", f"{merchant.name} is unavailable: {merchant.error}"))
            else:
                blocks.append(capabilities_block(merchant, await merchant.profile()))
        return blocks

    # ------------------------------------------------------------- catalog

    def stores_for(self, categories: list[str] | None) -> tuple[list[Merchant], str]:
        """The stores to search for these categories, and a note saying why, for the model."""
        searchable = [m for m in self.registry.all() if m.supports(SEARCH)]
        wanted = [c for c in categories or [] if c in self.registry.categories][:MAX_CATEGORIES]
        if not wanted:
            return searchable, "No category was given, so every store was searched."
        selling = self.registry.selling(wanted)
        chosen = [m for m in selling if m.supports(SEARCH)]
        down = [m.name for m in selling if not m.supports(SEARCH)]
        note = f" ({', '.join(down)} also sells this but can't be searched right now.)" if down else ""
        if not chosen:
            return searchable, f"No available store sells {', '.join(wanted)}, so every store was searched.{note}"
        return chosen, f"Searched {', '.join(m.name for m in chosen)} for {', '.join(wanted)}.{note}"

    async def search(self, session: Session, query: str | None, categories: list[str] | None,
                     low: int | None, high: int | None) -> Blocks:
        stores, why = self.stores_for(categories)
        logger.info(f"Categories {categories} -> {[m.id for m in stores]}: {why}")
        request: dict = {"context": {"currency": self.currency}}
        if query:
            request["query"] = query
        if low is not None or high is not None:
            request["filters"] = {"price": {k: v for k, v in (("min", low), ("max", high)) if v is not None}}
        session.last_search = request
        session.next_cursors = {}
        limit = PAGE_SIZE if len(stores) == 1 else MIN_PER_STORE
        results = await asyncio.gather(*(self.search_store(m, request, limit) for m in stores), return_exceptions=True)
        return self.show_results(session, request, list(zip(stores, results)), why)

    async def search_store(self, merchant: Merchant, request: dict, limit: int, cursor: str | None = None) -> dict:
        page = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        response = await self.call_tool(merchant, "search_catalog", {"catalog": {**request, "pagination": page}})
        query = request.get("query")
        # Nothing found for "lamps"? Try "lamp".
        if not cursor and not response.get("products") and query and (simpler := " ".join(map(singular, query.split()))) != query:
            response = await self.call_tool(merchant, "search_catalog",
                                            {"catalog": {**request, "query": simpler, "pagination": page}})
        return response

    async def more(self, session: Session) -> Blocks:
        if not session.last_search or not session.next_cursors:
            return [text("There are no more results for the last search.")]
        stores = [self.registry.get(store_id) for store_id in session.next_cursors]
        cursors = dict(session.next_cursors)
        session.next_cursors = {}
        limit = PAGE_SIZE if len(stores) == 1 else MIN_PER_STORE
        results = await asyncio.gather(
            *(self.search_store(m, session.last_search, limit, cursors[m.id]) for m in stores), return_exceptions=True
        )
        return self.show_results(session, session.last_search, list(zip(stores, results)),
                                 f"Next page from {', '.join(m.name for m in stores)}.")

    def show_results(self, session: Session, request: dict, results: list[tuple[Merchant, dict | BaseException]],
                     why: str) -> Blocks:
        price = (request.get("filters") or {}).get("price")
        products, counts, problems = [], [], []
        for merchant, response in results:
            if isinstance(response, BaseException):
                problems.append(notice("warning", f"{merchant.name} couldn't be searched: {response}"))
                continue
            found = within_price(response.get("products") or [], price, self.currency)
            for product in found:
                product[STORE_KEY] = merchant.id
            products += found
            counts.append(f"{merchant.name} {len(found)}")
            pagination = response.get("pagination") or {}
            if pagination.get("has_next_page") and pagination.get("cursor"):
                session.next_cursors[merchant.id] = pagination["cursor"]
            problems += notices(response.get("messages"))

        summary = f"{why} Results per store: {', '.join(counts) or 'none'}."
        if not products:
            more = " More pages are available." if session.next_cursors else ""
            return [text(f"No products matched. {summary}{more}"), *problems]
        session.products = products
        cards = {"type": "products", "products": [product_card(p, self.store_of(p).name) for p in products]}
        return [text(f"Found {len(products)} products. {summary}"), cards, *problems]

    async def detail(self, session: Session, product_id: str, option_label: str | None = None) -> Blocks:
        known = self.find_product(session, product_id)
        if not known:
            return [notice("warning", "Search for the product first, then ask for its details.")]
        merchant = self.store_of(known)
        request: dict = {"id": product_id, "context": {"currency": self.currency}}
        if option_label:
            # UCP get_product selects an option by its name and label, e.g. Color: Navy.
            option = next((o["name"] for o in known.get("options") or []
                           for v in o.get("values") or [] if v["label"].lower() == option_label.lower()), None)
            if not option:
                return [text(f"“{option_label}” isn't an option for this product.")]
            request["selected"] = [{"name": option, "label": option_label}]

        response = await self.call_tool(merchant, "get_product", {"catalog": request})
        product = response.get("product")
        if not product:
            return notices(response.get("messages")) or [text("That product couldn't be loaded.")]
        product[STORE_KEY] = merchant.id
        session.detail = product
        return [product_detail(product, merchant.name), *notices(response.get("messages"))]

    # ---------------------------------------------------------------- cart

    def find_product(self, session: Session, product_id: str) -> dict | None:
        # The detail view has the selected variant first, so prefer it.
        return next((p for p in [session.detail, *session.products] if p and p["id"] == product_id), None)

    @staticmethod
    def cart_lines(session: Session, store_id: str) -> dict[str, int]:
        """A store's cart as {variant id: quantity}."""
        cart = session.carts.get(store_id) or {}
        return {line["item"]["id"]: line["quantity"] for line in cart.get("line_items") or []}

    async def add_to_cart(self, session: Session, product_id: str, variant_id: str | None, quantity: int) -> Blocks:
        product = self.find_product(session, product_id)
        if not product:
            return [notice("warning", "Show the product first, then add it.")]
        merchant = self.store_of(product)
        if not merchant.supports(CART):
            where = f" It can be bought on the store's site: {product['url']}" if product.get("url") else ""
            return [notice("warning", f"{merchant.name} doesn't offer a cart over UCP, so this can't be added here.{where}")]
        variants = product.get("variants") or []
        variant = next((v for v in variants if v["id"] == variant_id), variants[0])
        lines = self.cart_lines(session, merchant.id)
        lines[variant["id"]] = lines.get(variant["id"], 0) + quantity
        name = f"{product['title']} ({variant['title']})" if len(variants) > 1 else product["title"]
        return await self.save_cart(session, merchant, lines, f"Added {name} × {quantity} to the {merchant.name} cart.")

    async def set_quantity(self, session: Session, variant_id: str, quantity: int) -> Blocks:
        """Set one cart line's quantity; 0 removes it."""
        store_id = next((s for s in session.carts if variant_id in self.cart_lines(session, s)), None)
        if not store_id:
            return [notice("warning", "That item isn't in any cart.")]
        lines = self.cart_lines(session, store_id)
        if quantity > 0:
            lines[variant_id] = quantity
        else:
            lines.pop(variant_id)
        merchant = self.registry.get(store_id)
        return await self.save_cart(session, merchant, lines, f"{merchant.name} cart updated.")

    async def empty_carts(self, session: Session) -> Blocks:
        if not session.carts:
            return [text("The cart is empty."), cart_block(None)]
        blocks: Blocks = []
        for store_id in list(session.carts):
            blocks += await self.save_cart(session, self.registry.get(store_id), {}, "")
        return [text("All carts are now empty."), *[b for b in blocks if b["type"] != "text"]]

    async def save_cart(self, session: Session, merchant: Merchant, lines: dict[str, int], message: str) -> Blocks:
        """
        Create the store's cart, or replace its contents. UCP update_cart is a full
        replacement, so every line (and the context) is sent each time.
        """
        items = [{"item": {"id": variant_id}, "quantity": qty} for variant_id, qty in lines.items()]
        cart = {"line_items": items, "context": self.cart_context}
        if session.checkout_store == merchant.id:
            # An open checkout no longer matches the cart; Checkout starts a new one.
            session.checkout = session.checkout_store = None

        response = None
        existing = session.carts.get(merchant.id)
        if existing:
            response = await self.call_tool(merchant, "update_cart", {"id": existing["id"], "cart": cart})
            if is_not_found(response):  # expired or cancelled on the store's side; start a new cart
                session.carts.pop(merchant.id)
                existing = response = None
        if not existing:
            if not items:
                return [text(f"The {merchant.name} cart is empty."), cart_block(None, merchant.name)]
            response = await self.call_tool(merchant, "create_cart", {"cart": cart})

        if is_error(response) or not response.get("id"):
            return notices(response.get("messages")) or [notice("error", f"{merchant.name} couldn't update the cart.")]
        if items:
            session.carts[merchant.id] = response
        else:
            session.carts.pop(merchant.id, None)
        return [text(message), cart_block(response, merchant.name), *notices(response.get("messages"))]

    async def view_cart(self, session: Session) -> Blocks:
        if not session.carts:
            return [text("The cart is empty."), cart_block(None)]

        blocks: Blocks = []
        for store_id, cart in list(session.carts.items()):
            merchant = self.registry.get(store_id)
            response = await self.call_tool(merchant, "get_cart", {"id": cart["id"]})
            if is_not_found(response):
                session.carts.pop(store_id)
                blocks.append(text(f"The {merchant.name} cart has expired, so it's empty now."))
            elif is_error(response):
                blocks += notices(response.get("messages")) or [notice("error", f"{merchant.name} couldn't load the cart.")]
            else:
                session.carts[store_id] = response
                blocks += [text(f"{merchant.name} cart:"), cart_block(response, merchant.name),
                           *notices(response.get("messages"))]
        return blocks or [text("The cart is empty."), cart_block(None)]

    # ------------------------------------------------------------ checkout

    async def start_checkout(self, session: Session, store_id: str | None) -> Blocks:
        with_items = [s for s, cart in session.carts.items() if cart_count(cart)]
        if not with_items:
            return [text("The cart is empty, so there is nothing to check out.")]
        if not store_id:
            if len(with_items) > 1:
                names = " and ".join(self.registry.get(s).name for s in with_items)
                return [text(f"There are carts at {names}. Each store checks out separately: "
                             "ask the shopper which store to check out first.")]
            store_id = with_items[0]
        if store_id not in with_items:
            return [text(f"There's nothing in the {self.registry.get(store_id).name} cart to check out.")]
        if session.checkout and session.checkout_store != store_id:
            open_at = self.registry.get(session.checkout_store).name
            return [text(f"A checkout is already open at {open_at}. Finish or cancel it before starting another.")]

        merchant = self.registry.get(store_id)
        if not merchant.supports(CHECKOUT):
            return [notice("warning", f"{merchant.name} doesn't offer checkout over UCP.")]
        # UCP: a checkout can be created from the cart; the store takes the items from it.
        response = await self.call_tool(
            merchant, "create_checkout", {"checkout": {"cart_id": session.carts[store_id]["id"], "line_items": []}}
        )
        if is_not_found(response):
            session.carts.pop(store_id)
            return [text(f"The {merchant.name} cart has expired, so there is nothing to check out.")]

        session.delivery_option = None
        session.checkout_store = store_id
        if response.get("id") and (session.buyer or session.address):
            # Reuse details the shopper already gave in this chat.
            session.checkout = response
            response = await self.call_tool(
                merchant, "update_checkout", {"id": response["id"], "checkout": self.checkout_payload(session)}
            )
        return self.checkout_reply(session, response)

    def checkout_merchant(self, session: Session) -> Merchant:
        return self.registry.get(session.checkout_store)

    async def view_checkout(self, session: Session) -> Blocks:
        if not session.checkout:
            return [text("There's no checkout open.")]
        response = await self.call_tool(self.checkout_merchant(session), "get_checkout", {"id": session.checkout["id"]})
        return self.checkout_reply(session, response)

    async def update_checkout(self, session: Session, details: dict) -> Blocks:
        """
        Save buyer and shipping details given as: email, phone, first_name, last_name,
        street_address, city, region, postal_code, country (any may be missing).
        The address can be in any country; the store decides whether it delivers there.
        """
        if not session.checkout:
            return [notice("warning", "Start the checkout first.")]
        for name in ("email", "first_name", "last_name"):
            if details.get(name):
                session.buyer[name] = details[name].strip()

        address = {"street_address": details.get("street_address"), "address_locality": details.get("city"),
                   "postal_code": details.get("postal_code"),
                   "address_country": (details.get("country") or "").strip().upper() or None}
        if all(address.values()):
            if details.get("region"):  # not every country has states or provinces
                address["address_region"] = details["region"].strip()
            session.address = address

        if details.get("phone"):
            phone = re.sub(r"[^\d+]", "", details["phone"])
            # UCP expects E.164 phone numbers. Without a country code, only a 10-digit number
            # for a US address can safely get +1; anything else is passed on for the store to check.
            if not phone.startswith("+"):
                us = session.address.get("address_country", "US") == "US" and len(phone) == 10
                phone = f"+1{phone}" if us else f"+{phone}"
            session.buyer["phone_number"] = phone
        return await self.send_checkout(session)

    async def choose_delivery(self, session: Session, option_id: str) -> Blocks:
        if not session.checkout:
            return [notice("warning", "Start the checkout first.")]
        session.delivery_option = option_id
        return await self.send_checkout(session)

    async def send_checkout(self, session: Session) -> Blocks:
        response = await self.call_tool(
            self.checkout_merchant(session), "update_checkout",
            {"id": session.checkout["id"], "checkout": self.checkout_payload(session)},
        )
        return self.checkout_reply(session, response)

    async def cancel_checkout(self, session: Session) -> Blocks:
        if not session.checkout:
            return [text("There's no checkout open.")]
        merchant = self.checkout_merchant(session)
        await self.call_tool(merchant, "cancel_checkout", {"id": session.checkout["id"]})
        session.checkout = session.checkout_store = None
        session.delivery_option = None
        # The store may close the cart together with its checkout.
        cart = session.carts.get(merchant.id)
        if cart and is_not_found(await self.call_tool(merchant, "get_cart", {"id": cart["id"]})):
            session.carts.pop(merchant.id)
        return [text(f"{merchant.name} checkout cancelled.")]

    def checkout_payload(self, session: Session) -> dict:
        """The whole checkout, as UCP update_checkout expects (it replaces the previous state)."""
        lines = session.checkout.get("line_items") or []
        payload: dict = {
            "line_items": [{"item": {"id": line["item"]["id"]}, "quantity": line["quantity"]} for line in lines],
            # The context follows the shipping address once there is one.
            "context": {**self.cart_context, **({"address_country": session.address["address_country"]}
                                                if session.address else {})},
        }
        if session.buyer:
            payload["buyer"] = session.buyer
        if session.address and not missing_recipient(session):
            # The store needs the recipient's name and phone with the address, and keeps the
            # first destination it was given, so the destination is sent once it is complete.
            recipient = {name: session.buyer[name] for name in RECIPIENT_FIELDS}
            method = {
                "type": "shipping",
                "line_item_ids": [line["id"] for line in lines],
                "destinations": [{**session.address, **recipient}],
            }
            group = delivery_group(session.checkout)
            if session.delivery_option and group.get("id"):
                method["groups"] = [{"id": group["id"], "selected_option_id": session.delivery_option}]
            payload["fulfillment"] = {"methods": [method]}
        return payload

    def checkout_reply(self, session: Session, response: dict) -> Blocks:
        """Show the checkout and say what the store still needs."""
        merchant = self.checkout_merchant(session)
        if is_not_found(response):
            session.checkout = session.checkout_store = None
            return [text("That checkout has expired. Start the checkout again.")]
        if not response.get("id"):
            if not session.checkout:
                session.checkout_store = None
            return notices(response.get("messages")) or [notice("error", f"{merchant.name} couldn't open a checkout.")]

        session.checkout = response
        # UCP: "recoverable" errors are fixed with update_checkout. Anything needing the buyer on the
        # store's own page (requires_buyer_input / review) would go to continue_url, which is switched off.
        needed = [m.get("content", "") for m in response.get("messages") or []
                  if m.get("type") == "error" and m.get("severity") == "recoverable"
                  # Before any address is given the store also says it can't deliver; skip that.
                  and not (m.get("code") == "delivery_no_delivery_available" and not session.address)
                  # We have the address but hold it until the recipient is complete (see checkout_payload).
                  and not (m.get("code") == "delivery_address_required" and session.address)]
        if session.address:
            needed += [f"Recipient {name.replace('_', ' ')}" for name in missing_recipient(session)]

        blocks = [checkout_block(response, {**session.buyer, **session.address}, merchant.name)]
        if needed:
            blocks.append(text(f"{merchant.name} still needs: " + "; ".join(needed)))
        else:
            blocks.append(text(f"The {merchant.name} checkout details are complete. "
                               "Payment isn't enabled, so checkout stops here."))
        # Hand-off to the store's own payment page (UCP continue_url); uncomment to bring it back.
        # elif response.get("continue_url"):
        #     blocks.append(text(
        #         f"Everything that can be filled in here is done. Payment and placing the order happen on "
        #         f"{merchant.name}'s own checkout page: "
        #         f"[Continue to payment on {merchant.name}]({response['continue_url']})"
        #     ))
        return blocks
