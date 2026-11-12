# Session revocation experiment

Status: Pre-disable access verified on 2026-10-30. Trial A timing: NOT MEASURED. Trial B: NOT RUN.

Compare disabling a disposable cloud-only user with disabling plus explicit session revocation. Keep the application, session age, memberships, and licensing comparable. Record fresh authenticated server requests, not just a cached page. Repeat both conditions and retain all observations, including no measurable difference.

| Trial | Application/browser | Condition | Disable time | Revoke time | Last successful request | First denied request | Observation limit | Notes |
| A | Microsoft Graph Explorer; Chrome 156.0.8078.48 (64-bit), Incognito | Disable only | 2026-10-30T22:45:54.3788866+00:00 | N/A: no revocation in Trial A | Pending | Pending | Up to 30 min; check every 60 seconds | Baseline: GET /me returned 200 OK for identitylifecycle.graph-test@hybrididentity.site. Observed at 2026-10-30T22:29:32.1000515+00:00. |
| B | Same Graph Explorer and Chrome Incognito setup planned | Disable, then revoke sessions | Not run | Not run | Not run | Not run | Up to 30 min; check every 60 seconds | Requires a fresh sign-in and its own successful GET /me after Trial A. |

Session policies, including Continuous Access Evaluation: not verified for these sessions.

Document Continuous Access Evaluation and other limitations when interpreting results.
