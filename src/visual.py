"""Visual fallback and local pixel regression checks, without fabricated UI data."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


class VisualMixin:
    def _capture_screen_and_ui(self, max_elements=2000):
        """Fetch pixels and hierarchy concurrently.

        They are independent device round trips, so serialising them costs the
        sum (~860 ms) where the slower one alone (~600 ms) would do. The u2
        session is resolved once here, on the thread that already holds the
        controller lock, so the workers never race the session property. On any
        worker failure we fall back to the strict serial path, because a partial
        capture must never be mistaken for a complete one.
        """
        if self.u2 is None:
            return self._screen_image(), self.dump_ui_elements(max_elements=max_elements)
        captured: dict = {}

        def _pixels():
            try:
                captured["image"] = self._screen_image()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                captured["image_error"] = exc

        def _tree():
            try:
                captured["ui"] = self.dump_ui_elements(max_elements=max_elements)
            except BaseException as exc:  # noqa: BLE001
                captured["ui_error"] = exc

        workers = [threading.Thread(target=_pixels, daemon=True),
                   threading.Thread(target=_tree, daemon=True)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        if "image_error" in captured or "ui_error" in captured or "image" not in captured:
            return self._screen_image(), self.dump_ui_elements(max_elements=max_elements)
        return captured["image"], captured.get("ui", {})

    def _screen_image(self):
        d = self.u2
        if d is not None:
            try:
                return d.screenshot().convert("RGB")
            except Exception:
                self.reset_u2()
        r = self.shell_result("screencap -p", root=False, timeout=15, max_bytes=32*1024*1024, binary=True)
        if not r.success or not r.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(f"Screen capture failed: {r.as_dict()}")
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")

    def capture_screenshot(self, annotate_elements=True, max_width=720, save_path=None):
        if not 64 <= max_width <= 8192:
            raise ValueError("max_width must be between 64 and 8192")
        t0 = time.monotonic()
        if annotate_elements:
            image, ui = self._capture_screen_and_ui(2000)
        else:
            image, ui = self._screen_image(), {}
        self._screen_size = image.size
        original = image.size
        if annotate_elements:
            draw = ImageDraw.Draw(image)
            for element in self._last_elements:
                if not any(f in element["flags"] for f in ("click", "long_click", "input")):
                    continue
                left, t, r, b = element["bounds"]
                if r <= 0 or b <= 0 or left >= image.width or t >= image.height:
                    continue
                box = (max(left, 0), max(t, 0), min(r, image.width-1), min(b, image.height-1))
                draw.rectangle(box, outline="#ee3333", width=3)
                label = f"#{element['index']}"
                draw.rectangle((box[0], max(0, box[1]-22), box[0]+len(label)*10+8, max(0, box[1]-22)+22), fill="#cc2222")
                draw.text((box[0]+3, max(0, box[1]-22)+2), label, fill="white")
        if save_path:
            path = Path(save_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)
        if image.width > max_width:
            image = image.resize((max_width, round(image.height*max_width/image.width)), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
        return buffer.getvalue(), "image/jpeg", {
            "success": True, "original_size": list(original), "returned_size": list(image.size),
            "annotated": annotate_elements, "saved_to": save_path,
            "snapshot_id": ui.get("snapshot_id"), "elements_summary": ui.get("summary", ""),
            "elapsed_ms": round((time.monotonic()-t0)*1000, 1),
            "note": "Secure/DRM surfaces may be blank; screen capture and UI dump are sequential, not atomic.",
        }

    def compare_screenshot(self, baseline_path, threshold=12, max_width=720, save_current_path="", save_diff_path=""):
        if not 0 <= threshold <= 255 or not 64 <= max_width <= 8192:
            raise ValueError("invalid threshold/max_width")
        with Image.open(baseline_path) as source:
            baseline = source.convert("RGB")
        current = self._screen_image()
        if save_current_path:
            path = Path(save_current_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            current.save(path)
        def resize(image):
            if image.width > max_width:
                return image.resize((max_width, round(image.height*max_width/image.width)), Image.Resampling.LANCZOS)
            return image
        baseline, current = resize(baseline), resize(current)
        if baseline.size != current.size:
            return {"success": True, "changed": True, "dimension_changed": True,
                    "baseline_size": list(baseline.size), "current_size": list(current.size)}
        diff = ImageChops.difference(baseline, current)
        r, g, b = diff.split()
        mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > threshold else 0)
        changed = mask.histogram()[255]
        if save_diff_path:
            path = Path(save_diff_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            diff.save(path)
        return {"success": True, "changed": changed > 0, "dimension_changed": False,
                "changed_pixels": changed, "changed_ratio": round(changed/(current.width*current.height), 6),
                "bounds": list(mask.getbbox()) if mask.getbbox() else None, "threshold": threshold,
                "comparison_size": list(current.size)}
