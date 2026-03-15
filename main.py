from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import edge_tts
import pdfplumber
import re
from num2words import num2words
import io
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CID_MAP = {
    224: 'à', 225: 'á', 226: 'â', 227: 'ã', 228: 'ä',
    232: 'è', 233: 'é', 234: 'ê', 235: 'ë',
    236: 'ì', 237: 'í', 238: 'î', 239: 'ï',
    242: 'ò', 243: 'ó', 244: 'ô', 245: 'õ', 246: 'ö',
    249: 'ù', 250: 'ú', 251: 'û', 252: 'ü',
    231: 'ç', 241: 'ñ',
    192: 'À', 193: 'Á', 194: 'Â', 195: 'Ã',
    200: 'È', 201: 'É', 202: 'Ê',
    205: 'Í', 211: 'Ó', 212: 'Ô', 213: 'Õ',
    218: 'Ú', 199: 'Ç',
}

def corrigir_cid(text: str) -> str:
    if not text:
        return ''
    def replace_cid(m):
        return CID_MAP.get(int(m.group(1)), '')
    return re.sub(r'\(cid:(\d+)\)', replace_cid, text)

def formatar_texto(text: str) -> str:
    text = corrigir_cid(text)
    text = re.sub(r'-\n', '', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r'-\s+', '', text)
    text = re.sub(r'[§ºª°]', ' ', text)
    text = re.sub(r'(Art\.|art\.)', r'\1,', text)
    text = re.sub(r'\b\d+\b', lambda m: num2words(int(m.group()), lang='pt_BR'), text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

@app.get("/")
def index():
    return FileResponse("index.html")

@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    content = await file.read()
    paragraphs = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            text = corrigir_cid(text)
            text = re.sub(r'-\n', '', text)
            text = re.sub(r'\n', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            chunks = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 20]
            paragraphs.extend(chunks)
    return JSONResponse({"paragraphs": paragraphs})

class TTSRequest(BaseModel):
    paragraphs: list[str]
    start_index: int = 0
    voice: str = "pt-BR-FranciscaNeural"
    rate: str = "+0%"

async def stream_from_index(paragraphs, start, voice, rate):
    for i in range(start, len(paragraphs)):
        texto = formatar_texto(paragraphs[i])
        if not texto:
            continue
        communicate = edge_tts.Communicate(texto, voice=voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

@app.post("/tts")
async def tts(body: TTSRequest):
    return StreamingResponse(
        stream_from_index(body.paragraphs, body.start_index, body.voice, body.rate),
        media_type="audio/mpeg"
    )
