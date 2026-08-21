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

  function start() {
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
  } else {
    start();
  }
})();
