// Кадры видео перерисовываются в холст: у <video> свой цветовой конвейер,
// и запечённый фон уезжает от CSS-цвета на 1-2 единицы — виден прямоугольник.
// Холст красится тем же путём, что и обычные цвета, поэтому стык пропадает.
document.querySelectorAll('.lips').forEach(function (box) {
  var v = box.querySelector('video');
  var c = box.querySelector('canvas');
  var ctx = c.getContext('2d', { alpha: false });

  function draw() { if (v.readyState >= 2) ctx.drawImage(v, 0, 0, c.width, c.height); }

  if (v.requestVideoFrameCallback) {
    var onFrame = function () { draw(); v.requestVideoFrameCallback(onFrame); };
    v.requestVideoFrameCallback(onFrame);
    v.addEventListener('loadeddata', draw);
  } else {
    (function pump() { draw(); requestAnimationFrame(pump); })();
  }

  v.playbackRate = 1.5;
  v.addEventListener('ended', function () {
    setTimeout(function () { v.currentTime = 0; v.play().catch(function () {}); }, 1100);
  });
  var p = v.play();
  if (p && p.catch) p.catch(function () {});
});
