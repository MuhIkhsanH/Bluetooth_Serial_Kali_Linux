#!/usr/bin/env python3
# bt_server_allinone_with_wifi.py
# Jalankan sebagai root: sudo python3 bt_server_allinone_with_wifi.py
# Fungsi: start bluetoothd --compat bila perlu, set adapter, listen RFCOMM chan1,
# menerima perintah dari client via Bluetooth:
#   "SCAN" atau "SCAN_WIFI" -> lakukan scan Wi-Fi dan kirim hasil
#   "HELP" -> kirim daftar perintah
# default behavior: echo + SERVER_ACK for other teks

import os, sys, time, shutil, subprocess, re
from bluetooth import BluetoothSocket, RFCOMM, advertise_service, SERIAL_PORT_CLASS, SERIAL_PORT_PROFILE
from bluetooth import btcommon

# ---------- util ----------
def run(cmd, timeout=15):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)

def ensure_root():
    if os.geteuid() != 0:
        print("ERROR: harus dijalankan sebagai root"); sys.exit(1)

def find_bluetoothd():
    for p in ("/usr/lib/bluetooth/bluetoothd","/usr/sbin/bluetoothd","/usr/bin/bluetoothd"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("bluetoothd")

def sdp_present():
    r = run("ss -lx | grep -i sdp")
    return r.returncode == 0

def start_bluetoothd_compat():
    if sdp_present():
        print("sdp socket sudah ada")
        return True
    btd = find_bluetoothd()
    if not btd:
        print("bluetoothd tidak ditemukan")
        return False
    run("systemctl stop bluetooth || true")
    run("pkill bluetoothd || true")
    print("Men-start bluetoothd --compat ...")
    subprocess.Popen([btd,"--compat","-n"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(8):
        time.sleep(1)
        if sdp_present():
            print("sdp socket dibuat")
            return True
    print("Gagal membuat sdp socket")
    return False

def setup_adapter():
    run("hciconfig hci0 up")
    run("hciconfig hci0 piscan")
    run("bluetoothctl power on")
    run("bluetoothctl discoverable on")
    run("bluetoothctl pairable on")
    run("bluetoothctl agent on")
    run("bluetoothctl default-agent")
    run("sdptool add SP")
    time.sleep(0.3)

def chunk_send(sock, data, chunk_size=1024):
    if isinstance(data, str):
        data = data.encode('utf-8', errors='replace')
    offset = 0
    while offset < len(data):
        end = min(offset + chunk_size, len(data))
        sock.send(data[offset:end])
        offset = end
        time.sleep(0.02)

# ---------- Wi-Fi scan ----------
def wifi_scan_nmcli():
    # try nmcli terse output
    r = run("nmcli -t -f SSID,SIGNAL,SECURITY device wifi list")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            # fallback: join remaining
            ssid = parts[0].strip() if parts else ""
            signal = parts[1].strip() if len(parts) > 1 else ""
            sec = parts[2].strip() if len(parts) > 2 else ""
        else:
            ssid, signal, sec = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if ssid == "--":
            ssid = "<hidden>"
        out.append((ssid, signal, sec))
    return out

def wifi_scan_iwlist():
    # find wifi iface
    r = run("iw dev | awk '$1==\"Interface\"{print $2; exit}'")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    iface = r.stdout.strip().splitlines()[0]
    r2 = run(f"iwlist {iface} scanning")
    if r2.returncode != 0 or not r2.stdout.strip():
        return None
    text = r2.stdout
    cells = text.split("Cell ")
    out = []
    for c in cells[1:]:
        # ESSID:"..."
        m_ssid = re.search(r'ESSID:\"(.*?)\"', c)
        ssid = m_ssid.group(1) if m_ssid else "<hidden>"
        m_sig = re.search(r'Signal level[=/](-?\d+)', c)
        signal = m_sig.group(1) + " dBm" if m_sig else ""
        m_sec = re.search(r'Encryption key:(on|off)', c)
        sec = "OPEN" if (m_sec and m_sec.group(1) == "off") else "ENCRYPTED"
        out.append((ssid, signal, sec))
    return out if out else None

def wifi_scan():
    res = wifi_scan_nmcli()
    if res:
        return res
    res = wifi_scan_iwlist()
    if res:
        return res
    return []

# ---------- Server ----------
def server_loop():
    server_sock = BluetoothSocket(RFCOMM)
    try:
        server_sock.bind(("", 1))
        server_sock.listen(1)
    except Exception as e:
        print("Bind/listen error:", e)
        server_sock.close()
        return

    try:
        advertise_service(
            server_sock,
            "KaliSerialServer",
            service_id = "00001101-0000-1000-8000-00805F9B34FB",
            service_classes = ["00001101-0000-1000-8000-00805F9B34FB", SERIAL_PORT_CLASS],
            profiles = [ SERIAL_PORT_PROFILE ],
        )
        print("advertise_service: OK (SDP tersedia)")
    except btcommon.BluetoothError as e:
        print("advertise_service gagal, lanjut tanpa SDP:", e)
    except Exception as e:
        print("advertise_service exception:", e)

    print("Menunggu koneksi RFCOMM di channel 1 ...")
    client_sock = None
    try:
        client_sock, client_info = server_sock.accept()
        print("TERHUBUNG:", client_info)
        # on connect, send brief welcome & available commands
        chunk_send(client_sock, "WELCOME\nAvailable commands: SCAN, HELP, QUIT\n")
        while True:
            data = client_sock.recv(1024)
            if not data:
                print("Client disconnected")
                break
            s = data.decode('utf-8', errors='replace').strip()
            print("<-", s)
            if not s:
                continue
            cmd = s.upper()
            if cmd in ("SCAN","SCAN_WIFI"):
                chunk_send(client_sock, "WIFI_LIST_START\n")
                nets = wifi_scan()
                if not nets:
                    chunk_send(client_sock, "WIFI_EMPTY\n")
                else:
                    # send lines SSID|SIGNAL|SEC
                    for ssid, sig, sec in nets:
                        line = f"{ssid}|{sig}|{sec}\n"
                        chunk_send(client_sock, line)
                chunk_send(client_sock, "WIFI_LIST_END\n")
            elif cmd == "HELP":
                chunk_send(client_sock, "COMMANDS: SCAN / SCAN_WIFI -> scan Wi-Fi\nHELP -> this message\nQUIT -> disconnect\n")
            elif cmd == "QUIT":
                chunk_send(client_sock, "BYE\n")
                break
            else:
                # echo + ack
                resp = ("SERVER_ACK:" + s + "\n")
                chunk_send(client_sock, resp)
    except KeyboardInterrupt:
        print("Stopping (keyboard)")
    except btcommon.BluetoothError as e:
        print("BluetoothError:", e)
    except Exception as e:
        print("Exception:", e)
    finally:
        if client_sock:
            try: client_sock.close()
            except: pass
        try: server_sock.close()
        except: pass
        print("Socket ditutup.")

# ---------- main ----------
if __name__ == "__main__":
    ensure_root()
    start_bluetoothd_compat()
    setup_adapter()
    server_loop()
