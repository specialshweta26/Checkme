# Voice input

The microphone button in the chat's message box (`apps/web/app/components/VoiceButton.tsx`) records up to two
minutes. Press it again to stop. The words appear in the message box, added to anything already typed, so the person
can read and fix them before sending. Nothing is sent on its own.

- API: `POST /api/voice/transcribe` (`apps/api/app/voice/routes.py`), multipart `audio` plus `seconds` and
  `language` (the browser's, as a hint). Formats: webm, ogg, mp4/m4a, mp3, wav, up to 8 MB.
- Model: `TRANSCRIBE_MODEL` (default `gpt-4o-mini-transcribe`) on the existing `OPENAI_API_KEY`. `VOICE_INPUT=false`
  hides the button.
- The audio isn't stored. Each recording counts toward usage as `transcribe:seconds` under chat. The daily cap
  (docs/ROLLOUT.md) and a limit of 20 recordings a minute apply.
- The desktop app doesn't show the button yet: its macOS bundle has to declare microphone use first
  (`NSMicrophoneUsageDescription`), which needs a desktop release.
