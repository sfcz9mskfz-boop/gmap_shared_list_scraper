async function importGoogleSavedList(listUrl) {
  const res = await fetch('https://gmap-shared-list-scraper.onrender.com/api/import-google-list', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      listUrl,
      maxPlacesPerList: 500,
      scrapeDetails: false,
      strictListOnly: true
    })
  });
  if (!res.ok) throw new Error('Google saved-list import failed');
  return await res.json();
}
