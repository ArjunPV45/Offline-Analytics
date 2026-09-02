import os
import json
import threading

_LOCK = threading.Lock()
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), 'cameras.json')


def _load(filepath=_DEFAULT_PATH):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            return data or {}
    except Exception:
        return {}


def _save(data, filepath=_DEFAULT_PATH):
    try:
        dirpath = os.path.dirname(filepath)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


def load_all_cameras(filepath=_DEFAULT_PATH):
    return _load(filepath)


def upsert_camera(camera_id, camera_obj, filepath=_DEFAULT_PATH):
    with _LOCK:
        data = _load(filepath)
        data[str(camera_id)] = camera_obj
        return _save(data, filepath)


def update_camera_fields(camera_id, fields, filepath=_DEFAULT_PATH):
    with _LOCK:
        data = _load(filepath)
        cam = data.get(str(camera_id), {})
        cam.update(fields)
        data[str(camera_id)] = cam
        return _save(data, filepath)


def remove_camera(camera_id, filepath=_DEFAULT_PATH):
    with _LOCK:
        data = _load(filepath)
        if str(camera_id) in data:
            del data[str(camera_id)]
            return _save(data, filepath)
        return False
