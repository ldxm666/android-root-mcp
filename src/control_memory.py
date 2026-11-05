"""Learned knowledge about what each on-screen control actually does.

Why this exists: on real devices the operating system names only a minority of
tappable controls. Measured on this project's target phone, 43.3% of clickable
nodes carried any OS label and 56.7% were completely bare - and WeChat reported
44 nodes with zero ``clickable="true"`` flags because it handles touch in
``onTouchEvent`` without ever calling ``setClickable``. A person copes with such
a screen by *remembering*: "that row opens the chat", "the third icon is
Discover". This module gives the agent the same faculty.

What it does NOT do: it never invents a name. A learned record holds an
*observation* - the effect that was measured when a specific control was
activated on a specific screen - with provenance. ``no_visual_change`` is stored
honestly as such and is explicitly not proof that the control is inert.

Storage is a single JSON file, written atomically, versioned, and bounded. A
missing or corrupt file starts an empty store rather than raising: losing
learned data must never take the server down.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1

#: Ceiling on distinct screens kept in the store. Oldest-by-activity is evicted.
MAX_SCREENS = 2000
#: Ceiling on learned controls per screen, LRU-evicted by ``last_seen``.
MAX_CONTROLS_PER_SCREEN = 400
#: Refuse absurd keys from a hostile or buggy caller.
MAX_KEY_LEN = 200
#: Stop the file growing without bound; screens are trimmed until it fits.
MAX_FILE_BYTES = 8 * 1024 * 1024
#: Evidence strings are capped so one chatty screen cannot bloat the store.
MAX_EFFECT_TEXT = 300

#: Confidence is evidence-weighted, not a yes/no. One observation is suggestive,
#: not certain: (1 hit, 0 miss) -> 0.5, (2,0) -> 0.67, (3,0) -> 0.75.
#: A contradiction lowers it immediately because it matters more than a repeat.
def _confidence(hits: int, misses: int) -> float:
    """Laplace-ish score: repeated agreement raises it, any disagreement lowers it."""
    hits = max(0, int(hits))
    misses = max(0, int(misses))
    return round(hits / float(hits + misses + 1), 3)


#: Bucket width for the geometric identity, in permille of the screen
#: dimension. 40 permille ~= 43 px on a 1080-wide panel: a few pixels of jitter
#: or a small scroll keeps the identity, while list rows (typically 100-250 px
#: apart) land in different buckets and can never collide.
_REL_BUCKET = 40
_ABS_BUCKET = 48


def _now() -> float:
    return time.time()


def _clip(text: Any, limit: int = MAX_EFFECT_TEXT) -> str:
    """Coerce to a bounded, single-line string. Never raises on odd input."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _clean_key(key: Any) -> str:
    """Bound and normalise an identity string."""
    text = _clip(key, MAX_KEY_LEN)
    return re.sub(r"\s+", " ", text)


def screen_key(package: Any, activity: Any) -> str:
    """Screen identity: where the control lives, not what list position it holds."""
    pkg = _clip(package, 120) or "?"
    act = _clip(activity, 160) or "?"
    return f"{pkg}/{act}"


def _bounds(node: Mapping[str, Any]) -> tuple[int, int, int, int]:
    raw = node.get("bounds")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            left, top, right, bottom = (int(v) for v in raw)
        except (TypeError, ValueError):
            return (0, 0, 0, 0)
        return (left, top, right, bottom)
    return (0, 0, 0, 0)


def _rel_bucket(value: int, extent: int) -> int:
    if extent <= 0:
        return 0
    return int(round((value * 1000.0 / extent) / _REL_BUCKET))


def geometric_fingerprint(
    node: Mapping[str, Any],
    screen_size: Sequence[int] | None = None,
    ordinal: int = 0,
) -> str:
    """Deterministic identity for a control that has no semantic key.

    Composed of role plus a *coarse* position-and-size bucket. Position must be
    part of the key or every same-role container on a screen collides with the
    first one - which is exactly what happens on a list of bare rows, and it
    makes two different rows share one learned record. The bucket is coarse
    enough (40 permille, ~43 px) that a few pixels of jitter or a small layout
    shift keep the identity, while rows a normal list-height apart never merge.

    ``ordinal`` is accepted for callers that want document order included; the
    default key does not use it, because inserting one row would then renumber
    every later row and lose all of their learned state.
    """
    role = _clip(node.get("role"), 40) or "unknown"
    left, top, right, bottom = _bounds(node)
    width = max(0, right - left)
    height = max(0, bottom - top)
    if screen_size and len(screen_size) >= 2 and int(screen_size[0]) > 0 and int(screen_size[1]) > 0:
        sx, sy = int(screen_size[0]), int(screen_size[1])
        box = "{}.{}.{}.{}".format(
            _rel_bucket(left, sx), _rel_bucket(top, sy),
            _rel_bucket(width, sx), _rel_bucket(height, sy),
        )
    else:
        box = "{}.{}.{}.{}".format(
            left // _ABS_BUCKET, top // _ABS_BUCKET,
            max(1, width // _ABS_BUCKET), max(1, height // _ABS_BUCKET),
        )
    return "geo:{}|{}".format(role, box)


def resolve_identities(
    controls: Sequence[Mapping[str, Any]],
    screen_size: Sequence[int] | None = None,
) -> list[str]:
    """Identity per control, disambiguating duplicated semantic keys in order.

    ``semantic_key`` is normally unique, but nothing stops an app (or a buggy
    dump) from repeating one. Duplicates get a stable ``|#n`` suffix assigned in
    document order so they at least never collide silently with each other.
    """
    seen: dict[str, int] = {}
    identities: list[str] = []
    for node in controls:
        key = _clean_key(node.get("semantic_key") or "")
        if not key:
            identities.append(geometric_fingerprint(node, screen_size))
            continue
        seen[key] = seen.get(key, 0) + 1
        occurrence = seen[key] - 1
        identities.append(key if occurrence == 0 else "{}|#{}".format(key, occurrence))
    return identities


def control_identity(
    control: Mapping[str, Any],
    screen_size: Sequence[int] | None = None,
    ordinal: int = 0,
) -> str:
    """Identity for a single control, preferring the perception layer's key."""
    key = _clean_key(control.get("semantic_key") or "")
    if key:
        return key
    return geometric_fingerprint(control, screen_size, ordinal)


def _normalise_effect(effect: Any) -> dict[str, str]:
    """Accept a diff dict, a summary string, or None.

    Callers naturally hold either the whole diff or just
    ``probe_engine.summarize_effect(diff)``; refusing the string made the two
    modules awkward to compose for no benefit. Unknown shapes degrade to an
    empty effect rather than raising, so one bad call cannot corrupt the store.
    """
    if effect is None:
        return {"kind": "", "summary": "", "detail": ""}
    if isinstance(effect, str):
        return {"kind": "", "summary": _clip(effect), "detail": ""}
    if isinstance(effect, Mapping):
        return {
            "kind": _clean_key(effect.get("kind")),
            "summary": _clip(effect.get("summary")),
            "detail": _clip(effect.get("detail")),
        }
    return {"kind": "", "summary": _clip(str(effect)), "detail": ""}


class ControlMemory:
    """Persistent, bounded, atomic store of observed control effects."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._screens: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        """Read the store, tolerating absence, truncation and corruption."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            return
        if not raw.strip():
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Keep the evidence rather than silently destroying it, then start clean.
            try:
                self.path.with_suffix(self.path.suffix + ".corrupt")
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:
                pass
            return
        if not isinstance(data, dict):
            return
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            # Unknown/future schema: refuse to interpret it rather than guess.
            return
        screens = data.get("screens")
        if not isinstance(screens, dict):
            return
        for name, controls in screens.items():
            if not isinstance(controls, dict):
                continue
            kept: dict[str, dict[str, Any]] = {}
            for key, record in controls.items():
                if isinstance(record, dict) and isinstance(record.get("observed_effect"), dict):
                    kept[_clean_key(key)] = record
            if kept:
                self._screens[str(name)] = kept

    def _save(self) -> None:
        """Trim to the caps, then write atomically. Caller holds the lock."""
        self._trim()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": _now(),
            "counts": {"screens": len(self._screens),
                       "controls": sum(len(v) for v in self._screens.values())},
            "screens": self._screens,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # One last squeeze if a single screen is pathologically large.
        if len(blob.encode("utf-8")) > MAX_FILE_BYTES:
            for name in sorted(self._screens, key=lambda n: self._screen_last_seen(n)):
                if len(blob.encode("utf-8")) <= MAX_FILE_BYTES:
                    break
                del self._screens[name]
                payload["screens"] = self._screens
                blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(blob, encoding="utf-8")
        os.replace(temporary, self.path)  # atomic on POSIX and Windows

    def _screen_last_seen(self, name: str) -> float:
        controls = self._screens.get(name) or {}
        return max((float(r.get("last_seen") or 0) for r in controls.values()), default=0.0)

    def _trim(self) -> None:
        """Enforce per-screen LRU cap and global screen cap."""
        for name, controls in list(self._screens.items()):
            if len(controls) <= MAX_CONTROLS_PER_SCREEN:
                continue
            order = sorted(controls, key=lambda k: float(controls[k].get("last_seen") or 0))
            for key in order[: len(controls) - MAX_CONTROLS_PER_SCREEN]:
                controls.pop(key, None)
        if len(self._screens) > MAX_SCREENS:
            order = sorted(self._screens, key=self._screen_last_seen)
            for name in order[: len(self._screens) - MAX_SCREENS]:
                self._screens.pop(name, None)

    def save(self) -> None:
        """Persist now. Safe to call often; writes are atomic."""
        with self._lock:
            self._save()

    # -- query / record ----------------------------------------------------
    def lookup(
        self,
        package: Any,
        activity: Any,
        control: Mapping[str, Any],
        screen_size: Sequence[int] | None = None,
        identity: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the learned record for this control, or None if never observed."""
        name = screen_key(package, activity)
        key = identity or control_identity(control, screen_size)
        with self._lock:
            record = (self._screens.get(name) or {}).get(key)
            if record is None:
                return None
            return {
                "identity": key,
                "observed_effect": dict(record.get("observed_effect") or {}),
                "hits": int(record.get("hits") or 0),
                "misses": int(record.get("misses") or 0),
                "confidence": float(record.get("confidence") or 0.0),
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
            }

    def record(
        self,
        package: Any,
        activity: Any,
        control: Mapping[str, Any],
        effect: Mapping[str, Any],
        screen_size: Sequence[int] | None = None,
        identity: str | None = None,
        snapshot_before: str | None = None,
        snapshot_after: str | None = None,
    ) -> dict[str, Any]:
        """Store one observation of what activating ``control`` did.

        A repeat of the same effect raises confidence; a *different* effect
        counts as a miss and replaces the stored observation, because the newer
        measurement is the better evidence.
        """
        name = screen_key(package, activity)
        key = identity or control_identity(control, screen_size)
        now = _now()
        normalised = _normalise_effect(effect)
        stable_key = _clean_key(normalised.get("kind") or normalised.get("summary") or "")
        with self._lock:
            controls = self._screens.setdefault(name, {})
            record = controls.get(key)
            if record is None:
                record = {"hits": 1, "misses": 0, "first_seen": now}
                controls[key] = record
            else:
                previous = _clean_key((record.get("observed_effect") or {}).get("kind") or "")
                if previous and stable_key and previous != stable_key:
                    record["misses"] = int(record.get("misses") or 0) + 1
                else:
                    record["hits"] = int(record.get("hits") or 0) + 1
            record["observed_effect"] = {
                "kind": normalised.get("kind", ""),
                "summary": normalised.get("summary", ""),
                "detail": normalised.get("detail", ""),
            }
            record["label"] = _clip(control.get("label"), 120)
            record["role"] = _clip(control.get("role"), 40)
            record["last_seen"] = now
            record["confidence"] = _confidence(record.get("hits", 0), record.get("misses", 0))
            record["observed_at"] = now
            if snapshot_before:
                record["snapshot_before"] = _clip(snapshot_before, 64)
            if snapshot_after:
                record["snapshot_after"] = _clip(snapshot_after, 64)
            self._save()
            return dict(record)

    def annotate(
        self,
        package: Any,
        activity: Any,
        controls: Sequence[Mapping[str, Any]],
        screen_size: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Attach learned knowledge to a copy of ``controls``.

        Controls with no record come back untouched - no placeholder text, no
        guessed effect. Callers can therefore treat the presence of
        ``learned_effect`` as real evidence.
        """
        name = screen_key(package, activity)
        identities = resolve_identities(controls, screen_size)
        with self._lock:
            records = dict(self._screens.get(name) or {})
        out: list[dict[str, Any]] = []
        for node, identity in zip(controls, identities):
            enriched = dict(node)
            record = records.get(identity)
            if record is not None:
                effect = record.get("observed_effect") or {}
                enriched["learned_effect"] = _clip(effect.get("summary"))
                enriched["learned_effect_kind"] = _clean_key(effect.get("kind"))
                enriched["learned_confidence"] = float(record.get("confidence") or 0.0)
                enriched["hits"] = int(record.get("hits") or 0)
                if int(record.get("misses") or 0):
                    enriched["misses"] = int(record["misses"])
                enriched["learned_identity"] = identity
            out.append(enriched)
        return out

    def forget(self, package: str | None = None, activity: str | None = None) -> int:
        """Drop learned data for one screen, one package, or everything."""
        removed = 0
        with self._lock:
            if package is None and activity is None:
                removed = sum(len(v) for v in self._screens.values())
                self._screens.clear()
            else:
                for name in list(self._screens):
                    pkg, _, act = name.partition("/")
                    if package is not None and pkg != _clip(package, 120):
                        continue
                    if activity is not None and act != (_clip(activity, 160) or "?"):
                        continue
                    removed += len(self._screens.pop(name, {}) or {})
            if removed:
                self._save()
        return removed

    def stats(self) -> dict[str, Any]:
        """Counts and the most-observed screens, for diagnostics."""
        with self._lock:
            total = sum(len(v) for v in self._screens.values())
            screens = []
            for name, controls in self._screens.items():
                if not controls:
                    continue
                screens.append({
                    "screen": name,
                    "controls": len(controls),
                    "avg_confidence": round(
                        sum(float(r.get("confidence") or 0) for r in controls.values()) / len(controls), 3
                    ),
                    "last_seen": max(float(r.get("last_seen") or 0) for r in controls.values()),
                })
            screens.sort(key=lambda s: (-s["controls"], s["screen"]))
            return {
                "path": str(self.path),
                "schema_version": SCHEMA_VERSION,
                "screens": len(self._screens),
                "controls": total,
                "top_screens": screens[:10],
                "caps": {
                    "max_screens": MAX_SCREENS,
                    "max_controls_per_screen": MAX_CONTROLS_PER_SCREEN,
                    "max_file_bytes": MAX_FILE_BYTES,
                },
            }

    def export(self) -> dict[str, Any]:
        """Whole store as a JSON-safe dict."""
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "exported_at": _now(),
                "screens": json.loads(json.dumps(self._screens)),
            }

    def import_(self, data: Mapping[str, Any], merge: bool = True) -> int:
        """Load an exported store. Returns the number of controls absorbed."""
        if not isinstance(data, Mapping) or data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("incompatible export: schema_version mismatch")
        screens = data.get("screens")
        if not isinstance(screens, dict):
            raise ValueError("incompatible export: screens missing")
        absorbed = 0
        with self._lock:
            if not merge:
                self._screens.clear()
            for name, controls in screens.items():
                if not isinstance(controls, dict):
                    continue
                target = self._screens.setdefault(str(name), {})
                for key, record in controls.items():
                    if isinstance(record, dict) and isinstance(record.get("observed_effect"), dict):
                        target[_clean_key(key)] = record
                        absorbed += 1
            self._save()
        return absorbed
