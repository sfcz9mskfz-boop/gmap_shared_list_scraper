import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("gmaps-shared-list-parseforge-like")

app = FastAPI(
    title="Google Maps Shared List Scraper - ParseForge-like Replica",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScrapeRequest(BaseModel):
    listUrl: HttpUrl
    maxPlacesPerList: int = Field(default=500, ge=1, le=500)
    scrapeDetails: bool = False
    headless: bool = True
    timeoutSeconds: int = Field(default=70, ge=20, le=180)
    debug: bool = False


class ScrapeResponse(BaseModel):
    ok: bool
    listName: Optional[str]
    sourceUrl: str
    resolvedUrl: Optional[str] = None
    count: int
    rawItemCount: int = 0
    normalizedCount: int = 0
    maxPlacesPerList: int = 500
    possibleLimitHit: bool = False
    places: List[Dict[str, Any]]
    warnings: List[str] = []
    debug: Optional[Dict[str, Any]] = None


JUNK_NAMES = {
    "directions", "save", "saved", "share", "nearby", "send to phone", "website", "call",
    "copy link", "route", "open", "close", "more", "menu", "reviews", "photos", "google maps",
    "maps", "back", "search", "start", "add", "edit", "remove", "visited", "want to go",
}

PLACE_URL_RE = re.compile(
    r"https?://(?:www\.)?google\.[a-z.]+/(?:maps|travel)/(?:place|search|dir|preview/place)[^\s\"'<>\\)\]}]+",
    re.I,
)
REL_PLACE_URL_RE = re.compile(r"(?<![A-Za-z0-9])(/maps/place/[^\s\"'<>\\)\]}]+)", re.I)
CID_URL_RE = re.compile(r"https?://(?:www\.)?google\.[a-z.]+/maps\?cid=\d+[^\s\"'<>\\)\]}]*", re.I)
MAPS_APP_RE = re.compile(r"https?://maps\.app\.goo\.gl/[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+", re.I)
FEATURE_ID_RE = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
CID_RE = re.compile(r"(?:cid=|!1s)(\d{6,}|0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
COORD_PAIR_RE = re.compile(r"(-?\d{1,2}\.\d{4,}),\s*(-?\d{1,3}\.\d{4,})")
DATA_COORD_RE = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
AT_COORD_RE = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def clean_text(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    text = html.unescape(text)
    text = text.replace("\u200e", "").replace("\u202a", "").replace("\ufeff", "")
    text = re.sub(r"\\u003[dD]", "=", text)
    text = re.sub(r"\\u0026", "&", text)
    text = text.replace(r"\/", "/")
    text = " ".join(text.split())
    return text or None


def decode_google_text(value: str) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = value.replace(r"\/", "/")
    value = re.sub(r"\\u003[dD]", "=", value)
    value = re.sub(r"\\u0026", "&", value)
    value = re.sub(r"\\u002[fF]", "/", value)
    # Keep this conservative; full unicode_escape can corrupt non-English place names.
    return value


def normalize_google_url(url: str) -> str:
    url = decode_google_text(url.strip())
    if url.startswith("/"):
        url = "https://www.google.com" + url
    url = url.rstrip(".,;:!\")'}]")
    url = url.replace("&amp;", "&")
    return url


def stable_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def extract_lat_lng_from_text(text: str) -> Tuple[Optional[float], Optional[float]]:
    if not text:
        return None, None
    for pattern in (DATA_COORD_RE, AT_COORD_RE, COORD_PAIR_RE):
        match = pattern.search(text)
        if match:
            try:
                lat = float(match.group(1))
                lng = float(match.group(2))
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    return lat, lng
            except Exception:
                pass
    return None, None


def place_name_from_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        match = re.search(r"/maps/place/([^/@?]+)", parsed.path)
        if match:
            name = clean_text(unquote(match.group(1)).replace("+", " "))
            if name and name.lower() not in JUNK_NAMES and not name.lower().startswith("data="):
                return name
        qs = parse_qs(parsed.query)
        for key in ("q", "query"):
            if qs.get(key):
                name = clean_text(unquote(qs[key][0]).replace("+", " "))
                if name and name.lower() not in JUNK_NAMES:
                    return name
    except Exception:
        pass
    return None


def extract_ids(text: str) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"cid": None, "featureId": None, "placeId": None}
    if not text:
        return out
    feature = FEATURE_ID_RE.search(text)
    if feature:
        out["featureId"] = feature.group(0)
    cid_match = re.search(r"[?&]cid=(\d+)", text)
    if cid_match:
        out["cid"] = cid_match.group(1)
    ftid_match = re.search(r"[?&]ftid=([^&]+)", text)
    if ftid_match:
        out["featureId"] = unquote(ftid_match.group(1))
    # ChIJ-style Place IDs frequently appear in detail URLs / payloads.
    place_id = re.search(r"ChI[A-Za-z0-9_-]{10,}", text)
    if place_id:
        out["placeId"] = place_id.group(0)
    return out


def maybe_category_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    if any(x in t for x in ("restaurant", "ramen", "sushi", "izakaya", "food", "bar & grill")):
        return "restaurant"
    if any(x in t for x in ("cafe", "coffee", "bakery", "dessert")):
        return "cafe"
    if any(x in t for x in ("hotel", "inn", "ryokan", "lodging", "hostel")):
        return "hotel"
    if any(x in t for x in ("station", "airport", "bus stop", "train")):
        return "transport"
    if any(x in t for x in ("shop", "store", "market", "mall", "shopping")):
        return "shopping"
    if any(x in t for x in ("park", "museum", "temple", "shrine", "garden", "tourist", "observation", "canal")):
        return "sight"
    return None


def make_place(
    *,
    name: Optional[str] = None,
    url: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    address: Optional[str] = None,
    category: Optional[str] = None,
    source: str = "unknown",
    raw: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    name = clean_text(name)
    url = normalize_google_url(url) if url else None
    raw = raw or url or name or ""
    if lat is None or lng is None:
        lat2, lng2 = extract_lat_lng_from_text(raw)
        lat = lat if lat is not None else lat2
        lng = lng if lng is not None else lng2
    ids = extract_ids(raw + " " + (url or ""))
    if not name and url:
        name = place_name_from_url(url)
    if not url:
        if ids.get("cid"):
            url = f"https://www.google.com/maps?cid={ids['cid']}"
        elif ids.get("featureId"):
            url = f"https://www.google.com/maps/search/?api=1&query={ids['featureId']}"
        elif lat is not None and lng is not None:
            url = f"https://www.google.com/maps/search/?api=1&query={lat:.7f},{lng:.7f}"
        elif name:
            url = f"https://www.google.com/maps/search/{unquote(name).replace(' ', '+')}"
    if not name and lat is not None and lng is not None:
        name = "지도 좌표 위치"
    if not name and not url:
        return None
    if name and name.lower() in JUNK_NAMES:
        return None
    if name and len(name) > 180:
        # Usually a whole card text blob; keep the first short line when possible.
        short = re.split(r"(?:\\n| · |  )", name)[0].strip()
        name = short[:180]
    key_seed = ids.get("placeId") or ids.get("cid") or ids.get("featureId") or url or f"{name}|{lat}|{lng}"
    return {
        "id": stable_id(key_seed),
        "placeId": ids.get("placeId"),
        "cid": ids.get("cid"),
        "featureId": ids.get("featureId"),
        "name": name,
        "title": name,
        "address": clean_text(address),
        "category": category or maybe_category_from_text(name or "") or maybe_category_from_text(address or ""),
        "type": category or maybe_category_from_text(name or "") or maybe_category_from_text(address or ""),
        "googleMapsUrl": url,
        "url": url,
        "latitude": lat,
        "longitude": lng,
        "lat": lat,
        "lng": lng,
        "rating": None,
        "reviewCount": None,
        "phone": None,
        "website": None,
        "source": source,
    }


def merge_place(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if existing.get(key) in (None, "", [], {}):
            existing[key] = value
    # Prefer human-readable names over coordinate placeholders.
    if existing.get("name") == "지도 좌표 위치" and incoming.get("name") and incoming.get("name") != "지도 좌표 위치":
        existing["name"] = incoming["name"]
        existing["title"] = incoming["name"]
    return existing


def dedupe_places(places: Iterable[Dict[str, Any]], max_places: int) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    ordered: List[str] = []
    for place in places:
        if not place:
            continue
        ids = [place.get("placeId"), place.get("cid"), place.get("featureId")]
        key = next((str(x).lower() for x in ids if x), None)
        if not key:
            lat = place.get("latitude")
            lng = place.get("longitude")
            if lat is not None and lng is not None:
                key = f"coord:{float(lat):.6f},{float(lng):.6f}"
        if not key:
            url = (place.get("googleMapsUrl") or place.get("url") or "").split("?")[0]
            if url:
                key = f"url:{url.lower()}"
        if not key:
            key = f"name:{(place.get('name') or '').lower()}"
        if key in seen:
            seen[key] = merge_place(seen[key], place)
        else:
            seen[key] = place
            ordered.append(key)
        if len(ordered) >= max_places:
            break
    return [seen[k] for k in ordered[:max_places]]


def extract_places_from_text_blob(text: str, source: str) -> List[Dict[str, Any]]:
    text = decode_google_text(text or "")
    if not text:
        return []
    candidates: List[Dict[str, Any]] = []

    for regex in (PLACE_URL_RE, REL_PLACE_URL_RE, CID_URL_RE):
        for m in regex.finditer(text):
            url = normalize_google_url(m.group(0))
            candidates.append(make_place(url=url, raw=url, source=source))

    # Feature IDs often appear in list payload even when URLs are not visible.
    # Create searchable Google Maps URLs from them; detail enrichment can later fill names.
    for m in FEATURE_ID_RE.finditer(text):
        window = text[max(0, m.start() - 500): min(len(text), m.end() + 500)]
        lat, lng = extract_lat_lng_from_text(window)
        name = best_string_near_feature(window)
        candidates.append(make_place(name=name, url=None, lat=lat, lng=lng, raw=m.group(0) + " " + window, source=source))

    return [p for p in candidates if p]


def best_string_near_feature(window: str) -> Optional[str]:
    # Pull plausible human labels from JSON/stringified payload windows.
    strings = re.findall(r'"([^"\\]{2,120})"', window)
    cleaned: List[str] = []
    for s in strings:
        s = clean_text(s)
        if not s:
            continue
        low = s.lower()
        if low in JUNK_NAMES:
            continue
        if low.startswith(("http", "0x", "//", "/maps", "data=", "!")):
            continue
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
            continue
        if len(s) < 2 or len(s) > 80:
            continue
        # Avoid internal RPC / class / CSS strings.
        if re.search(r"[{}<>]|\\u|google|maps|schema|aria|function|var |null", s, re.I):
            continue
        cleaned.append(s)
    if not cleaned:
        return None
    # Prefer strings with letters and spaces but no obvious address-only commas.
    cleaned.sort(key=lambda x: (bool(re.search(r"[A-Za-z가-힣ぁ-んァ-ン一-龯]", x)), -abs(len(x) - 22)), reverse=True)
    return cleaned[0]


async def safe_text(locator, timeout: int = 1200) -> Optional[str]:
    try:
        if await locator.count() > 0:
            return clean_text(await locator.first.text_content(timeout=timeout))
    except Exception:
        return None
    return None


async def safe_attr(locator, attr: str, timeout: int = 1200) -> Optional[str]:
    try:
        if await locator.count() > 0:
            return await locator.first.get_attribute(attr, timeout=timeout)
    except Exception:
        return None
    return None


async def handle_google_dialogs(page: Page) -> None:
    # Consent/cookie dialogs vary by region. These are intentionally best-effort.
    labels = ["Accept all", "I agree", "Reject all", "Not now", "Skip", "Agree", "Accept"]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if await btn.count() > 0:
                await btn.first.click(timeout=1200)
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def get_list_name(page: Page, resolved_url: Optional[str] = None) -> Optional[str]:
    selectors = ["h1", '[role="main"] h1', '[aria-level="1"]', 'meta[property="og:title"]']
    for selector in selectors:
        try:
            if selector.startswith("meta"):
                val = await page.locator(selector).first.get_attribute("content", timeout=1000)
                text = clean_text(val)
            else:
                text = await safe_text(page.locator(selector), timeout=1800)
            if text and text.lower() not in {"google maps", "maps"}:
                text = re.sub(r"\s*-\s*Google Maps\s*$", "", text, flags=re.I).strip()
                if text:
                    return text
        except Exception:
            continue
    try:
        title = clean_text(await page.title())
        if title:
            title = re.sub(r"\s*-\s*Google Maps\s*$", "", title, flags=re.I).strip()
            if title and title.lower() not in {"google maps", "maps"}:
                return title
    except Exception:
        pass
    if resolved_url:
        # Shared list URLs don't usually have a useful slug, but use it as last resort.
        path = urlparse(resolved_url).path.strip("/").split("/")
        if path:
            slug = unquote(path[-1]).replace("-", " ").replace("_", " ")
            if len(slug) > 3 and not re.match(r"^[A-Za-z0-9_-]{15,}$", slug):
                return clean_text(slug.title())
    return None


async def collect_visible_dom_places(page: Page) -> List[Dict[str, Any]]:
    # Extract in browser to avoid fragile Python locator loops. This grabs links and card text.
    raw = await page.evaluate(
        """
        () => {
          const out = [];
          const push = (o) => { if (o && (o.href || o.text || o.aria)) out.push(o); };
          document.querySelectorAll('a[href*="/maps/place/"], a[href*="maps?cid="], a[href*="google.com/maps"], a[href^="/maps/place/"]').forEach(a => {
            push({href: a.href || a.getAttribute('href'), aria: a.getAttribute('aria-label'), text: a.innerText});
          });
          const cards = Array.from(document.querySelectorAll('div[role="article"], div.Nv2PK, div[jsaction*="mouseover:pane"], div[aria-label][role="button"], div[aria-label][tabindex]')).slice(0, 700);
          for (const c of cards) {
            const a = c.querySelector('a[href*="/maps/place/"], a[href*="maps?cid="], a[href^="/maps/place/"]');
            push({href: a ? (a.href || a.getAttribute('href')) : '', aria: c.getAttribute('aria-label'), text: c.innerText});
          }
          return out;
        }
        """
    )
    places: List[Dict[str, Any]] = []
    for item in raw or []:
        href = normalize_google_url(item.get("href") or "") if item.get("href") else None
        aria = clean_text(item.get("aria"))
        text = clean_text(item.get("text"))
        name = aria or None
        address = None
        if text:
            lines = [clean_text(x) for x in re.split(r"[\n\r]+", text) if clean_text(x)]
            if not name and lines:
                name = lines[0]
            if len(lines) > 1:
                address = next((x for x in lines[1:5] if not re.search(r"stars|reviews|minutes|closed|open", x, re.I)), None)
        if href or name:
            p = make_place(name=name, url=href, address=address, raw=" ".join([href or "", aria or "", text or ""]), source="dom")
            if p:
                places.append(p)
    return places


async def collect_state_texts(page: Page) -> List[Tuple[str, str]]:
    texts: List[Tuple[str, str]] = []
    try:
        state = await page.evaluate(
            """
            () => {
              const chunks = [];
              const add = (name, value) => {
                try { if (value !== undefined && value !== null) chunks.push([name, JSON.stringify(value)]); } catch (e) {}
              };
              add('APP_INITIALIZATION_STATE', window.APP_INITIALIZATION_STATE);
              add('__APP_STATE__', window.__APP_STATE__);
              add('WIZ_global_data', window.WIZ_global_data);
              add('_F_cssRowKey', window._F_cssRowKey);
              chunks.push(['location', location.href]);
              chunks.push(['title', document.title]);
              chunks.push(['anchors', Array.from(document.querySelectorAll('a[href]')).map(a => a.href).join('\n')]);
              chunks.push(['visibleText', document.body ? document.body.innerText.slice(0, 300000) : '']);
              chunks.push(['scripts', Array.from(document.scripts).slice(0, 80).map(s => s.textContent || '').join('\n').slice(0, 1500000)]);
              return chunks;
            }
            """
        )
        for name, value in state or []:
            if value:
                texts.append((f"state:{name}", value))
    except Exception as exc:
        logger.info("state extraction failed: %s", exc)
    return texts


async def scroll_google_list(page: Page, rounds: int = 30, stop_after_no_change: int = 7) -> None:
    previous_height = 0
    still = 0
    for _ in range(rounds):
        try:
            metrics = await page.evaluate(
                """
                () => {
                  const candidates = Array.from(document.querySelectorAll('div, main, section'))
                    .filter(e => e.scrollHeight > e.clientHeight + 120)
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                  let total = 0;
                  for (const el of candidates.slice(0, 8)) {
                    total += el.scrollHeight;
                    el.scrollTop = el.scrollHeight;
                    el.dispatchEvent(new Event('scroll', {bubbles:true}));
                  }
                  window.scrollBy(0, Math.max(1200, document.body ? document.body.scrollHeight : 2000));
                  return {height: document.body ? document.body.innerText.length : 0, scrollables: candidates.length, total};
                }
                """
            )
            h = int(metrics.get("height") or 0)
            if h <= previous_height + 50:
                still += 1
            else:
                still = 0
                previous_height = h
            if still >= stop_after_no_change:
                break
        except Exception:
            pass
        await page.wait_for_timeout(850)


async def enrich_place_details(context: BrowserContext, place: Dict[str, Any], index: int) -> Dict[str, Any]:
    url = place.get("googleMapsUrl") or place.get("url")
    if not url:
        return place
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(1700)
        await handle_google_dialogs(page)
        name = await safe_text(page.locator("h1"), timeout=2000)
        if name and name.lower() not in JUNK_NAMES:
            place["name"] = place["title"] = name
        rating_text = await safe_text(page.locator('div.F7nice span[aria-hidden="true"]'), timeout=1000)
        if rating_text:
            m = re.search(r"\d+(?:\.\d+)?", rating_text)
            if m:
                place["rating"] = float(m.group(0))
        review_text = await safe_text(page.locator('button[aria-label*="review" i], span[aria-label*="review" i]'), timeout=1000)
        if review_text:
            m = re.search(r"[\d,]+", review_text)
            if m:
                place["reviewCount"] = int(m.group(0).replace(",", ""))
        address = await safe_attr(page.locator('button[data-item-id="address"], button[aria-label^="Address:" i]'), "aria-label", timeout=1200)
        if address:
            place["address"] = clean_text(re.sub(r"^Address:\s*", "", address, flags=re.I))
        phone = await safe_attr(page.locator('button[data-item-id^="phone:"], button[aria-label^="Phone:" i]'), "aria-label", timeout=1200)
        if phone:
            place["phone"] = clean_text(re.sub(r"^Phone:\s*", "", phone, flags=re.I))
        website = await safe_attr(page.locator('a[data-item-id="authority"], a[aria-label^="Website:" i]'), "href", timeout=1200)
        if website:
            place["website"] = website
        category = await safe_text(page.locator('button[jsaction*="pane.rating.category"], button[aria-label*="Category" i]'), timeout=1000)
        if category:
            place["category"] = place["type"] = category
        lat, lng = extract_lat_lng_from_text(page.url)
        if lat is not None and lng is not None:
            place["latitude"] = place["lat"] = lat
            place["longitude"] = place["lng"] = lng
        place["scrapedOk"] = True
    except Exception as exc:
        logger.info("detail scrape failed for item %s: %s", index + 1, exc)
        place["scrapedOk"] = False
    finally:
        await page.close()
    return place


async def scrape_google_shared_list(req: ScrapeRequest) -> ScrapeResponse:
    source_url = str(req.listUrl)
    deadline = time.monotonic() + req.timeoutSeconds
    warnings: List[str] = []
    debug_info: Dict[str, Any] = {"sources": {}, "responseUrls": []} if req.debug else {}

    captured_texts: List[Tuple[str, str]] = []
    captured_limit_chars = 7_000_000
    captured_chars = 0

    async def capture_response(response):
        nonlocal captured_chars
        try:
            url = response.url
            if "google" not in url and "gstatic" not in url:
                return
            if req.debug and len(debug_info.get("responseUrls", [])) < 100:
                debug_info["responseUrls"].append(url[:300])
            content_type = (response.headers.get("content-type") or "").lower()
            if not any(x in content_type for x in ("text", "json", "javascript", "x-protobuf")):
                return
            if captured_chars >= captured_limit_chars:
                return
            text = await response.text()
            if not text:
                return
            text = text[:1_200_000]
            captured_texts.append((f"network:{url[:160]}", text))
            captured_chars += len(text)
        except Exception:
            return

    async with async_playwright() as p:
        proxy = None
        proxy_server = os.getenv("PROXY_SERVER", "").strip()
        if proxy_server:
            proxy = {"server": proxy_server}
            if os.getenv("PROXY_USERNAME"):
                proxy["username"] = os.getenv("PROXY_USERNAME")
            if os.getenv("PROXY_PASSWORD"):
                proxy["password"] = os.getenv("PROXY_PASSWORD")

        browser = await p.chromium.launch(
            headless=req.headless,
            proxy=proxy,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1536, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            java_script_enabled=True,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9,ko;q=0.8"},
        )
        page = await context.new_page()
        page.on("response", lambda response: asyncio.create_task(capture_response(response)))

        try:
            try:
                await page.goto(source_url, wait_until="domcontentloaded", timeout=55_000)
            except PlaywrightTimeoutError:
                warnings.append("Google Maps took too long to load; using partial data collected so far.")
            await page.wait_for_timeout(2500)
            await handle_google_dialogs(page)
            resolved_url = page.url

            # First harvest: initial list payload and any visible rows.
            state_texts = await collect_state_texts(page)
            dom_places = await collect_visible_dom_places(page)

            # Scroll to trigger lazy list RPCs, then harvest again.
            if time.monotonic() < deadline:
                await scroll_google_list(page, rounds=38)
                await page.wait_for_timeout(1200)
            state_texts += await collect_state_texts(page)
            dom_places += await collect_visible_dom_places(page)

            # Let network capture tasks finish.
            await page.wait_for_timeout(1000)

            list_name = await get_list_name(page, resolved_url)

            places: List[Dict[str, Any]] = []
            places.extend(dom_places)
            source_counts: Dict[str, int] = {"dom": len(dom_places)}

            for label, blob in state_texts + captured_texts:
                extracted = extract_places_from_text_blob(blob, label)
                source_counts[label.split(":", 1)[0]] = source_counts.get(label.split(":", 1)[0], 0) + len(extracted)
                places.extend(extracted)

            raw_count = len(places)
            places = dedupe_places(places, req.maxPlacesPerList)

            # Enrich only if requested. The app defaults false because detail pages are much slower.
            if req.scrapeDetails and places and time.monotonic() < deadline:
                sem = asyncio.Semaphore(4)

                async def limited(i: int, pl: Dict[str, Any]) -> Dict[str, Any]:
                    async with sem:
                        if time.monotonic() >= deadline:
                            return pl
                        return await enrich_place_details(context, pl, i)

                places = await asyncio.gather(*(limited(i, pl) for i, pl in enumerate(places)))

            if not places:
                warnings.append(
                    "No places were extracted. The list may be private, Google may have served a consent/CAPTCHA page, or the Render IP may be blocked."
                )
            elif len(places) < 10:
                warnings.append(
                    "Only a small number of places were extracted. If the list has more, try making the list public, redeploying, or adding a proxy via PROXY_SERVER."
                )

            if req.debug:
                debug_info["sources"] = source_counts
                debug_info["capturedTextBlocks"] = len(captured_texts)
                debug_info["resolvedUrl"] = resolved_url
                debug_info["visibleDomCount"] = len(dom_places)

            return ScrapeResponse(
                ok=bool(places),
                listName=list_name,
                sourceUrl=source_url,
                resolvedUrl=resolved_url,
                count=len(places),
                rawItemCount=raw_count,
                normalizedCount=len(places),
                maxPlacesPerList=req.maxPlacesPerList,
                possibleLimitHit=len(places) >= req.maxPlacesPerList,
                places=places,
                warnings=warnings,
                debug=debug_info if req.debug else None,
            )
        except Exception as exc:
            logger.exception("scrape failed")
            return ScrapeResponse(
                ok=False,
                listName=None,
                sourceUrl=source_url,
                resolvedUrl=None,
                count=0,
                rawItemCount=0,
                normalizedCount=0,
                maxPlacesPerList=req.maxPlacesPerList,
                possibleLimitHit=False,
                places=[],
                warnings=[f"Scrape failed: {type(exc).__name__}. Check Render logs for details."],
                debug=debug_info if req.debug else None,
            )
        finally:
            await browser.close()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "status": "ok", "version": "2.0.0"}


@app.post("/scrape-google-list", response_model=ScrapeResponse)
async def scrape_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    return await scrape_google_shared_list(req)


@app.post("/api/import-google-list", response_model=ScrapeResponse)
async def app_import_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    return await scrape_google_shared_list(req)


@app.get("/api/import-google-list", response_model=ScrapeResponse)
async def app_import_get(
    url: str = Query(..., description="Google Maps shared-list URL"),
    maxPlacesPerList: int = Query(500, ge=1, le=500),
    scrapeDetails: bool = Query(False),
    debug: bool = Query(False),
) -> ScrapeResponse:
    try:
        req = ScrapeRequest(
            listUrl=url,
            maxPlacesPerList=maxPlacesPerList,
            scrapeDetails=scrapeDetails,
            debug=debug,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await scrape_google_shared_list(req)
