#!/usr/bin/env python3
"""Minimal dongle test -- does pyrtlsdr open the RTL-SDR at all?

Run this on the Pi:  python3 test_dongle.py
It bypasses rb-vor entirely and just tries to grab 1 second of samples.
"""
import sys, time

print("1. importing rtlsdr ...", flush=True)
try:
    from rtlsdr import RtlSdr
except Exception as e:
    print(f"   FAILED to import: {e}")
    sys.exit(1)
print("   ok")

print("2. opening device (this is where it hangs if the USB claim fails) ...", flush=True)
t0 = time.time()
try:
    sdr = RtlSdr()
except Exception as e:
    print(f"   FAILED to open: {e}")
    print("   -> almost always: DVB-T driver holding the dongle, or no librtlsdr")
    sys.exit(2)
print(f"   opened in {time.time()-t0:.1f}s")

print("3. configuring for 109.4 MHz VOR ...", flush=True)
sdr.sample_rate = 1_024_000
sdr.center_freq = 109_144_000     # 109.4 MHz - 256 kHz offset
sdr.gain = 32.8
print("   ok")

print("4. reading 1 second of samples ...", flush=True)
t0 = time.time()
try:
    samples = sdr.read_samples(1_024_000)
except Exception as e:
    print(f"   FAILED to read: {e}")
    sdr.close(); sys.exit(3)
print(f"   read {len(samples)} samples in {time.time()-t0:.1f}s")

import numpy as np
power = np.mean(np.abs(samples)**2)
print(f"5. mean power: {power:.4f}  (near 0 = no signal/gain; >0.001 = receiving something)")
sdr.close()
print("\nSUCCESS -- the dongle works with pyrtlsdr. rb-vor should too.")
