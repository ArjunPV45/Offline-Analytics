
import time
import logging
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

logger = logging.getLogger(__name__)


def validate_rtsp_url(rtsp_url: str, timeout: float = 10.0) -> bool:
    logger.info(f"Validating RTSP URL: {rtsp_url}")

    pipeline_str = (
        f"rtspsrc location={rtsp_url} latency=0 timeout=5000000000 ! "
        f"fakesink sync=false"
    )

    pipeline = None
    try:
        pipeline = Gst.parse_launch(pipeline_str)
        if not pipeline:
            logger.error("Failed to parse validation pipeline")
            return False

        bus = pipeline.get_bus()

        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.error(f"Validation pipeline failed to reach PLAYING state for: {rtsp_url}")
            return False

        deadline = time.monotonic() + timeout
        got_data = False

        while time.monotonic() < deadline:
            msg = bus.pop_filtered(
                Gst.MessageType.ERROR |
                Gst.MessageType.EOS |
                Gst.MessageType.STATE_CHANGED |
                Gst.MessageType.STREAM_START
            )
            if msg is None:
                time.sleep(0.1)
                continue

            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logger.error(
                    f"RTSP validation error for {rtsp_url}: {err.message} | {debug}"
                )
                return False

            if msg.type == Gst.MessageType.EOS:
            
                logger.error(f"RTSP validation got EOS immediately for: {rtsp_url}")
                return False

            if msg.type == Gst.MessageType.STREAM_START:
                
                got_data = True
                break

            
            if msg.type == Gst.MessageType.STATE_CHANGED:
                if msg.src == pipeline:
                    _, new_state, _ = msg.parse_state_changed()
                    if new_state == Gst.State.PLAYING:
                        
                        error_check_deadline = time.monotonic() + 2.0
                        while time.monotonic() < error_check_deadline:
                            err_msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.STREAM_START)
                            if err_msg is None:
                                time.sleep(0.05)
                                continue
                            if err_msg.type == Gst.MessageType.ERROR:
                                err, debug = err_msg.parse_error()
                                logger.error(
                                    f"RTSP validation error (post-PLAY) for {rtsp_url}: {err.message} | {debug}"
                                )
                                return False
                            if err_msg.type == Gst.MessageType.STREAM_START:
                                got_data = True
                                break
                        break

        if not got_data:
            err_msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if err_msg:
                err, debug = err_msg.parse_error()
                logger.error(f"RTSP validation error (timeout sweep) for {rtsp_url}: {err.message}")
                return False
            logger.error(f"RTSP validation timed out ({timeout}s) — no data received for: {rtsp_url}")
            return False

        logger.info(f"RTSP validation successful for {rtsp_url}")
        return True

    except Exception as e:
        logger.error(f"Exception during RTSP validation: {e}")
        return False
    finally:
        if pipeline:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
