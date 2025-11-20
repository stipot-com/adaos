from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import json
import urllib.request


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def get_file_path(token: str, file_id: str) -> Optional[str]:
    """Resolve Telegram internal file path by file_id via getFile API."""
    url = _api_url(token, "getFile") + f"?file_id={file_id}"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        return None
    return (data.get("result") or {}).get("file_path")


def download_file(token: str, file_path: str, dest_dir: str | Path) -> Path:
    """Download a Telegram file to dest_dir. Returns local path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = Path(file_path).name
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    tmp_path = dest_dir / f".{fname}.part"
    final_path = dest_dir / fname
    with urllib.request.urlopen(url) as resp, open(tmp_path, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp_path.replace(final_path)
    return final_path


def convert_opus_to_wav16k(src_path: str | Path, dst_path: str | Path) -> bool:
    """Convert OGG/OPUS to WAV 16k using ffmpeg if available. Returns True on success."""
    src = str(src_path)
    dst = str(dst_path)
    try:
        # ffmpeg -y -i input.ogg -ar 16000 -ac 1 output.wav
        res = subprocess.run(["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", dst], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and Path(dst).exists()
    except Exception:
        return False

