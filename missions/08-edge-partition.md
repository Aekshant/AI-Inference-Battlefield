# Mission 08: Edge devices go dark

**Setup:** 50 simulated devices (2 pods × 25), split into rings by hash: canary 4, early 17, broad 29. `make edge-partition DUR=40` cuts 40% of them off.

**Result:** 20 devices went offline (as the hub saw it) but kept running inference locally and buffered 208 records. After the partition healed, **280 late records arrived and 0 were dropped**. Buffers are bounded (300 records per device, oldest dropped first and counted), so a long partition loses old telemetry instead of memory.
