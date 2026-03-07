"""
Sistema de Follow-up Automático — MindMed
================================================

Timings:
    Follow-up 0 → 1h  após ACESSO_LIBERADO  — confirma acesso, pergunta se explorou
    Follow-up 1 → 24h sem resposta          — pergunta geral sobre o teste
    Follow-up 2 → 48h sem resposta          — urgência leve, acesso acabando
    Follow-up 3 → 72h sem resposta          — encerramento, marca FINALIZADO_INATIVO

Reengajamento (status CONTINUAR, lead sumiu antes de testar):
    Reengaj 1 → 48h
    Reengaj 2 → 96h
    Reengaj 3 → 144h → marca FINALIZADO_INATIVO
"""

import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client

# ============================================================================
# CORREÇÃO #7 — Fuso horário com zoneinfo em vez de offset fixo UTC-3
#
# ANTES: timezone(timedelta(hours=-3)) é um offset fixo que não representa
# corretamente o fuso de Brasília — depende da configuração do servidor e
# não considera variações regionais.
#
# AGORA: zoneinfo.ZoneInfo("America/Sao_Paulo") é o identificador oficial
# do fuso de Brasília, robusto e independente da configuração do servidor.
# Fallback para pytz caso o ambiente não tenha zoneinfo (Python < 3.9).
# ============================================================================
try:
    from zoneinfo import ZoneInfo
    FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
    print("✅ Fuso horário: zoneinfo.ZoneInfo('America/Sao_Paulo')")
except ImportError:
    try:
        import pytz
        FUSO_BRASILIA = pytz.timezone("America/Sao_Paulo")
        print("✅ Fuso horário: pytz ('America/Sao_Paulo') — fallback")
    except ImportError:
        # Último recurso: offset fixo UTC-3 (comportamento original)
        FUSO_BRASILIA = timezone(timedelta(hours=-3))
        print("⚠️ Fuso horário: offset fixo UTC-3 — instale zoneinfo ou pytz para maior precisão")

load_dotenv(override=True)

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

def horario_permitido() -> bool:
    """
    Retorna True se o horário atual em Brasília estiver entre 07h e 21h.
    Usa zoneinfo.ZoneInfo('America/Sao_Paulo') para conversão precisa,
    independente do fuso configurado no servidor.
    """
    agora = datetime.now(FUSO_BRASILIA)
    return 7 <= agora.hour < 21

log = logging.getLogger(__name__)

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

ZAPI_INSTANCE_ID  = os.getenv("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN        = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")

# Intervalo de verificação — a cada 15 min (necessário para pegar o follow-up de 1h)
INTERVALO_VERIFICACAO = int(os.getenv("FOLLOWUP_INTERVALO_SEGUNDOS", "900"))

# Timings dos follow-ups (em horas)
FOLLOWUP_0_HORAS = float(os.getenv("FOLLOWUP_0_HORAS", "1"))
FOLLOWUP_1_HORAS = int(os.getenv("FOLLOWUP_1_HORAS", "24"))
FOLLOWUP_2_HORAS = int(os.getenv("FOLLOWUP_2_HORAS", "48"))
FOLLOWUP_3_HORAS = int(os.getenv("FOLLOWUP_3_HORAS", "72"))

# Timings do reengajamento (em horas) para status CONTINUAR
REENGAJ_1_HORAS = int(os.getenv("REENGAJ_1_HORAS", "48"))
REENGAJ_2_HORAS = int(os.getenv("REENGAJ_2_HORAS", "96"))
REENGAJ_3_HORAS = int(os.getenv("REENGAJ_3_HORAS", "144"))

# ============================================================================
# CORREÇÃO #8 — Número máximo de tentativas para envio Z-API
#
# ANTES: sem retry. Se o envio falhasse, o contador não era atualizado
# (correto), mas não havia nenhuma tentativa adicional na mesma rodada.
# Na próxima rodada (15 min depois) o scheduler tentaria de novo, o que
# funcionava acidentalmente, mas sem log claro e sem controle.
#
# AGORA: até MAX_TENTATIVAS_ZAPI tentativas com espera exponencial entre
# elas (1s, 2s). Se todas falharem, o contador não é atualizado e o
# scheduler tenta novamente na próxima rodada normalmente.
# ============================================================================
MAX_TENTATIVAS_ZAPI = int(os.getenv("MAX_TENTATIVAS_ZAPI", "3"))
ESPERA_RETRY_ZAPI   = float(os.getenv("ESPERA_RETRY_ZAPI_SEGUNDOS", "1.0"))


# ============================================================================
# MENSAGENS DE FOLLOW-UP
# ============================================================================

def mensagem_followup_0(nome: str) -> str:
    """1h após liberar acesso — lead ainda está quente, confirma se conseguiu entrar."""
    n = nome if nome else "tudo bem"
    return (
        f"E aí, {n}! Já deu pra dar uma olhada na plataforma? 👀\n\n"
        f"Queria saber se você conseguiu acessar, ver algum deck ou testar os flashcards. "
        f"Se tiver qualquer dúvida pra começar, é só me avisar que te ajudo agora!"
    )

def mensagem_followup_1(nome: str) -> str:
    """24h — pergunta geral sobre o teste."""
    n = nome if nome else "tudo bem"
    return f"E aí, {n}? 👋\nConseguiu explorar a plataforma? Ficou com alguma dúvida?"

def mensagem_followup_2(nome: str) -> str:
    # CORREÇÃO #10 — antes dizia "tá acabando em breve", mas o acesso de 48h
    # já expirou nesse ponto. Mensagem ajustada para refletir a realidade
    # e ainda abrir porta para continuar com um plano pago.
    """48h — acesso expirado, pergunta o que achou e abre para continuar."""
    prefixo = f"{nome}, " if nome else ""
    return (
        f"{prefixo}seu acesso de teste expirou! ⏰\n"
        f"Você conseguiu explorar a plataforma? O que achou dos flashcards?\n"
        f"Se quiser continuar, é só me falar que a gente vê o melhor plano pra você!"
    )

def mensagem_followup_3(nome: str) -> str:
    # CORREÇÃO #10 — encerramento sem mencionar trial (já expirou há 24h).
    # Mensagem leve e sem pressão, deixa a porta aberta.
    """72h — encerramento leve, sem mencionar trial."""
    prefixo = f"{nome}, " if nome else ""
    return (
        f"{prefixo}tudo bem? 🤙\n"
        f"Se um dia quiser conhecer a MindMed melhor, é só chamar. Boa sorte nos estudos! 💪"
    )


# ============================================================================
# MENSAGENS DE REENGAJAMENTO
# ============================================================================

def mensagem_reengaj_1(nome: str) -> str:
    prefixo = f"Oi {nome}! " if nome else "Oi! "
    return f"{prefixo}Ficou alguma dúvida sobre a MindMed? Pode perguntar à vontade 😊"

def mensagem_reengaj_2(nome: str) -> str:
    prefixo = f"{nome}, " if nome else ""
    return (
        f"{prefixo}que tal testar a plataforma por 48h de graça? Sem precisar de cartão 😄\n"
        f"Só me avisa que libero seu acesso!"
    )

def mensagem_reengaj_3(nome: str) -> str:
    prefixo = f"{nome}, " if nome else ""
    return (
        f"{prefixo}tudo bem? 🤙\n"
        f"Se um dia quiser conhecer a MindMed, é só chamar. Boa sorte nos estudos!"
    )


# ============================================================================
# ENVIO VIA Z-API — com retry explícito (correção #8)
# ============================================================================

async def enviar_whatsapp(telefone: str, texto: str) -> bool:
    """
    Envia mensagem via Z-API com até MAX_TENTATIVAS_ZAPI tentativas.
    Espera exponencial entre tentativas (ESPERA_RETRY_ZAPI * tentativa).
    Retorna True se enviou com sucesso, False se todas as tentativas falharam.
    """
    if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
        log.warning(f"[SIMULADO] Follow-up para {telefone}: {texto[:60]}...")
        return True

    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload  = {"phone": telefone, "message": texto}
    headers  = {"Content-Type": "application/json", "client-token": ZAPI_CLIENT_TOKEN}

    for tentativa in range(1, MAX_TENTATIVAS_ZAPI + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                log.info(f"✅ Follow-up enviado para {telefone} (tentativa {tentativa})")
                return True
        except Exception as e:
            if tentativa < MAX_TENTATIVAS_ZAPI:
                espera = ESPERA_RETRY_ZAPI * tentativa  # 1s, 2s
                log.warning(f"⚠️ Tentativa {tentativa}/{MAX_TENTATIVAS_ZAPI} falhou para {telefone}: {e} — aguardando {espera}s")
                await asyncio.sleep(espera)
            else:
                log.error(f"❌ Todas as {MAX_TENTATIVAS_ZAPI} tentativas falharam para {telefone}: {e}")

    return False


# ============================================================================
# HELPERS
# ============================================================================

def calcular_horas_sem_resposta(updated_at_str: str) -> float:
    try:
        if updated_at_str.endswith("Z"):
            updated_at_str = updated_at_str[:-1] + "+00:00"
        updated_at = datetime.fromisoformat(updated_at_str)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated_at).total_seconds() / 3600
    except Exception as e:
        log.error(f"Erro ao calcular horas: {e}")
        return 0


async def _enviar_followup(conversa: dict, numero: int, mensagem: str):
    """Envia follow-up e atualiza contador no Supabase apenas se enviado com sucesso."""
    telefone = conversa.get("telefone")
    nome     = conversa.get("nome_aluno", "")

    log.info(f"📤 Follow-up {numero} → {telefone} ({nome})")

    enviado = await enviar_whatsapp(telefone, mensagem)

    if enviado:
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
            "historico": historico[-20:],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()

        log.info(f"✅ Follow-up {numero} registrado para {telefone}")
    else:
        log.warning(f"⚠️ Follow-up {numero} NÃO registrado para {telefone} — envio falhou, será retentado na próxima rodada")


async def _finalizar_inativo(telefone: str, nome: str):
    log.info(f"🏁 Finalizando conversa inativa: {telefone}")

    supabase.table("conversas").update({
        "status_conversa": "FINALIZADO_INATIVO",
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("telefone", telefone).execute()

    try:
        supabase.table("leads").update({
            "status_conversa": "FINALIZADO_INATIVO",
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()
    except Exception:
        pass

    log.info(f"✅ Conversa {telefone} finalizada como INATIVO")


# ============================================================================
# LÓGICA DE FOLLOW-UP PRINCIPAL
# ============================================================================

async def processar_followups():
    """
    Verifica e envia follow-ups para conversas em espera.

    Sequência por contador_followups:
      -1 ou ausente → aguarda 1h → envia followup 0 (quente, confirma acesso)
      0             → aguarda 24h total → envia followup 1
      1             → aguarda 48h total → envia followup 2
      2             → aguarda 72h total → envia followup 3 + finaliza inativo

    O contador -1 é definido quando o trial é liberado (registrar_acesso_trial),
    indicando que o followup 0 ainda não foi enviado.
    """
    if not horario_permitido():
        log.info("🌙 Fora do horário permitido (07:00-21:00 horário de Brasília). Follow-ups pausados.")
        return

    log.info("🔍 Verificando follow-ups pendentes...")

    try:
        # CORREÇÃO #9 — FINALIZADO_ERRO incluído na busca
        # Antes, conversas travadas com FINALIZADO_ERRO ficavam no limbo
        # sem ser retomadas nem notificadas. Agora são incluídas na varredura
        # para que o scheduler possa identificá-las e registrá-las no log,
        # facilitando diagnóstico e intervenção manual quando necessário.
        resultado = (
            supabase.table("conversas")
            .select("*")
            .in_("status_conversa", [
                "ACESSO_LIBERADO",
                "AGUARDAR_FOLLOW_UP",
                "CADASTRO_ENVIADO",
                "FINALIZADO_ERRO"        # correção #9
            ])
            .execute()
        )

        conversas = resultado.data or []
        log.info(f"📋 {len(conversas)} conversa(s) elegível(eis) para follow-up")

        for conversa in conversas:
            telefone   = conversa.get("telefone", "")
            nome       = conversa.get("nome_aluno", "")
            updated_at = conversa.get("updated_at", "")
            followup_n = conversa.get("contador_followups", -1)
            status     = conversa.get("status_conversa", "")

            # CORREÇÃO #9 — trata FINALIZADO_ERRO separadamente:
            # registra no log para facilitar diagnóstico e pula o processamento
            # normal de follow-up (não faz sentido mandar mensagem de trial
            # para uma conversa que terminou em erro técnico).
            if status == "FINALIZADO_ERRO":
                log.warning(
                    f"⚠️ FINALIZADO_ERRO detectado | {telefone} ({nome}) | "
                    f"updated_at: {updated_at} — requer verificação manual ou "
                    f"chamada ao endpoint /retomar/{telefone}"
                )
                continue

            # Normaliza: None ou ausente = -1 (nunca enviou nenhum)
            if followup_n is None:
                followup_n = -1

            if not telefone or not updated_at:
                continue

            horas = calcular_horas_sem_resposta(updated_at)

            # Follow-up 0: 1h pós acesso liberado — lead ainda quente
            if followup_n == -1 and horas >= FOLLOWUP_0_HORAS:
                await _enviar_followup(conversa, 0, mensagem_followup_0(nome))

            # Follow-up 1: 24h
            elif followup_n == 0 and horas >= FOLLOWUP_1_HORAS:
                await _enviar_followup(conversa, 1, mensagem_followup_1(nome))

            # Follow-up 2: 48h
            elif followup_n == 1 and horas >= FOLLOWUP_2_HORAS:
                await _enviar_followup(conversa, 2, mensagem_followup_2(nome))

            # Follow-up 3: 72h → encerra
            elif followup_n == 2 and horas >= FOLLOWUP_3_HORAS:
                await _enviar_followup(conversa, 3, mensagem_followup_3(nome))
                await _finalizar_inativo(telefone, nome)

            else:
                proximo = {-1: FOLLOWUP_0_HORAS, 0: FOLLOWUP_1_HORAS,
                           1: FOLLOWUP_2_HORAS, 2: FOLLOWUP_3_HORAS}.get(followup_n)
                if proximo:
                    log.debug(f"⏳ {telefone} | FU{followup_n+1} em {proximo - horas:.1f}h")

    except Exception as e:
        log.error(f"❌ Erro ao processar follow-ups: {e}")


# ============================================================================
# LÓGICA DE REENGAJAMENTO
# ============================================================================

async def processar_reengajamento():
    if not horario_permitido():
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
            "historico": historico[-20:],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()

        log.info(f"✅ Reengajamento {numero} registrado para {telefone}")
    else:
        log.warning(f"⚠️ Reengajamento {numero} NÃO registrado para {telefone} — será retentado na próxima rodada")


# ============================================================================
# LOOP PRINCIPAL
# ============================================================================

async def iniciar_scheduler():
    log.info(f"🚀 Follow-up Scheduler iniciado")
    log.info(f"   Intervalo: {INTERVALO_VERIFICACAO}s ({INTERVALO_VERIFICACAO//60} min)")
    log.info(f"   Follow-ups: 0={FOLLOWUP_0_HORAS}h | 1={FOLLOWUP_1_HORAS}h | 2={FOLLOWUP_2_HORAS}h | 3={FOLLOWUP_3_HORAS}h")
    log.info(f"   Fuso horário: America/Sao_Paulo")
    log.info(f"   Retry Z-API: {MAX_TENTATIVAS_ZAPI} tentativas, espera {ESPERA_RETRY_ZAPI}s base")

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
# PONTO DE ENTRADA
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    asyncio.run(iniciar_scheduler())
