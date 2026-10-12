"""
KDE Plasma Wayland screenshot via Spectacle.
Uses fullscreen capture and splits by monitor geometry.
"""
import json
import os
import subprocess
import tempfile
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from PIL import Image


def _run(cmd: List[str], timeout: float = 5.0) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def take_screenshot_kde_fullscreen() -> Optional[np.ndarray]:
    """
    Take fullscreen screenshot on KDE Plasma Wayland using Spectacle.
    Captures all monitors as one image.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    
    try:
        # Use spectacle with fullscreen capture in background mode
        # -f = fullscreen (all monitors)
        # -b = background (no GUI)
        # -n = no notification
        # -o = output file
        result = subprocess.run(
            ["spectacle", "-f", "-b", "-n", "-o", tmp_path],
            capture_output=True,
            timeout=5
        )
        
        if result.returncode != 0:
            print(f"[kde] spectacle fullscreen failed: {result.stderr}")
            return None
        
        img = Image.open(tmp_path).convert("RGB")
        return np.array(img)
        
    except subprocess.TimeoutExpired:
        print("[kde] spectacle timeout")
        return None
    except Exception as e:
        print(f"[kde] screenshot failed: {e!r}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def split_screenshot_by_monitors(full_image: np.ndarray, monitors: List[Dict[str, Any]]) -> List[Tuple[np.ndarray, int, Dict[str, Any]]]:
    """
    Split a fullscreen screenshot into per-monitor images based on monitor geometries.
    """
    screenshots = []
    
    if not monitors:
        monitors = [{"left": 0, "top": 0, "width": full_image.shape[1], "height": full_image.shape[0], "name": "Default", "id": 0}]
    
    for idx, mon in enumerate(monitors):
        left = mon.get("left", 0)
        top = mon.get("top", 0)
        width = mon.get("width", 0)
        height = mon.get("height", 0)
        
        # Skip invalid geometries
        if width <= 0 or height <= 0:
            continue
        
        # Crop the full image to this monitor's region
        # numpy array is [height, width, channels]
        cropped = full_image[top:top+height, left:left+width]
        
        if cropped.size == 0:
            continue
        
        monitor_info = {
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }
        
        screenshots.append((cropped, idx, monitor_info))
    
    return screenshots


def take_screenshot_kde_multi() -> List[Tuple[np.ndarray, int, Dict[str, Any]]]:
    """Take screenshots of all monitors using Spectacle fullscreen + split."""
    from .kde import get_monitors_kde
    
    monitors = get_monitors_kde()
    if not monitors:
        monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080, "name": "Default", "id": 0}]
    
    # Capture fullscreen
    full_image = take_screenshot_kde_fullscreen()
    if full_image is None:
        return []
    
    # Split by monitor geometries
    return split_screenshot_by_monitors(full_image, monitors)