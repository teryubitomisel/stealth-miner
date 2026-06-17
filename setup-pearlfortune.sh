#!/bin/bash
# PearlFortune Miner Setup Script
# Usage: ./setup-pearlfortune.sh <IP> <WORKER_NAME> [OPTIONS]
#
# Examples:
#   ./setup-pearlfortune.sh 38.128.233.43 pf-h100-node16
#   ./setup-pearlfortune.sh 38.128.233.43 pf-h100-node16 -w prl1xxx... -p global.pearlfortune.org:443

set -euo pipefail

# ─── Defaults ──────────────────────────────────────────────
SSH_KEY="${SSH_KEY:-/root/.ssh/id_stealth}"
SSH_USER="${SSH_USER:-ubuntu}"
PROXY="${PROXY:-global.pearlfortune.org:443}"
WALLET="${WALLET:-prl1pf2k2rw6e7ud40jkrwye2kfur06g3cxwuj654hls5psh5tt2dajcqp280tj}"
IMAGE="${IMAGE:-pearlfortune/pearl-miner:v1.1.5}"
CONTAINER_NAME="${CONTAINER_NAME:-ml-training-worker}"
REBOOT_WAIT="${REBOOT_WAIT:-90}"

# ─── Parse args ────────────────────────────────────────────
IP=""
WORKER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -w|--wallet)  WALLET="$2"; shift 2 ;;
        -p|--proxy)   PROXY="$2"; shift 2 ;;
        -k|--key)     SSH_KEY="$2"; shift 2 ;;
        -u|--user)    SSH_USER="$2"; shift 2 ;;
        -i|--image)   IMAGE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 <IP> <WORKER_NAME> [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -w, --wallet ADDR   PRL wallet address"
            echo "  -p, --proxy  HOST   PearlFortune proxy (default: global.pearlfortune.org:443)"
            echo "  -k, --key    PATH   SSH private key (default: /root/.ssh/id_stealth)"
            echo "  -u, --user   USER   SSH user (default: ubuntu)"
            echo "  -i, --image  IMG    Docker image (default: pearlfortune/pearl-miner:v1.1.5)"
            exit 0
            ;;
        -*)
            echo "Unknown option: $1"
            exit 1
            ;;
        *)
            if [[ -z "$IP" ]]; then IP="$1";
            elif [[ -z "$WORKER" ]]; then WORKER="$1";
            else echo "Too many arguments"; exit 1; fi
            shift
            ;;
    esac
done

if [[ -z "$IP" || -z "$WORKER" ]]; then
    echo "ERROR: IP and WORKER_NAME are required"
    echo "Usage: $0 <IP> <WORKER_NAME>"
    exit 1
fi

# ─── Colors ────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[..]${NC} $*"; }
err()  { echo -e "${RED}[!!]${NC} $*"; }

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 $SSH_USER@$IP"

# ─── Step 1: Check current state ───────────────────────────
warn "Connecting to $IP..."
DRIVER=$($SSH "nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null" || echo "NONE")
HAS_DOCKER=$($SSH "which docker 2>/dev/null" || echo "")

log "Driver: $DRIVER"
log "Docker: ${HAS_DOCKER:-NOT INSTALLED}"

# ─── Step 2: Upgrade driver if 535 ─────────────────────────
if [[ "$DRIVER" == 535.* ]]; then
    warn "Driver 535 detected — upgrading to 550..."
    $SSH "
        sudo add-apt-repository ppa:graphics-drivers/ppa -y
        sudo apt update -qq
        sudo apt install -y nvidia-driver-550
    "
    log "Driver 550 installed. Rebooting..."
    $SSH "sudo reboot" 2>/dev/null || true

    warn "Waiting ${REBOOT_WAIT}s for reboot..."
    sleep "$REBOOT_WAIT"

    # Wait for SSH to come back
    for i in $(seq 1 20); do
        if $SSH "echo ok" 2>/dev/null; then
            log "SSH back online"
            break
        fi
        sleep 5
    done
else
    log "Driver OK (>= 550 or unknown — skipping upgrade)"
fi

# ─── Step 3: Install Docker ────────────────────────────────
if ! $SSH "which docker" 2>/dev/null; then
    warn "Installing Docker..."
    $SSH "curl -fsSL https://get.docker.com | sudo sh"
    $SSH "sudo systemctl enable docker --now"
    log "Docker installed"
fi

# ─── Step 4: Install nvidia-container-toolkit ──────────────
TOOLKIT=$($SSH "dpkg -l nvidia-container-toolkit 2>/dev/null | grep '^ii'" || echo "")
if [[ -z "$TOOLKIT" ]]; then
    warn "Installing nvidia-container-toolkit..."
    $SSH "
        distribution=\$(. /etc/os-release;echo \$ID\$VERSION_ID)
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -s -L https://nvidia.github.io/libnvidia-container/\$distribution/libnvidia-container.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
        sudo apt update -qq
        sudo apt install -y nvidia-container-toolkit
    "
    log "nvidia-container-toolkit installed"
fi

# ─── Step 5: Configure Docker GPU runtime ──────────────────
warn "Configuring Docker GPU runtime..."
$SSH "
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
"
log "Docker GPU runtime configured"

# ─── Step 6: Kill old containers & deploy ──────────────────
warn "Deploying PearlFortune miner (worker: $WORKER)..."
$SSH "
    sudo docker rm -f \$(sudo docker ps -aq) 2>/dev/null || true
    sudo docker run -d \\
        --gpus all \\
        --restart unless-stopped \\
        --name $CONTAINER_NAME \\
        $IMAGE \\
        --proxy $PROXY \\
        --address $WALLET \\
        --worker $WORKER \\
        -gpu
"
log "Container started"

# ─── Step 7: Check hashrate ────────────────────────────────
warn "Waiting for miner to initialize (30s)..."
sleep 30

HASHRATE=$($SSH "sudo docker logs --tail 20 $CONTAINER_NAME 2>&1" | grep -oP 'proof_per_sec="[^"]*"' | tail -1 | grep -oP '[\d.]+(?=\s*T/s)')
GPU_UTIL=$($SSH "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader 2>/dev/null")

echo ""
echo -e "${GREEN}═══ Miner Deployed ═══${NC}"
echo "  IP:       $IP"
echo "  Worker:   $WORKER"
echo "  Hashrate: ${HASHRATE:-warming up...} T/s"
echo "  GPU Util: ${GPU_UTIL:-N/A}"
echo ""

# ─── Step 8: Show live logs (tail) ─────────────────────────
warn "Live logs (Ctrl+C to exit):"
$SSH "sudo docker logs -f --tail 10 $CONTAINER_NAME"
