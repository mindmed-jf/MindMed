"""
Servidor FastAPI - MindMed
Recebe webhooks do WhatsApp via Z-API e processa com o agente IA.
"""

import os
import json
import httpx
import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from agente_mindmed import GestorConversasMindMed
from followup_scheduler import iniciar_scheduler
from supabase import create_client, Client as SupabaseClient

load_dotenv(override=True)

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

app = FastAPI(title="MindMed API", version="3.0")
gestor = GestorConversasMindMed()
_scheduler_task = None

# Supabase — usado para deduplicação distribuída
_supabase_dedup: SupabaseClient = create_client(
    os.getenv("SUPABASE_URL", ""),
    os.getenv("SUPABASE_KEY", "")
)

app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/painel")
async def painel():
    return FileResponse("painel_mindmed.html")

# Z-API
ZAPI_INSTANCE_ID  = os.getenv("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN        = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")
ZAPI_BASE_URL     = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}"

# Cache de deduplicação — via Supabase (compartilhado entre todos os workers)
DEDUP_TTL_SEGUNDOS = 30


@app.on_event("startup")
async def startup_event():
    global _scheduler_task
    _scheduler_task = asyncio.create_task(iniciar_scheduler())
    log.info("✅ Follow-up Scheduler iniciado junto com o servidor")

@app.on_event("shutdown")
async def shutdown_event():
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        log.info("🛑 Follow-up Scheduler encerrado")


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "agente": "MindMed v3.0"}


# ============================================================================
# DEDUPLICAÇÃO DE WEBHOOK — via Supabase (funciona com múltiplos workers)
# ============================================================================

def ja_processado(telefone: str, texto: str) -> bool:
    """
    Verifica e registra se essa mensagem já foi processada.
    Usa o Supabase como store compartilhado — funciona mesmo com múltiplos
    workers/instâncias no Railway (cache em memória não seria suficiente).
    """
    chave = hashlib.md5(f"{telefone}:{texto}".encode()).hexdigest()
    agora = datetime.now(timezone.utc)
    expira_em = agora + timedelta(seconds=DEDUP_TTL_SEGUNDOS)

    try:
        # Tenta inserir a chave — se já existir, lança erro de unique constraint
        _supabase_dedup.table("webhook_dedup").insert({
            "chave": chave,
            "expira_em": expira_em.isoformat()
        }).execute()

        # Limpeza assíncrona de entradas expiradas (best-effort, não bloqueia)
        try:
            _supabase_dedup.table("webhook_dedup").delete().lt(
                "expira_em", agora.isoformat()
            ).execute()
        except Exception:
            pass

        return False  # não existia → pode processar

    except Exception as e:
        erro = str(e).lower()
        if "duplicate" in erro or "unique" in erro or "23505" in erro:
            return True  # já existia → duplicata
        # Erro inesperado no banco — permite processar (melhor duplicata que silêncio)
        log.warning(f"⚠️ Erro na deduplicação Supabase: {e} — permitindo processamento")
        return False


# ============================================================================
# DETECÇÃO DE MENSAGEM DE GRUPO
# ============================================================================

def eh_mensagem_de_grupo(body: dict) -> bool:
    """
    Detecta mensagens de grupo de forma robusta.
    A Z-API pode indicar grupos de diferentes formas dependendo da versão.
    """
    if body.get("isGroupMsg") is True:
        return True
    telefone_raw = body.get("phone", "")
    if "@g.us" in telefone_raw:
        return True
    if "@g.us" in body.get("chatId", ""):
        return True
    if "@g.us" in body.get("from", ""):
        return True
    if body.get("groupId"):
        return True
    if body.get("participantPhone"):
        return True
    return False


# ============================================================================
# WEBHOOK — Z-API
# ============================================================================

@app.post("/webhook/zapi")
async def receber_zapi(request: Request, background_tasks: BackgroundTasks):
    """
    Recebe mensagens de WhatsApp via Z-API.
    """
    client_token = request.headers.get("client-token", "")
    if ZAPI_CLIENT_TOKEN and client_token and client_token != ZAPI_CLIENT_TOKEN:
        log.warning("⚠️ Token de segurança inválido recebido no webhook")
        raise HTTPException(status_code=403, detail="Token inválido")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body inválido")

    log.debug(f"Webhook Z-API recebido: {json.dumps(body)[:200]}")

    # Ignora mensagens de grupo
    if eh_mensagem_de_grupo(body):
        telefone_raw = body.get("phone", "")
        log.info(f"🚫 Mensagem de grupo ignorada | {telefone_raw[:30]}")
        return JSONResponse({"status": "ignored", "reason": "group_message"})

    # Ignora newsletter/broadcast
    telefone_raw = body.get("phone", "")
    if "@newsletter" in telefone_raw or "@broadcast" in telefone_raw:
        return JSONResponse({"status": "ignored", "reason": "newsletter"})

    # Ignora tipos que não são mensagens recebidas
    tipo = body.get("type", "")
    if tipo not in ("ReceivedCallback",):
        return JSONResponse({"status": "ignored", "reason": f"type_{tipo}"})

    # Ignora mensagens do próprio bot
    if body.get("fromMe", False):
        return JSONResponse({"status": "ignored", "reason": "own_message"})

    # Extrai e normaliza telefone — remove sufixos que a Z-API pode incluir
    # ex: "5511999999999@c.us" → "5511999999999"
    telefone = (
        body.get("phone", "")
        .split("@")[0]          # remove @c.us ou qualquer sufixo
        .replace("+", "")
        .replace(" ", "")
        .strip()
    )

    texto = ""
    if body.get("text"):
        texto = body["text"].get("message", "").strip()
    elif body.get("caption"):
        texto = body.get("caption", "").strip()

    if not telefone or not texto:
        return JSONResponse({"status": "ignored", "reason": "no_text_or_phone"})

    # Deduplicação
    if ja_processado(telefone, texto):
        log.warning(f"⚠️ Mensagem duplicada ignorada | {telefone}: {texto[:40]}")
        return JSONResponse({"status": "ignored", "reason": "duplicate"})

    log.info(f"📨 Z-API | {telefone}: {texto[:60]}")

    background_tasks.add_task(
        processar_e_responder_zapi,
        telefone=telefone,
        mensagem=texto
    )

    return JSONResponse({"status": "received"})


# ============================================================================
# CORREÇÃO #2 — Bloqueio síncrono no servidor assíncrono
#
# ANTES: gestor.processar_mensagem() era chamado diretamente dentro de uma
# coroutine async, bloqueando o event loop inteiro do FastAPI por 3-8 segundos
# enquanto o agente aguardava resposta da OpenAI e do Supabase. Com múltiplos
# usuários simultâneos, o servidor travava e mensagens ficavam na fila.
#
# AGORA: asyncio.to_thread() executa a função síncrona em uma thread do pool
# do sistema operacional, liberando o event loop para continuar atendendo
# outras requisições enquanto o agente processa em paralelo.
# ============================================================================
async def processar_e_responder_zapi(telefone: str, mensagem: str):
    """Processa a mensagem com o agente e envia resposta via Z-API."""
    try:
        # gestor.processar_mensagem é síncrono (OpenAI + Supabase bloqueantes).
        # asyncio.to_thread roda em thread separada sem bloquear o event loop.
        resultado = await asyncio.to_thread(
            gestor.processar_mensagem,
            telefone=telefone,
            mensagem=mensagem
        )

        if resultado.get("deve_enviar") and resultado.get("resposta"):
            await enviar_mensagem_zapi(
                telefone=telefone,
                texto=resultado["resposta"]
            )
    except Exception as e:
        log.error(f"❌ Erro ao processar/responder Z-API para {telefone}: {e}")


def dividir_mensagem(texto: str) -> list:
    """Divide mensagens longas em partes para parecer mais humano."""
    texto = texto.replace("\\n\\n", "\n\n").replace("\\n", "\n")

    if len(texto) <= 120:
        return [texto.strip()]
    partes = [p.strip() for p in texto.split("\n\n") if p.strip()]
    resultado = []
    for parte in partes:
        if len(parte) > 300:
            sub = [s.strip() for s in parte.split("\n") if s.strip()]
            resultado.extend(sub)
        else:
            resultado.append(parte)
    return resultado if resultado else [texto.strip()]


def calcular_typing(texto: str) -> int:
    palavras = len(texto.split())
    segundos = round(palavras * 0.25)
    return min(max(segundos, 2), 10)


async def enviar_mensagem_zapi(telefone: str, texto: str):
    """Envia mensagem via Z-API com typing humanizado e quebra em partes."""
    if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
        log.warning("Credenciais Z-API não configuradas. Mensagem não enviada.")
        log.info(f"[SIMULADO] Para {telefone}: {texto}")
        return

    url = f"{ZAPI_BASE_URL}/send-text"
    partes = dividir_mensagem(texto)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for i, parte in enumerate(partes):
                typing = calcular_typing(parte)

                payload = {
                    "phone": telefone,
                    "message": parte,
                    "delayMessage": 1,
                    "delayTyping": typing
                }
                headers = {
                    "Content-Type": "application/json",
                    "client-token": ZAPI_CLIENT_TOKEN
                }

                log.info(f"⌨️ Digitando {typing}s → parte {i+1}/{len(partes)}")
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                log.info(f"✅ Parte {i+1}/{len(partes)} enviada para {telefone}")

    except Exception as e:
        log.error(f"❌ Erro ao enviar via Z-API para {telefone}: {e}")


# ============================================================================
# ENDPOINT DE RETOMAR AGENTE
# ============================================================================

@app.post("/retomar/{telefone}")
async def retomar_agente(telefone: str):
    from supabase import create_client
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    try:
        supabase.table("conversas").update({
            "status_conversa": "CONTINUAR",
            "updated_at": datetime.now().isoformat()
        }).eq("telefone", telefone).execute()

        supabase.table("leads").update({
            "status_conversa": "CONTINUAR",
        }).eq("telefone", telefone).execute()

        log.info(f"✅ Agente retomado para {telefone}")
        return JSONResponse({"sucesso": True, "telefone": telefone})
    except Exception as e:
        log.error(f"❌ Erro ao retomar agente para {telefone}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ENDPOINT DE TESTE MANUAL
# ============================================================================

class MensagemTeste(BaseModel):
    telefone: str
    mensagem: str


@app.post("/teste/mensagem")
async def testar_mensagem(body: MensagemTeste):
    log.info(f"🧪 Teste manual | {body.telefone}: {body.mensagem}")
    # Mesmo endpoint de teste também usa to_thread para consistência
    resultado = await asyncio.to_thread(
        gestor.processar_mensagem,
        telefone=body.telefone,
        mensagem=body.mensagem
    )
    return JSONResponse(resultado)


# ============================================================================
# PONTO DE ENTRADA
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8000, reload=True)
