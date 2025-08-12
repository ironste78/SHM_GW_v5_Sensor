import os
import json
import requests
from base64 import b64encode
from modules.tools.log import log
from modules.sensor import Sensor

SESSION = requests.Session()
DEFAULT_TIMEOUT = (3, 5)  # Connect timeout, Read timeout

class Request:
    HEADER = { "Content-Type": "application/json" }
    
    # 1. Communications with the DB
    @staticmethod
    def get_sensor():
        uuid = os.getenv("SENSOR_UUID", None)
        if not uuid:
            raise ValueError("UUID is not set")
        
        database_host = os.getenv("DATABASE_HOST", "localhost")
        database_port = os.getenv("DATABASE_PORT", 7001)
        url = f"http://{database_host}:{database_port}/sensor"
        params = "?command=FIND_ONE&where=uuid=?&params=" + uuid
        
        try:
            resp = SESSION.get(url + params, headers=Request.HEADER, timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            log(f"[Request] Network Error while getting sensor data from db for uuid={uuid}: {e}", "ERROR")
            raise ValueError(f"Network Error: {e}")
        
        if resp.status_code == 404:
            log(f"[Request] Sensor with uuid={uuid} not found in the database", "WARNING")
            return None
        
        if resp.status_code != 200:
            # Log the error message from the response
            log(f"[Request] get_sensor: unexpected {resp.status_code}. Body={resp.text}", "ERROR")
            raise ValueError(f"DB Error: {resp.status_code} - {resp.text}")

        try:
            result = resp.json()
        except Exception as e:
            log(f"[Request] get_sensor: invalid JSON. Body={resp.text}", "ERROR")
            raise ValueError(f"Invalid JSON response: {e}")

        return result.get("data")


    @staticmethod
    def update_sensor_status(payload: dict):
        # If in test mode, salta l’update, niente errori fastidiosi in log
        if os.getenv("ALLOW_UNREGISTERED") == "1":
            log("[Request] (test mode) skipping update_sensor_status", "DEBUG")
            return True
        
        try:
            # Copia per non mutare l'argomento originale
            payload = dict(payload)

            # Normalizza la chiave: usa sempre 'uuid' minuscolo
            uuid = (
                payload.pop("uuid", None)
                or payload.pop("UUID", None)
                or Sensor.get("uuid")
                or os.getenv("SENSOR_UUID")
            )

            if not uuid:
                raise ValueError("UUID is missing for update_sensor_status")

            payload["uuid"] = uuid  # rimetti la chiave normalizzata

            database_host = os.getenv("DATABASE_HOST", "localhost")
            database_port = os.getenv("DATABASE_PORT", 7001)
            url = f"http://{database_host}:{database_port}/sensor"

            response = SESSION.patch(url, json=payload, headers=Request.HEADER, timeout=DEFAULT_TIMEOUT)

            if response.status_code not in (200, 201, 202):
                log(f"[Request] update_sensor_status: unexpected {response.status_code}. Body: {response.text}", "ERROR")
                raise ValueError(f"Error while updating sensor status: {response.text}")

            if response.status_code == 202:
                # Suggerimento: declassa a DEBUG per evitare spam
                log(f"[Request] update_sensor_status: pending (sensor not yet registered)", "DEBUG")

            return (response.json()
                    if response.headers.get("Content-Type", "").startswith("application/json")
                    else response.text)
        except Exception as e:
            log(f"[Request] Error while updating sensor status: {e}", "ERROR")
            raise ValueError(str(e))


    # 2. Communications with the Manager
    @staticmethod
    def update_alert(trigger_timestamp: int, status: int, uuid: str):
        try:
            manager_host = os.getenv("MANAGER_HOST", "localhost")
            manager_port = int(os.getenv("MANAGER_PORT", 7010))
            timestamp = trigger_timestamp
            data = { "timestamp": timestamp, "procStatus": status, "uuid": uuid }
            url = f"http://{manager_host}:{manager_port}/manager/alert"
            response = SESSION.post(url, json=data, headers=Request.HEADER, timeout=DEFAULT_TIMEOUT)
            if response.status_code != 200:
                raise ValueError(f"Error while updating alarm notification: {response.text}")
            return response.json()
        except Exception as e:
            log(f"[Request] Error while updating alarm notification: {e}", "ERROR")
            raise ValueError(str(e))

    @staticmethod
    def update_alerting_data(trigger_timestamp: int, data_timestamp: int, data: list, uuid: str):
        try:
            manager_host = os.getenv("MANAGER_HOST", "localhost")
            manager_port = int(os.getenv("MANAGER_PORT", 7010))

            request_data = []
            if data and len(data) > 0:
                request_data = b"".join([b"".join(d) for d in data])

            request_data = b64encode(request_data).decode('ascii')
            mac = Sensor.get('mac', None)
            mac = mac.replace(":", "") if isinstance(mac, str) else None

            body = {
                "mac": mac,
                "triggerTimestamp": trigger_timestamp,
                "timestamp": data_timestamp,
                "data": request_data,
                "uuid": uuid
            }

            url = f"http://{manager_host}:{manager_port}/manager/alert"
            response = SESSION.post(url, json=body, headers=Request.HEADER, timeout=DEFAULT_TIMEOUT)
            if response.status_code != 200:
                raise ValueError(f"Error while updating alarm data: {response.text}")
            return response.json()
        except Exception as e:
            log(f"[Request] Error while updating alarm data: {e}", "ERROR")
            raise ValueError(str(e))
        
