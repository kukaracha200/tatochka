"""
Прикусывание нижней губы, стилизация под глянцевую иллюстрацию.
Blender 5.x, Cycles/Metal.

Два принципиальных решения этой версии:

  1) Губы — ОДНА непрерывная кольцевая поверхность (обход рта × сечение).
     В уголках верхний и нижний профиль плавно перетекают друг в друга,
     стыка и схождения в точку нет — именно они давали резкие углы.

  2) Заворот губы — ЖЁСТКИЙ поворот сечения вокруг оси у основания губы,
     а не сжатие областей. Поворот сохраняет объём, поэтому губа
     заворачивается, а не мнётся, как пакет.

Механика прикуса: губа заходит под зубы -> зубы ставятся на губу ->
губа постепенно выскальзывает из-под зубов.

Запуск:
    blender --background --python lip_bite.py -- [--test] [--frames 1,90]
"""

import bpy, bmesh, math, os, sys
from mathutils import Vector

ARGV = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
TEST = "--test" in ARGV
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render")
os.makedirs(OUT_DIR, exist_ok=True)

FPS, F_END = 30, 96
RES = (1920, 1080)
SAMPLES = 96

# Режимы для работы через MCP в живой сессии Blender:
#   LIPBITE_BUILD_ONLY=1 — собрать сцену и не рендерить
#   LIPBITE_FAST=1       — черновая плотность сетки для быстрых правок формы
#   LIPBITE_CLAY=1       — серый макет: форма и движение без цвета и бликов
BUILD_ONLY = os.environ.get("LIPBITE_BUILD_ONLY") == "1"
FAST = os.environ.get("LIPBITE_FAST") == "1"
CLAY = os.environ.get("LIPBITE_CLAY") == "1"
ZOOM = os.environ.get("LIPBITE_ZOOM") == "1"

TEST_FRAMES = [1, 18, 32, 44, 58, 90]
if "--frames" in ARGV:
    TEST_FRAMES = [int(v) for v in ARGV[ARGV.index("--frames") + 1].split(",")]


# =============================================================================
# Вспомогательное
# =============================================================================
def _hash(i, seed=0):
    x = (int(i) * 374761393 + seed * 668265263) & 0xFFFFFFFF
    x = ((x ^ (x >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((x ^ (x >> 16)) & 0xFFFFFFFF) / 4294967295.0


def vnoise(x, seed=0):
    i = math.floor(x)
    f = x - i
    f = f * f * (3 - 2 * f)
    a, b = _hash(i, seed), _hash(i + 1, seed)
    return a + (b - a) * f


def fbm(x, seed=0, octaves=4):
    s, amp, fr = 0.0, 0.5, 1.0
    for o in range(octaves):
        s += amp * vnoise(x * fr, seed + o * 37)
        amp *= 0.5
        fr *= 2.0
    return s


def sstep(a, b, x):
    t = (x - a) / (b - a) if b != a else 0.0
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def lerp(a, b, w):
    return a + (b - a) * w


def catmull(pts, s):
    n = len(pts) - 1
    fs = max(0.0, min(0.999999, s)) * n
    i = int(fs)
    t = fs - i
    p0 = pts[max(i - 1, 0)]
    p1, p2 = pts[i], pts[i + 1]
    p3 = pts[min(i + 2, n)]
    t2, t3 = t * t, t * t * t
    out = []
    for k in range(len(p1)):
        out.append(0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t +
                          (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2 +
                          (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3))
    return out


# =============================================================================
# 1. Контуры рта
# =============================================================================
W = 1.06                       # полуширина рта
CORNER_GAP = 0.010             # щель в спайке: маленькая тёмная точка


def asym(u, amp=0.014, seed=11):
    """Осознанная асимметрия одной плавной составляющей, а не шумом.
    Идеально симметричный рот — верный признак CG, но кривизна должна
    быть задуманной: шум читается браком, наклон читается характером.
    Рот чуть приподнят со стороны, противоположной прикусу."""
    return amp * BITE_SIDE * u * (1.0 - u * u) * 2.6


def corner(u):
    return 0.050 * u * u


def blunt(u, val, toward):
    """Внешний контур у спайки сходится не к оси рта, а к линии щели с
    небольшим запасом: иначе он пересекает её и сечение выворачивается."""
    cw = sstep(0.90, 1.0, abs(u))
    return val * (1.0 - cw) + toward * cw


def y_ap_top(u):
    return (0.058 * (1 - u * u) ** 0.80 + CORNER_GAP * sstep(0.5, 1.0, abs(u))
            + corner(u) + asym(u, 0.007, 5))


def y_ap_bot(u):
    return (-0.090 * (1 - u * u) ** 1.05 - CORNER_GAP * sstep(0.5, 1.0, abs(u))
            + corner(u) + asym(u, 0.007, 9))


def y_upper_outer(u):
    a = abs(u)
    base = 0.49 * (1 - a ** 1.85) ** 0.60
    dip = 0.105 * math.exp(-(u / 0.20) ** 2)
    peak = 0.062 * math.exp(-((a - 0.30) / 0.150) ** 2)
    return blunt(u, base - dip + peak + corner(u) + asym(u, 0.014, 21),
                 corner(u) + 0.070)


def y_lower_outer(u):
    return blunt(u, -0.62 * (1 - u * u) ** 0.60 + corner(u) + asym(u, 0.016, 33),
                 corner(u) - 0.075)


def fullness(u):
    # у спайки объём не гаснет в ноль — иначе получается острый клин
    return max(0.26, (1 - u * u) ** 0.34)


def face_z(x, y):
    """Общая выпуклость объёма, вокруг которого лежат губы."""
    return -0.22 * (x / 1.05) ** 2 - 0.17 * ((y + 0.05) / 1.10) ** 2


# амплитуды выноса сечения вперёд: (вход, полнота, спад, кромка)
AMP_UPPER = (0.170, 0.205, 0.150, 0.070)
AMP_LOWER = (0.190, 0.232, 0.175, 0.078)


W_AP = 0.91                    # щель рта заканчивается раньше силуэта губ


def aperture_xy(u, wup):
    """Кривая щели рта — замкнутая линза внутри силуэта."""
    return W * W_AP * u, lerp(y_ap_bot(u), y_ap_top(u), wup)


def outer_xy(u, wup, sn):
    """Внешний силуэт губ — свой замкнутый овал, шире щели.
    Именно поэтому в уголке остаётся ткань, а не вырожденная точка."""
    xr = 1.0 - 0.030 * math.exp(-(sn / 0.26) ** 2)
    return W * u * xr, lerp(y_lower_outer(u), y_upper_outer(u), wup)


def profile_z(t, wup, f):
    """Глубина сечения: вестибюль -> влажная линия -> вермильон -> кромка."""
    a = [lerp(AMP_LOWER[i], AMP_UPPER[i], wup) for i in range(4)]
    pts = [(-0.36,), (-0.075,), (0.090,),
           (a[0] * f,), (a[1] * f,), (a[2] * f,), (a[3] * f,),
           (-0.065 - 0.020 * f,)]
    n = len(pts) - 1
    sp = (t / 0.20) * (2.0 / n) if t < 0.20 else \
        (2.0 + (t - 0.20) / 0.80 * (n - 2)) / n
    return catmull(pts, sp)[0]


def across(t):
    """Доля пути от щели к силуэту; отрицательная — уход внутрь рта."""
    return (t - 0.20) / 0.80 if t >= 0.20 else (t - 0.20) / 0.20 * 0.38


def ring_detail(u, wup, t):
    """Вторичные формы: бугорок верхней губы, доли нижней, кромка."""
    up = 0.030 * math.exp(-(u / 0.135) ** 2) * math.exp(-((t - 0.55) / 0.28) ** 2)
    lo = -0.010 * math.exp(-(u / 0.11) ** 2) * math.exp(-((t - 0.60) / 0.30) ** 2)
    ridge = lerp(0.010 * math.exp(-((t - 0.90) / 0.065) ** 2),
                 0.014 * math.exp(-((t - 0.88) / 0.060) ** 2), wup)
    return lerp(lo, up, wup) + ridge * fullness(u)


LIP_THICK = 0.038              # толщина губы как объёмного тела


def build_lips(name, nth=300, nt=64, cage=False):
    """ЗАМКНУТЫЙ объём, а не оболочка с модификатором толщины.
    Солверу нужен настоящий объём: только тогда внутреннее давление
    держит форму и сжатие даёт набухание, а не складки материи."""
    if FAST:
        nth, nt = 200, 44
    bm = bmesh.new()
    lay_t = bm.verts.layers.float.new("lip_t")
    lay_u = bm.verts.layers.float.new("lip_u")
    lay_w = bm.verts.layers.float.new("lip_up")
    grid = []
    for i in range(nth):
        th = 2.0 * math.pi * i / nth
        sn = math.sin(th)
        u = max(-1.0, min(1.0, math.cos(th)))
        wup = sstep(-0.34, 0.34, sn)
        ax, ay = aperture_xy(u, wup)
        ox, oy = outer_xy(u, wup, sn)
        width = math.hypot(ox - ax, oy - ay)
        ds = max(0.45, min(1.10, width / 0.44))
        f = fullness(u)
        col = []
        # передняя сторона: от щели к внешнему краю
        for j in range(nt + 1):
            t = j / nt
            w = across(t)
            x = ax + (ox - ax) * w
            y = ay + (oy - ay) * w
            z = face_z(x, y) + profile_z(t, wup, f) * ds + ring_detail(u, wup, t) * ds
            v = bm.verts.new((x, y, z))
            v[lay_t], v[lay_u], v[lay_w] = t, u, wup
            col.append(v)
        # задняя сторона: тот же путь назад, со смещением вглубь.
        # Толщина гаснет на концах, поэтому объём замыкается.
        for j in range(nt - 1, 0, -1):
            t = j / nt
            w = across(t)
            x = ax + (ox - ax) * w
            y = ay + (oy - ay) * w
            z = face_z(x, y) + profile_z(t, wup, f) * ds + ring_detail(u, wup, t) * ds
            z -= LIP_THICK * ds * math.sin(math.pi * t) ** 0.7
            v = bm.verts.new((x, y, z))
            v[lay_t], v[lay_u], v[lay_w] = t, u, wup
            col.append(v)
        grid.append(col)

    ring = len(grid[0])
    for i in range(nth):
        k = (i + 1) % nth
        for j in range(ring):
            j2 = (j + 1) % ring
            bm.faces.new((grid[i][j], grid[k][j], grid[k][j2], grid[i][j2]))

    # Сварка ТОЛЬКО внутри своей половины. Общий remove_doubles сваривал
    # преддверие верхней губы с преддверием нижней: при закрытом рте они
    # совпадают. Сваренная вершина потом не могла уйти ни вверх, ни вниз
    # и тянула за собой плёнку через весь проём рта поверх зубов.
    up_v = [v for v in bm.verts if v[lay_w] > 0.5]
    lo_v = [v for v in bm.verts if v[lay_w] <= 0.5]
    bmesh.ops.remove_doubles(bm, verts=up_v, dist=0.0025)
    bmesh.ops.remove_doubles(bm, verts=lo_v, dist=0.0025)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-5, edges=bm.edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for fc in bm.faces:
        fc.smooth = True

    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    sub = ob.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels, sub.render_levels = 0, 2
    return ob


# =============================================================================
# 2. Деформации
# =============================================================================
BITE_SIDE = -1.0               # прикусывается левая (в кадре) половина

# ЗАВОРОТ = ИЗГИБ СЕЧЕНИЯ, а не поворот его как целого.
# Жёсткий поворот + отдельный подъём кромки + отдельное стягивание — это
# три разных закона по одной и той же поверхности; на их стыке неизбежно
# рождается ребро. Здесь вместо этого к сечению добавляется кривизна:
# угол касательной накапливается вдоль дуги непрерывно. Такая деформация
# изометрична — длина сечения сохраняется, складок и острых углов
# не возникает в принципе.
# Два слагаемых, и оба по построению не дают складок: жёсткий разворот
# всего захвата вокруг основания губы (изометрия) и плавно нарастающая
# вдоль дуги кривизна (тоже изометрия). Ребро в прошлой версии давал
# третий, лишний слой — подъём и стягивание со СВОИМИ профилями по t.
ROT_BASE = 0.62                # разворот захвата у основания, рад
ROLL_C = -1.40                 # добавляемая кривизна: подкручивание кромки
ROLL_ON = (0.10, 0.60)         # доля дуги, на которой кривизна набирается
LIFT = 0.020                   # добор высоты; основную даёт челюсть
# Заворот сам по себе не убавляет губу: низ силуэта остаётся на месте,
# и снизу её видно ровно столько же. Поэтому захваченный участок ещё и
# СЖИМАЕТСЯ по вертикали к линии рта. Коэффициент постоянен вдоль всего
# сечения — сечение остаётся подобным себе, новых рёбер это не даёт.
SHRINK = 0.34
SHRINK_Y = -0.05               # центр сжатия — линия рта
_SEC_N = 128


def side_mask(x):
    """Захват — локальное пятно, а не половина губы: в реальном прикусе
    под зубы уходит небольшой участок."""
    c = BITE_SIDE * 0.22 * W
    # Пятно шире, чем было: узкое давало ступеньку на нижнем контуре
    # там, где захват обрывался.
    return math.exp(-((x - c) / (0.98 * W)) ** 2)


def corner_damp(u):
    """Узел спайки остаётся жёстким целиком. Если гасить деформацию только
    в самой точке уголка, верх и низ у кончика щели сдвигаются друг
    относительно друга и протыкают сами себя."""
    return 1.0 - sstep(0.46, 0.95, abs(u))


def grip(u, wup):
    """Сила захвата ПОСТОЯННА вдоль сечения и меняется только по обходу
    рта. Если она меняется ещё и поперёк, сечение внутри себя срезается —
    это второй источник рёбер."""
    return side_mask(W * W_AP * u) * (1.0 - wup) * corner_damp(u)


def rot_x(y, z, ang, py, pz):
    dy, dz = y - py, z - pz
    return (py + dy * math.cos(ang) - dz * math.sin(ang),
            pz + dy * math.sin(ang) + dz * math.cos(ang))


_SEC_CACHE = {}


def _section(u, wup):
    """Сечение губы в плоскости (y,z) от основания (t=1) к кончику (t=0):
    точки, длина дуги, угол касательной. Считается раз на столбец."""
    key = (round(u, 5), round(wup, 5))
    hit = _SEC_CACHE.get(key)
    if hit is not None:
        return hit
    sn = math.sqrt(max(0.0, 1.0 - u * u))
    ax, ay = aperture_xy(u, wup)
    ox, oy = outer_xy(u, wup, sn)
    ds = max(0.45, min(1.10, math.hypot(ox - ax, oy - ay) / 0.44))
    fl = fullness(u)
    pts = []
    for i in range(_SEC_N + 1):
        t = 1.0 - i / _SEC_N
        w = across(t)
        x = ax + (ox - ax) * w
        y = ay + (oy - ay) * w
        z = face_z(x, y) + profile_z(t, wup, fl) * ds + ring_detail(u, wup, t) * ds
        pts.append((y, z))
    arc = [0.0]
    for i in range(1, len(pts)):
        arc.append(arc[-1] + math.hypot(pts[i][0] - pts[i - 1][0],
                                        pts[i][1] - pts[i - 1][1]))
    phi = []
    for i in range(len(pts)):
        p0 = pts[max(i - 1, 0)]
        p1 = pts[min(i + 1, len(pts) - 1)]
        phi.append(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
    res = (pts, arc, phi)
    _SEC_CACHE[key] = res
    return res


_BENT_CACHE = {}


def _bent(u, wup, k):
    """Сечение с добавленной кривизной. Возвращает новые точки и поворот
    касательной в каждой точке — вокруг него доворачивается толщина."""
    key = (round(u, 5), round(wup, 5))
    hit = _BENT_CACHE.get(key)
    if hit is not None:
        return hit
    pts, arc, phi = _section(u, wup)
    total = max(arc[-1], 1e-6)
    # кривизна набирается плавно: у основания губа не гнётся,
    # к кромке заворот максимальный
    acc = -ROT_BASE * k            # разворот тела губы
    dphi = [acc]
    for i in range(1, len(pts)):
        sm = 0.5 * (arc[i] + arc[i - 1]) / total
        acc += ROLL_C * k * sstep(ROLL_ON[0], ROLL_ON[1], sm) * (arc[i] - arc[i - 1])
        dphi.append(acc)
    out = [pts[0]]
    y, z = pts[0]
    for i in range(1, len(pts)):
        step = arc[i] - arc[i - 1]
        ang = 0.5 * (phi[i] + phi[i - 1]) + 0.5 * (dphi[i] + dphi[i - 1])
        y += math.cos(ang) * step
        z += math.sin(ang) * step
        out.append((y, z))
    res = (out, dphi)
    _BENT_CACHE[key] = res
    return res


def f_roll(co, t, u, wup):
    """Губа заходит под зубы: сечение доворачивается по дуге непрерывно."""
    x, y, z = co
    k = grip(u, wup)
    if k <= 1e-4:
        return Vector(co)
    pts, arc, phi = _section(u, wup)
    out, dphi = _bent(u, wup, k)
    g = max(0.0, min(1.0, 1.0 - t)) * _SEC_N
    i = min(int(g), _SEC_N - 1)
    w = g - i
    py = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * w
    pz = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * w
    qy = out[i][0] + (out[i + 1][0] - out[i][0]) * w
    qz = out[i][1] + (out[i + 1][1] - out[i][1]) * w
    rot = dphi[i] + (dphi[i + 1] - dphi[i]) * w
    # толщина губы едет за сечением как жёсткая: объём не сплющивается
    ey, ez = y - py, z - pz
    ca, sa = math.cos(rot), math.sin(rot)
    ny = qy + ey * ca - ez * sa
    nz = qz + ey * sa + ez * ca
    # захваченный участок целиком поднимается в зубы: сдвиг одинаков
    # вдоль всего сечения, поэтому нового ребра не создаёт
    ny += LIFT * k
    # и стягивается к линии рта — губы снизу становится меньше
    ny = SHRINK_Y + (ny - SHRINK_Y) * (1.0 - SHRINK * k)
    return Vector((x * (1.0 - 0.055 * k), ny, nz))


def f_mass_shift(co, t, u, wup):
    """Вытеснение объёма: то, что ушло под зубы слева, выдавливается
    в свободную сторону — губа смещается и набухает справа.
    В референсе это хорошо видно на кадрах f02-f07."""
    x, y, z = co
    c = -BITE_SIDE * 0.26 * W
    band = math.exp(-((x - c) / (0.55 * W)) ** 2)
    k = band * (1.0 - wup) * corner_damp(u) * sstep(0.22, 0.45, t)
    if k <= 0.0:
        return Vector(co)
    return Vector((x - BITE_SIDE * 0.055 * k,
                   y + 0.022 * k,
                   z + 0.040 * k))


def f_press_at(co, t, u, wup, band):
    """Зубы стоят на губе: мягкая широкая вмятина по линии контакта.
    Полоса контакта задаётся band — сдвигая её, получаем ощущение,
    что губа скользит под зубами."""
    x, y, z = co
    k = side_mask(x) * (1.0 - wup) * corner_damp(u)
    if k <= 0.0:
        return Vector(co)
    g = math.exp(-((t - band) / 0.070) ** 2)
    imp = 0.80 + 0.20 * math.cos(2 * math.pi * x / (0.235 * W))
    z -= 0.072 * g * imp * k                 # борозда от режущей кромки
    y -= 0.014 * g * k
    # мякоть, выдавленная ниже кромки: без неё борозда читается вмятиной
    # на резине, а не сжатой плотью
    z += 0.058 * math.exp(-((t - band - 0.17) / 0.095) ** 2) * k
    return Vector((x, y, z))


# Полоса контакта отсчитывается по УЖЕ ЗАВЁРНУТОЙ губе: после поворота
# на 46° кромка зубов приходится примерно на t=0.42, а не на 0.72.
def f_press_hi(co, t, u, wup):
    return f_press_at(f_roll(co, t, u, wup), t, u, wup, 0.42)


def f_press_lo(co, t, u, wup):
    """Выход: пятно контакта уползает к внутреннему краю — губа
    выскальзывает из-под зубов."""
    return f_press_at(f_roll(co, t, u, wup), t, u, wup, 0.26)


def _open_e(u):
    return (1.0 - u * u) ** 1.10 * (1.0 + 0.32 * BITE_SIDE * u) * corner_damp(u)


def f_open_up(co, t, u, wup):
    """Верхняя губа поднимается. Отделена от нижней: рот открывается
    подъёмом верха, а закрывается ПОДЪЁМОМ НИЗА — челюстью. Одним ключом
    это не выразить, а без челюсти прикуса не бывает."""
    e = _open_e(u) * wup
    return Vector((co[0], co[1] + 0.165 * e, co[2] + 0.012 * e))


def f_open_lo(co, t, u, wup):
    """Опускание нижней губы: 1 — челюсть опущена, 0 — сомкнута.
    В прикусе значение падает, губа идёт вверх в зубы."""
    e = _open_e(u) * (1.0 - wup)
    return Vector((co[0], co[1] - 0.150 * e, co[2] + 0.008 * e))


def f_pout(co, t, u, wup):
    m = sstep(0.30, 0.75, t) * (1.0 - sstep(0.88, 1.0, t)) * corner_damp(u)
    return Vector((co[0], co[1] - 0.008 * m, co[2] + 0.055 * m))


def f_wobble_a(co, t, u, wup):
    m = (1.0 - wup) * corner_damp(u) * math.exp(-((t - 0.60) / 0.24) ** 2)
    return Vector((co[0], co[1] - 0.006 * m, co[2] + 0.030 * m))


def f_wobble_b(co, t, u, wup):
    m = (1.0 - wup) * corner_damp(u) * math.exp(-((t - 0.66) / 0.28) ** 2) * (co[0] / W)
    return Vector((co[0] + 0.012 * m, co[1] + 0.010 * m, co[2] + 0.014 * m))


def f_smirk(co, t, u, wup):
    x, y, z = co
    w = sstep(0.10, 0.98, BITE_SIDE * x / W)
    return Vector((x + 0.022 * w * BITE_SIDE, y + 0.070 * w, z - 0.018 * w))


def lip_attrs(ob):
    n = len(ob.data.vertices)
    ts, us, ws = [0.0] * n, [0.0] * n, [0.0] * n
    ob.data.attributes["lip_t"].data.foreach_get("value", ts)
    ob.data.attributes["lip_u"].data.foreach_get("value", us)
    ob.data.attributes["lip_up"].data.foreach_get("value", ws)
    return ts, us, ws


def add_key(ob, name, fn, base_fn=None, lo=-0.6, hi=1.5):
    if ob.data.shape_keys is None:
        ob.shape_key_add(name="Basis", from_mix=False)
    k = ob.shape_key_add(name=name, from_mix=False)
    k.slider_min, k.slider_max = lo, hi
    ts, us, ws = lip_attrs(ob)
    for i, v in enumerate(ob.data.vertices):
        co = v.co
        tgt = fn(co, ts[i], us[i], ws[i])
        org = base_fn(co, ts[i], us[i], ws[i]) if base_fn else Vector(co)
        k.data[i].co = co + (tgt - org)
    return k


def bake_press_attribute(ob):
    """Полосы контакта совпадают с полосами ключей нажима: PressHigh
    работает по t=0.42, PressLow по t=0.26. Раньше тень рисовалась по
    своей полосе и за проскальзыванием не следовала — то есть врала."""
    at_hi = ob.data.attributes.new("ct_hi", 'FLOAT', 'POINT')
    at_lo = ob.data.attributes.new("ct_lo", 'FLOAT', 'POINT')
    ts, us, ws = lip_attrs(ob)
    hi, lo = [], []
    for i, v in enumerate(ob.data.vertices):
        k = side_mask(v.co.x) * (1.0 - ws[i]) * corner_damp(us[i])
        hi.append(k * math.exp(-((ts[i] - 0.42) / 0.085) ** 2))
        lo.append(k * math.exp(-((ts[i] - 0.26) / 0.075) ** 2))
    at_hi.data.foreach_set("value", hi)
    at_lo.data.foreach_set("value", lo)


# =============================================================================
# 3. Зубы, дёсны, задник
# =============================================================================


def build_tooth(name, width, height, depth, loc):
    nx, ny = 20, 24
    bm = bmesh.new()
    verts = {}
    for a in range(nx + 1):
        su = -1.0 + 2.0 * a / nx
        for b in range(ny + 1):
            sv = -1.0 + 2.0 * b / ny
            taper = 1.0 - 0.16 * sstep(-0.2, 1.0, sv)
            cr = max(0.0, (abs(su) - 0.82) / 0.18) * max(0.0, (-sv - 0.76) / 0.24)
            edge = 1.0 - 0.40 * min(1.0, cr) ** 1.4
            x = 0.5 * width * su * taper * edge
            y = 0.5 * height * sv
            bulge = math.cos(su * 1.2) * math.cos(sv * 0.85)
            zf = 0.5 * depth * (0.40 + 0.60 * max(0.0, bulge))
            zb = -0.5 * depth * (0.5 + 0.5 * max(0.0, bulge))
            verts[(a, b, 0)] = bm.verts.new((x, y, zf))
            verts[(a, b, 1)] = bm.verts.new((x, y, zb))
    for a in range(nx):
        for b in range(ny):
            bm.faces.new((verts[(a, b, 0)], verts[(a + 1, b, 0)],
                          verts[(a + 1, b + 1, 0)], verts[(a, b + 1, 0)]))
            bm.faces.new((verts[(a, b, 1)], verts[(a, b + 1, 1)],
                          verts[(a + 1, b + 1, 1)], verts[(a + 1, b, 1)]))
    for a in range(nx):
        for side, b in ((0, 0), (1, ny)):
            q = (verts[(a, b, 0)], verts[(a + 1, b, 0)],
                 verts[(a + 1, b, 1)], verts[(a, b, 1)])
            bm.faces.new(q if side == 0 else tuple(reversed(q)))
    for b in range(ny):
        for side, a in ((0, 0), (1, nx)):
            q = (verts[(a, b, 0)], verts[(a, b, 1)],
                 verts[(a, b + 1, 1)], verts[(a, b + 1, 0)])
            bm.faces.new(q if side == 0 else tuple(reversed(q)))
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for f in bm.faces:
        f.smooth = True
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.location = loc
    sub = ob.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels, sub.render_levels = 1, 2
    return ob


def build_row(prefix, y_edge, height, mat, parent, arc):
    """Ровный ряд: никакого случайного разброса высот и поворотов —
    он читался как плохие зубы."""
    for idx, u in enumerate(TOOTH_U):
        left = TOOTH_U[idx - 1] if idx > 0 else u - 0.223
        right = TOOTH_U[idx + 1] if idx < len(TOOTH_U) - 1 else u + 0.223
        wide = 1.06 if abs(u) < 0.20 else 0.94
        wt = 0.60 * (right - left) * 1.10 * wide * W
        h = abs(height) * (1.0 - 0.14 * abs(u))
        sign = 1.0 if height > 0 else -1.0
        ob = build_tooth(f"{prefix}_{idx}", wt, h, 0.115,
                         (W * u, y_edge + arc * u * u + sign * h * 0.5,
                          -0.135 - 0.30 * u * u))
        ob.data.materials.append(mat)
        ob.parent = parent
        ob.matrix_parent_inverse = parent.matrix_world.inverted()



# Зубной ряд — одна дуга с бороздками между резцами. Отдельные блоки
# читались как приклеенные кубики, и было видно, к чему они крепятся.
TOOTH_EDGES = (-0.95, -0.80, -0.62, -0.42, -0.22, 0.0,
               0.22, 0.42, 0.62, 0.80, 0.95)


def tooth_grooves(u):
    """Насколько глубоко в этой точке проходит межзубная бороздка.
    Ближе к уголкам бороздки чаще и глубже — зубы там мельче."""
    g = 0.0
    for b in TOOTH_EDGES:
        w = 0.020 + 0.008 * (1.0 - abs(b))
        g = max(g, math.exp(-((u - b) / w) ** 2) * (0.75 + 0.25 * abs(b)))
    return g


def build_arch(name, y_edge, height, depth, mat, parent, arc, sign):
    """sign=+1 — верхний ряд (растёт вверх), -1 — нижний."""
    bm = bmesh.new()
    nu, nv = 300, 26
    grid_f, grid_b = [], []
    for i in range(nu + 1):
        u = -0.90 + 1.80 * i / nu
        g = tooth_grooves(u)
        # режущая кромка слегка фестонит по бороздкам
        y0 = y_edge + arc * u * u + sign * 0.004 * g
        zc = -0.135 - 0.62 * u * u
        col_f, col_b = [], []
        for j in range(nv + 1):
            v = j / nv
            hs = 1.0 - 0.70 * sstep(0.40, 0.90, abs(u))
            y = y0 + sign * height * hs * v
            # выпуклость зуба: гаснет к бороздке и к десне
            bulge = (1.0 - 0.75 * g) * math.sin(math.pi * min(1.0, v * 1.15)) ** 0.6
            round_edge = sstep(0.0, 0.10, v)          # скруглённая кромка
            col_f.append(bm.verts.new((W * u, y, zc + depth * 0.5 * bulge * round_edge)))
            col_b.append(bm.verts.new((W * u, y, zc - depth * 0.5 * (0.4 + 0.6 * v))))
        grid_f.append(col_f)
        grid_b.append(col_b)
    for i in range(nu):
        for j in range(nv):
            bm.faces.new((grid_f[i][j], grid_f[i + 1][j], grid_f[i + 1][j + 1], grid_f[i][j + 1]))
            bm.faces.new((grid_b[i][j], grid_b[i][j + 1], grid_b[i + 1][j + 1], grid_b[i + 1][j]))
    for i in range(nu):                     # замыкание по режущей кромке
        bm.faces.new((grid_f[i][0], grid_b[i][0], grid_b[i + 1][0], grid_f[i + 1][0]))
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for f in bm.faces:
        f.smooth = True
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.data.materials.append(mat)
    ob.parent = parent
    sub = ob.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels, sub.render_levels = 0, 1
    return ob


def build_plate(name, mat, nu=140, nv=26):
    """Тёмный задник рта строго внутри силуэта губ."""
    bm = bmesh.new()
    grid = []
    for i in range(nu + 1):
        u = max(-1.0, min(1.0, -1.0 + 2.0 * i / nu))
        top = y_ap_top(u) + 0.50 * (y_upper_outer(u) - y_ap_top(u))
        # низ задника выше: при втягивании губа поднимается,
        # и прежний край вылезал из-под неё тёмным пятном
        bot = y_ap_bot(u) + 0.08 * (y_lower_outer(u) - y_ap_bot(u))
        col = []
        for j in range(nv + 1):
            v = j / nv
            col.append(bm.verts.new((W * 0.93 * u, top + (bot - top) * v,
                                     -0.74 - 0.24 * u * u)))
        grid.append(col)
    for i in range(nu):
        for j in range(nv):
            bm.faces.new((grid[i][j], grid[i + 1][j], grid[i + 1][j + 1], grid[i][j + 1]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.data.materials.append(mat)
    return ob


def base_material(name, color, rough, sss=0.0, sss_rad=(1, .2, .1), sss_scale=0.05,
                  coat=0.0, coat_rough=0.1):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]

    def setv(k, v):
        if k in b.inputs:
            b.inputs[k].default_value = v

    setv("Base Color", (*color, 1.0))
    setv("Roughness", rough)
    setv("Subsurface Weight", sss)
    setv("Subsurface Radius", sss_rad)
    setv("Subsurface Scale", sss_scale)
    setv("Coat Weight", coat)
    setv("Coat Roughness", coat_rough)
    return mat


def _toon_shade(nt, stops):
    """Ступенчатая светотень: свет считается диффузно, переводится в
    число и рубится рампой на плоские зоны. Это и отличает мультяшную
    подачу от реалистичной — градиента на поверхности нет вообще."""
    dif = nt.nodes.new("ShaderNodeBsdfDiffuse")
    dif.inputs["Color"].default_value = (1, 1, 1, 1)
    dif.inputs["Roughness"].default_value = 0.0
    s2r = nt.nodes.new("ShaderNodeShaderToRGB")
    nt.links.new(dif.outputs[0], s2r.inputs[0])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    cr = ramp.color_ramp
    cr.interpolation = 'CONSTANT'
    cr.elements[0].position = 0.0
    cr.elements[0].color = stops[0]
    cr.elements[1].position = stops[1][0]
    cr.elements[1].color = stops[1][1]
    for pos, col in stops[2:]:
        cr.elements.new(pos).color = col
    nt.links.new(s2r.outputs["Color"], ramp.inputs["Fac"])
    return ramp


def _flat_out(nt, color_socket):
    """Итог выводим свечением: заливка не должна ещё раз затеняться."""
    em = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(color_socket, em.inputs[0])
    nt.links.new(em.outputs[0], nt.nodes["Material Output"].inputs["Surface"])
    return em


def _blank_mat(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    for n in list(mat.node_tree.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            mat.node_tree.nodes.remove(n)
    return mat


def lip_material():
    """Мультяшная губа. Три вещи, которых не было в прошлой версии:
    тени уходят по ТОНУ, а не только по яркости; снизу формы идёт
    отражённый свет; уголок рта — заданная тёмная форма, а не то,
    что получилось на схождении кривых."""
    mat = _blank_mat("Lips")
    nt = mat.node_tree
    at_t = nt.nodes.new("ShaderNodeAttribute"); at_t.attribute_name = "lip_t"
    at_u = nt.nodes.new("ShaderNodeAttribute"); at_u.attribute_name = "lip_u"

    base = nt.nodes.new("ShaderNodeValToRGB")
    cr = base.color_ramp
    cr.interpolation = 'CONSTANT'
    cr.elements[0].position = 0.0
    cr.elements[0].color = (0.085, 0.006, 0.014, 1.0)     # слизистая
    cr.elements[1].position = 0.20
    cr.elements[1].color = (0.360, 0.030, 0.060, 1.0)     # влажная линия
    cr.elements.new(0.34).color = (0.660, 0.062, 0.105, 1.0)   # вермильон
    cr.elements.new(0.90).color = (0.430, 0.034, 0.066, 1.0)   # кромка
    nt.links.new(at_t.outputs["Fac"], base.inputs["Fac"])

    # --- УГОЛОК РТА заданной формой -----------------------------------
    # Тёмная запятая в спайке рисуется, а не получается сама. Ширина
    # берётся по обходу рта, длина — по сечению: пятно ложится вдоль
    # щели и не расползается на объём губы.
    uabs = nt.nodes.new("ShaderNodeMath"); uabs.operation = 'ABSOLUTE'
    nt.links.new(at_u.outputs["Fac"], uabs.inputs[0])
    cw = nt.nodes.new("ShaderNodeValToRGB")
    cw.color_ramp.interpolation = 'CONSTANT'
    cw.color_ramp.elements[0].position = 0.0
    cw.color_ramp.elements[0].color = (0, 0, 0, 1)
    cw.color_ramp.elements[1].position = 0.962
    cw.color_ramp.elements[1].color = (1, 1, 1, 1)
    nt.links.new(uabs.outputs[0], cw.inputs["Fac"])
    ct = nt.nodes.new("ShaderNodeValToRGB")           # только у щели
    ct.color_ramp.interpolation = 'CONSTANT'
    ct.color_ramp.elements[0].position = 0.0
    ct.color_ramp.elements[0].color = (1, 1, 1, 1)
    ct.color_ramp.elements[1].position = 0.30
    ct.color_ramp.elements[1].color = (0, 0, 0, 1)
    nt.links.new(at_t.outputs["Fac"], ct.inputs["Fac"])
    cmul = nt.nodes.new("ShaderNodeMath"); cmul.operation = 'MULTIPLY'
    nt.links.new(cw.outputs["Color"], cmul.inputs[0])
    nt.links.new(ct.outputs["Color"], cmul.inputs[1])
    corner_mix = nt.nodes.new("ShaderNodeMix")
    corner_mix.data_type = 'RGBA'
    corner_mix.inputs[7].default_value = (0.075, 0.006, 0.020, 1.0)
    nt.links.new(base.outputs["Color"], corner_mix.inputs[6])
    nt.links.new(cmul.outputs[0], corner_mix.inputs[0])

    # --- ТЕНЬ ОТ ЗУБОВ на губе ----------------------------------------
    # Вывод идёт свечением, теней в сцене нет физически ни одной.
    # Поэтому контакт рисуется отдельно — и это читается как прикус
    # сильнее, чем любая правка геометрии.
    at_hi = nt.nodes.new("ShaderNodeAttribute"); at_hi.attribute_name = "ct_hi"
    at_lo = nt.nodes.new("ShaderNodeAttribute"); at_lo.attribute_name = "ct_lo"
    v_hi = nt.nodes.new("ShaderNodeValue"); v_hi.label = "PressHigh"
    v_lo = nt.nodes.new("ShaderNodeValue"); v_lo.label = "PressLow"
    m_hi = nt.nodes.new("ShaderNodeMath"); m_hi.operation = 'MULTIPLY'
    nt.links.new(at_hi.outputs["Fac"], m_hi.inputs[0])
    nt.links.new(v_hi.outputs[0], m_hi.inputs[1])
    m_lo = nt.nodes.new("ShaderNodeMath"); m_lo.operation = 'MULTIPLY'
    nt.links.new(at_lo.outputs["Fac"], m_lo.inputs[0])
    nt.links.new(v_lo.outputs[0], m_lo.inputs[1])
    add = nt.nodes.new("ShaderNodeMath"); add.operation = 'ADD'; add.use_clamp = True
    nt.links.new(m_hi.outputs[0], add.inputs[0])
    nt.links.new(m_lo.outputs[0], add.inputs[1])
    sh_ramp = nt.nodes.new("ShaderNodeValToRGB")      # край жёсткий, набор плавный
    sh_ramp.color_ramp.elements[0].position = 0.14
    sh_ramp.color_ramp.elements[1].position = 0.30
    nt.links.new(add.outputs[0], sh_ramp.inputs["Fac"])
    contact = nt.nodes.new("ShaderNodeMix")
    contact.data_type = 'RGBA'
    contact.inputs[7].default_value = (0.150, 0.014, 0.052, 1.0)
    nt.links.new(corner_mix.outputs[2], contact.inputs[6])
    nt.links.new(sh_ramp.outputs["Color"], contact.inputs[0])

    # --- СВЕТОТЕНЬ ступенями, тень уходит в холод ----------------------
    # Множители не серые: тень, домноженная на синеватый, уводит красный
    # в фиолет. Тень тем же красным, только темнее, и есть тот самый
    # признак дешёвой картинки.
    lit = _toon_shade(nt, [(0.300, 0.235, 0.400, 1.0),
                           (0.34, (0.700, 0.650, 0.720, 1.0)),
                           (0.68, (1.0, 1.0, 1.0, 1.0))])
    mul = nt.nodes.new("ShaderNodeMix")
    mul.data_type = 'RGBA'
    mul.blend_type = 'MULTIPLY'
    mul.inputs[0].default_value = 1.0
    nt.links.new(contact.outputs[2], mul.inputs[6])
    nt.links.new(lit.outputs["Color"], mul.inputs[7])

    # --- ОТРАЖЁННЫЙ СВЕТ снизу формы ----------------------------------
    # Полоса теплее самой тени по нижнему краю: без неё нижняя губа
    # выглядит приклеенной к фону.
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Normal"], sep.inputs["Vector"])
    bnc = nt.nodes.new("ShaderNodeValToRGB")
    bnc.color_ramp.interpolation = 'CONSTANT'
    bnc.color_ramp.elements[0].position = 0.0
    bnc.color_ramp.elements[0].color = (1, 1, 1, 1)
    bnc.color_ramp.elements[1].position = 0.30
    bnc.color_ramp.elements[1].color = (0, 0, 0, 1)
    mr = nt.nodes.new("ShaderNodeMapRange")          # -1..0 нормали вниз
    mr.inputs["From Min"].default_value = -0.99
    mr.inputs["From Max"].default_value = -0.66
    nt.links.new(sep.outputs["Z"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], bnc.inputs["Fac"])
    bounce = nt.nodes.new("ShaderNodeMix")
    bounce.data_type = 'RGBA'
    bounce.inputs[7].default_value = (0.520, 0.120, 0.135, 1.0)
    nt.links.new(mul.outputs[2], bounce.inputs[6])
    bf = nt.nodes.new("ShaderNodeMath"); bf.operation = 'MULTIPLY'
    bf.inputs[1].default_value = 0.32
    nt.links.new(bnc.outputs["Color"], bf.inputs[0])
    nt.links.new(bf.outputs[0], bounce.inputs[0])

    _flat_out(nt, bounce.outputs[2])
    mat["v_hi"], mat["v_lo"] = v_hi.name, v_lo.name
    return mat


def teeth_material():
    """Зубы плоские, с одной ступенью тени: в мультяшной подаче эмаль
    не бликует и не просвечивает."""
    mat = _blank_mat("Enamel")
    nt = mat.node_tree
    col = nt.nodes.new("ShaderNodeRGB")
    col.outputs[0].default_value = (0.930, 0.925, 0.900, 1.0)
    lit = _toon_shade(nt, [(0.62, 0.62, 0.64, 1.0),
                           (0.52, (1.0, 1.0, 1.0, 1.0))])
    mul = nt.nodes.new("ShaderNodeMix")
    mul.data_type = 'RGBA'
    mul.blend_type = 'MULTIPLY'
    mul.inputs[0].default_value = 1.0
    nt.links.new(col.outputs[0], mul.inputs[6])
    nt.links.new(lit.outputs["Color"], mul.inputs[7])
    _flat_out(nt, mul.outputs[2])
    return mat


def outline_material(name, color=(0.02, 0.004, 0.008, 1.0)):
    mat = _blank_mat(name)
    nt = mat.node_tree
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs[0].default_value = color
    nt.links.new(em.outputs[0], nt.nodes["Material Output"].inputs["Surface"])
    mat.use_backface_culling = True
    return mat


def bake_outline_group(ob):
    """Вес обводки. Постоянная толщина в 3D на экране гуляет и комкуется
    в спайке. Живая линия имеет вес: толще снизу и в тени, тоньше
    сверху, и почти сходит на нет в уголке рта."""
    vg = ob.vertex_groups.new(name="outline")
    ts, us, ws = lip_attrs(ob)
    for i in range(len(ob.data.vertices)):
        w = (0.34 + 0.66 * (1.0 - ws[i])) * (1.0 - 0.82 * sstep(0.74, 0.99, abs(us[i])))
        vg.add([i], max(0.0, min(1.0, w)), 'REPLACE')
    return vg


def add_outline(ob, thickness, mat, depth=0.06, group=None):
    """Контур вывернутой оболочкой: копия объекта на тех же данных
    (значит, деформируется теми же ключами), раздутая по нормалям и
    вывернутая наизнанку. Видно только там, где она вылезает за силуэт."""
    o = ob.copy()
    o.data = ob.data                      # общие данные — общая анимация формы
    bpy.context.collection.objects.link(o)
    o.name = ob.name + "_Outline"
    o.parent = ob.parent
    o.matrix_parent_inverse = ob.matrix_parent_inverse.copy()
    for m in list(o.modifiers):
        o.modifiers.remove(m)
    for m in ob.modifiers:                # повторяем сглаживание оригинала
        if m.type == 'SUBSURF':
            n = o.modifiers.new("Subsurf", 'SUBSURF')
            n.levels, n.render_levels = m.levels, m.render_levels
    sol = o.modifiers.new("Outline", 'SOLIDIFY')
    sol.thickness = thickness
    sol.offset = 1.0
    sol.use_flip_normals = True
    sol.use_rim = False
    if group:
        sol.vertex_group = group
        sol.thickness_vertex_group = 0.18
    if len(o.material_slots) == 0:
        o.data.materials.append(mat)
    for sl in o.material_slots:
        sl.link = 'OBJECT'
        sl.material = mat
    # Оболочку отодвигаем от камеры. Толщина губы как объёма гаснет
    # к краям сечения в ноль, поэтому раздутая задняя стенка вылезает
    # сквозь переднюю чёрной крапиной — сдвиг по глубине это снимает,
    # а на силуэт не влияет.
    o.location = (0.0, 0.0, -depth)
    o.visible_shadow = False
    return o


# =============================================================================
# 5. Ключи и стиль затухания
# =============================================================================
KEY_STYLE = {}


def key_obj(ob, path, frame, value, index=-1, style=None):
    if index >= 0:
        getattr(ob, path)[index] = value
    else:
        setattr(ob, path, value)
    ob.keyframe_insert(data_path=path, frame=frame, index=index)
    if style:
        KEY_STYLE[(path, max(index, 0), round(frame))] = style


def key_shape(kb, frame, value, style=None):
    kb.value = value
    kb.keyframe_insert("value", frame=frame)
    if style:
        KEY_STYLE[(f'key_blocks["{kb.name}"].value', 0, round(frame))] = style


def bake_wobble(kb, start, end, amp, freq, decay, phase=0.0):
    key_shape(kb, start - 1, 0.0, ('LINEAR', 'AUTO'))
    for f in range(start, end + 1):
        tt = (f - start) / FPS
        key_shape(kb, f, amp * math.exp(-tt * decay)
                  * math.sin(2 * math.pi * freq * tt + phase), ('LINEAR', 'AUTO'))
    key_shape(kb, end + 1, 0.0, ('LINEAR', 'AUTO'))


def all_fcurves(act):
    if hasattr(act, "fcurves"):
        yield from act.fcurves
        return
    for layer in act.layers:
        for strip in layer.strips:
            for slot in act.slots:
                cb = strip.channelbag(slot)
                if cb:
                    yield from cb.fcurves


# =============================================================================
# 6. Сцена
# =============================================================================
def wipe_scene():
    """В живой сессии нельзя вызывать read_factory_settings: он снимает
    регистрацию аддонов, в том числе MCP-сервера. Чистим сцену вручную."""
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights,
                 bpy.data.cameras, bpy.data.node_groups, bpy.data.actions,
                 bpy.data.worlds):
        for item in list(coll):
            if item.users == 0:
                coll.remove(item)


def main():
    if BUILD_ONLY:
        wipe_scene()
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, F_END
    scene.render.fps = FPS

    world_root = bpy.data.objects.new("World_Root", None)
    bpy.context.collection.objects.link(world_root)
    world_root.rotation_euler = (math.radians(90), 0, 0)
    micro = bpy.data.objects.new("Micro_Motion", None)
    bpy.context.collection.objects.link(micro)
    micro.parent = world_root
    root = bpy.data.objects.new("Mouth_Root", None)
    bpy.context.collection.objects.link(root)
    root.parent = micro
    upper_jaw = bpy.data.objects.new("Upper_Jaw", None)
    bpy.context.collection.objects.link(upper_jaw)
    upper_jaw.parent = root

    mat_teeth = teeth_material()
    mat_cav = base_material("Cavity", (0.010, 0.001, 0.002), 0.85)

    plate = build_plate("Cavity", mat_cav)
    plate.parent = root
    # язык не строим: при втягивании губы он вылезал сквозь неё,
    # а в этой стилизации его всё равно не видно

    # нижний ряд не строим: в прикрытом рте он не виден, а в анимации
    # только мешает — лезет из-под губы при заворотe
    teeth = build_arch("Teeth_U", -0.030, 0.44, 0.115, mat_teeth,
                       upper_jaw, 0.095, 1.0)

    lips = build_lips("Lips")
    lips.data.materials.append(lip_material())
    lips.parent = root
    bake_press_attribute(lips)

    k_open = add_key(lips, "OpenUp", f_open_up)
    k_jaw = add_key(lips, "JawDrop", f_open_lo)
    k_roll = add_key(lips, "RollUnder", f_roll)
    k_mass = add_key(lips, "MassShift", f_mass_shift)
    k_press_hi = add_key(lips, "PressHigh", f_press_hi, base_fn=f_roll)
    k_press_lo = add_key(lips, "PressLow", f_press_lo, base_fn=f_roll)
    k_pout = add_key(lips, "Pout", f_pout)
    k_smirk = add_key(lips, "Smirk", f_smirk)
    k_wob_a = add_key(lips, "WobbleA", f_wobble_a, lo=-1.6, hi=1.6)
    k_wob_b = add_key(lips, "WobbleB", f_wobble_b, lo=-1.6, hi=1.6)

    if not CLAY:
        mat_ol = outline_material("Outline")
        # толщина заведомо меньше половины толщины губы (LIP_THICK),
        # иначе задняя стенка объёма протыкает переднюю крапинами
        bake_outline_group(lips)
        add_outline(lips, 0.030, mat_ol, depth=0.075, group="outline")
        add_outline(teeth, 0.009, mat_ol, depth=0.030)

    mat_lips = lips.data.materials[0]
    for prop, key in (("v_hi", "PressHigh"), ("v_lo", "PressLow")):
        node = mat_lips.node_tree.nodes[mat_lips[prop]]
        drv = node.outputs[0].driver_add("default_value").driver
        drv.type = 'AVERAGE'
        var = drv.variables.new()
        var.name = "v"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'KEY'
        var.targets[0].id = lips.data.shape_keys
        var.targets[0].data_path = f'key_blocks["{key}"].value'

    # ------------------------------------------------------------------
    # Механика: A (40-72) губа заходит под зубы; B (72-92) зубы ставятся
    # на губу; C (120-176) губа постепенно выскальзывает из-под зубов.
    # ------------------------------------------------------------------
    EO = ('QUART', 'EASE_OUT')
    EI = ('QUART', 'EASE_IN')
    SIO = ('SINE', 'EASE_IN_OUT')
    EXO = ('EXPO', 'EASE_OUT')
    BACK = ('BACK', 'EASE_OUT')

    # ------------------------------------------------------------------
    # Всё движение прикуса выводится из ОДНОЙ ведущей величины master(f)
    # и запекается покадрово. Шесть независимых кривых с разными типами
    # затухания давали скачки скорости на стыках — отсюда дёрганость.
    # ------------------------------------------------------------------
    def smoother(t):
        t = max(0.0, min(1.0, t))
        return t * t * t * (t * (t * 6 - 15) + 10)

    # ------------------------------------------------------------------
    # ТАЙМИНГ. Прежняя версия занимала 6 секунд, из которых полезного
    # действия было около 65 кадров: первые 30 не происходило ничего,
    # последние 85 — медленный дрейф. Здесь 3.2 секунды и структура,
    # принятая в рисованной анимации: замах — быстрый заход — посадка
    # с перелётом — осадка — держание.
    #
    #   1..10   покой
    #   10..24  ЗАМАХ: губа уходит в противоход, вперёд и наружу
    #   24..40  ЗАХОД: губа заворачивается внутрь, быстро
    #   40..57  ЗУБЫ садятся, с перелётом на 10%
    #   57..66  ОСАДКА: перелёт гасится, мякоть догоняет
    #   66..96  ДЕРЖАНИЕ с медленным проскальзыванием
    # ------------------------------------------------------------------
    F_ANT, F_IN, F_TOP = 10, 24, 40
    F_BITE, F_BITE_LEN = 40, 17

    def ease_out(x, p=2.4):
        """Резкий старт, мягкий приход. Именно так читается живое
        движение; smootherstep трогается почти горизонтально и даёт
        ощущение паузы там, где её нет."""
        x = max(0.0, min(1.0, x))
        return 1.0 - (1.0 - x) ** p

    def anticip(f):
        """Замах. Без него действие выглядит включённым, а не начатым."""
        if f <= F_ANT or f >= F_IN + 6:
            return 0.0
        if f < 18:
            return ease_out((f - F_ANT) / 8.0)
        return 1.0 - smoother((f - 18) / 12.0)

    def master(f):
        if f <= F_IN:
            return 0.0
        if f < F_TOP:
            return ease_out((f - F_IN) / (F_TOP - F_IN), 2.2)
        return 1.0

    def bite(f):
        """Посадка зубов с перелётом: приезжают на 10% глубже и
        возвращаются. Без перелёта зубы просто встают в позицию —
        это самый заметный признак неживого движения."""
        x = max(0.0, min(1.0, (f - F_BITE) / F_BITE_LEN))
        v = ease_out(x)
        over = 0.10 * math.exp(-((f - (F_BITE + F_BITE_LEN)) / 6.0) ** 2)
        return v + over * v

    def settle(f):
        """Осадка мякоти: один затухающий качок сразу после посадки."""
        if f <= F_BITE + 12:
            return 0.0
        d = f - (F_BITE + 12)
        return math.sin(2 * math.pi * d / 13.0) * math.exp(-d / 9.0)

    def slip(f):
        """Проскальзывание губы по зубам после того, как прикус состоялся."""
        st = F_BITE + F_BITE_LEN + 9
        if f <= st:
            return 0.0
        return smoother((f - st) / (F_END - 6 - st))

    for f in range(1, F_END + 1):
        m = master(f)
        an = anticip(f)
        sl = slip(f)
        bt = bite(f)
        se = settle(f)
        lin = ('LINEAR', 'AUTO')
        # ЗАМАХ вводится отрицательным значением того же ключа заворота:
        # губа сначала подаётся наружу и только потом уходит внутрь
        key_shape(k_roll, f, m - 0.15 * an, lin)
        # масса догоняет кромку: запаздывание в 3 кадра плюс осадка
        key_shape(k_mass, f, min(1.0, master(f - 3) * 1.05) + 0.22 * se, lin)
        key_shape(k_press_hi, f, bt * (1.0 - 0.45 * sl), lin)
        key_shape(k_press_lo, f, 0.90 * sl, lin)
        key_shape(k_open, f, 0.70 + 0.14 * m + 0.05 * an, lin)
        # челюсть перед укусом чуть ОТКРЫВАЕТСЯ — противоход
        key_shape(k_jaw, f, max(0.05, 0.62 - 0.54 * m + 0.11 * an), lin)
        key_obj(upper_jaw, "location", f, -0.055 * bt - 0.008 * sl, index=1, style=lin)
        # мякоть качается один раз после удара, а не болтается в конце
        key_shape(k_wob_a, f, 0.55 * se, lin)
        key_shape(k_wob_b, f, 0.34 * se, lin)

    for kb, frames in [
        # выпячивание убрано: оно смыкало щель и прятало зубы,
        # то есть читалось не замахом, а другим действием
        (k_pout, [(1, 0.0, SIO), (F_END, 0.0, SIO)]),
        (k_smirk, [(1, 0.0, SIO), (F_TOP, 0.0, SIO), (F_END, 0.20, SIO)]),
    ]:
        for f, v, st in frames:
            key_shape(kb, f, v, st)

    # Разворот всей головы убран. Медленный увод на 3.8 градуса за ролик
    # читается именно как проезд камеры, хотя двигался объект.
    root.rotation_euler = (0.006, 0.004, 0.010)

    # Микродрожание тоже снято: на статичной камере любая его доля
    # читается как шевеление кадра.
    # --- свет: вытянутые источники дают длинные блики, как в референсе ---
    target = bpy.data.objects.new("Cam_Target", None)
    bpy.context.collection.objects.link(target)
    target.location = (BITE_SIDE * 0.30, 0.0, -0.30) if ZOOM else (0.0, 0.0, -0.24)

    def add_light(name, loc, energy, sx, sy, color=(1, 1, 1)):
        data = bpy.data.lights.new(name, 'AREA')
        data.energy, data.color = energy, color
        data.shape = 'RECTANGLE'
        data.size, data.size_y = sx, sy
        ob = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(ob)
        ob.location = loc
        c = ob.constraints.new('TRACK_TO')
        c.target = target
        c.track_axis = 'TRACK_NEGATIVE_Z'
        c.up_axis = 'UP_Y'
        return ob

    # Свет выровнен: узкий яркий ключ давал на матовой губе светлую
    # полосу, которая всё равно читалась бликом. Ключ ослаблен, источник
    # для глянцевых полос убран, заполнение поднято.
    add_light("Key", (-1.9, -2.4, 1.9), 150, 3.6, 2.6, (1.0, 0.97, 0.95))
    add_light("Fill", (2.4, -2.2, 0.6), 95, 4.0, 4.0, (0.95, 0.96, 1.0))
    add_light("Rim", (0.9, 1.9, 1.1), 55, 1.8, 1.8, (1.0, 0.88, 0.86))

    # светлая подложка вместо кожи
    bmp = bmesh.new()
    for sx, sy in ((-9, -6), (9, -6), (9, 6), (-9, 6)):
        bmp.verts.new((sx, sy, 0.0))
    bmp.verts.ensure_lookup_table()
    bmp.faces.new(bmp.verts)
    mep = bpy.data.meshes.new("Backdrop")
    bmp.to_mesh(mep)
    bmp.free()
    backdrop = bpy.data.objects.new("Backdrop", mep)
    bpy.context.collection.objects.link(backdrop)
    backdrop.location = (0.0, 0.0, -3.6)
    backdrop.parent = world_root
    mat_bg = bpy.data.materials.new("Backdrop")
    mat_bg.use_nodes = True
    ntb = mat_bg.node_tree
    for n in list(ntb.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            ntb.nodes.remove(n)
    em = ntb.nodes.new("ShaderNodeEmission")
    em.inputs[0].default_value = (0.86, 0.850, 0.840, 1.0)
    # Ровная заливка фона сама по себе выглядит дёшево: в студии за
    # объектом всегда есть спад к краям. Он же отделяет силуэт.
    bc = ntb.nodes.new("ShaderNodeTexCoord")
    bmap = ntb.nodes.new("ShaderNodeMapping")
    bmap.inputs["Scale"].default_value = (0.115, 0.135, 0.115)
    bgr = ntb.nodes.new("ShaderNodeTexGradient")
    bgr.gradient_type = 'SPHERICAL'
    bramp = ntb.nodes.new("ShaderNodeValToRGB")
    # в мультяшной подаче фон плоский; спад оставлен едва заметным,
    # только чтобы силуэт не сливался с краями кадра
    bramp.color_ramp.elements[0].position = 0.05
    bramp.color_ramp.elements[0].color = (0.845, 0.845, 0.850, 1.0)
    bramp.color_ramp.elements[1].position = 0.95
    bramp.color_ramp.elements[1].color = (0.930, 0.928, 0.925, 1.0)
    ntb.links.new(bc.outputs["Object"], bmap.inputs["Vector"])
    ntb.links.new(bmap.outputs["Vector"], bgr.inputs["Vector"])
    ntb.links.new(bgr.outputs["Fac"], bramp.inputs["Fac"])
    ntb.links.new(bramp.outputs["Color"], em.inputs[0])
    ntb.links.new(em.outputs[0], ntb.nodes["Material Output"].inputs["Surface"])
    backdrop.data.materials.append(mat_bg)

    # --- серый макет ---------------------------------------------------
    # Форму и движение видно только без цвета: на глянце любой блик
    # маскирует и провал, и лишний объём. Цвет включается обратно,
    # когда по форме претензий нет.
    if CLAY:
        clay = bpy.data.materials.new("Clay")
        clay.use_nodes = True
        bsdf = clay.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value = (0.62, 0.60, 0.58, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.62
        bsdf.inputs["Specular IOR Level"].default_value = 0.22
        dark = bpy.data.materials.new("ClayDark")
        dark.use_nodes = True
        db = dark.node_tree.nodes["Principled BSDF"]
        db.inputs["Base Color"].default_value = (0.045, 0.045, 0.047, 1.0)
        db.inputs["Roughness"].default_value = 0.85
        for name, mat in (("Lips", clay), ("Teeth_U", clay), ("Cavity", dark)):
            ob = bpy.data.objects.get(name)
            if ob is None:
                continue
            ob.data.materials.clear()
            ob.data.materials.append(mat)
        em.inputs[0].default_value = (0.42, 0.42, 0.43, 1.0)

    world = bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.020, 0.020, 0.022, 1.0)

    # --- камера ---
    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = 85.0
    cam_data.dof.use_dof = True
    cam_data.dof.focus_object = target
    cam_data.dof.aperture_fstop = 4.0
    shake = bpy.data.objects.new("Cam_Shake", None)
    bpy.context.collection.objects.link(shake)
    cam = bpy.data.objects.new("Camera", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.parent = shake
    scene.camera = cam
    c = cam.constraints.new('TRACK_TO')
    c.target = target
    c.track_axis = 'TRACK_NEGATIVE_Z'
    c.up_axis = 'UP_Y'
    # КАМЕРА НЕПОДВИЖНА: ни наезда, ни тряски. Ключей у камеры нет вовсе.
    cam.location = (-0.28, -3.25, -0.12) if ZOOM else (0.0, -8.20, -0.02)

    for act in bpy.data.actions:
        for fc in all_fcurves(act):
            for kp in fc.keyframe_points:
                st = KEY_STYLE.get((fc.data_path, fc.array_index, round(kp.co.x)))
                if st and st[0] != 'BEZIER':
                    kp.interpolation, kp.easing = st
                else:
                    kp.interpolation = 'BEZIER'
                    kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
            fc.update()

    # --- рендер ---
    r = scene.render
    r.resolution_x, r.resolution_y = RES
    r.resolution_percentage = 100
    # На этом материале EEVEE даёт ту же картинку, что Cycles, но считает
    # секунды вместо минут. ENGINE=CYCLES включает трассировку при нужде.
    if os.environ.get("LIPBITE_ENGINE") == "CYCLES":
        r.engine = 'CYCLES'
        scene.cycles.device = 'GPU'
        scene.cycles.samples = SAMPLES
        scene.cycles.use_denoising = True
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = 0.01
        prefs = bpy.context.preferences.addons['cycles'].preferences
        prefs.compute_device_type = 'METAL'
        prefs.get_devices()
        for d in prefs.devices:
            d.use = (d.type == 'METAL')
    else:
        r.engine = 'BLENDER_EEVEE'
        ee = scene.eevee
        for attr, val in (("taa_render_samples", 16), ("use_raytracing", True),
                          ("use_shadows", True)):
            if hasattr(ee, attr):
                setattr(ee, attr, val)
    # Смаз выключен: на плоских заливках он мылит контур,
    # в мультяшной подаче его не применяют.
    r.use_motion_blur = False
    r.motion_blur_shutter = 0.40
    if CLAY:                       # макет смотрят на резкость формы
        cam_data.dof.use_dof = False
    try:
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
    except Exception:
        pass

    if BUILD_ONLY:
        print("SCENE BUILT")
        return

    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT_DIR, "lip_bite.blend"))

    if TEST:
        r.image_settings.file_format = 'PNG'
        for f in TEST_FRAMES:
            scene.frame_set(f)
            r.filepath = os.path.join(OUT_DIR, f"test_{f:03d}.png")
            bpy.ops.render.render(write_still=True)
    else:
        if "media_type" in r.image_settings.bl_rna.properties:
            r.image_settings.media_type = 'VIDEO'
        r.image_settings.file_format = 'FFMPEG'
        r.ffmpeg.format = 'MPEG4'
        r.ffmpeg.codec = 'H264'
        r.ffmpeg.constant_rate_factor = 'PERC_LOSSLESS'
        r.ffmpeg.ffmpeg_preset = 'GOOD'
        r.ffmpeg.audio_codec = 'NONE'
        r.filepath = os.path.join(OUT_DIR, "clay_" if CLAY else "lip_bite_")
        bpy.ops.render.render(animation=True)

    print("DONE:", OUT_DIR)


main()
