# Correct exact realtime transcription discovery hints

## Cause
The frozen provider catalog classifies `gpt-realtime-whisper` as audio_transcription (file STT) and has no mode for `gpt-live-transcribe`. Both exact models are implemented in Lumen's OpenAI realtime transport as transcription-only sessions.

## Change
Use the existing exact OpenAI/Gemini realtime transport allowlists before catalog operation hints. Preserve unknown/foreign-provider boundaries and the advisory nature of discovery. Do not mutate registered model kinds, prices, activation or provider credentials.

## Operator contract
Explicitly correct existing rows to `model_kind=realtime` while inactive, then enter confirmed duration pricing under `realtime_input_per_minute`; do not migrate file-STT `audio_input_per_minute` or invent an output rate. The native/compat transcription WebSocket exists, but Afterglow's current conversation UI does not implement transcription-only sessions.

## Evidence
- [x] Exact discovery response regression for both model names, with a misleading/missing frozen catalog fixture.
- [x] Existing transport validates input-only duration pricing and the transcription endpoint without provider I/O.
- [x] Focused discovery/realtime/scanner tests202 passed; touched Python Ruff passed.
