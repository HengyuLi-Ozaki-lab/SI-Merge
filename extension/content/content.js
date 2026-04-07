/**
 * SI Merge Content Script
 *
 * Runs on publisher article pages. Detects DOI and PDF URL from meta tags,
 * injects a floating action button, and communicates with the service worker
 * for the merge workflow.
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

  async function onFabClick() {
    if (fab.classList.contains('si-merge-processing')) return;

    if (fab.classList.contains('si-merge-error')) {
      setFabState('idle');
      return;
    }

    setFabState('processing', 'Starting...');
    showToast(`DOI: ${meta.doi}`);

    try {
      const response = await chrome.runtime.sendMessage({
        type: 'START_MERGE',
        payload: {
          doi: meta.doi,
          pdfUrl: meta.pdfUrl,
          siUrls: meta.siUrls || [],
          articleUrl: meta.articleUrl,
          title: meta.title,
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
