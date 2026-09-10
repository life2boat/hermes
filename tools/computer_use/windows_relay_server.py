import os
import sys
import json
import time
import uuid
import shutil
import logging
import subprocess
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def main():
    appdata = os.environ.get("LOCALAPPDATA")
    if not appdata:
        logging.error("LOCALAPPDATA not set.")
        sys.exit(1)
        
    base_dir = Path(appdata) / "Hermes" / "computer-use"
    req_dir = base_dir / "relay_ipc" / "req"
    resp_dir = base_dir / "relay_ipc" / "resp"
    
    req_dir.mkdir(parents=True, exist_ok=True)
    resp_dir.mkdir(parents=True, exist_ok=True)
    
    token_path = base_dir / "relay_token.txt"
    if not token_path.exists():
        logging.error(f"Token file not found: {token_path}")
        sys.exit(1)
        
    with open(token_path, "r", encoding="utf-8") as f:
        valid_token = f.read().strip()
        
    logging.info(f"Relay server started. Watching {req_dir}")
    
    processed_requests = set()
    
    while True:
        for file_path in req_dir.glob("*.json"):
            file_name = file_path.name
            if file_name in processed_requests:
                continue
                
            logging.info(f"Processing request: {file_name}")
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    req_data = json.load(f)
                    
                token = req_data.get("token")
                if not token or not hmac_compare(token, valid_token):
                    # For security, you can use secrets.compare_digest
                    import secrets
                    if not token or not secrets.compare_digest(token, valid_token):
                        raise ValueError("Invalid or missing token")

                request_id = req_data.get("request_id")
                verb = req_data.get("verb")
                args = req_data.get("args", {})
                
                if not request_id or not verb:
                    raise ValueError("Missing request_id or verb")
                    
                resp_data = handle_verb(verb, args)
                write_response(resp_dir, request_id, file_name, {"status": "success", "data": resp_data})
                
            except Exception as e:
                logging.exception(f"Error processing {file_name}")
                req_id_to_use = req_data.get("request_id") if 'req_data' in locals() and isinstance(req_data, dict) else "unknown"
                write_response(resp_dir, req_id_to_use, file_name, {"status": "error", "error": str(e)})
            
            processed_requests.add(file_name)
            
        time.sleep(0.5)

def hmac_compare(a, b):
    import secrets
    return secrets.compare_digest(a, b)

def write_response(resp_dir, request_id, original_file_name, payload):
    payload["request_id"] = request_id
    temp_file = resp_dir / f"{original_file_name}.tmp"
    final_file = resp_dir / original_file_name
    
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        
    os.replace(temp_file, final_file)
    logging.info(f"Response written to {final_file.name}")

def handle_verb(verb, args):
    if verb == "status":
        return {"status": "running"}
        
    cmd = ["winapp", "ui", verb, "--json"]
    
    if verb in ["list_windows", "inspect", "click", "invoke", "set_value"]:
        # Add any necessary arguments
        if "hwnd" in args:
            cmd.extend(["--hwnd", str(args["hwnd"])])
        if "element_id" in args:
            cmd.extend(["--element-id", str(args["element_id"])])
        if "value" in args:
            cmd.extend(["--value", str(args["value"])])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"winapp command failed: {result.stderr}")
            
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw_output": result.stdout}
            
    else:
        raise ValueError(f"Unsupported verb: {verb}")

if __name__ == "__main__":
    main()
