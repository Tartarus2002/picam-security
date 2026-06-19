#!/bin/bash
# ============================================
# PiCam Security Camera - Master Setup Script
# ============================================
# Run this ONCE after first boot on the Raspberry Pi Zero 2 W.
#
# Prerequisites:
#   - Raspberry Pi OS Lite (Bookworm 64-bit) installed
#   - SSH access working
#   - Internet connection active
#
# Usage:
#   chmod +x setup.sh
#   sudo ./setup.sh
#
# What this script does:
#   1. Updates the system
#   2. Installs dependencies (Python, OpenCV, ffmpeg)
#   3. Downloads and installs mediamtx (RTSP streaming server)
#   4. Configures the camera
#   5. Sets up project directories
#   6. Installs and enables systemd services
#   7. Configures WiFi (if not already done)
#   8. Sets up cron jobs for maintenance
#   9. Configures firewall basics
#
# Total time: ~15-25 minutes on Pi Zero 2 W
# ============================================

set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- Variables ---
PICAM_USER="pi"
PICAM_HOME="/home/${PICAM_USER}"
PICAM_DIR="${PICAM_HOME}/picam"
RECORDINGS_DIR="${PICAM_HOME}/picam-recordings"
LOGS_DIR="${PICAM_HOME}/picam-logs"
MEDIAMTX_VERSION="1.16.3"

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
    aarch64) MEDIAMTX_ARCH="arm64" ;;
    armv7l)  MEDIAMTX_ARCH="armv7" ;;
    armv6l)  MEDIAMTX_ARCH="armv6" ;;
    *)       echo -e "${RED}Unsupported architecture: $ARCH${NC}"; exit 1 ;;
esac

# --- Helper Functions ---
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

step() {
    STEP_NUM=$((${STEP_NUM:-0} + 1))
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN} Step ${STEP_NUM}: $1${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# --- Pre-flight Checks ---
if [ "$(id -u)" -ne 0 ]; then
    error "This script must be run as root (use sudo)"
fi

if ! ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
    error "No internet connection. Please connect to WiFi first."
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   PiCam Security Camera System - Setup Installer  ║${NC}"
echo -e "${GREEN}║   Raspberry Pi Zero 2 W                           ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Architecture: $ARCH ($MEDIAMTX_ARCH)"
echo "Target directory: $PICAM_DIR"
echo ""


# ============================================
# STEP 1: System Update
# ============================================
step "Updating system packages"

apt-get update -y
apt-get upgrade -y
success "System updated"


# ============================================
# STEP 2: Install Dependencies
# ============================================
step "Installing dependencies"

apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-opencv \
    python3-numpy \
    python3-picamera2 \
    ffmpeg \
    libcamera-apps \
    curl \
    jq \
    ufw

success "Dependencies installed"


# ============================================
# STEP 3: Setup Project Directories
# ============================================
step "Setting up project directories"

# Create main directories
mkdir -p "$PICAM_DIR"/{bin,config,services,scripts,motion-detect}
mkdir -p "$RECORDINGS_DIR"/{snapshots,clips,events}
mkdir -p "$LOGS_DIR"

# Copy project files (assumes this script is run from the project directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Copy configuration
cp "$SCRIPT_DIR/config/picam.conf" "$PICAM_DIR/config/"
cp "$SCRIPT_DIR/config/mediamtx.yml" "$PICAM_DIR/config/"

# Copy motion detector
cp "$SCRIPT_DIR/motion-detect/motion_detector.py" "$PICAM_DIR/motion-detect/"

# Copy scripts
cp "$SCRIPT_DIR/scripts/"*.sh "$PICAM_DIR/scripts/"
chmod +x "$PICAM_DIR/scripts/"*.sh

# Set ownership
chown -R "${PICAM_USER}:${PICAM_USER}" "$PICAM_DIR" "$RECORDINGS_DIR" "$LOGS_DIR"

success "Project directories created"


# ============================================
# STEP 4: Install mediamtx
# ============================================
step "Installing mediamtx v${MEDIAMTX_VERSION} (RTSP/WebRTC streaming server)"

MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${MEDIAMTX_ARCH}.tar.gz"
MEDIAMTX_TMP="/tmp/mediamtx.tar.gz"

info "Downloading from: $MEDIAMTX_URL"
curl -fSL "$MEDIAMTX_URL" -o "$MEDIAMTX_TMP"

# Extract to bin directory
tar -xzf "$MEDIAMTX_TMP" -C "$PICAM_DIR/bin/" mediamtx
chmod +x "$PICAM_DIR/bin/mediamtx"
rm -f "$MEDIAMTX_TMP"

# Verify
"$PICAM_DIR/bin/mediamtx" --help > /dev/null 2>&1 || warn "mediamtx binary check failed (may need different arch)"

chown "${PICAM_USER}:${PICAM_USER}" "$PICAM_DIR/bin/mediamtx"
success "mediamtx installed to $PICAM_DIR/bin/mediamtx"


# ============================================
# STEP 5: Configure Camera
# ============================================
step "Configuring camera"

# Ensure camera is enabled in boot config
if ! grep -q "camera_auto_detect=1" /boot/firmware/config.txt 2>/dev/null; then
    echo "" >> /boot/firmware/config.txt
    echo "# PiCam camera config" >> /boot/firmware/config.txt
    echo "camera_auto_detect=1" >> /boot/firmware/config.txt
    echo "gpu_mem=128" >> /boot/firmware/config.txt
    info "Camera config added to /boot/firmware/config.txt"
fi

# Add pi user to video group (for camera access)
usermod -aG video "$PICAM_USER" 2>/dev/null || true

# Quick camera test
if libcamera-hello --list-cameras 2>/dev/null | grep -q "Available cameras"; then
    success "Camera detected!"
else
    warn "Camera not detected yet. May need a reboot. Continuing setup..."
fi


# ============================================
# STEP 6: Install Python Dependencies
# ============================================
step "Installing Python dependencies"

# Create virtual environment for motion detector
su - "$PICAM_USER" -c "python3 -m venv $PICAM_DIR/venv --system-site-packages"

# Install additional Python packages in venv
su - "$PICAM_USER" -c "$PICAM_DIR/venv/bin/pip install --upgrade pip"

# opencv and numpy are already installed system-wide via apt
# picamera2 is also system-wide
success "Python environment ready"

# Update motion service to use venv Python
sed -i "s|/usr/bin/python3|$PICAM_DIR/venv/bin/python3|" "$SCRIPT_DIR/services/picam-motion.service" 2>/dev/null || true


# ============================================
# STEP 7: Install systemd Services
# ============================================
step "Installing systemd services"

# Copy service files
cp "$SCRIPT_DIR/services/picam-stream.service" /etc/systemd/system/
cp "$SCRIPT_DIR/services/picam-motion.service" /etc/systemd/system/
cp "$SCRIPT_DIR/services/picam-watchdog.service" /etc/systemd/system/
cp "$SCRIPT_DIR/services/picam-watchdog.timer" /etc/systemd/system/

# Update motion service to use venv
sed -i "s|/usr/bin/python3|$PICAM_DIR/venv/bin/python3|" /etc/systemd/system/picam-motion.service

# Reload systemd
systemctl daemon-reload

# Enable services (start on boot)
systemctl enable picam-stream.service
systemctl enable picam-motion.service
systemctl enable picam-watchdog.timer

success "Services installed and enabled"

# Start services
info "Starting services..."
systemctl start picam-stream.service
sleep 5
systemctl start picam-motion.service
systemctl start picam-watchdog.timer

# Check status
if systemctl is-active --quiet picam-stream; then
    success "Streaming service: RUNNING"
else
    warn "Streaming service failed to start (may need reboot for camera)"
fi

if systemctl is-active --quiet picam-motion; then
    success "Motion detection: RUNNING"
else
    warn "Motion detection failed to start (depends on streaming service)"
fi


# ============================================
# STEP 8: Setup Cron Jobs
# ============================================
step "Setting up maintenance cron jobs"

# Add storage cleanup cron (runs every hour as pi user)
CRON_LINE="0 * * * * /home/pi/picam/scripts/storage-cleanup.sh"
(crontab -u "$PICAM_USER" -l 2>/dev/null | grep -v "storage-cleanup"; echo "$CRON_LINE") | crontab -u "$PICAM_USER" -

# Add daily log rotation
LOG_CRON="0 3 * * * find /home/pi/picam-logs -name '*.log' -size +50M -exec truncate -s 10M {} \\;"
(crontab -u "$PICAM_USER" -l 2>/dev/null | grep -v "picam-logs.*truncate"; echo "$LOG_CRON") | crontab -u "$PICAM_USER" -

success "Cron jobs configured"


# ============================================
# STEP 9: Configure Hostname
# ============================================
step "Setting hostname"

hostnamectl set-hostname picam
if ! grep -q "picam" /etc/hosts; then
    sed -i 's/127.0.1.1.*/127.0.1.1\tpicam/' /etc/hosts
fi
success "Hostname set to 'picam'"


# ============================================
# STEP 10: Configure Basic Firewall
# ============================================
step "Configuring firewall"

ufw default deny incoming
ufw default allow outgoing
ufw allow ssh                  # Port 22
ufw allow 8554/tcp             # RTSP
ufw allow 8888/tcp             # HLS web viewer
ufw allow 8889/tcp             # WebRTC
ufw allow 8889/udp             # WebRTC UDP
ufw --force enable

success "Firewall configured (SSH + streaming ports open)"


# ============================================
# STEP 11: Create Convenience Scripts
# ============================================
step "Creating convenience scripts"

# Quick status check script
cat > "$PICAM_DIR/scripts/status.sh" << 'STATUSEOF'
#!/bin/bash
echo "╔════════════════════════════════════════╗"
echo "║     PiCam System Status                ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Services
echo "--- Services ---"
for svc in picam-stream picam-motion; do
    status=$(systemctl is-active $svc 2>/dev/null || echo "inactive")
    if [ "$status" = "active" ]; then
        echo "  $svc: RUNNING"
    else
        echo "  $svc: STOPPED"
    fi
done
echo ""

# Network
IP=$(hostname -I | awk '{print $1}')
echo "--- Network ---"
echo "  IP Address: $IP"
echo "  RTSP URL:   rtsp://$IP:8554/cam"
echo "  Web View:   http://$IP:8889/cam"
echo "  HLS View:   http://$IP:8888/cam"
echo ""

# Storage
echo "--- Storage ---"
USED=$(du -sh /home/pi/picam-recordings 2>/dev/null | awk '{print $1}')
FREE=$(df -h /home/pi --output=avail | tail -1 | tr -d ' ')
SNAPS=$(find /home/pi/picam-recordings/snapshots -name "*.jpg" 2>/dev/null | wc -l)
CLIPS=$(find /home/pi/picam-recordings/clips -name "*.mp4" 2>/dev/null | wc -l)
echo "  Recordings: $USED used, $FREE free"
echo "  Snapshots:  $SNAPS"
echo "  Video clips: $CLIPS"
echo ""

# System
echo "--- System ---"
TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -oP '[0-9.]+' || echo "N/A")
UPTIME=$(uptime -p)
MEM=$(free -m | awk '/Mem:/{printf "%dMB / %dMB", $3, $2}')
echo "  Temperature: ${TEMP}°C"
echo "  Memory: $MEM"
echo "  Uptime: $UPTIME"
STATUSEOF

chmod +x "$PICAM_DIR/scripts/status.sh"

# Quick service management
cat > "$PICAM_DIR/scripts/picam-ctl.sh" << 'CTLEOF'
#!/bin/bash
# PiCam control script
# Usage: picam-ctl {start|stop|restart|status|logs}

case "$1" in
    start)
        sudo systemctl start picam-stream picam-motion
        echo "PiCam services started"
        ;;
    stop)
        sudo systemctl stop picam-motion picam-stream
        echo "PiCam services stopped"
        ;;
    restart)
        sudo systemctl restart picam-stream
        sleep 3
        sudo systemctl restart picam-motion
        echo "PiCam services restarted"
        ;;
    status)
        /home/pi/picam/scripts/status.sh
        ;;
    logs)
        SERVICE="${2:-picam-stream}"
        journalctl -u "$SERVICE" -f --no-pager
        ;;
    *)
        echo "Usage: picam-ctl {start|stop|restart|status|logs [service]}"
        exit 1
        ;;
esac
CTLEOF

chmod +x "$PICAM_DIR/scripts/picam-ctl.sh"

# Add alias to bashrc
if ! grep -q "picam-ctl" "$PICAM_HOME/.bashrc" 2>/dev/null; then
    echo "" >> "$PICAM_HOME/.bashrc"
    echo "# PiCam aliases" >> "$PICAM_HOME/.bashrc"
    echo "alias picam-ctl='/home/pi/picam/scripts/picam-ctl.sh'" >> "$PICAM_HOME/.bashrc"
    echo "alias picam-status='/home/pi/picam/scripts/status.sh'" >> "$PICAM_HOME/.bashrc"
fi

chown -R "${PICAM_USER}:${PICAM_USER}" "$PICAM_DIR"
success "Convenience scripts installed"


# ============================================
# DONE!
# ============================================
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}║   PiCam Setup Complete!                                ║${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}╠════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║                                                        ║${NC}"

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
IP=${IP:-"<your-pi-ip>"}

echo -e "${GREEN}║   View your camera:                                    ║${NC}"
echo -e "${BLUE}║   Browser:  http://${IP}:8889/cam         ${NC}"
echo -e "${BLUE}║   RTSP:     rtsp://${IP}:8554/cam         ${NC}"
echo -e "${BLUE}║   HLS:      http://${IP}:8888/cam         ${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}║   Commands:                                             ║${NC}"
echo -e "${BLUE}║   picam-status     - Show system status                ${NC}"
echo -e "${BLUE}║   picam-ctl start  - Start camera services             ${NC}"
echo -e "${BLUE}║   picam-ctl stop   - Stop camera services              ${NC}"
echo -e "${BLUE}║   picam-ctl logs   - View live logs                    ${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}║   Recordings: $RECORDINGS_DIR     ${NC}"
echo -e "${GREEN}║   Config:     $PICAM_DIR/config/picam.conf${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${YELLOW}║   NOTE: Reboot recommended for camera detection.       ║${NC}"
echo -e "${YELLOW}║   Run: sudo reboot                                     ║${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""
