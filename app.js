(function () {
  var RATE = 1.7;        // во сколько раз ускоряем исходные 30 fps
  var FLY_MS = 1400;     // длительность пролёта
  var GUARD_MS = 6000;   // крайняя страховка, если всё пошло не так

  var video = document.getElementById('lips');
  var canvas = document.getElementById('lipsCanvas');
  var ctx = canvas.getContext('2d', { alpha: false });

  /* ── перерисовка кадров в холст ─────────────────────────────
     нужна не для эффекта, а ради цвета: у <video> свой конвейер,
     и запечённый фон уезжает от CSS-цвета на 1-2 единицы. */
  function draw() {
    if (video.readyState >= 2) ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  }
  if (video.requestVideoFrameCallback) {
    var onFrame = function () { draw(); video.requestVideoFrameCallback(onFrame); };
    video.requestVideoFrameCallback(onFrame);
    video.addEventListener('loadeddata', draw);
  } else {
    (function pump() { draw(); requestAnimationFrame(pump); })();
  }

  /* ── пролёт ── */
  var loaded = document.readyState === 'complete';
  var wanted = false;
  var done = false;

  function reveal() {
    if (done) return;
    if (!loaded) { wanted = true; return; }   // ждём, пока страница дозагрузится
    done = true;
    document.body.classList.remove('is-loading');
    document.body.classList.add('is-ready');
    setTimeout(function () {
      document.body.classList.add('is-settled');
      video.pause();                          // экран загрузки улетел, декодировать больше нечего
    }, FLY_MS);
  }

  window.addEventListener('load', function () {
    loaded = true;
    if (wanted) reveal();
  });

  /* Кадры гоним сами перемоткой — на случай, когда автовоспроизведение
     запрещено: энергосбережение, отключённый автоплей в настройках сайта,
     отладочные панели. Политика автоплея перемотку не ограничивает, а seek
     по уже загруженному файлу занимает доли миллисекунды. */
  function spin() {
    var t0 = performance.now();
    (function step() {
      if (done) return;
      if (!isFinite(video.duration)) { requestAnimationFrame(step); return; }
      var t = (performance.now() - t0) / 1000 * RATE;
      if (t >= video.duration) { reveal(); return; }
      video.currentTime = t;
      requestAnimationFrame(step);
    })();
  }

  function checkPlayback() {
    if (done) return;
    if (video.readyState < 2) { setTimeout(checkPlayback, 200); return; }  // ещё грузится
    if (!video.paused && video.currentTime > 0.03) return;                 // играет само
    video.pause();
    spin();
  }

  var started = false;

  function start() {
    if (started) return;
    started = true;
    // при перезагрузке браузер восстанавливает позицию видео с прошлого раза,
    // и прикус «заканчивался» ещё до того, как начался
    try { video.currentTime = 0; } catch (e) {}
    video.playbackRate = RATE;
    var play = video.play();
    if (play && play.catch) play.catch(function () {});
    video.addEventListener('ended', reveal);
    setTimeout(checkPlayback, 420);
    setTimeout(function () { loaded = true; reveal(); }, GUARD_MS);
  }

  /* Пока вкладка не на экране, браузер глушит и автоплей, и
     requestAnimationFrame. Если запуститься вслепую, вся загрузка отыграет
     в фоне, и человек, вернувшись, увидит уже готовую страницу без анимации.
     Поэтому ждём, когда на страницу действительно посмотрят. */
  if (document.hidden) {
    document.addEventListener('visibilitychange', function onShow() {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', onShow);
      start();
    });
    // Если браузер почему-то так и не скажет, что страницу показали,
    // стартуем сами: проиграть анимацию мимо зрителя не страшно,
    // а вот навсегда застрять на экране загрузки — страшно.
    setTimeout(start, 8000);
  } else {
    start();
  }

  /* ═══ карточка PUBG ═══ */
  var modal = document.getElementById('pubg');
  var opener = null;

  function openModal() {
    opener = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
    var x = modal.querySelector('.modal__x');
    if (x) x.focus();
  }
  function closeModal() {
    modal.hidden = true;
    document.body.style.overflow = '';
    if (opener && opener.focus) opener.focus();
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-open="pubg"]')) { openModal(); return; }
    if (e.target.closest('[data-close]')) { closeModal(); return; }

    var btn = e.target.closest('[data-copy]');
    if (!btn) return;

    var node = document.getElementById(btn.dataset.copy);
    var label = btn.querySelector('span');
    var icon = btn.querySelector('use');
    var was = label.textContent;

    copy(node.textContent.trim()).then(function (ok) {
      if (ok) {
        label.textContent = 'скопировано';
        icon.setAttribute('href', '#i-check');
        btn.classList.add('is-done');
      } else {
        // Встроенные браузеры соцсетей копирование иногда запрещают вовсе.
        // Молча ничего не делать нельзя — выделяем текст, чтобы человек
        // взял его сам долгим нажатием.
        label.textContent = 'выдели и скопируй';
        btn.classList.add('is-failed');
        var r = document.createRange();
        r.selectNodeContents(node);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      }
      setTimeout(function () {
        label.textContent = was;
        icon.setAttribute('href', '#i-copy');
        btn.classList.remove('is-done', 'is-failed');
      }, ok ? 1600 : 3000);
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.hidden) closeModal();
  });

  /* Clipboard API живёт только в защищённом контексте, а встроенные
     браузеры соцсетей его порой и вовсе не дают — отсюда запасной путь
     через временное поле и execCommand. */
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; },
                                                      function () { return legacy(text); });
    }
    return Promise.resolve(legacy(text));
  }
  function legacy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    return ok;
  }
})();
