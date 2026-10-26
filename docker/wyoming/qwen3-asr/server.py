# Wyoming speech-to-text server for Qwen3-ASR-0.6B on CPU (2026-10-23). Won the owner's 4-model test: 15/15 phrases right
# (tablet, 14 ft around the corner, and with the TV on) vs Whisper base 9/15, small 14/15.
# Behaves like wyoming-faster-whisper: Describe → Info; Transcribe + AudioStart/Chunk/Stop → one Transcript, then the
# connection closes. Audio is converted to 16 kHz mono 16-bit. One model on one worker thread (calls never overlap); a small
# word map fixes spellings the model makes (e.g. a model name spelled out letter by letter).
import argparse, asyncio, json, logging, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import numpy as np
import torch
from qwen_asr import Qwen3ASRModel
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

log = logging.getLogger("qwen3-asr")
VERSION = "1.0.1"
REQUEST_DEADLINE_S = 30   # HA gets an empty transcript if a request takes longer
STUCK_EXIT_S = 60         # a model call running this long means a stuck worker: exit, docker restarts us
# One worker thread owns the model, so calls can never overlap (even if a handler is cancelled mid-call).
MODEL_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
_running_since = [None]


def _watchdog():
    while True:
        time.sleep(5)
        t = _running_since[0]
        if t is not None and time.monotonic() - t > STUCK_EXIT_S:
            log.error("model call stuck for %ds: exiting so docker restarts the server", STUCK_EXIT_S)
            os._exit(1)


class Fixes:
    """Regex → replacement pairs from a JSON file, applied in order (case-insensitive). Reloaded when the file changes."""
    def __init__(self, path):
        self.path, self.mtime, self.rules = path, None, []

    def __call__(self, text):
        try:
            m = os.path.getmtime(self.path)
            if m != self.mtime:
                self.rules = [(re.compile(p, re.I), r) for p, r in json.load(open(self.path))]
                self.mtime = m
                log.info("fixes: %d rules from %s", len(self.rules), self.path)
        except FileNotFoundError:
            self.rules = []
        except Exception as e:   # bad edit: keep the last good rules
            log.warning("fixes reload failed, keeping %d old rules: %s", len(self.rules), e)
        for p, r in self.rules:
            text = p.sub(r, text)
        return text


class Vocab:
    """Terms (one per line, # comments) joined into the model's context to bias spelling. Reloaded when the file changes."""
    def __init__(self, path):
        self.path, self.mtime, self.text = path, None, ""

    def __call__(self):
        try:
            m = os.path.getmtime(self.path)
            if m != self.mtime:
                terms = [l.strip() for l in open(self.path) if l.strip() and not l.lstrip().startswith("#")]
                self.text, self.mtime = ", ".join(terms), m
                log.info("vocab: %d terms from %s", len(terms), self.path)
        except FileNotFoundError:
            self.text = ""
        return self.text


class Handler(AsyncEventHandler):
    def __init__(self, info, model, lock, fixes, vocab, *a, **kw):
        super().__init__(*a, **kw)
        self.info, self.model, self.lock, self.fixes, self.vocab = info, model, lock, fixes, vocab
        self.conv = AudioChunkConverter(rate=16000, width=2, channels=1)
        self.audio = bytearray()

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info.event())
            return True
        if Transcribe.is_type(event.type) or AudioStart.is_type(event.type):
            if AudioStart.is_type(event.type):
                self.audio = bytearray()
            return True
        if AudioChunk.is_type(event.type):
            try:
                self.audio += self.conv.convert(AudioChunk.from_event(event)).audio
            except Exception as e:   # keep going; the transcript will just miss this chunk
                log.warning("audio conversion failed: %s", e)
            return True
        if AudioStop.is_type(event.type):
            t, text, raw = time.monotonic(), "", ""
            try:
                a = np.frombuffer(bytes(self.audio), np.int16).astype(np.float32) / 32768.0
                if len(a) > 1600:
                    fut = asyncio.get_running_loop().run_in_executor(MODEL_THREAD, partial(self._run, a))
                    raw = await asyncio.wait_for(asyncio.shield(fut), timeout=REQUEST_DEADLINE_S)
                text = self.fixes(raw).strip()
                log.info("%.2fs audio → %.2fs: %s%s", len(a) / 16000, time.monotonic() - t, text,
                         f"  (raw: {raw})" if raw != text else "")
            except asyncio.TimeoutError:
                log.warning("request over %ds: sending an empty transcript", REQUEST_DEADLINE_S)
            except Exception as e:
                log.exception("transcription failed (%s): sending an empty transcript", e)
            try:
                await self.write_event(Transcript(text=text).event())   # HA always gets exactly one Transcript
            except Exception as e:
                log.warning("could not send transcript: %s", e)
            return False   # one transcript per connection, like wyoming-faster-whisper
        return True

    def _run(self, a):
        _running_since[0] = time.monotonic()
        try:
            ctx = self.vocab()
            text = self.model.transcribe(audio=(a, 16000), context=ctx, language="English")[0].text
            # On silence or noise the model can recite the context back; then redo it without the vocab.
            if ctx and len({t.strip().lower() for t in text.split(",")} & {t.lower() for t in ctx.split(", ")}) >= 3:
                log.info("transcript echoed the vocab; retrying without it")
                text = self.model.transcribe(audio=(a, 16000), language="English")[0].text
            return text
        finally:
            _running_since[0] = None


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uri", default="tcp://0.0.0.0:10302")
    p.add_argument("--model", default="Qwen/Qwen3-ASR-0.6B")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--fixes", default="/app/fixes.json")
    p.add_argument("--vocab", default="/app/vocab.txt")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    torch.set_num_threads(a.threads)
    t = time.monotonic()
    model = Qwen3ASRModel.from_pretrained(a.model, dtype=torch.float32, device_map="cpu",
                                          max_inference_batch_size=1, max_new_tokens=128)
    model.transcribe(audio=(np.zeros(16000, np.float32), 16000), language="English")   # warm-up
    log.info("model %s loaded + warmed in %.1fs, %d threads", a.model, time.monotonic() - t, a.threads)
    info = Info(asr=[AsrProgram(
        name="qwen3-asr", description="Qwen3-ASR-0.6B on CPU", version=VERSION, installed=True,
        attribution=Attribution(name="Qwen (Alibaba)", url="https://huggingface.co/Qwen/Qwen3-ASR-0.6B"),
        models=[AsrModel(name="Qwen3-ASR-0.6B", description="Qwen3-ASR 0.6B fp32", languages=["en"], installed=True,
                         version=VERSION,
                         attribution=Attribution(name="Qwen (Alibaba)", url="https://huggingface.co/Qwen/Qwen3-ASR-0.6B"))])])
    threading.Thread(target=_watchdog, daemon=True).start()
    server = AsyncServer.from_uri(a.uri)
    lock, fixes, vocab = None, Fixes(a.fixes), Vocab(a.vocab)   # serialization is MODEL_THREAD (one worker), not an asyncio lock
    fixes(""); vocab()   # load both files now so the startup log shows the real counts
    log.info("listening on %s (%d word fixes, %d vocab chars)", a.uri, len(fixes.rules), len(vocab.text))
    await server.run(partial(Handler, info, model, lock, fixes, vocab))


if __name__ == "__main__":
    asyncio.run(main())
