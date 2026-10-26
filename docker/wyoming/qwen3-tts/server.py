# Wyoming text-to-speech bridge for Qwen3-TTS (2026-10-24, live test). the owner's favorite in the blind voice test was
# Voice 10 (Qwen3-TTS 0.6B CustomVoice, speaker Ryan + a brisk/friendly instruction) sped up 1.2x; the PyTorch package
# was 10-13 s per sentence, the qwentts.cpp GGUF port on a 3090 is ~0.8 s. This bridge has no model: it posts the text to
# qwentts.cpp's OpenAI-style tts-server, speeds the result up with sox tempo (pitch kept; the GGUF voice is ~9% faster
# than PyTorch, so 1.1x here = Voice 10 at 1.2x) and returns it as one Wyoming audio stream. 4-digit numbers are spoken
# as pairs before synthesis (the model read 1105 as "eleventy 0 5"), and since 10-25 every other number and unit is
# spelled out too (spoken()); a word map (words.json) can fix other words.
# Sampling temperature 0.7 (server default 0.9) to cut one-off glitches.
# Clone mode (--voice + --voice-wav/--voice-text): the server runs the Base model and speaks every reply in the voice of
# one reference clip (made once with the 1.7B VoiceDesign model from the owner's description), because the CustomVoice
# speaker drifted in pitch, loudness and accent between replies. The server keeps cloned voices in memory only, so the
# bridge registers the clip at startup and again whenever a request fails (server restarted).
import argparse, asyncio, base64, json, logging, re, subprocess, time, urllib.request
from functools import partial

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from num2words import num2words
from wyoming.tts import Synthesize

log = logging.getLogger("qwen3-tts")
VERSION = "0.1.0"
RATE, WIDTH, CHANNELS = 24000, 2, 1
CHUNK = 4096
# 10-24: "friendly" let the voice turn anxious on error replies ("couldn't find a device"); the owner's own voice description.
INSTRUCT = ("A professional, mid-range male voice in his late 30s with a completely steady, measured, and unhurried pace. "
            "The tone is perfectly smooth, analytical, and objective\u2014resembling a clinical surgeon or an emergency "
            "operator. He speaks with absolute composure, a slight lower register, and crisp diction. There is zero trace "
            "of fear, hesitation, or emotional volatility, maintaining strict neutrality even under stress.")


FOUR_DIGITS = re.compile(r"(?<![\d.,:$])\b(\d{4})\b(?![.,:]\d)")


def pairs(m):
    """1105 → eleven oh five, 2026 → twenty twenty-six, 1500 → fifteen hundred (house numbers, years)."""
    n = int(m.group(1))
    a, b = divmod(n, 100)
    if n < 1000 or a % 10 == 0 and b == 0:   # 2000, 3000: "two thousand"
        return num2words(n)
    if b == 0:
        return f"{num2words(a)} hundred"
    return f"{num2words(a)} {'oh ' + num2words(b) if b < 10 else num2words(b)}"


class Words:
    """Regex → replacement pairs from a JSON file, applied in order (case-insensitive)."""
    def __init__(self, path):
        try:
            self.rules = [(re.compile(p, re.I), r) for p, r in json.load(open(path))]
        except FileNotFoundError:
            self.rules = []

    def __call__(self, text):
        for p, r in self.rules:
            text = p.sub(r, text)
        return spoken(text)


# 10-25: the model also garbles short numbers as digits: HA's weather intent says "50 °F and clear" and the voice said
# "Five T degrees" / "Four five two degrees" (the owner heard "40 50 degrees"); spelled out it was clean 6/6 round trips
# through Qwen3-ASR. So every number and unit is turned into words before synthesis.
UNITS = [(re.compile(p, re.I), r) for p, r in [
    (r"\s*°\s*F\b", " degrees"), (r"\s*°\s*C\b", " degrees Celsius"), (r"\s*°", " degrees"),
    (r"(\d)\s*%", r"\1 percent"), (r"(\d)\s*mph\b", r"\1 miles per hour"), (r"(\d)\s*km/h\b", r"\1 kilometers per hour"),
    (r"(\d)\s*kWh\b", r"\1 kilowatt hours"), (r"(\d)\s*kW\b", r"\1 kilowatts"), (r"(\d)\s*W\b", r"\1 watts"),
    (r"(\d)\s*Mbps\b", r"\1 megabits per second"), (r"(\d)\s*Gbps\b", r"\1 gigabits per second")]]
DATE = re.compile(r"\b(1[0-2]|0?[1-9])/(3[01]|[12]\d|0?[1-9])(?:/(\d{2,4}))?\b")   # 10/28 → October twenty-eighth
MONTHS = "January February March April May June July August September October November December".split()
TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
INT = r"(?:\d{1,3}(?:,\d{3})+|\d+)"   # 1,200 is one number; "45, high" is not
MONEY = re.compile(r"\$(" + INT + r")(?:\.(\d{2}))?\b")
ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b", re.I)
DOTTED = re.compile(r"\b\d+(?:\.\d+){2,}\b")   # 1.6.3 → one point six point three
RANGE = re.compile(r"(?<=\d)\s*[-–]\s*(?=\d)")   # 40-50 → 40 to 50
NUMBER = re.compile(r"(?<![\w.])(-?)(" + INT + r"(?:\.\d+)?)(?![\w])")


def clock(m):
    """7:05 → seven oh five, 7:00 → seven, 12:30 → twelve thirty."""
    h, mm = int(m.group(1)), int(m.group(2))
    if mm == 0:
        return num2words(h)
    return f"{num2words(h)} {'oh ' + num2words(mm) if mm < 10 else num2words(mm)}"


def money(m):
    d = int(m.group(1).replace(",", ""))
    words = f"{num2words(d)} dollar{'s' if d != 1 else ''}"
    return words + (f" {num2words(int(m.group(2)))}" if m.group(2) and int(m.group(2)) else "")


def number(m):
    s = m.group(2).replace(",", "")
    n = float(s) if "." in s else int(s)
    return ("minus " if m.group(1) else "") + num2words(n)


def spoken(text):
    """Numbers and units → words: 50 °F → fifty degrees, 2.5 mph → two point five miles per hour, 1105 → eleven oh five."""
    for p, r in UNITS:
        text = p.sub(r, text)
    text = DATE.sub(lambda m: f"{MONTHS[int(m.group(1)) - 1]} {num2words(int(m.group(2)), to='ordinal')}"
                    + (f" {m.group(3) if len(m.group(3)) == 4 else '20' + m.group(3)}" if m.group(3) else ""), text)
    text = TIME.sub(clock, text)
    text = MONEY.sub(money, text)
    text = ORDINAL.sub(lambda m: num2words(int(m.group(1)), to="ordinal"), text)
    text = DOTTED.sub(lambda m: " point ".join(num2words(int(x)) for x in m.group(0).split(".")), text)
    text = RANGE.sub(" to ", text)
    text = FOUR_DIGITS.sub(pairs, text)
    return NUMBER.sub(number, text)


def register_voice(url, name, wav, ref_text, timeout):
    """Blocking: (re)register the clone voice from its reference WAV + transcript."""
    with urllib.request.urlopen(url + "/v1/audio/voices", timeout=timeout) as r:
        if name in r.read().decode():
            return
    body = json.dumps({"name": name, "ref_text": open(ref_text).read().strip(),
                       "wav_b64": base64.b64encode(open(wav, "rb").read()).decode()}).encode()
    req = urllib.request.Request(url + "/v1/audio/voices", body, {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=timeout).read()
    log.info("registered clone voice %s from %s", name, wav)


def synth(url, speaker, tempo, temperature, timeout, text, clone=False):
    """Blocking: text → s16le mono 24 kHz pcm, sped up by sox tempo."""
    req = {"input": text, "voice": speaker, "language": "English", "response_format": "pcm", "temperature": temperature}
    if not clone:   # Base model rejects instructions; the clip carries the style
        req["instructions"] = INSTRUCT
    body = json.dumps(req).encode()
    req = urllib.request.Request(url + "/v1/audio/speech", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        pcm = r.read()
    if tempo != 1.0 and pcm:
        raw = ["-t", "raw", "-r", str(RATE), "-e", "signed", "-b", "16", "-c", "1"]
        pcm = subprocess.run(["sox", *raw, "-", *raw, "-", "tempo", "-s", str(tempo)], input=pcm,
                             capture_output=True, check=True, timeout=timeout).stdout
    return pcm


class Handler(AsyncEventHandler):
    def __init__(self, info, args, words, lock, *a, **kw):
        super().__init__(*a, **kw)
        self.info, self.args, self.words, self.lock = info, args, words, lock

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info.event())
            return True
        if not Synthesize.is_type(event.type):
            return True
        text = self.words(Synthesize.from_event(event).text.strip())
        t, a = time.monotonic(), self.args
        run = partial(synth, a.tts_url, a.speaker, a.tempo, a.temperature, a.timeout, text, bool(a.voice_wav))
        try:
            async with self.lock:   # one request at a time on the GPU
                try:
                    pcm = await asyncio.wait_for(asyncio.to_thread(run), timeout=a.timeout + 5)
                except Exception:
                    if not a.voice_wav:
                        raise
                    log.warning("synthesis failed, re-registering clone voice and retrying once")
                    await asyncio.to_thread(register_voice, a.tts_url, a.speaker, a.voice_wav, a.voice_text, a.timeout)
                    pcm = await asyncio.wait_for(asyncio.to_thread(run), timeout=a.timeout + 5)
        except Exception as e:
            log.exception("synthesis failed: %s", e)
            await self.write_event(Error(text=f"qwen3-tts failed: {e}", code="tts-failed").event())
            return False
        log.info("%.2fs audio in %.2fs: %s", len(pcm) / WIDTH / RATE, time.monotonic() - t, text)
        await self.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        for i in range(0, len(pcm), CHUNK):
            await self.write_event(AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=pcm[i:i + CHUNK]).event())
        await self.write_event(AudioStop().event())
        return True


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uri", default="tcp://0.0.0.0:10202")
    p.add_argument("--tts-url", default="http://127.0.0.1:8099")
    p.add_argument("--speaker", default="ryan", help="CustomVoice speaker, or the clone voice name with --voice-wav")
    p.add_argument("--voice-wav", help="clone reference clip (Base model server)")
    p.add_argument("--voice-text", help="transcript of the clone reference clip")
    p.add_argument("--tempo", type=float, default=1.1)
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--words", default="/app/words.json")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    attr = Attribution(name="Qwen (Alibaba) / qwentts.cpp", url="https://github.com/ServeurpersoCom/qwentts.cpp")
    info = Info(tts=[TtsProgram(
        name="qwen3-tts", description="Qwen3-TTS (GGUF, GPU)", version=VERSION, installed=True,
        attribution=attr, voices=[TtsVoice(name=a.speaker, description=f"Qwen3-TTS {a.speaker} x{a.tempo}",
                                           languages=["en"], installed=True, version=VERSION, attribution=attr)])])
    words = Words(a.words)
    if a.voice_wav:
        try:
            register_voice(a.tts_url, a.speaker, a.voice_wav, a.voice_text, a.timeout)
        except Exception as e:   # server not up yet: the first request registers it
            log.warning("clone voice not registered at startup: %s", e)
    log.info("listening on %s → %s, speaker %s, tempo %.2f, temperature %.2f (%d word fixes)", a.uri, a.tts_url,
             a.speaker, a.tempo, a.temperature, len(words.rules))
    await AsyncServer.from_uri(a.uri).run(partial(Handler, info, a, words, asyncio.Lock()))


if __name__ == "__main__":
    asyncio.run(main())
