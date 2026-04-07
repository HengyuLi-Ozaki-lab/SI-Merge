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
    // Try all common meta tag formats across publishers
    const selectors = [
      'meta[name="citation_doi"]',
      'meta[property="citation_doi"]',
      'meta[name="dc.Identifier"][scheme="doi"]',
      'meta[name="DC.Identifier"][scheme="doi"]',
      'meta[name="dc.identifier"]',
      'meta[name="DC.Identifier"]',
      'meta[name="DOI"]',
      'meta[name="doi"]',
      'meta[property="og:url"]',
    ];

    const doiRe = /10\.\d{4,9}\/[^\s,;"'>\]}{)]+/;

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      let val = el.getAttribute('content') || '';
      val = val.replace(/^(https?:\/\/doi\.org\/|doi:)/i, '').trim();
      const m = val.match(doiRe);
      if (m) return m[0].replace(/\.$/, '');
    }

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
      if (url) return url.startsWith('http') ? url : new URL(url, window.location.origin).href;
    }

    // Publisher-specific PDF URL patterns based on the article URL
    const loc = window.location;
    const doi = extractDoi();

    // ACS: /doi/full/10.1021/xxx → /doi/pdf/10.1021/xxx
    if (loc.hostname.includes('acs.org') && loc.pathname.includes('/doi/')) {
      const pdfPath = loc.pathname.replace(/\/doi\/(full|abs)\//, '/doi/pdf/');
      return `${loc.origin}${pdfPath}`;
    }

    // Wiley: /doi/10.1002/xxx → /doi/pdfdirect/10.1002/xxx
    if (loc.hostname.includes('wiley.com') && doi) {
      return `https://onlinelibrary.wiley.com/doi/pdfdirect/${doi}`;
    }

    // Nature/Springer: append .pdf to article URL
    if ((loc.hostname.includes('nature.com') || loc.hostname.includes('springer.com'))
        && loc.pathname.includes('/article')) {
      return `${loc.origin}${loc.pathname.replace(/\/$/, '')}.pdf`;
    }

    // Science/PNAS: /doi/10.xxx → /doi/pdf/10.xxx
    if ((loc.hostname.includes('science.org') || loc.hostname.includes('pnas.org')) && doi) {
      return `${loc.origin}/doi/pdf/${doi}`;
    }

    // Fallback: look for PDF links on the page
    const links = document.querySelectorAll('a[href]');
    for (const a of links) {
      const href = a.getAttribute('href') || '';
      const text = (a.textContent || '').trim().toLowerCase();
      if ((text === 'pdf' || text === 'download pdf' || text.includes('view pdf'))
          && (href.includes('/pdf') || href.endsWith('.pdf'))) {
        return href.startsWith('http') ? href : new URL(href, loc.origin).href;
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

  function extractSiUrls() {
    const results = [];
    const seen = new Set();
    const origin = window.location.origin;

    // Science / PNAS: look in core-supplementary-materials section
    const suppSection = document.querySelector('section.core-supplementary-materials, section.core-supplementary-material');
    if (suppSection) {
      for (const a of suppSection.querySelectorAll('a[href]')) {
        const href = a.getAttribute('href') || '';
        if (href.includes('suppl_file') || href.endsWith('.pdf')) {
          const full = href.startsWith('http') ? href : new URL(href, origin).href;
          if (!seen.has(full)) { seen.add(full); results.push(full); }
        }
      }
    }

    // Generic: links with /doi/suppl/ and suppl_file
    for (const a of document.querySelectorAll('a[href*="suppl_file"], a[href*="downloadSupplement"], a[download][href$=".pdf"]')) {
      const href = a.getAttribute('href') || '';
      if (!href) continue;
      const full = href.startsWith('http') ? href : new URL(href, origin).href;
      if (!seen.has(full)) { seen.add(full); results.push(full); }
    }

    // Nature/Springer: MOESM links
    for (const a of document.querySelectorAll('a[href*="/MOESM"], a[href*="/moesm"]')) {
      const href = a.getAttribute('href') || '';
      const full = href.startsWith('http') ? href : new URL(href, origin).href;
      if (!seen.has(full)) { seen.add(full); results.push(full); }
    }

    return results;
  }

  function extractMetadata() {
    const url = window.location.href;
    return {
      doi: extractDoi(),
      pdfUrl: extractPdfUrl(),
      siUrls: extractSiUrls(),
      title: extractTitle(),
      publisher: detectPublisher(url),
      articleUrl: url,
    };
  }

  return { detectPublisher, extractDoi, extractPdfUrl, extractSiUrls, extractTitle, extractMetadata };
})();

if (typeof globalThis !== 'undefined') {
  globalThis.Publishers = Publishers;
}
