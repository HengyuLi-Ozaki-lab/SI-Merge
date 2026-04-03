/**
 * Publisher detection and metadata extraction from article pages.
 * Shared by content script and popup.
 */

const Publishers = (() => {

  const PUBLISHER_PATTERNS = [
    { domain: 'nature.com',          name: 'Nature / Springer Nature' },
    { domain: 'springer.com',        name: 'Springer' },
    { domain: 'link.springer.com',   name: 'Springer' },
    { domain: 'pubs.acs.org',        name: 'ACS Publications' },
    { domain: 'onlinelibrary.wiley.com', name: 'Wiley' },
    { domain: 'sciencedirect.com',   name: 'Elsevier / ScienceDirect' },
    { domain: 'linkinghub.elsevier.com', name: 'Elsevier' },
    { domain: 'science.org',         name: 'Science (AAAS)' },
    { domain: 'pnas.org',            name: 'PNAS' },
    { domain: 'pubs.rsc.org',        name: 'RSC' },
    { domain: 'journals.aps.org',    name: 'APS' },
  ];

  function detectPublisher(url) {
    const hostname = new URL(url).hostname;
    for (const p of PUBLISHER_PATTERNS) {
      if (hostname.includes(p.domain)) return p.name;
    }
    return null;
  }

  function extractDoi() {
    const meta = document.querySelector('meta[name="citation_doi"]')
      || document.querySelector('meta[name="dc.identifier" i]')
      || document.querySelector('meta[name="DC.Identifier" i]')
      || document.querySelector('meta[property="citation_doi"]');
    if (meta) {
      let doi = meta.getAttribute('content') || '';
      doi = doi.replace(/^(https?:\/\/doi\.org\/|doi:)/i, '').trim();
      if (doi) return doi;
    }

    const doiRe = /10\.\d{4,9}\/[^\s,;"'>\]}{)]+/;
    const canonical = document.querySelector('link[rel="canonical"]');
    if (canonical) {
      const m = (canonical.getAttribute('href') || '').match(doiRe);
      if (m) return m[0].replace(/\.$/, '');
    }

    const m = window.location.href.match(doiRe);
    if (m) return m[0].replace(/\.$/, '');

    return null;
  }

  function extractPdfUrl() {
    const meta = document.querySelector('meta[name="citation_pdf_url"]')
      || document.querySelector('meta[property="citation_pdf_url"]');
    if (meta) {
      const url = meta.getAttribute('content');
      if (url) return url;
    }

    const links = document.querySelectorAll('a[href]');
    for (const a of links) {
      const href = a.getAttribute('href') || '';
      const text = (a.textContent || '').trim().toLowerCase();
      if ((text === 'pdf' || text === 'download pdf') && href.includes('/pdf')) {
        return href.startsWith('http') ? href : new URL(href, window.location.origin).href;
      }
    }
    return null;
  }

  function extractTitle() {
    const meta = document.querySelector('meta[name="citation_title"]')
      || document.querySelector('meta[name="dc.title" i]')
      || document.querySelector('meta[property="og:title"]');
    if (meta) return meta.getAttribute('content');
    const h1 = document.querySelector('h1');
    return h1 ? h1.textContent.trim() : document.title;
  }

  function extractMetadata() {
    const url = window.location.href;
    return {
      doi: extractDoi(),
      pdfUrl: extractPdfUrl(),
      title: extractTitle(),
      publisher: detectPublisher(url),
      articleUrl: url,
    };
  }

  return { detectPublisher, extractDoi, extractPdfUrl, extractTitle, extractMetadata };
})();

if (typeof globalThis !== 'undefined') {
  globalThis.Publishers = Publishers;
}
