/**
 * SI Merge Background Service Worker
 *
 * Orchestrates the merge workflow with three PDF download strategies:
 * 1. chrome.scripting.executeScript in MAIN world (bypasses CSP, uses user cookies)
 * 2. Service worker fetch (works for some open-access publishers)
 * 3. Backend DOI-based download (last resort)
 *
 * Also downloads SI files in the page context when the backend cannot
 * (e.g. Science.org Cloudflare Turnstile).
 */

const DEFAULT_BACKEND = 'https://si-merge.onrender.com';

async function getBackendUrl() {
  try {
    const data = await chrome.storage.sync.get({ backendUrl: DEFAULT_BACKEND });
    return data.backendUrl.replace(/\/+$/, '');
  } catch {
    return DEFAULT_BACKEND;
  }
}

function broadcastToTab(tabId, message) {
  chrome.tabs.sendMessage(tabId, message).catch(() => {});
}

function broadcastProgress(tabId, step, status, detail) {
  broadcastToTab(tabId, { type: 'MERGE_PROGRESS', step, status, detail });
  chrome.runtime.sendMessage({ type: 'MERGE_PROGRESS', step, status, detail }).catch(() => {});
}

/**
 * Fetch a file in the page's MAIN world using chrome.scripting.executeScript.
 * Bypasses CSP and Cloudflare Turnstile by using the user's actual session.
 * Returns a Uint8Array of the data, or null on failure.
 */
async function fetchInPageContext(tabId, fileUrl) {
  if (!fileUrl || !tabId) return null;

  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      args: [fileUrl],
      func: async (url) => {
        try {
          const resp = await fetch(url, { credentials: 'same-origin' });
          if (!resp.ok) return { error: `HTTP ${resp.status}` };
          const buf = await resp.arrayBuffer();
          return { data: Array.from(new Uint8Array(buf)) };
        } catch (e) {
          return { error: e.message };
        }
      },
    });

    const result = results?.[0]?.result;
    if (!result || result.error || !result.data) return null;

    const bytes = new Uint8Array(result.data);
    if (bytes.length < 500) return null;

    return bytes;
  } catch (err) {
    console.log('[SI Merge] Page-context fetch failed:', err.message);
    return null;
  }
}

/**
 * Fetch PDF in page context with validation that it's actually a PDF.
 */
async function fetchPdfInPageContext(tabId, pdfUrl) {
  const bytes = await fetchInPageContext(tabId, pdfUrl);
  if (!bytes) return null;

  const header = new TextDecoder().decode(bytes.slice(0, 5));
  if (!header.startsWith('%PDF')) return null;

  return bytes;
}

async function fetchPdfFromServiceWorker(pdfUrl, articleUrl) {
  if (!pdfUrl) return null;

  try {
    const resp = await fetch(pdfUrl, {
      credentials: 'include',
      headers: {
        'Accept': 'application/pdf,*/*',
        'Referer': articleUrl || pdfUrl,
      },
    });

    if (!resp.ok) return null;

    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    const blob = await resp.blob();

    if (blob.size < 1000) return null;

    const header = await blob.slice(0, 5).text();
    if (!header.startsWith('%PDF') && contentType.includes('html')) return null;

    return blob;
  } catch {
    return null;
  }
}

async function uploadPdfToBackend(pdfBlob, doi, siBlobs) {
  const base = await getBackendUrl();
  const form = new FormData();
  form.append('file', pdfBlob, 'article.pdf');
  if (doi) form.append('doi', doi);

  if (siBlobs && siBlobs.length > 0) {
    for (let i = 0; i < siBlobs.length; i++) {
      form.append('si_files', siBlobs[i], `si_${i + 1}.pdf`);
    }
  }

  const resp = await fetch(`${base}/api/tasks`, {
    method: 'POST',
    body: form,
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `Backend error ${resp.status}`);
  }
  return resp.json();
}

async function mergeByDoi(doi) {
  const base = await getBackendUrl();
  const resp = await fetch(`${base}/api/merge-by-doi`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ doi }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `Backend error ${resp.status}`);
  }
  return resp.json();
}

function listenProgress(taskId, tabId) {
  return new Promise(async (resolve, reject) => {
    const base = await getBackendUrl();
    const source = new EventSource(`${base}/api/tasks/${taskId}/events`);

    source.addEventListener('progress', (e) => {
      try {
        const ev = JSON.parse(e.data);
        broadcastProgress(tabId, ev.step, ev.status, ev.detail);
      } catch {}
    });

    source.addEventListener('complete', (e) => {
      source.close();
      try {
        resolve(JSON.parse(e.data));
      } catch (err) {
        reject(err);
      }
    });

    source.addEventListener('error', (e) => {
      try {
        const ev = JSON.parse(e.data);
        source.close();
        reject(new Error(ev.error || 'Unknown error'));
      } catch {
        if (source.readyState === EventSource.CLOSED) return;
        source.close();
        reject(new Error('Connection to server lost'));
      }
    });
  });
}

async function handleMerge(payload, tabId) {
  const { doi, pdfUrl, siUrls, articleUrl, title } = payload;

  try {
    let task;
    let pdfBlob = null;
    const siBlobs = [];

    // --- Download article PDF ---
    broadcastProgress(tabId, 1, 'started', 'Downloading article PDF...');
    const pageData = await fetchPdfInPageContext(tabId, pdfUrl);

    if (pageData) {
      pdfBlob = new Blob([pageData], { type: 'application/pdf' });
      broadcastProgress(tabId, 1, 'done', `PDF downloaded (${Math.round(pageData.length / 1024)} KB)`);
    }

    if (!pdfBlob) {
      const swBlob = await fetchPdfFromServiceWorker(pdfUrl, articleUrl);
      if (swBlob) {
        pdfBlob = swBlob;
        broadcastProgress(tabId, 1, 'done', `PDF downloaded (${Math.round(swBlob.size / 1024)} KB)`);
      }
    }

    // --- Download SI files in page context ---
    if (pdfBlob && siUrls && siUrls.length > 0) {
      broadcastProgress(tabId, 3, 'started', `Downloading ${siUrls.length} SI file(s)...`);
      for (let i = 0; i < siUrls.length; i++) {
        const siData = await fetchInPageContext(tabId, siUrls[i]);
        if (siData && siData.length > 500) {
          siBlobs.push(new Blob([siData], { type: 'application/pdf' }));
          broadcastProgress(tabId, 3, 'downloading',
            `SI ${i + 1}/${siUrls.length} downloaded (${Math.round(siData.length / 1024)} KB)`);
        }
      }
      if (siBlobs.length > 0) {
        broadcastProgress(tabId, 3, 'done', `Downloaded ${siBlobs.length} SI file(s)`);
      }
    }

    // --- Upload to backend ---
    if (pdfBlob) {
      broadcastProgress(tabId, 1, 'uploading', 'Uploading to server...');
      task = await uploadPdfToBackend(pdfBlob, doi, siBlobs);
    } else {
      broadcastProgress(tabId, 1, 'searching', 'Using server-side download...');
      task = await mergeByDoi(doi);
    }

    const taskId = task.task_id;
    const result = await listenProgress(taskId, tabId);

    const base = await getBackendUrl();
    const downloadUrl = `${base}/api/tasks/${taskId}/download`;
    const filename = title
      ? `${title.substring(0, 80).replace(/[/\\?%*:|"<>]/g, '_')}_with_SI.pdf`
      : 'article_with_SI.pdf';

    chrome.downloads.download({ url: downloadUrl, filename });

    broadcastToTab(tabId, { type: 'MERGE_COMPLETE', result });
    chrome.runtime.sendMessage({ type: 'MERGE_COMPLETE', result, taskId }).catch(() => {});

  } catch (err) {
    const errorMsg = err.message || 'Merge failed';
    broadcastToTab(tabId, { type: 'MERGE_ERROR', error: errorMsg });
    chrome.runtime.sendMessage({ type: 'MERGE_ERROR', error: errorMsg }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'START_MERGE') {
    const tabId = sender.tab ? sender.tab.id : msg.tabId;
    handleMerge(msg.payload, tabId);
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === 'START_MERGE_BY_DOI') {
    const tabId = msg.tabId || 0;
    handleMerge({ doi: msg.doi, pdfUrl: null, siUrls: [], articleUrl: null, title: null }, tabId);
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === 'GET_ARTICLE_META') {
    chrome.storage.session.get('articleMeta', (data) => {
      sendResponse(data.articleMeta || null);
    });
    return true;
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete' || !tab.url) return;

  const PUBLISHER_DOMAINS = [
    'nature.com', 'springer.com', 'pubs.acs.org', 'wiley.com',
    'sciencedirect.com', 'elsevier.com', 'science.org', 'pnas.org',
    'rsc.org', 'aps.org',
  ];

  const isArticle = PUBLISHER_DOMAINS.some(d => tab.url.includes(d));
  if (isArticle) {
    chrome.action.setBadgeText({ text: 'SI', tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#4f6ef7', tabId });
  } else {
    chrome.action.setBadgeText({ text: '', tabId });
  }
});
