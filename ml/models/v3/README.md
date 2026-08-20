# Spot ML V3 — Shadow Only

This directory contains the leakage-safe E2 (BTC + market regime) research
artifact used only for forward observation. It must not accept, reject, rank,
or resize trades.

- `v3_e2_btc_regime.joblib`: trained preprocessing and classifier pipeline
- `feature_schema.json`: ordered model input contract
- `metadata.json`: dataset, validation, candle policy, and artifact checksum

The scorer only uses BTC 4H candles whose close time is strictly earlier than
the scoring timestamp. Any load, feature, network, or inference failure is
fail-open and leaves normal Spot execution unchanged.
