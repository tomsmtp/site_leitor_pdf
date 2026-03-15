from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import edge_tts
import pdfplumber
import fitz  # pymupdf
import re
from num2words import num2words
import io
import asyncio  # <-- Adicionado para tratar o cancelamento da requisição
from collections import Counter

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CID_MAP = {
    224:'à',225:'á',226:'â',227:'ã',228:'ä',
    232:'è',233:'é',234:'ê',235:'ë',
    236:'ì',237:'í',238:'î',239:'ï',
    242:'ò',243:'ó',244:'ô',245:'õ',246:'ö',
    249:'ù',250:'ú',251:'û',252:'ü',
    231:'ç',241:'ñ',
    192:'À',193:'Á',194:'Â',195:'Ã',
    200:'È',201:'É',202:'Ê',
    205:'Í',211:'Ó',212:'Ô',213:'Õ',
    218:'Ú',199:'Ç',
}

# Pré-compilando Regex para máxima performance
RE_CID = re.compile(r'\(cid:(\d+)\)')
RE_SPACES = re.compile(r'\s+')
RE_URL = re.compile(r'https?://\S+')
RE_WWW = re.compile(r'www\.\S+')
RE_EMAIL = re.compile(r'\S+@\S+\.\S+')
RE_CHARS = re.compile(r'[§ºª°]')
RE_ART = re.compile(r'(Art\.|art\.)')
RE_NUM = re.compile(r'\b\d+\b')
RE_NUM_ONLY = re.compile(r'^\d+$')

ABREVIACOES_COMPILED = [
    (re.compile(r'\bDr\.'), 'Doutor'), (re.compile(r'\bDra\.'), 'Doutora'),
    (re.compile(r'\bSr\.'), 'Senhor'), (re.compile(r'\bSra\.'), 'Senhora'),
    (re.compile(r'\bProf\.'), 'Professor'), (re.compile(r'\bProfa\.'), 'Professora'),
    (re.compile(r'\bEng\.'), 'Engenheiro'), (re.compile(r'\bAdv\.'), 'Advogado'),
    (re.compile(r'\bR\$'), 'reais'), (re.compile(r'\bUS\$'), 'dólares'),
    (re.compile(r'\bEUA\b'), 'Estados Unidos'), (re.compile(r'\bONU\b'), 'O N U'),
    (re.compile(r'\bCap\.'), 'Capítulo'),
]

def corrigir_cid(text: str) -> str:
    if not text: return ''
    return RE_CID.sub(lambda m: CID_MAP.get(int(m.group(1)), ''), text)

def safe_num2words(m) -> str:
    try:
        return num2words(int(m.group()), lang='pt_BR')
    except:
        return m.group()

def formatar_texto(text: str, tipo: str = 'paragrafo') -> str:
    text = corrigir_cid(text)
    text = RE_SPACES.sub(' ', text)
    text = RE_URL.sub('', text)
    text = RE_WWW.sub('', text)
    text = RE_EMAIL.sub('', text)
    text = re.sub(r'(\w+)-\s+([a-záàâãéêíóôõúç])', r'\1\2', text)
    
    for pattern, replacement in ABREVIACOES_COMPILED:
        text = pattern.sub(replacement, text)
        
    text = RE_CHARS.sub(' ', text)
    text = RE_ART.sub(r'\1,', text)
    # text = RE_NUM.sub(safe_num2words, text)
    text = RE_NUM.sub(lambda m: safe_num2words(m) + ',', text)
    text = RE_SPACES.sub(' ', text).strip()
    
    if tipo in ('titulo', 'subtitulo') and text and not text.endswith(('.', ',')):
        text += ','
    return text

def detectar_palavras_coladas(words: list) -> bool:
    if not words: return False
    longas = [w for w in words if len(w['text']) > 15 and ' ' not in w['text']]
    return len(longas) >= 3

def is_sumario(words: list) -> bool:
    if not words: return False
    tokens = [w['text'].strip() for w in words]
    numeros = sum(1 for t in tokens if RE_NUM_ONLY.match(t))
    return numeros >= 5 and (numeros / max(len(tokens), 1)) > 0.08

def extrair_blocos_por_fonte(page) -> list[dict]:
    words = page.extract_words(x_tolerance=5, y_tolerance=3, extra_attrs=['size'])
    if not words: return []

    linhas = []
    linha_atual = []
    top_atual = None
    for w in words:
        top = round(w['top'], 0)
        if top_atual is None or abs(top - top_atual) <= 3:
            linha_atual.append(w)
            top_atual = top
        else:
            if linha_atual: linhas.append(linha_atual)
            linha_atual = [w]
            top_atual = top
    if linha_atual: linhas.append(linha_atual)

    all_sizes = [w.get('size', w['height']) for linha in linhas for w in linha]
    if not all_sizes: return []
    size_counter = Counter([round(s, 1) for s in all_sizes])
    tamanho_normal = size_counter.most_common(1)[0][0]

    page_height = page.height
    blocos = []
    buffer_texto = []
    buffer_tipo = 'paragrafo'

    def flush_buffer():
        if buffer_texto:
            texto = ' '.join(buffer_texto).strip()
            texto = RE_SPACES.sub(' ', texto).strip()
            if len(texto) >= 10:
                blocos.append({'text': texto, 'type': buffer_tipo})

    for linha in linhas:
        texto_linha = ' '.join(corrigir_cid(w['text']) for w in linha).strip()
        if not texto_linha: continue

        size = round(linha[0].get('size', linha[0]['height']), 1)
        top = linha[0]['top']

        if top < page_height * 0.05 and size <= tamanho_normal * 0.85: continue
        if top > page_height * 0.92 and size <= tamanho_normal * 0.85: continue
        if RE_URL.match(texto_linha): continue
        if RE_NUM_ONLY.match(texto_linha.strip()): continue

        if size >= tamanho_normal * 1.8: tipo = 'titulo'
        elif size >= tamanho_normal * 1.3: tipo = 'subtitulo'
        else: tipo = 'paragrafo'

        if tipo != buffer_tipo:
            flush_buffer()
            buffer_texto = [texto_linha]
            buffer_tipo = tipo
        else:
            if tipo in ('titulo', 'subtitulo'):
                flush_buffer()
                buffer_texto = [texto_linha]
                buffer_tipo = tipo
                flush_buffer()
                buffer_texto = []
            else:
                buffer_texto.append(texto_linha)

    flush_buffer()

    resultado = []
    acumulador = ''
    for b in blocos:
        if b['type'] != 'paragrafo':
            if acumulador.strip():
                resultado.append({'text': acumulador.strip(), 'type': 'paragrafo'})
                acumulador = ''
            resultado.append(b)
        else:
            acumulador += ' ' + b['text']
            if len(acumulador) >= 200:
                resultado.append({'text': acumulador.strip(), 'type': 'paragrafo'})
                acumulador = ''
    if acumulador.strip():
        resultado.append({'text': acumulador.strip(), 'type': 'paragrafo'})

    return resultado

def extrair_com_pymupdf(pdf_bytes: bytes) -> list[dict]:
    result = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    for page_num, page in enumerate(doc, start=1):
        texto_raw = page.get_text("text")
        numeros = re.findall(r'(?<!\w)\d+(?!\w)', texto_raw)
        palavras = texto_raw.split()
        if len(numeros) >= 5 and len(numeros) / max(len(palavras), 1) > 0.08:
            continue
            
        blocos = page.get_text("blocks")
        page_height = page.rect.height

        for bloco in blocos:
            x0, y0, x1, y1, texto, block_no, block_type = bloco
            if block_type != 0: continue
            if not texto.strip(): continue
            if y0 < page_height * 0.05 or y1 > page_height * 0.95: continue

            texto = re.sub(r'-\n', '', texto)
            texto = re.sub(r'\n', ' ', texto)
            texto = RE_URL.sub('', texto)
            texto = RE_SPACES.sub(' ', texto).strip()
            text = re.sub(r'(\w+)-\s+([a-záàâãéêíóôõúç])', r'\1\2', text)

            if len(texto) < 10: continue
            if RE_NUM_ONLY.match(texto): continue

            altura_bloco = y1 - y0
            chars = len(texto)
            if chars < 100 and altura_bloco > 20: tipo = 'titulo'
            elif chars < 200 and not texto.endswith('.'): tipo = 'subtitulo'
            else: tipo = 'paragrafo'

            result.append({'text': texto, 'type': tipo, 'page': page_num})

    final = []
    acumulador = ''
    ultima_pagina = 1
    
    for b in result:
        ultima_pagina = b['page']
        if b['type'] != 'paragrafo':
            if acumulador.strip():
                final.append({'text': acumulador.strip(), 'type': 'paragrafo', 'page': ultima_pagina})
                acumulador = ''
            final.append(b)
        else:
            acumulador += ' ' + b['text']
            if len(acumulador) >= 300:
                final.append({'text': acumulador.strip(), 'type': 'paragrafo', 'page': b['page']})
                acumulador = ''
                
    if acumulador.strip():
        final.append({'text': acumulador.strip(), 'type': 'paragrafo', 'page': ultima_pagina})

    return final

# Isolando o processamento síncrono da CPU
def processar_pdf_sincrono(content: bytes) -> list[dict]:
    result = []
    usar_pymupdf = False

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:5]:
            words = page.extract_words(x_tolerance=5, y_tolerance=3, extra_attrs=['size'])
            if detectar_palavras_coladas(words):
                usar_pymupdf = True
                break

        if not usar_pymupdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(x_tolerance=5, y_tolerance=3, extra_attrs=['size'])
                if is_sumario(words): continue
                blocos = extrair_blocos_por_fonte(page)
                for b in blocos:
                    if len(b['text'].strip()) >= 10:
                        result.append({**b, 'page': page_num})

    if usar_pymupdf:
        print('Encoding quebrado — usando pymupdf')
        result = extrair_com_pymupdf(content)

    return result

@app.get("/")
def index():
    return FileResponse("index.html")

@app.get("/google2e2619d14a369d8a.html")
def google_verify():
    return FileResponse("google2e2619d14a369d8a.html")

@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    try:
        content = await file.read()
        # Rodando o processamento pesado fora do evento principal do FastAPI
        result = await run_in_threadpool(processar_pdf_sincrono, content)
        return JSONResponse({"paragraphs": result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar o arquivo: {str(e)}")

class TTSRequest(BaseModel):
    paragraphs: list[str]
    types: list[str] = []
    start_index: int = 0
    voice: str = "pt-BR-ThalitaMultilingualNeural"
    rate: str = "+0%"

async def stream_from_index(paragraphs, types, start, voice, rate):
    try:
        for i in range(start, len(paragraphs)):
            tipo = types[i] if i < len(types) else 'paragrafo'
            texto = formatar_texto(paragraphs[i], tipo)
            if not texto: continue
            
            communicate = edge_tts.Communicate(texto, voice=voice, rate=rate)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
    except asyncio.CancelledError:
        # Quando o frontend usa o AbortController e mata a requisição,
        # o FastAPI lança essa exceção. A gente captura e sai fora para parar de gastar recurso à toa.
        return
    except Exception as e:
        print(f"Erro no streaming TTS: {str(e)}")

@app.post("/tts")
async def tts(body: TTSRequest):
    return StreamingResponse(
        stream_from_index(body.paragraphs, body.types, body.start_index, body.voice, body.rate),
        media_type="audio/mpeg"
    )