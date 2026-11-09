"""Create tests/data/history.npz: 40 days of synthetic hourly data.

Generated from a model similar to a real floor heated house (screed tau 4 h,
room tau ~21 h), with a heating curve, offset steps, sun gains and sensor noise.
"""
import numpy as np
from pathlib import Path

rng = np.random.default_rng(42)
n = 24 * 40
h = np.arange(n)
outdoor = 5 + 4 * np.sin(2 * np.pi * (h - 9) / 24) + np.cumsum(rng.normal(0, 0.12, n))
outdoor = np.clip(outdoor, -8, 16)
mean24 = np.convolve(outdoor, np.ones(24) / 24, mode="same")
supply = 33.5 - 0.47 * mean24 + np.repeat(rng.choice([-2.0, 0.0, 0.0, 2.0], n // 24 + 1), 24)[:n]
sun = np.maximum(0, np.sin(2 * np.pi * (h - 7) / 24)) * rng.uniform(0, 1, n // 24 + 1).repeat(24)[:n]
a, b, c, lam = 0.024, 0.018, 0.03, np.exp(-1 / 4)
y = np.empty(n); y[0] = 22.0; xf = supply[0]
for k in range(n - 1):
    xf_next = lam * xf + (1 - lam) * supply[k]
    y[k + 1] = y[k] + a * (xf - y[k]) + b * (outdoor[k] - y[k]) + c + 0.06 * sun[k]
    xf = xf_next
room_a = np.round(y + rng.normal(0, 0.05, n) - 0.2, 1)
room_b = np.round(y + rng.normal(0, 0.05, n) + 0.2, 1)
room_b[100:103] = np.nan  # a short gap
ts = 1_790_000_000 - 1_790_000_000 % 3600 + h * 3600.0
np.savez(Path(__file__).parent / "data" / "history.npz", ts=ts, room_a=room_a, room_b=room_b,
         supply=np.round(supply, 2), outdoor=np.round(outdoor, 2))
print("ok", np.nanmin(room_a), np.nanmax(room_a), supply.min().round(1), supply.max().round(1))
