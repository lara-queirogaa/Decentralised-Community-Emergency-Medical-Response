#!/usr/bin/env python3
"""
Interface gráfica (pygame) da simulação de resposta médica de emergência.

Esta interface NÃO altera os agentes nem o main.py. Ela "liga-se" por fora:

  1. Corre o main.run(cfg) (a simulação a sério) no mesmo loop asyncio.
  2. Apanha os objetos que o main cria (Environment, Dispatcher, Verification).
  3. Escuta todas as mensagens SPADE trocadas entre agentes e transforma-as
     em animações (balões, bolinhas a viajar, anéis, cartões de incidente).
  4. Lê o estado dos agentes (posição, reputação) 60 vezes por segundo
     e desenha o mapa, a partir da matriz do Environment (os "bloquinhos").

Utilização:
    spade run                              (noutro terminal, servidor XMPP)
    python interface.py                    (menu com todos os cenários)
    python interface.py scenarios/cenario01.json

Teclas (durante a simulação):
    ESC / Q  sair      R  reiniciar      S  captura de ecrã      L  mostrar/esconder log
"""

import argparse
import asyncio
import bisect
import collections
import io
import json
import math
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# --------------------------------------------------------------------------
# Argumentos (antes de importar pygame, para poder forçar modo sem janela)
# --------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Interface gráfica da simulação.")
parser.add_argument("scenario", nargs="?", type=Path,
                    help="JSON do cenário (se omitido, abre o menu)")
parser.add_argument("--original", action="store_true",
                    help="não liga dispatcher.environment (comportamento original do main.py)")
parser.add_argument("--headless", action="store_true",
                    help="sem janela (para testes / gerar imagens)")
parser.add_argument("--shot", default="",
                    help="tempos (s) para guardar capturas, ex.: 4,10,20 (e sair no fim)")
parser.add_argument("--shot-dir", type=Path, default=HERE / "capturas")
parser.add_argument("--size", default="1360x820", help="tamanho da janela")
parser.add_argument("--from-menu", action="store_true", help=argparse.SUPPRESS)
ARGS = parser.parse_args()

if ARGS.headless:
    os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402
import pygame.gfxdraw as gfx  # noqa: E402

# --------------------------------------------------------------------------
# Cores e constantes
# --------------------------------------------------------------------------
BG = (9, 13, 24)
PANEL = (15, 22, 38)
CARD = (23, 33, 54)
LINE = (40, 53, 80)
TEXT = (226, 232, 240)
DIM = (128, 144, 172)
MAPBG = (12, 18, 32)
ROAD = (45, 52, 66)
ROAD_LINE = (82, 92, 112)

RED = (239, 68, 68)
AMBER = (245, 158, 11)
GREEN = (52, 211, 153)
BLUE = (96, 165, 250)
CYAN = (34, 211, 238)
PURPLE = (167, 139, 250)
PINK = (244, 114, 182)
GREY = (148, 163, 184)
WHITE = (241, 245, 249)

BUILD_COLORS = [(52, 66, 96), (60, 75, 108), (47, 60, 88), (66, 80, 112)]

# Códigos da matriz do Environment
C_ROAD, C_BUILDING, C_HOSPITAL, C_BLOCK = 0, 1, 2, 9

MSG_COLORS = {
    "incident": AMBER, "urgent_pre_alert": RED, "verify_request": BLUE,
    "witness_response": CYAN, "confirmed_incident": RED,
    "cancel_alert": GREY, "reputation_feedback": (250, 204, 21),
    "dispatch_order": RED, "dispatch_cancel": GREY, "unit_status": GREEN,
    "cpr_request": PINK, "cpr_response": PINK, "cpr_cancel": GREY,
}

_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = pygame.font.SysFont(
            "segoeui,helveticaneue,ubuntu,dejavusans,arial,liberationsans",
            size, bold=bold)
    return _font_cache[key]


def text(surf, s, size, color, pos, anchor="topleft", bold=False):
    img = font(size, bold).render(str(s), True, color)
    r = img.get_rect(**{anchor: pos})
    surf.blit(img, r)
    return r


def fit(s, size, width, bold=False):
    """Corta o texto com '…' para caber em 'width' píxeis."""
    f = font(size, bold)
    if f.size(s)[0] <= width:
        return s
    while s and f.size(s + "…")[0] > width:
        s = s[:-1]
    return s + "…"


def wrap(s, size, width):
    f = font(size)
    lines, cur = [], ""
    for word in str(s).split():
        t = (cur + " " + word).strip()
        if f.size(t)[0] <= width:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def lerp(a, b, t):
    return a + (b - a) * t


def with_alpha(c, a):
    return (c[0], c[1], c[2], max(0, min(255, int(a))))


def circle(surf, color, c, r, alpha=255):
    r = max(1, int(r))
    x, y = int(c[0]), int(c[1])
    col = (*color[:3], alpha)
    gfx.filled_circle(surf, x, y, r, col)
    gfx.aacircle(surf, x, y, r, col)


def ring(surf, color, c, r, w=2, alpha=255):
    pygame.draw.circle(surf, (*color[:3], alpha), (int(c[0]), int(c[1])), max(1, int(r)), w)


def rrect(surf, color, rect, radius, w=0):
    pygame.draw.rect(surf, color, rect, w, border_radius=int(radius))


# --------------------------------------------------------------------------
# Recolha de dados da simulação (sem tocar nos agentes)
# --------------------------------------------------------------------------
class Recorder:
    """Guarda o que a interface precisa: objetos da simulação, mensagens, log."""

    def __init__(self):
        self.env = None
        self.dispatcher = None
        self.verification = None
        self.inbox = collections.deque()          # mensagens por tratar
        self.log = collections.deque(maxlen=500)  # linhas de texto (stdout)
        self.msg_count = 0
        self.t0 = time.monotonic()

    def now(self):
        return time.monotonic() - self.t0


class Tee(io.TextIOBase):
    """Copia o que o programa faz print para o painel de log (e para o terminal)."""

    def __init__(self, orig, rec):
        self.orig, self.rec, self.buf = orig, rec, ""

    def write(self, s):
        self.orig.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.rec.log.append((self.rec.now(), line.rstrip()))
        return len(s)

    def flush(self):
        self.orig.flush()


def short_id(e):
    """witness_001 -> W1, sensor_001 -> S1 (os outros ids já são curtos)."""
    digits = "".join(ch for ch in str(e.id) if ch.isdigit()).lstrip("0") or "0"
    if e.kind == "witness":
        return "W" + digits
    if e.kind == "smartwatch":
        return "S" + digits
    return str(e.id)


def bare(jid):
    return str(jid).split("/")[0]


def install_hooks(rec, sim, fix_dispatcher):
    """
    Liga a interface à simulação sem alterar os ficheiros do grupo:
      * Container.send  -> regista todas as mensagens entre agentes
      * sim.build_*     -> guarda o Environment, o Dispatcher e a Verification
    """
    from spade.container import Container

    orig_send = Container.send

    async def send(self, msg, behaviour=None):
        try:
            data = json.loads(msg.body)
            rec.inbox.append((rec.now(), bare(msg.sender), bare(msg.to), data))
            rec.msg_count += 1
        except Exception:
            pass
        return await orig_send(self, msg, behaviour)

    Container.send = send

    b_env, b_dis, b_ver = (sim.build_environment, sim.build_dispatcher,
                           sim.build_verification)

    def build_environment(cfg):
        rec.env = b_env(cfg)
        return rec.env

    def build_dispatcher(*a, **k):
        d = b_dis(*a, **k)
        rec.dispatcher = d
        if fix_dispatcher:
            d.environment = rec.env   # a linha que falta no main.py
        return d

    def build_verification(*a, **k):
        rec.verification = b_ver(*a, **k)
        return rec.verification

    sim.build_environment = build_environment
    sim.build_dispatcher = build_dispatcher
    sim.build_verification = build_verification

    sys.stdout = Tee(sys.stdout, rec)


# --------------------------------------------------------------------------
# Estado visual (o que está a acontecer no ecrã)
# --------------------------------------------------------------------------
class Ent:
    """Uma coisa desenhada no mapa (um agente)."""

    def __init__(self, kind, agent):
        self.kind = kind
        self.agent = agent
        self.jid = bare(agent.jid)
        self.id = (getattr(agent, "unit_id", None)
                   or getattr(agent, "witness_id", None)
                   or getattr(agent, "sensor_id", "?"))
        self.vpos = tuple(float(v) for v in agent.location)
        self.anim = None
        self.heading = (1, 0)
        self.status = "available"
        self.pending = {}             # incident_id -> destino da ordem
        self.cancel_return = False
        self.cpr = False
        self.cpr_target = None
        self.dest = None

    @property
    def moving(self):
        return self.anim is not None


class Visual:
    STATUS_PT = {"en_route": "a caminho", "on_scene": "no local",
                 "returning": "a regressar", "available": "livre",
                 "rejected": "ocupada"}

    def __init__(self, rec):
        self.rec = rec
        self.ents = {}                # id(agent) -> Ent
        self.by_jid = {}              # jid -> Ent ou 'dispatcher'/'verification'
        self.dots = []                # mensagens a viajar
        self.pulses = []              # anéis de chegada
        self.bubbles = []             # balões de fala
        self.incidents = collections.OrderedDict()
        self.hq = {}                  # posições dos dois agentes "fora do mapa"
        self.next_bubble = 0.0
        self.finished_at = None
        self.stats = {"confirmed": 0, "false": 0}

    # --- sincronização com os agentes -------------------------------------
    def sync(self):
        env = self.rec.env
        if env is None:
            return
        for kind, agents in env.agents.items():
            for a in agents:
                if id(a) not in self.ents:
                    e = Ent(kind, a)
                    self.ents[id(a)] = e
                    self.by_jid[e.jid] = e
        if self.rec.dispatcher is not None:
            self.by_jid[bare(self.rec.dispatcher.jid)] = "dispatcher"
        if self.rec.verification is not None:
            self.by_jid[bare(self.rec.verification.jid)] = "verification"

    def ent_list(self, *kinds):
        return [e for e in self.ents.values() if e.kind in kinds]

    # --- posição de qualquer coisa (agente ou HQ) em células ----------------
    def cell_pos(self, key):
        if isinstance(key, Ent):
            return key.vpos
        return None

    # --- eventos ----------------------------------------------------------
    def bubble(self, key, msg, color, now, dur=2.3):
        same = [b for b in self.bubbles if b["key"] is key]
        for old in same[:-1]:
            self.bubbles.remove(old)
        # escalonar: no máximo um balão novo a cada 0.45 s, para dar para ler
        t0 = max(now, self.next_bubble)
        self.next_bubble = t0 + 0.45
        self.bubbles.append({"key": key, "text": msg, "color": color,
                             "t0": t0, "dur": dur})

    def card(self, incident_id, now):
        c = self.incidents.get(incident_id)
        if c is None:
            c = {"id": incident_id, "origin": "?", "source": "?", "conf": None,
                 "rep": None, "urgent": False, "state": "verifying",
                 "score": None, "votes": [], "units": [], "cpr": None,
                 "loc": None, "t0": now}
            self.incidents[incident_id] = c
        return c

    def handle(self, ev, now):
        t, sender, to, data = ev
        kind = data.get("message_type", "?")
        src, dst = self.by_jid.get(sender), self.by_jid.get(to)
        color = MSG_COLORS.get(kind, GREY)

        if src is not None and dst is not None and src is not dst:
            self.dots.append({"src": src, "dst": dst, "t0": now, "dur": 0.9,
                              "color": color, "kind": kind})

        iid = data.get("incident_id")

        if kind == "incident":
            c = self.card(iid, now)
            c.update(origin=data.get("origin"), source=data.get("source_id"),
                     conf=data.get("confidence"), rep=data.get("reputation"),
                     urgent=bool(data.get("urgent")), loc=data.get("location"))
            if data.get("origin") == "smartwatch":
                self.bubble(src, f"Queda detetada! {int(100 * (data.get('confidence') or 0))}%", RED, now)
            else:
                self.bubble(src, "Vi um acidente!", AMBER, now)

        elif kind == "urgent_pre_alert":
            c = self.card(iid, now)
            c["urgent"] = True
            self.bubble("dispatcher", "Pré-alerta: despachar já!", RED, now)

        elif kind == "verify_request":
            self.card(iid, now)
            self.bubble(dst, "Podes confirmar?", BLUE, now)

        elif kind == "witness_response":
            c = self.card(iid, now)
            ok = bool(data.get("confirmed"))
            c["votes"].append((data.get("witness_id", "?"), ok))
            self.bubble(src, "Confirmo!" if ok else "Não vejo nada", GREEN if ok else GREY, now)

        elif kind == "confirmed_incident":
            c = self.card(iid, now)
            c["state"] = "confirmed"
            c["score"] = data.get("verification_score")
            self.stats["confirmed"] += 1
            sc = data.get("verification_score")
            self.bubble("verification", f"Confirmado ({sc})" if sc is not None else "Confirmado", RED, now)

        elif kind == "cancel_alert":
            c = self.card(iid, now)
            c["state"] = "false"
            self.stats["false"] += 1
            self.bubble("verification", "Falso alarme!", GREY, now)

        elif kind == "dispatch_order":
            c = self.card(iid, now)
            if isinstance(dst, Ent):
                c["units"].append(dst.id)
                dst.pending[iid] = tuple(data.get("location", (0, 0)))
                self.bubble(dst, "Ordem recebida", RED, now)

        elif kind == "dispatch_cancel":
            if isinstance(dst, Ent):
                self.cancel_unit(dst, iid, now)

        elif kind == "unit_status" and isinstance(src, Ent):
            self.unit_status(src, data, now)

        elif kind == "cpr_request":
            c = self.card(iid, now)
            c["cpr"] = "a pedir RCP"
            self.bubble(dst, "Sabes fazer RCP?", PINK, now)

        elif kind == "cpr_response" and isinstance(src, Ent):
            c = self.card(iid, now)
            if data.get("accepted"):
                c["cpr"] = f"{src.id} a fazer RCP"
                src.cpr = True
                src.cpr_target = tuple(c["loc"] or src.agent.location)
                self.bubble(src, "A fazer RCP!", PINK, now, 4)
            else:
                c["cpr"] = f"{src.id} recusou"
                self.bubble(src, "Não posso", GREY, now)

        elif kind == "cpr_cancel" and isinstance(dst, Ent):
            dst.cpr = False
            self.card(iid, now)["cpr"] = "cancelado"
            self.bubble(dst, "RCP cancelada", GREY, now)

        elif kind == "reputation_feedback" and isinstance(dst, Ent):
            good = data.get("outcome") == "confirmed"
            self.bubble(dst, "reputação +" if good else "reputação −",
                        GREEN if good else RED, now, 2.4)

    # --- movimento das viaturas ---------------------------------------------
    def start_move(self, e, dest, now):
        """Calcula o percurso (o mesmo que a viatura usa) e anima-o."""
        env = self.rec.env
        speed = getattr(e.agent, "speed", 2.0)
        start = tuple(e.agent.location)
        path = env.shortest_path(start, dest) if env else None
        pts = [tuple(map(float, p)) for p in path] if path else [start, tuple(dest)]
        if pts[-1] != tuple(map(float, dest)):
            pts.append(tuple(map(float, dest)))
        durs = []
        for a, b in zip(pts, pts[1:]):
            ia, ib = tuple(map(int, a)), tuple(map(int, b))
            if env and env.g.has_edge(ia, ib):
                w = env.g[ia][ib].get("weight", 1)
            else:
                w = math.dist(a, b)
            durs.append(w / speed)
        e.vpos = pts[0]
        self.set_anim(e, pts, durs, now)
        e.dest = tuple(map(float, dest))

    def set_anim(self, e, pts, durs, now):
        keep = [(p, d) for p, d in zip(pts[1:], durs) if d > 1e-6]
        pts2, durs2, cum = [pts[0]], [], [0.0]
        for p, d in keep:
            pts2.append(p)
            durs2.append(d)
            cum.append(cum[-1] + d)
        e.anim = {"pts": pts2, "dur": durs2, "cum": cum, "t0": now,
                  "total": cum[-1]} if durs2 else None
        if not durs2:
            e.vpos = pts[-1]

    def anim_state(self, e, now):
        a = e.anim
        t = now - a["t0"]
        if t >= a["total"]:
            return a["pts"][-1], len(a["pts"]) - 1, 1.0, True
        i = max(0, bisect.bisect_right(a["cum"], t) - 1)
        i = min(i, len(a["dur"]) - 1)
        f = (t - a["cum"][i]) / a["dur"][i]
        p0, p1 = a["pts"][i], a["pts"][i + 1]
        return (lerp(p0[0], p1[0], f), lerp(p0[1], p1[1], f)), i, f, False

    def update_units(self, now):
        for e in self.ents.values():
            if e.anim:
                pos, i, f, done = self.anim_state(e, now)
                a = e.anim
                nxt = a["pts"][min(i + 1, len(a["pts"]) - 1)]
                cur = a["pts"][i]
                dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
                if dx or dy:
                    e.heading = (dx, dy)
                e.vpos = pos
                if done:
                    e.anim = None

    def unit_status(self, e, data, now):
        status = data.get("status")
        e.status = status
        iid = data.get("incident_id")
        if status == "en_route":
            dest = e.pending.get(iid) or data.get("location")
            if dest:
                self.start_move(e, dest, now)
        elif status == "on_scene":
            e.anim = None
            if e.dest:
                e.vpos = e.dest
            self.pulses.append({"key": e, "t0": now, "color": GREEN})
        elif status == "returning":
            if not e.cancel_return and e.kind == "ambulance":
                self.start_move(e, tuple(e.agent.hospital_location), now)
        elif status == "available":
            e.cancel_return = False
            if e.kind == "ambulance":
                e.anim = None
                e.vpos = tuple(map(float, e.agent.hospital_location))
        elif status == "rejected":
            self.bubble(e, "Estou ocupada", GREY, now)

    def cancel_unit(self, e, iid, now):
        if e.anim and e.status == "en_route":
            if e.kind == "ambulance":
                # volta pelo mesmo caminho por onde veio
                pos, i, f, _ = self.anim_state(e, now)
                a = e.anim
                pts = [pos] + list(reversed(a["pts"][:i + 1]))
                durs = [a["dur"][i] * f] + list(reversed(a["dur"][:i]))
                self.set_anim(e, pts, durs, now)
                e.cancel_return = True
            else:
                e.anim = None          # a polícia fica onde está
        self.bubble(e, "Missão cancelada", GREY, now)


# --------------------------------------------------------------------------
# Desenho: mapa
# --------------------------------------------------------------------------
def render_static(env, cs):
    """Desenha a parte do mapa que não se mexe: estradas, edifícios, hospitais."""
    gw, gh = env.width, env.height
    surf = pygame.Surface((gw * cs, gh * cs))
    surf.fill(ROAD)
    rnd = random.Random(7)
    grid = env.grid

    def passable(x, y):
        return 0 <= x < gw and 0 <= y < gh and grid[y][x] in (C_ROAD, C_HOSPITAL, C_BLOCK)

    # linhas tracejadas ao centro das ruas
    for y in range(gh):
        for x in range(gw):
            if grid[y][x] != C_ROAD:
                continue
            for dx, dy in ((1, 0), (0, 1)):
                if grid[y][x] == C_ROAD and passable(x + dx, y + dy) and \
                        grid[y + dy][x + dx] == C_ROAD:
                    cx, cy = x * cs + cs / 2, y * cs + cs / 2
                    for k in range(3):
                        t = (k + .5) / 3 * cs
                        px, py = cx + dx * t, cy + dy * t
                        pygame.draw.circle(surf, ROAD_LINE, (int(px), int(py)),
                                           max(1, cs // 22))

    for y in range(gh):
        for x in range(gw):
            code = grid[y][x]
            r = pygame.Rect(x * cs, y * cs, cs, cs)
            if code == C_BUILDING:
                base = BUILD_COLORS[(x * 7 + y * 13) % len(BUILD_COLORS)]
                inner = r.inflate(-cs * .16, -cs * .16)
                rrect(surf, (30, 38, 56), inner.move(0, cs * .04), cs * .2)
                rrect(surf, base, inner, cs * .2)
                rrect(surf, tuple(min(255, c + 22) for c in base),
                      pygame.Rect(inner.x, inner.y, inner.w, max(3, inner.h // 5)), cs * .15)
                ws = max(3, cs // 8)
                for wy in range(2):
                    for wx in range(2):
                        lit = rnd.random() < .6
                        col = (252, 211, 77) if lit else (38, 48, 72)
                        wr = pygame.Rect(inner.x + inner.w * (.22 + .4 * wx),
                                         inner.y + inner.h * (.38 + .28 * wy), ws, ws)
                        rrect(surf, col, wr, 1)
            elif code == C_HOSPITAL:
                inner = r.inflate(-cs * .12, -cs * .12)
                rrect(surf, (220, 230, 242), inner, cs * .18)
                rrect(surf, (186, 200, 220), inner, cs * .18, 2)
                arm, th = cs * .46, max(4, cs * .16)
                pygame.draw.rect(surf, RED, (r.centerx - arm / 2, r.centery - th / 2, arm, th))
                pygame.draw.rect(surf, RED, (r.centerx - th / 2, r.centery - arm / 2, th, arm))
            elif code == C_BLOCK:
                inner = r.inflate(-cs * .1, -cs * .1)
                rrect(surf, (30, 30, 36), inner, 4)
                n = 6
                for k in range(-n, n + 1):
                    pts = [(inner.x + k * cs / 5, inner.bottom),
                           (inner.x + k * cs / 5 + cs / 10, inner.bottom),
                           (inner.x + k * cs / 5 + cs / 10 + inner.h, inner.y),
                           (inner.x + k * cs / 5 + inner.h, inner.y)]
                    clip = surf.get_clip()
                    surf.set_clip(inner)
                    pygame.draw.polygon(surf, AMBER if k % 2 == 0 else (30, 30, 36), pts)
                    surf.set_clip(clip)
                rrect(surf, RED, inner, 4, 2)
    return surf


# ---- ícones --------------------------------------------------------------
def draw_witness(surf, c, cs, color=GREEN, label=None):
    r = cs * .17
    circle(surf, (14, 80, 60), (c[0], c[1] + cs * .02), cs * .33, 140)
    pygame.draw.circle(surf, color, (int(c[0]), int(c[1] - cs * .14)), int(r))
    pygame.draw.ellipse(surf, color, (c[0] - cs * .2, c[1] - cs * .02, cs * .4, cs * .32))
    ring(surf, WHITE, (c[0], c[1] - cs * .14), r, 1)


def draw_watch(surf, c, cs, color=BLUE, t=0):
    w, h = cs * .32, cs * .44
    rrect(surf, (30, 41, 70), (c[0] - w * .42, c[1] - h * .72, w * .84, h * 1.44), cs * .06)
    rrect(surf, color, (c[0] - w / 2, c[1] - h / 2, w, h), cs * .08)
    rrect(surf, (15, 23, 42), (c[0] - w / 2 + 3, c[1] - h / 2 + 3, w - 6, h - 6), cs * .05)
    k = (t * 1.6) % 1
    pygame.draw.line(surf, WHITE, c, (c[0] + math.sin(k * 6.28) * cs * .1,
                                      c[1] - math.cos(k * 6.28) * cs * .1), 1)


def draw_car(surf, c, cs, kind, heading, moving, t):
    horiz = abs(heading[0]) >= abs(heading[1])
    L, W = cs * .74, cs * .42
    w, h = (L, W) if horiz else (W, L)
    body = pygame.Rect(0, 0, w, h)
    body.center = (int(c[0]), int(c[1]))
    rrect(surf, (10, 14, 24), body.move(1, 2), cs * .12)
    if kind == "ambulance":
        rrect(surf, WHITE, body, cs * .12)
        stripe = pygame.Rect(0, 0, w, h * .26) if horiz else pygame.Rect(0, 0, w * .26, h)
        stripe.center = body.center
        pygame.draw.rect(surf, RED, stripe)
        a, th = cs * .17, max(2, cs * .05)
        pygame.draw.rect(surf, RED, (body.centerx - a / 2, body.centery - th / 2, a, th))
        pygame.draw.rect(surf, RED, (body.centerx - th / 2, body.centery - a / 2, th, a))
    else:
        rrect(surf, (37, 99, 235), body, cs * .12)
        stripe = pygame.Rect(0, 0, w, h * .26) if horiz else pygame.Rect(0, 0, w * .26, h)
        stripe.center = body.center
        pygame.draw.rect(surf, WHITE, stripe)
    # sirene
    on = int(t * 6) % 2 == 0 if moving else False
    c1 = RED if on else (110, 40, 40)
    c2 = BLUE if not on else (30, 64, 130)
    if kind == "police":
        c1, c2 = (RED if on else (110, 40, 40)), (BLUE if not on else (30, 64, 130))
    off = cs * .09
    if horiz:
        circle(surf, c1, (body.centerx, body.top + 1), cs * .07)
        circle(surf, c2, (body.centerx, body.bottom - 1), cs * .07)
    else:
        circle(surf, c1, (body.left + 1, body.centery), cs * .07)
        circle(surf, c2, (body.right - 1, body.centery), cs * .07)
    rrect(surf, WHITE, body, cs * .12, 1)


def draw_heart(surf, c, size, color, alpha=255):
    s = size
    pygame.draw.circle(surf, (*color, alpha), (int(c[0] - s * .25), int(c[1] - s * .1)), int(s * .28))
    pygame.draw.circle(surf, (*color, alpha), (int(c[0] + s * .25), int(c[1] - s * .1)), int(s * .28))
    pygame.draw.polygon(surf, (*color, alpha), [(c[0] - s * .5, c[1] - s * .02),
                                                (c[0] + s * .5, c[1] - s * .02),
                                                (c[0], c[1] + s * .55)])


def dashed(surf, color, p0, p1, dash=7, gap=6, w=2):
    d = math.dist(p0, p1)
    if d < 1:
        return
    ux, uy = (p1[0] - p0[0]) / d, (p1[1] - p0[1]) / d
    s = 0
    while s < d:
        e = min(d, s + dash)
        pygame.draw.line(surf, color, (p0[0] + ux * s, p0[1] + uy * s),
                         (p0[0] + ux * e, p0[1] + uy * e), w)
        s += dash + gap


# --------------------------------------------------------------------------
# A aplicação gráfica
# --------------------------------------------------------------------------
class App:
    def __init__(self, cfg, scenario_path, rec, vis):
        self.cfg, self.path, self.rec, self.vis = cfg, scenario_path, rec, vis
        w, h = (int(v) for v in ARGS.size.lower().split("x"))
        self.screen = pygame.display.set_mode((w, h), pygame.RESIZABLE)
        pygame.display.set_caption("Resposta Médica de Emergência — Mapa da simulação")
        self.clock = pygame.time.Clock()
        self.static, self.static_key = None, None
        self.show_log = True
        self.map_rect = pygame.Rect(0, 0, 10, 10)
        self.cs = 32
        self.flash = None

    # --- geometria -------------------------------------------------------
    def layout(self):
        W, H = self.screen.get_size()
        self.W, self.H = W, H
        self.head = 66
        self.foot = 48
        self.strip = 54
        self.panel_w = max(330, min(410, int(W * .3)))
        self.panel = pygame.Rect(W - self.panel_w, self.head, self.panel_w, H - self.head - self.foot)
        area = pygame.Rect(0, self.head + self.strip, W - self.panel_w, H - self.head - self.strip - self.foot)
        env = self.rec.env
        gw, gh = env.width, env.height
        avail = area.inflate(-40, -34)
        self.cs = max(8, int(min(avail.w / gw, avail.h / gh)))
        mw, mh = gw * self.cs, gh * self.cs
        self.map_rect = pygame.Rect(area.centerx - mw // 2, area.centery - mh // 2, mw, mh)
        self.strip_rect = pygame.Rect(0, self.head, W - self.panel_w, self.strip)

    def to_px(self, p):
        return (self.map_rect.x + (p[0] + .5) * self.cs,
                self.map_rect.y + (p[1] + .5) * self.cs)

    def key_px(self, key, ents_px):
        if isinstance(key, Ent):
            return ents_px.get(id(key), self.to_px(key.vpos))
        return self.vis.hq.get(key)

    # --- quadro ----------------------------------------------------------
    def draw(self, now):
        env = self.rec.env
        scr = self.screen
        scr.fill(BG)
        if env is None:
            text(scr, "A preparar a simulação…", 22, DIM, scr.get_rect().center, "center")
            return
        self.layout()
        vis = self.vis

        # mapa estático (refeito só quando a matriz muda, ex.: estrada cortada)
        key = (self.cs, tuple(tuple(r) for r in env.grid))
        if key != self.static_key:
            self.static, self.static_key = render_static(env, self.cs), key

        # fundo da zona do mapa
        area = pygame.Rect(0, self.head, self.W - self.panel_w, self.H - self.head - self.foot)
        pygame.draw.rect(scr, MAPBG, area)
        frame = self.map_rect.inflate(14, 14)
        rrect(scr, (22, 31, 52), frame, 12)
        scr.blit(self.static, self.map_rect)
        rrect(scr, LINE, frame, 12, 2)

        # coordenadas nas margens
        for x in range(env.width):
            text(scr, x, 10, DIM, (self.map_rect.x + (x + .5) * self.cs, self.map_rect.y - 14), "center")
        for y in range(env.height):
            text(scr, y, 10, DIM, (self.map_rect.x - 14, self.map_rect.y + (y + .5) * self.cs), "center")

        fx = pygame.Surface((self.W, self.H), pygame.SRCALPHA)
        cs = self.cs

        # trânsito: ruas mais caras ficam coloridas
        pulse = .5 + .5 * math.sin(now * 3)
        for a, b, w in env.g.edges(data="weight", default=1):
            if w and w > 1:
                col = AMBER if w < 2.2 else RED
                pygame.draw.line(fx, with_alpha(col, 110 + 50 * pulse),
                                 self.to_px(a), self.to_px(b), max(4, int(cs * .22)))

        # posições dos agentes em píxeis (com um pequeno desvio se partilham célula)
        groups = collections.defaultdict(list)
        for e in vis.ents.values():
            groups[(round(e.vpos[0]), round(e.vpos[1]))].append(e)
        ents_px = {}
        for cell, lst in groups.items():
            lst.sort(key=lambda e: ("witness", "smartwatch", "police", "ambulance").index(e.kind))
            n = len(lst)
            for i, e in enumerate(lst):
                base = self.to_px(e.vpos)
                off = (i - (n - 1) / 2) * cs * (.30 if n > 1 else 0)
                ents_px[id(e)] = (base[0] + off, base[1] + (cs * .12 if n > 1 and e.kind in ("witness", "smartwatch") else 0))

        # acidentes
        for acc in env.accidents:
            c = self.to_px(acc["position"])
            real = acc["real"]
            col = RED if real else AMBER
            for k in range(3):
                ph = (now * .9 + k / 3) % 1
                ring(fx, col, c, cs * (.2 + .75 * ph), 2, 190 * (1 - ph))
            circle(fx, col, c, cs * .2, 230)
            text(fx, "!" if real else "?", int(cs * .34), WHITE, c, "center", True)

        # rotas das viaturas em movimento
        for e in vis.ents.values():
            if e.anim:
                col = BLUE if e.kind == "police" else WHITE
                a = e.anim
                _, i, _, _ = vis.anim_state(e, now)
                pts = [ents_px[id(e)]] + [self.to_px(p) for p in a["pts"][i + 1:]]
                for p0, p1 in zip(pts, pts[1:]):
                    dashed(fx, with_alpha(col, 190), p0, p1, max(4, cs // 6), max(4, cs // 7), 2)
                if e.dest:
                    ring(fx, col, self.to_px(e.dest), cs * (.42 + .08 * pulse), 2, 200)

        # RCP: ligação testemunha -> vítima
        for e in vis.ents.values():
            if e.cpr and e.cpr_target:
                dashed(fx, with_alpha(PINK, 220), ents_px[id(e)], self.to_px(e.cpr_target), 6, 5, 2)

        scr.blit(fx, (0, 0))

        # agentes (testemunhas e smartwatches por baixo, viaturas por cima)
        for kind in ("witness", "smartwatch", "police", "ambulance"):
            for e in vis.ent_list(kind):
                c = ents_px[id(e)]
                if kind == "witness":
                    draw_witness(scr, c, cs, PINK if e.cpr else GREEN)
                elif kind == "smartwatch":
                    draw_watch(scr, (c[0], c[1]), cs, BLUE, now)
                else:
                    draw_car(scr, c, cs, kind, e.heading, e.moving, now)
                lab = short_id(e)
                col = TEXT
                if kind in ("witness", "smartwatch"):
                    rep = e.agent.get_reputation()
                    text(scr, f"{rep:.2f}", max(9, int(cs * .22)), (252, 211, 77), (c[0], c[1] + cs * .52), "midtop")
                tr = text(scr, lab, max(9, int(cs * .26)), col, (c[0], c[1] - cs * .5), "midbottom", True)

        # coração do RCP
        for e in vis.ents.values():
            if e.cpr:
                c = ents_px[id(e)]
                beat = 1 + .25 * abs(math.sin(now * 5))
                draw_heart(scr, (c[0] + cs * .38, c[1] - cs * .45), cs * .32 * beat, PINK)

        # efeitos (por cima): bolinhas de mensagens, pulsos, balões
        fx2 = pygame.Surface((self.W, self.H), pygame.SRCALPHA)
        self.draw_hq(fx2, now)
        alive = []
        for d in vis.dots:
            t = (now - d["t0"]) / d["dur"]
            if t >= 1:
                vis.pulses.append({"key": d["dst"], "t0": now, "color": d["color"], "small": True})
                continue
            alive.append(d)
            p0, p1 = self.key_px(d["src"], ents_px), self.key_px(d["dst"], ents_px)
            if not p0 or not p1:
                continue
            e = t * t * (3 - 2 * t)
            for k in range(5):
                tt = max(0, e - k * .035)
                px = (lerp(p0[0], p1[0], tt), lerp(p0[1], p1[1], tt))
                circle(fx2, d["color"], px, max(2, cs * (.13 - .02 * k)), 230 - k * 45)
        vis.dots = alive

        alive = []
        for p in vis.pulses:
            t = (now - p["t0"]) / (.6 if p.get("small") else 1.1)
            if t >= 1:
                continue
            alive.append(p)
            c = self.key_px(p["key"], ents_px)
            if c:
                ring(fx2, p["color"], c, cs * (.3 + (.45 if p.get("small") else .9) * t), 3, 220 * (1 - t))
        vis.pulses = alive
        scr.blit(fx2, (0, 0))

        alive = []
        for b in vis.bubbles:
            if now - b["t0"] < b["dur"]:
                alive.append(b)
        vis.bubbles = alive
        stacks = collections.Counter()
        for b in reversed(vis.bubbles):
            if now < b["t0"]:
                continue
            c = self.key_px(b["key"], ents_px)
            if not c:
                continue
            k = id(b["key"]) if not isinstance(b["key"], str) else b["key"]
            slot = stacks[k]
            stacks[k] += 1
            self.draw_bubble(c, b, now, slot)

        self.draw_header(now)
        self.draw_panel(now)
        self.draw_footer()
        if self.vis.finished_at is not None:
            self.draw_end_banner()

    def draw_hq(self, surf, now):
        """Os dois agentes que não vivem no mapa: Dispatcher e Verificação."""
        vis = self.vis
        sr = self.strip_rect
        specs = [("verification", "VERIFICAÇÃO", "valida os alertas", CYAN, .28),
                 ("dispatcher", "DISPATCHER", "envia as unidades", RED, .72)]
        for key, name, sub, col, fx_ in specs:
            cx = sr.x + int(sr.w * fx_)
            r = pygame.Rect(0, 0, 220, 40)
            r.center = (cx, sr.centery)
            vis.hq[key] = (cx, sr.centery)
            rrect(self.screen, CARD, r, 10)
            rrect(self.screen, col, r, 10, 2)
            ic = (r.x + 22, r.centery)
            circle(self.screen, col, ic, 12, 60)
            if key == "dispatcher":      # auscultador / rádio
                pygame.draw.rect(self.screen, col, (ic[0] - 6, ic[1] - 5, 12, 10), 2, border_radius=2)
                pygame.draw.line(self.screen, col, (ic[0] + 2, ic[1] - 5), (ic[0] + 6, ic[1] - 10), 2)
            else:                        # lupa
                pygame.draw.circle(self.screen, col, (ic[0] - 1, ic[1] - 1), 6, 2)
                pygame.draw.line(self.screen, col, (ic[0] + 3, ic[1] + 3), (ic[0] + 8, ic[1] + 8), 2)
            text(self.screen, name, 13, TEXT, (r.x + 42, r.y + 6), bold=True)
            text(self.screen, sub, 11, DIM, (r.x + 42, r.y + 22))
            n = sum(1 for d in vis.dots if d["dst"] == key and False)
        # as pulsações extra são desenhadas em fx2 (via vis.pulses)

    def draw_bubble(self, c, b, now, slot):
        age = now - b["t0"]
        a = 255 if age < b["dur"] - .5 else int(255 * (b["dur"] - age) / .5)
        f = font(12, True)
        w = f.size(b["text"])[0] + 18
        h = 24
        rise = min(1, age / .18)
        x = c[0] - w / 2
        below = isinstance(b["key"], str)          # HQ: balão por baixo do cartão
        if below:
            y = c[1] + 26 + slot * (h + 4) + 4 * rise
        else:
            y = c[1] - self.cs * .95 - slot * (h + 4) - 6 * rise
        x = max(4, min(self.W - self.panel_w - w - 4, x))
        y = max(self.head + 4, y)
        s = pygame.Surface((w, h + 14), pygame.SRCALPHA)
        rrect(s, (15, 23, 42, int(a * .95)), (0, 7, w, h), 8)
        rrect(s, (*b["color"], a), (0, 7, w, h), 8, 2)
        px = max(10, min(w - 10, c[0] - x))
        if below:
            pygame.draw.polygon(s, (15, 23, 42, int(a * .95)), [(px - 5, 8), (px + 5, 8), (px, 1)])
        else:
            pygame.draw.polygon(s, (15, 23, 42, int(a * .95)), [(px - 5, h + 6), (px + 5, h + 6), (px, h + 13)])
        img = f.render(b["text"], True, TEXT)
        img.set_alpha(a)
        s.blit(img, (9, 11))
        self.screen.blit(s, (x, y - 7 if below else y))

    # --- cabeçalho / rodapé ------------------------------------------------
    def draw_header(self, now):
        scr = self.screen
        pygame.draw.rect(scr, PANEL, (0, 0, self.W, self.head))
        pygame.draw.line(scr, LINE, (0, self.head), (self.W, self.head))
        # logo: cruz médica
        pygame.draw.circle(scr, RED, (36, 33), 17)
        pygame.draw.rect(scr, WHITE, (36 - 9, 33 - 3, 18, 6))
        pygame.draw.rect(scr, WHITE, (36 - 3, 33 - 9, 6, 18))
        text(scr, "Resposta Médica de Emergência Descentralizada", 19, TEXT, (64, 12), bold=True)
        name = self.cfg.get("name", "sem nome")
        text(scr, fit(name, 13, self.W - self.panel_w - 90), 13, DIM, (64, 38))

        # chips à direita
        chips = []
        done = self.vis.finished_at is not None
        t = self.vis.finished_at if done else now
        chips.append((f"{int(t // 60):02d}:{int(t % 60):02d}", TEXT))
        chips.append((f"{self.rec.msg_count} mensagens", BLUE))
        if done:
            chips.append(("TERMINADO", GREY))
        elif self.rec.env and self.rec.env.accidents:
            chips.append(("EMERGÊNCIA", RED))
        else:
            chips.append(("À ESPERA", AMBER))
        x = self.W - 16
        for label, col in reversed(chips):
            f = font(14, True)
            w = f.size(label)[0] + 26
            r = pygame.Rect(x - w, 18, w, 30)
            rrect(scr, CARD, r, 15)
            rrect(scr, col, r, 15, 2)
            text(scr, label, 14, col, r.center, "center", True)
            x -= w + 8

    def draw_footer(self):
        scr = self.screen
        y0 = self.H - self.foot
        pygame.draw.rect(scr, PANEL, (0, y0, self.W, self.foot))
        pygame.draw.line(scr, LINE, (0, y0), (self.W, y0))
        cs = 26
        x = 16
        cy = y0 + self.foot // 2
        items = [("amb", "Ambulância"), ("pol", "Polícia"), ("wit", "Testemunha"),
                 ("sw", "Smartwatch"), ("acc", "Acidente real"), ("fal", "Falso alarme"),
                 ("blk", "Estrada cortada"), ("trf", "Trânsito")]
        for k, label in items:
            c = (x + 14, cy)
            if k == "amb":
                draw_car(scr, c, cs, "ambulance", (1, 0), False, 0)
            elif k == "pol":
                draw_car(scr, c, cs, "police", (1, 0), False, 0)
            elif k == "wit":
                draw_witness(scr, c, cs)
            elif k == "sw":
                draw_watch(scr, c, cs)
            elif k in ("acc", "fal"):
                col = RED if k == "acc" else AMBER
                circle(scr, col, c, 9)
                text(scr, "!" if k == "acc" else "?", 12, WHITE, c, "center", True)
            elif k == "blk":
                rrect(scr, AMBER, (c[0] - 10, c[1] - 8, 20, 16), 3)
                rrect(scr, RED, (c[0] - 10, c[1] - 8, 20, 16), 3, 2)
            else:
                pygame.draw.line(scr, AMBER, (c[0] - 11, c[1]), (c[0] + 11, c[1]), 5)
            r = text(scr, label, 12, DIM, (x + 32, cy), "midleft")
            x = r.right + 18
        hint = "ESC sair   R reiniciar   S captura   L log"
        text(scr, hint, 12, DIM, (self.W - 14, cy), "midright")

    # --- painel lateral ----------------------------------------------------
    def section(self, y, title, count=None):
        scr = self.screen
        x = self.panel.x + 14
        text(scr, title.upper(), 11, DIM, (x, y), bold=True)
        if count is not None:
            text(scr, str(count), 11, DIM, (self.panel.right - 14, y), "topright", True)
        pygame.draw.line(scr, LINE, (x, y + 16), (self.panel.right - 14, y + 16))
        return y + 22

    def draw_panel(self, now):
        scr = self.screen
        p = self.panel
        pygame.draw.rect(scr, PANEL, p)
        pygame.draw.line(scr, LINE, (p.x, p.y), (p.x, p.bottom))
        x, w = p.x + 14, p.w - 28
        y = p.y + 10

        # incidentes
        incs = list(self.vis.incidents.values())
        y = self.section(y, "Incidentes", len(incs))
        if not incs:
            text(scr, "Nenhum incidente ainda…", 13, DIM, (x, y + 2))
            y += 30
        for c in incs[-2:][::-1]:
            y = self.draw_card(c, x, y, w, now) + 8

        # unidades
        units = self.vis.ent_list("ambulance", "police")
        y = self.section(y + 2, "Unidades", len(units))
        for e in units:
            r = pygame.Rect(x, y, w, 26)
            rrect(scr, CARD, r, 8)
            draw_car(scr, (x + 22, y + 13), 24, e.kind, (1, 0), e.moving, now)
            text(scr, e.id, 13, TEXT, (x + 44, y + 4), bold=True)
            st = Visual.STATUS_PT.get(e.status, e.status)
            col = {"en_route": AMBER, "on_scene": GREEN, "returning": BLUE,
                   "rejected": RED}.get(e.status, DIM)
            circle(scr, col, (r.right - 14 - font(12).size(st)[0] - 10, y + 13), 4)
            text(scr, st, 12, col, (r.right - 10, y + 13), "midright")
            y += 30

        # testemunhas e smartwatches
        people = self.vis.ent_list("witness", "smartwatch")
        y = self.section(y + 2, "Pessoas e sensores", len(people))
        cx = x
        for e in people:
            rep = e.agent.get_reputation()
            label = f"{e.id}  {rep:.2f}"
            wch = font(12, True).size(label)[0] + 36
            if cx + wch > p.right - 14:
                cx, y = x, y + 28
            r = pygame.Rect(cx, y, wch, 24)
            rrect(scr, CARD, r, 12)
            ic = (r.x + 14, r.centery)
            if e.kind == "witness":
                draw_witness(scr, ic, 20, PINK if e.cpr else GREEN)
            else:
                draw_watch(scr, ic, 20)
            text(scr, label, 12, TEXT, (r.x + 28, r.centery), "midleft", True)
            cx += wch + 6
        y += 34

        # registo
        if self.show_log:
            y = self.section(y, "Registo dos agentes")
            lines = [(t, s) for t, s in self.rec.log
                     if not s.startswith("---") and not s.startswith("===")
                     and not set(s) <= set(".#HXWPAS!~ ")]
            avail = p.bottom - y - 6
            n = max(1, avail // 15)
            clip = scr.get_clip()
            scr.set_clip(pygame.Rect(p.x, y, p.w, avail))
            for i, (t, s) in enumerate(lines[-n:]):
                col = DIM
                if "[DISPATCHER]" in s:
                    col = (252, 165, 165)
                elif "[ENV]" in s:
                    col = (253, 224, 71)
                elif s.startswith("[") :
                    col = (134, 239, 172)
                elif "WITNESS" in s or "Witness" in s:
                    col = (147, 197, 253)
                text(scr, fit(s, 11, w), 11, col, (x, y + i * 15))
            scr.set_clip(clip)

    def draw_card(self, c, x, y, w, now):
        scr = self.screen
        h = 92
        r = pygame.Rect(x, y, w, h)
        age = now - c["t0"]
        rrect(scr, CARD, r, 10)
        col = {"verifying": AMBER, "confirmed": RED, "false": GREY}[c["state"]]
        pygame.draw.rect(scr, col, (r.x, r.y + 8, 4, h - 16), border_radius=2)
        title = f"Incidente {str(c['id'])[:6]}"
        text(scr, title, 14, TEXT, (r.x + 14, r.y + 8), bold=True)
        state = {"verifying": "A VERIFICAR", "confirmed": "CONFIRMADO", "false": "FALSO ALARME"}[c["state"]]
        f = font(11, True)
        cw = f.size(state)[0] + 16
        cr = pygame.Rect(r.right - cw - 10, r.y + 8, cw, 20)
        rrect(scr, (*col,), cr, 10, 2)
        text(scr, state, 11, col, cr.center, "center", True)

        origin = {"smartwatch": "smartwatch", "witness": "testemunha"}.get(c["origin"], c["origin"])
        conf = f"{int(100 * c['conf'])}%" if c["conf"] is not None else "–"
        text(scr, fit(f"{origin} {c['source']} · confiança {conf}" + ("  · URGENTE" if c["urgent"] else ""),
                      12, w - 24), 12, RED if c["urgent"] else DIM, (r.x + 14, r.y + 32))

        # barra de confiança
        bx, by, bw = r.x + 14, r.y + 52, w - 28
        rrect(scr, (10, 16, 30), (bx, by, bw, 6), 3)
        if c["conf"] is not None:
            rrect(scr, col, (bx, by, max(4, bw * c["conf"]), 6), 3)
        thr = getattr(self.rec.verification, "threshold", None)
        if thr is not None:
            tx = bx + bw * thr
            pygame.draw.line(scr, WHITE, (tx, by - 3), (tx, by + 9), 1)

        # votos das testemunhas + pontuação
        vx = r.x + 14
        for wid, ok in c["votes"][:5]:
            circle(scr, GREEN if ok else RED, (vx + 5, r.y + 72), 5)
            r2 = text(scr, wid, 11, TEXT, (vx + 14, r.y + 72), "midleft")
            vx = r2.right + 10
        if c["score"] is not None:
            text(scr, f"pontuação {c['score']}", 11, TEXT, (r.right - 12, r.y + 72), "midright", True)
        tail = []
        if c["units"]:
            tail.append("unidades: " + " ".join(c["units"]))
        if c["cpr"]:
            tail.append(c["cpr"])
        if not c["votes"] and tail:
            text(scr, fit(" · ".join(tail), 11, w - 28), 11, (251, 207, 232) if c["cpr"] else DIM,
                 (r.x + 14, r.y + 66))
        elif tail:
            text(scr, fit(" · ".join(tail), 11, w - 28), 11, (251, 207, 232) if c["cpr"] else DIM,
                 (r.x + 14, r.y + 80))
        return r.bottom

    def draw_end_banner(self):
        s = self.vis.stats
        r = pygame.Rect(0, 0, min(620, self.map_rect.w), 74)
        r.midbottom = (self.map_rect.centerx, self.map_rect.bottom - 14)
        ov = pygame.Surface(r.size, pygame.SRCALPHA)
        rrect(ov, (9, 13, 24, 235), ov.get_rect(), 14)
        rrect(ov, (*GREY, 255), ov.get_rect(), 14, 2)
        self.screen.blit(ov, r)
        text(self.screen, "Simulação terminada", 18, TEXT, (r.centerx, r.y + 22), "center", True)
        text(self.screen, f"{len(self.vis.incidents)} incidente(s) · {s['confirmed']} confirmado(s) · "
             f"{s['false']} falso(s) alarme(s) · {self.rec.msg_count} mensagens     R reiniciar · ESC sair",
             13, DIM, (r.centerx, r.y + 50), "center")

    def save_shot(self, name):
        self.ARGS_dir = ARGS.shot_dir
        ARGS.shot_dir.mkdir(parents=True, exist_ok=True)
        path = ARGS.shot_dir / name
        pygame.image.save(self.screen, str(path))
        print(f"[GUI] captura guardada em {path}")
        return path


# --------------------------------------------------------------------------
# Ecrãs auxiliares
# --------------------------------------------------------------------------
def message_screen(title, lines, color=AMBER, footer="ESC para sair"):
    size = tuple(int(v) for v in ARGS.size.lower().split("x"))
    scr = pygame.display.set_mode(size, pygame.RESIZABLE)
    clock = pygame.time.Clock()
    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q)):
                return False
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_r:
                return True
        scr.fill(BG)
        W, H = scr.get_size()
        r = pygame.Rect(0, 0, min(760, W - 60), 60 + 24 * len(lines) + 70)
        r.center = (W // 2, H // 2)
        rrect(scr, CARD, r, 16)
        rrect(scr, color, r, 16, 2)
        text(scr, title, 24, color, (r.x + 30, r.y + 24), bold=True)
        for i, ln in enumerate(lines):
            text(scr, ln, 15, TEXT, (r.x + 30, r.y + 70 + i * 24))
        text(scr, footer, 13, DIM, (r.x + 30, r.bottom - 34))
        pygame.display.flip()
        clock.tick(30)
        if ARGS.headless:
            return False


def xmpp_up(domain, port=5222):
    host = "localhost" if domain in ("localhost", "127.0.0.1") else domain
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def menu():
    """Lista os cenários; ao escolher um abre-o noutro processo."""
    size = tuple(int(v) for v in ARGS.size.lower().split("x"))
    scr = pygame.display.set_mode(size, pygame.RESIZABLE)
    pygame.display.set_caption("Resposta Médica de Emergência — Escolher cenário")
    files = sorted((HERE / "scenarios").glob("*.json"))
    items = []
    for f in files:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append((f, c.get("name", f.stem), c.get("description", "")))
    clock = pygame.time.Clock()
    scroll = 0
    cols, ch, gap = 2, 92, 14
    while True:
        W, H = scr.get_size()
        cw = (W - 2 * 40 - gap * (cols - 1)) // cols
        rows = math.ceil(len(items) / cols)
        total = rows * (ch + gap)
        view = H - 130
        scroll = max(0, min(max(0, total - view), scroll))
        mouse = pygame.mouse.get_pos()
        clicked = None
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q)):
                return
            if ev.type == pygame.MOUSEWHEEL:
                scroll -= ev.y * 50
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                clicked = ev.pos
        scr.fill(BG)
        pygame.draw.rect(scr, PANEL, (0, 0, W, 92))
        pygame.draw.circle(scr, RED, (62, 46), 22)
        pygame.draw.rect(scr, WHITE, (62 - 12, 46 - 4, 24, 8))
        pygame.draw.rect(scr, WHITE, (62 - 4, 46 - 12, 8, 24))
        text(scr, "Resposta Médica de Emergência Descentralizada", 24, TEXT, (100, 20), bold=True)
        text(scr, "Escolhe um cenário para ver a simulação no mapa", 14, DIM, (100, 56))
        clip = scr.get_clip()
        scr.set_clip(pygame.Rect(0, 100, W, view + 20))
        for i, (f, name, desc) in enumerate(items):
            col, row = i % cols, i // cols
            r = pygame.Rect(40 + col * (cw + gap), 110 + row * (ch + gap) - scroll, cw, ch)
            hover = r.collidepoint(mouse) and 100 < mouse[1] < H - 20
            rrect(scr, (32, 46, 74) if hover else CARD, r, 12)
            rrect(scr, BLUE if hover else LINE, r, 12, 2)
            num = f.stem.split("_")[0].replace("cenario", "")
            circle(scr, RED if hover else (60, 80, 120), (r.x + 30, r.y + 30), 17)
            text(scr, num, 14, WHITE, (r.x + 30, r.y + 30), "center", True)
            text(scr, fit(name, 15, cw - 80, True), 15, TEXT, (r.x + 58, r.y + 12), bold=True)
            for j, ln in enumerate(wrap(desc, 12, cw - 76)[:3]):
                text(scr, ln, 12, DIM, (r.x + 58, r.y + 38 + j * 16))
            if clicked and r.collidepoint(clicked) and 100 < clicked[1] < H - 20:
                scr.set_clip(clip)
                pygame.quit()
                subprocess.call([sys.executable, str(Path(__file__).resolve()), str(f),
                                 "--from-menu", "--size", ARGS.size]
                                + (["--original"] if ARGS.original else []))
                pygame.init()
                scr = pygame.display.set_mode(size, pygame.RESIZABLE)
                break
        scr.set_clip(clip)
        pygame.draw.rect(scr, BG, (0, H - 30, W, 30))
        text(scr, "roda do rato para deslizar · ESC sair", 12, DIM, (W // 2, H - 15), "center")
        pygame.display.flip()
        clock.tick(30)


# --------------------------------------------------------------------------
# Corrida de um cenário
# --------------------------------------------------------------------------
async def run_gui(cfg, path):
    import main as sim
    rec = Recorder()
    install_hooks(rec, sim, fix_dispatcher=not ARGS.original)
    vis = Visual(rec)
    app = App(cfg, path, rec, vis)
    rec.t0 = time.monotonic()
    task = asyncio.create_task(sim.run(cfg))
    shots = sorted(float(s) for s in ARGS.shot.split(",") if s.strip())
    result = "quit"
    started = time.monotonic()

    while True:
        now = rec.now()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                result = "quit"
                break
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    result = "quit"
                    break
                if ev.key == pygame.K_r:
                    result = "restart"
                    break
                if ev.key == pygame.K_l:
                    app.show_log = not app.show_log
                if ev.key == pygame.K_s:
                    app.save_shot(f"{path.stem}_{int(now):03d}s.png")
        else:
            result = None

        if result:
            break

        vis.sync()
        while rec.inbox:
            vis.handle(rec.inbox.popleft(), now)
        vis.update_units(now)

        if task.done() and vis.finished_at is None:
            vis.finished_at = now
            exc = None if task.cancelled() else task.exception()
            if exc is not None:
                result = ("error", exc)
                break

        app.draw(now)
        pygame.display.flip()

        if shots and now >= shots[0]:
            app.save_shot(f"{path.stem}_t{int(shots[0]):03d}.png")
            shots.pop(0)
            if not shots and ARGS.shot:
                result = "quit"
                break
        if ARGS.shot and task.done() and shots:
            # a simulação acabou antes dos tempos pedidos: tira uma última captura
            app.save_shot(f"{path.stem}_final.png")
            result = "quit"
            break

        await asyncio.sleep(1 / 60)

    # parar a simulação com calma (o main.run tem um 'finally' que pára os agentes)
    if not task.done():
        task.cancel()
    try:
        await asyncio.wait_for(task, timeout=15)
    except BaseException:
        pass
    sys.stdout = sys.__stdout__
    return result


def main():
    pygame.init()
    if ARGS.scenario is None:
        menu()
        return

    path = ARGS.scenario
    if not path.exists():
        print(f"Cenário não encontrado: {path}")
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))

    domain = cfg.get("server", {}).get("domain", "localhost")
    while not xmpp_up(domain):
        again = message_screen(
            "Servidor XMPP desligado",
            ["Os agentes SPADE precisam do servidor para conversarem.",
             "Abre outro terminal na pasta do projeto e corre:",
             "",
             "        spade run",
             "",
             "Quando aparecer 'Server started...' carrega em R para tentar outra vez."],
            footer="R tentar outra vez   ·   ESC sair")
        if not again:
            return

    while True:
        outcome = asyncio.run(run_gui(cfg, path))
        if outcome == "restart":
            # processo novo: o SPADE guarda estado global, é mais seguro recomeçar do zero
            pygame.quit()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        if isinstance(outcome, tuple):
            err = outcome[1]
            lines = wrap(f"{type(err).__name__}: {err}", 15, 680)[:6]
            if isinstance(err, (ConnectionError, OSError)):
                lines += ["", "Verifica se o 'spade run' está a correr."]
            message_screen("A simulação falhou", lines, RED, "ESC sair")
        break
    pygame.quit()


if __name__ == "__main__":
    main()
