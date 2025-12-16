from flask import Flask, render_template, request, jsonify, Response
import subprocess
import uuid
import os
import json
import threading
import time
import queue

import datetime

app = Flask(__name__)
HISTORY_FILE = "scan_history.json"

# Store scan data: {
#   "id": ...,
#   "target": ...,
#   "status": ...,
#   "process": subprocess.Popen (only while running),
#   "logs": [],
#   "results": ...
# }
#   "timestamp": ...,
#   "results": ... (loaded on demand)
# }
scans = {}

def save_history():
    history = {}
    for sid, data in scans.items():
        # Clean data for storage: remove objects and heavy data
        entry = {k: v for k, v in data.items() if k not in ['process', 'logs', 'results']}
        history[sid] = entry
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception as e:
        print(f"Error saving history: {e}")

def load_history():
    global scans
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                scans.update(json.load(f))
        except Exception as e:
            print(f"Error loading history: {e}")

load_history()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/test.html')
def test_file():
    return "<h1>Test File Found!</h1>", 200

def run_scan(scan_id, target, extensions):
    report_file = f"reports/{scan_id}.json"
    
    # Ensure reports directory exists
    if not os.path.exists("reports"):
        os.makedirs("reports")

    # Use stdbuf or unbuffered python to ensure real-time output if possible
    command = [
        "python3", "-u", "dirsearch.py", # -u for unbuffered python stdout
        "-u", target,
        "-e", extensions,
        "--output-formats", "json",
        "-o", report_file,
        # "-q" # Remove quiet mode to see progress in logs!
    ]
    
    # Check if we should use --no-color or handle color codes in frontend. 
    # dirsearch uses color codes. We can strip them or render them.
    # For simplicity, let's keep them and maybe strip in frontend or backend.
    # command.append("--no-color") 

    print(f"Starting scan {scan_id}")
    
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, # Enable stdin for interactive prompts
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr to stdout
            text=True,
            bufsize=1 # Line buffered
        )
        
        scans[scan_id]['process'] = process
        scans[scan_id]['status'] = 'running'
        
        # Read output line by line
        for line in iter(process.stdout.readline, ''):
            if line:
                # Filter out progress bar lines to prevent log flooding
                if "job:" in line and "%" in line:
                    continue
                    
                scans[scan_id]['logs'].append(line)
        
        process.stdout.close()
        return_code = process.wait()
        
        print(f"Scan {scan_id} finished with code {return_code}")

        if scans[scan_id]['status'] == 'stopped':
            return
        
        if return_code == 0 or os.path.exists(report_file):
             scans[scan_id]['status'] = 'completed'
             # We don't load results into memory here permanently to save RAM.
             # They are loaded on demand in get_status.
        else:
            scans[scan_id]['status'] = 'error'
            scans[scan_id]['error'] = 'Process failed'
        
        save_history()

    except Exception as e:
        scans[scan_id]['status'] = 'error'
        scans[scan_id]['error'] = str(e)
        save_history()
    finally:
        if 'process' in scans[scan_id]:
            del scans[scan_id]['process']

@app.route('/scan', methods=['POST'])
def start_scan():
    data = request.json
    target = data.get('target')
    extensions = data.get('extensions', 'php,html,js')
    
    if not target:
        return jsonify({"error": "Target is required"}), 400

    scan_id = str(uuid.uuid4())
    scans[scan_id] = {
        "id": scan_id,
        "target": target,
        "status": "pending",
        "timestamp": datetime.datetime.now().isoformat(),
        "results": None,
        "error": None,
        "logs": []
    }
    save_history()

    thread = threading.Thread(target=run_scan, args=(scan_id, target, extensions))
    thread.start()

    return jsonify({"scan_id": scan_id})

@app.route('/status/<scan_id>', methods=['GET'])
def get_status(scan_id):
    scan = scans.get(scan_id)
    if not scan:
        return jsonify({"error": "Scan not found"}), 404
    
    results = scan.get("results")
    if results is None and scan["status"] == "completed":
        # Try to load from disk
        report_file = f"reports/{scan_id}.json"
        if os.path.exists(report_file):
            try:
                with open(report_file, 'r') as f:
                    data = json.load(f)
                    results = data.get("results", [])
            except:
                pass

    return jsonify({
        "id": scan["id"],
        "status": scan["status"],
        "timestamp": scan.get("timestamp"),
        "results": results,
        "error": scan["error"]
    })

@app.route('/history', methods=['GET'])
def get_history():
    # Return list sorted by timestamp desc
    history_list = []
    for s in scans.values():
        history_list.append({
            "id": s["id"],
            "target": s["target"],
            "status": s["status"],
            "timestamp": s.get("timestamp"),
            "error": s.get("error")
        })
    
    # Sort by timestamp desc
    history_list.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify(history_list)

@app.route('/stop/<scan_id>', methods=['POST'])
def stop_scan(scan_id):
    scan = scans.get(scan_id)
    if not scan:
        return jsonify({"error": "Scan not found"}), 404
    
    if scan['status'] == 'running' and 'process' in scan:
        try:
            # Send SIGTERM to trigger the handler
            scan['process'].terminate()
            time.sleep(1) # Wait for handler to catch and print prompt

            if scan['process'].stdin:
                try:
                    # First 'q' to select [q]uit from [q]uit / [c]ontinue
                    scan['process'].stdin.write('q\n')
                    scan['process'].stdin.flush()
                    time.sleep(0.5)
                    
                    # Second 'q' to select [q]uit from [s]ave / [q]uit without saving
                    scan['process'].stdin.write('q\n')
                    scan['process'].stdin.flush()
                except (BrokenPipeError, OSError):
                    pass 

            scan['process'].wait(timeout=5)
        except Exception as e:
            print(f"Error stopping process: {e}")
            try:
                scan['process'].kill()
            except:
                pass
        
        scan['status'] = 'stopped'
        scan['logs'].append("Scan stopped by user.\n")
        save_history()
        return jsonify({"status": "stopped"})
    
    return jsonify({"error": "Scan not running"}), 400

@app.route('/stream/<scan_id>')
def stream_logs(scan_id):
    def generate():
        scan = scans.get(scan_id)
        if not scan:
            return
        
        last_idx = 0
        while True:
            # Check if logs added
            logs = scan['logs']
            if last_idx < len(logs):
                # Yield new lines
                for line in logs[last_idx:]:
                    yield f"data: {json.dumps(line)}\n\n"
                last_idx = len(logs)
            
            # Use 'process' key to verify if still running. 
            # If status completed/error/stopped AND we caught up with logs, break.
            if scan['status'] in ['completed', 'error', 'stopped'] and last_idx >= len(scan['logs']):
                yield f"data: [DONE]\n\n"
                break
            
            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)
