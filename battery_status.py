import socket
import struct
import time
import datetime
import csv
import os
import random
import threading
import pystray
from PIL import Image, ImageDraw

# ==========================================
# 設定
# ==========================================
CSV_FILE_NAME = "battery_log_30min.csv"

# ECHONET Lite 通信設定
ECHONET_PORT = 3610
MULTICAST_ADDR = "224.0.23.0"
TIMEOUT = 5.0
RETRY_COUNT = 3

# ==========================================
# 通信・データ取得クラス (変更なし)
# ==========================================
EOJ_CONTROLLER = b'\x05\xff\x01'
EOJ_BATTERY    = b'\x02\x7d\x01'
EPC_SOH             = 0xE5
EPC_TOTAL_DISCHARGE = 0xD6
EPC_TOTAL_CHARGE    = 0xD8

class NichiconLogger:
    def __init__(self, ip):
        self.ip = ip
        self.port = ECHONET_PORT

    def _create_tid(self):
        return struct.pack('>H', random.randint(60000, 65535))

    def _send_recv(self, epc):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(TIMEOUT)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            # 3610番にバインド（絶対必須）
            # ここで失敗する場合は、既にポートを独占（REUSEADDRなし）しているアプリがいます
            sock.bind(('0.0.0.0', self.port))
        except Exception as e:
            # バインドできなければ通信できないので終了
            # (通常、REUSEADDRがあればここは通りません)
            sock.close()
            return None

        try:
            for i in range(RETRY_COUNT):
                tid = self._create_tid()
                command = b'\x10\x81' + tid + EOJ_CONTROLLER + EOJ_BATTERY + \
                          b'\x62\x01' + struct.pack('B', epc) + b'\x00'
                try:
                    sock.sendto(command, (self.ip, self.port))
                    start_time = time.time()
                    while time.time() - start_time < TIMEOUT:
                        data, addr = sock.recvfrom(1024)
                        if len(data) < 14: continue
                        if data[2:4] != tid: continue
                        if data[4:7] != EOJ_BATTERY: continue
                        return data[14:]
                except socket.timeout:
                    continue
        finally:
            sock.close()
        return None

    def get_data(self):
        # 通信安定化のためWaitを入れる
        raw_soh = self._send_recv(EPC_SOH)
        soh = int(raw_soh[0]) if raw_soh and len(raw_soh) >= 1 else None
        time.sleep(0.5)
        
        raw_dis = self._send_recv(EPC_TOTAL_DISCHARGE)
        total_discharge = int.from_bytes(raw_dis, 'big') if raw_dis and len(raw_dis) >= 4 else None
        time.sleep(0.5)
        
        raw_chg = self._send_recv(EPC_TOTAL_CHARGE)
        total_charge = int.from_bytes(raw_chg, 'big') if raw_chg and len(raw_chg) >= 4 else None
        
        return {"soh": soh, "total_discharge_wh": total_discharge, "total_charge_wh": total_charge}

def discover_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(4.0)
    try:
        sock.bind(('0.0.0.0', ECHONET_PORT))
        msg = b'\x10\x81\x00\x00\x05\xff\x01\x0e\xf0\x01\x62\x01\xd6\x00'
        sock.sendto(msg, (MULTICAST_ADDR, ECHONET_PORT))
        start = time.time()
        while time.time() - start < 4.0:
            try:
                data, addr = sock.recvfrom(1024)
                if len(data) > 14 and b'\x02\x7d' in data[14:]:
                    return addr[0]
            except socket.timeout:
                continue
    except:
        pass
    finally:
        sock.close()
    return None

def run_task(icon=None):
    """記録実行ロジック"""
    # 頻繁に通知が出るとうっとうしいので、通知は「エラー時」か「手動実行時」のみ推奨
    # 自動実行時はサイレントにします
    
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_ip = discover_ip()
    
    if not target_ip: return # サイレント失敗

    logger = NichiconLogger(target_ip)
    data = logger.get_data()

    if data["soh"] is None: return

    file_exists = os.path.isfile(CSV_FILE_NAME)
    try:
        with open(CSV_FILE_NAME, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "SOH (%)", "Total Charge (Wh)", "Total Discharge (Wh)"])
            writer.writerow([now_str, data['soh'], data['total_charge_wh'], data['total_discharge_wh']])
        
        # アイコンのツールチップを更新して「動いていること」を示す
        if icon:
            icon.title = f"最終記録: {now_str} (SOH:{data['soh']}%)"
            
    except Exception as e:
        pass

# ==========================================
# アイコン生成とスケジューラ
# ==========================================
def create_image():
    """アイコン画像を生成 (青い丸に変更)"""
    width = 64
    height = 64
    image = Image.new('RGB', (width, height), (255, 255, 255))
    dc = ImageDraw.Draw(image)
    dc.rectangle((0, 0, width, height), fill=(255, 255, 255))
    dc.ellipse((10, 10, 54, 54), fill=(0, 100, 255)) # 青
    return image

def scheduler_loop(icon):
    """30分ごとに実行する監視ループ"""
    # 起動直後にも一度実行しておく
    run_task(icon)

    while icon.visible:
        now = datetime.datetime.now()
        
        # 毎時 00分 または 30分 に実行
        if now.minute == 0 or now.minute == 30:
            run_task(icon)
            
            # 実行後は1分以上待機して、同じ分内で連打されるのを防ぐ
            time.sleep(65)
        else:
            # それ以外の時間は10秒ごとにチェック
            time.sleep(10)

def on_manual_run(icon, item):
    """手動実行（通知あり）"""
    icon.notify("手動実行を開始します...", "Battery Logger")
    run_task(icon)
    icon.notify("記録完了しました", "Battery Logger")

def on_exit(icon, item):
    icon.stop()

def main():
    image = create_image()
    menu = pystray.Menu(
        pystray.MenuItem("今すぐ記録する", on_manual_run),
        pystray.MenuItem("終了", on_exit)
    )
    
    icon = pystray.Icon("BatteryLogger30min", image, "蓄電池監視 (30分間隔)", menu)
    
    t = threading.Thread(target=scheduler_loop, args=(icon,))
    t.daemon = True
    t.start()
    
    icon.run()

if __name__ == "__main__":
    main()