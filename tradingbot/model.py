"""ModelBundle: LightGBM per horizon + isotone kalibratie, zonder pickle opgeslagen."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd

from .features import FEATURES


class IsoCal:
    """Isotone kalibratie als drempels; voorspelling via lineaire interpolatie."""

    def __init__(self, xs: list[float], ys: list[float]):
        self.xs, self.ys = np.asarray(xs, float), np.asarray(ys, float)

    @staticmethod
    def fit(p: np.ndarray, y: np.ndarray) -> "IsoCal":
        from sklearn.isotonic import IsotonicRegression
        iso = IsotonicRegression(out_of_bounds="clip",
                                 y_min=0.0, y_max=1.0).fit(p, y)
        return IsoCal(list(iso.X_thresholds_), list(iso.y_thresholds_))

    def predict(self, p: np.ndarray | float) -> np.ndarray:
        if len(self.xs) == 0:
            return np.asarray(p, float)
        return np.interp(p, self.xs, self.ys)

    def to_dict(self) -> dict:
        return {"xs": self.xs.tolist(), "ys": self.ys.tolist()}


@dataclass
class ModelBundle:
    version: str
    horizons: list[int]
    boosters: dict[int, lgb.Booster] = field(default_factory=dict)
    calibrators: dict[int, IsoCal] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)   # config, gate-uitkomst, metrics

    def predict(self, X: pd.DataFrame, horizon: int) -> np.ndarray:
        raw = self.boosters[horizon].predict(X[FEATURES].astype(float))
        return self.calibrators[horizon].predict(raw)

    def predict_raw(self, X: pd.DataFrame, horizon: int) -> np.ndarray:
        return self.boosters[horizon].predict(X[FEATURES].astype(float))

    @property
    def passed_gate(self) -> bool:
        return bool(self.meta.get("gate", {}).get("passed", False))

    def save(self, root: Path) -> Path:
        d = root / self.version
        d.mkdir(parents=True, exist_ok=True)
        for h, b in self.boosters.items():
            b.save_model(str(d / f"h{h}.txt"))
            (d / f"h{h}_cal.json").write_text(json.dumps(
                self.calibrators[h].to_dict()), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({"version": self.version, "horizons": self.horizons,
                                                 "features": FEATURES, **self.meta}, indent=1, default=str),
                                     encoding="utf-8")
        return d

    @staticmethod
    def load(d: Path) -> "ModelBundle":
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta.get("features") != FEATURES:
            raise ValueError(
                "Model is getraind met een andere featurelijst; opnieuw trainen")
        hs = [int(h) for h in meta["horizons"]]
        boosters = {h: lgb.Booster(
            model_file=str(d / f"h{h}.txt")) for h in hs}
        cals = {}
        for h in hs:
            c = json.loads((d / f"h{h}_cal.json").read_text(encoding="utf-8"))
            cals[h] = IsoCal(c["xs"], c["ys"])
        extra = {k: v for k, v in meta.items() if k not in (
            "version", "horizons", "features")}
        return ModelBundle(meta["version"], hs, boosters, cals, extra)


def set_current(root: Path, version: str) -> None:
    """Atomische vervanging van de wijzer naar het actieve model."""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "current.json.tmp"
    tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
    os.replace(tmp, root / "current.json")


def load_current(root: Path) -> Optional[ModelBundle]:
    p = root / "current.json"
    if not p.exists():
        return None
    version = json.loads(p.read_text(encoding="utf-8"))["version"]
    d = root / version
    return ModelBundle.load(d) if d.exists() else None
