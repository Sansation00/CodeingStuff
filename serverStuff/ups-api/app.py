from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.security import check_password_hash
import socket
import time
import threading
import psutil
import json
import datetime
import subprocess

with open('/etc/al-1server/power.hash') as f:
    POWER_PASSWORD_HASH = f.read().strip()


app = Flask(__name__)
CORS(app)

NUT_HOST = 'localhost'
NUT_PORT = 24876
NUT_USER = 'monitor'
NUT_PASS = 'monitorpass'
UPS_NAME = 'AL-PowerBac'

outage_active = False
outage_lock = threading.Lock()

ups_cache = {}
ups_cache_time = 0
UPS_CACHE_TTL = 10
ups_cache_lock = threading.Lock()

WATTAGE_LOG = '/opt/ups-api/wattage_log.json'
wattage_lock_file = threading.Lock()

def poll_ups():
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((NUT_HOST, NUT_PORT))
                s.settimeout(3)
                def send(c):
                    s.sendall((c + '\n').encode())
                    data = b''
                    while True:
                        try:
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                            if b'END LIST' in data or b'ERR' in data:
                                break
                        except socket.timeout:
                            break
                    return data.decode()
                send(f'USERNAME {NUT_USER}')
                send(f'PASSWORD {NUT_PASS}')
                response = send(f'LIST VAR {UPS_NAME}')
                send('LOGOUT')
                vars = {}
                for line in response.splitlines():
                    if line.startswith(f'VAR {UPS_NAME}'):
                        parts = line.split('"')
                        key = line.split()[2]
                        value = parts[1]
                        vars[key] = value
                with ups_cache_lock:
                    ups_cache.update(vars)
        except Exception as e:
            print(f'UPS poll error: {e}')
        time.sleep(5)

# Start background poller
poller = threading.Thread(target=poll_ups, daemon=True)
poller.start()

def log_wattage():
    while True:
        try:
            with ups_cache_lock:
                load_pct = float(ups_cache.get('ups.load', 0))
                real_power = float(ups_cache.get('ups.realpower.nominal', 600))
            watts = (load_pct / 100) * real_power
            now = datetime.datetime.now().isoformat()
            entry = {'time': now, 'watts': watts}
            with wattage_lock_file:
                try:
                    with open(WATTAGE_LOG, 'r') as f:
                        log = json.load(f)
                except:
                    log = []
                log.append(entry)
                # Keep only last 10080 entries (1 week at 1/min)
                log = log[-10080:]
                with open(WATTAGE_LOG, 'w') as f:
                    json.dump(log, f)
        except Exception as e:
            print(f'Wattage log error: {e}')
        time.sleep(60)

# Start wattage logger
watt_logger = threading.Thread(target=log_wattage, daemon=True)
watt_logger.start()



def get_ups_vars():
    with ups_cache_lock:
        return dict(ups_cache)

@app.route('/ups/api/status')
def status():
    global outage_active
    try:
        data = get_ups_vars()
        ups_status = data.get('ups.status', '')
        if 'OL' in ups_status and 'OB' not in ups_status:
            with outage_lock:
                outage_active = False
        with outage_lock:
            active = outage_active
        return jsonify({
            'success': True,
            'outage': active,
            'data': {
                'status': data.get('ups.status', 'N/A'),
		'battery_charge': data.get('battery.charge', 'N/A'),
                'battery_runtime': data.get('battery.runtime', 'N/A'),
                'battery_voltage': data.get('battery.voltage', 'N/A'),
                'load': data.get('ups.load', 'N/A'),
                'input_voltage': data.get('input.voltage', 'N/A'),
                'model': data.get('ups.model', 'N/A'),
                'mfr': data.get('ups.mfr', 'N/A'),
                'firmware': data.get('ups.firmware', 'N/A'),
                'beeper': data.get('ups.beeper.status', 'N/A'),
                'battery_type': data.get('battery.type', 'N/A'),
                'real_power': data.get('ups.realpower.nominal', 'N/A'),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/ups/api/outage', methods=['POST'])
def set_outage():
    global outage_active
    data = request.json
    with outage_lock:
        outage_active = data.get('active', False)
    return jsonify({'success': True, 'outage': outage_active})

@app.route('/ups/api/outage', methods=['GET'])
def get_outage():
    with outage_lock:
        active = outage_active
    return jsonify({'outage': active})

@app.route('/ups/api/stats')
def stats():
    try:
        net = psutil.net_io_counters()
        return jsonify({
            'success': True,
            'cpu': psutil.cpu_percent(interval=0.1),
            'memory': {
                'total': psutil.virtual_memory().total,
                'used': psutil.virtual_memory().used,
                'percent': psutil.virtual_memory().percent
            },
            'disks': [
                {
                    'mount': '/',
                    'label': 'OS',
                    'color': '#378ADD',
                    'total': psutil.disk_usage('/').total,
                    'used': psutil.disk_usage('/').used,
                    'percent': psutil.disk_usage('/').percent
                }
            ],
            'network': {
                'bytes_sent': net.bytes_sent,
                'bytes_recv': net.bytes_recv
            },
            'uptime': int(time.time() - psutil.boot_time())
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/ups/api/wattage')
def wattage():
    try:
        with wattage_lock_file:
            try:
                with open(WATTAGE_LOG, 'r') as f:
                    log = json.load(f)
            except:
                log = []

        # Aggregate by day of week
        days = {0:'Mon',1:'Tue',2:'Wed',3:'Thu',4:'Fri',5:'Sat',6:'Sun'}
        totals = {d: 0 for d in days.values()}

        for entry in log:
            try:
                dt = datetime.datetime.fromisoformat(entry['time'])
                day = days[dt.weekday()]
                totals[day] += entry['watts'] / 60
            except:
                pass

        # Convert Wh to kWh
        result = [{'day': d, 'kwh': round(totals[d] / 1000, 3)} for d in days.values()]

        # Today's live kWh
        today_entries = [e for e in log if datetime.datetime.fromisoformat(e['time']).date() == datetime.datetime.now().date()]
        today_kwh = sum(e['watts'] / 60 for e in today_entries) / 1000

        # Live wattage from cache
        with ups_cache_lock:
            load_pct = float(ups_cache.get('ups.load', 0))
            real_power = float(ups_cache.get('ups.realpower.nominal', 600))
        live_watts = (load_pct / 100) * real_power

        return jsonify({
            'success': True,
            'data': result,
            'today_kwh': round(today_kwh, 4),
            'live_watts': round(live_watts, 1),
            'live_load_pct': round(load_pct, 1)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


IDRAC_HOST = '10.189.248.116'
IDRAC_USER = 'sansation'
IDRAC_KEY = '/home/sw-l1/.ssh/idrac_key_rsa'

@app.route('/ups/api/power/<action>', methods=['POST'])
def server_power(action):
    actions = {
        'poweron': 'powerup',
        'poweroff': 'powerdown',
        'restart': 'powercycle'
    }
    if action not in actions:
        return jsonify({'success': False, 'error': 'Invalid action'}), 400

    data = request.get_json(silent=True) or {}
    if not check_password_hash(POWER_PASSWORD_HASH, data.get('password', '')):
        return jsonify({'success': False, 'error': 'Incorrect password'}), 401

    try:
        result = subprocess.run(
            ['ssh', '-i', IDRAC_KEY, '-o', 'StrictHostKeyChecking=no',
             f'{IDRAC_USER}@{IDRAC_HOST}',
             'racadm', 'serveraction', actions[action]],
            capture_output=True, text=True, timeout=15
        )
        return jsonify({
            'success': result.returncode == 0,
            'output': result.stdout.strip(),
            'error': result.stderr.strip() if result.returncode != 0 else None
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, threaded=True)
