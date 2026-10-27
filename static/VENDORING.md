# Вендоринг фронтенда

`vendor/` содержит локальные копии Chart.js, чтобы страницы работали без
доступа к CDN:

- `chart.umd.min.js` — chart.js 4.4.1 (umd), с
  `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js`
- `chartjs-adapter-date-fns.bundle.min.js` — chartjs-adapter-date-fns 3.0.0, с
  `https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js`

Страницы подключают локальные файлы; если локальный файл недоступен,
`onerror` через `Capsule.vendorFallback()` синхронно подгружает CDN-копию
той же версии. Обновление: скачать новые файлы в `vendor/` и поменять URL
фолбэка в `index.html` / `charts.html` / `more.html`.
