#!/bin/bash
# OpenRecall Linux setup script
# Creates virtual environment and installs dependencies

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT/.venv"

echo "=== OpenRecall Linux Setup ==="
echo "Root: $ROOT"
echo "Venv: $VENV_DIR"

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Install it first (e.g., sudo pacman -S python)"
    exit 1
fi

python3 --version

# Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists"
fi

# Upgrade pip
echo "Upgrading pip..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip

# Install core dependencies (compatible with Python 3.14)
echo "Installing core dependencies..."
"$VENV_DIR/bin/python" -m pip install \
    Flask==3.1.0 \
    numpy \
    mss==9.0.1 \
    sentence-transformers==3.0.0 \
    torch==2.14.0 \
    torchvision==0.29.0 \
    shapely>=2.1.0 \
    h5py==3.11.0 \
    rapidfuzz==3.9.3 \
    Pillow==10.3.0 \
    psutil \
    pystray \
    python-doctr@git+https://github.com/koenvaneijk/doctr.git@af711bc04eb8876a7189923fb51ec44481ee18cd

# Linux-specific dependencies. These bindings (dbus/atspi) frequently fail to
# build; they are OPTIONAL — the app falls back to the Gio backend — so don't
# let a failure abort the whole setup.
echo "Installing optional Linux dependencies..."
"$VENV_DIR/bin/python" -m pip install dbus-python pyatspi2 jeepney \
    || echo "  (optional dbus/atspi bindings unavailable — using the Gio backend instead)"

# Check for system dependencies
echo "Checking system dependencies..."
MISSING=()

command -v xrandr >/dev/null || MISSING+=("xorg-xrandr")
command -v wmctrl >/dev/null || MISSING+=("wmctrl")
command -v xprop >/dev/null || MISSING+=("xorg-xprop")
command -v xprintidle >/dev/null || MISSING+=("xprintidle")
command -v xdg-open >/dev/null || MISSING+=("xdg-utils")
command -v notify-send >/dev/null || MISSING+=("libnotify")
command -v qdbus >/dev/null || MISSING+=("qttools")
command -v kwriteconfig6 >/dev/null || MISSING+=("kconfig")
command -v pw-cli >/dev/null || MISSING+=("pipewire")
command -v journalctl >/dev/null || MISSING+=("systemd")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Missing optional system packages: ${MISSING[*]}"
    echo "Install with: sudo pacman -S ${MISSING[*]}"
fi

# Create launcher scripts
echo "Creating launcher scripts..."

cat > "$ROOT/tray.sh" << 'SCRIPTEOF'
#!/bin/bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
cd "$ROOT"
exec python tray.py
SCRIPTEOF
chmod +x "$ROOT/tray.sh"

cat > "$ROOT/snapshot.sh" << 'SCRIPTEOF'
#!/bin/bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
cd "$ROOT"
exec python snapshot.py "$@"
SCRIPTEOF
chmod +x "$ROOT/snapshot.sh"

cat > "$ROOT/snapshot_loop.sh" << 'SCRIPTEOF'
#!/bin/bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
cd "$ROOT"
exec python snapshot.py --delay 60 "$@"
SCRIPTEOF
chmod +x "$ROOT/snapshot_loop.sh"

cat > "$ROOT/bulk_summary.sh" << 'SCRIPTEOF'
#!/bin/bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
cd "$ROOT"
exec python bulk_summary.py "$@"
SCRIPTEOF
chmod +x "$ROOT/bulk_summary.sh"

cat > "$ROOT/prune.sh" << 'SCRIPTEOF'
#!/bin/bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
cd "$ROOT"
exec python prune.py "$@"
SCRIPTEOF
chmod +x "$ROOT/prune.sh"

cat > "$ROOT/wallpaper_calibrate.sh" << 'SCRIPTEOF'
#!/bin/bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
cd "$ROOT"
exec python wallpaper_calibrate.py "$@"
SCRIPTEOF
chmod +x "$ROOT/wallpaper_calibrate.sh"

# Create systemd user services. Capture and web are SEPARATE so they never
# double-capture (the web service runs with OPENRECALL_DISABLE_CAPTURE=1).
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_DIR/openrecall-capture.service" << SVC_EOF
[Unit]
Description=OpenRecall Screen Capture
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python -u -m openrecall.screenshot
WorkingDirectory=$ROOT
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
# Storage defaults to ~/.local/share/openrecall. To use a custom path, uncomment:
#Environment=OPENRECALL_STORAGE=%h/openrecall
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_SESSION_TYPE=wayland
Environment=XDG_CURRENT_DESKTOP=KDE
Environment=QT_QPA_PLATFORM=wayland
Environment=GDK_BACKEND=wayland
Environment=MOZ_ENABLE_WAYLAND=1

[Install]
WantedBy=default.target
SVC_EOF

cat > "$SERVICE_DIR/openrecall-web.service" << SVC_EOF
[Unit]
Description=OpenRecall Web Server
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python -u -m openrecall.app
WorkingDirectory=$ROOT
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# Storage defaults to ~/.local/share/openrecall. To use a custom path, uncomment:
#Environment=OPENRECALL_STORAGE=%h/openrecall
# Capture is owned by openrecall-capture.service; don't double-write.
Environment=OPENRECALL_DISABLE_CAPTURE=1
# Show even very short activity runs (broad history tracker).
Environment=OPENRECALL_SESSION_MIN_LEN_MINUTES=0

[Install]
WantedBy=default.target
SVC_EOF

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To run OpenRecall (systemd --user):"
echo "  systemctl --user enable --now openrecall-capture.service openrecall-web.service"
echo "  # then open http://localhost:8082"
echo ""
echo "Manual / one-off:"
echo "  $ROOT/snapshot.sh                # one-time capture"
echo "  $ROOT/snapshot_loop.sh           # continuous capture (60s interval)"
echo "  $ROOT/bulk_summary.sh            # run AI summaries"
echo "  $ROOT/prune.sh --dry-run         # preview pruning"
echo "  $ROOT/wallpaper_calibrate.sh     # calibrate wallpaper detection"
echo ""
echo "Optional: $ROOT/tray.sh for a system-tray launcher."
echo "Web UI: http://localhost:8082"
echo "Data directory: ~/.local/share/openrecall (override with OPENRECALL_STORAGE or --storage-path)"
