## Why

Adding weekly API-key limits made both monthly and weekly fields required on the existing PATCH endpoint. Existing monthly-only clients now receive 422 instead of updating their monthly limit.

## What Changes

Restore partial PATCH semantics: omitted limit fields remain unchanged, explicitly null fields clear their limit, and an empty body remains invalid. Update the service boundary to receive only supplied fields and cover monthly-only requests with regression tests.
