#!/usr/bin/env python3
import atexit
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

PREFERRED_CHANNEL = 4
RESTORE_SYSTEM_BLUETOOTH = True
SCRIPT_NAME = os.path.basename(__file__)


def run(cmd: str):
    return subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def log(msg: str) -> None:
    print(msg, flush=True)


def now() -> str:
    return time.strftime("%H:%M:%S")


class BtChat:
    def __init__(self) -> None:
        self.stop = False
        self.cleaned = False
        self.server_sock = None
        self.client_sock = None
        self.client_info = None
        self.rx_buffer = b""
        self.channel = None
        self.btd_proc = None
        self.sdp_handles = set()
        self.stdin_open = True

    # ---------- util ----------
    @staticmethod
    def ensure_root() -> None:
        if os.geteuid() != 0:
            log("ERROR: jalankan dengan sudo")
            sys.exit(1)

    @staticmethod
    def get_ancestors() -> set:
        """PID sendiri + semua leluhur (sudo, sudo monitor, shell, dst)."""
        pids = set()
        pid = os.getpid()
        while pid > 1 and pid not in pids:
            pids.add(pid)
            try:
                with open(f"/proc/{pid}/stat", "r") as f:
                    stat = f.read()
                # format: pid (comm) state ppid ...
                pid = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
        return pids

    @classmethod
    def kill_stale_instances(cls) -> None:
        """Bunuh instance lama script ini (termasuk yang stopped karena Ctrl+Z)."""
        protected = cls.get_ancestors()
        killed = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in protected:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = [
                        a.decode(errors="replace")
                        for a in f.read().split(b"\0")
                        if a
                    ]
            except OSError:
                continue
            if len(argv) < 2:
                continue
            # harus proses python yang menjalankan file script ini (bukan sudo)
            if not os.path.basename(argv[0]).startswith("python"):
                continue
            if not any(os.path.basename(a) == SCRIPT_NAME for a in argv[1:]):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except OSError:
                pass
        if killed:
            log(f"Instance lama dibunuh: {killed}")
            time.sleep(0.7)

    # ---------- bluetoothd / adapter ----------
    @staticmethod
    def find_bluetoothd():
        for p in (
            "/usr/lib/bluetooth/bluetoothd",
            "/usr/libexec/bluetooth/bluetoothd",
            "/usr/sbin/bluetoothd",
            "/usr/bin/bluetoothd",
        ):
            if os.path.exists(p) and os.access(p, os.X_OK):
                return p
        return shutil.which("bluetoothd")

    @staticmethod
    def sdp_present() -> bool:
        r = run("ss -lx | grep -i sdp")
        return r.returncode == 0 or os.path.exists("/var/run/sdp")

    def start_bluetoothd_compat(self) -> bool:
        if self.sdp_present():
            return True
        btd = self.find_bluetoothd()
        if not btd:
            log("bluetoothd tidak ditemukan")
            return False
        run("systemctl stop bluetooth || true")
        run("pkill bluetoothd || true")
        log("Men-start bluetoothd --compat ...")
        self.btd_proc = subprocess.Popen(
            [btd, "--compat", "-n"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(8):
            time.sleep(1)
            if self.sdp_present():
                return True
        log("Gagal membuat sdp socket")
        return False

    @staticmethod
    def setup_adapter() -> None:
        run("rfkill unblock bluetooth || true")
        run("hciconfig hci0 up || true")
        run("hciconfig hci0 piscan || true")
        run("bluetoothctl power on || true")
        run("bluetoothctl discoverable on || true")
        run("bluetoothctl pairable on || true")
        run("bluetoothctl agent on || true")
        run("bluetoothctl default-agent || true")

    # ---------- SDP ----------
    @staticmethod
    def get_sdp_handles() -> set:
        r = run("sdptool browse local")
        return set(re.findall(r"Service RecHandle:\s*(0x[0-9a-fA-F]+)", r.stdout or ""))

    def register_sdp(self, channel: int) -> None:
        before = self.get_sdp_handles()
        run(f"sdptool add --channel={channel} SP")
        time.sleep(0.5)
        self.sdp_handles = self.get_sdp_handles() - before

    def unregister_sdp(self) -> None:
        for h in list(self.sdp_handles):
            run(f"sdptool del {h}")
        self.sdp_handles.clear()

    # ---------- socket ----------
    def create_server_socket(self, preferred: int) -> bool:
        order = [preferred] + [c for c in range(1, 31) if c != preferred]
        for ch in order:
            s = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            try:
                s.bind(("00:00:00:00:00:00", ch))
                s.listen(1)
                self.server_sock = s
                self.channel = ch
                if ch != preferred:
                    log(f"Channel {preferred} terpakai, pakai channel {ch}")
                return True
            except OSError as e:
                s.close()
                if e.errno == 98:
                    continue
                log(f"Bind/listen error di channel {ch}: {e}")
                return False
        log("Semua channel RFCOMM 1-30 terpakai.")
        return False

    def close_client(self, reason: str = "") -> None:
        if self.client_sock is None:
            return
        try:
            self.client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.client_sock.close()
        except OSError:
            pass
        log(f"[{now()}] HP terputus{': ' + reason if reason else ''}")
        self.client_sock = None
        self.client_info = None
        self.rx_buffer = b""

    # ---------- cleanup ----------
    def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = True
        log("\nCleanup ...")
        self.close_client("server berhenti")
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except OSError:
                pass
            self.server_sock = None
        self.unregister_sdp()
        if self.btd_proc is not None:
            try:
                self.btd_proc.terminate()
                self.btd_proc.wait(timeout=3)
            except Exception:
                try:
                    self.btd_proc.kill()
                except Exception:
                    pass
            self.btd_proc = None
            if RESTORE_SYSTEM_BLUETOOTH:
                run("systemctl start bluetooth || true")
        log("Selesai.")

    def handle_signal(self, signum, _frame) -> None:
        self.stop = True

    def install_signal_handlers(self) -> None:
        for sig in (
            signal.SIGINT,
            signal.SIGTERM,
            signal.SIGHUP,
            signal.SIGQUIT,
            signal.SIGTSTP,  # Ctrl+Z -> berhenti bersih
        ):
            signal.signal(sig, self.handle_signal)
        atexit.register(self.cleanup)

    # ---------- chat ----------
    def send_to_phone(self, text: str) -> None:
        if self.client_sock is None:
            log("(belum ada HP terhubung, pesan tidak terkirim)")
            return
        try:
            self.client_sock.sendall((text + "\n").encode("utf-8"))
            log(f"[{now()}] Kamu : {text}")
        except OSError as e:
            self.close_client(f"gagal kirim: {e}")
            self.print_waiting()

    def handle_command(self, line: str) -> None:
        cmd = line.strip().lower()
        if cmd in ("/quit", "/exit", "/q"):
            self.stop = True
        elif cmd == "/disconnect":
            if self.client_sock:
                self.close_client("diputus manual")
                self.print_waiting()
            else:
                log("(tidak ada HP terhubung)")
        elif cmd == "/status":
            log(f"Channel: {self.channel}")
            log(f"HP     : {self.client_info if self.client_sock else '-'}")
        elif cmd == "/help":
            log("/status  /disconnect  /quit  (teks lain = kirim ke HP)")
        else:
            log("Perintah tidak dikenal. Ketik /help")

    def on_stdin(self) -> None:
        line = sys.stdin.readline()
        if line == "":
            self.stdin_open = False
            return
        line = line.rstrip("\n")
        if not line:
            return
        if line.startswith("/"):
            self.handle_command(line)
        else:
            self.send_to_phone(line)

    def on_client_data(self) -> None:
        try:
            data = self.client_sock.recv(1024)
        except OSError as e:
            self.close_client(str(e))
            self.print_waiting()
            return
        if not data:
            self.close_client("HP menutup koneksi")
            self.print_waiting()
            return
        self.rx_buffer += data
        # pecah per baris; sisa tanpa newline ditahan sampai lengkap
        while True:
            m = re.search(rb"\r\n|\n|\r", self.rx_buffer)
            if not m:
                break
            raw = self.rx_buffer[: m.start()]
            self.rx_buffer = self.rx_buffer[m.end():]
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                log(f"[{now()}] HP   : {text}")

    def on_accept(self) -> None:
        try:
            sock, info = self.server_sock.accept()
        except OSError as e:
            log(f"Accept error: {e}")
            return
        self.client_sock, self.client_info = sock, info
        self.rx_buffer = b""
        log(f"[{now()}] HP terhubung: {info[0]}")
        log("Ketik pesan lalu Enter untuk kirim. /help untuk perintah.")

    def print_waiting(self) -> None:
        log(f"Menunggu HP connect di channel {self.channel} ...")

    def serve(self) -> None:
        self.print_waiting()
        log("Ctrl+C untuk berhenti.")
        while not self.stop:
            watch = [self.client_sock if self.client_sock else self.server_sock]
            if self.stdin_open:
                watch.append(sys.stdin)
            try:
                readable, _, _ = select.select(watch, [], [], 0.5)
            except (OSError, ValueError, InterruptedError):
                continue
            for s in readable:
                if s is self.server_sock:
                    self.on_accept()
                elif s is self.client_sock:
                    self.on_client_data()
                elif s is sys.stdin:
                    self.on_stdin()

    def main(self) -> int:
        self.ensure_root()
        self.install_signal_handlers()
        self.kill_stale_instances()
        self.start_bluetoothd_compat()
        self.setup_adapter()
        if not self.create_server_socket(PREFERRED_CHANNEL):
            return 1
        self.register_sdp(self.channel)
        try:
            self.serve()
        except Exception as e:
            log(f"Exception: {e}")
            return 1
        finally:
            self.cleanup()
        return 0


if __name__ == "__main__":
    sys.exit(BtChat().main())
