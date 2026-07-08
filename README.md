# Google Maps Shared List Scraper - ParseForge-like replica

This is a self-hosted FastAPI + Playwright backend for importing public Google Maps shared lists into the Sapporo itinerary app.

It is more ParseForge-like than the previous version because it does **three** extraction passes:

1. Resolves `maps.app.goo.gl/...` short links in Chromium.
2. Parses visible Google Maps list rows after repeated lazy-load scrolling.
3. Parses hidden Google Maps page/network payloads for place URLs, feature IDs, CIDs, and coordinates.

It returns the app-compatible shape:

```json
{
  "ok": true,
  "listName": "...",
  "count": 43,
  "places": [
    {
      "name": "...",
      "googleMapsUrl": "...",
      "lat": 43.06,
      "lng": 141.35,
      "address": "...",
      "category": "restaurant"
    }
  ]
}
```

## Deploy update on Render

Upload/replace these files at the **root** of your GitHub scraper repository:

- `main.py`
- `requirements.txt`
- `Dockerfile`
- `render.yaml`
- `README.md`

Then Render should auto-deploy. If not, open Render and use:

**Manual Deploy → Deploy latest commit**

After deploy, test:

```txt
https://gmap-shared-list-scraper.onrender.com/health
```

It should return:

```json
{"ok":true,"status":"ok","version":"2.0.0"}
```

## App endpoint

Your app can continue using:

```txt
https://gmap-shared-list-scraper.onrender.com/api/import-google-list
```

No app change is required if your v55/v56 app already points to that URL.

## Manual test in browser

Use a GET test URL like this:

```txt
https://gmap-shared-list-scraper.onrender.com/api/import-google-list?url=PASTE_ENCODED_GOOGLE_MAPS_LIST_URL&debug=true
```

Or use the `/docs` page:

```txt
https://gmap-shared-list-scraper.onrender.com/docs
```

## Optional proxy

Google sometimes blocks Render's free datacenter IPs. If the endpoint returns 0 places or only a consent/CAPTCHA page, add a proxy in Render environment variables:

```txt
PROXY_SERVER=http://host:port
PROXY_USERNAME=optional_username
PROXY_PASSWORD=optional_password
```

Without a reliable proxy, no self-hosted Google Maps scraper can fully match paid Apify/ParseForge reliability.
