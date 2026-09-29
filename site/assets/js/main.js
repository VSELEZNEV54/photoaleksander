/* ANPhotoLab v2 «Проявочная» (редакция v3 «аккуратнее») — поведение сайта. Без библиотек и без сети: работает и с file://,
   и на любом хостинге.
   Модули: подгонка огромных слов под ширину, первый экран, лоадер «Проявка» и шторка переходов между страницами,
   появления, шапка (прячет дубли имени и CTA на первом экране, уезжает на жанрах), меню-оверлей, sticky-вкладки,
   счётчики, бегущая строка (блок marquee — если стоит на странице), вопросы, «Подробнее о съёмке», лайтбокс, «наверх».
   v3: превью направлений за курсором и стопка кейсов удалены — направления и кейсы работают на чистом CSS
   (наведение — лёгкое увеличение). Форм нет — все CTA ведут в Telegram. prefers-reduced-motion — всё статично. */
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  window.ANP = true; /* сигнал inline-скрипту в <head>: JS загрузился, прятать контент под анимацию можно */
  root.classList.add('js');
  var mm = function (q) { return window.matchMedia ? matchMedia(q).matches : false; };
  var reduce = mm('(prefers-reduced-motion: reduce)');
  var $ = function (s, c) { return (c || d).querySelector(s); };
  var $$ = function (s, c) { return Array.prototype.slice.call((c || d).querySelectorAll(s)); };
  var raf = window.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
  var debounce = function (fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; };

  /* ---------- Огромные слова на всю ширину: имя, слово жанра, «Обсудим съёмку», ANPHOTOLAB, 404 */
  var fitTo = function (el, inner, target) {
    el.style.fontSize = '';
    var fs = parseFloat(getComputedStyle(el).fontSize);
    var w = inner.getBoundingClientRect().width;
    if (!fs || !w || !target) return;
    var size = fs * target / w * 0.997;
    /* data-fit-max="0.4": слово по ширине, но не выше этой доли окна — короткое слово («Отзывы», «404»)
       не раздувается на весь первый экран, а встаёт влево своим размером */
    var max = parseFloat(el.getAttribute('data-fit-max'));
    if (max > 0) size = Math.min(size, window.innerHeight * max);
    el.style.fontSize = size.toFixed(2) + 'px';
  };
  var contentWidth = function (el) {
    var cs = getComputedStyle(el);
    return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  };
  var fitAll = function () {
    $$('[data-fit-name]').forEach(function (nm) {
      var words = $$('.hp__w', nm);
      nm.style.fontSize = '';
      words.forEach(function (w) { w.style.fontSize = ''; });
      var perLine = words.length && getComputedStyle(words[0]).display === 'block';
      if (perLine) {
        words.forEach(function (w) { fitTo(w, w.firstElementChild, contentWidth(nm)); });
      } else {
        var r = d.createRange(); r.selectNodeContents(nm);
        var fs = parseFloat(getComputedStyle(nm).fontSize), tw = r.getBoundingClientRect().width;
        if (fs && tw) nm.style.fontSize = (fs * contentWidth(nm) / tw * 0.997).toFixed(2) + 'px';
      }
    });
    $$('[data-fit]').forEach(function (el) { if (el.firstElementChild) fitTo(el, el.firstElementChild, contentWidth(el)); });
  };

  /* ---------- Первый экран: подгоняем слова и запускаем появление (is-ready). Без шторки — как только готовы шрифты
     (не дольше 900 мс). Если на экране лоадер или шторка (модуль «Загрузка и переходы» ниже) — в момент их ухода:
     имя и портрет «проявляются» на глазах, а не под шторкой. Появления блоков и счётчики ждут того же (onShown). */
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var ptMode = root.classList.contains('ld-on') ? 'ld' : root.classList.contains('veil-in') ? 'in' : '';
  var shown = !ptMode, shownQ = [], introDone = false;
  var onShown = function (fn) { if (shown) fn(); else shownQ.push(fn); };
  var intro = function () {
    if (!shown) { shown = true; shownQ.splice(0).forEach(function (f) { f(); }); }
    if (introDone) return;
    introDone = true;
    fitAll(); setTimeout(function () { root.classList.add('is-ready'); }, 30);
  };
  if (!ptMode) {
    if (reduce || !d.fonts) intro();
    else Promise.race([d.fonts.ready, wait(900)]).then(intro, intro);
  }
  if (d.fonts) {
    d.fonts.ready.then(fitAll);
    if (d.fonts.addEventListener) d.fonts.addEventListener('loadingdone', fitAll);
  }
  var lastW = window.innerWidth, lastH = window.innerHeight;
  /* по высоте — только заметные изменения (панель адреса на телефоне дёргает innerHeight при скролле) */
  addEventListener('resize', debounce(function () {
    var w = window.innerWidth, h = window.innerHeight;
    if (w !== lastW || Math.abs(h - lastH) > 120) { lastW = w; lastH = h; fitAll(); }
  }, 120));

  /* ---------- Загрузка и переходы «Проявка» (разметка — build.py → curtain, стили — main.css § 24, README).
     ld — первый визит за сессию: лампа загорается, счётчик 000 → 100 идёт по времени (PT.count) и догоняет до 100,
       когда готовы DOM, шрифты и кадр первого экрана (img.decode) — window.load не ждём. Не короче PT.count,
       не дольше PT.ceil от начала загрузки, даже если CDN с фото тормозит. Потом лист уходит вверх (PT.exit)
       и в этот же момент стартует появление первого экрана.
     in — повторная загрузка в той же сессии: лист закрыт с первого кадра и уходит вверх за PT.reveal.
     Клик по ссылке на другую страницу сайта: лист накрывает экран снизу вверх за PT.cover, затем переход.
     Все длительности — в PT (мс). Страховки: свой таймер main.js + таймеры inline-скрипта в <head>. */
  var PT = { count: 1450, hold: 120, exit: 900, ceil: 2800, reveal: 500, cover: 340 };
  var pt = $('#veil'), sheet = pt && $('.veil__sheet', pt), ptIn = pt && $('.veil__in', pt);
  var EIO = 'cubic-bezier(.76,0,.24,1)', noop = function () {};
  (function () {
    /* визит отмечен: следующие страницы сессии — без счётчика. На 404 (ld-off) не отмечаем: с неё на главную — полный лоадер */
    if (!root.classList.contains('ld-off')) {
      var ok = false;
      try { sessionStorage.setItem('anp-seen', '1'); ok = sessionStorage.getItem('anp-seen') === '1'; } catch (e) {}
      /* запасной флаг: file:// в части браузеров даёт каждой странице своё хранилище, приватный режим может его запретить */
      if (!ok || location.protocol === 'file:') {
        try { if (!/(^| )anp-seen( |$)/.test(window.name)) window.name = (window.name ? window.name + ' ' : '') + 'anp-seen'; } catch (e) {}
      }
    }
  })();
  var anims = [];
  var anim = function (el, kf, o) {
    if (!el || !el.animate) return null;
    var a = el.animate(kf, o); anims.push(a); return a;
  };
  var after = function (a, ms, fn) {
    var done = false, go = function () { if (!done) { done = true; fn(); } };
    if (a && a.finished) a.finished.then(go, go);
    setTimeout(go, ms + 300);
  };
  /* снять шторку: классы режима прочь (лист прячется под экран, прокрутка свободна), наши анимации — отменить.
     В одном кадре — без мигания */
  var ptClean = function () {
    root.classList.remove('ld-on', 'ld-x', 'ld-f', 'veil-in');
    if (pt) pt.style.pointerEvents = '';
    anims.splice(0).forEach(function (a) { try { a.cancel(); } catch (e) {} });
  };
  /* лист уходит вверх целиком, вместе с засветкой под нижним краем */
  var lift = function (dur, delay) {
    var h = sheet.offsetHeight, b = parseFloat(getComputedStyle(sheet, '::after').height) || 0;
    return anim(sheet, [{ transform: 'translateY(0)' }, { transform: 'translateY(' + -Math.ceil(h + b + 2) + 'px)' }],
      { duration: dur, delay: delay || 0, easing: EIO, fill: 'forwards' });
  };
  /* содержимое листа отстаёт от него и гаснет — лёгкий параллакс */
  var sink = function (dur, delay, k) {
    anim(ptIn, [{ transform: 'translateY(0)', opacity: 1 }, { transform: 'translateY(' + Math.round(window.innerHeight * k) + 'px)', opacity: 0 }],
      { duration: dur, delay: delay || 0, easing: EIO, fill: 'forwards' });
  };
  var reveal = function () {
    if (pt) pt.style.pointerEvents = 'none';
    sink(PT.reveal, 0, 0.16);
    var a = lift(PT.reveal);
    intro();
    after(a, PT.reveal, ptClean);
  };
  var loader = function () {
    var t0 = performance.now(), D = PT.count, p = -1, ready = false, fin = false;
    var hard = Math.max(PT.ceil, t0 + 900); /* потолок — от начала загрузки страницы */
    var v = $('.ld__v', pt), bar = $('.ld__bar i', pt), lcp = $('img[fetchpriority="high"]');
    var txt = function () { root.classList.add('ld-f'); };
    var fl = d.fonts && d.fonts.load ? Promise.all([d.fonts.load('800 100px "Sofia Sans Extra Condensed"', '0123456789'), d.fonts.load('500 12px "JetBrains Mono"', 'ANPHOTOLAB')]) : Promise.resolve();
    Promise.race([fl, wait(700)]).then(txt, txt);
    var dom = new Promise(function (r) { if (d.readyState !== 'loading') r(); else d.addEventListener('DOMContentLoaded', r); });
    var ok = function () { ready = true; };
    Promise.all([dom, d.fonts ? d.fonts.ready : 0, lcp && lcp.decode ? lcp.decode().catch(noop) : 0]).then(ok, ok);
    /* заливка цифр: доля высоты строки, где начинаются и кончаются глифы (подобрано под Sofia Sans Extra Condensed) */
    var FL = 16, FH = 88;
    var draw = function (q) {
      var n = Math.floor(q), s = n < 10 ? '00' + n : n < 100 ? '0' + n : String(n);
      if (v) {
        if (v.textContent !== s) v.textContent = s;
        v.style.setProperty('--f', (FL + (FH - FL) * q / 100).toFixed(1) + '%');
      }
      if (bar) bar.style.transform = 'scaleX(' + (q / 100).toFixed(4) + ')';
    };
    var exit = function () {
      root.classList.add('ld-x'); /* прокрутка свободна, клики проходят сквозь уходящий лист */
      pt.style.pointerEvents = 'none';
      /* «100» уходит вверх из маски, метки гаснут — и следом лист поднимается, открывая первый экран */
      anim(v, [{ transform: 'translateY(0)' }, { transform: 'translateY(-106%)' }], { duration: 420, easing: 'cubic-bezier(.65,0,.35,1)', fill: 'forwards' });
      $$('.ld__k, .ld__st, .ld__bar', pt).forEach(function (el) { anim(el, [{ opacity: 1 }, { opacity: 0 }], { duration: 320, easing: 'linear', fill: 'forwards' }); });
      sink(PT.exit, 140, 0.3);
      var a = lift(PT.exit, 140);
      setTimeout(intro, 230);
      after(a, PT.exit + 140, ptClean);
    };
    var tick = function (now) {
      if (fin) return;
      now = now || performance.now();
      if (now >= hard - 280) ready = true;
      /* номинальная кривая — по времени; пока не готово, счётчик замедляется у 90 и еле ползёт к 99 */
      var k = Math.min(1, (now - t0) / D), goal = 100 * k * k * (3 - 2 * k);
      if (!ready) goal = Math.min(goal, 90 + 9 * (1 - Math.exp(-Math.max(0, now - t0 - D) / 800)));
      else if (k >= 1) goal = 100;
      var q = p >= 0 && goal - p > 2.5 ? p + (goal - p) * 0.2 : Math.max(p, goal); /* готовность пришла поздно — догоняем за ~0,15 с */
      if (q !== p) { p = q; draw(p); }
      if (ready && p >= 100) { fin = true; setTimeout(exit, PT.hold); return; }
      raf(tick);
    };
    raf(tick);
  };
  var ptStart = function () {
    clearTimeout(window.ANPfs); /* таймер-страховку из <head> заменяем своим — от момента, когда вкладка видима */
    setTimeout(function () { ptClean(); intro(); }, ptMode === 'ld' ? PT.ceil + PT.exit + 1500 : PT.reveal + 2000);
    if (ptMode === 'ld') loader();
    else Promise.race([d.fonts ? d.fonts.ready : 0, wait(300)]).then(reveal, reveal);
  };
  if (ptMode) {
    try {
      if (!pt || !sheet || reduce) { ptClean(); intro(); }
      else if (d.hidden) {
        /* страница открыта в фоновой вкладке: лоадер покажем, когда на неё переключатся */
        clearTimeout(window.ANPfs);
        var onVis = function () { if (!d.hidden) { d.removeEventListener('visibilitychange', onVis); ptStart(); } };
        d.addEventListener('visibilitychange', onVis);
      } else ptStart();
    } catch (e) { ptClean(); intro(); }
  }

  /* переход между страницами: перехватываем только обычный клик по ссылке на другую страницу этого же сайта */
  var samePath = function (a, b) { return a.replace(/index\.html$/, '') === b.replace(/index\.html$/, ''); };
  var ptTarget = function (a) {
    if ((a.target && a.target.toLowerCase() !== '_self') || a.hasAttribute('download') || a.hasAttribute('data-lb')) return '';
    if (a.closest('dialog, [data-no-pt]')) return '';
    var raw = a.getAttribute('href');
    if (!raw || raw.charAt(0) === '#') return '';
    var u;
    try { u = new URL(a.href, location.href); } catch (e) { return ''; }
    if (!/^(https?|file):$/.test(u.protocol) || u.protocol !== location.protocol || u.host !== location.host) return '';
    if (samePath(u.pathname, location.pathname) && u.search === location.search && u.hash) return ''; /* якорь на этой же странице */
    if (/\.(jpe?g|png|webp|avif|gif|svg|pdf|zip|mp4|mov|webm)$/i.test(u.pathname)) return '';
    return u.href;
  };
  /* клик уведёт на другую страницу с шторкой? (нужно и меню: такую ссылку оно не закрывает — его накроет шторка) */
  var ptWill = function (e, a) {
    if (!pt || !sheet || reduce || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return '';
    return a ? ptTarget(a) : '';
  };
  if (pt && sheet && !reduce) {
    var leaving = false, leaveT = 0;
    d.addEventListener('click', function (e) {
      if (leaving) return;
      var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
      var href = ptWill(e, a);
      if (!href) return;
      e.preventDefault();
      leaving = true;
      /* если лоадер или шторка ещё уходят — снимаем их и фиксируем стиль, чтобы лист накрыл экран снизу, а не возник сразу */
      if (anims.length || ptMode && !shown) { ptClean(); void sheet.offsetHeight; }
      root.classList.add('veil-out');
      setTimeout(function () { location.href = href; }, PT.cover + 20);
      /* переход так и не случился (сеть, отмена) — шторку не оставляем */
      leaveT = setTimeout(function () { leaving = false; root.classList.remove('veil-out'); }, 6000);
    });
    /* «Назад» из bfcache вернёт страницу с закрытой шторкой — открываем её */
    addEventListener('pageshow', function (e) {
      if (!e.persisted || !root.classList.contains('veil-out')) return;
      clearTimeout(leaveT); leaving = false;
      root.classList.remove('veil-out'); root.classList.add('veil-in');
      reveal();
    });
  }

  /* ---------- Появления при скролле — один раз; кадры ждут загрузки, чтобы «проявиться» */
  var show = function (el) {
    el.classList.add('is-in');
    if (el.classList.contains('ph')) setTimeout(function () { el.classList.add('is-done'); el.style.setProperty('--dl', '0ms'); }, 2000);
  };
  var rvs = $$('[data-rv]');
  if (reduce || !('IntersectionObserver' in window)) rvs.forEach(show);
  else {
    var io = new IntersectionObserver(function (ents) {
      ents.forEach(function (e) {
        if (!e.isIntersecting) return;
        io.unobserve(e.target);
        var el = e.target, img = el.classList.contains('ph') ? $('img', el) : null;
        if (img && !img.complete) {
          var done = false, go = function () { if (!done) { done = true; show(el); } };
          img.addEventListener('load', go); img.addEventListener('error', go); setTimeout(go, 2500);
        } else show(el);
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });
    onShown(function () { rvs.forEach(function (el) { io.observe(el); }); });
  }

  /* ---------- Шапка: подложка после начала скролла */
  var hdr = $('#hdr');
  if (hdr) {
    var onScroll = function () { hdr.classList.toggle('is-scrolled', window.scrollY > 8); };
    addEventListener('scroll', onScroll, { passive: true }); onScroll();
  }

  /* ---------- Пока огромное имя первого экрана на виду, имя и кнопка в шапке не дублируют его */
  var heroName = $('.hp__h1');
  if (hdr && heroName && 'IntersectionObserver' in window) {
    root.classList.add('at-hero');
    new IntersectionObserver(function (es) {
      es.forEach(function (e) { root.classList.toggle('at-hero', e.isIntersecting); });
    }, { rootMargin: '-' + (hdr.offsetHeight || 64) + 'px 0px 0px 0px' }).observe(heroName);
  }

  /* ---------- Жанры на телефоне: пока вкладки прилипли, шапка уезжает при скролле вниз и возвращается при скролле вверх */
  var tabsBar = $('.gtabs');
  if (hdr && tabsBar) {
    var lastY = window.scrollY, hideTick = false;
    var hideUpd = function () {
      hideTick = false;
      var y = window.scrollY, dy = y - lastY;
      if (Math.abs(dy) < 6 && y > 0) return;
      lastY = y;
      if (!mm('(max-width: 899px)')) { root.classList.remove('hdr-hide'); return; }
      var h = hdr.offsetHeight, hidden = root.classList.contains('hdr-hide'), tr = tabsBar.getBoundingClientRect();
      /* вкладки прилипают только до формы заявки (обёртка .gtabs-scope): когда они ушли вверх, шапка возвращается */
      var stuck = tr.top <= (hidden ? 1 : h + 1) && tr.bottom > (hidden ? 0 : h);
      root.classList.toggle('hdr-hide', stuck && dy > 0 && !root.classList.contains('menu-open'));
    };
    addEventListener('scroll', function () { if (!hideTick) { hideTick = true; raf(hideUpd); } }, { passive: true });
  }

  /* ---------- Меню-оверлей */
  var burger = $('.burger'), menu = $('#menu'), bt = $('.burger__t');
  if (burger && menu) {
    var outside = [$('#main'), $('.ftr')];
    var setMenu = function (open, kbd) {
      burger.setAttribute('aria-expanded', String(open));
      menu.classList.toggle('is-open', open);
      menu.setAttribute('aria-hidden', String(!open));
      if (open) menu.removeAttribute('inert'); else menu.setAttribute('inert', '');
      outside.forEach(function (el) { if (el) { if (open) el.setAttribute('inert', ''); else el.removeAttribute('inert'); } });
      root.classList.toggle('menu-open', open);
      d.body.style.overflow = open ? 'hidden' : '';
      if (bt) bt.textContent = open ? 'Закрыть' : 'Меню';
      /* с клавиатуры фокус переходит на список меню (не на первый пункт — иначе «Свадьбы» сразу стоит со сдвигом
         :focus-visible); следующий Tab ведёт к первому пункту. Мышью фокус остаётся на кнопке */
      if (open && kbd) setTimeout(function () {
        var nv = $('.menu__nav', menu) || menu;
        if (!nv.hasAttribute('tabindex')) nv.setAttribute('tabindex', '-1');
        nv.focus({ preventScroll: true });
      }, 90);
    };
    burger.addEventListener('click', function (e) { setMenu(burger.getAttribute('aria-expanded') !== 'true', e.detail === 0); });
    menu.addEventListener('click', function (e) {
      var a = e.target.closest('a');
      /* ссылка на другую страницу: меню остаётся открытым, его накрывает шторка перехода (без двойного движения) */
      if (a && !ptWill(e, a)) setMenu(false);
    });
    /* «Назад» из bfcache: страница вернётся с открытым меню — закрываем */
    addEventListener('pageshow', function (e) { if (e.persisted && menu.classList.contains('is-open')) setMenu(false); });
    d.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && menu.classList.contains('is-open')) { setMenu(false); burger.focus(); }
    });
  }

  /* ---------- Sticky-вкладки жанров: активная по центру, затухание краёв */
  $$('[data-tabs]').forEach(function (sc) {
    var act = $('[aria-current="page"]', sc);
    if (act) {
      var r = act.getBoundingClientRect(), s = sc.getBoundingClientRect();
      sc.scrollLeft += (r.left - s.left) - (s.width - r.width) / 2;
    }
    var upd = function () {
      var max = sc.scrollWidth - sc.clientWidth;
      sc.classList.toggle('fade-l', sc.scrollLeft > 4);
      sc.classList.toggle('fade-r', sc.scrollLeft < max - 4);
    };
    sc.addEventListener('scroll', upd, { passive: true }); addEventListener('resize', debounce(upd, 100)); upd();
  });

  /* ---------- Счётчики: один раз при появлении, ширина не прыгает (за числом — невидимый «призрак») */
  var counters = $$('[data-count]');
  if (!reduce && counters.length && 'IntersectionObserver' in window) {
    var run = function (el) {
      var target = el.getAttribute('data-count'), v = parseFloat(target), out = $('.st__v', el);
      var dec = (target.split('.')[1] || '').length, t0 = null, dur = 1600;
      var step = function (t) {
        if (t0 === null) t0 = t;
        var p = Math.min(1, (t - t0) / dur), e = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
        out.textContent = (v * e).toFixed(dec);
        if (p < 1) raf(step); else out.textContent = target;
      };
      raf(step);
    };
    counters.forEach(function (el) { var out = $('.st__v', el); if (out) out.textContent = '0'; });
    var cio = new IntersectionObserver(function (ents) {
      ents.forEach(function (e) { if (e.isIntersecting) { cio.unobserve(e.target); setTimeout(function () { run(e.target); }, 200); } });
    }, { threshold: 0.5 });
    onShown(function () { counters.forEach(function (el) { cio.observe(el); }); });
  }

  /* ---------- Бегущая строка: скорость в px/с, пауза вне экрана и на наведении */
  $$('[data-mq]').forEach(function (v) {
    var tr = $('.mq__track', v), list = $('.mq__list', v);
    if (!tr || !list) return;
    var sp = parseFloat(v.style.getPropertyValue('--speed')) || 60;
    var set = function () { tr.style.setProperty('--dur', (list.offsetWidth / sp).toFixed(1) + 's'); };
    set(); addEventListener('resize', debounce(set, 200));
    if (d.fonts) d.fonts.ready.then(set);
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) { es.forEach(function (e) { v.classList.toggle('is-off', !e.isIntersecting); }); }).observe(v);
    }
  });

  /* ---------- Вопросы: плавное раскрытие <details> */
  $$('.faq__i').forEach(function (det) {
    var sum = $('summary', det), body = $('.faq__a', det);
    if (!sum || !body) return;
    sum.addEventListener('click', function (e) {
      if (reduce || !body.animate) return;
      e.preventDefault();
      if (det.open) {
        det.classList.add('is-closing');
        var an = body.animate([{ height: body.offsetHeight + 'px', opacity: 1 }, { height: '0px', opacity: 0 }], { duration: 320, easing: 'cubic-bezier(.65,0,.35,1)' });
        an.onfinish = function () { det.open = false; det.classList.remove('is-closing'); };
      } else {
        det.open = true;
        body.animate([{ height: '0px', opacity: 0 }, { height: body.offsetHeight + 'px', opacity: 1 }], { duration: 460, easing: 'cubic-bezier(.16,1,.3,1)' });
      }
    });
  });

  /* ---------- «Подробнее о съёмке»: SEO-текст в DOM, свёрнут до нескольких строк */
  $$('[data-more]').forEach(function (box) {
    var btn = $('.seo__btn', box), body = $('.seo__body', box);
    if (!btn || !body) return;
    if (box.hasAttribute('data-open')) box.classList.add('is-open');
    if (!box.classList.contains('is-open') && body.scrollHeight <= body.clientHeight + 24) { box.classList.add('is-open'); return; }
    btn.hidden = false;
    btn.setAttribute('aria-expanded', String(box.classList.contains('is-open')));
    body.style.transition = reduce ? '' : 'max-height .7s cubic-bezier(.16,1,.3,1)';
    btn.addEventListener('click', function () {
      var open = !box.classList.contains('is-open');
      body.style.maxHeight = body.scrollHeight + 'px';
      if (open) {
        box.classList.add('is-open');
        setTimeout(function () { if (box.classList.contains('is-open')) body.style.maxHeight = 'none'; }, reduce ? 0 : 750);
      } else {
        void body.offsetHeight;
        box.classList.remove('is-open');
        body.style.maxHeight = '';
        var r = box.getBoundingClientRect();
        if (r.top < 0) box.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
      }
      btn.setAttribute('aria-expanded', String(open));
    });
  });

  /* ---------- Лайтбокс: группы по галереям, стрелки, клавиатура, свайп, Esc, счётчик */
  var lb = $('#lb');
  if (lb && typeof lb.showModal === 'function') {
    var lbImg = $('#lbImg'), lbCap = $('#lbCap'), lbCount = $('#lbCount');
    /* <img> лайтбокса создаём здесь: в разметке img без src и размеров невалиден */
    if (!lbImg) {
      lbImg = d.createElement('img');
      lbImg.id = 'lbImg'; lbImg.alt = ''; lbImg.decoding = 'async';
      var stage = $('.lb__stage', lb);
      if (stage) stage.insertBefore(lbImg, stage.firstChild);
    }
    var list = [], li = 0, opener = null;
    var pad = function (n) { n = String(n); return n.length < 2 ? '0' + n : n; };
    var preload = function (a) { if (a) { var im = new Image(); im.src = a.href; } };
    var load = function (i, dir) {
      li = (i + list.length) % list.length;
      var a = list[li];
      var apply = function () {
        lbImg.classList.remove('is-ready', 'to-l', 'to-r');
        lbImg.onload = function () { lbImg.classList.add('is-ready'); };
        var th = $('img', a);
        lbImg.alt = th ? th.alt : '';
        lbImg.src = a.href;
        if (lbImg.complete && lbImg.naturalWidth) lbImg.classList.add('is-ready');
        lbCap.textContent = a.getAttribute('data-cap') || '';
        lbCount.textContent = pad(li + 1) + ' / ' + pad(list.length);
        preload(list[(li + 1) % list.length]); preload(list[(li - 1 + list.length) % list.length]);
      };
      if (dir && !reduce && lbImg.classList.contains('is-ready')) {
        lbImg.classList.add(dir > 0 ? 'to-l' : 'to-r');
        setTimeout(apply, 170);
      } else apply();
    };
    var open = function (a) {
      opener = a;
      /* обложка, которая есть и в галерее, открывается внутри галереи — со стрелками и счётчиком */
      if (a.getAttribute('data-lb') === 'hero') {
        var twin = $$('a[data-lb]').filter(function (x) { return x !== a && x.getAttribute('data-lb') !== 'hero' && x.href === a.href; })[0];
        if (twin) a = twin;
      }
      var g = a.getAttribute('data-lb') || 'all';
      list = $$('a[data-lb="' + g + '"]');
      /* обложки нет в галерее (генератор не повторяет её там): в лайтбоксе она первая, дальше — кадры галереи */
      var wth = a.getAttribute('data-lb-with');
      if (g === 'hero' && wth) list = [a].concat($$('a[data-lb="' + wth + '"]'));
      load(list.indexOf(a));
      lb.showModal(); d.body.style.overflow = 'hidden';
      setTimeout(function () { lb.classList.add('is-in'); }, 16);
    };
    var close = function () {
      lb.classList.remove('is-in');
      setTimeout(function () { if (lb.open) lb.close(); }, reduce ? 0 : 280);
    };
    d.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[data-lb]');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault(); open(a);
    });
    lb.addEventListener('close', function () { d.body.style.overflow = ''; lb.classList.remove('is-in'); if (opener) opener.focus({ preventScroll: true }); });
    lb.addEventListener('cancel', function (e) { e.preventDefault(); close(); });
    $('[data-lb-close]', lb).addEventListener('click', close);
    $$('[data-lb-prev]', lb).forEach(function (b) { b.addEventListener('click', function () { load(li - 1, -1); }); });
    $$('[data-lb-next]', lb).forEach(function (b) { b.addEventListener('click', function () { load(li + 1, 1); }); });
    lb.addEventListener('click', function (e) { if (e.target.classList && e.target.classList.contains('lb__stage')) close(); });
    lb.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); load(li - 1, -1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); load(li + 1, 1); }
      else if (e.key === 'Home') { e.preventDefault(); load(0); }
      else if (e.key === 'End') { e.preventDefault(); load(list.length - 1); }
    });
    var sx = 0, sy = 0, st0 = 0;
    lb.addEventListener('touchstart', function (e) { var t = e.changedTouches[0]; sx = t.clientX; sy = t.clientY; st0 = Date.now(); }, { passive: true });
    lb.addEventListener('touchend', function (e) {
      var t = e.changedTouches[0], dx = t.clientX - sx, dy = t.clientY - sy;
      if (Date.now() - st0 > 800) return;
      if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.3) load(li + (dx < 0 ? 1 : -1), dx < 0 ? 1 : -1);
      else if (dy > 90 && Math.abs(dy) > Math.abs(dx) * 1.5) close();
    }, { passive: true });
  }

  /* ---------- CTA: форм на сайте нет — все кнопки «Обсудить съёмку» — обычные ссылки на Telegram (data-cta="tg"). */

  /* ---------- «Перейти к содержанию»: переносим фокус на <main> сами — на 404 <base> превратил бы «#main» в переход на главную */
  $$('a.skip').forEach(function (a) {
    a.addEventListener('click', function (e) {
      var m = $('#main');
      if (!m) return;
      e.preventDefault();
      if (!m.hasAttribute('tabindex')) m.setAttribute('tabindex', '-1');
      m.focus({ preventScroll: true });
      m.scrollIntoView({ behavior: 'auto', block: 'start' });
    });
  });

  /* ---------- Наверх */
  $$('[data-top]').forEach(function (b) {
    b.addEventListener('click', function (e) {
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
      var lg = $('.logo'); if (lg) lg.focus({ preventScroll: true });
    });
  });
})();
