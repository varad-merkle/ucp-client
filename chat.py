"""Chat for the shopping frontend, built on the UCP catalog tools.

The rules are simple and deterministic (no LLM):

    "hi" / "help"                        -> short help text
    "what can the store do?"             -> the store's UCP capabilities
    "table lamps under $150"             -> search_catalog with a price filter
    "show more"                          -> search_catalog with the next cursor
    "tell me about the second one"       -> get_product on the last results
    "in Sable"                           -> get_product with that option selected
    "add the <product>"                  -> link to the store's checkout for it

Each reply is a list of blocks (text, notice, products, product, capabilities)
in the shape the frontend expects (see ucp-frontend/src/lib/types.ts).
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
COMMERCE_RE = re.compile(r"\b(add|cart|checkout|check out|pay|order)\b", re.I)
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
    def __init__(self, call_tool: CallTool, fetch_store_profile: FetchProfile, store_name: str, currency: str):
        self.call_tool = call_tool
        self.fetch_store_profile = fetch_store_profile
        self.store_name = store_name
        self.currency = currency
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
            blocks = [notice("error", f"The store could not be reached: {exc}")]
            suggestions = ["Try again"]
        return {"session_id": session.id, "blocks": blocks, "suggestions": suggestions, "state": EMPTY_STATE}

    async def route(self, message: str, session: Session) -> tuple[list[dict], list[str]]:
        if not message or GREETING_RE.match(message):
            return self.help(), ["Table lamps under $150", "What can the store do?"]

        if CAPABILITIES_RE.search(message):
            profile = await self.fetch_store_profile()
            intro = text(f"**{self.store_name}** publishes these UCP capabilities:")
            return [intro, capabilities_block(self.store_name, profile)], ["Show me floor lamps", "Quilt sets"]

        if match := ADD_RE.match(message):
            return self.buy_link(session, match["title"], match["label"], int(match["qty"] or 1))

        if COMMERCE_RE.search(message):
            return [notice("info", "Cart and checkout aren't built into this assistant yet. "
                                   "You can keep browsing and asking about products.")], []

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

    # ---------------------------------------------------------------- buy

    def buy_link(self, session: Session, title: str, label: str | None, quantity: int):
        product = self.find_product(session, title)
        if not product:
            return [text("Which product? Search for it first, then use “Add to cart” on its card.")], []

        variants = product.get("variants") or []
        variant = next((v for v in variants if label and v.get("title", "").lower() == label.lower()), None)
        variant = variant or (variants[0] if variants else {})
        url = variant.get("checkout_url")
        if not url:
            return [notice("warning", "The store didn't return a checkout link for this product.")], []

        name = f"{product['title']} ({variant['title']})" if len(variants) > 1 else product["title"]
        suggestions = ["Show more"] if session.next_cursor else []

        # Checkout link to the store is switched off for now; uncomment to bring it back.
        # url = re.sub(r":\d+$", f":{quantity}", url)  # the store's cart permalink ends in :<quantity>
        # return [text(
        #     "Cart and checkout aren't built into this assistant yet, but you can buy it straight from the store:\n\n"
        #     f"- **{name}** × {quantity} — [Check out on {self.store_name}]({url})"
        # )], suggestions

        return [notice("info", f"Cart and checkout aren't built into this assistant yet, "
                               f"so “{name}” can't be added right now.")], suggestions
