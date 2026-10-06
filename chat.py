"""Chat for the shopping frontend, built on the UCP catalog, cart and checkout tools.

The rules are simple and deterministic (no LLM):

    "hi" / "help"                        -> short help text
    "what can the store do?"             -> the store's UCP capabilities
    "table lamps under $150"             -> search_catalog with a price filter
    "show more"                          -> search_catalog with the next cursor
    "tell me about the second one"       -> get_product on the last results
    "in Sable"                           -> get_product with that option selected
    "add 2 of the <product>"             -> create_cart, or update_cart if there is one
    "make the <cart item> 3"             -> update_cart with the new quantity
    "remove the <cart item>"             -> update_cart without that line
    "what's in my cart?"                 -> get_cart
    "checkout"                           -> create_checkout from the cart
    "my email is ..." / "my phone is ..." /
    "my name is ..." / "ship to <address>" -> update_checkout with those details
    "Standard"                           -> update_checkout selecting that delivery option
    "cancel checkout"                    -> cancel_checkout

Payment is not enabled: checkout stops at reviewing the details, and no order is placed.

Each reply is a list of blocks (text, notice, products, product, capabilities,
cart, checkout) in the shape the frontend expects (see ucp-frontend/src/lib/types.ts).
"""

import html
import re
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

PAGE_SIZE = 8
MAX_SESSIONS = 500

EMPTY_STATE = {"cart_count": 0, "cart_subtotal": 0, "checkout_id": None, "order_id": None}

GREETING_RE = re.compile(r"^\s*(hi|hello|hey|help|start|what can you do)\b[\s!?.]*$", re.I)
CAPABILITIES_RE = re.compile(r"\b(capabilit\w*|what can (the |this )?(merchant|store|shop) do|discovery)\b", re.I)
ADD_RE = re.compile(r"^\s*add (?:(?P<qty>\d+) of )?the (?P<title>.+?)(?: in (?P<label>.+?))?\s*$", re.I)
SET_QTY_RE = re.compile(r"^\s*make the (?P<title>.+?) (?P<qty>\d+)\s*$", re.I)
REMOVE_RE = re.compile(r"^\s*remove (?:the )?(?P<title>.+?)(?: from (?:my |the )?cart)?\s*$", re.I)
EMPTY_CART_RE = re.compile(r"^\s*(empty|clear) (my |the )?cart\b", re.I)
VIEW_CART_RE = re.compile(r"\b(what'?s in my cart|(show|view|see|open) (me )?(my |the )?cart|my cart)\b", re.I)
CANCEL_CHECKOUT_RE = re.compile(r"^\s*cancel (the |my )?checkout\b", re.I)
VIEW_CHECKOUT_RE = re.compile(r"\b((show|view|see) (me )?(my |the )?checkout|checkout status)\b", re.I)
START_CHECKOUT_RE = re.compile(r"^\s*(proceed to |go to |start )?(checkout|check out)\s*[.!]?\s*$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RE = re.compile(r"\bphone(?: number)?(?: is)?\s*:?\s*(?P<phone>\+?\(?\d[\d\s().-]{6,}\d)", re.I)
NAME_RE = re.compile(r"\bmy name is (?P<first>[A-Za-z'-]+)(?: (?!and\b)(?P<last>[A-Za-z'-]+))?", re.I)
ADDRESS_RE = re.compile(r"^\s*(?:ship|deliver|send)(?: it)? to (?P<address>.+?)\s*[.!]?\s*$", re.I)
US_ADDRESS_RE = re.compile(r"^(?P<street>[^,]+),\s*(?P<city>[^,]+),\s*(?P<region>[A-Za-z]{2})\s+(?P<postal>\d{5}(?:-\d{4})?)$")
COMMERCE_RE = re.compile(r"\b(checkout|check out|pay|payment|order)\b", re.I)
MORE_RE = re.compile(r"^\s*(show |load |see )?(more|next( page)?)\s*[.!]?\s*$", re.I)
OPTION_RE = re.compile(r"^\s*in (?P<label>.{1,40}?)\s*$", re.I)
DETAIL_RE = re.compile(
    r"^\s*(tell me (more )?about|more about|details? (of|for|on|about)|describe)\s+(the\s+)?(?P<target>.+?)\s*[?.!]?\s*$",
    re.I,
)

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "last": -1}

NUMBER = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?"
PRICE_BETWEEN_RE = re.compile(rf"\bbetween\s+{NUMBER}\s+(?:and|to|-)\s+{NUMBER}", re.I)
PRICE_MAX_RE = re.compile(rf"\b(?:under|below|less than|up to|within|max|cheaper than)\s+{NUMBER}", re.I)
PRICE_MIN_RE = re.compile(rf"\b(?:over|above|more than|at least|min|from)\s+{NUMBER}", re.I)

FILLER = {"show", "me", "find", "search", "for", "i", "want", "need", "looking", "look", "get", "some", "any",
          "a", "an", "the", "please", "products", "product", "items", "item", "with", "price", "and", "or",
          "of", "in", "on", "to", "is", "are", "you", "have", "do", "can", "buy", "all", "list", "what"}

CallTool = Callable[[str, dict], Awaitable[dict]]
FetchProfile = Callable[[], Awaitable[dict]]


@dataclass
class Session:
    id: str
    products: list[dict] = field(default_factory=list)  # last products shown, in display order
    last_search: dict | None = None
    next_cursor: str | None = None
    detail: dict | None = None  # last product shown in detail
    cart: dict | None = None  # the store's last cart response
    checkout: dict | None = None  # the store's last checkout response
    # What the shopper told us for checkout. UCP update_checkout is a full
    # replacement, so these are sent again with every update.
    buyer: dict = field(default_factory=dict)
    address: dict = field(default_factory=dict)
    delivery_option: str | None = None


# ------------------------------------------------------------------ blocks


def text(markdown: str) -> dict:
    return {"type": "text", "text": markdown}


def notice(tone: str, message: str) -> dict:
    return {"type": "notice", "tone": tone, "text": message}


def notices(messages: list[dict] | None) -> list[dict]:
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


def product_card(product: dict) -> dict:
    variants = product.get("variants") or []
    featured = variants[0]
    many = len(variants) > 1
    price_range = product.get("price_range") or {}
    card = {
        "id": product["id"],
        "title": product["title"],
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


def product_detail(product: dict) -> dict:
    detail = product_card(product)
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


# Hidden from the capabilities card for now. Empty this set to show them again.
HIDDEN_CAPABILITIES = {
    "dev.ucp.shopping.checkout",
    "dev.ucp.shopping.discount",
    "dev.ucp.shopping.fulfillment",
    "dev.ucp.shopping.order",
    "dev.ucp.common.identity_linking",
}


def capabilities_block(store_name: str, profile: dict) -> dict:
    ucp = profile.get("ucp") or {}
    return {
        "type": "capabilities",
        "version": ucp.get("version"),
        "business": {"name": store_name},
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


def total_of(totals: list[dict] | None, kind: str) -> int:
    return next((t["amount"] for t in totals or [] if t.get("type") == kind), 0)


def cart_block(cart: dict | None) -> dict:
    """A UCP cart as the frontend's cart card (an empty card when there is no cart)."""
    cart = cart or {}
    lines = [
        {
            "variant_id": line["item"]["id"],
            "title": line["item"].get("title", ""),
            "image": line["item"].get("image_url"),
            "price": line["item"].get("price", 0),
            "quantity": line["quantity"],
            "line_total": total_of(line.get("totals"), "total") or line["item"].get("price", 0) * line["quantity"],
        }
        for line in cart.get("line_items") or []
    ]
    return {
        "type": "cart",
        "currency": cart.get("currency", "USD"),
        "subtotal": total_of(cart.get("totals"), "subtotal"),
        "lines": lines,
    }


def cart_state(cart: dict | None, checkout: dict | None = None) -> dict:
    """The cart summary the frontend shows in the top bar and sidebar."""
    cart = cart or {}
    return {
        **EMPTY_STATE,
        "cart_count": sum(line["quantity"] for line in cart.get("line_items") or []),
        "cart_subtotal": total_of(cart.get("totals"), "subtotal"),
        "checkout_id": (checkout or {}).get("id"),
    }


def shipping_method(checkout: dict) -> dict:
    return next((m for m in (checkout.get("fulfillment") or {}).get("methods") or [] if m.get("type") == "shipping"), {})


def checkout_block(checkout: dict) -> dict:
    """A UCP checkout as the frontend's checkout card."""
    method = shipping_method(checkout)
    destination = next((d for d in method.get("destinations") or [] if d.get("id") == method.get("selected_destination_id")),
                       (method.get("destinations") or [{}])[0])
    group = (method.get("groups") or [{}])[0]
    return {
        "type": "checkout",
        "checkout": {
            "id": checkout["id"],
            "status": checkout.get("status", ""),
            "currency": checkout.get("currency", "USD"),
            "line_items": [
                {
                    "id": line["id"],
                    "title": line["item"].get("title", ""),
                    "image": line["item"].get("image_url"),
                    "price": line["item"].get("price", 0),
                    "quantity": line["quantity"],
                    "line_total": total_of(line.get("totals"), "total") or line["item"].get("price", 0) * line["quantity"],
                }
                for line in checkout.get("line_items") or []
            ],
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
            "messages": [{"type": m.get("type", "info"), "code": m.get("code"), "content": m.get("content", "")}
                         for m in checkout.get("messages") or []],
            "links": checkout.get("links") or [],
            # Payment is switched off for now, so the store's payment page link isn't passed on.
            # "continue_url": checkout.get("continue_url"),
        },
    }


def parse_address(text: str) -> dict | None:
    """'123 Main St, Springfield, IL 62701' -> UCP postal address fields (US addresses)."""
    match = US_ADDRESS_RE.match(text.strip())
    if not match:
        return None
    return {
        "street_address": match["street"].strip(),
        "address_locality": match["city"].strip(),
        "address_region": match["region"].upper(),
        "postal_code": match["postal"],
        "address_country": "US",
    }


# ----------------------------------------------------------------- parsing


def parse_price(message: str) -> tuple[int | None, int | None, str]:
    """Pull a price range (in cents) out of the message and return the rest of it."""

    def cents(number: str, thousands: str | None) -> int:
        return round(float(number.replace(",", "")) * (1000 if thousands else 1) * 100)

    low = high = None
    if match := PRICE_BETWEEN_RE.search(message):
        low, high = cents(match[1], match[2]), cents(match[3], match[4])
        message = message.replace(match[0], " ")
    if match := PRICE_MAX_RE.search(message):
        high = cents(match[1], match[2])
        message = message.replace(match[0], " ")
    if match := PRICE_MIN_RE.search(message):
        low = cents(match[1], match[2])
        message = message.replace(match[0], " ")
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high, message


def keywords(message: str) -> list[str]:
    words = re.findall(r"[\w&+-]+", message.lower())
    return [w for w in words if w not in FILLER and not w.isdigit()]


def ordinal(message: str) -> int | None:
    for word, position in ORDINALS.items():
        if re.search(rf"\b{word}\b", message.lower()):
            return position
    return None


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def within_price(products: list[dict], price: dict | None) -> list[dict]:
    """The store's price filter is loose, so check each product's lowest price again."""
    if not price:
        return products
    low, high = price.get("min", 0), price.get("max", float("inf"))
    return [p for p in products if low <= ((p.get("price_range") or {}).get("min") or {}).get("amount", 0) <= high]


def dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}"


# -------------------------------------------------------------------- chat


class Chat:
    def __init__(self, call_tool: CallTool, call_store_tool: CallTool, fetch_store_profile: FetchProfile,
                 store_name: str, currency: str, country: str):
        self.call_tool = call_tool  # catalog tools: (tool, catalog parameters)
        self.call_store_tool = call_store_tool  # cart and checkout tools: (tool, tool arguments)
        self.fetch_store_profile = fetch_store_profile
        self.store_name = store_name
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

    async def reply(self, message: str, session_id: str | None) -> dict:
        session = self.session(session_id)
        try:
            blocks, suggestions = await self.route(message.strip(), session)
        except Exception as exc:
            if "rate limit" in str(exc).lower():
                blocks = [notice("warning", "The store is getting too many requests right now. "
                                            "Wait a few seconds and try again.")]
            else:
                blocks = [notice("error", f"The store could not be reached: {exc}")]
            suggestions = ["Try again"]
        return {"session_id": session.id, "blocks": blocks, "suggestions": suggestions,
                "state": cart_state(session.cart, session.checkout)}

    async def route(self, message: str, session: Session) -> tuple[list[dict], list[str]]:
        if not message or GREETING_RE.match(message):
            return self.help(), ["Table lamps under $150", "What can the store do?"]

        if CAPABILITIES_RE.search(message):
            profile = await self.fetch_store_profile()
            intro = text(f"**{self.store_name}** publishes these UCP capabilities:")
            return [intro, capabilities_block(self.store_name, profile)], ["Show me floor lamps", "Quilt sets"]

        if match := ADD_RE.match(message):
            return await self.add_to_cart(session, match["title"], match["label"], int(match["qty"] or 1))

        if match := SET_QTY_RE.match(message):
            return await self.set_quantity(session, match["title"], int(match["qty"]))

        if EMPTY_CART_RE.match(message):
            return await self.save_cart(session, [], "Your cart is now empty.")

        if match := REMOVE_RE.match(message):
            return await self.set_quantity(session, match["title"], 0)

        if VIEW_CART_RE.search(message):
            return await self.view_cart(session)

        if START_CHECKOUT_RE.match(message):
            return await self.start_checkout(session)

        if CANCEL_CHECKOUT_RE.match(message):
            return await self.cancel_checkout(session)

        if session.checkout:
            if VIEW_CHECKOUT_RE.search(message):
                return await self.view_checkout(session)
            if (match := ADDRESS_RE.match(message)) and not parse_address(match["address"]):
                return [text("Please give the address as *street, city, state ZIP*, "
                             "for example *ship to 123 Main St, Springfield, IL 62701*.")], []
            if details := self.checkout_details(session, message):
                return await self.update_checkout(session, details)
            if option := self.find_delivery_option(session, message):
                session.delivery_option = option["id"]
                return await self.update_checkout(session, f"delivery: {option.get('title')}")

        if COMMERCE_RE.search(message):
            # "Pay by ...", "Place the order": payment is switched off for now.
            if session.checkout:
                return [notice("info", "Payment isn't enabled in this assistant yet. "
                                       "Checkout stops at reviewing your details.")], ["Show my checkout"]
            # Hand-off to the store's own payment page (UCP continue_url); uncomment to bring it back.
            # if session.checkout and session.checkout.get("continue_url"):
            #     return [text(f"Payment and placing the order happen on {self.store_name}'s own checkout page: "
            #                  f"[Continue to payment on {self.store_name}]({session.checkout['continue_url']})")], []
            return [notice("info", "Add something to your cart first, then choose Checkout.")], ["What's in my cart?"]

        if MORE_RE.match(message):
            return await self.more(session)

        if (match := OPTION_RE.match(message)) and session.detail:
            return await self.select_option(session, match["label"])

        if match := DETAIL_RE.match(message):
            if product := self.find_product(session, match["target"]):
                return await self.detail(session, product["id"])
            message = match["target"]  # not something we showed, so search for it
        elif ordinal(message) is not None and (product := self.find_product(session, message)):
            return await self.detail(session, product["id"])

        low, high, rest = parse_price(message)
        words = keywords(rest)
        if not words and low is None and high is None:
            return self.help(), ["Table lamps under $150", "What can the store do?"]
        return await self.search(session, " ".join(words) or None, low, high)

    def help(self) -> list[dict]:
        return [text(
            f"I can search **{self.store_name}**'s live catalog over UCP. Try:\n\n"
            "- *table lamps under $150* — search with a price filter\n"
            "- *tell me about the second one* — full product detail\n"
            "- *show more* — next page of results\n"
            "- *what can the store do?* — the store's UCP capabilities"
        )]

    # ------------------------------------------------------------- search

    async def search(self, session: Session, query: str | None, low: int | None, high: int | None):
        request: dict = {"pagination": {"limit": PAGE_SIZE}, "context": {"currency": self.currency}}
        if query:
            request["query"] = query
        if low is not None or high is not None:
            request["filters"] = {"price": {k: v for k, v in (("min", low), ("max", high)) if v is not None}}

        response = await self.call_tool("search_catalog", request)
        # Nothing found for "lamps"? Try "lamp".
        if not response.get("products") and query and (simpler := " ".join(map(singular, query.split()))) != query:
            request = {**request, "query": simpler}
            response = await self.call_tool("search_catalog", request)
        return self.show_results(session, request, response, first_page=True)

    async def more(self, session: Session):
        if not session.last_search or not session.next_cursor:
            return [text("There are no more results for the last search.")], ["Table lamps under $150"]
        request = {**session.last_search, "pagination": {"limit": PAGE_SIZE, "cursor": session.next_cursor}}
        response = await self.call_tool("search_catalog", request)
        return self.show_results(session, request, response, first_page=False)

    def show_results(self, session: Session, request: dict, response: dict, first_page: bool):
        products = within_price(response.get("products") or [], (request.get("filters") or {}).get("price"))
        pagination = response.get("pagination") or {}
        session.last_search = {k: v for k, v in request.items() if k != "pagination"}
        session.next_cursor = pagination.get("cursor") if pagination.get("has_next_page") else None
        blocks = notices(response.get("messages"))

        described = self.describe(request)
        if not products:
            if session.next_cursor:
                return [text(f"Nothing on this page matched {described}."), *blocks], ["Show more"]
            return [text(f"I couldn't find anything {described}. Try different words?"), *blocks], \
                ["Floor lamps", "Throw pillows", "Rugs"]

        session.products = products
        count = "1 product" if len(products) == 1 else f"{len(products)} products"
        heading = f"Here {'is' if len(products) == 1 else 'are'} {count} {described}:" if first_page else "Here are more:"
        cards = {"type": "products", "products": [product_card(p) for p in products]}
        suggestions = ["Tell me about the first one"] + (["Show more"] if session.next_cursor else [])
        return [text(heading), cards, *blocks], suggestions

    def describe(self, request: dict) -> str:
        parts = [f"for “{request['query']}”"] if request.get("query") else []
        price = (request.get("filters") or {}).get("price") or {}
        if "min" in price:
            parts.append(f"from {dollars(price['min'])}")
        if "max" in price:
            parts.append(f"up to {dollars(price['max'])}")
        return " ".join(parts) or "for your search"

    # ------------------------------------------------------------- detail

    def find_product(self, session: Session, target: str) -> dict | None:
        """Match "the second one" or a product title against what we last showed."""
        shown = session.products + ([session.detail] if session.detail else [])
        if not shown:
            return None
        if session.products and (position := ordinal(target)) is not None:
            index = position - 1 if position > 0 else len(session.products) - 1
            return session.products[index] if 0 <= index < len(session.products) else None
        wanted = target.strip().lower()
        for product in shown:
            if product["title"].lower() == wanted:
                return product
        wanted_words = set(keywords(wanted))
        best = max(shown, key=lambda p: len(wanted_words & set(keywords(p["title"]))))
        return best if wanted_words & set(keywords(best["title"])) else None

    async def detail(self, session: Session, product_id: str, selected: list[dict] | None = None):
        request: dict = {"id": product_id, "context": {"currency": self.currency}}
        if selected:
            request["selected"] = selected
        response = await self.call_tool("get_product", request)
        product = response.get("product")
        if not product:
            return notices(response.get("messages")) or [text("I couldn't load that product.")], []
        session.detail = product
        suggestions = ["Show more"] if session.next_cursor else []
        return [product_detail(product), *notices(response.get("messages"))], suggestions

    async def select_option(self, session: Session, label: str):
        product = session.detail or {}
        for option in product.get("options") or []:
            for value in option.get("values") or []:
                if value["label"].lower() == label.lower():
                    selected = [{"name": option["name"], "label": value["label"]}]
                    return await self.detail(session, product["id"], selected)
        return [text(f"“{label}” isn't an option for this product.")], []

    # --------------------------------------------------------------- cart

    def pick_variant(self, session: Session, title: str, label: str | None) -> tuple[dict, dict] | None:
        """The product and variant the shopper means by "add the <title> in <label>"."""
        # The product open in detail comes first: its first variant is the one the shopper selected.
        if session.detail and session.detail["title"].lower() == title.strip().lower():
            product = session.detail
        else:
            product = self.find_product(session, title)
        variants = (product or {}).get("variants") or []
        if not variants:
            return None
        chosen = next((v for v in variants if label and v.get("title", "").lower() == label.strip().lower()), None)
        return product, chosen or variants[0]

    @staticmethod
    def cart_lines(session: Session) -> dict[str, int]:
        """The current cart as {variant id: quantity}."""
        return {line["item"]["id"]: line["quantity"] for line in (session.cart or {}).get("line_items") or []}

    @staticmethod
    def find_cart_line(session: Session, title: str) -> str | None:
        """The variant id of the cart line the shopper means, matched on its title."""
        lines = (session.cart or {}).get("line_items") or []
        wanted = title.strip().lower()
        for line in lines:
            if line["item"].get("title", "").lower() == wanted:
                return line["item"]["id"]
        wanted_words = set(keywords(wanted))
        scored = [(len(wanted_words & set(keywords(line["item"].get("title", "")))), line) for line in lines]
        best = max(scored, key=lambda pair: pair[0], default=(0, None))
        return best[1]["item"]["id"] if best[0] else None

    async def add_to_cart(self, session: Session, title: str, label: str | None, quantity: int):
        found = self.pick_variant(session, title, label)
        if not found:
            return [text("Which product? Search for it first, then use “Add to cart” on its card.")], []
        product, variant = found
        lines = self.cart_lines(session)
        lines[variant["id"]] = lines.get(variant["id"], 0) + quantity
        name = f"{product['title']} ({variant['title']})" if len(product["variants"]) > 1 else product["title"]
        return await self.save_cart(session, lines, f"Added **{name}** × {quantity} to your cart.")

    async def set_quantity(self, session: Session, title: str, quantity: int):
        variant_id = self.find_cart_line(session, title)
        if not variant_id:
            return [text(f"“{title}” isn't in your cart.")], ["What's in my cart?"]
        lines = self.cart_lines(session)
        if quantity > 0:
            lines[variant_id] = quantity
            message = "Updated the quantity in your cart."
        else:
            lines.pop(variant_id)
            message = "Removed it from your cart."
        return await self.save_cart(session, lines, message)

    async def save_cart(self, session: Session, lines: dict[str, int] | list, message: str):
        """
        Create the cart, or replace its contents. UCP update_cart is a full
        replacement, so every line (and the context) is sent each time.
        """
        items = [{"item": {"id": variant_id}, "quantity": qty} for variant_id, qty in dict(lines).items()]
        cart = {"line_items": items, "context": self.cart_context}
        session.checkout = None  # an open checkout no longer matches the cart; Checkout starts a new one

        response = None
        if session.cart:
            response = await self.call_store_tool("update_cart", {"id": session.cart["id"], "cart": cart})
            if is_not_found(response):  # expired or cancelled on the store's side; start a new cart
                session.cart = response = None
        if not session.cart:
            if not items:
                return [text("Your cart is empty."), cart_block(None)], ["Show me floor lamps"]
            response = await self.call_store_tool("create_cart", {"cart": cart})

        if response.get("ucp", {}).get("status") == "error" or not response.get("id"):
            failed = notices(response.get("messages")) or [notice("error", "The store couldn't update the cart.")]
            return failed, ["What's in my cart?"]
        session.cart = response
        suggestions = ["What's in my cart?"] + (["Show more"] if session.next_cursor else [])
        return [text(message), cart_block(response), *notices(response.get("messages"))], suggestions

    async def view_cart(self, session: Session):
        if not session.cart:
            return [text("Your cart is empty."), cart_block(None)], ["Show me floor lamps"]

        response = await self.call_store_tool("get_cart", {"id": session.cart["id"]})
        if is_not_found(response):
            session.cart = None
            return [text("Your cart has expired, so it's empty now."), cart_block(None)], ["Show me floor lamps"]
        if response.get("ucp", {}).get("status") == "error":
            return notices(response.get("messages")) or [notice("error", "The store couldn't load the cart.")], []

        session.cart = response
        count = cart_state(response)["cart_count"]
        heading = f"You have {count} item{'' if count == 1 else 's'} in your cart:" if count else "Your cart is empty."
        blocks = [text(heading), cart_block(response),
                  *notices(response.get("messages"))]

        # Checkout hand-off to the store is switched off for now; uncomment to show it.
        # UCP gives the cart a continue_url for finishing the purchase on the store's site.
        # if response.get("continue_url"):
        #     blocks.append(text(f"[Check out on {self.store_name}]({response['continue_url']})"))

        return blocks, []

    # ----------------------------------------------------------- checkout

    async def start_checkout(self, session: Session):
        if not cart_state(session.cart)["cart_count"]:
            return [text("Your cart is empty. Add something first, then choose Checkout.")], ["Show me floor lamps"]

        # UCP: a checkout can be created from the cart; the store takes the items from it.
        response = await self.call_store_tool(
            "create_checkout", {"checkout": {"cart_id": session.cart["id"], "line_items": []}}
        )
        if is_not_found(response):
            session.cart = None
            return [text("Your cart has expired, so there is nothing to check out.")], ["Show me floor lamps"]

        session.delivery_option = None
        if response.get("id") and (session.buyer or session.address):
            # Reuse details the shopper already gave in this chat.
            session.checkout = response
            response = await self.call_store_tool(
                "update_checkout", {"id": response["id"], "checkout": self.checkout_payload(session)}
            )
        return self.checkout_reply(session, response, "Here's your checkout:")

    async def view_checkout(self, session: Session):
        response = await self.call_store_tool("get_checkout", {"id": session.checkout["id"]})
        return self.checkout_reply(session, response, "Here's your checkout:")

    async def update_checkout(self, session: Session, saved: str):
        response = await self.call_store_tool(
            "update_checkout", {"id": session.checkout["id"], "checkout": self.checkout_payload(session)}
        )
        return self.checkout_reply(session, response, f"Saved {saved}.")

    async def cancel_checkout(self, session: Session):
        if not session.checkout:
            return [text("There's no checkout open.")], ["What's in my cart?"]
        await self.call_store_tool("cancel_checkout", {"id": session.checkout["id"]})
        session.checkout = None
        session.delivery_option = None
        # The store may close the cart together with its checkout.
        if session.cart and is_not_found(await self.call_store_tool("get_cart", {"id": session.cart["id"]})):
            session.cart = None
        return [text("Checkout cancelled.")], ["What's in my cart?"]

    def checkout_payload(self, session: Session) -> dict:
        """The whole checkout, as UCP update_checkout expects (it replaces the previous state)."""
        lines = session.checkout.get("line_items") or []
        payload: dict = {
            "line_items": [{"item": {"id": line["item"]["id"]}, "quantity": line["quantity"]} for line in lines],
            "context": self.cart_context,
        }
        if session.buyer:
            payload["buyer"] = session.buyer
        if session.address and not missing_recipient(session):
            # The store needs the recipient's name and phone with the address, and keeps the
            # first destination it was given, so the destination is sent once it is complete.
            recipient = {k: session.buyer[k] for k in RECIPIENT_FIELDS}
            method = {
                "type": "shipping",
                "line_item_ids": [line["id"] for line in lines],
                "destinations": [{**session.address, **recipient}],
            }
            group = (shipping_method(session.checkout).get("groups") or [{}])[0]
            if session.delivery_option and group.get("id"):
                method["groups"] = [{"id": group["id"], "selected_option_id": session.delivery_option}]
            payload["fulfillment"] = {"methods": [method]}
        return payload

    @staticmethod
    def checkout_details(session: Session, message: str) -> str | None:
        """Pick up email, phone, name and address from the message; returns what was saved."""
        saved = []
        if match := EMAIL_RE.search(message):
            session.buyer["email"] = match[0]
            saved.append("your email")
        if match := PHONE_RE.search(message):
            phone = re.sub(r"[^\d+]", "", match["phone"])
            # UCP expects E.164 phone numbers; assume a US number when no country code is given.
            session.buyer["phone_number"] = phone if phone.startswith("+") else f"+1{phone[-10:]}"
            saved.append("your phone number")
        if match := NAME_RE.search(message):
            session.buyer["first_name"] = match["first"].strip()
            if match["last"]:
                session.buyer["last_name"] = match["last"].strip()
            saved.append("your name")
        if (match := ADDRESS_RE.match(message)) and (address := parse_address(match["address"])):
            session.address = address
            saved.append("the shipping address")
        return " and ".join(saved) or None

    @staticmethod
    def find_delivery_option(session: Session, message: str) -> dict | None:
        group = (shipping_method(session.checkout).get("groups") or [{}])[0]
        wanted = message.strip().lower()
        return next((o for o in group.get("options") or [] if o.get("title", "").lower() == wanted), None)

    def checkout_reply(self, session: Session, response: dict, heading: str):
        """Show the checkout, say what the store still needs, and hand off when only the store can finish."""
        if is_not_found(response):
            session.checkout = None
            return [text("That checkout has expired. Choose Checkout again to start a new one.")], ["What's in my cart?"]
        if not response.get("id"):
            return notices(response.get("messages")) or [notice("error", "The store couldn't open a checkout.")], []

        session.checkout = response
        blocks = [text(heading), checkout_block(response)]

        # UCP: fix "recoverable" errors with update_checkout. Anything needing the buyer on the
        # store's own page (requires_buyer_input / review) would go to continue_url, which is switched off.
        hints = {
            "buyer_identity_contact_method_required": "your email — *my email is you@example.com*",
            "delivery_address_required": "a shipping address — *ship to 123 Main St, Springfield, IL 62701*",
            "delivery_first_name_required": "your name — *my name is Jane Doe*",
            "delivery_last_name_required": "your name — *my name is Jane Doe*",
            "delivery_phone_number_required": "a phone number — *my phone is +1 217 555 0123*",
        }
        recoverable = [m for m in response.get("messages") or []
                       if m.get("type") == "error" and m.get("severity") == "recoverable"
                       # Before any address is given the store also says it can't deliver; skip that.
                       and not (m.get("code") == "delivery_no_delivery_available" and not session.address)]
        if session.address and missing_recipient(session):
            # We have the address but haven't sent it yet (see checkout_payload).
            recoverable = [m for m in recoverable if m.get("code") != "delivery_address_required"]
            recoverable += [{"code": f"delivery_{field}_required"} for field in missing_recipient(session)]
        needed = list(dict.fromkeys(hints.get(m.get("code"), m.get("content", "")) for m in recoverable))
        if needed:
            blocks.append(text("To continue, tell me:\n\n" + "\n".join(f"- {item}" for item in needed)))
        else:
            blocks.append(text("Your checkout details are complete. Payment isn't enabled in this assistant yet, "
                               "so checkout stops here."))
        # Hand-off to the store's own payment page (UCP continue_url); uncomment to bring it back.
        # elif response.get("continue_url"):
        #     blocks.append(text(
        #         f"Everything I can fill in here is done. Payment and placing the order happen on "
        #         f"{self.store_name}'s own checkout page: "
        #         f"[Continue to payment on {self.store_name}]({response['continue_url']})"
        #     ))
        return blocks, ["Show my checkout", "Cancel checkout"]


RECIPIENT_FIELDS = ("first_name", "last_name", "phone_number")


def missing_recipient(session: Session) -> list[str]:
    """Recipient details the delivery destination still needs."""
    return [field for field in RECIPIENT_FIELDS if not session.buyer.get(field)]


def is_not_found(response: dict) -> bool:
    """UCP reports a missing (expired or cancelled) cart as an error with a *not_found code."""
    return response.get("ucp", {}).get("status") == "error" and any(
        str(m.get("code", "")).endswith("not_found") for m in response.get("messages") or []
    )
