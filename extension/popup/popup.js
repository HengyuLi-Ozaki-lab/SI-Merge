const DEFAULT_BACKEND = 'https://si-merge.onrender.com';

const $ = (sel) => document.querySelector(sel);

const articleInfo = $('#articleInfo');
const noArticle = $('#noArticle');
const mergeBtn = $('#mergeBtn');
const progressArea = $('#progressArea');
const resultArea = $('#resultArea');
const errorArea = $('#errorArea');
const stepDetail = $('#stepDetail');

let currentMeta = null;

// --- Init: detect article on current tab ---
async function init() {
  const meta = await chrome.runtime.sendMessage({ type: 'GET_ARTICLE_META' });

  if (meta && meta.doi) {
    currentMeta = meta;
    showArticleInfo(meta);
    mergeBtn.disabled = false;
  } else {
    noArticle.style.display = 'block';
    articleInfo.style.display = 'none';
    mergeBtn.disabled = true;
  }

  // Enable merge button when manual DOI is entered
  $('#manualDoi').addEventListener('input', (e) => {
    const val = e.target.value.trim();
    mergeBtn.disabled = !val;
    if (val) {
      currentMeta = { doi: val, pdfUrl: null, articleUrl: null, title: null, publisher: null };
    }
  });

  // Load settings
  const settings = await chrome.storage.sync.get({ backendUrl: DEFAULT_BACKEND });
  $('#backendUrlInput').value = settings.backendUrl || DEFAULT_BACKEND;
}

function showArticleInfo(meta) {
  articleInfo.style.display = 'block';
  noArticle.style.display = 'none';

  $('#articleTitle').textContent = meta.title || 'Untitled article';
  $('#articleDoi').textContent = meta.doi;

  const badge = $('#publisherBadge');
  if (meta.publisher) {
    badge.textContent = meta.publisher;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

// --- Merge ---
mergeBtn.addEventListener('click', startMerge);

async function startMerge() {
  if (!currentMeta || !currentMeta.doi) return;

  mergeBtn.disabled = true;
  progressArea.classList.add('active');
  resultArea.classList.remove('active');
  errorArea.classList.remove('active');
  resetSteps();

  // Get active tab to send merge command
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  try {
    await chrome.runtime.sendMessage({
      type: 'START_MERGE',
      payload: {
        doi: currentMeta.doi,
        pdfUrl: currentMeta.pdfUrl,
        siUrls: currentMeta.siUrls || [],
        articleUrl: currentMeta.articleUrl,
        title: currentMeta.title,
      },
      tabId: tab ? tab.id : 0,
    });
  } catch (err) {
    showError(err.message);
  }
}

// --- Listen for progress from service worker ---
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'MERGE_PROGRESS') {
    const { step, status, detail } = msg;
    updateStep(step, status);
    if (detail) stepDetail.textContent = detail;
  }

  if (msg.type === 'MERGE_COMPLETE') {
    markAllDone();
    progressArea.classList.remove('active');
    resultArea.classList.add('active');
  }

  if (msg.type === 'MERGE_ERROR') {
    showError(msg.error || 'Merge failed');
  }
});

// --- Steps UI ---
function resetSteps() {
  document.querySelectorAll('.step-item').forEach(el => {
    el.classList.remove('active', 'done', 'error');
    el.querySelector('.step-dot').textContent = el.dataset.step;
  });
  stepDetail.textContent = '';
}

function updateStep(step, status) {
  document.querySelectorAll('.step-item').forEach(el => {
    const s = parseInt(el.dataset.step);
    if (s < step) {
      el.classList.remove('active', 'error');
      el.classList.add('done');
      el.querySelector('.step-dot').textContent = '\u2713';
    } else if (s === step) {
      if (status === 'error') {
        el.classList.add('error');
        el.querySelector('.step-dot').textContent = '!';
      } else if (['done', 'complete', 'extracted', 'downloaded'].includes(status)) {
        el.classList.remove('active');
        el.classList.add('done');
        el.querySelector('.step-dot').textContent = '\u2713';
      } else {
        el.classList.add('active');
      }
    }
  });
}

function markAllDone() {
  document.querySelectorAll('.step-item').forEach(el => {
    el.classList.remove('active', 'error');
    el.classList.add('done');
    el.querySelector('.step-dot').textContent = '\u2713';
  });
}

function showError(msg) {
  progressArea.classList.remove('active');
  errorArea.classList.add('active');
  $('#errorMsg').textContent = msg;
  mergeBtn.disabled = false;
}

// --- Settings ---
$('#settingsToggle').addEventListener('click', () => {
  $('#settingsBody').classList.toggle('open');
});

$('#backendUrlInput').addEventListener('change', (e) => {
  const url = e.target.value.trim() || DEFAULT_BACKEND;
  chrome.storage.sync.set({ backendUrl: url });
});

// --- Go ---
init();
