"""The shopping session and the UCP actions behind the chat.

agent.py's model decides what to do; the methods here do it. Each action calls a
store's UCP tools and returns blocks for the frontend (text, notice, products,
product, capabilities, cart, checkout - see ucp-frontend/src/lib/types.ts).

Several stores: a search goes to the stores selling the categories the model picked,
every product is tagged with its store, and each store has its own cart. UCP carts
and checkouts belong to one business, so there is one checkout per store, and one
open at a time.

Market context (currency, country, language, region, postal_code) is read from the
persisted buyer profile (profile_store.py) on each relevant UCP call. Buyer info
(name, email, phone) is pre-filled from the profile when a checkout starts if the
session has not yet collected those details. Both are read for the user the session
belongs to, so each logged-in user shops with their own details.

Request-level signals (IP address, User-Agent) are stored on the session and forwarded
to UCP calls that accept a `signals` field, per the official schema. They are never
stored in the profile database.

Payment is not enabled: checkout stops at reviewing the details.
"""

import asyncio
import html
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

import db
from merchants import CART, CHECKOUT, SEARCH, Merchant, Registry

logger = logging.getLogger("ucp-client")

PAGE_SIZE = 8  # products per page from a single store
MIN_PER_STORE = 4  # products per page from each store when several are searched
MAX_CATEGORIES = 3
MAX_SEARCHES = 3  # different things asked for in one message, each shown in its own column
SESSION_DAYS = 7  # a chat untouched this long is forgotten
CART_EMPTY = "The cart is empty."
START_CHECKOUT_FIRST = "Start the checkout first."
EMPTY_STATE: dict[str, Any] = {"cart_count": 0, "cart_subtotal": 0, "checkout_id": None}

# The store needs these with a shipping address before it can offer delivery.
RECIPIENT_FIELDS = ("first_name", "last_name", "phone_number")

# Products returned by a store are tagged with its merchant id under this key.
STORE_KEY = "_merchant"

# A JSON object: a UCP request or response, or a block for the frontend.
JSON = dict[str, Any]


def items(value: Any) -> list[Any]:
    """`value or []`: a JSON list that may be missing or null."""
    return value or []


def fields(value: Any) -> JSON:
    """`value or {}`: a JSON object that may be missing or null."""
    return value or {}


CallTool = Callable[[Merchant, str, JSON], Awaitable[JSON]]
Blocks = list[JSON]


@dataclass
class Search:
    """One thing the shopper asked for, searched at the stores selling its categories."""
    label: str  # shown above this search's column of results
    request: JSON  # without pagination
    stores: list[Merchant]
    why: str  # which stores were searched and why, for the model
    next_cursors: dict[str, str] = field(default_factory=dict[str, str])  # merchant id -> next page cursor
    found: list[JSON] = field(default_factory=list[JSON])  # products this search last showed


@dataclass
class Session:
    id: str
    username: str  # the logged-in user this session belongs to; profiles are read per user
    products: list[JSON] = field(default_factory=list[JSON])  # last products shown, in display order, tagged with STORE_KEY
    searches: list[Search] = field(default_factory=list[Search])  # the searches behind the products shown
    detail: JSON | None = None  # last product shown in detail
    carts: dict[str, JSON] = field(default_factory=dict[str, JSON])  # merchant id -> that store's last cart response
    checkout: JSON | None = None  # the open checkout's last response
    checkout_store: str | None = None  # merchant id of the open checkout
    # Checkout details the shopper gave. UCP update_checkout is a full replacement,
    # so these are sent again with every update.
    buyer: JSON = field(default_factory=JSON)
    address: JSON = field(default_factory=JSON)
    delivery_option: str | None = None
    history: list[JSON] = field(default_factory=list[JSON])  # earlier messages, for the model
    # IP address and User-Agent from the most recent HTTP request for this session.
    # These are forwarded in the UCP `signals` field; never stored in the profile DB.
    signals: JSON = field(default_factory=JSON)

    def has_more(self) -> bool:
        return any(search.next_cursors for search in self.searches)


# ------------------------------------------------------------------ blocks


def text(markdown: str) -> JSON:
    return {"type": "text", "text": markdown}


def notice(tone: str, message: str) -> JSON:
    return {"type": "notice", "tone": tone, "text": message}


def notices(messages: list[JSON] | None) -> Blocks:
    return [notice(m.get("type", "info"), m["content"]) for m in items(messages) if m.get("content")]


def plain_text(description: JSON | None) -> str | None:
    description = fields(description)
    if description.get("plain"):
        return description["plain"]
    if description.get("html"):
        return html.unescape(re.sub(r"<[^<>]+>", " ", description["html"])).strip()
    return None


def image_of(item: JSON) -> str | None:
    media = items(item.get("media"))
    return fields(media[0]).get("url") if media else None


def total_of(totals: list[JSON] | None, kind: str) -> int:
    return next((t["amount"] for t in items(totals) if t.get("type") == kind), 0)


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def money(price: JSON) -> str:
    """A UCP price ({amount, currency}) for the model to read; not every store prices in USD."""
    if price.get("currency", "USD") == "USD":
        return dollars(price["amount"])
    return f"{price['amount'] / 100:,.2f} {price['currency']}"


def product_card(product: JSON, store: str | None = None) -> JSON:
    variants = items(product.get("variants"))
    featured = variants[0]
    many = len(variants) > 1
    price_range = fields(product.get("price_range"))
    card: JSON = {
        "id": product["id"],
        "title": product["title"],
        "brand": store,  # the frontend shows it above the title
        "image": image_of(product) or image_of(featured),
        "price": featured["price"],
        "availability": "in_stock" if featured.get("availability", {}).get("available", True) else "out_of_stock",
        "variant_count": len(variants),
        "variant_label": featured.get("title") if many else None,
        "option_name": " / ".join(o["name"] for o in items(product.get("options"))) or None,
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


def product_detail(product: JSON, store: str | None = None) -> JSON:
    detail = product_card(product, store)
    featured = product["variants"][0]
    available = featured.get("availability", {}).get("available", True)
    description = plain_text(product.get("description"))

    # The detail card shows one option group; use the first one with a real choice.
    option = fields(next((o for o in items(product.get("options")) if len(items(o.get("values"))) > 1), None))
    selected = {s["name"]: s["label"] for s in items(product.get("selected"))}

    highlights = [f"SKU: {featured['sku']}"] if featured.get("sku") else []
    if description:
        highlights.append(description)

    detail.update({
        "description": description,
        "highlights": highlights,
        "tags": items(product.get("tags")),
        "option_name": option.get("name"),
        "options": [{"label": v["label"], "available": True} for v in items(option.get("values"))],
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


def capabilities_summary(merchant: Merchant, profile: JSON) -> str:
    """What a store supports, in plain words for the model. Nothing here is shown to the shopper."""
    ucp = fields(profile.get("ucp"))
    names = sorted(name for name, entries in fields(ucp.get("capabilities")).items() if entries)
    logger.info(f"{merchant.name} capabilities (UCP {ucp.get('version')}): {', '.join(names)}")
    return (f"{merchant.name} sells {', '.join(merchant.categories)}. "
            f"It supports: {', '.join(names) or 'nothing listed'}.")


def line_view(line: JSON) -> JSON:
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


def cart_block(cart: JSON | None, merchant: Merchant | None = None) -> JSON:
    """A UCP cart as the frontend's cart card (an empty card when there is no cart)."""
    cart = fields(cart)
    # Use the merchant's actual currency; fall back to empty string (not a hardcoded USD).
    return {
        "type": "cart",
        "store": merchant.name if merchant else None,
        "store_id": merchant.id if merchant else None,
        "currency": cart.get("currency", ""),
        "subtotal": total_of(cart.get("totals"), "subtotal"),
        "lines": [line_view(line) for line in items(cart.get("line_items"))],
    }


def cart_count(cart: JSON | None) -> int:
    return sum(line["quantity"] for line in items(fields(cart).get("line_items")))


def cart_state(session: "Session") -> JSON:
    """The cart summary the frontend shows in the top bar and sidebar, across all stores' carts."""
    return {
        **EMPTY_STATE,
        "cart_count": sum(cart_count(cart) for cart in session.carts.values()),
        "cart_subtotal": sum(total_of(cart.get("totals"), "subtotal") for cart in session.carts.values()),
        "checkout_id": fields(session.checkout).get("id"),
    }


def shipping_method(checkout: JSON) -> JSON:
    methods = items(fields(checkout.get("fulfillment")).get("methods"))
    return fields(next((m for m in methods if m.get("type") == "shipping"), None))


def delivery_group(checkout: JSON) -> JSON:
    groups = items(shipping_method(checkout).get("groups"))
    return groups[0] if groups else {}


def checkout_messages(checkout: JSON, address_sent: bool, store: str | None = None) -> list[JSON]:
    """
    The store's checkout messages worth showing the shopper. Until a delivery address has
    been sent, the store says both that one is required and that it can't deliver; those
    only restate that the address is still to come, so they are left out. The store's own
    "can't be shipped" text tells the shopper to remove items, so it is reworded.
    """
    shown: list[JSON] = []
    for m in items(checkout.get("messages")):
        code = m.get("code") or ""
        no_delivery = code.startswith("delivery_no_delivery_available")
        if not address_sent and (no_delivery or code == "delivery_address_required"):
            continue
        content = m.get("content", "")
        if no_delivery:
            content = f"{store or 'The store'} doesn't deliver these items to that address."
        shown.append({"type": m.get("type", "info"), "code": m.get("code"), "content": content,
                      "path": m.get("path"), "severity": m.get("severity")})
    return shown


# Severities meaning only the buyer can resolve the message, on the store's own page (UCP).
BUYER_ONLY = {"requires_buyer_input", "requires_buyer_review"}


def store_checkout_url(checkout: JSON) -> str | None:
    """The store's own checkout page (UCP continue_url), where payment and placing the order happen.

    Every store takes the same path: once nothing fixable is left, the shopper continues
    there, whether the store says ready_for_complete (e.g. Auto-Xpert) or
    requires_escalation (e.g. Pier 1, whose checkout extension needs a click). Only an
    https link is passed on, so a store can't put a script link on the card.
    """
    url = checkout.get("continue_url") or ""
    return url if url.startswith("https://") else None


def checkout_block(checkout: JSON, given: JSON | None = None, store: str | None = None,
                   address_sent: bool = True, continue_url: str | None = None) -> JSON:
    """
    A UCP checkout as the frontend's checkout card. `given` holds the buyer and address
    details the shopper entered, to pre-fill the card's delivery details form.
    """
    method = shipping_method(checkout)
    destinations: list[JSON] = items(method.get("destinations")) or [{}]
    destination = next((d for d in destinations if d.get("id") == method.get("selected_destination_id")), destinations[0])
    group = delivery_group(checkout)
    return {
        "type": "checkout",
        "checkout": {
            "store": store,
            "id": checkout["id"],
            "status": checkout.get("status", ""),
            # Use the merchant's actual currency; fall back to empty string (not a hardcoded USD).
            "currency": checkout.get("currency", ""),
            "line_items": [line_view(line) for line in items(checkout.get("line_items"))],
            "totals": items(checkout.get("totals")),
            "buyer": fields(checkout.get("buyer")),
            "address": {k: v for k, v in destination.items()
                        if k in ("street_address", "address_locality", "address_region", "postal_code")},
            "shipping_options": _shipping_options(group),
            "selected_shipping_id": group.get("selected_option_id"),
            "instruments": _instruments(checkout),
            # path (e.g. "$.buyer.phone_number") and severity let the card point a message at a form field.
            "messages": checkout_messages(checkout, address_sent, store),
            "links": items(checkout.get("links")),
            "details": _details_view(fields(given)),
            # The store's checkout page, set only once nothing fixable is left (see checkout_reply).
            "continue_url": continue_url,
        },
    }


def _shipping_options(group: JSON) -> list[JSON]:
    """A delivery group's options, as the checkout card shows them."""
    return [
        {
            "id": option["id"],
            "title": option.get("title", ""),
            "description": plain_text(option.get("description")),
            "amount": total_of(option.get("totals"), "total"),
        }
        for option in items(group.get("options"))
    ]


def _instruments(checkout: JSON) -> list[JSON]:
    """The checkout's payment instruments, as the checkout card shows them."""
    return [
        {"id": i.get("id", ""), "label": i.get("display", {}).get("brand") or i.get("handler_id", ""),
         "handler": i.get("handler_id", ""), "selected": bool(i.get("selected"))}
        for i in items(fields(checkout.get("payment")).get("instruments"))
    ]


# Delivery details form field -> where the shopper's entered value is kept.
_DETAIL_FIELDS = {
    "email": "email", "first_name": "first_name", "last_name": "last_name", "phone": "phone_number",
    "street_address": "street_address", "city": "address_locality", "region": "address_region",
    "postal_code": "postal_code", "country": "address_country",
}


def _details_view(given: JSON) -> JSON:
    """The shopper's entered details, to pre-fill the card's delivery details form."""
    return {field: given.get(key, "") for field, key in _DETAIL_FIELDS.items()}


# ----------------------------------------------------------------- helpers


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def within_price(products: list[JSON], price: JSON | None, currency: str | None) -> list[JSON]:
    """
    The store's price filter is loose, so check each product's lowest price again.
    Prices in another currency can't be compared, so those products are kept.
    """
    if not price:
        return products
    low, high = price.get("min", 0), price.get("max", float("inf"))
    kept: list[JSON] = []
    for p in products:
        lowest = fields(fields(p.get("price_range")).get("min"))
        if lowest.get("currency", currency) != currency or low <= lowest.get("amount", 0) <= high:
            kept.append(p)
    return kept


def is_error(response: JSON) -> bool:
    return response.get("ucp", {}).get("status") == "error"


def is_not_found(response: JSON) -> bool:
    """UCP reports a missing (expired or cancelled) cart or checkout as an error with a *not_found code."""
    return is_error(response) and any(str(m.get("code", "")).endswith("not_found") for m in items(response.get("messages")))


def given_address(details: JSON) -> JSON | None:
    """The shipping address in the shopper's checkout details, or None until every required part is given."""
    address: JSON = {"street_address": details.get("street_address"), "address_locality": details.get("city"),
                     "postal_code": details.get("postal_code"),
                     "address_country": (details.get("country") or "").strip().upper() or None}
    if not all(address.values()):
        return None
    if details.get("region"):  # not every country has states or provinces
        address["address_region"] = details["region"].strip()
    return address


def e164(phone: str, country: str) -> str:
    """A phone number as UCP expects it (E.164). Without a country code, only a 10-digit
    number for a US address can safely get +1; anything else is passed on for the store to check."""
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("+"):
        return digits
    return f"+1{digits}" if country == "US" and len(digits) == 10 else f"+{digits}"


def missing_recipient(session: Session) -> list[str]:
    """Recipient details the delivery destination still needs."""
    return [name for name in RECIPIENT_FIELDS if not session.buyer.get(name)]


# ---------------------------------------------------------------- sessions


def init_sessions() -> None:
    """Create the session table. Call after auth.init_auth(), which creates app_user."""
    with db.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_session (
                id          TEXT PRIMARY KEY,
                username    TEXT NOT NULL REFERENCES app_user(username) ON DELETE CASCADE,
                state       JSONB NOT NULL,           -- see session_state()
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def session_state(session: Session) -> JSON:
    """What is kept of a session between requests. Signals belong to one request, so they are not."""
    return {
        "products": session.products,
        "searches": [{"label": s.label, "request": s.request, "stores": [m.id for m in s.stores], "why": s.why,
                      "next_cursors": s.next_cursors, "found": s.found} for s in session.searches],
        "detail": session.detail,
        "carts": session.carts,
        "checkout": session.checkout,
        "checkout_store": session.checkout_store,
        "buyer": session.buyer,
        "address": session.address,
        "delivery_option": session.delivery_option,
        "history": session.history,
    }


# -------------------------------------------------------------------- shop


class Shop:
    def __init__(self, registry: Registry, call_tool: CallTool):
        self.registry = registry
        self.call_tool = call_tool  # (merchant, tool, tool arguments)

    def _market_context(self, username: str) -> JSON:
        """Build a UCP context dict from this user's persisted market preferences.

        Only non-None profile values are included. The fields map to the official
        UCP ContextCreateRequest schema (context_create_request.py in the SDK).
        Verified supported fields: currency, address_country, language,
        address_region, postal_code.
        """
        from profile_store import (
            get_profile,  # local import avoids circular deps at module load
        )
        market = get_profile(username)["market"]
        ctx: JSON = {}
        if market.get("preferred_currency"):
            ctx["currency"] = market["preferred_currency"]
        if market.get("country"):
            ctx["address_country"] = market["country"]
        if market.get("language"):
            ctx["language"] = market["language"]
        if market.get("region"):
            ctx["address_region"] = market["region"]
        if market.get("postal_code"):
            ctx["postal_code"] = market["postal_code"]
        return ctx

    def _profile_buyer(self, username: str) -> JSON:
        """Buyer fields from this user's profile, mapped to UCP checkout buyer keys.

        phone_number is the UCP field name (BuyerCreateRequest); the profile stores it
        as 'phone'. Used only for pre-filling a new checkout session, not for catalog.
        """
        from profile_store import get_profile
        buyer = get_profile(username)["buyer"]
        result: JSON = {}
        if buyer.get("first_name"):
            result["first_name"] = buyer["first_name"]
        if buyer.get("last_name"):
            result["last_name"] = buyer["last_name"]
        if buyer.get("email"):
            result["email"] = buyer["email"]
        if buyer.get("phone"):
            result["phone_number"] = buyer["phone"]  # UCP checkout uses phone_number
        return result

    def session(self, session_id: str | None, username: str) -> Session:
        """This user's session with that id, or a new one.

        Sessions are read from the shop_session table, so any worker or dyno can carry
        on a chat and a restart loses nothing. Carts and an open checkout live on the
        session, so it is never shared: an id belonging to another user simply starts
        a fresh session here. Call save() once the request is done.
        """
        if session_id:
            with db.connect() as conn:
                row = conn.execute("SELECT state FROM shop_session WHERE id = %s AND username = %s",
                                   (session_id, username)).fetchone()
            if row:
                return self._restore(session_id, username, row["state"])
        session = Session(id=str(uuid.uuid4()), username=username)
        with db.connect() as conn:
            conn.execute("DELETE FROM shop_session WHERE updated_at < now() - make_interval(days => %s)",
                         (SESSION_DAYS,))
        self.save(session)
        return session

    @staticmethod
    def save(session: Session) -> None:
        """Store the session for this user's next request (whichever worker it reaches)."""
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO shop_session (id, username, state) VALUES (%s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()
                   WHERE shop_session.username = EXCLUDED.username""",
                (session.id, session.username, Jsonb(session_state(session))),
            )

    def _restore(self, session_id: str, username: str, state: JSON) -> Session:
        """A session as session_state() stored it."""
        searches = [
            Search(s["label"], s["request"], [m for store_id in s["stores"] if (m := self.registry.get(store_id))],
                   s["why"], s["next_cursors"], s["found"])
            for s in items(state.get("searches"))
        ]
        return Session(id=session_id, username=username, products=items(state.get("products")),
                       searches=searches, detail=state.get("detail"), carts=fields(state.get("carts")),
                       checkout=state.get("checkout"), checkout_store=state.get("checkout_store"),
                       buyer=fields(state.get("buyer")), address=fields(state.get("address")),
                       delivery_option=state.get("delivery_option"), history=items(state.get("history")))

    def store_of(self, product: JSON) -> Merchant:
        return self.registry.require(product[STORE_KEY])

    async def capabilities(self, store_id: str | None) -> Blocks:
        chosen = self.registry.get(store_id)
        merchants = [chosen] if chosen else self.registry.all()
        blocks: Blocks = []
        for merchant in merchants:
            if merchant.error:
                blocks.append(notice("warning", f"{merchant.name} is unavailable: {merchant.error}"))
            else:
                blocks.append(text(capabilities_summary(merchant, await merchant.profile())))
        return blocks

    # ------------------------------------------------------------- catalog

    def stores_for(self, categories: list[str] | None) -> tuple[list[Merchant], str]:
        """The stores to search for these categories, and a note saying why, for the model."""
        searchable = [m for m in self.registry.all() if m.supports(SEARCH)]
        wanted = [c for c in items(categories) if c in self.registry.categories][:MAX_CATEGORIES]
        if not wanted:
            return searchable, "No category was given, so every store was searched."
        selling = self.registry.selling(wanted)
        chosen = [m for m in selling if m.supports(SEARCH)]
        down = [m.name for m in selling if not m.supports(SEARCH)]
        note = f" ({', '.join(down)} also sells this but can't be searched right now.)" if down else ""
        if not chosen:
            return searchable, f"No available store sells {', '.join(wanted)}, so every store was searched.{note}"
        return chosen, f"Searched {', '.join(m.name for m in chosen)} for {', '.join(wanted)}.{note}"

    async def search(self, session: Session, searches: list[JSON], add: bool = False) -> Blocks:
        """
        Run one to MAX_SEARCHES searches together, each {query, categories, low, high}.
        Several searches (e.g. shoes and a dining table) are shown side by side.

        add=True keeps the searches already shown in this chat turn: when the model asks
        for "shoes and a couch" as two calls instead of one, the couch results join the
        shoes instead of replacing them. Earlier searches are not sent to the stores again.
        """
        ctx = self._market_context(session.username)
        new = [self._build_search(wanted, ctx) for wanted in searches[:MAX_SEARCHES]]
        earlier = self._kept_searches(session, new) if add else []
        session.searches = earlier + new
        return await self.run_searches(session, new, earlier=earlier)

    def _build_search(self, wanted: JSON, ctx: JSON) -> Search:
        """One search {query, categories, low, high} as a UCP catalog request and the stores to send it to."""
        stores, why = self.stores_for(wanted["categories"])
        logger.info(f"Categories {wanted['categories']} -> {[m.id for m in stores]}: {why}")
        request: JSON = {}
        if ctx:
            request["context"] = ctx
        if wanted["query"]:
            request["query"] = wanted["query"]
        price = {k: v for k, v in (("min", wanted["low"]), ("max", wanted["high"])) if v is not None}
        if price:
            request["filters"] = {"price": price}
        label = wanted["query"] or ", ".join(items(wanted["categories"])) or "Results"
        return Search(label, request, stores, why)

    @staticmethod
    def _kept_searches(session: Session, new: list[Search]) -> list[Search]:
        """Earlier searches that found something and aren't being asked again."""
        labels = {s.label.lower() for s in new}
        return [s for s in session.searches if s.found and s.label.lower() not in labels]

    async def more(self, session: Session) -> Blocks:
        if not session.has_more():
            return [text("There are no more results for the last search.")]
        searches = [s for s in session.searches if s.next_cursors]
        cursors = [s.next_cursors for s in searches]
        for search in searches:
            search.stores = [self.registry.require(store_id) for store_id in search.next_cursors]
            search.why = f"Next page from {', '.join(m.name for m in search.stores)}."
            search.next_cursors = {}
        return await self.run_searches(session, searches, cursors)

    async def run_searches(self, session: Session, searches: list[Search], cursors: list[dict[str, str]] | None = None,
                           earlier: list[Search] | None = None) -> Blocks:
        """Send every search to its stores at once, then show the results (after any earlier ones)."""
        calls: list[Awaitable[JSON]] = []
        for i, search in enumerate(searches):
            limit = PAGE_SIZE if len(search.stores) == 1 else MIN_PER_STORE
            for m in search.stores:
                cursor = cursors[i].get(m.id) if cursors else None
                calls.append(self.search_store(m, search.request, limit, cursor, signals=session.signals or None))
        results = iter(await asyncio.gather(*calls, return_exceptions=True))
        return self.show_results(session, [(s, [(m, next(results)) for m in s.stores]) for s in searches], earlier)

    async def search_store(self, merchant: Merchant, request: JSON, limit: int,
                           cursor: str | None = None, signals: JSON | None = None) -> JSON:
        catalog: JSON = {**request, "pagination": {"limit": limit, **({"cursor": cursor} if cursor else {})}}
        if signals:
            catalog["signals"] = signals
        response = await self.call_tool(merchant, "search_catalog", {"catalog": catalog})
        query = request.get("query")
        # Nothing found for "lamps"? Try "lamp".
        if not cursor and not response.get("products") and query and (simpler := " ".join(map(singular, query.split()))) != query:
            catalog_retry: JSON = {**catalog, "query": simpler}
            response = await self.call_tool(merchant, "search_catalog", {"catalog": catalog_retry})
        return response

    def search_results(self, search: Search, results: list[tuple[Merchant, JSON | BaseException]]
                       ) -> tuple[list[JSON], str, Blocks]:
        """One search's products, a summary for the model, and the stores' notices."""
        price = fields(search.request.get("filters")).get("price")
        # Use the currency from the request context for price comparison (not a hardcoded default).
        currency = fields(search.request.get("context")).get("currency")
        products: list[JSON] = []
        counts: list[str] = []
        problems: Blocks = []
        for merchant, response in results:
            if isinstance(response, BaseException):
                problems.append(notice("warning", f"{merchant.name} couldn't be searched: {response}"))
                continue
            found = within_price(items(response.get("products")), price, currency)
            for product in found:
                product[STORE_KEY] = merchant.id
            products += found
            counts.append(f"{merchant.name} {len(found)}")
            pagination = fields(response.get("pagination"))
            if pagination.get("has_next_page") and pagination.get("cursor"):
                search.next_cursors[merchant.id] = pagination["cursor"]
            problems += notices(response.get("messages"))
        return products, f"{search.why} Results per store: {', '.join(counts) or 'none'}.", problems

    def show_results(self, session: Session,
                     searches: list[tuple[Search, list[tuple[Merchant, JSON | BaseException]]]],
                     earlier: list[Search] | None = None) -> Blocks:
        groups, summary, problems = self._collect_results(searches, earlier)
        products = [p for _, found in groups for p in found]
        if not products:
            more = " More pages are available." if session.has_more() else ""
            return [text(f"No products matched. {summary}{more}"), *problems]
        session.products = products
        cards: JSON = {"type": "products", "products": [product_card(p, self.store_of(p).name) for p in products]}
        if len(groups) > 1:
            # Different things asked for at once: the frontend shows each in its own column.
            cards["groups"] = self._grouped_cards(groups)
        return [text(f"Found {len(products)} products. {summary}"), cards, *problems]

    def _collect_results(self, searches: list[tuple[Search, list[tuple[Merchant, JSON | BaseException]]]],
                         earlier: list[Search] | None) -> tuple[list[tuple[Search, list[JSON]]], str, Blocks]:
        """Each search with its products (earlier ones first), one summary for the model, and the notices."""
        groups = [(search, search.found) for search in earlier or ()]
        summaries: list[str] = []
        problems: Blocks = []
        for search, results in searches:
            found, summary, found_problems = self.search_results(search, results)
            search.found = found
            groups.append((search, found))
            label = f'"{search.label}": {len(found)} products. ' if len(groups) > 1 else ""
            summaries.append(label + summary)
            problems += found_problems
        return groups, " ".join(summaries), problems

    def _grouped_cards(self, groups: list[tuple[Search, list[JSON]]]) -> list[JSON]:
        """One column per search, for when different things were asked for at once."""
        return [
            {"title": search.label,
             "stores": [m.name for m in search.stores],
             "products": [product_card(p, self.store_of(p).name) for p in found]}
            for search, found in groups
        ]

    async def detail(self, session: Session, product_id: str, option_label: str | None = None) -> Blocks:
        known = self.find_product(session, product_id)
        if not known:
            return [notice("warning", "Search for the product first, then ask for its details.")]
        merchant = self.store_of(known)
        ctx = self._market_context(session.username)
        request: JSON = {"id": product_id}
        if ctx:
            request["context"] = ctx
        if option_label:
            # UCP get_product selects an option by its name and label, e.g. Color: Navy.
            option = next((o["name"] for o in items(known.get("options"))
                           for v in items(o.get("values")) if v["label"].lower() == option_label.lower()), None)
            if not option:
                return [text(f'"{option_label}" is not a known option for this product.')]
            request["selected"] = [{"name": option, "label": option_label}]

        catalog: JSON = {**request}
        if session.signals:
            catalog["signals"] = session.signals
        response = await self.call_tool(merchant, "get_product", {"catalog": catalog})
        product = response.get("product")
        if not product:
            return notices(response.get("messages")) or [text("That product couldn't be loaded.")]
        product[STORE_KEY] = merchant.id
        session.detail = product
        return [product_detail(product, merchant.name), *notices(response.get("messages"))]

    # ---------------------------------------------------------------- cart

    def find_product(self, session: Session, product_id: str) -> JSON | None:
        # The detail view has the selected variant first, so prefer it.
        candidates: list[JSON | None] = [session.detail, *session.products]
        return next((p for p in candidates if p and p["id"] == product_id), None)

    @staticmethod
    def cart_lines(session: Session, store_id: str) -> dict[str, int]:
        """A store's cart as {variant id: quantity}."""
        cart = fields(session.carts.get(store_id))
        return {line["item"]["id"]: line["quantity"] for line in items(cart.get("line_items"))}

    async def add_to_cart(self, session: Session, product_id: str, variant_id: str | None, quantity: int) -> Blocks:
        product = self.find_product(session, product_id)
        if not product:
            return [notice("warning", "Show the product first, then add it.")]
        merchant = self.store_of(product)
        if not merchant.supports(CART):
            where = f" It can be bought on the store's site: {product['url']}" if product.get("url") else ""
            return [notice("warning", f"{merchant.name} doesn't offer a cart over UCP, so this can't be added here.{where}")]
        variants = items(product.get("variants"))
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
        merchant = self.registry.require(store_id)
        return await self.save_cart(session, merchant, lines, f"{merchant.name} cart updated.")

    async def empty_carts(self, session: Session) -> Blocks:
        if not session.carts:
            return [text(CART_EMPTY), cart_block(None)]
        blocks: Blocks = []
        store_ids = [*session.carts]  # a copy: save_cart removes emptied carts from session.carts
        for store_id in store_ids:
            blocks += await self.save_cart(session, self.registry.require(store_id), {}, "")
        return [text("All carts are now empty."), *[b for b in blocks if b["type"] != "text"]]

    async def save_cart(self, session: Session, merchant: Merchant, lines: dict[str, int], message: str) -> Blocks:
        """
        Create the store's cart, or replace its contents. UCP update_cart is a full
        replacement, so every line (and the context) is sent each time.
        Market context is read from the persisted profile on every cart save so that
        preference changes take effect on the next cart operation.
        """
        line_items: list[JSON] = [{"item": {"id": variant_id}, "quantity": qty} for variant_id, qty in lines.items()]
        ctx = self._market_context(session.username)
        cart: JSON = {"line_items": line_items}
        if ctx:
            cart["context"] = ctx
        if session.signals:
            cart["signals"] = session.signals
        if session.checkout_store == merchant.id:
            # An open checkout no longer matches the cart; Checkout starts a new one.
            session.checkout = session.checkout_store = None

        response: JSON | None = None
        existing = session.carts.get(merchant.id)
        if existing:
            response = await self.call_tool(merchant, "update_cart", {"id": existing["id"], "cart": cart})
            if is_not_found(response):  # expired or cancelled on the store's side; start a new cart
                session.carts.pop(merchant.id)
                existing = response = None
        if response is None:
            if not line_items:
                return [text(f"The {merchant.name} cart is empty."), cart_block(None, merchant)]
            response = await self.call_tool(merchant, "create_cart", {"cart": cart})

        if is_error(response) or not response.get("id"):
            return notices(response.get("messages")) or [notice("error", f"{merchant.name} couldn't update the cart.")]
        if line_items:
            session.carts[merchant.id] = response
        else:
            session.carts.pop(merchant.id, None)
        return [text(message), cart_block(response, merchant), *notices(response.get("messages"))]

    async def view_cart(self, session: Session) -> Blocks:
        if not session.carts:
            return [text(CART_EMPTY), cart_block(None)]

        blocks: Blocks = []
        carts = [*session.carts.items()]  # a copy: expired carts are removed from session.carts below
        for store_id, cart in carts:
            merchant = self.registry.require(store_id)
            response = await self.call_tool(merchant, "get_cart", {"id": cart["id"]})
            if is_not_found(response):
                session.carts.pop(store_id)
                blocks.append(text(f"The {merchant.name} cart has expired, so it's empty now."))
            elif is_error(response):
                blocks += notices(response.get("messages")) or [notice("error", f"{merchant.name} couldn't load the cart.")]
            else:
                session.carts[store_id] = response
                blocks += [text(f"{merchant.name} cart:"), cart_block(response, merchant),
                           *notices(response.get("messages"))]
        return blocks or [text(CART_EMPTY), cart_block(None)]

    # ------------------------------------------------------------ checkout

    async def start_checkout(self, session: Session, store_id: str | None) -> Blocks:
        store_id, refusal = self._checkout_store(session, store_id)
        if store_id is None:  # no store id comes back exactly when there is a refusal
            return refusal or []
        merchant = self.registry.require(store_id)

        # Pre-fill buyer details from the persisted profile if the session has none yet.
        # This spares the shopper from re-entering info they already saved.
        if not session.buyer and (profile_buyer := self._profile_buyer(session.username)):
            session.buyer.update(profile_buyer)

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

    def _checkout_store(self, session: Session, store_id: str | None) -> tuple[str | None, Blocks | None]:
        """The store to check out at, or the reply explaining why a checkout can't start."""
        with_items = [s for s, cart in session.carts.items() if cart_count(cart)]
        if not with_items:
            return None, [text("The cart is empty, so there is nothing to check out.")]
        if not store_id and len(with_items) > 1:
            names = " and ".join(self.registry.require(s).name for s in with_items)
            return None, [text(f"There are carts at {names}. Each store checks out separately: "
                               "ask the shopper which store to check out first.")]
        store_id = store_id or with_items[0]
        if store_id not in with_items:
            unknown = self.registry.get(store_id)
            return None, [text(f"There's nothing in the {unknown.name if unknown else store_id} cart to check out.")]
        if session.checkout and session.checkout_store != store_id:
            open_at = self.registry.require(session.checkout_store).name
            return None, [text(f"A checkout is already open at {open_at}. "
                               "Finish or cancel it before starting another.")]
        merchant = self.registry.require(store_id)
        if not merchant.supports(CHECKOUT):
            return None, [notice("warning", f"{merchant.name} doesn't offer checkout over UCP.")]
        return store_id, None

    def checkout_merchant(self, session: Session) -> Merchant:
        return self.registry.require(session.checkout_store)

    async def view_checkout(self, session: Session) -> Blocks:
        if not session.checkout:
            return [text("There's no checkout open.")]
        response = await self.call_tool(self.checkout_merchant(session), "get_checkout", {"id": session.checkout["id"]})
        return self.checkout_reply(session, response)

    async def update_checkout(self, session: Session, details: JSON) -> Blocks:
        """
        Save buyer and shipping details given as: email, phone, first_name, last_name,
        street_address, city, region, postal_code, country (any may be missing).
        The address can be in any country; the store decides whether it delivers there.
        """
        if not session.checkout:
            return [notice("warning", START_CHECKOUT_FIRST)]
        for name in ("email", "first_name", "last_name"):
            if details.get(name):
                session.buyer[name] = details[name].strip()
        address = given_address(details)
        if address:
            session.address = address
        if details.get("phone"):
            session.buyer["phone_number"] = e164(details["phone"], session.address.get("address_country", "US"))
        return await self.send_checkout(session)

    async def choose_delivery(self, session: Session, option_id: str) -> Blocks:
        if not session.checkout:
            return [notice("warning", START_CHECKOUT_FIRST)]
        session.delivery_option = option_id
        return await self.send_checkout(session)

    async def send_checkout(self, session: Session) -> Blocks:
        if not session.checkout:  # callers check this first
            return [notice("warning", START_CHECKOUT_FIRST)]
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

    def checkout_payload(self, session: Session) -> JSON:
        """The whole checkout, as UCP update_checkout expects (it replaces the previous state).

        Market context is read from the persisted profile. If the shopper has provided
        a shipping address, its country overrides the profile country for this context,
        since an authoritative address takes precedence over a preference hint.
        Signals (IP, User-Agent) are included when available.
        """
        checkout = fields(session.checkout)
        lines = items(checkout.get("line_items"))
        ctx = self._market_context(session.username)
        # A confirmed shipping address country supersedes the profile preference hint.
        if session.address:
            ctx["address_country"] = session.address["address_country"]
        payload: JSON = {
            "line_items": [{"item": {"id": line["item"]["id"]}, "quantity": line["quantity"]} for line in lines],
        }
        if ctx:
            payload["context"] = ctx
        if session.signals:
            payload["signals"] = session.signals
        if session.buyer:
            payload["buyer"] = session.buyer
        if session.address and not missing_recipient(session):
            # The store needs the recipient's name and phone with the address, and keeps the
            # first destination it was given, so the destination is sent once it is complete.
            recipient = {name: session.buyer[name] for name in RECIPIENT_FIELDS}
            method: JSON = {
                "type": "shipping",
                "line_item_ids": [line["id"] for line in lines],
                "destinations": [{**session.address, **recipient}],
            }
            group = delivery_group(checkout)
            if session.delivery_option and group.get("id"):
                method["groups"] = [{"id": group["id"], "selected_option_id": session.delivery_option}]
            payload["fulfillment"] = {"methods": [method]}
        return payload

    def checkout_reply(self, session: Session, response: JSON) -> Blocks:
        """Show the checkout and say what the store still needs."""
        merchant = self.checkout_merchant(session)
        failed = self._checkout_failed(session, response, merchant)
        if failed:
            return failed

        session.checkout = response
        # UCP: "recoverable" errors are fixed here with update_checkout first; then the shopper
        # continues on the store's own page (continue_url) to pay and place the order. That page
        # also handles anything only the buyer can do (requires_buyer_input / review).
        # The address is held back until the recipient is complete (see checkout_payload).
        address_sent = bool(session.address) and not missing_recipient(session)
        messages = checkout_messages(response, address_sent, merchant.name)
        needed = self._still_needed(session, messages)
        store_url = None if needed else store_checkout_url(response)
        card = checkout_block(response, {**session.buyer, **session.address}, merchant.name, address_sent,
                              continue_url=store_url)
        return [card, self._next_step(merchant, needed, store_url, messages)]

    @staticmethod
    def _checkout_failed(session: Session, response: JSON, merchant: Merchant) -> Blocks | None:
        """The reply when the store gave no usable checkout, or None when it did."""
        if is_not_found(response):
            session.checkout = session.checkout_store = None
            return [text("That checkout has expired. Start the checkout again.")]
        if response.get("id"):
            return None
        if not session.checkout:
            session.checkout_store = None
        return notices(response.get("messages")) or [notice("error", f"{merchant.name} couldn't open a checkout.")]

    @staticmethod
    def _still_needed(session: Session, messages: list[JSON]) -> list[str]:
        """What the shopper must still give here before moving on to the store's page."""
        needed = [m["content"] for m in messages if m["type"] == "error" and m["severity"] == "recoverable"]
        if not session.address:
            return [*needed, "Shipping address"]
        return needed + [f"Recipient {name.replace('_', ' ')}" for name in missing_recipient(session)]

    @staticmethod
    def _next_step(merchant: Merchant, needed: list[str], store_url: str | None, messages: list[JSON]) -> JSON:
        """The line under the checkout card: what is missing, or where to go next."""
        if needed:
            return text(f"{merchant.name} still needs: " + "; ".join(needed))
        if not store_url:
            return text(f"The {merchant.name} checkout details are complete, but {merchant.name} gave "
                        "no checkout page to continue on, so the order can't be finished from here.")
        # Hand-off to the store's own payment page (UCP continue_url). The store's own wording for
        # any step left to the shopper is already listed on the checkout card, so it isn't repeated here.
        extra_step = any(m["severity"] in BUYER_ONLY for m in messages)
        extra = f" {merchant.name} will ask you to complete one more step there." if extra_step else ""
        return text(f"Your details are complete. [Continue to payment on {merchant.name}]({store_url}) "
                    f"to pay and place your order.{extra}")
