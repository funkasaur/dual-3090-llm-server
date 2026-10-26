# Home Assistant voice services (Wyoming)

`docker compose up -d` here runs the speech services Home Assistant's voice pipeline uses, on CCD1 (cpuset
`8-15,24-31`, memory capped) so they never touch the inference cores. Ports bind to the server's LAN address only:
the Wyoming protocol has no authentication. Replace `192.0.2.26` with your server's address.

| Service | Port | What |
|---|---|---|
| `wyoming-qwen3-asr` | 10302 | Speech-to-text: Qwen3-ASR-0.6B fp32 on CPU, 8 threads, ~3.7 GB RAM, ~1-2.3 s per command ([`qwen3-asr/`](qwen3-asr/)) |
| `wyoming-whisper` | 10300 | faster-whisper `base-int8`, English (~0.5 s per command); fallback STT |
| `wyoming-qwen3-tts` | 10202 | Text-to-speech bridge ([`qwen3-tts/`](qwen3-tts/)) to a [qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp) (built from `5916bf7`) `tts-server` on 127.0.0.1:8099 running the Qwen3-TTS 0.6B Base model on one GPU (~2.7 GB VRAM) |
| `wyoming-piper` | 10200 | Piper `en_US-lessac-medium`; fallback TTS |
| `wyoming-openwakeword` | 10400 | openWakeWord, for satellites that don't detect the wake word on the device |

In Home Assistant, add each one with the Wyoming Protocol integration (`<server>:<port>`) and pick them in the voice
pipeline. The conversation agent is the local Qwen model through an OpenAI-compatible endpoint.

## Choosing the speech-to-text model

Four CPU models on the same 15 spoken commands (synthetic far-field clips plus a real voice recorded at a wall tablet:
next to it, ~14 ft away around a corner, and with a TV playing): Qwen3-ASR-0.6B 15/15, Whisper small-int8 14/15,
distil-whisper medium 11/15, Whisper base 9/15. Qwen3-ASR costs ~1-2 s per command on 8 CPU threads, which is fine
for short commands.

`qwen3-asr/server.py` always sends exactly one transcript (empty on an error or a 30 s timeout), runs the model on one
thread at a time, and exits for a Docker restart if a call is stuck for 60 s.

**Speech dictionary.** `qwen3-asr/vocab.txt` (one term per line) is passed to the model as context to bias spelling;
`qwen3-asr/fixes.json` (regex -> replacement) is applied afterwards. Both are mounted at `/cfg` and reload on the next
request after an edit, no rebuild. Cost: ~+0.6-0.9 s per command, so keep the list short. On silence the model can
recite the vocabulary; a transcript holding three or more vocabulary terms as a comma list is redone without it.

## Text-to-speech

The TTS bridge needs a qwentts.cpp server running on the host first: build
[qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp) with CUDA, put the Qwen3-TTS 0.6B Base talker and the
12 Hz tokenizer GGUFs in its `models/`, then install [`systemd/qwentts-server.service`](systemd/qwentts-server.service)
as a user unit and start it before `docker compose up -d`. Without it the bridge starts but every synthesis fails.

`qwen3-tts/server.py` is a Wyoming bridge to qwentts.cpp's HTTP server. It speaks every reply in one cloned voice: the
reference clip in `qwen3-tts/voice/` is synthetic, made once by the Qwen3-TTS 1.7B VoiceDesign model from the text in
`steady-instruct.txt` (seed 15). The bridge registers the clip at startup and again after a server restart (the server
keeps cloned voices in memory only). It plays back at 1.1x through sox, temperature 0.7, and spells out four-digit
numbers as pairs, which the model otherwise misreads. The stock CustomVoice speakers drifted in pitch, loudness and
accent between replies; the clone didn't.
