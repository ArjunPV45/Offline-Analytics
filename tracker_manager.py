
import logging
import json
import os
import numpy as np
import datetime
from typing import Dict, List, Tuple, Any

try:
    import norfair
    from norfair import Detection, Tracker
    from norfair import distances
    NORFAIR_AVAILABLE = True
except ImportError:
    NORFAIR_AVAILABLE = False

from logging_config import get_logger

logger = get_logger(__name__)

PERSISTENT_ID_FILE = "persistent_ids.json"

def _vectorized_iou(detections, tracked_objects):
    
    is_list_detections = isinstance(detections, (list, tuple))
    is_list_tracks = isinstance(tracked_objects, (list, tuple))

    dets = detections if is_list_detections else [detections]
    trks = tracked_objects if is_list_tracks else [tracked_objects]

    if not dets or not trks:
        res = np.empty((len(dets), len(trks)))
        return res[0, 0] if (not is_list_detections and not is_list_tracks) else res

    
    try:
        det_boxes = np.vstack([d.points for d in dets])
        trk_boxes = np.vstack([t.estimate for t in trks])
        
        
        dists = distances.iou_opt(det_boxes, trk_boxes)
        
        if not is_list_detections and not is_list_tracks:
            return float(dists[0, 0])
        return dists
    except Exception:
        
        if not is_list_detections and not is_list_tracks:
            return 1.0
        return np.ones((len(dets), len(trks)))


_vectorized_iou.is_vectorized = True
_vectorized_iou.vectorized = True


class CameraTrackerManager:
    
    def __init__(self,
                 distance_threshold: float = 0.7,
                 hit_counter_max: int = 30,
                 initialization_delay: int = 1):
        
        self._trackers: Dict[str, 'Tracker'] = {}
        self._distance_threshold = distance_threshold
        self._hit_counter_max = hit_counter_max
        self._initialization_delay = initialization_delay
        
        # Persistence state
        self._max_ids: Dict[str, int] = {}
        self._id_offsets: Dict[str, int] = {}
        self._last_reset_date = datetime.date.today()
        self._load_max_ids()

        if not NORFAIR_AVAILABLE:
            logger.warning(
                "norfair not installed — tracking disabled. "
                "Run: pip install norfair"
            )

    def _load_max_ids(self):
        """Loads the last used max IDs from disk."""
        if os.path.exists(PERSISTENT_ID_FILE):
            try:
                with open(PERSISTENT_ID_FILE, 'r') as f:
                    self._max_ids = json.load(f)
                logger.info(f"[TrackerPersistence] Loaded max IDs: {self._max_ids}")
            except Exception as e:
                logger.error(f"[TrackerPersistence] Failed to load {PERSISTENT_ID_FILE}: {e}")
        else:
            self._max_ids = {}

    def _save_max_ids(self):
        """Saves current max IDs to disk."""
        try:
            with open(PERSISTENT_ID_FILE, 'w') as f:
                json.dump(self._max_ids, f, indent=2)
        except Exception as e:
            logger.error(f"[TrackerPersistence] Failed to save {PERSISTENT_ID_FILE}: {e}")

    def _get_or_create_tracker(self, camera_id: str) -> 'Tracker | None':
        if not NORFAIR_AVAILABLE:
            return None
        if camera_id not in self._trackers:
            self._trackers[camera_id] = Tracker(
                distance_function=_vectorized_iou,
                distance_threshold=self._distance_threshold,
                hit_counter_max=self._hit_counter_max,
                initialization_delay=self._initialization_delay,
            )
            # Set offset based on previously saved max_id
            self._id_offsets[camera_id] = self._max_ids.get(camera_id, 0)
            logger.info(
                f"[Tracker] Created tracker for camera '{camera_id}' "
                f"(ID offset: {self._id_offsets[camera_id]})"
            )
        return self._trackers[camera_id]

    def update(
        self,
        camera_id: str,
        raw_detections: List[Tuple[float, float, float, float, float, str]]
    ) -> List[Tuple[int, float, float]]:
        
        tracker = self._get_or_create_tracker(camera_id)

        
        person_dets = [d for d in raw_detections if d[5] == 'person']

        if not NORFAIR_AVAILABLE or tracker is None:
            
            results = []
            for i, (x1, y1, x2, y2, conf, label) in enumerate(person_dets):
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2
                results.append((i, cx, cy))
            return results

        if not person_dets:
            # Check for daily reset before update
            self._check_daily_reset()
            tracker.update(detections=[])
            return []

        # Check for daily reset before update
        self._check_daily_reset()
        tracker = self._get_or_create_tracker(camera_id)

        
        norfair_dets = []
        for (x1, y1, x2, y2, conf, label) in person_dets:
            # Norfair's iou/iou_opt expect (N, 4) for bounding boxes: [[xmin, ymin, xmax, ymax]]
            points = np.array([[x1, y1, x2, y2]], dtype=float)
            norfair_dets.append(Detection(points=points, scores=np.array([conf])))

        tracked_objects = tracker.update(detections=norfair_dets)

        results = []
        offset = self._id_offsets.get(camera_id, 0)
        current_max = self._max_ids.get(camera_id, 0)
        updated_max = current_max

        for obj in tracked_objects:
            if obj.is_initializing:
                continue  
            
            # Application of the persistent ID offset
            persistent_id = obj.id + offset
            
            bbox = obj.estimate[0]  # Shape (4,) -> [xmin, ymin, xmax, ymax]
            x1, x2 = sorted([bbox[0], bbox[2]])
            y1, y2 = sorted([bbox[1], bbox[3]])

            results.append((persistent_id, x1, y1, x2, y2))

            
            # Track the highest ID seen to preserve for next session
            if persistent_id > updated_max:
                updated_max = persistent_id

        # If we saw a new highest ID, save it
        if updated_max > current_max:
            self._max_ids[camera_id] = updated_max
            self._save_max_ids()

        return results

    def remove_camera(self, camera_id: str):
        
        if camera_id in self._trackers:
            del self._trackers[camera_id]
            logger.info(f"[Tracker] Removed tracker for camera '{camera_id}'")

    def _check_daily_reset(self):
        """Checks if the date has changed and performs a reset if so."""
        current_date = datetime.date.today()
        if current_date != self._last_reset_date:
            logger.info(f"[TrackerPersistence] Date changed from {self._last_reset_date} to {current_date}. Performing daily ID reset.")
            self._perform_daily_reset()
            self._last_reset_date = current_date

    def _perform_daily_reset(self):
        """Resets all tracking IDs and clears persistence for the new day."""
        try:
            # Clear in-memory state
            self._trackers.clear()
            self._id_offsets.clear()
            self._max_ids.clear()
            
            # Clear persistence file
            if os.path.exists(PERSISTENT_ID_FILE):
                try:
                    with open(PERSISTENT_ID_FILE, 'w') as f:
                        json.dump({}, f, indent=2)
                    logger.info("[TrackerPersistence] Persistent ID file cleared for the new day.")
                except Exception as e:
                    logger.error(f"[TrackerPersistence] Failed to clear {PERSISTENT_ID_FILE}: {e}")
            
            logger.info("[TrackerPersistence] Daily ID reset complete. All new tracks will start from ID 1.")
        except Exception as e:
            logger.error(f"[TrackerPersistence] Error during daily reset: {e}")
