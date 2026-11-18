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

## Поверхность мозга

`brain_surface.js` — **изменённая и упрощённая** усреднённая pial-поверхность
FreeSurfer fsaverage5, полученная из Nilearn. Это общая анатомическая иллюстрация,
не реконструкция участника. Файл содержит лицензии и не требует сети при работе.

All or portions of this licensed product (such portions are the "Software") have
been obtained under license from The General Hospital Corporation "MGH" and are
subject to the terms and conditions in [FreeSurfer Software License](vendor/freesurfer-LICENSE.txt).
Дополнительно сохранена [лицензия Nilearn](vendor/nilearn-LICENSE.txt).

Источник: `https://raw.githubusercontent.com/nilearn/nilearn/8a558ba11867c5f1ae91f3abe4ca5a831d419e7e/nilearn/datasets/data/fsaverage5/`.
Нужны `pial_{left,right}.gii.gz`, `sphere_{left,right}.gii.gz`, `sulc_{left,right}.gii.gz`.
Сборка: `python scripts/build_brain_surface.py /каталог/с/файлами`. Требуются NumPy
и SciPy из основных зависимостей проекта.

Сетку сокращают в сферических координатах до 2562 вершин на полушарие; это
сохраняет раздельные берега борозд. RAS преобразуют в координаты схемы
(x справа, y вверх, z сзади), масштабируют и округляют. Итог: 5124 вершины,
10240 треугольников. Значения sulc используются только для постоянной светотени.

SHA-256 исходников:

- `pial_left.gii.gz`: `1e76fe43ac194c15fd272643f7ae7995621e2a496b3102b2d6175f0f8e6d7fc8`
- `sphere_left.gii.gz`: `1047fe84e0055f40896afaba320edcc791e84f459daadc4b0002850baf8e79f3`
- `sulc_left.gii.gz`: `bd0f87e0cba153e9d5c5281edc7267f631b1967e5883d4027924766c8430eb62`
- `pial_right.gii.gz`: `fdfae008bc10acf7cba82737ea5db9a7298948c41884a2d3330a785783a60a91`
- `sphere_right.gii.gz`: `08899ce6022b739ceb4e5f925e947a79041fc505007c46ace3454ad02587ea6b`
- `sulc_right.gii.gz`: `97afe76225903205a16b88f88cd796945f2a27011328e98decaa9128ff063afb`
