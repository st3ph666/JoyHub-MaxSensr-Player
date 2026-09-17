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

APP_VERSION = "v1.5.1-PlaylistTransitionFix"
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
    data = json.loads(path.read_text(encoding="utf-8"))
    actions = sorted(data.get("actions", []), key=lambda x: int(x.get("at", 0)))
    return [(int(a["at"]), clamp(float(a["pos"]), 0, 100)) for a in actions if "at" in a and "pos" in a]

class BLEWorker:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.loop = None
        self._start_lock = threading.Lock()
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
        """Démarre une lecture sans réutiliser le stop_evt d'un ancien thread.

        Le v1.5.0 faisait stop() puis clear() immédiatement sur le MÊME Event.
        Lors d'un passage automatique à la vidéo suivante, l'ancien thread BLE
        pouvait donc voir l'Event remis à zéro et repartir en reconnexion pendant
        que le nouveau thread essayait lui aussi de prendre le MaxSensr.
        """
        old_thread = self.thread
        old_evt = self.stop_evt
        old_evt.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass

        # Chaque lecture reçoit désormais son propre Event. L'ancien thread garde
        # son Event positionné à SET et ne peut donc jamais "ressusciter".
        new_evt = threading.Event()
        self.stop_evt = new_evt
        self.end_callback_sent = False
        self.last_tms = 0.0
        self.duration_ms = 0.0
        self.video, self.script, self.settings = video, script, settings

        def launch_when_previous_is_done():
            # Ne jamais avoir deux sessions BLE concurrentes. On attend la sortie
            # réelle de l'ancienne session hors du thread Tk (UI reste fluide).
            if old_thread and old_thread.is_alive() and old_thread is not threading.current_thread():
                old_thread.join(timeout=12.0)
            if new_evt.is_set():
                return
            # Si un démarrage plus récent a déjà remplacé cet Event, abandonner.
            if self.stop_evt is not new_evt:
                return
            self.thread = threading.Thread(
                target=self._thread_main_with_event, args=(new_evt,), daemon=True
            )
            self.thread.start()

        threading.Thread(target=launch_when_previous_is_done, daemon=True).start()

    def _thread_main_with_event(self, run_evt):
        # _run() utilise self.stop_evt. On garantit que cette session est toujours
        # la session courante avant de l'autoriser à entrer dans la boucle BLE.
        if self.stop_evt is not run_evt or run_evt.is_set():
            return
        self._thread_main()

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
        self.title("JoyHub MaxSensr Funscript Player v1.5.1 — Playlist Transition Fix")
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
        self.playlist_dir = None
        self.playlist_videos = []

        self._vars()
        self.configure_styles()
        self.load_config()
        self.build_ui()
        self._install_live_controls()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _vars(self):
        self.video_path = tk.StringVar()
        self.script_path = tk.StringVar(value="Aucun script sélectionné")
        self.status_text = tk.StringVar(value="Choisis une vidéo pour commencer.")
        self.profile = tk.StringVar(value="Normal")

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

        # v1.5.0 : toute modification de configuration est appliquée ET
        # sauvegardée immédiatement, sans devoir relancer la lecture.
        persistent_live_vars = live_vars + (
            self.profile, self.fullscreen, self.delete_after, self.play_next,
        )
        for var in persistent_live_vars:
            var.trace_add("write", self._save_live_config)

    def _push_live_settings(self, *_):
        # Cette méthode est appelée dans le thread Tk. On prend un snapshot
        # puis on met à jour le dictionnaire déjà utilisé par le worker BLE.
        try:
            new_settings = self.settings()
            if self.worker.settings is not None:
                self.worker.settings.update(new_settings)
        except Exception:
            pass

    def _save_live_config(self, *_):
        """Sauvegarde live avec anti-rebond pour éviter d'écrire à chaque pixel d'un slider."""
        old = getattr(self, "_live_save_after_id", None)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass
        self._live_save_after_id = self.after(180, self.save_config)

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

        # Combobox sombre et lisible (champ + liste déroulante)
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#111317",
            background="#1a1d23",
            foreground="#ffffff",
            arrowcolor=c["accent_hover"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
            padding=(8, 6),
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", "#111317"), ("focus", "#111317")],
            foreground=[("readonly", "#ffffff"), ("focus", "#ffffff")],
            background=[("active", "#262a31")],
            selectbackground=[("readonly", c["accent"])],
            selectforeground=[("readonly", "#ffffff")],
        )

        # Couleurs de la liste popup des ttk.Combobox sous Tk/X11.
        try:
            self.option_add("*TCombobox*Listbox.background", "#111317")
            self.option_add("*TCombobox*Listbox.foreground", "#ffffff")
            self.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
            self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
            self.option_add("*TCombobox*Listbox.font", "Noto Sans 11")
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
            text="FUNSCRIPT PRINCIPAL — trame complète",
            bg="#050609", fg="#aeb6c5",
            font=("Noto Sans", 8, "bold"), anchor="w", padx=10
        ).pack(side="left", fill="x", expand=True)

        self.graph_info = tk.Label(
            graph_header, text="0 point", bg="#050609",
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
        ttk.Label(header, text="v1.5.1 — Playlist Transition Fix • Auto Delete • Live Config", style="Subtitle.TLabel").grid(
            row=0, column=1, sticky="e")
        ttk.Label(
            header,
            text="BLE direct • Osc + Vib + Con • MPV écran de droite",
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

        ttk.Label(file_card, text="Vidéo / Funscript", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(file_card, text="Vidéo", style="CardText.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Entry(file_card, textvariable=self.video_path, state="readonly",
                  style="Dark.TEntry").grid(row=1, column=1, sticky="ew", padx=7, pady=2)
        browse_box = ttk.Frame(file_card, style="Card.TFrame")
        browse_box.grid(row=1, column=2, pady=2)
        ttk.Button(browse_box, text="Vidéo", style="Secondary.TButton",
                   command=self.choose_video).pack(side="left", padx=(0,4))
        ttk.Button(browse_box, text="Dossier", style="Secondary.TButton",
                   command=self.choose_folder).pack(side="left")

        ttk.Label(file_card, text="Script", style="CardText.TLabel").grid(row=2, column=0, sticky="w")
        self.script_label = ttk.Label(file_card, textvariable=self.script_path,
                                      style="CardText.TLabel")
        self.script_label.grid(row=2, column=1, columnspan=2, sticky="w", padx=7, pady=2)

        profile_card = ttk.Frame(top, style="Card.TFrame", padding=(12, 9))
        profile_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        profile_card.columnconfigure(1, weight=1)
        ttk.Label(profile_card, text="Profil / Fonctions", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(profile_card, text="Profil", style="CardText.TLabel").grid(row=1, column=0, sticky="w")
        combo = ttk.Combobox(
            profile_card, textvariable=self.profile,
            values=tuple(PROFILES.keys()), state="readonly", width=17, style="Dark.TCombobox"
        )
        combo.grid(row=1, column=1, sticky="w", padx=7)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_profile())

        ttk.Checkbutton(profile_card, text="OSC", variable=self.osc_enabled,
                        style="Dark.TCheckbutton").grid(row=1, column=2, padx=6)
        ttk.Checkbutton(profile_card, text="VIB", variable=self.vib_enabled,
                        style="Dark.TCheckbutton").grid(row=1, column=3, padx=6)
        ttk.Checkbutton(profile_card, text="CON", variable=self.con_enabled,
                        style="Dark.TCheckbutton").grid(row=2, column=2, padx=6, pady=(3,0))
        ttk.Checkbutton(profile_card, text="Plein écran droite", variable=self.fullscreen,
                        style="Dark.TCheckbutton").grid(row=2, column=0, columnspan=2, sticky="w", pady=(3,0))

        # 3 cartes moteur compactes
        settings_row = ttk.Frame(root, style="Root.TFrame")
        settings_row.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for i in range(3):
            settings_row.columnconfigure(i, weight=1, uniform="motor")

        osc = ttk.Frame(settings_row, style="Card.TFrame", padding=(12, 8))
        osc.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ttk.Label(osc, text="OSCILLATION", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(osc, text="Activer", variable=self.osc_enabled,
                        style="Dark.TCheckbutton").grid(row=0, column=2, sticky="e")
        self.add_scale(osc, 1, "Min", self.osc_min, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(osc, 2, "Max", self.osc_max, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(osc, 3, "Amp", self.osc_amp, 25, 150, lambda v: f"{float(v):.0f}%")
        self.add_scale(osc, 4, "Transition", self.osc_transition_ms, 200, 4000,
                       lambda v: f"{int(float(v))}ms")
        ttk.Checkbutton(
            osc, text="Mode ultra-lent par impulsions",
            variable=self.osc_pulse_enabled, style="Dark.TCheckbutton"
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 1))
        self.add_scale(osc, 6, "Impulsion ON", self.osc_pulse_on_ms, 100, 1000,
                       lambda v: f"{int(float(v))}ms")
        self.add_scale(osc, 7, "Pause OFF", self.osc_pulse_off_ms, 100, 3000,
                       lambda v: f"{int(float(v))}ms")
        self.add_scale(osc, 8, "Force impulsion", self.osc_pulse_force, 10, 100,
                       lambda v: f"{float(v):.0f}%")
        ttk.Label(osc, textvariable=self.live_osc, style="Value.TLabel").grid(
            row=9, column=1, sticky="w", padx=7, pady=(3, 0))

        vib = ttk.Frame(settings_row, style="Card.TFrame", padding=(12, 8))
        vib.grid(row=0, column=1, sticky="nsew", padx=4)
        ttk.Label(vib, text="VIBRATION", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(vib, text="Activer", variable=self.vib_enabled,
                        style="Dark.TCheckbutton").grid(row=0, column=2, sticky="e")
        self.add_scale(vib, 1, "Min", self.vib_min, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 2, "Max", self.vib_max, 0, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 3, "Amp", self.vib_amp, 25, 150, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 4, "Liss.", self.smoothing, 0, 90, lambda v: f"{float(v):.0f}%")
        self.add_scale(vib, 5, "Transition", self.vib_transition_ms, 150, 2500,
                       lambda v: f"{int(float(v))}ms")
        self.add_scale(vib, 6, "Durée VIB", self.vib_duration_ms, 150, 3000,
                       lambda v: f"{int(float(v))}ms")
        ttk.Label(vib, text="Pattern", style="CardText.TLabel").grid(
            row=7, column=0, sticky="w", pady=2)
        ttk.Combobox(vib, textvariable=self.vib_pattern, values=VIB_PATTERNS,
                     state="readonly", width=20, style="Dark.TCombobox").grid(
            row=7, column=1, columnspan=2, sticky="ew", padx=7, pady=2)
        ttk.Label(vib, textvariable=self.live_vib, style="Value.TLabel").grid(
            row=8, column=1, sticky="w", padx=7, pady=(3, 0))

        con = ttk.Frame(settings_row, style="Card.TFrame", padding=(12, 8))
        con.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        ttk.Label(con, text="CONSTRICTION", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(con, text="Activer", variable=self.con_enabled,
                        style="Dark.TCheckbutton").grid(row=0, column=2, sticky="e")
        self.add_scale(con, 1, "Niv.", self.con_level, 1, 5, lambda v: f"{int(float(v))}")
        self.add_scale(con, 2, "Seuil", self.con_threshold, 40, 100, lambda v: f"{float(v):.0f}%")
        self.add_scale(con, 3, "Durée pompage", self.con_duration, 150, 1200,
                       lambda v: f"{int(float(v))}ms")
        ttk.Label(con, text="Pattern", style="CardText.TLabel").grid(
            row=4, column=0, sticky="w", pady=2)
        ttk.Combobox(con, textvariable=self.con_pattern, values=CON_PATTERNS,
                     state="readonly", width=20, style="Dark.TCombobox").grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=7, pady=2)
        ttk.Label(con, textvariable=self.live_con, style="Value.TLabel").grid(
            row=5, column=1, sticky="w", padx=7, pady=(3, 0))

        # Ligne options ultra compacte
        opts = ttk.Frame(root, style="Card.TFrame", padding=(12, 6))
        opts.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(opts, text="Vidéo suivante auto", variable=self.play_next,
                        style="Dark.TCheckbutton").pack(side="left", padx=(0, 15))
        ttk.Checkbutton(opts, text="Supprimer après lecture", variable=self.delete_after,
                        style="Dark.TCheckbutton").pack(side="left")

        # Actions sur une seule ligne
        actions = ttk.Frame(root, style="Root.TFrame")
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text="▶ Lancer", command=self.play,
                   style="Accent.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(actions, text="⏩ Suivante", command=self.next_video,
                   style="Secondary.TButton").grid(row=0, column=1, padx=5)
        ttk.Button(actions, text="Test OSC", command=lambda: self.test("osc"),
                   style="Secondary.TButton").grid(row=0, column=2, padx=5)
        ttk.Button(actions, text="Test VIB", command=lambda: self.test("vib"),
                   style="Secondary.TButton").grid(row=0, column=3, padx=5)
        ttk.Button(actions, text="Test CON", command=lambda: self.test("con"),
                   style="Secondary.TButton").grid(row=0, column=4, padx=5)
        ttk.Button(actions, text="■ STOP", command=self.stop,
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
        self.graph_info.configure(text=f"{len(self.graph_actions)} points")
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
                text="Choisis une vidéo avec son funscript",
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
        self.live_con.set("ACTIF" if con_active else "INACTIF")
        self.draw_playhead_only()

    def set_status(self, text, kind=None):
        self.status_text.set(text)

    def apply_profile(self):
        data = PROFILES.get(self.profile.get())
        if not data:
            return
        for k, v in data.items():
            getattr(self, k).set(v)
        self.status_text.set(f"Profil {self.profile.get()} appliqué.")

    def _set_current_video(self, video, warn_missing=True):
        video = Path(video)
        self.current_video = video
        self.video_path.set(str(video))
        script = find_script(video)
        if script:
            self.current_script = script
            self.script_path.set(f"✓  {script}")
            self.load_funscript_graph(script)
            self.status_text.set(f"Prêt : {video.name}")
            return True

        self.current_script = None
        self.script_path.set("✕  Aucun funscript correspondant")
        self.graph_actions = []
        self.graph_duration_ms = 0
        self.draw_funscript_graph()
        if warn_missing:
            messagebox.showwarning(
                "Funscript introuvable",
                f"Aucun funscript portant le même nom que :\n{video.name}"
            )
        return False

    def _scan_playlist_folder(self, folder):
        folder = Path(folder)
        self.playlist_dir = folder
        self.playlist_videos = sorted(
            [p for p in folder.iterdir()
             if p.is_file() and p.suffix.casefold() in VIDEO_EXTENSIONS],
            key=natural_key
        )
        return self.playlist_videos

    def choose_folder(self):
        folder = filedialog.askdirectory(
            initialdir=str(
                self.playlist_dir
                if self.playlist_dir and Path(self.playlist_dir).is_dir()
                else VIDEO_DIR if VIDEO_DIR.is_dir()
                else Path.home()
            ),
            title="Choisir un dossier complet de vidéos",
        )
        if not folder:
            return

        videos = self._scan_playlist_folder(folder)
        if not videos:
            messagebox.showinfo("Dossier vide", "Aucune vidéo compatible dans ce dossier.")
            return

        # Prendre la première vidéo qui possède un funscript.
        for video in videos:
            if find_script(video):
                self._set_current_video(video, warn_missing=False)
                self.status_text.set(
                    f"Dossier chargé — {len(videos)} vidéo(s) — première : {video.name}"
                )
                self.save_config()
                return

        messagebox.showwarning(
            "Aucun funscript",
            "Des vidéos ont été trouvées, mais aucune n'a de funscript correspondant."
        )

    def choose_video(self):
        p = filedialog.askopenfilename(
            initialdir=str(VIDEO_DIR if VIDEO_DIR.is_dir() else Path.home()),
            filetypes=[("Vidéos", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v"), ("Tous", "*")]
        )
        if not p:
            return
        video = Path(p)
        self._scan_playlist_folder(video.parent)
        self._set_current_video(video)
        self.save_config()

    def play(self):
        video = self.current_video or (Path(self.video_path.get()) if self.video_path.get() else None)
        script_text = self.script_path.get().removeprefix("✓  ").strip()
        script = self.current_script or (Path(script_text) if script_text else None)
        if not video or not script or not video.is_file() or not script.is_file():
            messagebox.showerror("Fichier introuvable", "Choisis une vidéo et son funscript.")
            return
        if self.osc_min.get() > self.osc_max.get() or self.vib_min.get() > self.vib_max.get():
            messagebox.showerror("Réglages invalides", "Les minimums doivent être inférieurs aux maximums.")
            return

        self.current_video, self.current_script = video, script
        self.load_funscript_graph(script)
        self.save_config()
        self.worker.start(video, script, self.settings())
        self.status_text.set("Connexion BLE et lancement de MPV sur l’écran de droite…")

    def stop(self):
        self.worker.stop()
        self.live_osc.set("0 %")
        self.live_vib.set("0 %")
        self.live_con.set("INACTIF")
        self.graph_position_ms = 0
        self.draw_funscript_graph()
        self.status_text.set("Arrêt demandé.")

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
        self.status_text.set(f"Test {which.upper()} pendant 2 secondes…")

    def on_natural_end(self):
        if self.play_next.get():
            self.next_video(auto=True, delete_current=True)
        else:
            old_video, old_script = self.current_video, self.current_script
            self.worker.stop()
            self._delete_completed_media(old_video, old_script)
            self.status_text.set("Lecture terminée.")
            self.save_config()

    def _delete_completed_media(self, video, script):
        """Supprime la vidéo lue et son funscript si l'option est activée."""
        if not self.delete_after.get():
            return
        errors = []
        for p in (video, script):
            try:
                if p and Path(p).is_file():
                    Path(p).unlink()
            except OSError as exc:
                errors.append(f"{Path(p).name}: {exc}")
        if errors:
            self.status_text.set("Suppression partielle : " + " | ".join(errors))

    def next_video(self, auto=False, delete_current=True):
        old_video, old_script = self.current_video, self.current_script
        if not old_video or not old_video.parent.is_dir():
            self.status_text.set("Aucune vidéo actuelle.")
            return

        # La liste est prise AVANT suppression pour connaître le successeur.
        folder = self.playlist_dir if self.playlist_dir and Path(self.playlist_dir).is_dir() else old_video.parent
        videos = self._scan_playlist_folder(folder)

        try:
            idx = videos.index(old_video)
            ordered = videos[idx + 1:] + videos[:idx]
        except ValueError:
            ordered = videos

        candidate_pair = None
        for candidate in ordered:
            if candidate == old_video:
                continue
            script = find_script(candidate)
            if script:
                candidate_pair = (candidate, script)
                break

        self.worker.stop()

        # v1.5.0 : passage manuel OU fin naturelle = suppression automatique
        # lorsque "Supprimer après lecture" est coché.
        if delete_current:
            self._delete_completed_media(old_video, old_script)

        if candidate_pair:
            candidate, script = candidate_pair
            self.current_video = candidate
            self.current_script = script
            self.video_path.set(str(candidate))
            self.script_path.set(f"✓  {script}")
            self.load_funscript_graph(script)
            self.status_text.set(f"Vidéo suivante : {candidate.name}")
            self.save_config()
            if auto:
                self.after(1200, self.play)
            return

        self.current_video = None
        self.current_script = None
        self.video_path.set("")
        self.script_path.set("Fin du dossier")
        self.graph_actions = []
        self.graph_duration_ms = 0
        self.draw_funscript_graph()
        self.status_text.set("Fin du dossier / playlist.")
        self.save_config()

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
        }

    def save_config(self):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        data = self.settings() | {
            "profile": self.profile.get(),
            "play_next": self.play_next.get(),
            "video": self.video_path.get(),
            "script": self.script_path.get(),
            "playlist_dir": str(self.playlist_dir) if self.playlist_dir else "",
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

        folder_text = data.get("playlist_dir", "")
        if folder_text and Path(folder_text).is_dir():
            self.playlist_dir = Path(folder_text)
            try:
                self._scan_playlist_folder(self.playlist_dir)
            except Exception:
                self.playlist_videos = []

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
