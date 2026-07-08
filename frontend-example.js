async function importGoogleSavedList(listUrl) {
  const endpoint = 'https://gmap-shared-list-scraper.onrender.com/api/import-google-list';
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      listUrl,
      maxPlacesPerList: 500,
      scrapeDetails: false
    })
  });
  const data = await response.json();
  if (!response.ok || !data.ok) {
    throw new Error((data.warnings && data.warnings.join('\n')) || data.detail || 'Import failed');
  }
  return data;
}
