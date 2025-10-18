#!/usr/bin/env python3
# bt_server_allinone.py
# Jalankan sebagai root: sudo python3 bt_server_allinone.py

import os, sys, time, shutil, subprocess
from bluetooth import BluetoothSocket, RFCOMM, advertise_service, SERIAL_PORT_CLASS, SERIAL_PORT_PROFILE
from bluetooth import btcommon

def run(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def ensure_root():
    if os.geteuid() != 0:
        print("ERROR: harus dijalankan sebagai root") ; sys.exit(1)

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
    # kill existing daemons to avoid conflicts
    run("systemctl stop bluetooth || true")
    run("pkill bluetoothd || true")
    # start compat
    print("Men-start bluetoothd --compat ...")
    subprocess.Popen([btd,"--compat","-n"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(6):
        time.sleep(1)
        if sdp_present():
            print("sdp socket dibuat")
            return True
    print("Gagal membuat sdp socket")
    return False

def setup_adapter():
    run("hciconfig hci0 up")
    run("hciconfig hci0 piscan")  # page+inquiry
    run("bluetoothctl power on")
    run("bluetoothctl discoverable on")
    run("bluetoothctl pairable on")
    run("bluetoothctl agent on")
    run("bluetoothctl default-agent")
    # add SPP
    run("sdptool add SP")
    time.sleep(0.5)

def server_loop():
    server_sock = BluetoothSocket(RFCOMM)
    try:
        server_sock.bind(("", 1))
        server_sock.listen(1)
    except Exception as e:
        print("Bind/listen error:", e)
        server_sock.close()
        return
    # try advertise_service (boleh gagal)
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

    print("Menunggu koneksi RFCOMM di channel 1 (Ctrl-C untuk stop)...")
    client_sock = None
    try:
        client_sock, client_info = server_sock.accept()
        print("TERHUBUNG:", client_info)
        while True:
            data = client_sock.recv(1024)
            if not data:
                print("Client disconnected")
                break
            try:
                s = data.decode('utf-8', errors='replace').strip()
            except:
                s = repr(data)
            print("<-", s)
            resp = ("SERVER_ACK:" + s + "\n").encode('utf-8')
            client_sock.send(resp)
            print("-> ACK")
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

if __name__ == "__main__":
    ensure_root()
    # start bluetoothd with SDP if belum ada
    ok = start_bluetoothd_compat()
    # still proceed even if sdp missing (rfcomm accept tetap mungkin)
    setup_adapter()
    server_loop()
