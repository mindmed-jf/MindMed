"""
Servidor FastAPI - MindMed v3.0
Recebe webhooks do WhatsApp via Z-API e processa com o agente IA.

Endpoints:
    POST /webhook/zapi   → Recebe mensagens da Z-API
    GET  /health         → Health check
    POST /teste/mensagem → Teste manual sem WhatsApp
"""

import os
import json
import httpx
import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from agente_mindmed import GestorConversasMindMed
from followup_scheduler import iniciar_scheduler

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

# Z-API
ZAPI_INSTANCE_ID  = os.getenv("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN        = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")  # Security token do webhook
ZAPI_BASE_URL     = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}"


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
# WEBHOOK — Z-API
# ============================================================================

@app.post("/webhook/zapi")
async def receber_zapi(request: Request, background_tasks: BackgroundTasks):
    """
    Recebe mensagens de WhatsApp via Z-API.

    Z-API envia JSON com o seguinte formato:
    {
      "phone": "5511999999999",
      "isGroupMsg": false,
      "isStatusReply": false,
      "text": { "message": "texto da mensagem" },
      "type": "ReceivedCallback"
    }
    """
    # Verificação do security token (opcional mas recomendado)
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
    if body.get("isGroupMsg", False):
        return JSONResponse({"status": "ignored", "reason": "group_message"})

    # Ignora mensagens de newsletter/broadcast
    telefone_raw = body.get("phone", "")
    if "@newsletter" in telefone_raw or "@broadcast" in telefone_raw:
        return JSONResponse({"status": "ignored", "reason": "newsletter"})

    # Ignora status updates e confirmações de entrega
    tipo = body.get("type", "")
    if tipo not in ("ReceivedCallback",):
        return JSONResponse({"status": "ignored", "reason": f"type_{tipo}"})

    # Ignora mensagens do próprio bot (enviadas por nós)
    if body.get("fromMe", False):
        return JSONResponse({"status": "ignored", "reason": "own_message"})

    # Extrai telefone e texto
    telefone = body.get("phone", "").replace("+", "").replace(" ", "")
    
    # Texto pode vir em diferentes formatos dependendo do tipo de mensagem
    texto = ""
    if body.get("text"):
        texto = body["text"].get("message", "").strip()
    elif body.get("caption"):
        texto = body.get("caption", "").strip()

    if not telefone or not texto:
        return JSONResponse({"status": "ignored", "reason": "no_text_or_phone"})

    log.info(f"📨 Z-API | {telefone}: {texto[:60]}")

    background_tasks.add_task(
        processar_e_responder_zapi,
        telefone=telefone,
        mensagem=texto
    )

    return JSONResponse({"status": "received"})


async def processar_e_responder_zapi(telefone: str, mensagem: str):
    """Processa a mensagem com o agente e envia resposta via Z-API."""
    try:
        resultado = gestor.processar_mensagem(telefone=telefone, mensagem=mensagem)

        if resultado.get("deve_enviar") and resultado.get("resposta"):
            await enviar_mensagem_zapi(
                telefone=telefone,
                texto=resultado["resposta"]
            )
    except Exception as e:
        log.error(f"❌ Erro ao processar/responder Z-API para {telefone}: {e}")


def dividir_mensagem(texto: str) -> list:
    """Divide mensagens longas em partes para parecer mais humano."""
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


def calcular_delay(texto: str) -> float:
    """Calcula delay proporcional ao tamanho do texto. Min 1.5s, max 5s."""
    palavras = len(texto.split())
    return min(max(1.5 + (palavras * 0.08), 1.5), 5.0)


async def enviar_mensagem_zapi(telefone: str, texto: str):
    """Envia mensagem via Z-API com delay humanizado e quebra em partes."""
    if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
        log.warning("Credenciais Z-API não configuradas. Mensagem não enviada.")
        log.info(f"[SIMULADO] Para {telefone}: {texto}")
        return

    url = f"{ZAPI_BASE_URL}/send-text"
    partes = dividir_mensagem(texto)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for i, parte in enumerate(partes):
                delay = calcular_delay(parte)
                log.info(f"⏳ Aguardando {delay:.1f}s (parte {i+1}/{len(partes)})")
                await asyncio.sleep(delay)

                payload = {
                    "phone": telefone,
                    "message": parte
                }
                headers = {
                    "Content-Type": "application/json",
                    "client-token": ZAPI_CLIENT_TOKEN
                }

                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                log.info(f"✅ Parte {i+1}/{len(partes)} enviada para {telefone}")

    except Exception as e:
        log.error(f"❌ Erro ao enviar via Z-API para {telefone}: {e}")


# ============================================================================
# ENDPOINT DE RETOMAR AGENTE (usado pelo painel)
# ============================================================================

@app.post("/retomar/{telefone}")
async def retomar_agente(telefone: str):
    """
    Retoma o agente para uma conversa que estava em pausa (PASSAR_HUMANO).
    Chamado pelo painel quando Davi termina o atendimento manual.
    """
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
    """Testa o agente via HTTP sem precisar do WhatsApp."""
    log.info(f"🧪 Teste manual | {body.telefone}: {body.mensagem}")
    resultado = gestor.processar_mensagem(
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