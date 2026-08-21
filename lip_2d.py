"""
Прикусывание нижней губы — ЧЕСТНОЕ 2D, для сравнения с трёхмерной версией.

Здесь нет ни одного полигона в пространстве, ни камеры, ни света.
Кадр собирается из плоских залитых форм, как в векторной анимации:
контуры губ заданы контрольными точками, поза интерполируется между
тремя ключевыми положениями, порядок отрисовки сам даёт перекрытие —
зубы рисуются поверх нижней губы, поэтому «губа зашла под зубы»
получается само собой, без единого расчёта глубины.

Растеризатор свой: заливка полигонов со сглаживанием, numpy из Blender.
Кодирование — секвенсором Blender, ffmpeg в системе нет.

Запуск:
    blender --background --python lip_2d.py -- [--frames 1,40]
"""

import bpy, math, os, sys, zlib, struct
import numpy as np

ARGV = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "render", "2d")
if "--out" in ARGV:                            # чтобы веб-прогон не затирал основную секвенцию
    OUT = os.path.join(ROOT, ARGV[ARGV.index("--out") + 1])
os.makedirs(OUT, exist_ok=True)

W_PX, H_PX = 1920, 1080
if "--size" in ARGV:                           # для веба кадр нужен мельче
    W_PX = int(ARGV[ARGV.index("--size") + 1])
    H_PX = W_PX * 9 // 16
FPS, F_END = 30, 144
X_SPAN = 3.40                                  # ширина кадра в единицах рта
# Рот был широкий и плоский — это силуэт улыбки, а не прикуса.
# Сжатие по X делает губы полными, не трогая ни одну кривую.
XS = 0.74
SCALE = W_PX / X_SPAN
ONLY = None
if "--frames" in ARGV:
    ONLY = [int(v) for v in ARGV[ARGV.index("--frames") + 1].split(",")]

BITE_SIDE = -1.0
GLOSS = "--gloss" in ARGV

# --- палитра: те же тона, что и в трёхмерной версии -------------------
BG_TOP = np.array([0.945, 0.943, 0.940])
BG_BOT = np.array([0.900, 0.896, 0.893])
if "--bg" in ARGV:
    # Плоская заливка под цвет страницы: на сайте видео ложится в панель,
    # и любой градиент в фоне выдал бы прямоугольник. Возводим в 2.2,
    # потому что write_png кодирует гамму на выходе.
    _h = ARGV[ARGV.index("--bg") + 1].lstrip("#")
    BG_TOP = BG_BOT = np.array([int(_h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]) ** 2.2
LIP_LIGHT = np.array([0.720, 0.055, 0.115])
LIP_MID = np.array([0.505, 0.028, 0.070])
LIP_DARK = np.array([0.265, 0.020, 0.070])     # тень уходит в холод
LIP_BOUNCE = np.array([0.455, 0.075, 0.100])
MOUTH = np.array([0.055, 0.010, 0.025])
CONTACT = np.array([0.185, 0.018, 0.060])
TOOTH = np.array([0.960, 0.955, 0.945])
TOOTH_SH = np.array([0.815, 0.815, 0.825])
INK = np.array([0.075, 0.020, 0.045])
GLOSS_C = np.array([0.980, 0.760, 0.790])


# =============================================================================
# Геометрия форм
# =============================================================================
def sstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def grip(x, sl=0.0):
    """Пятно захвата. sl — выскальзывание: пятно медленно уезжает к
    уголку и слабеет. Прежде выскальзывания не было вовсе, за него
    работал затухающий синус на нижнем контуре — а один качок читается
    ударом, а не скольжением."""
    c = BITE_SIDE * (0.22 + 0.40 * sl)
    g = math.exp(-((x - c) / 0.42) ** 2)
    return g * (1.0 - sstep(0.55, 0.99, abs(x)))


def y_upper_outer(x):
    a = abs(x)
    return (0.395 * (1.0 - a ** 1.80) ** 0.60
            - 0.082 * math.exp(-(x / 0.165) ** 2)
            + 0.048 * math.exp(-((a - 0.30) / 0.135) ** 2))


def y_upper_inner(x):
    """Верхний край щели рта."""
    return 0.030 * (1.0 - x * x) ** 0.70


def y_lower_inner(x, r, sl):
    """Верхний край нижней губы. Замах убран совсем: он опускал край
    и тут же возвращал, губа сплющивалась и надувалась обратно —
    лишнее движение перед действием."""
    amt = r * (1.0 - 0.40 * sl)
    jaw = 0.012 * amt * (1.0 - x * x) ** 0.5
    return -0.240 * (1.0 - x * x) ** 0.95 + jaw + 0.265 * amt * grip(x, sl)


def y_lower_outer(x, r, sl):
    """Нижний контур. Он ОБЯЗАН подниматься вместе с заходом, иначе
    губы снизу остаётся столько же и объём не убывает. Затухающий
    качок убран — он и читался ударом."""
    amt = r * (1.0 - 0.40 * sl)
    jaw = 0.010 * amt * (1.0 - x * x) ** 0.5
    return (-0.620 * (1.0 - x * x) ** 0.52 + jaw
            + 0.190 * amt * grip(x, sl) ** 0.55)


# Границы зубов и длина режущей кромки по номеру от центра.
# Ряд заканчивается ДО спайки: у самого уголка рта зубов не видно,
# там темнота, и обрывать ряд белой стенкой нельзя.
# Ширины не равные: центральный резец самый широкий, дальше сужение.
# Одинаковые зубы читаются клавишами, а не зубами.
TOOTH_EDGES = (-0.830, -0.720, -0.610, -0.490, -0.350, -0.200, 0.0,
               0.200, 0.350, 0.490, 0.610, 0.720, 0.830)
TOOTH_LEN = (1.0, 0.87, 0.94, 0.78, 0.64, 0.48)
GAP = 0.006


def y_tooth_edge(x, b):
    # Кромка загибается ВВЕРХ к краям: при -0.062*x*x лента была выше
    # у уголков, чем в середине, и читалась ровной чертой. Живой ряд
    # полнее по центру и сходит к краям.
    return -0.034 + 0.034 * x * x - 0.012 * b


def tooth_len(i):
    n = len(TOOTH_EDGES) - 1
    k = (n // 2 - 1 - i) if i < n // 2 else (i - n // 2)
    return TOOTH_LEN[min(k, len(TOOTH_LEN) - 1)]


def tooth_shape(i, b, top_fn, grow=0.0):
    """Отдельный зуб со скруглённой режущей кромкой. Сплошная плашка
    с волосяными линиями между зубами читается плашкой, а не зубами:
    у зуба должна быть своя скруглённая кромка и свой просвет."""
    x0 = TOOTH_EDGES[i] + GAP * 0.5 - grow
    x1 = TOOTH_EDGES[i + 1] - GAP * 0.5 + grow
    cx = 0.5 * (x0 + x1)
    ln = tooth_len(i)
    bot = y_tooth_edge(cx, b) + (1.0 - ln) * 0.115 - grow
    top = top_fn(cx)
    r = min(0.030, (x1 - x0) * 0.42)
    pts = [(x0, top), (x0, bot + r)]
    for k in range(9):                      # левый нижний угол
        a = math.pi * (1.0 + 0.5 * k / 8.0)
        pts.append((x0 + r + r * math.cos(a), bot + r + r * math.sin(a)))
    for k in range(9):                      # правый нижний угол
        a = math.pi * (1.5 + 0.5 * k / 8.0)
        pts.append((x1 - r + r * math.cos(a), bot + r + r * math.sin(a)))
    pts.append((x1, top))
    return pts


def curve(fn, x0, x1, n=180):
    return [(x0 + (x1 - x0) * i / n, fn(x0 + (x1 - x0) * i / n)) for i in range(n + 1)]


def stroke(path, w0, w1):
    """Обводка с переменным весом: полигон, полученный сдвигом пути
    по нормали. Постоянная толщина мертва — живая линия имеет вес."""
    n = len(path)
    left, right = [], []
    for i, (x, y) in enumerate(path):
        px, py = path[max(i - 1, 0)]
        nx, ny = path[min(i + 1, n - 1)]
        dx, dy = nx - px, ny - py
        L = math.hypot(dx, dy) or 1.0
        ox, oy = -dy / L, dx / L
        w = (w0 + (w1 - w0) * (i / (n - 1))) * 0.5
        left.append((x + ox * w, y + oy * w))
        right.append((x - ox * w, y - oy * w))
    return left + right[::-1]


# =============================================================================
# Растеризатор
# =============================================================================
def to_px(pts):
    out = np.empty((len(pts), 2), np.float64)
    for i, (x, y) in enumerate(pts):
        out[i, 0] = (x * XS + X_SPAN * 0.5) * SCALE
        out[i, 1] = H_PX * 0.5 - y * SCALE
    return out


def coverage(pts, ss=4):
    """Заливка по чётности пересечений: подпиксельная выборка по Y,
    аналитическое покрытие по X. Даёт чистый край без ступенек."""
    p = to_px(pts)
    cov = np.zeros((H_PX, W_PX), np.float32)
    y0 = max(0, int(np.floor(p[:, 1].min())))
    y1 = min(H_PX - 1, int(np.ceil(p[:, 1].max())))
    if y1 < y0:
        return cov
    xa, ya = p[:, 0], p[:, 1]
    xb, yb = np.roll(xa, -1), np.roll(ya, -1)
    dy = yb - ya
    live = dy != 0
    xa, ya, xb, yb, dy = xa[live], ya[live], xb[live], yb[live], dy[live]
    ylo, yhi = np.minimum(ya, yb), np.maximum(ya, yb)
    wgt = 1.0 / ss
    for row in range(y0, y1 + 1):
        acc = cov[row]
        for s in range(ss):
            yy = row + (s + 0.5) / ss
            hit = (ylo <= yy) & (yhi > yy)
            if not hit.any():
                continue
            xs = np.sort(xa[hit] + (yy - ya[hit]) / dy[hit] * (xb[hit] - xa[hit]))
            for k in range(0, len(xs) - 1, 2):
                _span(acc, xs[k], xs[k + 1], wgt)
    return np.clip(cov, 0.0, 1.0)


def _span(row, x0, x1, w):
    x0, x1 = max(x0, 0.0), min(x1, float(W_PX))
    if x1 <= x0:
        return
    i0, i1 = int(x0), int(min(x1, W_PX - 1e-6))
    if i0 == i1:
        row[i0] += (x1 - x0) * w
        return
    row[i0] += (i0 + 1 - x0) * w
    if i1 > i0 + 1:
        row[i0 + 1:i1] += w
    row[i1] += (x1 - i1) * w


def draw(buf, pts, color, alpha=1.0):
    if len(pts) < 3:
        return
    c = coverage(pts)
    if alpha != 1.0:
        c *= alpha
    m = c[:, :, None]
    buf *= (1.0 - m)
    buf += m * color


# =============================================================================
# Тайминг — тот же, что в трёхмерной версии
# =============================================================================
def smoother(t):
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (t * 6 - 15) + 10)


def ease_out(x, p=2.4):
    x = max(0.0, min(1.0, x))
    return 1.0 - (1.0 - x) ** p


# 1..14   покой
# 14..40  заход губы
# 21..45  ЗУБЫ — старт ровно на половине хода губы, внахлёст
# 45..62  осадка нажима
# 62..134 ВЫСКАЛЬЗЫВАНИЕ
# 134..144 покой в новом положении
#
# Стык двух фаз встык и давал рваность: губа доходила до упора,
# кадр тишины, потом трогались зубы. Перекрытие в 13 кадров сшивает
# движение в одно, при этом зубы всё равно приходят последними.
F_IN, F_TOP = 14, 40
F_BITE, F_BITE_LEN = 21, 24
F_SLIP0, F_SLIP1 = 62, 134


def master(f):
    if f <= F_IN:
        return 0.0
    if f < F_TOP:
        return ease_out((f - F_IN) / (F_TOP - F_IN), 2.2)
    return 1.0


def bite(f):
    x = max(0.0, min(1.0, (f - F_BITE) / F_BITE_LEN))
    v = ease_out(x)
    return v + 0.10 * math.exp(-((f - (F_BITE + F_BITE_LEN)) / 6.0) ** 2) * v


def slip(f):
    """Выскальзывание — ДЛИННОЕ и ровное. Импульсом оно выглядело
    потому, что его вообще не было: за него работал затухающий синус."""
    if f <= F_SLIP0:
        return 0.0
    return smoother((f - F_SLIP0) / (F_SLIP1 - F_SLIP0))


# =============================================================================
# Сборка кадра
# =============================================================================
def mix(a, b, w):
    return a * (1.0 - w) + b * w


def lens(top_fn, bot_fn, x0, x1, fc, half, n=90):
    """Блик формой линзы: сходит на нет к концам. В мультяшной подаче
    блик — заданная форма с жёстким краем, а не растяжка."""
    up, dn = [], []
    for i in range(n + 1):
        t = i / n
        x = x0 + (x1 - x0) * t
        env = math.sin(math.pi * t) ** 0.75
        h = top_fn(x) + (bot_fn(x) - top_fn(x)) * fc
        d = (bot_fn(x) - top_fn(x)) * half * env
        up.append((x, h - d))
        dn.append((x, h + d))
    return dn + up[::-1]


def band(top_fn, bot_fn, f0, f1, x0=-1.0, x1=1.0):
    """Полоса между двумя кривыми по долям f0..f1 — так рисуют плоскую
    светотень: не градиентом, а границей."""
    up = [(x, top_fn(x) + (bot_fn(x) - top_fn(x)) * f0) for x, _ in curve(lambda t: 0, x0, x1)]
    dn = [(x, top_fn(x) + (bot_fn(x) - top_fn(x)) * f1) for x, _ in curve(lambda t: 0, x0, x1)]
    return up + dn[::-1]


def render_frame(f):
    r, bt, sl = master(f), bite(f), slip(f)
    gmax = max(grip(BITE_SIDE * 0.22), 1e-6)

    gy = np.linspace(0.0, 1.0, H_PX)[:, None, None]
    buf = (BG_TOP[None, None, :] * (1 - gy) + BG_BOT[None, None, :] * gy)
    buf = np.repeat(buf, W_PX, axis=1).astype(np.float32)

    # ВЕРХ ЕДЕТ ЦЕЛИКОМ: верхняя губа вместе с зубами. Высота белой
    # ленты почти не меняется, меняется её положение — это и читается
    # опусканием челюсти.
    # Сдвиг гаснет к спайке: уголок рта закреплён, он никуда не едет.
    # Пока сдвиг был общим, у самого уголка край верхней губы уходил
    # НИЖЕ края нижней, и на финале губы пересекались крестом.
    def dzf(x):
        # опускание верха уменьшено: при -0.052 губа догоняла ряд
        # и белая лента схлопывалась в черту
        return -0.024 * bt * (1.0 - sstep(0.50, 0.94, abs(x)))

    def up_o(x):
        return y_upper_outer(x) + dzf(x)

    def up_i(x):
        return y_upper_inner(x) + dzf(x)

    def lo_raw(x):
        return y_lower_inner(x, r, sl)

    def lo_out(x):
        return y_lower_outer(x, r, sl)

    def lo_in(x):
        """ВИДИМЫЙ край нижней губы. Смешивание по пятну захвата было
        ошибкой: вне пятна губа наползала на ряд и закрывала его, из-за
        чего оставался один зуб — и это читалось выбитым зубом, а не
        прикусом. Ряд виден ВЕСЬ и всегда; губа просто не может
        подняться выше режущей кромки — она за зубами."""
        # у спайки щель обязана сомкнуться: без этого край губы уходил
        # ниже уголка и лента зубов вылезала за силуэт прямоугольником
        op = 1.0 - sstep(0.70, 0.97, abs(x))
        return lo_raw(x) * (1.0 - op) + min(lo_raw(x), y_tooth_edge(x, bt) + dzf(x)) * op

    def t_top(x):
        return min(up_i(x) + 0.030, up_o(x) - 0.040)

    up_out = curve(up_o, -0.995, 0.995)
    up_in = curve(up_i, -1.0, 1.0)
    lo_i = curve(lo_in, -1.0, 1.0)
    lo_o = curve(lo_out, -0.995, 0.995)

    # 1. тёмная масса рта
    # масса рта идёт до ВИДИМОГО края губы: по lo_raw у спайки
    # оставалась незакрытая щель и туда светил фон белым клином
    draw(buf, up_in + lo_i[::-1], MOUTH)

    # 2. ЗУБЫ — ОДНА ПОЛОСКА. На референсе отдельных зубов не видно
    #    вообще: это узкая белая лента между верхней губой и провалом
    #    рта. Ряд из двенадцати форм в такой подаче лишний, и именно он
    #    всё время выходил кривым.
    def tap(x):
        """Гашение ленты к уголкам. Раньше начиналось с 0.58 — ряд
        получался коротким; теперь держится почти до спайки."""
        return 1.0 - sstep(0.74, 0.93, abs(x))

    def edge(x):
        """Лента ограничена щелью и ЗАКАНЧИВАЕТСЯ ДО УГОЛКОВ: ряд уходит
        в полость рта, у спайки зубов не видно, там темнота. Доведённая
        до уголка лента читалась белым клином, вставленным в мякоть."""
        lo = max(y_tooth_edge(x, bt) + dzf(x), lo_in(x))
        t = tap(x)
        return up_i(x) + (lo - up_i(x)) * t
    draw(buf, curve(up_i, -0.94, 0.94) + curve(edge, -0.94, 0.94)[::-1], TOOTH)
    # Тень по верху ленты гаснет ТЕМ ЖЕ гашением. Она шла на всю
    # ширину и оставляла белую нитку там, где зубов уже нет.
    def sh(x):
        return up_i(x) - 0.016 * tap(x)
    draw(buf, curve(up_i, -0.94, 0.94) + curve(sh, -0.94, 0.94)[::-1], TOOTH_SH)
    # намёк на отдельные зубы: тонкие просветы, НЕ доходящие до кромки.
    # Доведённые до низа они снова превращают ленту в частокол.
    for bx in (-0.44, -0.27, -0.10, 0.10, 0.27, 0.44):
        top = up_i(bx) - 0.012
        bot = edge(bx)
        if top - bot < 0.05:
            continue
        lo_lim = bot + (top - bot) * 0.34
        draw(buf, [(bx - 0.004, top), (bx + 0.004, top),
                   (bx + 0.004, lo_lim), (bx - 0.004, lo_lim)], TOOTH_SH, 0.75)

    # 3. НИЖНЯЯ ГУБА поверх ряда: где она впереди — ряд закрыт,
    #    где ушла за зубы — они её перекрывают
    draw(buf, lo_i + lo_o[::-1], LIP_MID)
    # Ступеней больше: на референсе переход мягкий, а три полосы
    # читаются плакатом. Край всё равно остаётся жёстким — это
    # по-прежнему заливки, а не растяжка.
    draw(buf, band(lo_in, lo_out, 0.00, 0.26), LIP_DARK)
    draw(buf, band(lo_in, lo_out, 0.30, 0.62), LIP_LIGHT)
    draw(buf, band(lo_in, lo_out, 0.78, 0.92), LIP_DARK)
    draw(buf, band(lo_in, lo_out, 0.92, 1.00), LIP_BOUNCE)
    if bt > 0.02:
        def sh_bot(x):
            # тень глубже и только в пятне прикуса: она и есть
            # единственный признак нажима на плоской заливке
            return lo_in(x) - 0.085 * bt * (grip(x, sl) / gmax) ** 1.6
        draw(buf, lo_i + curve(sh_bot, -1.0, 1.0)[::-1], CONTACT)
    draw(buf, stroke(lo_o, 0.016, 0.016), INK)
    draw(buf, stroke(lo_i, 0.009, 0.009), INK)

    # 4. ВЕРХНЯЯ ГУБА поверх всего
    draw(buf, up_out + up_in[::-1], LIP_MID)
    draw(buf, band(up_o, up_i, 0.00, 0.42), LIP_LIGHT)
    draw(buf, band(up_o, up_i, 0.74, 1.00), LIP_DARK)
    draw(buf, stroke(up_out, 0.013, 0.013), INK)
    draw(buf, stroke(up_in, 0.010, 0.010), INK)

    if GLOSS:
        draw(buf, lens(lo_in, lo_out, 0.06, 0.55, 0.46, 0.10), GLOSS_C)
        draw(buf, lens(lo_in, lo_out, -0.60, -0.34, 0.54, 0.06), GLOSS_C)
        draw(buf, lens(up_o, up_i, 0.20, 0.50, 0.58, 0.07), GLOSS_C)

    # 5. уголки — заданная тёмная запятая
    for sg in (-1.0, 1.0):
        cx = sg * 0.955
        pts = [(cx - sg * 0.045, up_i(cx - sg * 0.045)),
               (cx, 0.0),
               (cx - sg * 0.045, lo_raw(cx - sg * 0.045))]
        draw(buf, pts, INK, 0.9)

    return np.clip(buf, 0.0, 1.0)


def write_png(path, buf):
    a = (buf ** (1 / 2.2) * 255.0 + 0.5).astype(np.uint8)
    raw = np.hstack([np.zeros((H_PX, 1), np.uint8), a.reshape(H_PX, W_PX * 3)])
    comp = zlib.compress(raw.tobytes(), 6)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", W_PX, H_PX, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", comp))
        fh.write(chunk(b"IEND", b""))


def encode():
    """Кодирование секвенсором Blender: ffmpeg в системе нет."""
    sc = bpy.context.scene
    sc.render.resolution_x, sc.render.resolution_y = W_PX, H_PX
    sc.render.fps = FPS
    sc.frame_start, sc.frame_end = 1, F_END
    sc.sequence_editor_create()
    seq = sc.sequence_editor
    # ПУСТАЯ коллекция ложна: getattr(...) or ... уводил на старое имя
    holder = seq.strips if hasattr(seq, "strips") else seq.sequences
    st = holder.new_image(name="frames", filepath=os.path.join(OUT, "f0001.png"),
                          channel=1, frame_start=1)
    for i in range(2, F_END + 1):
        st.elements.append("f%04d.png" % i)
    r = sc.render
    if "media_type" in r.image_settings.bl_rna.properties:
        r.image_settings.media_type = 'VIDEO'
    r.image_settings.file_format = 'FFMPEG'
    r.ffmpeg.format = 'MPEG4'
    r.ffmpeg.codec = 'H264'
    r.ffmpeg.constant_rate_factor = 'PERC_LOSSLESS'
    r.ffmpeg.audio_codec = 'NONE'
    r.filepath = os.path.join(os.path.dirname(OUT), "lip_2d_")
    bpy.ops.render.render(animation=True)


frames = ONLY if ONLY else range(1, F_END + 1)
if "--encode" not in ARGV:
    for f in frames:
        write_png(os.path.join(OUT, "f%04d.png" % f), render_frame(f))
        print("кадр", f)
if not ONLY:
    encode()
print("2D ГОТОВО")
