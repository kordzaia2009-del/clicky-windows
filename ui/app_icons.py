"""
Which app is being opened, its Georgian name, and its real icon.

  resolve(label)  -> AppInfo(name_ka, shortcut) or None
      label is what the autopilot clicks ("Zoom Workplace",
      "Google Chrome - 1 running window", ...). A shortcut is looked up on the
      desktop, in the Start menu and on the taskbar; known apps also get a
      Georgian name (Zoom -> ზუმი).
  icon_for(info)  -> QIcon from the shortcut (Windows supplies the app icon)

Pure except icon_for (needs a QApplication).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Known apps -> how a Georgian speaker says them. Longest key wins.
KA_APP_NAMES = {
    "zoom": "ზუმი",
    "google chrome": "გუგლ ქრომი",
    "chrome": "ქრომი",
    "microsoft edge": "ეჯი",
    "edge": "ეჯი",
    "firefox": "ფაირფოქსი",
    "opera": "ოპერა",
    "word": "ვორდი",
    "excel": "ექსელი",
    "powerpoint": "პაუერპოინტი",
    "outlook": "აუთლუქი",
    "microsoft teams": "თიმსი",
    "teams": "თიმსი",
    "file explorer": "საქაღალდეები",
    "explorer": "საქაღალდეები",
    "this pc": "ეს კომპიუტერი",
    "control panel": "მართვის პანელი",
    "settings": "პარამეტრები",
    "calculator": "კალკულატორი",
    "notepad": "ბლოკნოტი",
    "paint": "პეინტი",
    "photos": "ფოტოები",
    "camera": "კამერა",
    "discord": "დისქორდი",
    "skype": "სკაიპი",
    "viber": "ვაიბერი",
    "whatsapp": "ვოთსაპი",
    "telegram": "ტელეგრამი",
    "messenger": "მესენჯერი",
    "facebook": "ფეისბუქი",
    "youtube": "იუთუბი",
    "gmail": "ჯიმეილი",
    "spotify": "სპოტიფაი",
    "vlc": "ვიელსი",
    "steam": "სტიმი",
    "roblox": "რობლოქსი",
    "grammarly": "გრამარლი",
}

SHORTCUT_EXT = (".lnk", ".url", ".appref-ms", ".exe")

# Taskbar/UIA decorations that are not part of the app's name
_NOISE = re.compile(
    r"\s+-\s+\d+\s+running\s+windows?$|\s+-\s+pinned$|\s+\(\d+\)$|\.(lnk|url|exe)$",
    re.IGNORECASE,
)


@dataclass
class AppInfo:
    label: str                 # cleaned on-screen name
    name_ka: str               # Georgian name (or the label itself)
    shortcut: Optional[str]    # path used for the icon, if found


def clean_label(label: str) -> str:
    return _NOISE.sub("", (label or "").strip()).strip()


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").casefold()).strip()


def georgian_name(label: str) -> Optional[str]:
    """Georgian name of a known app inside `label`, else None."""
    n = f" {_norm(clean_label(label))} "
    for key in sorted(KA_APP_NAMES, key=len, reverse=True):
        if f" {key} " in n:
            return KA_APP_NAMES[key]
    return None


def shortcut_dirs() -> List[Path]:
    env = os.environ
    home = Path(env.get("USERPROFILE", Path.home()))
    appdata = Path(env.get("APPDATA", home / "AppData" / "Roaming"))
    programdata = Path(env.get("PROGRAMDATA", r"C:\ProgramData"))
    public = Path(env.get("PUBLIC", r"C:\Users\Public"))
    return [
        home / "Desktop", home / "OneDrive" / "Desktop", public / "Desktop",
        appdata / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar",
        appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        programdata / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]


_index_cache: Optional[List[Tuple[str, str]]] = None


def _index(dirs: Optional[Iterable[Path]] = None) -> List[Tuple[str, str]]:
    """[(normalized stem, path)] of every shortcut, searched once."""
    global _index_cache
    if dirs is None and _index_cache is not None:
        return _index_cache
    out = []
    for d in (dirs if dirs is not None else shortcut_dirs()):
        try:
            if not Path(d).is_dir():
                continue
            for root, _subdirs, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(SHORTCUT_EXT) and "uninstall" not in f.lower():
                        out.append((_norm(Path(f).stem), str(Path(root) / f)))
        except Exception:
            continue
    if dirs is None:
        _index_cache = out
    return out


def find_shortcut(label: str, dirs: Optional[Iterable[Path]] = None) -> Optional[str]:
    """Best-matching shortcut for an on-screen app name, or None."""
    want = _norm(clean_label(label))
    if len(want) < 2:
        return None
    best, best_score = None, 0.0
    for stem, path in _index(dirs):
        if not stem:
            continue
        if stem == want:
            score = 1.0
        elif want in stem or stem in want:
            short, long_ = sorted((stem, want), key=len)
            score = 0.6 + 0.35 * len(short) / len(long_) if len(short) >= 3 else 0.0
        else:
            ws, ww = set(stem.split()), set(want.split())
            score = 0.7 * len(ws & ww) / max(len(ww), 1)
        if score > best_score:
            best, best_score = path, score
    return best if best_score >= 0.6 else None


def resolve(label: str, dirs: Optional[Iterable[Path]] = None) -> Optional[AppInfo]:
    """An app behind this label (shortcut found or a known app), else None."""
    cleaned = clean_label(label)
    if not cleaned:
        return None
    ka = georgian_name(cleaned)
    shortcut = find_shortcut(cleaned, dirs)
    if ka is None and shortcut is None:
        return None
    return AppInfo(label=cleaned, name_ka=ka or cleaned, shortcut=shortcut)


def icon_for(info: Optional[AppInfo]):
    """QIcon for the app (empty QIcon if unknown). GUI thread only."""
    from PyQt6.QtCore import QFileInfo
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QFileIconProvider
    if info is None or not info.shortcut:
        return QIcon()
    try:
        fi = QFileInfo(info.shortcut)
        target = fi.symLinkTarget() if fi.isShortcut() else ""
        # The target program's icon has no shortcut arrow / sync badge.
        if target and QFileInfo(target).exists():
            fi = QFileInfo(target)
        return QFileIconProvider().icon(fi)
    except Exception:
        return QIcon()
