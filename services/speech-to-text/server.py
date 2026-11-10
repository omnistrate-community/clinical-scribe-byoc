"""Speech-to-text service backed by faster-whisper.

It exposes POST /v1/audio/transcriptions with the same multipart shape as the
OpenAI transcription API, so the agent can also point at any compatible server.
"""

import os
import subprocess
import tempfile

import numpy as np

from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel

MODEL_NAME = os.environ.get("WHISPER_MODEL", "base.en")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/models")

app = FastAPI(title="speech-to-text")
model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE, download_root=MODEL_DIR)


def decode_audio(path: str) -> np.ndarray:
    """Decode any ffmpeg-readable file to 16 kHz mono float32 samples."""
    pcm = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model_name: str = Form("whisper", alias="model")):
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp.flush()
        segments, info = model.transcribe(decode_audio(tmp.name), vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments)
    return {"text": text, "language": info.language, "duration": info.duration}
