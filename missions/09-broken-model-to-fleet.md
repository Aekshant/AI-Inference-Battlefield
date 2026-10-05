# Mission 09: Broken model to the fleet

**Setup:** `make fleet-rollout VERSION=edge-v2-bad`. The model fails 30% of local inferences.

**Result:** the rollout went to the canary ring. After about 12 s the gate saw a **28% error rate on 3 updated devices**, halted, and pointed every ring back at `edge-v1`. Blast radius: **4 / 50 devices (8%)**. The early and broad rings never received the bad model. Offline devices don't block a ring: only online devices that are already on the new version are judged.
