"""
Sistema de Follow-up Automático — MindMed v3.0
================================================

Roda em background e envia mensagens automáticas para alunos
que pararam de responder após receberem o acesso trial.

Lógica de timing:
    Follow-up 1 → 24h sem resposta após ACESSO_LIBERADO / CADASTRO_ENVIADO
    Follow-up 2 → 48h sem resposta após ACESSO_LIBERADO / CADASTRO_ENVIADO
    Follow-up 3 → 72h sem resposta → marca como FINALIZADO_INATIVO

Como usar:
    Opção A (recomendado) — integrado ao main_fastapi.py (já configurado)
    Opção B — rodar separado: python followup_scheduler.py
"""

import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(override=True)

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

def horario_permitido() -> bool:
    """Verifica se está dentro do horário permitido para envio (07:00 - 21:00 horário de Brasília)."""
    from datetime import timezone, timedelta
    brasilia = timezone(timedelta(hours=-3))
    agora = datetime.now(brasilia)
    return 7 <= agora.hour < 21

log = logging.getLogger(__name__)

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

ZAPI_INSTANCE_ID  = os.getenv("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN        = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")

# Intervalo de verificação (em segundos) — verifica a cada 30 minutos
INTERVALO_VERIFICACAO = int(os.getenv("FOLLOWUP_INTERVALO_SEGUNDOS", "1800"))

# Timings dos follow-ups (em horas)
FOLLOWUP_1_HORAS = int(os.getenv("FOLLOWUP_1_HORAS", "24"))
FOLLOWUP_2_HORAS = int(os.getenv("FOLLOWUP_2_HORAS", "48"))
FOLLOWUP_3_HORAS = int(os.getenv("FOLLOWUP_3_HORAS", "72"))

# Mensagens de follow-up (personalize aqui)
def mensagem_followup_1(nome: str) -> str:
    nome_str = nome if nome else "tudo bem"
    return f"E aí, {nome_str}? 👋\nConseguiu explorar a plataforma? Ficou com alguma dúvida?"

def mensagem_followup_2(nome: str) -> str:
    nome_str = nome if nome else ""
    prefixo = f"{nome_str}, " if nome_str else ""
    return f"{prefixo}seu acesso de 48h tá acabando em breve! ⏰\nVocê conseguiu testar? O que achou?\nQualquer dúvida, tô aqui!"

def mensagem_followup_3(nome: str) -> str:
    nome_str = nome if nome else ""
    prefixo = f"{nome_str}, " if nome_str else ""
    return f"{prefixo}tudo bem? 💪\nSe você tiver interesse em continuar com a MindMed, é só chamar. Tô por aqui!"


# ============================================================================
# MENSAGENS DE REENGAJAMENTO (leads que sumiram sem testar)
# ============================================================================

# Timings do reengajamento (em horas) para status CONTINUAR
REENGAJ_1_HORAS = int(os.getenv("REENGAJ_1_HORAS", "48"))   # 2 dias
REENGAJ_2_HORAS = int(os.getenv("REENGAJ_2_HORAS", "96"))   # 4 dias
REENGAJ_3_HORAS = int(os.getenv("REENGAJ_3_HORAS", "144"))  # 6 dias

def mensagem_reengaj_1(nome: str) -> str:
    nome_str = nome if nome else ""
    prefixo = f"Oi {nome_str}! " if nome_str else "Oi! "
    return f"{prefixo}Ficou alguma dúvida sobre a MindMed? Pode perguntar à vontade 😊"

def mensagem_reengaj_2(nome: str) -> str:
    nome_str = nome if nome else ""
    prefixo = f"{nome_str}, " if nome_str else ""
    return f"{prefixo}que tal testar a plataforma por 48h de graça? Sem precisar de cartão 😄\nSó me avisa que libero seu acesso!"

def mensagem_reengaj_3(nome: str) -> str:
    nome_str = nome if nome else ""
    prefixo = f"{nome_str}, " if nome_str else ""
    return f"{prefixo}tudo bem? 🤙\nSe um dia quiser conhecer a MindMed, é só chamar. Boa sorte nos estudos!"


# ============================================================================
# ENVIO DE MENSAGEM VIA TWILIO
# ============================================================================

async def enviar_whatsapp(telefone: str, texto: str) -> bool:
    """Envia mensagem WhatsApp via Z-API. Retorna True se enviou com sucesso."""
    if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
        log.warning(f"[SIMULADO] Follow-up para {telefone}: {texto}")
        return True

    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "phone": telefone,
        "message": texto
    }
    headers = {
        "Content-Type": "application/json",
        "client-token": ZAPI_CLIENT_TOKEN
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            log.info(f"✅ Follow-up enviado para {telefone}")
            return True
    except Exception as e:
        log.error(f"❌ Erro ao enviar follow-up para {telefone}: {e}")
        return False


# ============================================================================
# LÓGICA DE FOLLOW-UP
# ============================================================================

def calcular_horas_sem_resposta(updated_at_str: str) -> float:
    """Calcula quantas horas se passaram desde a última atualização da conversa."""
    try:
        # Supabase retorna timestamps em UTC com formato ISO
        if updated_at_str.endswith("Z"):
            updated_at_str = updated_at_str[:-1] + "+00:00"

        updated_at = datetime.fromisoformat(updated_at_str)

        # Garante que updated_at tem timezone
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        agora = datetime.now(timezone.utc)
        delta = agora - updated_at
        return delta.total_seconds() / 3600
    except Exception as e:
        log.error(f"Erro ao calcular horas: {e}")
        return 0


async def processar_followups():
    """
    Verifica o Supabase e envia follow-ups para conversas paradas.
    
    Busca conversas com status ACESSO_LIBERADO, CADASTRO_ENVIADO ou AGUARDAR_FOLLOW_UP
    que não tiveram atualização nos últimos X horas.
    """
    if not horario_permitido():
        log.info("🌙 Fora do horário permitido (07:00-21:00). Follow-ups pausados.")
        return

    log.info("🔍 Verificando follow-ups pendentes...")

    try:
        # Busca conversas elegíveis para follow-up
        resultado = (
            supabase.table("conversas")
            .select("*")
            .in_("status_conversa", ["ACESSO_LIBERADO", "AGUARDAR_FOLLOW_UP", "CADASTRO_ENVIADO"])
            .execute()
        )

        conversas = resultado.data or []
        log.info(f"📋 {len(conversas)} conversa(s) elegível(eis) para follow-up")

        for conversa in conversas:
            telefone    = conversa.get("telefone", "")
            nome        = conversa.get("nome_aluno", "")
            updated_at  = conversa.get("updated_at", "")
            followup_n  = conversa.get("contador_followups", 0) or 0

            if not telefone or not updated_at:
                continue

            horas = calcular_horas_sem_resposta(updated_at)

            # Determina qual follow-up enviar
            if followup_n == 0 and horas >= FOLLOWUP_1_HORAS:
                await _enviar_followup(conversa, 1, mensagem_followup_1(nome))

            elif followup_n == 1 and horas >= FOLLOWUP_2_HORAS:
                await _enviar_followup(conversa, 2, mensagem_followup_2(nome))

            elif followup_n == 2 and horas >= FOLLOWUP_3_HORAS:
                await _enviar_followup(conversa, 3, mensagem_followup_3(nome))
                await _finalizar_inativo(telefone, nome)

            else:
                log.debug(f"⏳ {telefone} | Follow-up {followup_n+1} | {horas:.1f}h sem resposta")

    except Exception as e:
        log.error(f"❌ Erro ao processar follow-ups: {e}")


async def _enviar_followup(conversa: dict, numero: int, mensagem: str):
    """Envia um follow-up e atualiza o contador no Supabase."""
    telefone = conversa.get("telefone")
    nome     = conversa.get("nome_aluno", "")

    log.info(f"📤 Follow-up {numero} → {telefone} ({nome})")

    enviado = await enviar_whatsapp(telefone, mensagem)

    if enviado:
        # Atualiza histórico e contador
        historico = conversa.get("historico", []) or []
        historico.append({
            "role": "assistant",
            "content": mensagem,
            "followup": numero,
            "enviado_em": datetime.now(timezone.utc).isoformat()
        })

        supabase.table("conversas").update({
            "contador_followups": numero,
            "status_conversa": "AGUARDAR_FOLLOW_UP",
            "historico": historico,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()

        log.info(f"✅ Follow-up {numero} registrado para {telefone}")


async def _finalizar_inativo(telefone: str, nome: str):
    """Marca conversa como FINALIZADO_INATIVO após 3 follow-ups sem resposta."""
    log.info(f"🏁 Finalizando conversa inativa: {telefone}")

    supabase.table("conversas").update({
        "status_conversa": "FINALIZADO_INATIVO",
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("telefone", telefone).execute()

    # Também atualiza na tabela de leads
    try:
        supabase.table("leads").update({
            "status_conversa": "FINALIZADO_INATIVO",
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()
    except Exception:
        pass

    log.info(f"✅ Conversa {telefone} finalizada como INATIVO")


# ============================================================================
# LÓGICA DE REENGAJAMENTO (leads CONTINUAR que sumiram sem testar)
# ============================================================================

async def processar_reengajamento():
    """
    Verifica leads que estão com status CONTINUAR há muito tempo sem responder.
    Envia mensagens de reengajamento para tentar recuperá-los.
    """
    if not horario_permitido():
        log.info("🌙 Fora do horário permitido (07:00-21:00). Reengajamentos pausados.")
        return

    log.info("🔍 Verificando reengajamentos pendentes...")

    try:
        resultado = (
            supabase.table("conversas")
            .select("*")
            .eq("status_conversa", "CONTINUAR")
            .execute()
        )

        conversas = resultado.data or []
        elegiveis = 0

        for conversa in conversas:
            telefone   = conversa.get("telefone", "")
            nome       = conversa.get("nome_aluno", "")
            updated_at = conversa.get("updated_at", "")
            reengaj_n  = conversa.get("contador_reengajamento", 0) or 0

            if not telefone or not updated_at:
                continue

            horas = calcular_horas_sem_resposta(updated_at)

            # Só age se ficou parado há pelo menos 48h
            if horas < REENGAJ_1_HORAS:
                continue

            elegiveis += 1

            if reengaj_n == 0 and horas >= REENGAJ_1_HORAS:
                await _enviar_reengajamento(conversa, 1, mensagem_reengaj_1(nome))

            elif reengaj_n == 1 and horas >= REENGAJ_2_HORAS:
                await _enviar_reengajamento(conversa, 2, mensagem_reengaj_2(nome))

            elif reengaj_n == 2 and horas >= REENGAJ_3_HORAS:
                await _enviar_reengajamento(conversa, 3, mensagem_reengaj_3(nome))
                await _finalizar_inativo(telefone, nome)

        log.info(f"📋 {elegiveis} lead(s) elegível(eis) para reengajamento")

    except Exception as e:
        log.error(f"❌ Erro ao processar reengajamento: {e}")


async def _enviar_reengajamento(conversa: dict, numero: int, mensagem: str):
    """Envia mensagem de reengajamento e atualiza contador no Supabase."""
    telefone = conversa.get("telefone")
    nome     = conversa.get("nome_aluno", "")

    log.info(f"📤 Reengajamento {numero} → {telefone} ({nome})")

    enviado = await enviar_whatsapp(telefone, mensagem)

    if enviado:
        historico = conversa.get("historico", []) or []
        historico.append({
            "role": "assistant",
            "content": mensagem,
            "reengajamento": numero,
            "enviado_em": datetime.now(timezone.utc).isoformat()
        })

        supabase.table("conversas").update({
            "contador_reengajamento": numero,
            "historico": historico,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()

        log.info(f"✅ Reengajamento {numero} registrado para {telefone}")


# ============================================================================
# LOOP PRINCIPAL
# ============================================================================

async def iniciar_scheduler():
    """
    Loop principal do scheduler de follow-ups.
    Roda indefinidamente verificando a cada INTERVALO_VERIFICACAO segundos.
    """
    log.info(f"🚀 Follow-up Scheduler iniciado")
    log.info(f"   Intervalo de verificação: {INTERVALO_VERIFICACAO}s ({INTERVALO_VERIFICACAO//60} min)")
    log.info(f"   Follow-up 1: {FOLLOWUP_1_HORAS}h | 2: {FOLLOWUP_2_HORAS}h | 3: {FOLLOWUP_3_HORAS}h")

    while True:
        try:
            await processar_followups()
        except Exception as e:
            log.error(f"❌ Erro no loop do scheduler: {e}")

        try:
            await processar_reengajamento()
        except Exception as e:
            log.error(f"❌ Erro no loop de reengajamento: {e}")

        await asyncio.sleep(INTERVALO_VERIFICACAO)


# ============================================================================
# PONTO DE ENTRADA (rodar separado se necessário)
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    asyncio.run(iniciar_scheduler())
