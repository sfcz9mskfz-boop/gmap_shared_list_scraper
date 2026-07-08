import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmaps-list-scraper")

app = FastAPI(title="Google Maps Shared List Scraper Replica")

# For local testing you can leave this open. In production, replace "*" with your site URL.
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


class ScrapeResponse(BaseModel):
    ok: bool
    listName: Optional[str]
    sourceUrl: str
    count: int
    places: List[Dict[str, Any]]
    warnings: List[str] = []


def clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = " ".join(value.replace("\u200e", "").replace("\u202a", "").split())
    return cleaned or None


def extract_lat_lng(url: str) -> Dict[str, Optional[float]]:
    patterns = [
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return {
                "latitude": float(match.group(1)),
                "longitude": float(match.group(2)),
            }
    return {"latitude": None, "longitude": None}


def normalize_google_url(url: str) -> str:
    if url.startswith("/"):
        return "https://www.google.com" + url
    return url


def name_from_maps_url(url: str) -> Optional[str]:
    try:
        match = re.search(r"/maps/place/([^/@?]+)", url)
        if not match:
            return None
        return clean_text(unquote(match.group(1)).replace("+", " "))
    except Exception:
        return None


async def safe_text(locator, timeout: int = 1500) -> Optional[str]:
    try:
        if await locator.count() > 0:
            text = await locator.first.text_content(timeout=timeout)
            return clean_text(text)
    except Exception:
        return None
    return None


async def safe_attr(locator, attr: str, timeout: int = 1500) -> Optional[str]:
    try:
        if await locator.count() > 0:
            return await locator.first.get_attribute(attr, timeout=timeout)
    except Exception:
        return None
    return None


async def click_if_visible(page, selector_texts: List[str]) -> None:
    for text in selector_texts:
        try:
            button = page.get_by_role("button", name=re.compile(text, re.I))
            if await button.count() > 0:
                await button.first.click(timeout=1500)
                await page.wait_for_timeout(700)
        except Exception:
            pass


async def handle_google_dialogs(page) -> None:
    await click_if_visible(page, [
        "Accept all",
        "I agree",
        "Reject all",
        "Not now",
        "Skip",
    ])


async def get_list_name(page) -> Optional[str]:
    selectors = [
        "h1",
        '[role="main"] h1',
        '[aria-level="1"]',
    ]
    for selector in selectors:
        text = await safe_text(page.locator(selector), timeout=2500)
        if text and text.lower() not in {"google maps", "maps"}:
            return text

    try:
        title = clean_text(await page.title())
        if title:
            title = re.sub(r"\s*-\s*Google Maps\s*$", "", title).strip()
            return title or None
    except Exception:
        pass
    return None


async def collect_place_links(page, max_places: int) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    stagnant_rounds = 0
    previous_count = 0

    # Google lists lazy-load. Scroll repeatedly until no new place links appear.
    for _ in range(100):
        link_selectors = [
            'a[href*="/maps/place/"]',
            'a[href*="google.com/maps/place/"]',
            'a[href*="maps?cid="]',
        ]

        for selector in link_selectors:
            anchors = await page.locator(selector).all()
            for anchor in anchors:
                try:
                    href = await anchor.get_attribute("href")
                    if not href:
                        continue
                    href = normalize_google_url(href)

                    # Prefer /maps/place/ links because they contain readable names/coords.
                    if "/maps/place/" not in href and "maps?cid=" not in href:
                        continue

                    label = clean_text(await anchor.get_attribute("aria-label"))
                    text = clean_text(await anchor.text_content())
                    name = label or text or name_from_maps_url(href)

                    # Avoid junk labels that are clearly not place cards.
                    if name and name.lower() in {"directions", "save", "share", "nearby", "send to phone"}:
                        continue

                    key = href.split("?")[0]
                    if key not in seen:
                        coords = extract_lat_lng(href)
                        seen[key] = {
                            "name": name,
                            "address": None,
                            "rating": None,
                            "reviewCount": None,
                            "type": None,
                            "phone": None,
                            "website": None,
                            "googleMapsUrl": href,
                            "latitude": coords["latitude"],
                            "longitude": coords["longitude"],
                        }

                    if len(seen) >= max_places:
                        return list(seen.values())[:max_places]

                except Exception:
                    continue

        current_count = len(seen)
        if current_count == previous_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        previous_count = current_count

        if stagnant_rounds >= 10 and current_count > 0:
            break

        # Scroll both page and possible list/feed containers.
        try:
            await page.mouse.wheel(0, 2500)
        except Exception:
            pass

        for selector in ['div[role="feed"]', 'div[role="main"]', '.m6QErb[aria-label]']:
            try:
                containers = await page.locator(selector).all()
                for container in containers[:4]:
                    await container.evaluate("el => el.scrollBy(0, 2500)")
            except Exception:
                pass

        await page.wait_for_timeout(850)

    return list(seen.values())[:max_places]


def parse_number_from_text(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"[\d,]+", text)
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except Exception:
        return None


def parse_rating(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


async def scrape_place_details(context, place: Dict[str, Any], index: int) -> Dict[str, Any]:
    page = await context.new_page()
    try:
        await page.goto(place["googleMapsUrl"], wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2400)
        await handle_google_dialogs(page)

        name = await safe_text(page.locator("h1"), timeout=2500)
        if name:
            place["name"] = name

        # Rating and review count selectors change often. These are best-effort.
        rating_text = await safe_text(page.locator('div.F7nice span[aria-hidden="true"]'), timeout=1200)
        place["rating"] = parse_rating(rating_text)

        review_text = await safe_text(page.locator('button[aria-label*="review" i], span[aria-label*="review" i]'), timeout=1200)
        place["reviewCount"] = parse_number_from_text(review_text)

        address = await safe_attr(
            page.locator('button[data-item-id="address"], button[aria-label^="Address:" i]'),
            "aria-label",
            timeout=1500,
        )
        if address:
            place["address"] = clean_text(re.sub(r"^Address:\s*", "", address, flags=re.I))

        phone = await safe_attr(
            page.locator('button[data-item-id^="phone:"], button[aria-label^="Phone:" i]'),
            "aria-label",
            timeout=1500,
        )
        if phone:
            place["phone"] = clean_text(re.sub(r"^Phone:\s*", "", phone, flags=re.I))

        website = await safe_attr(
            page.locator('a[data-item-id="authority"], a[aria-label^="Website:" i]'),
            "href",
            timeout=1500,
        )
        if website:
            place["website"] = website

        category = await safe_text(
            page.locator('button[jsaction*="pane.rating.category"], button[aria-label*="Category" i]'),
            timeout=1200,
        )
        if category:
            place["type"] = category

        coords = extract_lat_lng(page.url)
        if coords["latitude"] is not None and coords["longitude"] is not None:
            place["latitude"] = coords["latitude"]
            place["longitude"] = coords["longitude"]

        place["scrapedOk"] = True
        return place

    except Exception as exc:
        # Do not fail the whole list because one place failed.
        place["scrapedOk"] = False
        place["warning"] = f"Details unavailable for item {index + 1}"
        logger.warning("Failed to scrape place detail %s: %s", index + 1, exc)
        return place

    finally:
        await page.close()


async def scrape_google_shared_list(
    list_url: str,
    max_places: int,
    scrape_details: bool,
    headless: bool,
) -> ScrapeResponse:
    warnings: List[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        page = await context.new_page()

        try:
            try:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                warnings.append("Google Maps took too long to load. Returning anything collected so far.")

            await page.wait_for_timeout(3000)
            await handle_google_dialogs(page)

            list_name = await get_list_name(page)
            places = await collect_place_links(page, max_places)

            if not places:
                warnings.append(
                    "No place links were found. Make sure the Google Maps list is public/shared and not private."
                )

            if scrape_details and places:
                # Limit concurrency to reduce blocking and avoid hammering Google.
                semaphore = asyncio.Semaphore(3)

                async def limited_detail(i: int, p_: Dict[str, Any]) -> Dict[str, Any]:
                    async with semaphore:
                        return await scrape_place_details(context, p_, i)

                places = await asyncio.gather(
                    *(limited_detail(i, place) for i, place in enumerate(places))
                )

            return ScrapeResponse(
                ok=len(places) > 0,
                listName=list_name,
                sourceUrl=list_url,
                count=len(places),
                places=places,
                warnings=warnings,
            )

        except Exception as exc:
            logger.exception("Scrape failed")
            return ScrapeResponse(
                ok=False,
                listName=None,
                sourceUrl=list_url,
                count=0,
                places=[],
                warnings=["Scrape failed. Check that the list URL is public and try again."],
            )

        finally:
            await browser.close()


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


async def run_scrape_request(req: ScrapeRequest) -> ScrapeResponse:
    return await scrape_google_shared_list(
        list_url=str(req.listUrl),
        max_places=req.maxPlacesPerList,
        scrape_details=req.scrapeDetails,
        headless=req.headless,
    )


@app.post("/scrape-google-list", response_model=ScrapeResponse)
async def scrape_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    return await run_scrape_request(req)


# App-compatible alias. Your Sapporo HTML app can point directly to:
# https://YOUR-RENDER-APP.onrender.com/api/import-google-list
@app.post("/api/import-google-list", response_model=ScrapeResponse)
async def app_import_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    return await run_scrape_request(req)
