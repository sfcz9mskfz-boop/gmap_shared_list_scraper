# Google Maps Shared List Scraper — Phone/App Integrated

This is the self-hosted backend for your Sapporo iPhone HTML app.

It exposes two identical endpoints:

- `POST /api/import-google-list` ← use this in your app
- `POST /scrape-google-list` ← direct test endpoint

The response matches what your app expects:

```json
{
  "ok": true,
  "listName": "My Saved Places",
  "sourceUrl": "https://maps.app.goo.gl/...",
  "count": 43,
  "places": [
    {
      "name": "Place name",
      "googleMapsUrl": "https://www.google.com/maps/place/...",
      "latitude": 43.0,
      "longitude": 141.0
    }
  ],
  "warnings": []
}
```

## Phone setup

You cannot run Playwright/Chromium directly inside the iPhone HTML app. Deploy this folder as a backend, then paste the backend URL into the app gear/settings field.

Recommended endpoint to paste into the app:

```txt
https://YOUR-RENDER-APP.onrender.com/api/import-google-list
```

## Deploy on Render from your phone

1. Download and unzip this package in the iPhone Files app.
2. Open GitHub in Safari/Chrome, create a new repo, then upload these files to the repo root:
   - `main.py`
   - `requirements.txt`
   - `Dockerfile`
   - `render.yaml`
3. Open Render, create a new Web Service from that GitHub repo.
4. Use Docker environment.
5. Health check path: `/health`.
6. Deploy.
7. Copy your Render URL and append `/api/import-google-list`.
8. In the app: Google saved lists → tap ⚙️ → paste the endpoint → paste Google Maps shared list link → Import list.

## Local setup, optional

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Test

```bash
curl -X POST https://YOUR-RENDER-APP.onrender.com/api/import-google-list \
  -H "Content-Type: application/json" \
  -d '{
    "listUrl":"https://maps.app.goo.gl/YOUR_LIST",
    "maxPlacesPerList":500,
    "scrapeDetails":false,
    "headless":true
  }'
```

## Notes

- `scrapeDetails` defaults to `false` for faster phone/app importing.
- Set `scrapeDetails:true` only if you need address/rating/phone/website.
- This is best-effort because Google Maps markup changes and may block automated browsers.
