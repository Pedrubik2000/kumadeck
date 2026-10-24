#!/usr/bin/env python3
"""OCR worker — runs under the venv python, never under Decky's runtime.

Modes:
  recognize:  ocr_worker.py recognize <opts_json>
      opts: {"image_path", "models_dir", "min_confidence", "region",
             "crop_out"}
      region is {"x","y","w","h"} in 0-1 fractions of the frame, optional.
      If crop_out is set, the cropped region is also saved there (PNG).
      Prints JSON: {"error", "regions": [{text, rect, confidence}],
      "crop_path", "width", "height"}.

  encode_raw: ocr_worker.py encode_raw <width> <height> <out_png>
      Reads raw RGB24 bytes from stdin, writes a PNG. Used when the system
      GStreamer lacks pngenc. Prints JSON: {"error", "path"}.

  crop:       ocr_worker.py crop <opts_json>
      opts: {"image_path", "region", "crop_out"} — crop only, no OCR
      (used before sending a region to a cloud OCR backend).
"""

import json
import os
import sys
from typing import NoReturn

# Cap native thread pools before the heavy imports — mirrors
# Decky-Translator's fix for deadlocks when the parent awaits the subprocess.
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('MKL_NUM_THREADS', '4')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')


def _fail(msg, **extra) -> NoReturn:
    print(json.dumps({"error": msg, **extra}, ensure_ascii=False))
    sys.exit(0)


def _load_and_crop(image_path, region, crop_out):
    from PIL import Image
    img = Image.open(image_path)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    offset = (0, 0)
    if region:
        W, H = img.size
        left = max(0, int(region["x"] * W))
        top = max(0, int(region["y"] * H))
        right = min(W, int((region["x"] + region["w"]) * W))
        bottom = min(H, int((region["y"] + region["h"]) * H))
        if right - left >= 8 and bottom - top >= 8:
            img = img.crop((left, top, right, bottom))
            offset = (left, top)
    if crop_out:
        # a card's picture goes out as .webp (~1/10 of the PNG, synced to every phone); OCR crops stay PNG
        if crop_out.endswith(".webp"):
            img.save(crop_out, "WEBP", quality=80)
        else:
            img.save(crop_out, "PNG")
    return img, offset


# A tight, text-height-only crop starves the detector of the spatial context
# it needs to group characters into full-line boxes — small VN text boxes
# reliably fragment into garbage without some margin, even though the tight
# crop looks fine to a human. Pad with solid white rather than more of the
# real frame: it gives the detector the same margin without ever risking
# pulling in real neighboring screen content (a name tag, a UI element)
# that a real-pixel border could hand it as a spurious detection.
OCR_DETECTION_PAD_PX = 56


def cmd_recognize(opts):
    image_path = opts["image_path"]
    models_dir = opts["models_dir"]
    min_conf = float(opts.get("min_confidence", 0.4))
    region = opts.get("region")
    crop_out = opts.get("crop_out")

    if not os.path.exists(image_path):
        _fail(f"image not found: {image_path}", regions=[])

    try:
        from PIL import ImageOps
        cropped, _ = _load_and_crop(image_path, region, crop_out)
        img = (ImageOps.expand(cropped, OCR_DETECTION_PAD_PX, fill=(255, 255, 255))
              if region else cropped)
    except Exception as e:
        _fail(f"image load/crop failed: {e}", regions=[])

    try:
        import numpy as np
        from rapidocr import EngineType, RapidOCR

        det_model = os.path.join(models_dir, "ch_PP-OCRv5_mobile_det.onnx")
        cls_model = os.path.join(models_dir, "ch_ppocr_mobile_v2.0_cls_infer.onnx")
        rec_model = os.path.join(models_dir, "ch_rec.onnx")
        rec_keys = os.path.join(models_dir, "ch_dict.txt")
        for p in (det_model, cls_model, rec_model):
            if not os.path.exists(p):
                _fail(f"OCR model missing: {os.path.basename(p)} — "
                      "download models in plugin settings", regions=[])

        params = {
            "Global.text_score": min_conf,
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Det.model_path": det_model,
            "Cls.model_path": cls_model,
            "Rec.model_path": rec_model,
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        if os.path.exists(rec_keys):
            params["Rec.rec_keys_path"] = rec_keys

        engine = RapidOCR(params=params)
        result = engine(np.array(img))
    except Exception as e:
        import traceback
        _fail(f"OCR failed: {e}", regions=[], trace=traceback.format_exc())

    regions = []
    if result and result.txts:
        for box, text, confidence in zip(result.boxes, result.txts,
                                         result.scores, strict=False):
            if not text or not text.strip():
                continue
            if confidence < min_conf:
                continue
            if box is not None and len(box) >= 4:
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                rect = {"left": int(min(xs)), "top": int(min(ys)),
                        "right": int(max(xs)), "bottom": int(max(ys))}
            else:
                rect = {"left": 0, "top": 0, "right": 0, "bottom": 0}
            regions.append({
                "text": text.strip(),
                "rect": rect,
                "confidence": float(confidence),
            })

    print(json.dumps({"error": None, "regions": regions,
                      "crop_path": crop_out or None,
                      "width": img.width, "height": img.height},
                     ensure_ascii=False))


def cmd_encode_raw(width, height, out_png):
    from io import BytesIO

    from PIL import Image, ImageStat

    raw = sys.stdin.buffer.read()
    expected = width * height * 3
    if len(raw) != expected:
        _fail(f"raw frame size {len(raw)} != expected {expected}")
    img = Image.frombytes("RGB", (width, height), raw)
    if max(ImageStat.Stat(img).stddev) < 10:
        _fail("frame too uniform — likely warmup garbage")
    buf = BytesIO()
    img.save(buf, "PNG")
    with open(out_png, "wb") as f:
        f.write(buf.getvalue())
    print(json.dumps({"error": None, "path": out_png}))


def cmd_crop(opts):
    try:
        _load_and_crop(opts["image_path"], opts.get("region"), opts["crop_out"])
        print(json.dumps({"error": None, "crop_path": opts["crop_out"]}))
    except Exception as e:
        _fail(f"crop failed: {e}")


def main():
    if len(sys.argv) < 2:
        _fail("usage: ocr_worker.py recognize|encode_raw|crop ...")
    mode = sys.argv[1]
    if mode == "recognize":
        cmd_recognize(json.loads(sys.argv[2]))
    elif mode == "encode_raw":
        cmd_encode_raw(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    elif mode == "crop":
        cmd_crop(json.loads(sys.argv[2]))
    else:
        _fail(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
