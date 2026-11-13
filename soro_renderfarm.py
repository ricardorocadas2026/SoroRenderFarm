# -*- coding: utf-8 -*-
"""
SoroRenderFarm V0.5  (rede local)

Um único ficheiro, dois modos:
  1) Dentro do Blender -> add-on, separador "SoroFarm" na barra lateral (N) da vista 3D.
  2) Fora do Blender   -> Worker (Slave):  python soro_renderfarm.py --worker

Só biblioteca standard + o próprio Blender. Sem Internet, sem códigos, sem contas.
"""

import json
import os
import sys
import socket
import threading
import time
import uuid
import glob
import re
import ipaddress
import subprocess
import urllib.request
import urllib.error
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

_CLI_FLAGS = ("--worker", "--install-startup", "--remove-startup")
if any(a in sys.argv for a in _CLI_FLAGS):
    bpy = None
else:
    try:
        import bpy
    except ImportError:
        bpy = None

bl_info = {
    "name": "SoroRenderFarm",
    "author": "Soro Arquitetos",
    "version": (0, 5, 0),
    "blender": (3, 6, 0),
    "location": "Vista 3D > Barra lateral (N) > SoroFarm",
    "description": "Render distribuído em rede local",
    "category": "Render",
}

APP = "SoroRenderFarm"
VERSION = "0.5.0"
HTTP_PORT = 8765
POLL_SECONDS = 1.5
MAX_ATTEMPTS = 3


# ----------------------------------------------------------------------------
# Utilitários
# ----------------------------------------------------------------------------

def data_dir():
    d = os.environ.get("SORO_DATA_DIR")
    if not d:
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
        d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return d


def load_json(name, default):
    try:
        with open(os.path.join(data_dir(), name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(name, obj):
    path = os.path.join(data_dir(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def http_json(ip, port, path, method="GET", body=None, timeout=1.5):
    """Devolve (codigo_http, dict). (None, None) se não houver ligação."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://{ip}:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return None, None


def to_unc(path):
    """Converte K:\\pasta (unidade de rede) em \\\\servidor\\partilha\\pasta.
    Assim todos os PCs chegam à pasta, mesmo com letras de unidade diferentes.
    Devolve None se a pasta for local (não partilhada)."""
    path = os.path.abspath(path)
    if os.name != "nt":
        return path
    if path.startswith("\\\\"):
        return path
    drive, rest = os.path.splitdrive(path)
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(1024)
        if ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(n)) == 0:
            return buf.value.rstrip("\\") + rest
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# Script que cada Slave executa dentro do Blender para renderizar um pedaço
# ----------------------------------------------------------------------------

TILE_SCRIPT = r'''
import bpy, os, json
t = json.loads(os.environ["SORO_TASK"])
sc = bpy.context.scene
sc.frame_set(int(t["frame"]))
r = sc.render
W, H = t["W"], t["H"]
x0, x1, y0, y1 = t["rect"]
r.use_border = True
r.use_crop_to_border = True
r.border_min_x = 0.0 if x0 <= 0 else (x0 + 0.01) / W
r.border_max_x = 1.0 if x1 >= W else (x1 + 0.01) / W
r.border_min_y = 0.0 if y0 <= 0 else (y0 + 0.01) / H
r.border_max_y = 1.0 if y1 >= H else (y1 + 0.01) / H
r.use_file_extension = False
r.filepath = t["out"]
r.image_settings.file_format = 'OPEN_EXR'
r.image_settings.color_depth = '16'
r.image_settings.color_mode = 'RGBA'
if r.engine == 'CYCLES':
    sc.cycles.device = 'GPU' if t.get("device") == "GPU" else 'CPU'
bpy.ops.render.render(write_still=True)
'''


# ----------------------------------------------------------------------------
# WORKER (Slave) - corre em cada computador, sem precisar do Blender aberto
# ----------------------------------------------------------------------------

W = None


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        if sys.stdout:
            print(line, flush=True)
    except Exception:
        pass
    try:
        with open(os.path.join(data_dir(), "worker.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_blender():
    env = os.environ.get("BLENDER_PATH")
    if env and os.path.exists(env):
        return env
    found = []
    for p in (r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
              r"C:\Program Files (x86)\Blender Foundation\Blender *\blender.exe"):
        found += glob.glob(p)
    return max(found, key=lambda p: [int(x) for x in re.findall(r"\d+", p)]) if found else ""


class WorkerState:
    NO_BLENDER = "Blender não encontrado (defina BLENDER_PATH)"

    def __init__(self, port):
        self.lock = threading.RLock()
        self.port = port
        self.hostname = socket.gethostname()
        self.blender = find_blender()
        self.paused = False
        self.task = None
        self.last = None
        self.proc = None
        self.cancelled = False
        self.error = "" if self.blender else self.NO_BLENDER

    def state(self):
        if self.paused:
            return "parado"
        if self.task:
            return "a_renderizar"
        if self.error:
            return "falha"
        return "livre"

    def status(self):
        with self.lock:
            return {"app": APP, "version": VERSION, "hostname": self.hostname,
                    "state": self.state(), "error": self.error, "cpus": os.cpu_count() or 0,
                    "task": dict(self.task) if self.task else None,
                    "last": dict(self.last) if self.last else None}

    # -- tarefas ------------------------------------------------------------
    def start_task(self, t):
        with self.lock:
            if self.paused:
                return 409, {"error": "parado"}
            if self.task:
                return 409, {"error": "ocupado"}
            if not self.blender:
                return 409, {"error": self.NO_BLENDER}
            self.task = {"job": t["job"], "tile": t["tile"], "run": t["run"], "progress": 0.0}
            self.cancelled = False
        threading.Thread(target=self._run, args=(t,), daemon=True).start()
        return 200, {"ok": True}

    def _run(self, t):
        script = os.path.join(data_dir(), "tile_render.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(TILE_SCRIPT)
        env = dict(os.environ, SORO_TASK=json.dumps(t))
        cmd = [self.blender, "-b", t["blend"], "--python-exit-code", "1", "-P", script]
        tail = deque(maxlen=12)
        rc = -1
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                 text=True, errors="replace", creationflags=flags)
            self.proc = p
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                m = re.search(r"Sample (\d+)/(\d+)", line)
                if m and int(m.group(2)) > 0:
                    with self.lock:
                        if self.task:
                            self.task["progress"] = min(1.0, int(m.group(1)) / int(m.group(2)))
            rc = p.wait()
        except Exception as e:
            tail.append(str(e))
        ok = rc == 0 and os.path.exists(t["out"])
        with self.lock:
            if ok:
                result, self.error = "ok", ""
            elif self.cancelled:
                result = "cancelado"
            else:
                result = "erro"
                self.error = ("Falha no render: " + (tail[-1] if tail else "sem informação"))[:160]
            self.last = {"job": t["job"], "tile": t["tile"], "run": t["run"], "result": result,
                         "error": self.error if result == "erro" else ""}
            self.task = None
            self.proc = None
            self.cancelled = False

    def _kill(self):
        with self.lock:
            if self.proc and self.task:
                self.cancelled = True
                try:
                    self.proc.terminate()
                except Exception:
                    pass

    # -- ligar / desligar / reiniciar ---------------------------------------
    def control(self, action):
        if action == "pausar":
            with self.lock:
                self.paused = True
            self._kill()
        elif action == "ativar":
            with self.lock:
                self.paused = False
                self.blender = find_blender()
                self.error = "" if self.blender else self.NO_BLENDER
        elif action == "cancelar":
            self._kill()
        elif action == "reiniciar":
            self._kill()
            threading.Thread(target=self._restart, daemon=True).start()
        else:
            return 400, {"error": "ação desconhecida"}
        return 200, {"ok": True}

    def _restart(self):
        time.sleep(0.5)
        args = [sys.executable, os.path.abspath(__file__), "--worker",
                "--port", str(self.port), "--delay", "2"]
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200
        subprocess.Popen(args, **kw)
        os._exit(0)


class Handler(BaseHTTPRequestHandler):
    server_version = "SoroWorker"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _lan_only(self):
        try:
            if ipaddress.ip_address(self.client_address[0]).is_private:
                return True
        except Exception:
            pass
        self._send(403, {"error": "só rede local"})
        return False

    def do_GET(self):
        if not self._lan_only():
            return
        if self.path == "/status":
            self._send(200, W.status())
        else:
            self._send(404, {})

    def do_POST(self):
        if not self._lan_only():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:
            body = {}
        if self.path == "/task":
            code, resp = W.start_task(body)
        elif self.path == "/control":
            code, resp = W.control(body.get("action", ""))
        else:
            code, resp = 404, {}
        self._send(code, resp)


def run_worker(port=HTTP_PORT, delay=0.0):
    global W
    if delay:
        time.sleep(delay)
    W = WorkerState(port)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"{APP} Worker {VERSION} ativo na porta {port} ({W.hostname})")
    log("Blender: " + (W.blender or "NÃO ENCONTRADO (defina BLENDER_PATH)"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def install_startup():
    exe = sys.executable
    pyw = exe.replace("python.exe", "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    cmd = f'"{exe}" "{os.path.abspath(__file__)}" --worker'
    r = subprocess.run(["schtasks", "/Create", "/TN", "SoroRenderFarmWorker", "/TR", cmd,
                        "/SC", "ONLOGON", "/F"])
    print("Arranque automático instalado." if r.returncode == 0 else "Falhou (tente como administrador).")


def worker_cli():
    import argparse
    ap = argparse.ArgumentParser(description=f"{APP} Worker")
    ap.add_argument("--worker", action="store_true", help="iniciar o Worker")
    ap.add_argument("--port", type=int, default=HTTP_PORT)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--install-startup", action="store_true", help="iniciar com o Windows")
    ap.add_argument("--remove-startup", action="store_true")
    a = ap.parse_args()
    if a.install_startup:
        install_startup()
    elif a.remove_startup:
        subprocess.run(["schtasks", "/Delete", "/TN", "SoroRenderFarmWorker", "/F"])
    else:
        run_worker(a.port, a.delay)


# ----------------------------------------------------------------------------
# MASTER (lógica sem dependência do Blender)
# ----------------------------------------------------------------------------

class Tile:
    def __init__(self, i, rect, out_local, out_unc):
        self.id = i
        self.rect = rect              # (x0, x1, y0, y1) em píxeis, origem em baixo à esquerda
        self.out_local = out_local
        self.out_unc = out_unc
        self.state = "pendente"       # pendente | a_renderizar | concluido
        self.slave = None
        self.run = ""
        self.progress = 0.0
        self.attempts = 0
        self.excluded = set()
        self.t_assigned = 0.0
        self.lost_since = None
        self.error = ""


class Job:
    def __init__(self, job_id, folder, folder_unc, W, H, tiles_rects, device, frame, scene_name):
        self.id = job_id
        self.folder = folder
        self.blend_unc = os.path.join(folder_unc, "scene.blend")
        self.W, self.H = W, H
        self.device = device
        self.frame = frame
        self.scene_name = scene_name
        self.state = "a_renderizar"   # a_renderizar | compor | terminado | falhou | cancelado
        self.cancel = False
        self.composing = False
        self.msg = ""
        self.final = ""
        self.denoised = ""
        self.denoise_msg = ""
        self.tiles = []
        for i, rect in enumerate(tiles_rects):
            name = f"tile_{i:03d}.exr"
            self.tiles.append(Tile(i, rect, os.path.join(folder, name), os.path.join(folder_unc, name)))

    def progress(self):
        if not self.tiles:
            return 0.0
        return sum(1.0 if t.state == "concluido" else t.progress for t in self.tiles) / len(self.tiles)


def make_tiles(W, H, n):
    """Divide a imagem em n pedaços o mais quadrados possível."""
    n = max(1, n)
    cols = max(1, min(n, round((n * W / H) ** 0.5)))
    rows = -(-n // cols)
    base, extra = divmod(n, rows)
    counts = [base + (1 if i < extra else 0) for i in range(rows)]
    ys = [round(H * i / rows) for i in range(rows + 1)]
    rects = []
    for r, c in enumerate(counts):
        xs = [round(W * i / c) for i in range(c + 1)]
        for i in range(c):
            rects.append((xs[i], xs[i + 1], ys[r], ys[r + 1]))
    return rects


class Master:
    def __init__(self):
        self.lock = threading.RLock()
        self.slaves = load_json("master.json", {}).get("slaves", {})   # ip -> dados
        self.live = {}
        self.running = False
        self.job = None

    def save(self):
        with self.lock:
            save_json("master.json", {"slaves": self.slaves})

    def add(self, ip, port=HTTP_PORT):
        with self.lock:
            if ip in self.slaves:
                return False
            self.slaves[ip] = {"ip": ip, "port": port, "name": ip}
        self.save()
        threading.Thread(target=self.poll_all, daemon=True).start()
        return True

    def remove(self, ip):
        with self.lock:
            self.slaves.pop(ip, None)
            self.live.pop(ip, None)
        self.save()

    def name(self, ip):
        with self.lock:
            return self.slaves.get(ip, {}).get("name", ip)

    # -- monitorização ------------------------------------------------------
    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self.poll_all()
            except Exception:
                pass
            time.sleep(POLL_SECONDS)

    def poll_all(self):
        with self.lock:
            items = [dict(s) for s in self.slaves.values()]
        if items:
            with ThreadPoolExecutor(max_workers=min(16, len(items))) as ex:
                list(ex.map(self.probe, items))

    def probe(self, s):
        ip = s["ip"]
        t0 = time.time()
        code, st = http_json(ip, s["port"], "/status")
        if code != 200 or not isinstance(st, dict) or st.get("app") != APP:
            self.live[ip] = {"state": "offline"}
            return
        st["ms"] = int((time.time() - t0) * 1000)
        self.live[ip] = st
        host = st.get("hostname")
        with self.lock:
            if host and ip in self.slaves and self.slaves[ip]["name"] == ip:
                self.slaves[ip]["name"] = host
                changed = True
            else:
                changed = False
        if changed:
            self.save()

    # -- ligar / desligar / reiniciar ---------------------------------------
    def control(self, ip, action):
        with self.lock:
            s = self.slaves.get(ip)
        if s:
            http_json(ip, s["port"], "/control", "POST", {"action": action}, timeout=2)
            time.sleep(0.4)
            self.probe(s)

    # -- render distribuído -------------------------------------------------
    def start_job(self, job):
        self.job = job
        threading.Thread(target=self._job_loop, args=(job,), daemon=True).start()

    def _job_loop(self, job):
        while job.state == "a_renderizar":
            try:
                self._schedule(job)
            except Exception as e:
                job.msg = f"Erro interno: {e}"
            time.sleep(1.0)

    def _requeue(self, t, count_attempt=False, exclude=False):
        if count_attempt:
            t.attempts += 1
        if exclude and t.slave:
            t.excluded.add(t.slave)
        t.state, t.slave, t.run, t.progress, t.lost_since = "pendente", None, "", 0.0, None

    def _schedule(self, job):
        if job.cancel:
            for t in job.tiles:
                if t.state == "a_renderizar" and t.slave:
                    self.control(t.slave, "cancelar")
            job.state = "cancelado"
            return
        with self.lock:
            slaves = {ip: dict(s) for ip, s in self.slaves.items()}
        live = dict(self.live)
        now = time.time()
        busy = set()

        for t in job.tiles:
            if t.state != "a_renderizar":
                continue
            st = live.get(t.slave, {"state": "offline"})
            last, task = st.get("last"), st.get("task")
            if last and last.get("run") == t.run:
                if last.get("result") == "ok":
                    t.state, t.progress = "concluido", 1.0
                    continue
                if last.get("result") == "erro":
                    t.error = last.get("error", "")
                    self._requeue(t, count_attempt=True, exclude=True)
                else:                                   # cancelado (ex.: Slave desligado)
                    self._requeue(t)
                continue
            if task and task.get("run") == t.run:
                t.progress, t.lost_since = task.get("progress", 0.0), None
                busy.add(t.slave)
                continue
            if st.get("state") == "offline":
                t.lost_since = t.lost_since or now
                if now - t.lost_since > 6:             # Slave caiu: redistribuir o pedaço
                    self._requeue(t)
                else:
                    busy.add(t.slave)
            elif st.get("state") == "parado" or now - t.t_assigned > 5:
                self._requeue(t)
            else:
                busy.add(t.slave)

        if all(t.state == "concluido" for t in job.tiles):
            job.state = "compor"
            return
        bad = [t for t in job.tiles if t.state == "pendente" and t.attempts >= MAX_ATTEMPTS]
        if bad:
            job.state = "falhou"
            job.msg = f"Pedaço {bad[0].id + 1} falhou {MAX_ATTEMPTS}x: {bad[0].error}"
            for t in job.tiles:
                if t.state == "a_renderizar" and t.slave:
                    self.control(t.slave, "cancelar")
            return

        pend = [t for t in job.tiles if t.state == "pendente"]
        for ip, s in slaves.items():
            if not pend:
                break
            if live.get(ip, {}).get("state") != "livre" or ip in busy:
                continue
            t = next((x for x in pend if ip not in x.excluded), None)
            if t is None:
                continue
            run = uuid.uuid4().hex[:8]
            body = {"job": job.id, "tile": t.id, "run": run, "blend": job.blend_unc,
                    "out": t.out_unc, "rect": list(t.rect), "W": job.W, "H": job.H,
                    "frame": job.frame, "device": job.device}
            code, _ = http_json(ip, s["port"], "/task", "POST", body, timeout=3)
            if code == 200:
                t.state, t.slave, t.run, t.t_assigned, t.progress = "a_renderizar", ip, run, now, 0.0
                pend.remove(t)
                busy.add(ip)

        if pend and not busy:
            free = [ip for ip in slaves if live.get(ip, {}).get("state") == "livre"]
            job.msg = "" if free else "À espera de Slaves livres…"
            if free:
                for t in pend:
                    t.excluded.clear()
        else:
            job.msg = ""


MASTER = None


# ----------------------------------------------------------------------------
# INTERFACE BLENDER
# ----------------------------------------------------------------------------

STATE_UI = {   # (texto, ícones por ordem de preferência - os nomes mudaram entre versões)
    "pending":      ("A verificar…", ("TIME",)),
    "offline":      ("Offline",      ("RADIOBUT_OFF", "X")),
    "livre":        ("Online",        ("STRIP_COLOR_04", "SEQUENCE_COLOR_04", "CHECKMARK")),
    "a_renderizar": ("A renderizar", ("STRIP_COLOR_05", "SEQUENCE_COLOR_05", "RENDER_ANIMATION", "TIME")),
    "parado":       ("Online · desligado da farm",    ("STRIP_COLOR_03", "SEQUENCE_COLOR_03", "PAUSE")),
    "falha":        ("Falha",        ("STRIP_COLOR_01", "SEQUENCE_COLOR_01", "ERROR")),
}
TILE_UI = {
    "pendente":     ("Pendente",     ("TIME",)),
    "a_renderizar": ("A renderizar", ("STRIP_COLOR_05", "SEQUENCE_COLOR_05", "RENDER_ANIMATION", "TIME")),
    "concluido":    ("Concluído",    ("CHECKMARK",)),
}

if bpy is not None:

    _ICON_SET = None

    def I(*names):
        """Devolve o primeiro ícone que existe nesta versão do Blender (evita erros de desenho)."""
        global _ICON_SET
        if _ICON_SET is None:
            try:
                _ICON_SET = set(bpy.types.UILayout.bl_rna.functions["label"]
                                .parameters["icon"].enum_items.keys())
            except Exception:
                _ICON_SET = set()
        for n in names:
            if n in _ICON_SET:
                return n
        return 'NONE'

    def _bar(layout, factor, text):
        if hasattr(layout, "progress"):
            layout.progress(factor=factor, type='BAR', text=text)
        else:
            layout.label(text=f"{text}  {int(factor * 100)}%")

    def _async(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    # -- operadores -----------------------------------------------------------
    class SORO_OT_add_slave(bpy.types.Operator):
        bl_idname = "soro.add_slave"
        bl_label = "Adicionar Slave"
        bl_description = "Adiciona o computador com este IP à farm"

        def execute(self, context):
            ip = context.window_manager.soro_new_ip.strip()
            try:
                ip = str(ipaddress.IPv4Address(ip))
            except Exception:
                self.report({'ERROR'}, "IP inválido")
                return {'CANCELLED'}
            if not MASTER.add(ip):
                self.report({'INFO'}, "Esse IP já está na lista")
            return {'FINISHED'}

    class SORO_OT_remove_slave(bpy.types.Operator):
        bl_idname = "soro.remove_slave"
        bl_label = "Remover da lista"
        ip: bpy.props.StringProperty()

        def execute(self, context):
            MASTER.remove(self.ip)
            return {'FINISHED'}

    class SORO_OT_slave_power(bpy.types.Operator):
        bl_idname = "soro.slave_power"
        bl_label = "Ligar / Desligar Slave"
        bl_description = "Liga ou desliga este Slave da farm (o programa continua a correr)"
        ip: bpy.props.StringProperty()

        def execute(self, context):
            parado = MASTER.live.get(self.ip, {}).get("state") == "parado"
            _async(MASTER.control, self.ip, "ativar" if parado else "pausar")
            return {'FINISHED'}

    class SORO_OT_slave_restart(bpy.types.Operator):
        bl_idname = "soro.slave_restart"
        bl_label = "Reiniciar Slave"
        bl_description = "Reinicia o programa Worker neste computador"
        ip: bpy.props.StringProperty()

        def execute(self, context):
            _async(MASTER.control, self.ip, "reiniciar")
            return {'FINISHED'}

    class SORO_OT_render(bpy.types.Operator):
        bl_idname = "soro.render"
        bl_label = "Render distribuído"
        bl_description = "Divide a imagem em pedaços e distribui pelos Slaves livres"

        def execute(self, context):
            sc = context.scene
            old = MASTER.job
            if old and old.state in ("a_renderizar", "compor"):
                self.report({'ERROR'}, "Já existe um render em curso")
                return {'CANCELLED'}
            folder = bpy.path.abspath(sc.soro_folder).strip()
            if not folder:
                self.report({'ERROR'}, "Escolha a pasta partilhada")
                return {'CANCELLED'}
            if not any(v.get("state") == "livre" for v in MASTER.live.values()):
                self.report({'ERROR'}, "Nenhum Slave livre")
                return {'CANCELLED'}
            job_id = time.strftime("%Y%m%d_%H%M%S")
            jf = os.path.join(folder, "SoroFarm", job_id)
            try:
                os.makedirs(jf, exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Não consegui criar a pasta: {e}")
                return {'CANCELLED'}
            unc = to_unc(jf)
            if not unc:
                self.report({'ERROR'}, "Essa pasta é local. Use uma pasta de rede (unidade mapeada ou \\\\PC\\pasta)")
                return {'CANCELLED'}
            bpy.ops.wm.save_as_mainfile(filepath=os.path.join(jf, "scene.blend"), copy=True)
            r = sc.render
            W = r.resolution_x * r.resolution_percentage // 100
            H = r.resolution_y * r.resolution_percentage // 100
            job = Job(job_id, jf, unc, W, H, make_tiles(W, H, sc.soro_tiles),
                      sc.soro_device, sc.frame_current, sc.name)
            MASTER.start_job(job)
            return {'FINISHED'}

    class SORO_OT_cancel(bpy.types.Operator):
        bl_idname = "soro.cancel"
        bl_label = "Cancelar render"

        def execute(self, context):
            if MASTER.job:
                MASTER.job.cancel = True
            return {'FINISHED'}

    # -- composição final (no Master, só junta os pedaços) --------------------
    def _compose(job):
        import numpy as np
        W, H = job.W, job.H
        out = np.zeros((H, W, 4), dtype=np.float32)
        for t in job.tiles:
            img = bpy.data.images.load(t.out_local)
            w, h = img.size
            arr = np.empty(w * h * 4, dtype=np.float32)
            img.pixels.foreach_get(arr)
            arr = arr.reshape(h, w, 4)
            x0, x1, y0, y1 = t.rect
            ww, hh = min(w, W - x0), min(h, H - y0)
            out[y0:y0 + hh, x0:x0 + ww] = arr[:hh, :ww]
            bpy.data.images.remove(img)
        name = "SoroFarm Resultado"
        old = bpy.data.images.get(name)
        if old:
            bpy.data.images.remove(old)
        res = bpy.data.images.new(name, W, H, alpha=True, float_buffer=True)
        res.pixels.foreach_set(out.ravel())
        exr = os.path.join(job.folder, "final.exr")
        png = os.path.join(job.folder, "final.png")
        res.filepath_raw = exr
        res.file_format = 'OPEN_EXR'
        res.save()
        scene = bpy.data.scenes.get(job.scene_name) or bpy.context.scene
        fmt = scene.render.image_settings.file_format
        try:
            scene.render.image_settings.file_format = 'PNG'
            res.save_render(png, scene=scene)
        finally:
            scene.render.image_settings.file_format = fmt
        job.final = png
        _show_image(res)

    def _show_image(img):
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.spaces.active.image = img

    def _denoise(job):
        """Denoise final (OpenImageDenoise, nó Denoise do Compositing) sobre o final.exr."""
        exr = os.path.join(job.folder, "final.exr")
        if not os.path.exists(exr):
            raise RuntimeError("final.exr não encontrado")
        base = os.path.join(job.folder, "final_denoise")
        scene0 = bpy.data.scenes.get(job.scene_name) or bpy.context.scene
        sc = bpy.data.scenes.new("SoroDenoiseTmp")
        img = bpy.data.images.load(exr)
        group = None
        try:
            sc.render.engine = 'BLENDER_WORKBENCH'
            sc.render.resolution_x, sc.render.resolution_y = job.W, job.H
            sc.render.resolution_percentage = 100
            for attr in ("view_transform", "look", "exposure", "gamma"):
                try:
                    setattr(sc.view_settings, attr, getattr(scene0.view_settings, attr))
                except Exception:
                    pass
            try:
                sc.display_settings.display_device = scene0.display_settings.display_device
            except Exception:
                pass
            if hasattr(sc, "compositing_node_group"):          # Blender 5+
                group = bpy.data.node_groups.new("SoroDenoise", "CompositorNodeTree")
                group.interface.new_socket(name="Image", in_out='OUTPUT', socket_type='NodeSocketColor')
                sc.compositing_node_group = group
                tree, n_out = group, group.nodes.new("NodeGroupOutput")
                out_in = n_out.inputs[0]
            else:                                              # Blender 3.6 - 4.x
                sc.use_nodes = True
                tree = sc.node_tree
                tree.nodes.clear()
                n_out = tree.nodes.new("CompositorNodeComposite")
                out_in = n_out.inputs["Image"]
            n_img = tree.nodes.new("CompositorNodeImage")
            n_img.image = img
            n_dn = tree.nodes.new("CompositorNodeDenoise")
            try:
                n_dn.use_hdr = True                            # 3.6 - 4.x
            except Exception:
                pass
            if "HDR" in n_dn.inputs:                           # 5.x (passou a socket)
                try:
                    n_dn.inputs["HDR"].default_value = True
                except Exception:
                    pass
            tree.links.new(n_img.outputs["Image"], n_dn.inputs["Image"])
            tree.links.new(n_dn.outputs["Image"], out_in)
            try:
                sc.render.use_compositing = True
            except Exception:
                pass
            sc.render.image_settings.file_format = 'PNG'
            sc.render.image_settings.color_mode = 'RGB'
            sc.render.use_file_extension = True
            sc.render.filepath = base
            bpy.ops.render.render(write_still=True, scene=sc.name)
        finally:
            bpy.data.scenes.remove(sc)
            bpy.data.images.remove(img)
            if group:
                bpy.data.node_groups.remove(group)
        out = base + ".png"
        if not os.path.exists(out):
            raise RuntimeError("o Blender não gerou a imagem")
        name = "SoroFarm Denoise"
        old = bpy.data.images.get(name)
        if old:
            bpy.data.images.remove(old)
        res = bpy.data.images.load(out)
        res.name = name
        _show_image(res)
        job.denoised = out

    class SORO_OT_denoise(bpy.types.Operator):
        bl_idname = "soro.denoise"
        bl_label = "Denoise"
        bl_description = "Remove o ruído da imagem final (corre neste PC, demora alguns segundos)"

        def execute(self, context):
            j = MASTER.job
            if not j or j.state != "terminado":
                self.report({'ERROR'}, "Não há render terminado")
                return {'CANCELLED'}
            try:
                _denoise(j)
                j.denoise_msg = ""
            except Exception as e:
                j.denoise_msg = f"Denoise falhou: {e}"
                self.report({'ERROR'}, j.denoise_msg)
                return {'CANCELLED'}
            return {'FINISHED'}

    _sig = [None]

    def _signature():
        with MASTER.lock:
            ips = list(MASTER.slaves)
        parts = []
        for ip in ips:
            l = MASTER.live.get(ip, {})
            t = l.get("task") or {}
            parts.append((ip, l.get("state"), int(t.get("progress", 0.0) * 50)))
        j = MASTER.job
        if j:
            parts.append((j.state, int(j.progress() * 100), j.msg, j.denoised, j.denoise_msg,
                          tuple(t.state for t in j.tiles)))
        return tuple(parts)

    def _tick():
        try:
            j = MASTER.job
            if j and j.state == "compor" and not j.composing:
                j.composing = True
                try:
                    _compose(j)
                    j.state = "terminado"
                except Exception as e:
                    j.state, j.msg = "falhou", f"Erro ao juntar pedaços: {e}"
            sig = _signature()
            if sig != _sig[0]:                 # só redesenha quando algo mudou
                _sig[0] = sig
                for w in bpy.context.window_manager.windows:
                    for a in w.screen.areas:
                        if a.type == 'VIEW_3D':
                            for r in a.regions:
                                if r.type == 'UI':
                                    r.tag_redraw()   # só a barra lateral, não a vista 3D
        except Exception:
            pass
        return 1.0

    # -- painéis ----------------------------------------------------------------
    class SORO_PT_farm(bpy.types.Panel):
        bl_label = "Slaves"
        bl_idname = "SORO_PT_farm"
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = "SoroFarm"

        def draw(self, context):
            layout = self.layout
            with MASTER.lock:
                slaves = list(MASTER.slaves.values())
            states = [MASTER.live.get(s["ip"], {}).get("state", "pending") for s in slaves]
            offline = states.count("offline")
            online = sum(1 for x in states if x not in ("offline", "pending"))
            rendering = states.count("a_renderizar")
            falhas = states.count("falha")
            txt = f"{online} online · {offline} offline"
            if rendering:
                txt += f" · {rendering} a renderizar"
            if falhas:
                txt += f" · {falhas} com falha"
            layout.label(text=txt, icon=I('NETWORK_DRIVE'))

            row = layout.row(align=True)
            row.prop(context.window_manager, "soro_new_ip", text="")
            row.operator("soro.add_slave", text="Adicionar", icon=I('ADD'))


            for s in slaves:
                ip = s["ip"]
                live = MASTER.live.get(ip, {"state": "pending"})
                st = live.get("state", "offline")
                label, icons = STATE_UI.get(st, (st, ('QUESTION',)))
                icon = I(*icons)
                box = layout.box()
                row = box.row(align=True)
                row.label(text=s["name"], icon=icon)
                sub = row.row(align=True)
                sub.enabled = st not in ("offline", "pending")
                op = sub.operator("soro.slave_power", text="",
                                  icon=I('PLAY' if st == "parado" else 'PAUSE'))
                op.ip = ip
                op = sub.operator("soro.slave_restart", text="", icon=I('FILE_REFRESH'))
                op.ip = ip
                op = row.operator("soro.remove_slave", text="", icon=I('X'))
                op.ip = ip
                info = f"{label} · {ip}"
                if "ms" in live:
                    info += f" · {live['ms']} ms"
                box.label(text=info)
                task = live.get("task")
                if st == "a_renderizar" and task:
                    _bar(box, float(task.get("progress", 0.0)), f"Pedaço {task.get('tile', 0) + 1}")
                if st == "falha" and live.get("error"):
                    box.label(text=live["error"], icon=I('ERROR'))

    class SORO_PT_job(bpy.types.Panel):
        bl_label = "Render"
        bl_idname = "SORO_PT_job"
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = "SoroFarm"

        def draw(self, context):
            layout = self.layout
            sc = context.scene
            layout.prop(sc, "soro_folder", text="")
            row = layout.row(align=True)
            row.prop(sc, "soro_tiles", text="Pedaços")
            row.prop(sc, "soro_device", text="")
            j = MASTER.job
            if j and j.state in ("a_renderizar", "compor"):
                layout.operator("soro.cancel", text="Cancelar", icon=I('CANCEL'))
            else:
                layout.operator("soro.render", text="Render distribuído", icon=I('RENDER_STILL'))
            if not j:
                return
            done = sum(1 for t in j.tiles if t.state == "concluido")
            labels = {"a_renderizar": "A renderizar", "compor": "A juntar pedaços…",
                      "terminado": "Concluído", "falhou": "Falhou", "cancelado": "Cancelado"}
            _bar(layout, j.progress(), f"{labels.get(j.state, j.state)} · {done}/{len(j.tiles)}")
            if j.msg:
                layout.label(text=j.msg, icon=I('INFO'))
            if j.state == "terminado":
                layout.label(text="Terminado!", icon=I('CHECKMARK'))
                layout.label(text=j.final)
                layout.operator("soro.denoise", text="Denoise", icon=I('SHADERFX'))
                if j.denoised:
                    layout.label(text="Denoise: " + j.denoised)
                if j.denoise_msg:
                    layout.label(text=j.denoise_msg, icon=I('ERROR'))
            box = layout.box()
            for t in j.tiles:
                label, icons = TILE_UI[t.state]
                icon = I(*icons)
                txt = f"{t.id + 1}"
                if t.state == "a_renderizar":
                    txt += f" · {MASTER.name(t.slave)} · {int(t.progress * 100)}%"
                elif t.state == "concluido":
                    txt += " · concluído"
                else:
                    txt += " · pendente" + (f" (tentativa {t.attempts + 1})" if t.attempts else "")
                box.label(text=txt, icon=icon)

    _CLASSES = (SORO_OT_add_slave, SORO_OT_remove_slave, SORO_OT_slave_power,
                SORO_OT_slave_restart, SORO_OT_render, SORO_OT_cancel, SORO_OT_denoise,
                SORO_PT_farm, SORO_PT_job)

    def register():
        global MASTER
        MASTER = Master()
        bpy.types.WindowManager.soro_new_ip = bpy.props.StringProperty(default="192.168.1.")
        bpy.types.Scene.soro_folder = bpy.props.StringProperty(
            name="Pasta partilhada", subtype='DIR_PATH',
            description="Pasta de rede acessível por todos os PCs (ex.: K:\\Render ou \\\\PC\\Render)")
        bpy.types.Scene.soro_tiles = bpy.props.IntProperty(
            name="Pedaços", default=6, min=1, max=64,
            description="Em quantos pedaços dividir a imagem")
        bpy.types.Scene.soro_device = bpy.props.EnumProperty(
            name="Dispositivo", items=[("CPU", "CPU", ""), ("GPU", "GPU", "")], default="CPU")
        for c in _CLASSES:
            bpy.utils.register_class(c)
        MASTER.start()
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, persistent=True)

    def unregister():
        if MASTER:
            MASTER.stop()
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
        for c in reversed(_CLASSES):
            bpy.utils.unregister_class(c)
        del bpy.types.WindowManager.soro_new_ip
        del bpy.types.Scene.soro_folder
        del bpy.types.Scene.soro_tiles
        del bpy.types.Scene.soro_device


if __name__ == "__main__":
    if bpy is None:
        worker_cli()
    else:
        register()
