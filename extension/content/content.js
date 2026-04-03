/**
 * SI Merge Content Script
 *
 * Runs on publisher article pages. Detects DOI and PDF URL from meta tags,
 * injects a floating action button, and fetches the article PDF using the
 * page's own context (preserving Cloudflare clearance and institutional
 * access cookies).
 */

(() => {
  const meta = Publishers.extractMetadata();
  if (!meta.doi) return;

  chrome.storage.session.set({ articleMeta: meta });

  let fab = null;
  let toast = null;

  function createFab() {
    fab = document.createElement('button');
    fab.id = 'si-merge-fab';
    fab.innerHTML = `
      <span class="si-merge-icon">\u{1F4CE}</span>
      <span class="si-merge-label">Merge SI</span>
    `;
    fab.title = `Merge Supplementary Information for DOI: ${meta.doi}`;
    fab.addEventListener('click', onFabClick);
    document.body.appendChild(fab);

    toast = document.createElement('div');
    toast.id = 'si-merge-toast';
    document.body.appendChild(toast);
  }

  function setFabState(state, text) {
    if (!fab) return;
    const label = fab.querySelector('.si-merge-label');
    const icon = fab.querySelector('.si-merge-icon');

    fab.className = '';
    switch (state) {
      case 'idle':
        icon.innerHTML = '\u{1F4CE}';
        label.textContent = text || 'Merge SI';
        break;
      case 'processing':
        fab.classList.add('si-merge-processing');
        icon.innerHTML = '<span class="si-merge-spinner"></span>';
        label.textContent = text || 'Processing...';
        break;
      case 'success':
        fab.classList.add('si-merge-success');
        icon.innerHTML = '\u2713';
        label.textContent = text || 'Done!';
        break;
      case 'error':
        fab.classList.add('si-merge-error');
        icon.innerHTML = '\u2715';
        label.textContent = text || 'Failed';
        break;
    }
  }

  function showToast(text) {
    if (!toast) return;
    toast.textContent = text;
    toast.classList.add('visible');
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => toast.classList.remove('visible'), 5000);
  }

  /**
   * Fetch PDF in the page's MAIN world context.
   * This preserves the user's cookies, Cloudflare clearance, and
   * institutional access — the fetch behaves as if the user clicked
   * a download link on the page.
   */
  function fetchPdfInPageContext(pdfUrl) {
    return new Promise((resolve, reject) => {
      const nonce = 'si_merge_' + Date.now();
      const timeout = setTimeout(() => {
        window.removeEventListener('message', handler);
        reject(new Error('PDF download timed out'));
      }, 60000);

      function handler(event) {
        if (event.source !== window) return;
        if (!event.data || event.data.type !== nonce) return;
        window.removeEventListener('message', handler);
        clearTimeout(timeout);

        if (event.data.error) {
          reject(new Error(event.data.error));
        } else {
          resolve(new Uint8Array(event.data.buffer));
        }
      }
      window.addEventListener('message', handler);

      const script = document.createElement('script');
      script.textContent = `(async()=>{try{` +
        `const r=await fetch(${JSON.stringify(pdfUrl)},{credentials:'same-origin'});` +
        `if(!r.ok)throw new Error('HTTP '+r.status);` +
        `const b=await r.arrayBuffer();` +
        `window.postMessage({type:${JSON.stringify(nonce)},buffer:Array.from(new Uint8Array(b))},'*');` +
        `}catch(e){` +
        `window.postMessage({type:${JSON.stringify(nonce)},error:e.message},'*');` +
        `}})();`;
      document.documentElement.appendChild(script);
      script.remove();
    });
  }

  async function onFabClick() {
    if (fab.classList.contains('si-merge-processing')) return;

    if (fab.classList.contains('si-merge-error')) {
      setFabState('idle');
      return;
    }

    setFabState('processing', 'Downloading PDF...');
    showToast(`DOI: ${meta.doi}`);

    try {
      let pdfData = null;

      // Try fetching the PDF in the page context (with user's cookies)
      if (meta.pdfUrl) {
        try {
          setFabState('processing', 'Downloading PDF...');
          pdfData = await fetchPdfInPageContext(meta.pdfUrl);

          if (pdfData && pdfData.length < 1000) {
            pdfData = null;
          }
          if (pdfData) {
            const header = new TextDecoder().decode(pdfData.slice(0, 5));
            if (!header.startsWith('%PDF')) {
              pdfData = null;
            }
          }
        } catch (err) {
          console.log('[SI Merge] Page-context PDF fetch failed:', err.message);
          pdfData = null;
        }
      }

      if (pdfData) {
        setFabState('processing', 'Uploading to server...');
        showToast(`PDF downloaded (${Math.round(pdfData.length / 1024)} KB), uploading...`);
      } else {
        setFabState('processing', 'Using server-side download...');
        showToast('Browser PDF download failed, using server-side download...');
      }

      const response = await chrome.runtime.sendMessage({
        type: 'START_MERGE',
        payload: {
          doi: meta.doi,
          pdfUrl: meta.pdfUrl,
          articleUrl: meta.articleUrl,
          title: meta.title,
          pdfData: pdfData ? Array.from(pdfData) : null,
        },
      });

      if (response && response.error) {
        setFabState('error', 'Failed');
        showToast(response.error);
      }
    } catch (err) {
      setFabState('error', 'Failed');
      showToast(err.message);
    }
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'MERGE_PROGRESS') {
      const { step, status, detail } = msg;
      const STEPS = ['', 'Extract DOI', 'Find SI', 'Download SI', 'Extract Text', 'Analyze', 'Merge'];
      const stepName = STEPS[step] || `Step ${step}`;

      if (status === 'error') {
        setFabState('error', 'Failed');
        showToast(detail || 'Merge failed');
      } else {
        setFabState('processing', `${stepName}...`);
        if (detail) showToast(detail);
      }
    }

    if (msg.type === 'MERGE_COMPLETE') {
      setFabState('success', 'Download ready!');
      showToast('Merged PDF is downloading...');
      setTimeout(() => setFabState('idle'), 8000);
    }

    if (msg.type === 'MERGE_ERROR') {
      setFabState('error', 'Failed');
      showToast(msg.error || 'Merge failed. Click to retry.');
    }
  });

  createFab();
})();
