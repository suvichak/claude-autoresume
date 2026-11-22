# Reference — what Claude Code writes when a session dies

Everything the watchdog needs is already in the session transcript at
`~/.claude/projects/<project-slug>/<session-id>.jsonl`, one JSON object per
line. Findings from 74 local transcripts, 2026-09-20.

## A quota stop

```json
{
  "type": "assistant",
  "error": "rate_limit",
  "apiErrorStatus": 429,
  "sessionId": "2dee128d-…",
  "uuid": "…",
  "timestamp": "2026-09-09T21:01:39.048Z",
  "quotaLimits": {
    "status": "rejected",
    "resetsAt": 1788819000,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "out_of_credits",
    "unifiedRateLimitFallbackAvailable": false,
    "isUsingOverage": false,
    "upgradePaths": ["upgrade_plan"]
  },
  "message": {"content": [{"type": "text",
     "text": "You've hit your session limit · resets 5:10am (Asia/Bangkok)"}]}
}
```

`resetsAt` is a **unix epoch in seconds** and is authoritative — across 7 real
limit hits it matched the human-readable text every time:

```
err=09-08 05:00  reset=09-08 05:10  | resets 5:10am
err=09-10 04:01  reset=09-10 04:20  | resets 4:20am
err=09-10 23:57  reset=09-11 00:00  | resets 12am
err=09-11 23:38  reset=09-12 01:20  | resets 1:20am
err=09-15 04:09  reset=09-15 04:30  | resets 4:30am
```

Note how short those gaps are — 3 to 20 minutes. Limits are usually hit near the
end of a window, so the wait is often brief.

## Every other way a session ends

None of these carry `apiErrorStatus`, and none should be retried unattended:

| `error` value | count | what it is |
|---|---|---|
| `{"message": "Connection error.", …}` | 12 | network dropped |
| `{"message": "529 {\"type\":\"error\"…"}` | 7 | API overloaded |
| `server_error` | 1 | server-side failure |
| *(text)* `API Error: Connection lost mid-response` | 1 | truncated stream |

## Reading the tail correctly

A rate-limited transcript does **not** end on the 429 record. Real tail:

```
type=user         …the last prompt
type=atis-latch
type=assistant    error=rate_limit  apiErrorStatus=429
type=last-prompt
```

So "did the session continue past the stop?" must only count `assistant` and
`user` records. Bookkeeping records — `last-prompt`, `atis-latch`, `attachment`
— follow a 429 normally and must not be mistaken for continuation.

Duplicate 429 records for the same stop are also normal (seen 2× per hit), which
is why resumes dedupe on the record `uuid`.

Used by [[README]] / [[runbook]].
