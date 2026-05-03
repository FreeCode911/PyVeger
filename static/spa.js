/* PyVegar SPA Router + real-time bridge
   Provides:
     PV3.go(url)          — programmatic navigation
     PV3.onNavigate(fn)   — register cleanup that fires on navigate-away
     PV3.cleanup()        — called internally before each swap
*/
(function () {
  'use strict';

  // ── Cleanup registry ─────────────────────────────────────────────────────
  var _cleanups = [];
  window.PV3 = {
    onNavigate: function (fn) { _cleanups.push(fn); },
    cleanup: function () {
      var fn;
      while ((fn = _cleanups.pop())) { try { fn(); } catch (e) { } }
    },
    go: function (url) { _navigate(url, true); }
  };

  // ── Progress bar ─────────────────────────────────────────────────────────
  var _bar = null, _bt1 = null, _bt2 = null;

  var _ss = document.createElement('style');
  _ss.id = 'pv3-spa-style';
  _ss.textContent =
    '@keyframes pv3sh{from{background-position:200% 0}to{background-position:-200% 0}}' +
    '#pv3-bar{position:fixed;top:0;left:0;z-index:2147483647;height:3px;width:0;pointer-events:none;' +
    'border-radius:0 2px 2px 0;opacity:0;' +
    'background:linear-gradient(90deg,#fb3640 0%,#ff8080 50%,#fb3640 100%);' +
    'background-size:300% 100%;animation:pv3sh 1.2s linear infinite;' +
    'transition:width .28s ease,opacity .28s ease}';
  document.head.appendChild(_ss);

  function _initBar() {
    if (_bar && document.body && document.body.contains(_bar)) return;
    if (!document.body) return;
    _bar = document.createElement('div');
    _bar.id = 'pv3-bar';
    document.body.appendChild(_bar);
  }

  function _barStart() {
    clearTimeout(_bt1); clearTimeout(_bt2);
    _initBar();
    if (!_bar) return;
    _bar.style.opacity = '1';
    _bar.style.width = '30%';
    _bt1 = setTimeout(function () { if (_bar) _bar.style.width = '60%'; }, 300);
    _bt2 = setTimeout(function () { if (_bar) _bar.style.width = '82%'; }, 800);
  }

  function _barDone() {
    clearTimeout(_bt1); clearTimeout(_bt2);
    if (!_bar) return;
    _bar.style.width = '100%';
    _bt1 = setTimeout(function () {
      if (!_bar) return;
      _bar.style.opacity = '0';
      _bt2 = setTimeout(function () { if (_bar) { _bar.style.width = '0'; } }, 300);
    }, 180);
  }

  // ── Router ───────────────────────────────────────────────────────────────
  var SKIP = { '/logout': 1, '/login': 1 };
  var _busy = false;

  function _ok(a) {
    if (!a || !a.href) return false;
    try {
      var u = new URL(a.href);
      if (u.origin !== location.origin) return false;
      if (SKIP[u.pathname]) return false;
      if (a.target === '_blank') return false;
      var h = (a.getAttribute('href') || '');
      if (h.charAt(0) === '#') return false;
      if (a.hasAttribute('data-no-spa')) return false;
      return true;
    } catch (e) { return false; }
  }

  function _navigate(href, push) {
    if (_busy) return;
    _busy = true;
    _barStart();

    fetch(href, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok || r.url.indexOf('/login') !== -1) {
          location.href = href; return null;
        }
        return r.text();
      })
      .then(function (html) {
        if (html === null) { _busy = false; return; }

        // Run current page cleanup (closes WS, timers etc.)
        PV3.cleanup();

        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');

        // Update title
        document.title = doc.title;

        // Swap the page-level <style> tag
        var ns = doc.querySelector('head > style');
        var os = document.querySelector('head > style.pv3-pg');
        if (!os) {
          os = document.querySelector('head > style:not(#pv3-spa-style)');
          if (os) os.className = 'pv3-pg';
        }
        if (ns && os) os.textContent = ns.textContent;

        // Copy body inline style (server page needs height:100vh)
        var nb = doc.body;
        if (nb) {
          var bsi = nb.getAttribute('style') || '';
          document.body.setAttribute('style', bsi || '');
        }

        // Fade out
        document.body.style.transition = 'opacity .12s ease';
        document.body.style.opacity = '0';

        setTimeout(function () {
          // Swap body content
          document.body.innerHTML = doc.body.innerHTML;

          // Re-insert bar (lost in swap)
          _bar = null;
          _initBar();
          _barDone();

          // Fade in
          document.body.style.opacity = '0';
          document.body.style.transition = '';
          requestAnimationFrame(function () {
            requestAnimationFrame(function () {
              document.body.style.transition = 'opacity .18s ease';
              document.body.style.opacity = '1';
              setTimeout(function () {
                document.body.style.transition = '';
                document.body.style.opacity = '';
              }, 200);
            });
          });

          // Re-execute body scripts (creates new global scope vars, re-registers event listeners)
          document.body.querySelectorAll('script').forEach(function (old) {
            var ns2 = document.createElement('script');
            for (var i = 0; i < old.attributes.length; i++) {
              var at = old.attributes[i];
              ns2.setAttribute(at.name, at.value);
            }
            ns2.textContent = old.textContent;
            if (old.parentNode) old.parentNode.replaceChild(ns2, old);
          });

          if (push !== false) history.pushState({ url: href }, '', href);
          _busy = false;

        }, 125);
      })
      .catch(function () {
        _busy = false;
        _barDone();
        location.href = href;
      });
  }

  // Intercept clicks on <a> tags
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey) return;
    var el = e.target;
    while (el && el !== document.body) {
      if (el.tagName === 'A') {
        if (_ok(el)) { e.preventDefault(); _navigate(el.href, true); }
        return;
      }
      el = el.parentNode;
    }
  }, true);

  // Browser back / forward
  window.addEventListener('popstate', function () {
    _navigate(location.href, false);
  });

  history.replaceState({ url: location.href }, '', location.href);

  if (document.body) _initBar();
  else document.addEventListener('DOMContentLoaded', _initBar);

})();
