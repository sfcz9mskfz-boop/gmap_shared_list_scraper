// Paste your deployed backend URL into your app gear/settings field like this:
// https://YOUR-RENDER-APP.onrender.com/api/import-google-list

async function scrapeGoogleList(listUrl) {
  const API_ENDPOINT = "https://YOUR-RENDER-APP.onrender.com/api/import-google-list";

  const response = await fetch(API_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      listUrl,
      maxPlacesPerList: 500,
      // false is faster and enough for the app to import place names/links.
      // Set true only if you really need address/rating/phone/website.
      scrapeDetails: false,
      headless: true,
    }),
  });

  const data = await response.json();

  return {
    listName: data.listName || "Google Saved List",
    places: data.places || [],
    count: data.count || 0,
    warnings: data.warnings || [],
  };
}
