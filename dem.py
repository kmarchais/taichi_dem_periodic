import taichi as ti
import math
import os
import numpy as np

ti.init(arch=ti.gpu)
vec = ti.math.vec2

SAVE_FRAMES = False

window_size = 512  # Number of pixels of the window
n = 8192  # Number of grains

density = 100.0
stiffness = 8e3
restitution_coef = 0.001
gravity = -9.81
dt = 0.0001  # Larger dt might lead to unstable results.
substeps = 60


@ti.dataclass
class Grain:
    p: vec  # Position
    m: ti.f32  # Mass
    r: ti.f32  # Radius
    v: vec  # Velocity
    a: vec  # Acceleration
    f: vec  # Force


gf = Grain.field(shape=(n, ))

length = 1.0
grid_n = 128
grid_size = length / grid_n  # Simulation domain of size [0, 1]
print(f"Grid size: {grid_n}x{grid_n}")

grain_r_min = 0.002
grain_r_max = 0.003

assert grain_r_max * 2 < grid_size


@ti.kernel
def init():
    for i in gf:
        # Spread grains in a restricted area.
        l = i * grid_size
        padding = 0.1 * length
        region_width = length - padding * 2
        gf[i].p = vec(
            l % region_width + padding + grid_size * ti.random() * 0.2,
            l // region_width * grid_size + 0.1 * length,
        )
        gf[i].r = ti.random() * (grain_r_max - grain_r_min) + grain_r_min
        gf[i].m = density * math.pi * gf[i].r**2


@ti.kernel
def update():
    for i in gf:
        a = gf[i].f / gf[i].m
        gf[i].v += (gf[i].a + a) * dt / 2.0
        gf[i].p += gf[i].v * dt + 0.5 * a * dt**2
        gf[i].a = a


@ti.kernel
def apply_gravity():
    for i in gf:
        gf[i].f = vec(0.0, gravity * gf[i].m)


@ti.kernel
def apply_bc():
    bounce_coef = 0.3  # Velocity damping
    for i in gf:
        x = gf[i].p[0]
        y = gf[i].p[1]

        if y - gf[i].r < 0:
            gf[i].p[1] = gf[i].r
            gf[i].v[1] *= -bounce_coef

        elif y + gf[i].r > length:
            gf[i].p[1] = length - gf[i].r
            gf[i].v[1] *= -bounce_coef

        if x < 0:
            gf[i].p[0] += length
        elif x > length:
            gf[i].p[0] -= length


@ti.func
def resolve(i, j, shift):
    rel_pos = gf[j].p - gf[i].p
    rel_pos[0] -= shift
    dist = ti.sqrt(rel_pos[0]**2 + rel_pos[1]**2)
    delta = -dist + gf[i].r + gf[j].r  # delta = d - 2 * r
    if delta > 0:  # in contact
        normal = rel_pos / dist
        f1 = normal * delta * stiffness
        # Damping force
        M = (gf[i].m * gf[j].m) / (gf[i].m + gf[j].m)
        K = stiffness
        C = (2.0 * (1.0 / ti.sqrt(1.0 +
                                  (math.pi / ti.log(restitution_coef))**2)) *
             ti.sqrt(K * M))
        V = (gf[j].v - gf[i].v) * normal
        f2 = C * V * normal
        gf[i].f += f2 - f1
        gf[j].f -= f2 - f1


list_head = ti.field(dtype=ti.i32, shape=grid_n * grid_n)
list_cur = ti.field(dtype=ti.i32, shape=grid_n * grid_n)
list_tail = ti.field(dtype=ti.i32, shape=grid_n * grid_n)

grain_count = ti.field(dtype=ti.i32,
                       shape=(grid_n, grid_n),
                       name="grain_count")
column_sum = ti.field(dtype=ti.i32, shape=grid_n, name="column_sum")
prefix_sum = ti.field(dtype=ti.i32, shape=(grid_n, grid_n), name="prefix_sum")
particle_id = ti.field(dtype=ti.i32, shape=n, name="particle_id")


@ti.kernel
def contact():
    """
    Handle the collision between grains.
    """
    grain_count.fill(0)

    for i in range(n):
        grid_idx = ti.floor(gf[i].p * grid_n / length, int)
        grain_count[grid_idx] += 1

    for i in range(grid_n):
        sum = 0
        for j in range(grid_n):
            sum += grain_count[i, j]
        column_sum[i] = sum

    prefix_sum[0, 0] = 0

    ti.loop_config(serialize=True)
    for i in range(1, grid_n):
        prefix_sum[i, 0] = prefix_sum[i - 1, 0] + column_sum[i - 1]

    for i in range(grid_n):
        for j in range(grid_n):
            if j == 0:
                prefix_sum[i, j] += grain_count[i, j]
            else:
                prefix_sum[i, j] = prefix_sum[i, j - 1] + grain_count[i, j]

            linear_idx = i * grid_n + j

            list_head[linear_idx] = prefix_sum[i, j] - grain_count[i, j]
            list_cur[linear_idx] = list_head[linear_idx]
            list_tail[linear_idx] = prefix_sum[i, j]

    for i in range(n):
        grid_idx = ti.floor(gf[i].p * grid_n / length, int)
        linear_idx = grid_idx[0] * grid_n + grid_idx[1]
        grain_location = ti.atomic_add(list_cur[linear_idx], 1)
        particle_id[grain_location] = i

    # Fast collision detection
    for i in range(n):
        grid_idx = ti.floor(gf[i].p * grid_n / length, int)
        x_begin = grid_idx[0] - 1
        x_end = grid_idx[0] + 2

        y_begin = ti.max(grid_idx[1] - 1, 0)
        y_end = ti.min(grid_idx[1] + 2, grid_n)

        for x_i in range(x_begin, x_end):
            neigh_i = x_i % grid_n
            for neigh_j in range(y_begin, y_end):
                neigh_linear_idx = neigh_i * grid_n + neigh_j
                for p_idx in range(list_head[neigh_linear_idx],
                                   list_tail[neigh_linear_idx]):
                    j = particle_id[p_idx]
                    if i < j:
                        if x_i != -1 and x_i != grid_n:
                            resolve(i, j, 0)
                        elif x_i == -1:
                            resolve(i, j, length)
                        else:
                            resolve(i, j, -length)


init()
gui = ti.GUI("Taichi DEM",
             (int(2 * length * window_size), int(length * window_size)))
step = 0

if SAVE_FRAMES:
    os.makedirs("output", exist_ok=True)

while gui.running:
    for _ in range(substeps):
        apply_gravity()
        contact()
        apply_bc()
        update()

    pos = gf.p.to_numpy() / (2.0 * length)
    pos[:, 0] += 0.25
    periodic_pos = pos + np.array([0.5, 0.0])
    periodic_pos[pos[:, 0] > 0.5, 0] -= 1.0
    r = gf.r.to_numpy() * window_size
    gui.circles(pos, radius=r)
    gui.circles(
        periodic_pos,
        radius=r,
        color=0xDC7633,
    )
    if SAVE_FRAMES:
        gui.show(f"output/{step:06d}.png")
    else:
        gui.show()
    step += 1
