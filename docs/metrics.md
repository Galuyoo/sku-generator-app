# Production metrics

## Scope
Capture generation jobs, products/variants/SKUs generated, duplicate prevention, image mapping, Shopify publication outcomes, processing duration, workload size and application version. Do not encode a claimed productivity percentage.

## Storage
Telemetry uses a small append-only local JSONL file by default. Set `METRICS_PATH` to a persistent production location for deployed use.

## Events
Recommended event names include `generation_started`, `generation_completed`, `generation_failed`, `shopify_publish_completed` and `shopify_publish_failed`.

## Privacy and reliability
No customer PII, credentials, raw Shopify payloads or image contents. Telemetry is best-effort and must not block generation/publishing.

## Export
JSONL can be loaded directly by Python or converted to CSV. Compare actual duration/workload measurements with a separately documented manual benchmark before making any productivity claim.
