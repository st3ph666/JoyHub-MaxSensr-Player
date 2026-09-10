"""Device, path, profile, and application constants."""

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

APP_VERSION = "v1.4.16-PersistentResume"

APP_NAME = f"JoyHub MaxSensr Funscript Player {APP_VERSION}"

VIDEO_DIR = Path("/media/Video2/ABCD/")

SCRIPT_DIR = VIDEO_DIR / "Funscript"

CONFIG = Path.home() / ".config/maxsensr-player-gui.json"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}

DEVICE_NAME = "J-MaxSensr"

WRITE_UUID = "0000ffa1-0000-1000-8000-00805f9b34fb"

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
        "video": "Vidéo", "browse": "Parcourir", "folder": "Dossier", "script": "Script", "profile_functions": "Profil / Fonctions",
        "profile": "Profil", "language": "Langue", "fullscreen": "Plein écran droite", "enable": "Activer",
        "ultraslow": "Mode ultra-lent par impulsions", "transition": "Transition", "pulse_on": "Impulsion ON",
        "pause_off": "Pause OFF", "pulse_force": "Force impulsion", "smoothing": "Liss.",
        "vib_duration": "Durée VIB", "pattern": "Pattern", "level": "Niv.", "threshold": "Seuil",
        "pump_duration": "Durée pompage", "auto_next": "Vidéo suivante auto", "delete_after": "Supprimer après lecture", "resume_video": "Reprendre la vidéo où elle a été arrêtée",
        "play": "▶ Lancer", "next": "⏩ Suivante", "test_osc": "Test OSC", "test_vib": "Test VIB",
        "test_con": "Test CON", "stop": "■ STOP", "choose_video_graph": "Choisis une vidéo avec son funscript",
        "no_script": "Aucun script sélectionné", "choose_start": "Choisis une vidéo pour commencer.",
        "inactive": "INACTIF", "active": "ACTIF",
    },
    "en": {
        "graph_title": "MAIN FUNSCRIPT — full timeline", "point": "point", "points": "points",
        "subtitle": "Direct BLE • Osc + Vib + Con • MPV on right screen", "video_script": "Video / Funscript",
        "video": "Video", "browse": "Browse", "folder": "Folder", "script": "Script", "profile_functions": "Profile / Functions",
        "profile": "Profile", "language": "Language", "fullscreen": "Fullscreen right screen", "enable": "Enable",
        "ultraslow": "Ultra-slow pulse mode", "transition": "Transition", "pulse_on": "Pulse ON",
        "pause_off": "Pause OFF", "pulse_force": "Pulse force", "smoothing": "Smooth",
        "vib_duration": "VIB duration", "pattern": "Pattern", "level": "Level", "threshold": "Threshold",
        "pump_duration": "Pump duration", "auto_next": "Auto play next video", "delete_after": "Delete after playback", "resume_video": "Resume video where it was stopped",
        "play": "▶ Play", "next": "⏩ Next", "test_osc": "Test OSC", "test_vib": "Test VIB",
        "test_con": "Test CON", "stop": "■ STOP", "choose_video_graph": "Choose a video with its funscript",
        "no_script": "No script selected", "choose_start": "Choose a video to begin.",
        "inactive": "INACTIVE", "active": "ACTIVE",
    },
}
