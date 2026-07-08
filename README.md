# Google Maps Shared List Scraper - Strict v4

This version keeps the strict saved-list logic from v3, and adds a visual row click fallback for cases where Google shows the list shell/count but does not expose place anchors in the DOM.

## What changed in v3

- Does **not** scan the entire Google Maps hidden payload by default.
- Does **not** turn list metadata such as `Hokkaido Trip Taeun Kim` into a place.
- Extracts only saved-list rows with real Google Maps place/cid/ftid links.
- Reads list metadata separately: `listName`, `ownerName`, and visible count such as `43 places`.
- If Google returns more unique place-like links than the visible saved-list count, v3 trims to the visible count to avoid nearby/search/recommendation spillover.
- Adds `/debug`, a phone-friendly debug page showing accepted and rejected candidates.
- Adds visual row click fallback: if no place links are visible, it clicks each visible saved-list row and reads the resulting place detail page.
- Debug now shows `visualRounds` so you can see whether Google rendered rows at all.

## Endpoints

### App endpoint

```txt
POST /api/import-google-list
```

Body:

```json
{
  "listUrl": "https://maps.app.goo.gl/...",
  "maxPlacesPerList": 500,
  "scrapeDetails": false,
  "strictListOnly": true,
  "clickFallback": true
}
```

### Debug page

Open this on your phone:

```txt
https://YOUR-RENDER-SERVICE.onrender.com/debug
```

Paste your Google Maps saved-list link and run. It will show:

- saved list name
- owner name
- visible count
- returned places
- raw candidates
- accepted places
- rejected/ignored items with reasons

### Health check

```txt
GET /health
```

Expected:

```json
{"ok": true, "status": "ok", "version": "4.0.0-strict-list-click-fallback"}
```

## Deploy on Render

Upload these files to the root of your GitHub repository:

```txt
main.py
requirements.txt
Dockerfile
render.yaml
README.md
```

Then in Render:

```txt
Manual Deploy -> Deploy latest commit
```
