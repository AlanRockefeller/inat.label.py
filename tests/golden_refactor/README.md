# Golden Refactor Verification

These scripts generate and compare renderer-sensitive golden outputs from local
mocked data. They avoid network calls and are intended for refactor gates.

Typical usage:

```sh
env PYTHONHASHSEED=0 SOURCE_DATE_EPOCH=0 TZ=UTC INAT_MAX_WORKERS=1 INAT_RATE_LIMIT_RPM=0 INAT_QUIET=1 \
  .venv/bin/python tests/golden_refactor/generate_current_outputs.py --output-dir /tmp/inat_goldens/current

.venv/bin/python tests/golden_refactor/compare_outputs.py \
  --before /tmp/inat_goldens/before \
  --after /tmp/inat_goldens/current
```
