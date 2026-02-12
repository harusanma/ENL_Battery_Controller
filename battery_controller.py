import socket
import struct
import time
import datetime
import random
import threading
import requests
import pystray
import tkinter as tk
import json
import os
import traceback
from tkinter import ttk, messagebox
from PIL import Image, ImageDraw
from dateutil import parser

# ==========================================
# 設定管理
# ==========================================
CONFIG_FILE = "config.json"

class Config:
    def __init__(self):
        self.SOLCAST_API_KEY = ""
        self.RESOURCE_ID_1 = ""
        self.RESOURCE_ID_2 = ""
        self.BATTERY_IP = ""
        self.BATTERY_CAPACITY_KWH = 19.9
        self.MIN_BATTERY_LEVEL = 20.0
        self.MAX_GRID_CHARGE_LEVEL = 90.0
        self.MORNING_TOTAL_PCT = 40.0
        self.DARK_TIME_PCT = 30.0
        self.FORECAST_COEFF = 1.0
        self.TIME1_START = 1; self.TIME1_END = 5
        self.TIME2_START = 11; self.TIME2_END = 13
        self.CHECK_INTERVAL = 60
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.SOLCAST_API_KEY = data.get("api_key", "")
                    self.RESOURCE_ID_1 = data.get("rid1", "")
                    self.RESOURCE_ID_2 = data.get("rid2", "")
                    self.BATTERY_IP = data.get("batt_ip", "")
                    self.BATTERY_CAPACITY_KWH = float(data.get("cap", 19.9))
                    self.MIN_BATTERY_LEVEL = float(data.get("min_lev", 20.0))
                    self.MAX_GRID_CHARGE_LEVEL = float(data.get("max_grid", 90.0))
                    self.MORNING_TOTAL_PCT = float(data.get("m_cons", 40.0))
                    self.DARK_TIME_PCT = float(data.get("d_cons", 30.0))
                    self.FORECAST_COEFF = float(data.get("f_coeff", 1.0))
            except: pass

    def save(self):
        data = {
            "api_key": self.SOLCAST_API_KEY, "rid1": self.RESOURCE_ID_1, "rid2": self.RESOURCE_ID_2,
            "batt_ip": self.BATTERY_IP, "cap": self.BATTERY_CAPACITY_KWH, "min_lev": self.MIN_BATTERY_LEVEL,
            "max_grid": self.MAX_GRID_CHARGE_LEVEL, "m_cons": self.MORNING_TOTAL_PCT, "d_cons": self.DARK_TIME_PCT,
            "f_coeff": self.FORECAST_COEFF
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except: pass

conf = Config()

# ==========================================
# 通信クラス
# ==========================================
ECHONET_PORT = 3610

class NichiconController:
    def __init__(self, ip): self.ip = ip
    
    def _send_recv(self, te, esv, epc, edt=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('0.0.0.0', ECHONET_PORT))
            s.settimeout(0.01)
            try:
                while True: s.recvfrom(1024)
            except: pass 
            
            tid_int = random.randint(0, 65535)
            tid = struct.pack('>H', tid_int)
            p = (struct.pack('B', esv)+b'\x01'+struct.pack('B', epc)+(b'\x00' if edt is None else struct.pack('B', len(edt))+edt))
            s.sendto(b'\x10\x81'+tid+b'\x05\xff\x01'+te+p, (self.ip, ECHONET_PORT))
            
            s.settimeout(3.0) 
            st = time.time()
            while time.time() - st < 3.0:
                try:
                    d, _ = s.recvfrom(1024)
                    if len(d) > 4 and d[2:4] == tid: return d[14:] 
                except socket.timeout: break
            return None
        except: return None
        finally: s.close()
    
    def get_status(self):
        l = self._send_recv(b'\x02\x7d\x01', 0x62, 0xE4)
        m = self._send_recv(b'\x02\x7d\x01', 0x62, 0xDA)
        if l and m: return int(l[0]), int(m[0])
        return None, None

    def set_mode(self, m):
        return self._send_recv(b'\x02\x7d\x01', 0x61, 0xDA, m) is not None

def discover_ip():
    if conf.BATTERY_IP and len(conf.BATTERY_IP.split('.')) == 4:
        return conf.BATTERY_IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3.0)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('0.0.0.0', ECHONET_PORT))
        s.sendto(b'\x10\x81\x00\x00\x05\xff\x01\x0e\xf0\x01\x62\x01\xd6\x00', ("224.0.23.0", ECHONET_PORT))
        st = time.time()
        while time.time() - st < 3.0:
            try:
                d, a = s.recvfrom(1024)
                if b'\x02\x7d' in d: return a[0]
            except: break
    except: pass
    finally: s.close()
    return None

# ==========================================
# ロジック & スレッド管理
# ==========================================
morning_kwh = 0.0; afternoon_kwh = 0.0; last_api_check = ""
plan_night_target = 60; plan_day_target = 90
target_reached = False; last_zone = ""
is_charging_active = False
cached_ip = None
is_running = True # ★ icon.visibleの代わりの生存フラグ

def update_forecast():
    global morning_kwh, afternoon_kwh, last_api_check, plan_night_target, plan_day_target
    if not conf.SOLCAST_API_KEY: return
    now = datetime.datetime.now()
    if last_api_check == f"{now.day}-{now.hour}": return
    rids = [x.strip() for x in [conf.RESOURCE_ID_1, conf.RESOURCE_ID_2] if x.strip()]
    if not rids: return
    total_m = 0.0; total_a = 0.0
    for rid in rids:
        try:
            r = requests.get(f"https://api.solcast.com.au/rooftop_sites/{rid}/forecasts", 
                             params={"format":"json", "api_key":conf.SOLCAST_API_KEY, "hours":48}, timeout=10)
            if r.status_code == 200:
                for item in r.json().get("forecasts", []):
                    dt = parser.parse(item['period_end']) + datetime.timedelta(hours=9); dt = dt.replace(tzinfo=None)
                    val = item['pv_estimate'] * 0.5 * conf.FORECAST_COEFF
                    base_date = now.date()
                    if now.hour >= 20: base_date += datetime.timedelta(days=1)
                    t1_s = datetime.datetime.combine(base_date, datetime.time(1,0))
                    t1_e = datetime.datetime.combine(base_date, datetime.time(11,0))
                    t2_s = datetime.datetime.combine(base_date, datetime.time(11,0))
                    t2_e = t2_s + datetime.timedelta(hours=14)
                    if t1_s <= dt < t1_e: total_m += val
                    elif t2_s <= dt < t2_e: total_a += val
        except: pass
    morning_kwh, afternoon_kwh = total_m, total_a
    m_solar_pct = (morning_kwh / conf.BATTERY_CAPACITY_KWH) * 100
    plan_night_target = int(min(conf.MAX_GRID_CHARGE_LEVEL, max((conf.MIN_BATTERY_LEVEL + conf.MORNING_TOTAL_PCT) - m_solar_pct, conf.MIN_BATTERY_LEVEL + conf.DARK_TIME_PCT)))
    plan_day_target = int(min(conf.MAX_GRID_CHARGE_LEVEL, max(100 - (afternoon_kwh / conf.BATTERY_CAPACITY_KWH) * 100, conf.MIN_BATTERY_LEVEL)))
    last_api_check = f"{now.day}-{now.hour}"

def control_thread(icon):
    global target_reached, last_zone, is_charging_active, cached_ip, is_running
    
    # 起動直後の安定待ち
    time.sleep(2)
    
    while is_running:
        try:
            if not conf.SOLCAST_API_KEY:
                icon.title = "設定未完了"; time.sleep(5); continue
            now = datetime.datetime.now()
            try: update_forecast()
            except: pass
            target = 0; zone = "通常"
            if conf.TIME1_START <= now.hour < conf.TIME1_END: target = plan_night_target; zone = "深夜"
            elif conf.TIME2_START <= now.hour < conf.TIME2_END: target = plan_day_target; zone = "昼間"
            if zone != last_zone: target_reached = False; is_charging_active = False; last_zone = zone

            if not cached_ip: cached_ip = discover_ip()
            if cached_ip:
                ctrl = NichiconController(cached_ip)
                level, mode = ctrl.get_status()
                if level is not None:
                    if target > 0 and not target_reached:
                        if level <= (target - 2): is_charging_active = True
                    if target > 0 and level >= target:
                        is_charging_active = False; target_reached = True
                    
                    status_label = "自動"
                    if is_charging_active:
                        if mode != 0x42: ctrl.set_mode(b'\x42')
                        status_label = "充電中"
                    elif target > 0:
                        if mode != 0x44: ctrl.set_mode(b'\x44')
                        status_label = "待機"
                    else:
                        if mode != 0x46: ctrl.set_mode(b'\x46')
                    
                    icon.title = f"[{zone}] {level}% 目標{target}% {status_label} 予{morning_kwh:.0f}/{afternoon_kwh:.0f}"
                else:
                    icon.title = f"[{zone}] 応答なし(再探索中)"; cached_ip = None 
            else:
                icon.title = f"[{zone}] 蓄電池未発見"
        except: pass
        time.sleep(conf.CHECK_INTERVAL)

# ==========================================
# UI / メイン
# ==========================================
def show_settings():
    root = tk.Tk(); root.title("設定"); root.attributes("-topmost", True)
    fields = [("Solcast API Key", "SOLCAST_API_KEY"), ("Resource ID 1", "RESOURCE_ID_1"), ("Resource ID 2", "RESOURCE_ID_2"), ("★蓄電池IP", "BATTERY_IP"), ("---", None), ("予測係数", "FORECAST_COEFF"), ("容量 (kWh)", "BATTERY_CAPACITY_KWH"), ("非常用最低(%)", "MIN_BATTERY_LEVEL"), ("買電上限(%)", "MAX_GRID_CHARGE_LEVEL"), ("朝消費(%)", "MORNING_TOTAL_PCT"), ("日の出前(%)", "DARK_TIME_PCT")]
    entries = {}
    for r, (l, k) in enumerate(fields):
        if k is None: ttk.Separator(root).grid(row=r, columnspan=2, sticky='ew', pady=5); continue
        tk.Label(root, text=l).grid(row=r, column=0, sticky='w', padx=5)
        e = tk.Entry(root); e.insert(0, str(getattr(conf, k))); e.grid(row=r, column=1, padx=5); entries[k]=e
    def save():
        try:
            for k, e in entries.items():
                v = e.get().strip()
                if k in ["SOLCAST_API_KEY", "RESOURCE_ID_1", "RESOURCE_ID_2", "BATTERY_IP"]: setattr(conf, k, v)
                else: setattr(conf, k, float(v))
            conf.save(); global cached_ip; cached_ip = None; root.destroy()
        except: messagebox.showerror("Error", "Input Error")
    tk.Button(root, text="保存", command=save).grid(row=len(fields), columnspan=2, pady=10)
    root.mainloop()

def on_exit(icon):
    global is_running
    is_running = False
    icon.stop()

def setup(icon):
    icon.visible = True
    threading.Thread(target=control_thread, args=(icon,), daemon=True).start()

def main():
    try:
        if not conf.SOLCAST_API_KEY: show_settings()
        
        icon = pystray.Icon("BatteryCtrl", Image.new('RGB',(64,64),(255,255,255)), "起動中...")
        d = ImageDraw.Draw(icon.icon); d.ellipse((10,10,54,54), fill=(0,128,0))
        
        icon.menu = pystray.Menu(
            pystray.MenuItem("設定", lambda: threading.Thread(target=show_settings, daemon=True).start()),
            pystray.MenuItem("終了", lambda i: on_exit(i))
        )
        
        icon.run(setup=setup)
    except Exception:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Fatal Error", traceback.format_exc())

if __name__ == "__main__":
    main()