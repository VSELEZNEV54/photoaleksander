# Архитектура сборки сайта ANPhotoLab

Статический многостраничный сайт. Генератор на Python 3.9 без зависимостей (только stdlib), чтобы любой мог пересобрать: `python3 build.py` → папка `site/` готова к заливке на любой хостинг.

## Структура

```
САНЯ/
  build.py                 генератор (stdlib only, Python 3.9 совместим: без match/case, без | в типах)
  README.md                как собрать, как добавить фото/страницу, что заменить перед запуском
  src/
    config.json            site_url, бренд, контакты, навигация, соцсети, счётчики (пусто)
    assets/css/main.css    вся дизайн-система: токены в :root + компоненты + блоки
    assets/js/main.js      меню, reveal, лайтбокс, фильтр/табы, форма (фронт), без зависимостей
    assets/img/            favicon.svg, og-заглушка при необходимости (SVG→ нельзя в OG, поэтому OG = фото с CDN)
  content/
    photos.json            манифест фото (из _work/photos.json + поле alt по жанру)
    reviews.json           отзывы (из брифа)
    pages/<name>.json      одна страница = один файл (см. схему ниже)
  site/                    СГЕНЕРИРОВАНО, не редактировать руками
    index.html
    svadby/index.html  ... (чистые URL со слэшем)
    sitemap.xml robots.txt 404.html assets/...
```

## Схема страницы `content/pages/<name>.json`

```json
{
  "slug": "/korporativy/",
  "type": "genre | seo | service | home",
  "nav": "Корпоративы",                 // подпись во вкладках (для genre)
  "title": "...", "description": "...", "h1": "...",
  "og_image": 9,                          // id фото
  "breadcrumbs": true,
  "schema": ["Service", "FAQPage", "BreadcrumbList"],
  "blocks": [ { "type": "...", ... } ]
}
```

## Библиотека блоков (рендерит build.py, стили — main.css)

Точные поля каждого блока задаёт core-агент и документирует в `content/BLOCKS.md` с примерами. Минимальный набор:

- `page-hero` — H1, лид (антиква), фон/кадр (id фото), eyebrow-метка (жанр / номер раздела), CTA.
- `genre-tabs` — вкладки по жанрам (sticky-лента под шапкой на страницах жанров и в портфолио), активная — текущая.
- `gallery` — фото по жанру или списку id; раскладка `masonry | editorial | strip`; лайтбокс; учитывает ориентацию; `limit`.
- `text` — заголовок H2 + абзацы (markdown-lite: **жирный**, ссылки [текст](/slug/)), опционально колонка-сноска.
- `features` — 3–6 пунктов «что входит / почему со мной» (номер, заголовок, текст).
- `steps` — этапы работы.
- `formats` — форматы съёмки/пакеты БЕЗ цен («Стоимость — по запросу»), что входит.
- `quote` — одна крупная цитата-отзыв (антиква).
- `reviews` — список отзывов по id из reviews.json.
- `faq` — аккордеон на <details>, + JSON-LD FAQPage автоматически.
- `related` — перелинковка: карточки других страниц по slug (подтягивает h1/обложку автоматически).
- `cta` — финальный призыв + контакты + форма заявки.
- `genre-index` — индекс жанров для главной и хаба услуг.
- `split` — фото + текст бок о бок.

Каждый блок рендерится в `<section class="block block--<type>">` и получает reveal-анимацию.

## SEO, которое build.py делает автоматически
- `<title>`, meta description, canonical (`site_url + slug`), OG/Twitter (og:image — large URL фото), `<html lang="ru">`.
- JSON-LD: на всех страницах `ProfessionalService`(+`LocalBusiness`) с areaServed Новосибирск, телефон, sameAs (Telegram/WhatsApp) и `Person` (Александр Непомнящих, jobTitle «Фотограф и видеограф»); `BreadcrumbList` на внутренних; `Service` на genre/seo; `FAQPage` если есть блок faq.
- Хлебные крошки (видимые) на внутренних.
- `sitemap.xml` (все страницы, lastmod = дата сборки), `robots.txt` (Sitemap:, Host не нужен), `404.html`.
- alt у каждого фото: из photos.json (`alt` по жанру + номер/описание), не пустой.
- Один H1 на страницу; иерархия H2/H3 без пропусков.
- Внутренняя перелинковка: футер-карта сайта + блок `related` + ссылки в текстах.
- Картинки: `srcset` (thumb 480w, mid 1280w, large 1920w+), `sizes`, `width/height`, `loading="lazy"` (кроме героя: `fetchpriority="high"`), `decoding="async"`.
- `<link rel="preconnect">` к fonts.googleapis.com, fonts.gstatic.com, i.wfolio.ru.

## Общие требования
- ТОЛЬКО ФРОНТ, НИКАКОГО БЭКЕНДА. Результат — папка `site/` с чистыми HTML/CSS/JS, которая открывается двойным кликом по `site/index.html` (file://) без сервера и так же работает на любом статическом хостинге. `build.py` — только инструмент разработчика для генерации HTML, в рантайме его нет.
- Поэтому ВСЕ внутренние ссылки и пути к ассетам — ОТНОСИТЕЛЬНЫЕ с явным `index.html`: со страницы `/svadby/` на главную — `../index.html`, на отзывы — `../otzyvy/index.html`, CSS — `../assets/css/main.css`; с главной — `svadby/index.html`, `assets/css/main.css`. Build.py вычисляет префикс по глубине страницы. Canonical, og:url, sitemap, JSON-LD — абсолютные чистые URL от `site_url` (`https://anphotolab.ru/svadby/`).
- Для проверки в браузере используем http://127.0.0.1:8080/ (сервер уже запущен и отдаёт папку site/); относительные ссылки работают и там, и с file://.
- Форма заявки — кликабельная заглушка без отправки на сервер: валидация полей, по submit — состояние «Заявка принята» (демо) + две кнопки «Написать в Telegram» / «Написать в WhatsApp» с предзаполненным текстом заявки (wa.me/79529427787?text=…, t.me/+79529427787 + копирование текста в буфер). Никаких fetch/XHR, сторонних сервисов и серверных файлов (.htaccess, _redirects, php не создавать).
- Всё кликабельно: шапка и мобильное меню, вкладки жанров, карточки жанров, лайтбокс (стрелки, клавиатура, свайп, Esc, счётчик), FAQ-аккордеоны, якорные CTA к форме, tel:, мессенджеры, хлебные крошки, «наверх», 404 со ссылками.
- Доступность: фокус-стили, skip-link, aria у меню/лайтбокса, контраст AA, `prefers-reduced-motion`.
- Производительность: CSS один файл, JS один файл с `defer`, никакого jQuery/библиотек.
