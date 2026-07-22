# creative-tester

A deployed AWS service that submits media-asset URLs to the dev media-classifier,
paces the submissions (the Gemini backend blocks bursts — 200/hr ≈ one every 18s),
collects results via webhook push with a polling fallback, stores everything for 30
days under a **job id** you can query, and compares results when the same URL was run
against two endpoints.

It is the deployed successor to the local `prod-vs-dev/dev_batch.py` +
`build_comparison.py` scripts, reusing their request/pace/poll/stall and tag-verdict
logic.

## Architecture

```
POST /jobs (urls, endpoints?, gap?, compare_on_complete?)
      │  api Lambda → writes Job + Result rows (status=pending)
      ▼
DynamoDB single table  ${stack}-jobs  (PAY_PER_REQUEST, TTL 30d)
      ▲   ▲   ▲
      │   │   └── pacer Lambda (EventBridge 1m, reserved-concurrency 1)
      │   │        every 18s: win a slot on RATE#dev (atomic CAS) → POST classifier
      │   │        with callback_url → flip row pending→submitted
      │   ├────── callback Lambda  POST /callback/{secret}
      │   │        classifier pushes result → fan out to all rows sharing the job_id
      │   └────── poller Lambda (EventBridge 2m)
      │            fallback: GET /jobs/{id} for anything the callback missed;
      │            mark rows stuck > 10 min as stalled
      └────────── compare Lambda (async, separate job)
                   diff two result rows → normalized verdict → CMP# row
```

Why these choices (full rationale in the plan): the 18s gap is **global** across all
batches, so a single reserved-concurrency-1 pacer draining one FIFO queue, gated by an
atomic CAS on a `RATE#<classifier>` record, is the correct rate valve — not per-batch
Step Functions waits. Job completion is derived from the result rows themselves, so a
crash between writes can't wedge a job open.

## Data model (single table, generic `pk`/`sk`)

| Entity | pk | sk |
|---|---|---|
| Job | `JOB#<job_id>` | `META` |
| Result (per url×endpoint) | `JOB#<job_id>` | `RESULT#<endpoint>#<url_hash>` |
| Comparison | `CMP#<comparison_id>` | `META` |
| Rate gate | `RATE#<classifier_id>` | `PACE` |

GSIs: `classifier-job-index` (classifier_job_id → all rows, for the dedup fan-out) and
`submit-state-index` (submit_state + state_ts, the pending FIFO and the in-flight sweep).

## HTTP API

| Route | Purpose |
|---|---|
| `POST /jobs` | body `{urls: "a,b,c"｜[...], endpoints?: ["auto"｜"dev-video"｜"dev-image"], gap_seconds?, compare_on_complete?}` → `{job_id, enqueued_rows, skipped}` |
| `GET /jobs/{job_id}` | status + per-status counts |
| `GET /jobs/{job_id}/results` | every result row (raw + normalized) |
| `POST /compare` | body `{left:{job_id,endpoint,url}, right:{...}}` → `{comparison_id}` (runs as a separate job) |
| `GET /comparisons/{comparison_id}` | the comparison verdict |
| `POST /callback/{secret}` | classifier webhook (not for callers) |

### Auth — all routes require AWS IAM (SigV4)

Every route except the webhook callback requires a SigV4-signed request from an IAM
identity allowed `execute-api:Invoke` (any admin-ish user/role in the account works).
Unsigned calls get 403 before reaching a Lambda. The callback route is instead
protected by the high-entropy secret embedded in its path.

With curl (≥ 7.75), using your normal AWS CLI credentials:

```bash
eval $(aws configure export-credentials --format env)   # loads KEY/SECRET/TOKEN

curl --aws-sigv4 "aws:amz:us-east-1:execute-api" \
     --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
     ${AWS_SESSION_TOKEN:+-H "x-amz-security-token: $AWS_SESSION_TOKEN"} \
     -XPOST "$API/jobs" -H 'content-type: application/json' \
     -d '{"urls": ["https://cdn.example.com/a.mp4"]}'
```

GETs are the same minus the body, e.g. `... "$API/jobs/JOB_ID/results"`.

From Postman: Authorization type **AWS Signature** with AccessKey, SecretKey,
Region `us-east-1`, Service Name `execute-api` (plus Session Token when using
temporary/SSO credentials). Any AWS SDK works too — e.g. boto3 with
`requests-aws4auth`, or `awscurl`.

## Develop

```
make test       # pytest — pure logic + moto-backed state machine (22 tests, no AWS)
make validate   # sam validate --lint
make build      # sam build
make deploy     # sam build && sam deploy   (needs AWS creds; see POST_DEPLOY.md)
```

## Adding an endpoint (e.g. prod)

Add an entry to `ENDPOINTS` in `shared/src/ct_shared/config.py` with a distinct
`classifier_id` (→ its own independent submit gap) and its own `api_key_secret`; add the
secret to the template. If its tag vocabulary is granular, add it to
`_GRANULAR_CLASSIFIERS` in `compare.py` so it collapses into the canonical set via
`TAG_MAP`. No handler changes.

## Known caveats

- **Callback param name is unconfirmed** for the dev classifier (`CALLBACK_PARAM`,
  default `callback_url`). The poll fallback keeps the service correct even if the
  callback never fires. See POST_DEPLOY.md to verify and flip it.
- **The dev classifier silently stalls** ~50% of images and ~15% of videos (measured):
  jobs never return a terminal status. The poller marks these `stalled` after 10 min so
  jobs don't hang forever; the classifier dedupes by URL, so a stalled job cannot be
  revived by re-submitting — that needs a fix on the classifier side.
- With **dev-only** endpoints, the built-in comparison pair (video vs image) only fires
  when you deliberately send one URL to both; it's mainly there for when a prod endpoint
  is added.
