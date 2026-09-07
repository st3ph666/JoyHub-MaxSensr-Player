#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
import subprocess
import threading
import tempfile
import urllib.request
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = BleakScanner = None

APP_VERSION = "v1.4.13-FunscriptLoaderFix"
APP_NAME = f"JoyHub MaxSensr Funscript Player {APP_VERSION}"

VIDEO_DIR = Path("/media/Video2/ABCD/")
SCRIPT_DIR = VIDEO_DIR / "Funscript"
CONFIG = Path.home() / ".config/maxsensr-player-gui.json"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}

DEVICE_NAME = "J-MaxSensr"
WRITE_UUID = "0000ffa1-0000-1000-8000-00805f9b34fb"

# Valeurs validées lors du test BLE direct.
MOTOR_MIN = 0x01
MOTOR_MAX = 0xE8
CON_MAX = 0x05

PROFILES = {
    "Doux": dict(osc_min=8, osc_max=48, vib_min=18, vib_max=68, con_level=3, con_threshold=82),
    "Normal": dict(osc_min=10, osc_max=68, vib_min=30, vib_max=100, con_level=5, con_threshold=58),
    "Fort": dict(osc_min=16, osc_max=82, vib_min=42, vib_max=100, con_level=5, con_threshold=50),
    "Osc dominant": dict(osc_min=14, osc_max=88, vib_min=20, vib_max=72, con_level=4, con_threshold=68),
    "Vib dominant": dict(osc_min=8, osc_max=52, vib_min=48, vib_max=100, con_level=5, con_threshold=60),
    "Con accentué": dict(osc_min=8, osc_max=58, vib_min=30, vib_max=92, con_level=5, con_threshold=45),
}

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def scale_motor(percent):
    p = clamp(float(percent), 0, 100)
    if p <= 0:
        return 0
    return int(round(MOTOR_MIN + (MOTOR_MAX - MOTOR_MIN) * p / 100.0))

def motion_packet(vib=0, osc=0):
    return bytes([0xA0, 0x03, vib & 0xff, osc & 0xff, 0, 0, 0xAA])

def con_packet(level=0):
    if level <= 0:
        return bytes.fromhex("A0 07 00 00 00 FF")
    return bytes([0xA0, 0x07, 0x01, 0x00, int(clamp(level, 1, CON_MAX)), 0xFF])

def natural_key(path):
    import re
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", path.name)]

def find_script(video):
    candidates = [
        SCRIPT_DIR / f"{video.stem}.funscript",
        video.with_suffix(".funscript"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None

def load_actions(path):
    """Charge un funscript standard ou plusieurs blocs JSON concaténés.

    Certains générateurs/exporteurs écrivent plusieurs objets JSON à la suite
    (souvent un objet par ligne). json.loads() lève alors ``Extra data``.
    On accepte ici les deux formats sans modifier les funscripts standards.
    """
    text = path.read_text(encoding="utf-8-sig")
    decoder = json.JSONDecoder()
    values = []
    i = 0
    n = len(text)

    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        value, end = decoder.raw_decode(text, i)
        values.append(value)
        i = end

    if not values:
        return []

    raw_actions = []
    for value in values:
        if isinstance(value, dict):
            acts = value.get("actions")
            if isinstance(acts, list):
                raw_actions.extend(acts)
            elif "at" in value and "pos" in value:
                raw_actions.append(value)
        elif isinstance(value, list):
            raw_actions.extend(a for a in value if isinstance(a, dict))

    actions = []
    for a in raw_actions:
        if not isinstance(a, dict) or "at" not in a or "pos" not in a:
            continue
        try:
            at = int(float(a["at"]))
            pos = clamp(float(a["pos"]), 0, 100)
        except (TypeError, ValueError):
            continue
        actions.append((at, pos))

    actions.sort(key=lambda x: x[0])
    return actions

class BLEWorker:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.loop = None
        self.stop_evt = threading.Event()
        self.settings = {}
        self.video = None
        self.script = None
        self.process = None
        self.requested_seek_ms = None
        self.last_tms = 0.0
        self.duration_ms = 0.0
        self.end_callback_sent = False
        self.seek_lock = threading.Lock()
        self.ipc_path = None

    def request_seek(self, target_ms):
        """Demande un seek absolu. Le thread BLE l'applique à MPV."""
        with self.seek_lock:
            self.requested_seek_ms = max(0, int(target_ms))

    def _take_seek(self):
        with self.seek_lock:
            value = self.requested_seek_ms
            self.requested_seek_ms = None
        return value

    def start(self, video, script, settings):
        self.stop()
        self.stop_evt.clear()
        self.end_callback_sent = False
        self.last_tms = 0.0
        self.duration_ms = 0.0
        self.video, self.script, self.settings = video, script, settings
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.process = None

    def _notify_natural_end_once(self):
        if self.end_callback_sent or self.stop_evt.is_set():
            return
        self.end_callback_sent = True
        self.app.after(0, self.app.on_natural_end)

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as e:
            # Un périphérique BLE peut parfois se déconnecter exactement à la fin.
            # Si MPV était déjà dans les 2 dernières secondes, on traite quand même
            # cela comme une fin naturelle et on passe à la vidéo suivante.
            near_end = (
                self.duration_ms > 0
                and self.last_tms >= max(0.0, self.duration_ms - 2000.0)
            )
            if near_end and not self.stop_evt.is_set():
                self._notify_natural_end_once()
            else:
                self.app.after(0, lambda e=e: self.app.set_status(f"Erreur: {e}", "danger"))

    async def _write(self, client, data):
        async def do_write():
            await client.write_gatt_char(WRITE_UUID, data, response=False)

        try:
            await asyncio.wait_for(do_write(), timeout=1.0)
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout BLE: le MaxSensr ne répond plus.")
        except Exception:
            await asyncio.sleep(0.08)
            try:
                await asyncio.wait_for(do_write(), timeout=1.0)
            except asyncio.TimeoutError:
                raise RuntimeError("Timeout BLE après nouvelle tentative.")

    async def _stop_all(self, client):
        try:
            # Toujours couper Vib/Osc d'abord, puis le solénoïde/Con.
            await self._write(client, motion_packet(0, 0))
            await asyncio.sleep(.10)
            await self._write(client, con_packet(0))
            await asyncio.sleep(.10)
        except Exception:
            pass

    async def _find(self):
        return await BleakScanner.find_device_by_filter(
            lambda d, ad: (ad.local_name or d.name or "") == DEVICE_NAME,
            timeout=10.0
        )

    async def _run(self):
        if BleakScanner is None:
            raise RuntimeError(
                "Bleak absent même dans ~/venv-maxsensr. "
                "Installe-le avec : ~/venv-maxsensr/bin/pip install bleak"
            )

        actions = load_actions(self.script)
        self.duration_ms = float(actions[-1][0]) if actions else 0.0
        if len(actions) < 2:
            raise RuntimeError("Funscript vide ou insuffisant.")

        # ---------------------------------------------------------
        # MPV démarre UNE SEULE FOIS et reste indépendant des
        # reconnexions Bluetooth.
        # ---------------------------------------------------------
        self.ipc_path = str(
            Path(tempfile.gettempdir())
            / f"maxsensr-mpv-{os.getpid()}-{int(time.time()*1000)}.sock"
        )
        try:
            Path(self.ipc_path).unlink(missing_ok=True)
        except Exception:
            pass

        cmd = [
            "mpv", str(self.video), "--force-window=yes",
            "--screen=1", "--fs-screen=1",
            f"--input-ipc-server={self.ipc_path}",
            "--keep-open=no",
        ]
        if self.settings["fullscreen"]:
            cmd.append("--fs")

        self.process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        for _ in range(120):
            if self.stop_evt.is_set():
                break
            if Path(self.ipc_path).exists():
                break
            await asyncio.sleep(.03)

        if not Path(self.ipc_path).exists():
            raise RuntimeError("MPV n'a pas créé son canal IPC.")

        reader, writer = await asyncio.open_unix_connection(self.ipc_path)

        async def mpv_cmd(command):
            payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
            writer.write(payload)
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=1.5)
                if not line:
                    raise RuntimeError("Connexion IPC MPV fermée.")
                try:
                    reply = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if "error" in reply:
                    return reply

        async def mpv_time_ms():
            reply = await mpv_cmd(["get_property", "time-pos"])
            value = reply.get("data")
            if value is None:
                return 0.0
            return max(0.0, float(value) * 1000.0)

        ended_naturally = False
        reconnect_count = 0

        try:
            while (
                not self.stop_evt.is_set()
                and self.process is not None
                and self.process.poll() is None
            ):
                # -------------------------------------------------
                # Connexion / reconnexion MaxSensr
                # -------------------------------------------------
                self.app.after(
                    0,
                    lambda n=reconnect_count: self.app.set_status(
                        "Recherche du J-MaxSensr…"
                        if n == 0
                        else "Reconnexion du J-MaxSensr après seek…",
                        "warning",
                    ),
                )

                dev = await self._find()
                if not dev:
                    raise RuntimeError("J-MaxSensr introuvable.")

                reconnect_for_seek = False

                async with BleakClient(dev, timeout=20) as client:
                    self.app.after(
                        0,
                        lambda n=reconnect_count: self.app.set_status(
                            "MaxSensr connecté — lecture."
                            if n == 0
                            else "MaxSensr reconnecté — reprise synchronisée.",
                            "success",
                        ),
                    )

                    # Après une reconnexion, aucun actionneur ne doit repartir
                    # immédiatement avec un ancien état.
                    await self._stop_all(client)
                    await asyncio.sleep(0.35)

                    idx = 0
                    last_send = 0.0
                    last_ui_update = 0.0
                    last_loop_time = time.monotonic()
                    last_mpv_poll = 0.0
                    cached_tms = await mpv_time_ms()
                    previous_mpv_tms = cached_tms
                    self.last_tms = cached_tms

                    con_until = 0.0
                    con_active = False
                    con_rearm_at = time.monotonic() + 1.0
                    vib_until = 0.0
                    vib_rearm_at = time.monotonic() + 1.0
                    vib_burst_active = False
                    smooth_vib = 0.0
                    smooth_osc = 0.0

                    while (
                        not self.stop_evt.is_set()
                        and self.process is not None
                        and self.process.poll() is None
                    ):
                        now = time.monotonic()
                        dt = clamp(now - last_loop_time, 0.001, 0.100)
                        last_loop_time = now

                        # -----------------------------------------
                        # Seek depuis le graphique du player.
                        # -----------------------------------------
                        requested = self._take_seek()
                        if requested is not None:
                            # STOP physique avant le seek.
                            await self._stop_all(client)
                            await mpv_cmd(
                                ["set_property", "time-pos", requested / 1000.0]
                            )

                            self.last_tms = float(requested)
                            reconnect_for_seek = True
                            self.app.after(
                                0,
                                lambda: self.app.set_status(
                                    "Seek — Bluetooth suspendu puis reconnecté…",
                                    "warning",
                                ),
                            )
                            break

                        # MPV horloge maître, polling ~20 Hz.
                        if now - last_mpv_poll >= 0.050:
                            try:
                                cached_tms = await mpv_time_ms()
                                last_mpv_poll = now
                            except (
                                asyncio.TimeoutError,
                                BrokenPipeError,
                                ConnectionResetError,
                                asyncio.IncompleteReadError,
                            ):
                                if self.process.poll() is not None:
                                    break
                                await asyncio.sleep(.05)
                                continue
                        else:
                            cached_tms += dt * 1000.0

                        tms = cached_tms
                        self.last_tms = tms

                        # -----------------------------------------
                        # Seek effectué directement dans MPV.
                        # Si la position saute de > 1.2 s, on arrête
                        # totalement les moteurs puis on FERME la
                        # connexion BLE. La boucle externe reconnecte.
                        # -----------------------------------------
                        if (
                            previous_mpv_tms is not None
                            and abs(tms - previous_mpv_tms) > 1200.0
                        ):
                            await self._stop_all(client)
                            reconnect_for_seek = True
                            self.app.after(
                                0,
                                lambda: self.app.set_status(
                                    "Seek MPV détecté — reconnexion Bluetooth…",
                                    "warning",
                                ),
                            )
                            break

                        previous_mpv_tms = tms

                        # Position funscript
                        if idx >= len(actions) or actions[idx][0] > tms:
                            idx = 0
                        while (
                            idx + 1 < len(actions)
                            and actions[idx + 1][0] <= tms
                        ):
                            idx += 1

                        if idx + 1 >= len(actions):
                            pos = actions[-1][1]
                            speed = 0.0
                        else:
                            t0, p0 = actions[idx]
                            t1, p1 = actions[idx + 1]
                            span = max(1, t1 - t0)
                            f = clamp((tms - t0) / span, 0, 1)
                            pos = p0 + (p1 - p0) * f
                            speed = clamp(
                                abs(p1 - p0) / span * 250.0, 0, 100
                            )

                        s = self.settings

                        # OSC
                        osc_pct = 0.0
                        if s["osc_enabled"]:
                            osc_pct = (
                                s["osc_min"]
                                + (s["osc_max"] - s["osc_min"])
                                * pos / 100.0
                            )
                            osc_pct = clamp(
                                osc_pct * s["osc_amp"] / 100.0,
                                0, 100
                            )

                        # VIB
                        vib_pct = 0.0
                        if s["vib_enabled"]:
                            target_vib = (
                                s["vib_min"]
                                + (s["vib_max"] - s["vib_min"])
                                * speed / 100.0
                            )
                            target_vib = clamp(
                                target_vib * s["vib_amp"] / 100.0,
                                0, 100
                            )
                            target_vib = clamp(
                                100.0 * ((target_vib / 100.0) ** 0.68),
                                0, 100
                            )
                            target_vib *= pattern_gate(
                                s.get("vib_pattern", "Funscript direct"),
                                tms / 1000.0,
                                s.get("vib_duration_ms", 900),
                            )

                            if (
                                target_vib > 1.0
                                and not vib_burst_active
                                and now >= vib_rearm_at
                            ):
                                vib_burst_active = True
                                vib_until = (
                                    now
                                    + float(
                                        s.get("vib_duration_ms", 900)
                                    ) / 1000.0
                                )

                            if vib_burst_active and now >= vib_until:
                                vib_burst_active = False
                                vib_rearm_at = now + 0.30

                            vib_pct = (
                                min(target_vib, 92.0)
                                if vib_burst_active
                                else 0.0
                            )

                        # Progressions douces
                        osc_step = (
                            100.0 * (dt * 1000.0)
                            / max(
                                100.0,
                                float(
                                    s.get("osc_transition_ms", 2500)
                                ),
                            )
                        )
                        vib_step = (
                            100.0 * (dt * 1000.0)
                            / max(
                                100.0,
                                float(
                                    s.get("vib_transition_ms", 700)
                                ),
                            )
                        )

                        smooth_osc += clamp(
                            osc_pct - smooth_osc,
                            -osc_step, osc_step
                        )
                        smooth_vib += clamp(
                            vib_pct - smooth_vib,
                            -vib_step, vib_step
                        )

                        vib_alpha = clamp(
                            1.0 - s["smoothing"] / 100.0,
                            .05, 1.0
                        )
                        smooth_vib = (
                            smooth_vib * vib_alpha
                            + vib_pct * (1.0 - vib_alpha)
                        )

                        # -------------------------------------------------
                        # OSC ultra-lent par impulsions.
                        # Le contrôleur interne du MaxSensr reste trop rapide
                        # même à faible consigne. On garde donc la consigne
                        # funscript/lissée, mais on coupe périodiquement l'OSC.
                        # Avec 150 ms ON / 350 ms OFF, le moteur n'est alimenté
                        # qu'environ 30 % du temps. Les commandes BLE restent
                        # limitées à ~10 Hz, donc le réglage réel est quantifié
                        # par pas d'environ 100 ms.
                        # -------------------------------------------------
                        osc_output = smooth_osc
                        if s.get("osc_pulse_enabled", False) and smooth_osc > 0.5:
                            on_ms = max(100.0, float(s.get("osc_pulse_on_ms", 150)))
                            off_ms = max(100.0, float(s.get("osc_pulse_off_ms", 350)))
                            force_pct = clamp(float(s.get("osc_pulse_force", 60)), 10.0, 100.0)
                            cycle_s = (on_ms + off_ms) / 1000.0
                            phase_s = now % cycle_s
                            if phase_s < on_ms / 1000.0:
                                # La force est indépendante de ON/OFF : elle limite
                                # seulement la puissance envoyée pendant l'impulsion.
                                osc_output = clamp(smooth_osc * force_pct / 100.0, 0.0, 100.0)
                            else:
                                osc_output = 0.0

                        # CON / pompage
                        if (
                            s["con_enabled"]
                            and pos >= s["con_threshold"]
                            and pattern_gate(
                                s.get(
                                    "con_pattern",
                                    "Funscript direct",
                                ),
                                tms / 1000.0,
                                s.get("con_duration", 550),
                            ) >= .50
                            and not con_active
                            and now >= con_rearm_at
                        ):
                            await self._write(
                                client,
                                con_packet(s["con_level"])
                            )
                            con_active = True
                            con_until = (
                                now
                                + min(
                                    float(s["con_duration"]),
                                    700.0
                                ) / 1000.0
                            )

                        if con_active and now >= con_until:
                            await self._write(client, con_packet(0))
                            con_active = False
                            con_rearm_at = now + .50

                        # Commandes Vib/Osc limitées à ~10 Hz.
                        if now - last_send >= .100:
                            await self._write(
                                client,
                                motion_packet(
                                    scale_motor(smooth_vib),
                                    scale_motor(osc_output),
                                ),
                            )
                            last_send = now

                        # UI ~10 Hz.
                        if now - last_ui_update >= .100:
                            last_ui_update = now
                            duration_ms = max(1, actions[-1][0])
                            timeline_pct = clamp(
                                tms / duration_ms * 100.0,
                                0, 100
                            )
                            self.app.after(
                                0,
                                lambda pct=timeline_pct,
                                       p=pos,
                                       v=smooth_vib,
                                       o=osc_output,
                                       c=con_active:
                                    self.app.update_progress(
                                        pct, p, v, o, c
                                    ),
                            )

                        await asyncio.sleep(.01)

                    # Toujours stopper avant de laisser fermer
                    # la connexion Bluetooth.
                    await self._stop_all(client)

                # Ici l'async-with a réellement fermé le BLE.
                if reconnect_for_seek and not self.stop_evt.is_set():
                    reconnect_count += 1

                    # Laisser le périphérique/BlueZ respirer avant
                    # une nouvelle connexion.
                    await asyncio.sleep(1.5)

                    # MPV continue pendant la reconnexion.
                    continue

                break

            duration_ms = max(1, actions[-1][0])
            ended_naturally = (
                self.last_tms >= max(0, duration_ms - 2000)
            )

        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

            if self.process and self.process.poll() is None:
                self.process.terminate()
            self.process = None

            if self.ipc_path:
                try:
                    Path(self.ipc_path).unlink(missing_ok=True)
                except Exception:
                    pass

            if ended_naturally and not self.stop_evt.is_set():
                self._notify_natural_end_once()
            elif not self.stop_evt.is_set():
                self.app.after(
                    0,
                    lambda: self.app.set_status(
                        "Lecture arrêtée sans fin naturelle.",
                        "warning",
                    ),
                )

    async def test_motor(self, which, value, settings=None):
        dev = await self._find()
        if not dev:
            raise RuntimeError("J-MaxSensr introuvable.")
        async with BleakClient(dev, timeout=20) as c:
            await self._stop_all(c)
            if which == "vib":
                await self._write(c, motion_packet(scale_motor(value), 0))
                await asyncio.sleep(2)
            elif which == "osc":
                st = settings or {}
                if st.get("osc_pulse_enabled", False):
                    on_s = max(0.10, float(st.get("osc_pulse_on_ms", 150)) / 1000.0)
                    off_s = max(0.10, float(st.get("osc_pulse_off_ms", 350)) / 1000.0)
                    force_pct = clamp(float(st.get("osc_pulse_force", 60)), 10.0, 100.0)
                    pulse_value = clamp(float(value) * force_pct / 100.0, 0.0, 100.0)
                    end_at = time.monotonic() + 2.0
                    while time.monotonic() < end_at:
                        await self._write(c, motion_packet(0, scale_motor(pulse_value)))
                        await asyncio.sleep(min(on_s, max(0.0, end_at - time.monotonic())))
                        await self._write(c, motion_packet(0, 0))
                        if time.monotonic() < end_at:
                            await asyncio.sleep(min(off_s, max(0.0, end_at - time.monotonic())))
                else:
                    await self._write(c, motion_packet(0, scale_motor(value)))
                    await asyncio.sleep(2)
            else:
                await self._write(c, con_packet(value))
                await asyncio.sleep(2)
            await self._stop_all(c)



VIB_PATTERNS = [
    "Funscript direct", "Continu", "Pulsation lente", "Pulsation moyenne",
    "Pulsation rapide", "Double pulse", "Triple pulse", "Battement",
    "Battement rapide", "Montée progressive", "Descente progressive",
    "Vague lente", "Vague rapide", "Escalier montant", "Escalier descendant",
    "Alternance faible/fort", "Burst court", "Burst moyen", "Burst long",
    "3 courts + 1 long", "1 long + 3 courts", "Mitraillette", "Staccato",
    "Respiration", "Heartbeat", "Heartbeat rapide", "Rampe cyclique",
    "Triangle lent", "Triangle rapide", "Saw montant", "Saw descendant",
    "Random doux", "Random moyen", "Random fort", "Random extrême",
    "Micro-pulses", "Macro-pulses", "Pause courte", "Pause moyenne",
    "Pause longue", "Progressif 3 niveaux", "Progressif 5 niveaux",
    "100% intermittent", "50/100 alterné", "25/75/100", "Écho",
    "Double écho", "Vague double", "Ultra rapide", "Chaos contrôlé",
]

CON_PATTERNS = [
    "Funscript direct", "Pompage continu", "Pompage lent", "Pompage moyen",
    "Pompage rapide", "Pompe courte", "Pompe moyenne", "Pompe longue",
    "Double pompe", "Triple pompe", "Pompe + pause courte",
    "Pompe + pause moyenne", "Pompe + pause longue", "2 courtes + 1 longue",
    "1 longue + 2 courtes", "Progressif lent", "Progressif rapide",
    "Alternance court/long", "Burst x3", "Burst x5", "Respiration",
    "Battement", "Battement double", "Vague", "Escalier 3 niveaux",
    "Escalier 5 niveaux", "Random doux", "Random moyen", "Random rapide",
    "Rafale", "Rafale + pause", "Maintien court", "Maintien moyen",
    "Maintien long", "Micro-pompes", "Macro-pompes", "Sync pics",
    "Sync descentes", "Sync changements", "50/50", "25/75", "75/25",
    "Court-court-long", "Long-court-court", "Pause 1 s", "Pause 2 s",
    "Pause 3 s", "Ultra rapide", "Cycle profond", "Chaos contrôlé",
]


PROFILE_EN = {
    "Doux": "Gentle",
    "Normal": "Normal",
    "Fort": "Strong",
    "Osc dominant": "OSC dominant",
    "Vib dominant": "VIB dominant",
    "Con accentué": "CON enhanced",
}

VIB_PATTERN_EN = {
    "Funscript direct": "Direct funscript", "Continu": "Continuous",
    "Pulsation lente": "Slow pulse", "Pulsation moyenne": "Medium pulse",
    "Pulsation rapide": "Fast pulse", "Double pulse": "Double pulse",
    "Triple pulse": "Triple pulse", "Battement": "Beat", "Battement rapide": "Fast beat",
    "Montée progressive": "Progressive rise", "Descente progressive": "Progressive fall",
    "Vague lente": "Slow wave", "Vague rapide": "Fast wave", "Escalier montant": "Ascending steps",
    "Escalier descendant": "Descending steps", "Alternance faible/fort": "Low/high alternating",
    "Burst court": "Short burst", "Burst moyen": "Medium burst", "Burst long": "Long burst",
    "3 courts + 1 long": "3 short + 1 long", "1 long + 3 courts": "1 long + 3 short",
    "Mitraillette": "Machine gun", "Staccato": "Staccato", "Respiration": "Breathing",
    "Heartbeat": "Heartbeat", "Heartbeat rapide": "Fast heartbeat", "Rampe cyclique": "Cyclic ramp",
    "Triangle lent": "Slow triangle", "Triangle rapide": "Fast triangle", "Saw montant": "Rising saw",
    "Saw descendant": "Falling saw", "Random doux": "Gentle random", "Random moyen": "Medium random",
    "Random fort": "Strong random", "Random extrême": "Extreme random", "Micro-pulses": "Micro pulses",
    "Macro-pulses": "Macro pulses", "Pause courte": "Short pause", "Pause moyenne": "Medium pause",
    "Pause longue": "Long pause", "Progressif 3 niveaux": "3-level progressive",
    "Progressif 5 niveaux": "5-level progressive", "100% intermittent": "100% intermittent",
    "50/100 alterné": "50/100 alternating", "25/75/100": "25/75/100", "Écho": "Echo",
    "Double écho": "Double echo", "Vague double": "Double wave", "Ultra rapide": "Ultra fast",
    "Chaos contrôlé": "Controlled chaos",
}

CON_PATTERN_EN = {
    "Funscript direct": "Direct funscript", "Pompage continu": "Continuous pumping",
    "Pompage lent": "Slow pumping", "Pompage moyen": "Medium pumping", "Pompage rapide": "Fast pumping",
    "Pompe courte": "Short pump", "Pompe moyenne": "Medium pump", "Pompe longue": "Long pump",
    "Double pompe": "Double pump", "Triple pompe": "Triple pump", "Pompe + pause courte": "Pump + short pause",
    "Pompe + pause moyenne": "Pump + medium pause", "Pompe + pause longue": "Pump + long pause",
    "2 courtes + 1 longue": "2 short + 1 long", "1 longue + 2 courtes": "1 long + 2 short",
    "Progressif lent": "Slow progressive", "Progressif rapide": "Fast progressive",
    "Alternance court/long": "Short/long alternating", "Burst x3": "Burst x3", "Burst x5": "Burst x5",
    "Respiration": "Breathing", "Battement": "Beat", "Battement double": "Double beat", "Vague": "Wave",
    "Escalier 3 niveaux": "3-level steps", "Escalier 5 niveaux": "5-level steps",
    "Random doux": "Gentle random", "Random moyen": "Medium random", "Random rapide": "Fast random",
    "Rafale": "Burst", "Rafale + pause": "Burst + pause", "Maintien court": "Short hold",
    "Maintien moyen": "Medium hold", "Maintien long": "Long hold", "Micro-pompes": "Micro pumps",
    "Macro-pompes": "Macro pumps", "Sync pics": "Sync peaks", "Sync descentes": "Sync drops",
    "Sync changements": "Sync changes", "50/50": "50/50", "25/75": "25/75", "75/25": "75/25",
    "Court-court-long": "Short-short-long", "Long-court-court": "Long-short-short",
    "Pause 1 s": "1 s pause", "Pause 2 s": "2 s pause", "Pause 3 s": "3 s pause",
    "Ultra rapide": "Ultra fast", "Cycle profond": "Deep cycle", "Chaos contrôlé": "Controlled chaos",
}

UI_TEXT = {
    "fr": {
        "graph_title": "FUNSCRIPT PRINCIPAL — trame complète", "point": "point", "points": "points",
        "subtitle": "BLE direct • Osc + Vib + Con • MPV écran de droite", "video_script": "Vidéo / Funscript",
        "video": "Vidéo", "browse": "Parcourir", "script": "Script", "profile_functions": "Profil / Fonctions",
        "profile": "Profil", "language": "Langue", "fullscreen": "Plein écran droite", "enable": "Activer",
        "ultraslow": "Mode ultra-lent par impulsions", "transition": "Transition", "pulse_on": "Impulsion ON",
        "pause_off": "Pause OFF", "pulse_force": "Force impulsion", "smoothing": "Liss.",
        "vib_duration": "Durée VIB", "pattern": "Pattern", "level": "Niv.", "threshold": "Seuil",
        "pump_duration": "Durée pompage", "auto_next": "Vidéo suivante auto", "delete_after": "Supprimer après lecture",
        "play": "▶ Lancer", "next": "⏩ Suivante", "test_osc": "Test OSC", "test_vib": "Test VIB",
        "test_con": "Test CON", "stop": "■ STOP", "choose_video_graph": "Choisis une vidéo avec son funscript",
        "no_script": "Aucun script sélectionné", "choose_start": "Choisis une vidéo pour commencer.",
        "inactive": "INACTIF", "active": "ACTIF",
    },
    "en": {
        "graph_title": "MAIN FUNSCRIPT — full timeline", "point": "point", "points": "points",
        "subtitle": "Direct BLE • Osc + Vib + Con • MPV on right screen", "video_script": "Video / Funscript",
        "video": "Video", "browse": "Browse", "script": "Script", "profile_functions": "Profile / Functions",
        "profile": "Profile", "language": "Language", "fullscreen": "Fullscreen right screen", "enable": "Enable",
        "ultraslow": "Ultra-slow pulse mode", "transition": "Transition", "pulse_on": "Pulse ON",
        "pause_off": "Pause OFF", "pulse_force": "Pulse force", "smoothing": "Smooth",
        "vib_duration": "VIB duration", "pattern": "Pattern", "level": "Level", "threshold": "Threshold",
        "pump_duration": "Pump duration", "auto_next": "Auto play next video", "delete_after": "Delete after playback",
        "play": "▶ Play", "next": "⏩ Next", "test_osc": "Test OSC", "test_vib": "Test VIB",
        "test_con": "Test CON", "stop": "■ STOP", "choose_video_graph": "Choose a video with its funscript",
        "no_script": "No script selected", "choose_start": "Choose a video to begin.",
        "inactive": "INACTIVE", "active": "ACTIVE",
    },
}


def pattern_gate(name, t, duration_ms=900):
    """Retourne 0..1 selon le pattern choisi; conserve le funscript comme enveloppe."""
    import math, random
    d = max(0.15, duration_ms / 1000.0)
    phase = t % max(d, 0.001)
    x = phase / d

    if name == "Funscript direct" or name in ("Continu", "Pompage continu"):
        return 1.0
    if "Ultra rapide" in name or "Mitraillette" in name:
        return 1.0 if (t % 0.12) < 0.06 else 0.0
    if "Micro" in name:
        return 1.0 if (t % 0.20) < 0.08 else 0.0
    if "Macro" in name:
        return 1.0 if (t % 1.8) < 1.15 else 0.0
    if "Double" in name:
        q = t % d
        return 1.0 if q < d*.18 or d*.32 < q < d*.50 else 0.0
    if "Triple" in name or "x3" in name:
        q = t % d
        return 1.0 if any(a*d < q < (a+.13)*d for a in (.05,.30,.55)) else 0.0
    if "x5" in name:
        return 1.0 if (t % (d/5)) < (d/10) else 0.0
    if "Heartbeat" in name or "Battement" in name:
        q = t % d
        return 1.0 if q < d*.16 or d*.24 < q < d*.38 else 0.0
    if "Montée" in name or "Saw montant" in name or "Progressif" in name:
        return x
    if "Descente" in name or "Saw descendant" in name:
        return 1.0 - x
    if "Vague" in name or "Respiration" in name or "Triangle" in name:
        return 0.5 + 0.5 * math.sin(2*math.pi*x - math.pi/2)
    if "Escalier" in name:
        levels = 5 if "5" in name else 3
        return (int(x*levels) % levels + 1) / levels
    if "faible/fort" in name or "50/100" in name or "50/50" in name:
        return 0.5 if x < .5 else 1.0
    if "25/75/100" in name:
        return (0.25, 0.75, 1.0)[min(2, int(x*3))]
    if "25/75" in name:
        return .25 if x < .5 else .75
    if "75/25" in name:
        return .75 if x < .5 else .25
    if "Pause 1 s" in name:
        return 1.0 if (t % 2.0) < 1.0 else 0.0
    if "Pause 2 s" in name:
        return 1.0 if (t % 3.0) < 1.0 else 0.0
    if "Pause 3 s" in name:
        return 1.0 if (t % 4.0) < 1.0 else 0.0
    if "Pause courte" in name:
        return 1.0 if x < .70 else 0.0
    if "Pause moyenne" in name:
        return 1.0 if x < .55 else 0.0
    if "Pause longue" in name:
        return 1.0 if x < .35 else 0.0
    if "Random" in name or "Chaos" in name:
        slot = int(t / max(.12, d/5))
        rng = random.Random(slot + sum(map(ord, name)))
        floor = .15 if "doux" in name else .35
        return floor + (1-floor)*rng.random()
    if "court" in name.lower() or "Burst" in name or "Rafale" in name:
        return 1.0 if x < .30 else 0.0
    if "long" in name.lower() or "Maintien" in name:
        return 1.0 if x < .75 else 0.0
    if "Staccato" in name or "Écho" in name:
        return 1.0 if (t % .35) < .12 else .25
    return 1.0 if x < .55 else 0.0

class App(tk.Tk):
    COLORS = {
        "bg": "#0d0908",
        "panel": "#17110f",
        "panel_alt": "#241713",
        "border": "#4b2418",
        "text": "#f2f4f8",
        "muted": "#9aa3b2",
        "accent": "#ff5a1f",
        "accent_hover": "#ff8a3d",
        "success": "#39d98a",
        "warning": "#ffb84d",
        "danger": "#ff5c72",
        "track": "#3a2119",
        "blue": "#2d8cff",
        "pink": "#ff3da7",
        "gold": "#ffbd2e",
    }

    def __init__(self):
        super().__init__()
        self.title("JoyHub MaxSensr Funscript Player v1.4.13 — FR/EN + Funscript Fix")
        self.geometry("1100x790")
        self.minsize(760, 560)
        self.configure(bg=self.COLORS["bg"])

        try:
            self.attributes("-zoomed", True)
        except tk.TclError:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

        self.worker = BLEWorker(self)
        self.current_video = None
        self.current_script = None
        self.graph_actions = []
        self.graph_duration_ms = 0
        self.graph_position_ms = 0

        self._vars()
        self.configure_styles()
        self.load_config()
        self._sync_display_vars()
        self.build_ui()
        self._install_live_controls()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _vars(self):
        self.language = tk.StringVar(value="Français")
        self.video_path = tk.StringVar()
        self.script_path = tk.StringVar(value="Aucun script sélectionné")
        self.status_text = tk.StringVar(value="Choisis une vidéo pour commencer.")
        self.profile = tk.StringVar(value="Normal")
        self.profile_display = tk.StringVar(value="Normal")
        self.vib_pattern_display = tk.StringVar(value="Funscript direct")
        self.con_pattern_display = tk.StringVar(value="Funscript direct")

        self.osc_enabled = tk.BooleanVar(value=True)
        self.osc_min = tk.DoubleVar(value=15)
        self.osc_max = tk.DoubleVar(value=80)
        self.osc_amp = tk.DoubleVar(value=100)
        self.osc_transition_ms = tk.IntVar(value=2500)
        # Mode ultra-lent : le moteur OSC fonctionne par impulsions ON/OFF.
        # Cela réduit la vitesse moyenne sans modifier la mécanique de l'appareil.
        self.osc_pulse_enabled = tk.BooleanVar(value=True)
        self.osc_pulse_on_ms = tk.IntVar(value=150)
        self.osc_pulse_off_ms = tk.IntVar(value=350)
        # Force appliquée uniquement pendant la phase ON du mode impulsion.
        # 100 % = consigne OSC normale; 50 % = moitié de cette consigne.
        self.osc_pulse_force = tk.DoubleVar(value=60)

        self.vib_enabled = tk.BooleanVar(value=True)
        self.vib_min = tk.DoubleVar(value=30)
        self.vib_max = tk.DoubleVar(value=90)
        self.vib_amp = tk.DoubleVar(value=130)
        self.smoothing = tk.DoubleVar(value=25)
        self.vib_transition_ms = tk.IntVar(value=700)
        self.vib_duration_ms = tk.IntVar(value=900)
        self.vib_pattern = tk.StringVar(value="Funscript direct")

        self.con_enabled = tk.BooleanVar(value=True)
        self.con_level = tk.IntVar(value=5)
        self.con_threshold = tk.DoubleVar(value=58)
        self.con_duration = tk.IntVar(value=550)
        self.con_pattern = tk.StringVar(value="Funscript direct")

        self.fullscreen = tk.BooleanVar(value=True)
        self.delete_after = tk.BooleanVar(value=False)
        self.play_next = tk.BooleanVar(value=True)

        self.live_osc = tk.StringVar(value="0 %")
        self.live_vib = tk.StringVar(value="0 %")
        self.live_con = tk.StringVar(value="INACTIF")


    def _lang(self):
        return "en" if self.language.get() == "English" else "fr"

    def tr(self, key):
        return UI_TEXT[self._lang()].get(key, key)

    def _profile_to_display(self, canonical):
        return PROFILE_EN.get(canonical, canonical) if self._lang() == "en" else canonical

    def _display_to_profile(self, display):
        if self._lang() == "en":
            rev = {v: k for k, v in PROFILE_EN.items()}
            return rev.get(display, display)
        return display

    def _pattern_to_display(self, canonical, kind):
        if self._lang() != "en":
            return canonical
        table = VIB_PATTERN_EN if kind == "vib" else CON_PATTERN_EN
        return table.get(canonical, canonical)

    def _display_to_pattern(self, display, kind):
        if self._lang() != "en":
            return display
        table = VIB_PATTERN_EN if kind == "vib" else CON_PATTERN_EN
        rev = {v: k for k, v in table.items()}
        return rev.get(display, display)

    def _sync_display_vars(self):
        self.profile_display.set(self._profile_to_display(self.profile.get()))
        self.vib_pattern_display.set(self._pattern_to_display(self.vib_pattern.get(), "vib"))
        self.con_pattern_display.set(self._pattern_to_display(self.con_pattern.get(), "con"))

        # Le texte d'absence de funscript doit lui aussi suivre la langue.
        # On ne touche jamais à un vrai chemin de fichier déjà sélectionné.
        script_text = self.script_path.get().strip()
        if script_text in ("Aucun script sélectionné", "No script selected"):
            self.script_path.set(self.tr("no_script"))

        if self.live_con.get() in ("ACTIF", "ACTIVE"):
            self.live_con.set(self.tr("active"))
        else:
            self.live_con.set(self.tr("inactive"))

    def _profile_selected(self):
        self.profile.set(self._display_to_profile(self.profile_display.get()))
        self.apply_profile()

    def _pattern_selected(self, kind):
        if kind == "vib":
            self.vib_pattern.set(self._display_to_pattern(self.vib_pattern_display.get(), "vib"))
        else:
            self.con_pattern.set(self._display_to_pattern(self.con_pattern_display.get(), "con"))

    def change_language(self, *_):
        """Change la langue de toute l'interface sans arrêter la lecture."""
        self._sync_display_vars()
        for child in list(self.winfo_children()):
            child.destroy()
        self.build_ui()
        self.title("JoyHub MaxSensr Funscript Player v1.4.13 — FR/EN")
        self.status_text.set(
            "Interface switched to English." if self._lang() == "en"
            else "Interface passée en français."
        )


    def _install_live_controls(self):
        """Rend les réglages moteurs réellement live pendant la lecture."""
        live_vars = (
            self.osc_enabled, self.osc_min, self.osc_max, self.osc_amp,
            self.osc_transition_ms, self.osc_pulse_enabled,
            self.osc_pulse_on_ms, self.osc_pulse_off_ms, self.osc_pulse_force,
            self.vib_enabled, self.vib_min, self.vib_max, self.vib_amp,
            self.smoothing, self.vib_transition_ms, self.vib_duration_ms,
            self.vib_pattern, self.con_enabled, self.con_level,
            self.con_threshold, self.con_duration, self.con_pattern,
        )
        for var in live_vars:
            var.trace_add("write", self._push_live_settings)

    def _push_live_settings(self, *_):
        # Cette méthode est appelée dans le thread Tk. On prend un snapshot
        # puis on met à jour le dictionnaire déjà utilisé par le worker BLE.
        try:
            new_settings = self.settings()
            if self.worker.settings is not None:
                self.worker.settings.update(new_settings)
        except Exception:
            pass

    def configure_styles(self):
        c = self.COLORS
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=c["bg"], foreground=c["text"],
            bordercolor=c["border"], lightcolor=c["border"],
            darkcolor=c["border"], font=("Noto Sans", 10),
        )
        style.configure("Root.TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["panel"], borderwidth=1, relief="solid")
        style.configure("Header.TLabel", background=c["bg"], foreground=c["text"],
                        font=("Noto Sans", 22, "bold"))
        style.configure("Subtitle.TLabel", background=c["bg"], foreground=c["muted"],
                        font=("Noto Sans", 10))
        style.configure("CardTitle.TLabel", background=c["panel"], foreground=c["text"],
                        font=("Noto Sans", 12, "bold"))
        style.configure("CardText.TLabel", background=c["panel"], foreground=c["muted"])
        style.configure("Value.TLabel", background=c["panel"], foreground=c["accent_hover"],
                        font=("Noto Sans", 10, "bold"))
        style.configure("Status.TLabel", background=c["panel_alt"], foreground=c["muted"],
                        padding=(12, 9))
        style.configure("Dark.TEntry", fieldbackground=c["panel_alt"], foreground=c["text"],
                        insertcolor=c["text"], bordercolor=c["border"], padding=9)
        style.map("Dark.TEntry",
                  fieldbackground=[("readonly", c["panel_alt"])],
                  foreground=[("readonly", c["text"])])

        # Combobox sombre, lisible et surtout cliquable sous Tk/X11.
        # On évite les options de padding/selectbackground qui peuvent rendre
        # le bouton de flèche inactif avec certains thèmes KDE/Tk.
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#15171c",
            background="#2a2d34",
            foreground="#ffffff",
            arrowcolor=c["accent_hover"],
            bordercolor=c["border"],
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", "#15171c")],
            foreground=[("readonly", "#ffffff")],
            background=[("active", "#343841")],
        )

        # Couleurs de la liste déroulante sous Tk/X11.
        try:
            self.option_add("*TCombobox*Listbox.background", "#15171c")
            self.option_add("*TCombobox*Listbox.foreground", "#ffffff")
            self.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
            self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        except tk.TclError:
            pass

        style.configure("Accent.TButton", background=c["accent"], foreground="#ffffff",
                        borderwidth=0, focusthickness=0, padding=(16, 11),
                        font=("Noto Sans", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", c["accent_hover"]), ("disabled", c["panel_alt"])],
                  foreground=[("disabled", "#6e7582")])
        style.configure("Secondary.TButton", background=c["panel_alt"], foreground=c["text"],
                        borderwidth=1, padding=(14, 10))
        style.map("Secondary.TButton", background=[("active", "#2a2f39")])
        style.configure("Danger.TButton", background="#382028", foreground="#ff8797",
                        borderwidth=0, padding=(14, 10),
                        font=("Noto Sans", 10, "bold"))
        style.map("Danger.TButton", background=[("active", "#4a2530")])
        style.configure("Dark.Horizontal.TScale", background=c["panel"],
                        troughcolor=c["track"], bordercolor=c["panel"],
                        lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure("Dark.TCheckbutton", background=c["panel"], foreground=c["text"],
                        indicatorbackground=c["panel_alt"], indicatorforeground=c["accent"], padding=4)
        style.map("Dark.TCheckbutton",
                  background=[("active", c["panel"])],
                  foreground=[("active", c["text"])])

    def _bind_combo_click(self, combo):
        """Force l'ouverture du menu au clic, y compris sous certains thèmes KDE/Tk."""
        def _open(event):
            try:
                event.widget.tk.call("ttk::combobox::Post", str(event.widget))
                return "break"
            except tk.TclError:
                return None
        combo.bind("<Button-1>", _open, add="+")

    def build_ui(self):
        c = self.COLORS

        shell = ttk.Frame(self, style="Root.TFrame")
        shell.pack(fill="both", expand=True)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_rowconfigure(1, weight=1, minsize=260)

        # Trame complète fixe en bas.
        graph_frame = tk.Frame(
            shell, bg="#050609", height=300,
            highlightbackground="#3b4050", highlightthickness=1
        )
        graph_frame.grid(row=1, column=0, sticky="nsew")
        graph_frame.grid_propagate(False)

        graph_header = tk.Frame(graph_frame, bg="#050609", height=26)
        graph_header.pack(side="top", fill="x")
        graph_header.pack_propagate(False)

        tk.Label(
            graph_header,
            text=self.tr("graph_title"),
            bg="#050609", fg="#aeb6c5",
            font=("Noto Sans", 8, "bold"), anchor="w", padx=10
        ).pack(side="left", fill="x", expand=True)

        self.graph_info = tk.Label(
            graph_header, text=f"0 {self.tr("point")}", bg="#050609",
            fg=c["accent_hover"], font=("Noto Sans", 8, "bold"), padx=10
        )
        self.graph_info.pack(side="right")

        self.graph_canvas = tk.Canvas(
            graph_frame, bg="#090b10", highlightthickness=0,
            borderwidth=0, height=268
        )
        self.graph_canvas.pack(side="bottom", fill="both", expand=True)
        self.graph_canvas.bind("<Configure>", lambda _e: self.draw_funscript_graph())
        # Pendant un glissement, on ne demande PLUS un seek BLE à chaque pixel.
        # Sinon chaque événement peut provoquer un cycle STOP/déconnexion/reconnexion
        # et laisser une nouvelle demande de seek en attente pendant la reconnexion.
        # On déplace seulement le curseur visuel pendant le drag et on envoie UNE
        # seule demande de seek au relâchement du bouton.
        self.graph_canvas.bind("<Button-1>", self.preview_seek_from_graph)
        self.graph_canvas.bind("<B1-Motion>", self.preview_seek_from_graph)
        self.graph_canvas.bind("<ButtonRelease-1>", self.commit_seek_from_graph)

        # Zone principale compacte : aucun défilement vertical.
        root = ttk.Frame(shell, style="Root.TFrame", padding=(20, 14, 20, 12))
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)

        # HEADER compact
        header = ttk.Frame(root, style="Root.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="JoyHub MaxSensr Player", style="Header.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(header, text="v1.4.13 — FR/EN + Funscript Fix", style="Subtitle.TLabel").grid(
            row=0, column=1, sticky="e")
        ttk.Label(
            header,
            text=self.tr("subtitle"),
            style="Subtitle.TLabel"
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))

        # Ligne sélection vidéo/script + profil
        top = ttk.Frame(root, style="Root.TFrame")
        top.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)

        file_card = ttk.Frame(top, style="Card.TFrame", padding=(12, 9))
        file_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        file_card.columnconfigure(1, weight=1)

        ttk.Label(file_card, text=self.tr("video_script"), style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(file_card, text=self.tr("video"), style="CardText.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Entry(file_card, textvariable=self.video_path, state="readonly",
                  style="Dark.TEntry").grid(row=1, column=1, sticky="ew", padx=7, pady=2)
        ttk.Button(file_card, text=self.tr("browse"), style="Secondary.TButton",
                   command=self.choose_video).grid(row=1, column=2, pady=2)

        ttk.Label(file_card, text=self.tr("script"), style="CardText.TLabel").grid(row=2, column=0, sticky="w")
        self.script_label = ttk.Label(file_card, textvariable=self.script_path,
                                      style="CardText.TLabel")
        self.script_label.grid(row=2, column=1, columnspan=2, sticky="w", padx=7, pady=2)

        profile_card = ttk.Frame(top, style="Card.TFrame", padding=(12, 9))
        profile_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        profile_card.columnconfigure(1, weight=1)
        ttk.Label(profile_card, text=self.tr("profile_functions"), style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(profile_card, text=self.tr("profile"), style="CardText.TLabel").grid(row=1, column=0, sticky="w")
        combo = ttk.Combobox(
            profile_card, textvariable=self.profile_display,
            values=tuple(self._profile_to_display(x) for x in PROFILES.keys()), state="readonly", width=17, style="Dark.TCombobox"
        )
        combo.grid(row=1, column=1, sticky="w", padx=7)
        self._bind_combo_click(combo)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._profile_selected())

        ttk.Checkbutton(profile_card, text="OSC", variable=self.osc_enabled,
                        style="Dark.TCheckbutton").grid(row=1, column=2, padx=6)
        ttk.Checkbutton(profile_card, text="VIB", variable=self.vib_enabled,
                        style="Dark.TCheckbutton").grid(row=1, column=3, padx=6)
        ttk.Checkbutton(profile_card, text="CON", variable=self.con_enabled,
                        style="Dark.TCheckbutton").grid(row=2, column=2, padx=6, pady=(3,0))
        ttk.Checkbutton(profile_card, text=self.tr("fullscreen"), variable=self.fullscreen,
                        style="Dark.TCheckbutton").grid(row=2, column=0, columnspan=2, sticky="w", pady=(3,0))
        ttk.Label(profile_card, text=self.tr("language"), style="CardText.TLabel").grid(
            row=3, column=0, sticky="w", pady=(4,0))
        lang_combo = ttk.Combobox(
            profile_card, textvariable=self.language, values=("Français", "English"),
            state="readonly", width=12, style="Dark.TCombobox"
        )
        lang_combo.grid(row=3, column=1, sticky="w", padx=7, pady=(4,0))
        self._bind_combo_click(lang_combo)
        lang_combo.bind("<<ComboboxSelected>>", self.change_language)

        # 3 cartes moteur compactes
        settings_row = ttk.Frame(root, style="Root.TFrame")
        settings_row.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for i in range(3):
            settings_row.columnconfigure(i, weight=1, uniform="motor")

        osc = ttk.Frame(settings_row, style="Card.TFrame", padding=(12, 8))
        osc.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ttk.Label(osc, text="OSCILLATION", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(osc, text=self.tr("enable"), variable=self.osc_enabled,
                        style="Dark.TCheckbutton").grid(row=0, column=2, sticky="e")
        self.add_scale(osc, 1, "Min", self.osc_min, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(osc, 2, "Max", self.osc_max, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(osc, 3, "Amp", self.osc_amp, 25, 150, lambda v: f"{float(v):.0f}%")
        self.add_scale(osc, 4, self.tr("transition"), self.osc_transition_ms, 200, 4000,
                       lambda v: f"{int(float(v))}ms")
        ttk.Checkbutton(
            osc, text=self.tr("ultraslow"),
            variable=self.osc_pulse_enabled, style="Dark.TCheckbutton"
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 1))
        self.add_scale(osc, 6, self.tr("pulse_on"), self.osc_pulse_on_ms, 100, 1000,
                       lambda v: f"{int(float(v))}ms")
        self.add_scale(osc, 7, self.tr("pause_off"), self.osc_pulse_off_ms, 100, 3000,
                       lambda v: f"{int(float(v))}ms")
        self.add_scale(osc, 8, self.tr("pulse_force"), self.osc_pulse_force, 10, 100,
                       lambda v: f"{float(v):.0f}%")
        ttk.Label(osc, textvariable=self.live_osc, style="Value.TLabel").grid(
            row=9, column=1, sticky="w", padx=7, pady=(3, 0))

        vib = ttk.Frame(settings_row, style="Card.TFrame", padding=(12, 8))
        vib.grid(row=0, column=1, sticky="nsew", padx=4)
        ttk.Label(vib, text="VIBRATION", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(vib, text=self.tr("enable"), variable=self.vib_enabled,
                        style="Dark.TCheckbutton").grid(row=0, column=2, sticky="e")
        self.add_scale(vib, 1, "Min", self.vib_min, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 2, "Max", self.vib_max, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 3, "Amp", self.vib_amp, 25, 150, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 4, self.tr("smoothing"), self.smoothing, 0, 90, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 5, self.tr("transition"), self.vib_transition_ms, 150, 2500,
                       lambda v: f"{int(float(v))}ms")
        self.add_scale(vib, 6, self.tr("vib_duration"), self.vib_duration_ms, 150, 3000,
                       lambda v: f"{int(float(v))}ms")
        ttk.Label(vib, text=self.tr("pattern"), style="CardText.TLabel").grid(
            row=7, column=0, sticky="w", pady=2)
        vib_combo = ttk.Combobox(vib, textvariable=self.vib_pattern_display,
                                 values=[self._pattern_to_display(x, "vib") for x in VIB_PATTERNS],
                                 state="readonly", width=20, style="Dark.TCombobox")
        vib_combo.grid(row=7, column=1, columnspan=2, sticky="ew", padx=7, pady=2)
        self._bind_combo_click(vib_combo)
        vib_combo.bind("<<ComboboxSelected>>", lambda _e: self._pattern_selected("vib"))
        ttk.Label(vib, textvariable=self.live_vib, style="Value.TLabel").grid(
            row=8, column=1, sticky="w", padx=7, pady=(3, 0))

        con = ttk.Frame(settings_row, style="Card.TFrame", padding=(12, 8))
        con.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        ttk.Label(con, text="CONSTRICTION", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(con, text=self.tr("enable"), variable=self.con_enabled,
                        style="Dark.TCheckbutton").grid(row=0, column=2, sticky="e")
        self.add_scale(con, 1, self.tr("level"), self.con_level, 1, 5, lambda v: f"{int(float(v))}")
        self.add_scale(con, 2, self.tr("threshold"), self.con_threshold, 40, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(con, 3, self.tr("pump_duration"), self.con_duration, 150, 1200,
                       lambda v: f"{int(float(v))}ms")
        ttk.Label(con, text=self.tr("pattern"), style="CardText.TLabel").grid(
            row=4, column=0, sticky="w", pady=2)
        con_combo = ttk.Combobox(con, textvariable=self.con_pattern_display,
                                 values=[self._pattern_to_display(x, "con") for x in CON_PATTERNS],
                                 state="readonly", width=20, style="Dark.TCombobox")
        con_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=7, pady=2)
        self._bind_combo_click(con_combo)
        con_combo.bind("<<ComboboxSelected>>", lambda _e: self._pattern_selected("con"))
        ttk.Label(con, textvariable=self.live_con, style="Value.TLabel").grid(
            row=5, column=1, sticky="w", padx=7, pady=(3, 0))

        # Ligne options ultra compacte
        opts = ttk.Frame(root, style="Card.TFrame", padding=(12, 6))
        opts.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(opts, text=self.tr("auto_next"), variable=self.play_next,
                        style="Dark.TCheckbutton").pack(side="left", padx=(0, 15))
        ttk.Checkbutton(opts, text=self.tr("delete_after"), variable=self.delete_after,
                        style="Dark.TCheckbutton").pack(side="left")

        # Actions sur une seule ligne
        actions = ttk.Frame(root, style="Root.TFrame")
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text=self.tr("play"), command=self.play,
                   style="Accent.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(actions, text=self.tr("next"), command=self.next_video,
                   style="Secondary.TButton").grid(row=0, column=1, padx=5)
        ttk.Button(actions, text=self.tr("test_osc"), command=lambda: self.test("osc"),
                   style="Secondary.TButton").grid(row=0, column=2, padx=5)
        ttk.Button(actions, text=self.tr("test_vib"), command=lambda: self.test("vib"),
                   style="Secondary.TButton").grid(row=0, column=3, padx=5)
        ttk.Button(actions, text=self.tr("test_con"), command=lambda: self.test("con"),
                   style="Secondary.TButton").grid(row=0, column=4, padx=5)
        ttk.Button(actions, text=self.tr("stop"), command=self.stop,
                   style="Danger.TButton").grid(row=0, column=5, padx=(5, 0))

        ttk.Label(root, textvariable=self.status_text, style="Status.TLabel",
                  anchor="w").grid(row=5, column=0, sticky="ew")

        self.after(100, self.draw_funscript_graph)

    def add_scale(self, parent, row, label, variable, low, high, formatter):
        ttk.Label(parent, text=label, style="CardText.TLabel").grid(
            row=row, column=0, sticky="w", pady=2)
        scale = ttk.Scale(parent, from_=low, to=high, variable=variable,
                          style="Dark.Horizontal.TScale")
        scale.grid(row=row, column=1, sticky="ew", padx=10, pady=2)
        parent.columnconfigure(1, weight=1)
        value = ttk.Label(parent, style="Value.TLabel", width=8, anchor="e")
        value.grid(row=row, column=2, sticky="e", pady=2)

        def update(*_):
            try:
                value.configure(text=formatter(variable.get()))
            except Exception:
                pass
        variable.trace_add("write", update)
        update()

    def load_funscript_graph(self, script):
        try:
            self.graph_actions = load_actions(script)
            self.graph_duration_ms = self.graph_actions[-1][0] if self.graph_actions else 0
        except Exception:
            self.graph_actions = []
            self.graph_duration_ms = 0
        self.graph_position_ms = 0
        self.graph_info.configure(text=f"{len(self.graph_actions)} {self.tr('point') if len(self.graph_actions) == 1 else self.tr('points')}")
        self.draw_funscript_graph()

    def draw_funscript_graph(self):
        if not hasattr(self, "graph_canvas"):
            return
        c = self.graph_canvas
        c.delete("all")
        w = max(c.winfo_width(), 2)
        h = max(c.winfo_height(), 2)

        for frac in (0.25, 0.5, 0.75):
            y = h * frac
            c.create_line(0, y, w, y, fill="#1a1e27")

        if not self.graph_actions or self.graph_duration_ms <= 0:
            c.create_text(
                w / 2, h / 2,
                text=self.tr("choose_video_graph"),
                fill="#6f7787", font=("Noto Sans", 9)
            )
            return

        pts = []
        step = max(1, len(self.graph_actions) // max(1, int(w)))
        for at, pos in self.graph_actions[::step]:
            x = at / self.graph_duration_ms * w
            y = h - (pos / 100.0 * (h - 10)) - 5
            pts.extend((x, y))
        if len(pts) >= 4:
            c.create_line(*pts, fill=self.COLORS["accent_hover"], width=2.0)

        x = min(max(self.graph_position_ms / self.graph_duration_ms, 0.0), 1.0) * w
        c.create_line(x, 0, x, h, fill="#ffffff", width=2, tags="playhead")
        c.create_oval(x-4, 3, x+4, 11, fill=self.COLORS["accent"], outline="", tags="playhead")

    def draw_playhead_only(self):
        if not hasattr(self, "graph_canvas") or not self.graph_duration_ms:
            return
        c = self.graph_canvas
        w = max(c.winfo_width(), 2)
        h = max(c.winfo_height(), 2)
        c.delete("playhead")
        x = min(max(self.graph_position_ms / self.graph_duration_ms, 0.0), 1.0) * w
        c.create_line(x, 0, x, h, fill="#ffffff", width=2, tags="playhead")
        c.create_oval(
            x - 4, 3, x + 4, 11,
            fill=self.COLORS["accent"], outline="", tags="playhead"
        )

    def _graph_target_ms(self, event):
        """Convertit la position X de la souris en position vidéo, en ms."""
        if not self.graph_duration_ms:
            return None
        width = max(1, self.graph_canvas.winfo_width())
        ratio = clamp(event.x / width, 0.0, 1.0)
        return int(ratio * self.graph_duration_ms)

    def preview_seek_from_graph(self, event):
        """Pendant clic/glissé : déplace seulement le curseur visuel."""
        target_ms = self._graph_target_ms(event)
        if target_ms is None:
            return
        self.graph_position_ms = target_ms
        self.draw_playhead_only()
        self.status_text.set(
            f"Position visée : {target_ms/1000:.1f} s — relâche pour avancer."
        )

    def commit_seek_from_graph(self, event):
        """Au relâchement : envoie UNE seule demande de seek au worker."""
        target_ms = self._graph_target_ms(event)
        if target_ms is None:
            return
        self.graph_position_ms = target_ms
        self.draw_playhead_only()

        if self.worker.process and self.worker.process.poll() is None:
            # Écrase une éventuelle vieille demande et n'en laisse qu'une seule.
            with self.worker.seek_lock:
                self.worker.requested_seek_ms = max(0, int(target_ms))
            self.status_text.set(
                f"Seek : {target_ms/1000:.1f} s — arrêt OSC, seek, puis reconnexion BLE unique…"
            )

    def update_progress(self, timeline_pct, fun_pos, vib, osc, con_active):
        if self.graph_duration_ms:
            self.graph_position_ms = int(
                clamp(float(timeline_pct), 0, 100) / 100.0 * self.graph_duration_ms
            )
        self.live_osc.set(f"{osc:.0f} %")
        self.live_vib.set(f"{vib:.0f} %")
        self.live_con.set(self.tr("active") if con_active else self.tr("inactive"))
        self.draw_playhead_only()

    def set_status(self, text, kind=None):
        self.status_text.set(text)

    def apply_profile(self):
        data = PROFILES.get(self.profile.get())
        if not data:
            return
        for k, v in data.items():
            getattr(self, k).set(v)
        self.status_text.set((f"Profile {self._profile_to_display(self.profile.get())} applied." if self._lang() == "en" else f"Profil {self.profile.get()} appliqué."))

    def choose_video(self):
        p = filedialog.askopenfilename(
            initialdir=str(VIDEO_DIR if VIDEO_DIR.is_dir() else Path.home()),
            filetypes=[("Vidéos", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v"), ("Tous", "*")]
        )
        if not p:
            return
        video = Path(p)
        self.current_video = video
        self.video_path.set(str(video))
        script = find_script(video)
        if script:
            self.current_script = script
            self.script_path.set(f"✓  {script}")
            self.load_funscript_graph(script)
            self.status_text.set("Video and funscript ready." if self._lang() == "en" else "Vidéo et funscript prêts.")
        else:
            self.current_script = None
            self.script_path.set(
                f"✕  No matching script in {SCRIPT_DIR}" if self._lang() == "en"
                else f"✕  Aucun script correspondant dans {SCRIPT_DIR}"
            )
            self.graph_actions = []
            self.graph_duration_ms = 0
            self.draw_funscript_graph()
            messagebox.showwarning(
                "Funscript not found" if self._lang() == "en" else "Funscript introuvable",
                (f"No funscript with the same name as the video.\n\nFolder searched:\n{SCRIPT_DIR}"
                 if self._lang() == "en"
                 else f"Aucun funscript portant le même nom que la vidéo.\n\nDossier recherché :\n{SCRIPT_DIR}")
            )

    def play(self):
        video = self.current_video or (Path(self.video_path.get()) if self.video_path.get() else None)
        script_text = self.script_path.get().removeprefix("✓  ").strip()
        script = self.current_script or (Path(script_text) if script_text else None)
        if not video or not script or not video.is_file() or not script.is_file():
            messagebox.showerror(
                "File not found" if self._lang() == "en" else "Fichier introuvable",
                "Choose a video and its funscript." if self._lang() == "en" else "Choisis une vidéo et son funscript."
            )
            return
        if self.osc_min.get() > self.osc_max.get() or self.vib_min.get() > self.vib_max.get():
            messagebox.showerror(
                "Invalid settings" if self._lang() == "en" else "Réglages invalides",
                "Minimum values must be lower than maximum values." if self._lang() == "en" else "Les minimums doivent être inférieurs aux maximums."
            )
            return

        self.current_video, self.current_script = video, script
        self.load_funscript_graph(script)
        self.save_config()
        self.worker.start(video, script, self.settings())
        self.status_text.set("BLE connection and MPV launch on the right screen…" if self._lang() == "en" else "Connexion BLE et lancement de MPV sur l’écran de droite…")

    def stop(self):
        self.worker.stop()
        self.live_osc.set("0 %")
        self.live_vib.set("0 %")
        self.live_con.set("INACTIF")
        self.graph_position_ms = 0
        self.draw_funscript_graph()
        self.status_text.set("Stop requested." if self._lang() == "en" else "Arrêt demandé.")

    def test(self, which):
        if BleakScanner is None:
            messagebox.showerror(
                "Bleak absent",
                "Lance ce player avec :\n~/venv-maxsensr/bin/python"
            )
            return
        value = 60 if which in ("vib", "osc") else self.con_level.get()

        def runner():
            try:
                asyncio.run(self.worker.test_motor(which, value, self.settings()))
                self.after(0, lambda: self.status_text.set(f"Test {which.upper()} terminé."))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Test BLE", str(exc)))

        threading.Thread(target=runner, daemon=True).start()
        self.status_text.set(f"Test {which.upper()} for 2 seconds…" if self._lang() == "en" else f"Test {which.upper()} pendant 2 secondes…")

    def on_natural_end(self):
        old_video, old_script = self.current_video, self.current_script
        if self.delete_after.get():
            for p in (old_video, old_script):
                try:
                    if p and p.is_file():
                        p.unlink()
                except OSError:
                    pass

        if self.play_next.get():
            self.next_video(auto=True)
        else:
            self.status_text.set("Playback finished." if self._lang() == "en" else "Lecture terminée.")

    def next_video(self, auto=False):
        self.worker.stop()
        video = self.current_video
        if not video or not video.parent.is_dir():
            self.status_text.set("No current video." if self._lang() == "en" else "Aucune vidéo actuelle.")
            return

        videos = sorted(
            [p for p in video.parent.iterdir()
             if p.is_file() and p.suffix.casefold() in VIDEO_EXTENSIONS],
            key=natural_key
        )
        try:
            idx = videos.index(video)
        except ValueError:
            return

        for candidate in videos[idx + 1:]:
            script = find_script(candidate)
            if script:
                self.current_video = candidate
                self.current_script = script
                self.video_path.set(str(candidate))
                self.script_path.set(f"✓  {script}")
                self.load_funscript_graph(script)
                self.status_text.set((f"Next video: {candidate.name}" if self._lang() == "en" else f"Vidéo suivante : {candidate.name}"))
                if auto:
                    self.after(500, self.play)
                return

        self.status_text.set("End of playlist." if self._lang() == "en" else "Fin de la playlist.")

    def settings(self):
        return {
            "osc_enabled": self.osc_enabled.get(),
            "osc_min": self.osc_min.get(),
            "osc_max": self.osc_max.get(),
            "osc_amp": self.osc_amp.get(),
            "osc_transition_ms": self.osc_transition_ms.get(),
            "osc_pulse_enabled": self.osc_pulse_enabled.get(),
            "osc_pulse_on_ms": self.osc_pulse_on_ms.get(),
            "osc_pulse_off_ms": self.osc_pulse_off_ms.get(),
            "osc_pulse_force": self.osc_pulse_force.get(),

            "vib_enabled": self.vib_enabled.get(),
            "vib_min": self.vib_min.get(),
            "vib_max": self.vib_max.get(),
            "vib_amp": self.vib_amp.get(),
            "smoothing": self.smoothing.get(),
            "vib_transition_ms": self.vib_transition_ms.get(),
            "vib_duration_ms": self.vib_duration_ms.get(),
            "vib_pattern": self.vib_pattern.get(),

            "con_enabled": self.con_enabled.get(),
            "con_level": self.con_level.get(),
            "con_threshold": self.con_threshold.get(),
            "con_duration": self.con_duration.get(),
            "con_pattern": self.con_pattern.get(),

            "fullscreen": self.fullscreen.get(),
            "delete_after": self.delete_after.get(),
            "language": self.language.get(),
        }

    def save_config(self):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        data = self.settings() | {
            "profile": self.profile.get(),
            "play_next": self.play_next.get(),
            "video": self.video_path.get(),
            "script": self.script_path.get(),
        }
        try:
            CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def load_config(self):
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return

        for k, v in data.items():
            if hasattr(self, k) and hasattr(getattr(self, k), "set"):
                try:
                    getattr(self, k).set(v)
                except Exception:
                    pass

        video_text = self.video_path.get()
        if video_text:
            p = Path(video_text)
            if p.is_file():
                self.current_video = p

        script_text = self.script_path.get().removeprefix("✓  ").strip()
        if script_text:
            p = Path(script_text)
            if p.is_file():
                self.current_script = p

    def close(self):
        self.stop()
        self.save_config()
        self.after(150, self.destroy)


def _ensure_venv():
    """Relance automatiquement dans ~/venv-maxsensr si Bleak manque."""
    global BleakClient, BleakScanner
    if BleakScanner is not None:
        return

    vpy = Path.home() / "venv-maxsensr/bin/python"
    marker = "MAXSENSR_VENV_REEXEC"

    if vpy.is_file() and os.environ.get(marker) != "1":
        env = os.environ.copy()
        env[marker] = "1"
        os.execve(
            str(vpy),
            [str(vpy), str(Path(__file__).resolve())] + sys.argv[1:],
            env,
        )


if __name__ == "__main__":
    _ensure_venv()
    App().mainloop()
