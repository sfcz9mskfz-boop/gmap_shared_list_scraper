import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, HttpUrl
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("gmaps-shared-list-strict")

APP_VERSION = "3.0.0-strict-list"

app = FastAPI(
    title="Google Maps Shared List Scraper - Strict Saved List Replica",
    version=APP_VERSION,
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
    timeoutSeconds: int = Field(default=85, ge=20, le=220)
    debug: bool = False
    strictListOnly: bool = True


class ScrapeResponse(BaseModel):
    ok: bool
    listName: Optional[str]
    ownerName: Optional[str] = None
    expectedCount: Optional[int] = None
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
    "view all", "done", "cancel", "learn more", "settings", "sort", "follow", "following",
}

PLACE_URL_RE = re.compile(r"/maps/place/|[?&]cid=\d+|[?&]ftid=0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", re.I)
FEATURE_ID_RE = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
DATA_COORD_RE = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
AT_COORD_RE = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
COORD_PAIR_RE = re.compile(r"(-?\d{1,2}\.\d{4,}),\s*(-?\d{1,3}\.\d{4,})")
COUNT_PATTERNS = [
    re.compile(r"(\d{1,4})\s*(?:places?|장소|곳|件)", re.I),
    re.compile(r"(?:places?|장소|saved places?)\s*[:：]?\s*(\d{1,4})", re.I),
]


def clean_text(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    text = html.unescape(text)
    text = text.replace("\u200e", "").replace("\u202a", "").replace("\ufeff", "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"\\u003[dD]", "=", text)
    text = re.sub(r"\\u0026", "&", text)
    text = text.replace(r"\/", "/")
    text = " ".join(text.split())
    return text or None


def norm_text(value: Optional[str]) -> str:
    value = clean_text(value) or ""
    return re.sub(r"[^\w가-힣ぁ-んァ-ン一-龯]+", "", value.lower())


def normalize_google_url(url: str) -> str:
    url = html.unescape((url or "").strip()).replace(r"\/", "/")
    if url.startswith("/"):
        url = "https://www.google.com" + url
    url = re.sub(r"\\u003[dD]", "=", url)
    url = re.sub(r"\\u0026", "&", url)
    url = url.replace("&amp;", "&")
    return url.rstrip(".,;:!\")'}]")


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


def place_name_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        match = re.search(r"/maps/place/([^/@?]+)", parsed.path)
        if match:
            raw = unquote(match.group(1)).replace("+", " ")
            name = clean_text(raw)
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
    cid_match = re.search(r"[?&]cid=(\d+)", text)
    if cid_match:
        out["cid"] = cid_match.group(1)
    ftid_match = re.search(r"[?&]ftid=([^&]+)", text)
    if ftid_match:
        out["featureId"] = unquote(ftid_match.group(1))
    feature = FEATURE_ID_RE.search(text)
    if feature and not out["featureId"]:
        out["featureId"] = feature.group(0)
    place_id = re.search(r"ChI[A-Za-z0-9_-]{10,}", text)
    if place_id:
        out["placeId"] = place_id.group(0)
    return out


def maybe_category_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    if any(x in t for x in ("restaurant", "ramen", "sushi", "izakaya", "food", "bar & grill", "yakiniku", "curry")):
        return "restaurant"
    if any(x in t for x in ("cafe", "coffee", "bakery", "dessert", "roastery", "tea")):
        return "cafe"
    if any(x in t for x in ("hotel", "inn", "ryokan", "lodging", "hostel", "resort")):
        return "hotel"
    if any(x in t for x in ("station", "airport", "bus stop", "train", "terminal")):
        return "transport"
    if any(x in t for x in ("shop", "store", "market", "mall", "shopping", "department")):
        return "shopping"
    if any(x in t for x in ("park", "museum", "temple", "shrine", "garden", "tourist", "observation", "canal", "zoo")):
        return "sight"
    return None


def has_place_url(url: Optional[str]) -> bool:
    return bool(url and PLACE_URL_RE.search(url))


def reject_reason(candidate: Dict[str, Any], list_name: Optional[str], owner_name: Optional[str], strict: bool = True) -> Optional[str]:
    name = clean_text(candidate.get("name") or candidate.get("title"))
    url = candidate.get("googleMapsUrl") or candidate.get("url")
    raw = " ".join(str(candidate.get(k) or "") for k in ("name", "title", "address", "googleMapsUrl", "url", "rawText"))
    n_name = norm_text(name)
    n_list = norm_text(list_name)
    n_owner = norm_text(owner_name)

    if not name and not url:
        return "no name or url"
    if name and name.lower() in JUNK_NAMES:
        return "ui control, not a place"
    if n_list and n_name == n_list:
        return "saved-list title, not a place"
    if n_owner and n_name == n_owner:
        return "saved-list owner, not a place"
    if n_list and n_owner and n_name in {n_list + n_owner, n_owner + n_list}:
        return "combined saved-list title/owner, not a place"
    if n_list and n_owner and n_list in n_name and n_owner in n_name:
        return "saved-list metadata, not a place"
    if strict and not has_place_url(url):
        return "not a Google Maps place/cid/ftid URL"
    if name and len(name) > 140:
        return "name is too long; likely a text blob"
    if name and re.search(r"^(https?://|www\.|maps\.app\.goo\.gl)", name, re.I):
        return "url used as name"
    if raw and re.search(r"Hokkaido Trip\s+Taeun Kim", raw, re.I) and not has_place_url(url):
        return "known list metadata text"
    return None


def make_place_from_candidate(candidate: Dict[str, Any], list_name: Optional[str], owner_name: Optional[str], strict: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    href = normalize_google_url(candidate.get("href") or candidate.get("url") or "") if (candidate.get("href") or candidate.get("url")) else None
    aria = clean_text(candidate.get("aria"))
    text = clean_text(candidate.get("text"))
    raw_text = clean_text(candidate.get("rawText") or " ".join([href or "", aria or "", text or ""]))

    url_name = place_name_from_url(href)
    name = clean_text(candidate.get("name")) or url_name or aria
    address = clean_text(candidate.get("address"))

    if text:
        lines = [clean_text(x) for x in re.split(r"[\n\r]+|\\n", text) if clean_text(x)]
        if not name and lines:
            name = lines[0]
        if not address and len(lines) > 1:
            address = next((x for x in lines[1:7] if not re.search(r"stars?|reviews?|minutes?|closed|open|saved|share|directions", x, re.I)), None)

    if not name and href:
        name = url_name
    if name and len(name) > 140:
        name = re.split(r"(?: · |  |\\n)", name)[0][:140]

    lat, lng = extract_lat_lng_from_text(" ".join([href or "", raw_text or ""]))
    ids = extract_ids(" ".join([href or "", raw_text or ""]))
    if not href:
        if ids.get("cid"):
            href = f"https://www.google.com/maps?cid={ids['cid']}"
        elif ids.get("featureId"):
            href = f"https://www.google.com/maps/search/?api=1&query={quote_plus(ids['featureId'])}"
        elif lat is not None and lng is not None and not strict:
            href = f"https://www.google.com/maps/search/?api=1&query={lat:.7f},{lng:.7f}"

    place = {
        "id": stable_id(ids.get("placeId") or ids.get("cid") or ids.get("featureId") or href or f"{name}|{lat}|{lng}"),
        "placeId": ids.get("placeId"),
        "cid": ids.get("cid"),
        "featureId": ids.get("featureId"),
        "name": name,
        "title": name,
        "address": address,
        "category": maybe_category_from_text(name or "") or maybe_category_from_text(address or ""),
        "type": maybe_category_from_text(name or "") or maybe_category_from_text(address or ""),
        "googleMapsUrl": href,
        "url": href,
        "latitude": lat,
        "longitude": lng,
        "lat": lat,
        "lng": lng,
        "rating": None,
        "reviewCount": None,
        "phone": None,
        "website": None,
        "source": candidate.get("source") or "strict_dom",
        "listIndex": candidate.get("index"),
        "scrapedOk": None,
        "rawText": raw_text if candidate.get("debug") else None,
    }
    reason = reject_reason(place, list_name, owner_name, strict=strict)
    if reason:
        return None, reason
    return place, None


def merge_place(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if existing.get(key) in (None, "", [], {}):
            existing[key] = value
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
    return [seen[k] for k in ordered[:max_places]]


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
    labels = ["Accept all", "I agree", "Reject all", "Not now", "Skip", "Agree", "Accept"]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if await btn.count() > 0:
                await btn.first.click(timeout=1200)
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def get_page_metadata(page: Page, resolved_url: Optional[str] = None) -> Dict[str, Any]:
    visible_text = ""
    try:
        visible_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        pass
    lines = [clean_text(x) for x in re.split(r"[\n\r]+", visible_text or "") if clean_text(x)]

    list_name = None
    for selector in ["h1", '[role="main"] h1', '[aria-level="1"]', 'meta[property="og:title"]']:
        try:
            if selector.startswith("meta"):
                val = await page.locator(selector).first.get_attribute("content", timeout=1000)
                text = clean_text(val)
            else:
                text = await safe_text(page.locator(selector), timeout=1800)
            if text and text.lower() not in {"google maps", "maps"}:
                text = re.sub(r"\s*-\s*Google Maps\s*$", "", text, flags=re.I).strip()
                if text:
                    list_name = text
                    break
        except Exception:
            continue
    if not list_name:
        try:
            title = clean_text(await page.title())
            if title:
                title = re.sub(r"\s*-\s*Google Maps\s*$", "", title, flags=re.I).strip()
                if title and title.lower() not in {"google maps", "maps"}:
                    list_name = title
        except Exception:
            pass

    owner_name = None
    if list_name and lines:
        n_list = norm_text(list_name)
        for i, line in enumerate(lines[:80]):
            if norm_text(line) == n_list:
                for nxt in lines[i + 1:i + 6]:
                    if not nxt:
                        continue
                    if any(p.search(nxt) for p in COUNT_PATTERNS):
                        continue
                    if re.search(r"^(public|private|shared|edit|follow|directions|save)$", nxt, re.I):
                        continue
                    if len(nxt) <= 60:
                        owner_name = nxt
                        break
                break
    if not owner_name:
        for pattern in [r"(?:By|Created by|List by)\s+([^\n\r]{2,60})", r"(?:만든 사람|작성자)\s*[:：]?\s*([^\n\r]{2,60})"]:
            m = re.search(pattern, visible_text or "", re.I)
            if m:
                owner_name = clean_text(m.group(1))
                break

    expected_count = None
    for pattern in COUNT_PATTERNS:
        matches = [int(x) for x in pattern.findall(visible_text or "") if str(x).isdigit()]
        matches = [x for x in matches if 1 <= x <= 500]
        if matches:
            # On list pages, the visible saved-list count is usually the smallest prominent count.
            expected_count = min(matches)
            break

    return {
        "listName": list_name,
        "ownerName": owner_name,
        "expectedCount": expected_count,
        "visibleLinesPreview": lines[:35],
    }


async def collect_strict_list_candidates(page: Page) -> Dict[str, Any]:
    return await page.evaluate(
        r"""
        () => {
          const norm = (s) => (s || '').replace(/\u200e|\u202a|\ufeff/g, '').replace(/\s+/g, ' ').trim();
          const isPlaceHref = (href) => !!href && (/\/maps\/place\//i.test(href) || /[?&]cid=\d+/i.test(href) || /[?&]ftid=0x[0-9a-f]+:0x[0-9a-f]+/i.test(href));
          const titleFromHref = (href) => {
            try {
              const u = new URL(href, location.href);
              const m = u.pathname.match(/\/maps\/place\/([^/@?]+)/i);
              if (m) return decodeURIComponent(m[1].replace(/\+/g, ' '));
              const q = u.searchParams.get('q') || u.searchParams.get('query');
              if (q && !/^0x[0-9a-f]+:/i.test(q)) return decodeURIComponent(q.replace(/\+/g, ' '));
            } catch(e) {}
            return '';
          };
          const allContainers = Array.from(document.querySelectorAll('div[role="feed"], main, div[role="main"], section, div'));
          const scored = [];
          for (const el of allContainers) {
            const anchors = Array.from(el.querySelectorAll('a[href]')).filter(a => isPlaceHref(a.href || a.getAttribute('href')));
            if (!anchors.length) continue;
            const rect = el.getBoundingClientRect();
            const articleCount = el.querySelectorAll('div[role="article"], div[role="listitem"], div.Nv2PK').length;
            const scrollBonus = el.scrollHeight > el.clientHeight + 80 ? 6 : 0;
            const bodyPenalty = (el === document.body || el === document.documentElement) ? -100 : 0;
            const widthPenalty = rect.width > window.innerWidth * 0.95 ? -8 : 0;
            const score = anchors.length * 3 + articleCount * 2 + scrollBonus + widthPenalty + bodyPenalty;
            scored.push({el, score, anchorCount: anchors.length, articleCount, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, role: el.getAttribute('role') || '', className: String(el.className || '').slice(0, 120), rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}});
          }
          scored.sort((a,b) => b.score - a.score);
          let roots = scored.slice(0, 4).map(x => x.el);
          if (!roots.length) roots = [document];

          const rows = [];
          const seen = new Set();
          for (const root of roots) {
            const anchors = Array.from(root.querySelectorAll('a[href]')).filter(a => isPlaceHref(a.href || a.getAttribute('href')));
            for (const a of anchors) {
              const href = a.href || a.getAttribute('href') || '';
              if (!href || seen.has(href)) continue;
              seen.add(href);
              const card = a.closest('div[role="article"], div[role="listitem"], div.Nv2PK, div[jsaction*="mouseover:pane"], div[aria-label][role="button"]') || a.parentElement;
              const text = norm(card ? card.innerText : a.innerText);
              const aria = norm(a.getAttribute('aria-label') || (card ? card.getAttribute('aria-label') : '') || '');
              const name = norm(titleFromHref(href) || aria || (text ? text.split('\n')[0] : ''));
              const rect = (card || a).getBoundingClientRect();
              rows.push({
                href, aria, text, name,
                source: 'strict_dom_place_anchor',
                index: rows.length + 1,
                rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
              });
            }
          }
          return {
            url: location.href,
            title: document.title,
            rows,
            containerCandidates: scored.slice(0, 8).map(({el, ...rest}) => rest),
            visibleTextLength: document.body ? document.body.innerText.length : 0
          };
        }
        """
    )


async def scroll_best_list_container(page: Page, rounds: int = 44, expected_count: Optional[int] = None) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    last_count = 0
    stable = 0
    for i in range(rounds):
        try:
            data = await collect_strict_list_candidates(page)
            count = len(data.get("rows") or [])
            snapshots.append({"round": i, "candidateRows": count, "containerCandidates": data.get("containerCandidates", [])[:3]})
            if expected_count and count >= expected_count:
                break
            if count <= last_count:
                stable += 1
            else:
                stable = 0
                last_count = count
            if stable >= 9 and count > 0:
                break
        except Exception:
            pass
        try:
            await page.evaluate(
                r"""
                () => {
                  const isPlaceHref = (href) => !!href && (/\/maps\/place\//i.test(href) || /[?&]cid=\d+/i.test(href) || /[?&]ftid=0x[0-9a-f]+:0x[0-9a-f]+/i.test(href));
                  const candidates = Array.from(document.querySelectorAll('div[role="feed"], main, div[role="main"], section, div'))
                    .map(el => {
                      const anchors = Array.from(el.querySelectorAll('a[href]')).filter(a => isPlaceHref(a.href || a.getAttribute('href'))).length;
                      const rect = el.getBoundingClientRect();
                      const scrollBonus = el.scrollHeight > el.clientHeight + 80 ? 6 : 0;
                      const bodyPenalty = (el === document.body || el === document.documentElement) ? -100 : 0;
                      const widthPenalty = rect.width > window.innerWidth * 0.95 ? -8 : 0;
                      return {el, score: anchors * 3 + scrollBonus + widthPenalty + bodyPenalty, anchors};
                    })
                    .filter(x => x.anchors > 0)
                    .sort((a,b) => b.score - a.score);
                  for (const c of candidates.slice(0, 4)) {
                    c.el.scrollTop = c.el.scrollTop + Math.max(900, Math.floor(c.el.clientHeight * 0.85));
                    c.el.dispatchEvent(new Event('scroll', {bubbles:true}));
                    c.el.dispatchEvent(new WheelEvent('wheel', {deltaY: 1200, bubbles:true}));
                  }
                  window.scrollBy(0, 1000);
                }
                """
            )
            await page.keyboard.press("PageDown")
        except Exception:
            pass
        await page.wait_for_timeout(900)
    return snapshots


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
    debug_info: Dict[str, Any] = {"version": APP_VERSION, "rounds": [], "acceptedPreview": [], "rejectedPreview": []} if req.debug else {}

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

        try:
            try:
                await page.goto(source_url, wait_until="domcontentloaded", timeout=55_000)
            except PlaywrightTimeoutError:
                warnings.append("Google Maps took too long to load; using partial data collected so far.")
            await page.wait_for_timeout(2600)
            await handle_google_dialogs(page)
            resolved_url = page.url

            metadata = await get_page_metadata(page, resolved_url)
            list_name = metadata.get("listName")
            owner_name = metadata.get("ownerName")
            expected_count = metadata.get("expectedCount")
            if req.debug:
                debug_info["metadata"] = metadata

            # Initial harvest, then strict scrolling of the saved-list panel.
            first_data = await collect_strict_list_candidates(page)
            if req.debug:
                debug_info["initialContainerCandidates"] = first_data.get("containerCandidates", [])[:8]
                debug_info["initialRawRows"] = (first_data.get("rows") or [])[:20]

            if time.monotonic() < deadline:
                snapshots = await scroll_best_list_container(page, rounds=46, expected_count=expected_count)
                if req.debug:
                    debug_info["rounds"] = snapshots[:60]
                await page.wait_for_timeout(1200)

            final_data = await collect_strict_list_candidates(page)
            raw_candidates = final_data.get("rows") or []

            accepted: List[Dict[str, Any]] = []
            rejected: List[Dict[str, Any]] = []
            for c in raw_candidates:
                c["debug"] = bool(req.debug)
                place, reason = make_place_from_candidate(c, list_name, owner_name, strict=req.strictListOnly)
                if place:
                    accepted.append(place)
                else:
                    rejected.append({"reason": reason, "name": c.get("name"), "href": c.get("href"), "text": (c.get("text") or "")[:220]})

            raw_count = len(raw_candidates)
            places = dedupe_places(accepted, req.maxPlacesPerList)

            # If Google repeats place anchors across nested containers, final dedupe should land on the saved-list count.
            if expected_count and len(places) > expected_count:
                warnings.append(
                    f"Extracted {len(places)} unique place-like links, but the visible saved-list count is {expected_count}. "
                    "Returning the first saved-list rows only to avoid nearby/search/recommendation spillover."
                )
                places = places[:expected_count]

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
                    "No saved-list place rows were extracted. The list may be private, Google may have served a consent/CAPTCHA page, or the Render IP may be blocked."
                )
            elif expected_count and len(places) < expected_count:
                warnings.append(
                    f"Only {len(places)} of the visible {expected_count} saved-list places were extracted. This usually means the list panel did not finish lazy-loading."
                )
            elif expected_count and len(places) == expected_count:
                warnings.append(f"Matched visible saved-list count: {expected_count} places.")

            # Remove internal rawText field before normal app response unless debug enabled.
            if not req.debug:
                for p in places:
                    p.pop("rawText", None)

            if req.debug:
                debug_info["finalContainerCandidates"] = final_data.get("containerCandidates", [])[:8]
                debug_info["rawCandidateCount"] = raw_count
                debug_info["acceptedBeforeDedupe"] = len(accepted)
                debug_info["rejectedCount"] = len(rejected)
                debug_info["acceptedPreview"] = [
                    {"name": p.get("name"), "url": p.get("googleMapsUrl"), "source": p.get("source"), "listIndex": p.get("listIndex")}
                    for p in places[:60]
                ]
                debug_info["rejectedPreview"] = rejected[:60]
                debug_info["strictListOnly"] = req.strictListOnly

            return ScrapeResponse(
                ok=bool(places),
                listName=list_name,
                ownerName=owner_name,
                expectedCount=expected_count,
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
                ownerName=None,
                expectedCount=None,
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
    return {"ok": True, "status": "ok", "version": APP_VERSION}


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
    strictListOnly: bool = Query(True),
) -> ScrapeResponse:
    try:
        req = ScrapeRequest(
            listUrl=url,
            maxPlacesPerList=maxPlacesPerList,
            scrapeDetails=scrapeDetails,
            debug=debug,
            strictListOnly=strictListOnly,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await scrape_google_shared_list(req)


@app.post("/api/debug-google-list", response_model=ScrapeResponse)
async def debug_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    req.debug = True
    req.scrapeDetails = False
    req.strictListOnly = True
    return await scrape_google_shared_list(req)


@app.get("/api/debug-google-list", response_model=ScrapeResponse)
async def debug_get(
    url: str = Query(..., description="Google Maps shared-list URL"),
    maxPlacesPerList: int = Query(500, ge=1, le=500),
) -> ScrapeResponse:
    req = ScrapeRequest(listUrl=url, maxPlacesPerList=maxPlacesPerList, debug=True, scrapeDetails=False, strictListOnly=True)
    return await scrape_google_shared_list(req)


@app.get("/debug", response_class=HTMLResponse)
async def debug_page() -> str:
    return """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Google Saved List Debug</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:18px;background:#fafafa;color:#111}textarea,input,button{font:inherit}textarea{width:100%;height:90px;border:1px solid #ccc;border-radius:12px;padding:12px;box-sizing:border-box}button{margin-top:10px;border:0;border-radius:12px;background:#111;color:#fff;padding:12px 16px;font-weight:700}.card{background:#fff;border:1px solid #e5e5e5;border-radius:16px;padding:14px;margin:14px 0;box-shadow:0 1px 4px rgba(0,0,0,.04)}.row{padding:9px 0;border-bottom:1px solid #eee}.small{font-size:13px;color:#666;word-break:break-all}pre{white-space:pre-wrap;word-break:break-word;background:#111;color:#eee;border-radius:12px;padding:12px;max-height:420px;overflow:auto}.bad{color:#b42318}.good{color:#067647}</style>
</head>
<body>
<h2>Google Saved List Debug</h2>
<p class="small">Saved list URL을 붙여넣고 실행하면, 실제로 어떤 항목을 장소로 받아들이고 어떤 항목을 버렸는지 보여줘요.</p>
<textarea id="url" placeholder="https://maps.app.goo.gl/... 또는 Google Maps saved list URL"></textarea>
<button onclick="run()">Run debug</button>
<div id="out"></div>
<script>
async function run(){
  const out = document.getElementById('out');
  const url = document.getElementById('url').value.trim();
  if(!url){ out.innerHTML='<p class="bad">URL을 넣어줘.</p>'; return; }
  out.innerHTML='<div class="card">가져오는 중... Render free plan이면 1분 정도 걸릴 수 있어.</div>';
  try{
    const res = await fetch('/api/debug-google-list', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({listUrl:url, maxPlacesPerList:500, debug:true, scrapeDetails:false, strictListOnly:true})});
    const data = await res.json();
    const accepted = (data.debug && data.debug.acceptedPreview) || [];
    const rejected = (data.debug && data.debug.rejectedPreview) || [];
    out.innerHTML = `
      <div class="card">
        <div><b>List:</b> ${escapeHtml(data.listName||'')}</div>
        <div><b>Owner:</b> ${escapeHtml(data.ownerName||'')}</div>
        <div><b>Visible count:</b> ${data.expectedCount ?? ''}</div>
        <div><b>Returned places:</b> <span class="good">${data.count}</span></div>
        <div><b>Raw candidates:</b> ${data.rawItemCount}</div>
        <div class="small">${(data.warnings||[]).map(escapeHtml).join('<br>')}</div>
      </div>
      <div class="card"><h3>Accepted places</h3>${accepted.map((p,i)=>`<div class="row"><b>${i+1}. ${escapeHtml(p.name||'')}</b><div class="small">${escapeHtml(p.url||'')}</div></div>`).join('') || '<div class="small">None</div>'}</div>
      <div class="card"><h3>Rejected / ignored</h3>${rejected.map((p,i)=>`<div class="row"><b>${i+1}. ${escapeHtml(p.name||'')}</b><div class="small bad">${escapeHtml(p.reason||'')}</div><div class="small">${escapeHtml(p.href||'')}</div></div>`).join('') || '<div class="small">None</div>'}</div>
      <div class="card"><h3>Full JSON</h3><pre>${escapeHtml(JSON.stringify(data,null,2))}</pre></div>`;
  }catch(e){ out.innerHTML='<p class="bad">Failed: '+escapeHtml(String(e))+'</p>'; }
}
function escapeHtml(s){return String(s??'').replace(/[&<>'"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
</script>
</body>
</html>
"""
