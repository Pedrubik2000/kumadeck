"""RapidOCR model download — Japanese subset only.

Japanese uses the PP-OCRv5 'ch' recognition model (it covers kana + kanji
natively; this is also what Decky-Translator maps `ja` to). Sources mirror
Decky-Translator's manifest.
"""

import logging
import os
import shutil
import threading
import urllib.request

from .net import ssl_context

logger = logging.getLogger(__name__)

PADDLEOCR_RELEASES = (
    "https://github.com/MeKo-Christian/paddleocr-onnx/releases/download/v1.0.0"
)
MONKT_BASE = "https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages"
SWHL_CLS_URL = (
    "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv3/"
    "ch_ppocr_mobile_v2.0_cls_train.onnx"
)

# (url, dest filename, approx bytes)
MANIFEST = (
    (f"{PADDLEOCR_RELEASES}/PP-OCRv5_mobile_det.onnx",
     "ch_PP-OCRv5_mobile_det.onnx", 4_748_769),
    (f"{PADDLEOCR_RELEASES}/PP-OCRv5_mobile_rec.onnx",
     "ch_rec.onnx", 16_517_247),
    (f"{MONKT_BASE}/chinese/dict.txt", "ch_dict.txt", 74_012),
    (SWHL_CLS_URL, "ch_ppocr_mobile_v2.0_cls_infer.onnx", 581_639),
)

REQUIRED_FILES = tuple(name for _, name, _ in MANIFEST)
TOTAL_BYTES = sum(approx for _, _, approx in MANIFEST)


class ModelDownloader:
    def __init__(self, base_dir: str):
        self.target_dir = os.path.join(base_dir, "models")
        self._staging = self.target_dir + ".downloading"
        self._downloading = False
        self._progress = 0.0
        self._error = None
        self._lock = threading.Lock()
        os.makedirs(base_dir, exist_ok=True)
        shutil.rmtree(self._staging, ignore_errors=True)

    def is_installed(self) -> bool:
        return all(os.path.exists(os.path.join(self.target_dir, f))
                   for f in REQUIRED_FILES)

    def get_status(self) -> dict:
        with self._lock:
            return {
                "installed": self.is_installed(),
                "downloading": self._downloading,
                "progress": self._progress,
                "error": self._error,
                "approx_size_mb": round(TOTAL_BYTES / 1_048_576),
            }

    def start_download(self) -> bool:
        with self._lock:
            if self._downloading:
                return False
            self._downloading = True
            self._error = None
            self._progress = 0.0
        threading.Thread(target=self._download, daemon=True).start()
        return True

    def _download(self):
        try:
            shutil.rmtree(self._staging, ignore_errors=True)
            os.makedirs(self._staging, exist_ok=True)
            done_bytes = 0
            for url, name, _approx in MANIFEST:
                dest = os.path.join(self._staging, name)
                logger.info(f"downloading {url}")
                req = urllib.request.Request(url, headers={"User-Agent": "kumadeck"})
                with urllib.request.urlopen(req, timeout=60,
                                            context=ssl_context()) as resp, \
                        open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        done_bytes += len(chunk)
                        with self._lock:
                            self._progress = min(0.99, done_bytes / TOTAL_BYTES)
            shutil.rmtree(self.target_dir, ignore_errors=True)
            os.replace(self._staging, self.target_dir)
            with self._lock:
                self._progress = 1.0
        except Exception as e:
            logger.error(f"model download failed: {e}")
            shutil.rmtree(self._staging, ignore_errors=True)
            with self._lock:
                self._error = str(e)
        finally:
            with self._lock:
                self._downloading = False
