"""
Sistema de Agente IA - Bia MindMed
- Tem ferramentas que pode chamar autonomamente
- Age no mundo: cria leads, notifica time, consulta horários
- Loop de raciocínio: pensa → age → observa → responde
"""

import os
import json
import time
import httpx
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional, Any
from openai import OpenAI
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv(override=True)

# ============================================================================
# 1. CONFIGURAÇÃO
# ============================================================================

# Clientes
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# Config
MODELO_IA = "gpt-4o-mini" 
MAX_TENTATIVAS_API = 3
WEBHOOK_NOTIFICACAO = os.getenv("WEBHOOK_NOTIFICACAO_URL", "")
DAVI_WHATSAPP = os.getenv("DAVI_WHATSAPP", "")


def carregar_prompt(caminho: str = "prompt_mindmed.md") -> str:
    with open(caminho, "r", encoding="utf-8") as f:
        conteudo = f.read()
    if len(conteudo) < 500:
        raise ValueError(f"Prompt muito curto: {len(conteudo)} chars")
    print(f"✅ Prompt carregado ({len(conteudo)} chars)")
    return conteudo


try:
    SYSTEM_PROMPT = carregar_prompt()
except Exception as e:
    print(f"❌ ERRO AO CARREGAR PROMPT: {e}")
    SYSTEM_PROMPT = ""


# ============================================================================
# 2. DEFINIÇÃO DAS FERRAMENTAS
# ============================================================================

FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_dados_aluno",
            "description": (
                "Busca os dados cadastrais de um aluno no banco pelo telefone. "
                "Use SEMPRE antes de iniciar qualquer conversa para verificar "
                "se o aluno já está cadastrado e o que já foi coletado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {
                        "type": "string",
                        "description": "Telefone do aluno no formato E.164, ex: 5511999999999"
                    }
                },
                "required": ["telefone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "criar_ou_atualizar_lead",
            "description": (
                "Cria um novo lead ou atualiza os dados de um lead existente no CRM. "
                "Chame sempre que coletar informações novas do aluno: nome, fase, "
                "se usa flashcards, se presta residência esse ano, maior dificuldade. "
                "Também atualiza o status da conversa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {"type": "string"},
                    "nome": {"type": "string"},
                    "fase": {
                        "type": "string",
                        "enum": ["ciclo_basico", "ciclo_clinico", "internato", "formado", "residencia"]
                    },
                    "usa_flashcards": {"type": "boolean"},
                    "presta_residencia_esse_ano": {"type": "boolean"},
                    "maior_dificuldade": {"type": "string"},
                    "status_teste": {
                        "type": "string",
                        "enum": ["nao_iniciou", "testando", "testou_gostou", "testou_nao_gostou"]
                    },
                    "status_conversa": {
                        "type": "string",
                        "enum": [
                            "CONTINUAR", "CADASTRO_ENVIADO", "ACESSO_LIBERADO",
                            "AGUARDAR_FOLLOW_UP", "PASSAR_HUMANO", "FINALIZADO_SUCESSO",
                            "FINALIZADO_RECUSOU", "FINALIZADO_NAO_QUALIFICADO",
                            "FINALIZADO_INATIVO", "FINALIZADO_ERRO"
                        ]
                    }
                },
                "required": ["telefone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_acesso_trial",
            "description": (
                "Registra que o link de cadastro foi enviado ao aluno e notifica "
                "o time para liberar o acesso de 48h na plataforma. "
                "Use APÓS enviar o link https://app.mindmedicina.com/app/cadastro ao aluno "
                "e ele confirmar que se cadastrou."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {"type": "string"},
                    "nome_aluno": {"type": "string"},
                    "fase": {"type": "string"},
                    "contexto": {"type": "string"}
                },
                "required": ["telefone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notificar_time_comercial",
            "description": (
                "Envia notificação ao time comercial via webhook. "
                "Use nos seguintes momentos: "
                "1) Aluno confirmou cadastro (status: ACESSO_LIBERADO) — time precisa liberar acesso; "
                "2) Aluno quer fechar plano (status: PASSAR_HUMANO) — transferir para humano; "
                "3) Conversa finalizada (status: FINALIZADO_SUCESSO, FINALIZADO_RECUSOU, FINALIZADO_INATIVO). "
                "Sempre inclua resumo da conversa para o time ter contexto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {"type": "string"},
                    "nome_aluno": {"type": "string"},
                    "fase": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "ACESSO_LIBERADO", "PASSAR_HUMANO", "FINALIZADO_SUCESSO",
                            "FINALIZADO_RECUSOU", "FINALIZADO_NAO_QUALIFICADO", "FINALIZADO_INATIVO"
                        ]
                    },
                    "resumo_conversa": {"type": "string"},
                    "plano_interesse": {
                        "type": "string",
                        "enum": ["mensal", "anual", "bianual", "nao_informado"]
                    }
                },
                "required": ["telefone", "status"]
            }
        }
    }
]


# ============================================================================
# 3. IMPLEMENTAÇÃO DAS FERRAMENTAS
# ============================================================================

TAGS_STATUS = {
    "CONTINUAR":                  "🔵 EM_ATENDIMENTO",
    "CADASTRO_ENVIADO":           "🟡 CADASTRO_ENVIADO",
    "ACESSO_LIBERADO":            "🟢 TRIAL_ATIVO",
    "AGUARDAR_FOLLOW_UP":         "🔵 FOLLOW_UP",
    "PASSAR_HUMANO":              "🔴 FECHAR",
    "FINALIZADO_SUCESSO":         "✅ FECHOU",
    "FINALIZADO_RECUSOU":         "⚫ RECUSOU",
    "FINALIZADO_NAO_QUALIFICADO": "⚫ NAO_QUALIFICADO",
    "FINALIZADO_INATIVO":         "⚫ INATIVO",
    "FINALIZADO_ERRO":            "❌ ERRO",
}

def buscar_dados_aluno(telefone: str) -> Dict:
    try:
        resultado = (
            supabase.table("leads")
            .select("*")
            .eq("telefone", telefone)
            .limit(1)
            .execute()
        )
        if resultado.data:
            dados = resultado.data[0]
            print(f"✅ Aluno encontrado: {dados.get('nome', 'sem nome')}")
            return {"encontrado": True, "dados": dados}
        return {"encontrado": False, "dados": {}}
    except Exception as e:
        print(f"ℹ️ Erro ao buscar aluno no banco: {e}")
        return {"encontrado": False, "dados": {}}


def criar_ou_atualizar_lead(
    telefone: str,
    nome: str = None,
    fase: str = None,
    usa_flashcards: bool = None,
    presta_residencia_esse_ano: bool = None,
    maior_dificuldade: str = None,
    status_teste: str = None,
    status_conversa: str = None
) -> Dict:
    try:
        dados = {"telefone": telefone, "updated_at": datetime.now(timezone.utc).isoformat()}
        if nome: dados["nome"] = nome
        if fase: dados["fase"] = fase
        if usa_flashcards is not None: dados["usa_flashcards"] = usa_flashcards
        if presta_residencia_esse_ano is not None: dados["presta_residencia_esse_ano"] = presta_residencia_esse_ano
        if maior_dificuldade: dados["maior_dificuldade"] = maior_dificuldade
        if status_teste: dados["status_teste"] = status_teste
        if status_conversa: dados["status_conversa"] = status_conversa

        resultado = (
            supabase.table("leads")
            .upsert(dados, on_conflict="telefone")
            .execute()
        )
        print(f"✅ Lead salvo: {telefone}")
        return {"sucesso": True, "dados": resultado.data}
    except Exception as e:
        print(f"❌ Erro ao salvar lead: {e}")
        return {"sucesso": False, "erro": str(e)}


def registrar_acesso_trial(
    telefone: str,
    nome_aluno: str = None,
    fase: str = None,
    contexto: str = None
) -> Dict:
    try:
        criar_ou_atualizar_lead(
            telefone=telefone,
            nome=nome_aluno,
            fase=fase,
            status_teste="testando",
            status_conversa="ACESSO_LIBERADO"
        )
        notificar_time_comercial(
            telefone=telefone,
            nome_aluno=nome_aluno,
            fase=fase,
            status="ACESSO_LIBERADO",
            resumo_conversa=contexto or "Aluno se cadastrou. Liberar acesso de 48h na plataforma."
        )
        print(f"✅ Trial registrado para {telefone}")
        return {"sucesso": True, "mensagem": "Time notificado para liberar acesso de 48h."}
    except Exception as e:
        print(f"❌ Erro ao registrar trial: {e}")
        return {"sucesso": False, "erro": str(e)}


def notificar_time_comercial(
    telefone: str,
    status: str,
    nome_aluno: str = None,
    fase: str = None,
    resumo_conversa: str = None,
    plano_interesse: str = None
) -> Dict:
    tag = TAGS_STATUS.get(status, status)
    try:
        supabase.table("conversas").update({
            "tag_crm": tag,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()

        supabase.table("leads").update({
            "tag_crm": tag,
            "status_conversa": status,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).execute()
        print(f"🏷️ Tag CRM salva: {tag} para {telefone}")
    except Exception as e:
        print(f"⚠️ Erro ao salvar tag CRM: {e}")

    eventos_criticos = ["PASSAR_HUMANO", "ACESSO_LIBERADO", "CADASTRO_ENVIADO"]
    if status in eventos_criticos:
        notificar_davi_whatsapp(
            telefone_aluno=telefone,
            nome_aluno=nome_aluno,
            status=status,
            fase=fase,
            plano_interesse=plano_interesse,
            resumo_conversa=resumo_conversa
        )

    payload = {
        "evento": "notificacao_bia",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "telefone": telefone,
        "nome_aluno": nome_aluno,
        "fase": fase,
        "status": status,
        "tag_crm": tag,
        "resumo_conversa": resumo_conversa,
        "plano_interesse": plano_interesse
    }

    if not WEBHOOK_NOTIFICACAO:
        print(f"ℹ️ [WEBHOOK NÃO CONFIGURADO] Payload: {json.dumps(payload, ensure_ascii=False)[:200]}")
        return {"sucesso": True, "modo": "log_apenas"}

    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(WEBHOOK_NOTIFICACAO, json=payload)
        response.raise_for_status()
        print(f"✅ Webhook enviado | Status HTTP: {response.status_code}")
        return {"sucesso": True, "status_http": response.status_code}
    except Exception as e:
        print(f"❌ Erro ao enviar webhook: {e}")
        return {"sucesso": False, "erro": str(e)}


MAPA_FERRAMENTAS = {
    "buscar_dados_aluno": buscar_dados_aluno,
    "criar_ou_atualizar_lead": criar_ou_atualizar_lead,
    "registrar_acesso_trial": registrar_acesso_trial,
    "notificar_time_comercial": notificar_time_comercial
}


def notificar_davi_whatsapp(
    telefone_aluno: str,
    nome_aluno: str,
    status: str,
    fase: str = None,
    plano_interesse: str = None,
    resumo_conversa: str = None
):
    if not DAVI_WHATSAPP:
        print("ℹ️ DAVI_WHATSAPP não configurado — notificação não enviada")
        return

    zapi_instance = os.getenv("ZAPI_INSTANCE_ID", "")
    zapi_token    = os.getenv("ZAPI_TOKEN", "")
    zapi_client   = os.getenv("ZAPI_CLIENT_TOKEN", "")

    if not zapi_instance or not zapi_token:
        print("ℹ️ Credenciais Z-API não configuradas — notificação não enviada")
        return

    resumo = resumo_conversa or ""
    if status == "PASSAR_HUMANO":
        if resumo.startswith("🔧"):
            emoji = "🔧"; titulo = "PROBLEMA TÉCNICO"
            acao = "⚡ *AÇÃO:* Resolva o problema técnico do aluno!"
        elif resumo.startswith("📋"):
            emoji = "📋"; titulo = "PROBLEMA DE CONTEÚDO"
            acao = "⚡ *AÇÃO:* Verifique e corrija o conteúdo reportado!"
        elif resumo.startswith("❓"):
            emoji = "❓"; titulo = "DÚVIDA PENDENTE"
            acao = "⚡ *AÇÃO:* Responda a dúvida do aluno!"
        elif resumo.startswith("🟢"):
            emoji = "🟢"; titulo = "LIBERAR ACESSO"
            acao = "⚡ *AÇÃO:* Acesse app.mindmedicina.com e libere o acesso manualmente."
        elif resumo.startswith("🔴"):
            emoji = "🔴"; titulo = "LEAD QUER FECHAR"
            acao = "⚡ *AÇÃO:* Assuma a conversa e feche a venda agora!"
        else:
            emoji = "🔴"; titulo = "LEAD QUER FECHAR"
            acao = "⚡ *AÇÃO:* Assuma a conversa e feche a venda agora!"
    elif status == "ACESSO_LIBERADO":
        emoji = "🟢"; titulo = "NOVO TRIAL — LIBERAR ACESSO AGORA"
        acao = "⚡ *AÇÃO:* Acesse app.mindmedicina.com e libere o trial de 48h manualmente."
    elif status == "CADASTRO_ENVIADO":
        emoji = "🔵"; titulo = "ALUNO SE CADASTROU — AGUARDANDO TRIAL"
        acao = None
    else:
        emoji = "🟡"; titulo = "ATENÇÃO NECESSÁRIA"
        acao = None

    linhas = [
        f"{emoji} *{titulo}*",
        f"Nome: {nome_aluno or 'não informado'}",
        f"Telefone: +{telefone_aluno}",
    ]
    if fase:
        linhas.append(f"Fase: {fase}")
    if plano_interesse and plano_interesse != "nao_informado":
        linhas.append(f"Plano de interesse: {plano_interesse}")
    if resumo_conversa:
        linhas.append(f"Contexto: {resumo_conversa}")
    if acao:
        linhas.append(f"\n{acao}")

    mensagem = "\n".join(linhas)

    try:
        url = f"https://api.z-api.io/instances/{zapi_instance}/token/{zapi_token}/send-text"
        payload = {"phone": DAVI_WHATSAPP, "message": mensagem}
        headers = {"Content-Type": "application/json", "client-token": zapi_client}
        print(f"📤 Notificando Davi ({DAVI_WHATSAPP}) | Evento: {status}")
        with httpx.Client(timeout=20) as client:
            response = client.post(url, json=payload, headers=headers)
        print(f"📬 Z-API resposta: {response.status_code} | {response.text[:150]}")
        response.raise_for_status()
        print(f"✅ Davi notificado | Status: {status}")
    except Exception as e:
        print(f"❌ Erro ao notificar Davi via Z-API: {e}")


# ============================================================================
# 4. LOOP DO AGENTE
# ============================================================================

def executar_agente(
    telefone: str,
    historico_conversa: List[Dict],
    contador_mensagens: int = 0
) -> Tuple[str, str, Dict]:

    if not SYSTEM_PROMPT:
        return ("Problema técnico. Tente novamente.", "FINALIZADO_ERRO", {})

    prompt = SYSTEM_PROMPT
    if contador_mensagens >= 3:
        prompt += (
            f"\n\n⚠️ ATENÇÃO: Esta é sua mensagem #{contador_mensagens + 1}. "
            "Se o aluno não demonstrou interesse claro, encerre a conversa com elegância."
        )

    # ── C2/C3: REMINDER reforçado ────────────────────────────────────────────
    prompt_final = (
        prompt
        + f"\n\nCONTEXTO DA SESSÃO: O telefone do aluno nesta conversa é {telefone}. "
        "Use este valor em TODAS as chamadas de ferramentas quando o campo 'telefone' for necessário."
        "\n\nREGRA CRÍTICA — FERRAMENTA + JSON NA MESMA RESPOSTA:"
        "\n1. Se você precisa chamar uma ferramenta E responder ao aluno, faça os dois na mesma resposta."
        "\n2. NUNCA espere o aluno confirmar ('ok', 'tá bom') para disparar uma ferramenta. Dispare IMEDIATAMENTE ao decidir."
        "\n3. Após executar todas as ferramentas necessárias, retorne APENAS o JSON abaixo — sem texto antes, sem texto depois:"
        '\n{"resposta": "sua mensagem aqui", "status": "CONTINUAR", "dados_coletados": {"nome": null, "fase": null, "usa_flashcards": null, "presta_residencia_esse_ano": null, "maior_dificuldade": null, "status_teste": null}}'
        "\n4. Se não retornar JSON puro, o sistema quebra e o aluno não recebe nada."
    )

    mensagens = [{"role": "system", "content": prompt_final}] + historico_conversa

    max_iteracoes = 5
    for iteracao in range(max_iteracoes):
        print(f"🔄 Iteração do agente #{iteracao + 1}")

        for tentativa in range(1, MAX_TENTATIVAS_API + 1):
            try:
                if tentativa > 1:
                    print(f"  🔄 Retry {tentativa}/{MAX_TENTATIVAS_API}...")
                    time.sleep(1)

                response = openai_client.chat.completions.create(
                    model=MODELO_IA,
                    messages=mensagens,
                    tools=FERRAMENTAS,
                    tool_choice="auto",
                    temperature=0.5,
                    max_tokens=500,
                    timeout=30
                )
                break

            except Exception as e:
                print(f"  ⚠️ Erro na API (tentativa {tentativa}): {e}")
                if tentativa == MAX_TENTATIVAS_API:
                    return ("Problema técnico momentâneo. Pode tentar de novo?", "CONTINUAR", {})

        choice = response.choices[0]
        message = choice.message

        # CASO 1: modelo quer chamar ferramentas
        if message.tool_calls:
            mensagens.append(message)

            for tool_call in message.tool_calls:
                nome_fn = tool_call.function.name
                args = json.loads(tool_call.function.arguments)

                if "telefone" in args and not args.get("telefone"):
                    args["telefone"] = telefone

                print(f"  🔧 Ferramenta: {nome_fn}({json.dumps(args, ensure_ascii=False)})")

                fn = MAPA_FERRAMENTAS.get(nome_fn)
                resultado = fn(**args) if fn else {"erro": f"Ferramenta '{nome_fn}' não encontrada"}

                print(f"  📤 Resultado: {json.dumps(resultado, ensure_ascii=False)[:200]}")

                mensagens.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(resultado, ensure_ascii=False)
                })

            continue

        # CASO 2: modelo gerou resposta final
        if message.content:
            conteudo = message.content.strip()

            # ── C1: normaliza \n\n escapado antes de parsear ─────────────────
            conteudo_normalizado = conteudo.replace("\\n\\n", "\n\n").replace("\\n", "\n")

            try:
                dados_json = json.loads(conteudo_normalizado)
                resposta = dados_json.get("resposta", "")
                status = dados_json.get("status", "CONTINUAR")
                dados_coletados = dados_json.get("dados_coletados", {})
            except json.JSONDecodeError:
                # Fallback: tenta o conteúdo original sem normalização
                try:
                    dados_json = json.loads(conteudo)
                    resposta = dados_json.get("resposta", "")
                    status = dados_json.get("status", "CONTINUAR")
                    dados_coletados = dados_json.get("dados_coletados", {})
                except json.JSONDecodeError:
                    print("⚠️ Resposta não é JSON, usando texto direto")
                    resposta = conteudo
                    status = "CONTINUAR"
                    dados_coletados = {}

            # ── C1: normaliza \n\n no campo resposta (caso venha escapado) ───
            if isinstance(resposta, str):
                resposta = resposta.replace("\\n\\n", "\n\n").replace("\\n", "\n")

            status_validos = [
                "CONTINUAR", "CADASTRO_ENVIADO", "ACESSO_LIBERADO", "AGUARDAR_FOLLOW_UP",
                "PASSAR_HUMANO", "FINALIZADO_SUCESSO", "FINALIZADO_RECUSOU",
                "FINALIZADO_NAO_QUALIFICADO", "FINALIZADO_INATIVO", "FINALIZADO_ERRO"
            ]
            if status not in status_validos:
                status = "CONTINUAR"

            print(f"✅ Resposta gerada | Status: {status} | {len(resposta)} chars")
            return (resposta, status, dados_coletados)

        print("⚠️ Modelo retornou resposta vazia")
        return ("Desculpe, pode repetir?", "CONTINUAR", {})

    print("⚠️ Loop do agente esgotou iterações")
    return ("Hmm, tive um problema aqui. Pode tentar de novo?", "CONTINUAR", {})


# ============================================================================
# 5. GESTOR DE CONVERSAS
# ============================================================================

class GestorConversasMindMed:

    def processar_mensagem(self, telefone: str, mensagem: str) -> Dict:
        print(f"\n{'='*60}")
        print(f"📨 Mensagem de {telefone}: {mensagem[:80]}")

        try:
            estado = self._buscar_estado(telefone)

            status_finalizados = [
                "FINALIZADO_RECUSOU", "FINALIZADO_SUCESSO", "FINALIZADO_INATIVO",
                "FINALIZADO_NAO_QUALIFICADO", "FINALIZADO_ERRO"
            ]
            if estado.get("status_conversa") in status_finalizados:
                print(f"ℹ️ Conversa já encerrada com status {estado['status_conversa']}")
                return {"resposta": "", "status": estado["status_conversa"], "deve_enviar": False}

            if estado.get("status_conversa") in ("PASSAR_HUMANO", "ACESSO_LIBERADO"):
                status_atual = estado["status_conversa"]
                print(f"ℹ️ Agente em pausa [{status_atual}] para {telefone}")
                historico = estado.get("historico", [])
                historico.append({"role": "user", "content": mensagem})
                supabase.table("conversas").update({
                    "historico": historico[-20:],
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("telefone", telefone).execute()
                return {"resposta": "", "status": status_atual, "deve_enviar": False}

            historico = estado.get("historico", [])
            contador = estado.get("contador_mensagens_alex", 0)

            historico.append({"role": "user", "content": mensagem})

            resposta, status, dados_coletados = executar_agente(
                telefone=telefone,
                historico_conversa=historico,
                contador_mensagens=contador
            )

            historico.append({"role": "assistant", "content": resposta})
            contador += 1

            self._salvar_estado(
                telefone=telefone,
                historico=historico,
                status=status,
                contador=contador,
                dados_coletados=dados_coletados
            )

            # Fallback de notificação (segurança caso ferramenta não tenha sido chamada)
            status_anterior = estado.get("status_conversa", "CONTINUAR")
            if status_anterior == "ATIVO":
                status_anterior = "CONTINUAR"
            status_criticos = ["PASSAR_HUMANO", "ACESSO_LIBERADO", "CADASTRO_ENVIADO", "FINALIZADO_INATIVO", "FINALIZADO_SUCESSO"]
            tag_ja_salva = estado.get("tag_crm", "")
            ferramenta_ja_chamou = status in status_criticos and TAGS_STATUS.get(status, "") in tag_ja_salva
            if status in status_criticos and status != status_anterior and not ferramenta_ja_chamou:
                nome = dados_coletados.get("nome") or estado.get("nome_aluno", "")
                fase = dados_coletados.get("fase") or estado.get("fase", "")
                notificar_time_comercial(
                    telefone=telefone,
                    nome_aluno=nome,
                    fase=fase,
                    status=status,
                    resumo_conversa=f"Status automático detectado pelo sistema: {status}"
                )

            print(f"✅ Processado | Status: {status}")
            return {
                "resposta": resposta,
                "status": status,
                "deve_enviar": bool(resposta),
                "dados_coletados": dados_coletados
            }

        except Exception as e:
            print(f"❌ Erro crítico ao processar mensagem: {e}")
            return {
                "resposta": "Ops, tive um problema técnico. Pode tentar novamente?",
                "status": "CONTINUAR",
                "deve_enviar": True,
                "dados_coletados": {}
            }

    def _buscar_estado(self, telefone: str) -> Dict:
        try:
            resultado = (
                supabase.table("conversas")
                .select("*")
                .eq("telefone", telefone)
                .limit(1)
                .execute()
            )
            if resultado.data:
                return resultado.data[0]
            return {
                "telefone": telefone,
                "historico": [],
                "status_conversa": "ATIVO",
                "contador_mensagens_alex": 0
            }
        except Exception as e:
            print(f"ℹ️ Erro ao buscar estado da conversa: {e}")
            return {
                "telefone": telefone,
                "historico": [],
                "status_conversa": "ATIVO",
                "contador_mensagens_alex": 0
            }

    def _salvar_estado(
        self,
        telefone: str,
        historico: List,
        status: str,
        contador: int,
        dados_coletados: Dict
    ):
        MAX_HISTORICO = 20
        if len(historico) > MAX_HISTORICO:
            historico = historico[-MAX_HISTORICO:]

        dados = {
            "telefone": telefone,
            "historico": historico,
            "status_conversa": status,
            "contador_mensagens_alex": contador,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        if dados_coletados.get("nome"): dados["nome_aluno"] = dados_coletados["nome"]
        if dados_coletados.get("fase"): dados["fase"] = dados_coletados["fase"]
        if dados_coletados.get("usa_flashcards") is not None: dados["usa_flashcards"] = dados_coletados["usa_flashcards"]
        if dados_coletados.get("presta_residencia_esse_ano") is not None: dados["presta_residencia_esse_ano"] = dados_coletados["presta_residencia_esse_ano"]
        if dados_coletados.get("maior_dificuldade"): dados["maior_dificuldade"] = dados_coletados["maior_dificuldade"]
        if dados_coletados.get("status_teste"): dados["status_teste"] = dados_coletados["status_teste"]

        supabase.table("conversas").upsert(dados, on_conflict="telefone").execute()


# ============================================================================
# 6. SIMULADOR
# ============================================================================

def simular_conversa():
    print("=" * 65)
    print("🤖 SIMULADOR - BIA MINDMED (Agente IA)")
    print("=" * 65)

    telefone = input("📱 Telefone do aluno (Enter = 5511999999999): ").strip() or "5511999999999"
    gestor = GestorConversasMindMed()

    print(f"\n💬 Conversa iniciada para {telefone}")
    print("    Digite 'sair' para encerrar\n")
    print("-" * 65)

    while True:
        mensagem = input("\n👤 ALUNO: ").strip()
        if not mensagem: continue
        if mensagem.lower() in ["sair", "exit", "quit"]:
            print("\n👋 Simulação encerrada.")
            break

        resultado = gestor.processar_mensagem(telefone=telefone, mensagem=mensagem)
        print(f"\n🤖 BIA: {resultado['resposta']}")
        print(f"\n📊 Status: {resultado['status']}")
        if resultado.get("dados_coletados") and any(resultado["dados_coletados"].values()):
            print(f"📝 Dados coletados: {resultado['dados_coletados']}")
        print("-" * 65)

        if resultado["status"].startswith("FINALIZADO"):
            print(f"\n✅ Conversa encerrada: {resultado['status']}")
            break


# ============================================================================
# 7. PONTO DE ENTRADA
# ============================================================================

if __name__ == "__main__":
    simular_conversa()
