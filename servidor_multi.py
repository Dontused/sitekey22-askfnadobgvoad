from flask import Flask, request, jsonify, send_file
import requests
import time
import os
import threading
import sys
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# =========================================================
# CONFIGURAÇÃO DE CAMINHOS
# =========================================================

if getattr(sys, "frozen", False):
    PASTA_PROGRAMA = Path(sys.executable).resolve().parent
else:
    PASTA_PROGRAMA = Path(__file__).resolve().parent

NOME_ARQUIVO = "config_te.txt"
CAMINHO_ARQUIVO = PASTA_PROGRAMA / NOME_ARQUIVO
ARQUIVO_CONTROLE = PASTA_PROGRAMA / ".sitekey_controle"
ARQUIVO_PC_ID = PASTA_PROGRAMA / ".sitekey_pc_id"

PASTA_ARQUIVOS = PASTA_PROGRAMA / "arquivos"
PASTA_ARQUIVOS.mkdir(parents=True, exist_ok=True)

MODO = os.environ.get("MODE", "servidor").lower()
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")

INTERVALO = 5
TIMEOUT_ONLINE = 20
FUSO_BRASIL = timezone(timedelta(hours=-3))
HORA_ENVIO = 12
MINUTO_ENVIO = 25
MAXIMO_ARQUIVOS = 25

# =========================================================
# IDENTIFICAÇÃO DO COMPUTADOR
# =========================================================

def obter_pc_id():
    """
    Cria um ID único para este computador e o mantém salvo.
    Assim o mesmo notebook continua sendo reconhecido mesmo
    depois de reiniciar.
    """
    try:
        if ARQUIVO_PC_ID.exists():
            valor = ARQUIVO_PC_ID.read_text(encoding="utf-8").strip()
            if valor:
                return valor

        nome = os.environ.get("COMPUTERNAME", "PC").strip()
        mac = uuid.getnode()

        pc_id = f"{nome}-{mac:012X}"

        ARQUIVO_PC_ID.write_text(pc_id, encoding="utf-8")
        return pc_id

    except Exception:
        return f"PC-{uuid.uuid4().hex[:12]}"


PC_ID = obter_pc_id()
PC_NOME = os.environ.get("COMPUTERNAME", PC_ID)

# =========================================================
# DADOS DOS PCS CONECTADOS
# =========================================================

# Estrutura:
# pcs[pc_id] = {
#     "nome": "NOTEBOOK-01",
#     "ultimo_heartbeat": timestamp,
#     "atualizacao_solicitada": False
# }

pcs = {}
lock_pcs = threading.Lock()


def agora_brasil():
    return datetime.now(FUSO_BRASIL)


def obter_dados_pc(pc_id):
    with lock_pcs:
        return pcs.get(pc_id)


def garantir_pc(pc_id, nome=None):
    with lock_pcs:
        if pc_id not in pcs:
            pcs[pc_id] = {
                "nome": nome or pc_id,
                "ultimo_heartbeat": 0,
                "atualizacao_solicitada": False
            }

        if nome:
            pcs[pc_id]["nome"] = nome

        return pcs[pc_id]


def pasta_do_pc(pc_id):
    # Impede tentativa de usar caminhos fora da pasta "arquivos".
    pc_id_seguro = os.path.basename(pc_id)
    pasta = PASTA_ARQUIVOS / pc_id_seguro
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def nome_pc_exibicao(pc_id):
    dados = obter_dados_pc(pc_id)
    if dados:
        return dados.get("nome") or pc_id
    return pc_id


@app.after_request
def adicionar_cors(resposta):
    resposta.headers["Access-Control-Allow-Origin"] = "*"
    resposta.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, DELETE, OPTIONS"
    )
    resposta.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resposta


# =========================================================
# INDEX / INFORMAÇÕES DO SERVIDOR
# =========================================================

@app.route("/")
def index():
    return jsonify({
        "servidor": "Sitekey",
        "status": "online",
        "modo": MODO,
        "pc_id": PC_ID if MODO == "secundario" else None
    })


# =========================================================
# LISTAR TODOS OS PCS
# =========================================================

@app.route("/pcs")
def listar_pcs():
    agora = time.time()
    resultado = []

    with lock_pcs:
        snapshot = dict(pcs)

    for pc_id, dados in snapshot.items():
        ultimo = dados.get("ultimo_heartbeat", 0)
        online = bool(ultimo and (agora - ultimo <= TIMEOUT_ONLINE))

        data_heartbeat = None

        if ultimo:
            data_heartbeat = datetime.fromtimestamp(
                ultimo,
                timezone.utc
            ).astimezone(FUSO_BRASIL).strftime("%d/%m/%Y %H:%M:%S")

        resultado.append({
            "id": pc_id,
            "nome": dados.get("nome") or pc_id,
            "online": online,
            "ultimo_contato": ultimo,
            "data_heartbeat": data_heartbeat,
            "atualizacao_solicitada": bool(
                dados.get("atualizacao_solicitada")
            )
        })

    # Online primeiro, depois por nome.
    resultado.sort(
        key=lambda pc: (
            not pc["online"],
            pc["nome"].lower()
        )
    )

    return jsonify({
        "sucesso": True,
        "pcs": resultado
    })


# =========================================================
# STATUS DE UM PC
# =========================================================

@app.route("/pc_status")
def pc_status():
    pc_id = request.args.get("pc_id", "").strip()

    if not pc_id:
        return jsonify({
            "sucesso": False,
            "erro": "pc_id não informado."
        }), 400

    dados = obter_dados_pc(pc_id)

    if not dados:
        return jsonify({
            "sucesso": True,
            "online": False,
            "id": pc_id,
            "nome": pc_id,
            "data_heartbeat": None
        })

    ultimo = dados.get("ultimo_heartbeat", 0)
    online = bool(ultimo and (time.time() - ultimo <= TIMEOUT_ONLINE))

    data_heartbeat = None

    if ultimo:
        data_heartbeat = datetime.fromtimestamp(
            ultimo,
            timezone.utc
        ).astimezone(FUSO_BRASIL).strftime("%d/%m/%Y %H:%M:%S")

    return jsonify({
        "sucesso": True,
        "online": online,
        "id": pc_id,
        "nome": dados.get("nome") or pc_id,
        "ultimo_contato": ultimo,
        "data_heartbeat": data_heartbeat
    })


# =========================================================
# HEARTBEAT
# =========================================================

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    dados = request.get_json(silent=True) or {}

    pc_id = str(dados.get("pc_id", "")).strip()
    pc_nome = str(dados.get("pc_nome", "")).strip()

    if not pc_id:
        return jsonify({
            "sucesso": False,
            "erro": "pc_id não informado."
        }), 400

    if not pc_nome:
        pc_nome = pc_id

    pc = garantir_pc(pc_id, pc_nome)

    with lock_pcs:
        pc["ultimo_heartbeat"] = time.time()

    return jsonify({
        "sucesso": True,
        "online": True,
        "pc_id": pc_id,
        "pc_nome": pc_nome,
        "mensagem": "PC secundário conectado."
    })


# =========================================================
# LISTAR ARQUIVOS DE UM PC
# =========================================================

@app.route("/arquivos")
def listar_arquivos():
    pc_id = request.args.get("pc_id", "").strip()

    if not pc_id:
        return jsonify({
            "sucesso": False,
            "erro": "pc_id não informado."
        }), 400

    pasta = pasta_do_pc(pc_id)
    arquivos = []

    for arquivo in pasta.iterdir():
        if arquivo.is_file():
            dados = arquivo.stat()

            data = datetime.fromtimestamp(
                dados.st_mtime,
                timezone.utc
            ).astimezone(FUSO_BRASIL)

            arquivos.append({
                "id": arquivo.name,
                "nome": arquivo.name,
                "pc_id": pc_id,
                "pc_nome": nome_pc_exibicao(pc_id),
                "data": data.strftime("%d/%m/%Y"),
                "hora": data.strftime("%H:%M:%S"),
                "timestamp": dados.st_mtime
            })

    arquivos.sort(
        key=lambda x: x["timestamp"],
        reverse=True
    )

    return jsonify({
        "sucesso": True,
        "pc_id": pc_id,
        "arquivos": arquivos
    })


# =========================================================
# SOLICITAR ATUALIZAÇÃO PARA UM PC ESPECÍFICO
# =========================================================

@app.route("/atualizar", methods=["POST"])
def atualizar():
    pc_id = request.args.get("pc_id", "").strip()

    if not pc_id:
        dados = request.get_json(silent=True) or {}
        pc_id = str(dados.get("pc_id", "")).strip()

    if not pc_id:
        return jsonify({
            "sucesso": False,
            "erro": "pc_id não informado."
        }), 400

    pc = obter_dados_pc(pc_id)

    if not pc:
        return jsonify({
            "sucesso": False,
            "erro": "Computador não encontrado."
        }), 404

    with lock_pcs:
        pcs[pc_id]["atualizacao_solicitada"] = True

    print("======================================")
    print("Pedido de atualização recebido.")
    print("PC:", pc_id)
    print("Nome:", pc.get("nome"))

    return jsonify({
        "sucesso": True,
        "pc_id": pc_id,
        "pc_nome": pc.get("nome"),
        "mensagem": "Pedido enviado ao PC secundário."
    })


# =========================================================
# PC SECUNDÁRIO VERIFICA SE TEM ATUALIZAÇÃO
# =========================================================

@app.route("/verificar")
def verificar():
    pc_id = request.args.get("pc_id", "").strip()

    if not pc_id:
        return jsonify({
            "atualizar": False,
            "erro": "pc_id não informado."
        }), 400

    pc = obter_dados_pc(pc_id)

    if not pc:
        garantir_pc(pc_id, pc_id)
        return jsonify({
            "atualizar": False
        })

    with lock_pcs:
        atualizar_pedido = bool(
            pcs[pc_id].get("atualizacao_solicitada")
        )

    return jsonify({
        "atualizar": atualizar_pedido
    })


# =========================================================
# PC INFORMANDO QUE NÃO POSSUI ARQUIVO
# =========================================================

@app.route("/sem_arquivo", methods=["POST"])
def sem_arquivo():
    pc_id = request.args.get("pc_id", "").strip()

    if not pc_id:
        dados = request.get_json(silent=True) or {}
        pc_id = str(dados.get("pc_id", "")).strip()

    if not pc_id:
        return jsonify({
            "sucesso": False,
            "erro": "pc_id não informado."
        }), 400

    with lock_pcs:
        if pc_id in pcs:
            pcs[pc_id]["atualizacao_solicitada"] = False

    print("Pedido de atualização cancelado:")
    print("nenhum config_te.txt disponível.")
    print("PC:", pc_id)

    return jsonify({
        "sucesso": True,
        "mensagem": "Nenhum arquivo disponível para enviar."
    })


# =========================================================
# UPLOAD
# =========================================================

@app.route("/upload", methods=["POST"])
def upload():
    arquivo = request.files.get("arquivo")

    pc_id = request.form.get("pc_id", "").strip()
    pc_nome = request.form.get("pc_nome", "").strip()

    if not pc_id:
        return jsonify({
            "sucesso": False,
            "erro": "pc_id não informado."
        }), 400

    if not arquivo:
        return jsonify({
            "sucesso": False,
            "erro": "Arquivo não enviado."
        }), 400

    if not (arquivo.filename or "").lower().endswith(".txt"):
        return jsonify({
            "sucesso": False,
            "erro": "Arquivo inválido."
        }), 400

    garantir_pc(pc_id, pc_nome or pc_id)

    agora = agora_brasil()

    nome = (
        f"config_te_"
        f"{agora.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    )

    pasta = pasta_do_pc(pc_id)
    caminho = pasta / nome

    try:
        arquivo.save(caminho)
    except Exception as erro:
        print("Erro ao salvar arquivo:", erro)

        return jsonify({
            "sucesso": False,
            "erro": str(erro)
        }), 500

    print("Arquivo recebido:", nome)
    print("PC:", pc_id)
    print("Nome:", pc_nome or pc_id)

    # Mantém no máximo MAXIMO_ARQUIVOS por computador.
    try:
        arquivos = [
            item for item in pasta.iterdir()
            if item.is_file() and item.name.startswith("config_te_")
        ]

        arquivos.sort(
            key=lambda item: item.stat().st_mtime
        )

        while len(arquivos) > MAXIMO_ARQUIVOS:
            arquivo_antigo = arquivos.pop(0)

            try:
                arquivo_antigo.unlink()
                print(
                    "Arquivo antigo excluído:",
                    arquivo_antigo.name
                )
            except Exception as erro:
                print(
                    "Erro ao excluir arquivo antigo:",
                    erro
                )

    except Exception as erro:
        print("Erro ao organizar arquivos:", erro)

    with lock_pcs:
        if pc_id in pcs:
            pcs[pc_id]["atualizacao_solicitada"] = False

    return jsonify({
        "sucesso": True,
        "arquivo": nome,
        "pc_id": pc_id,
        "pc_nome": pc_nome or pc_id,
        "data": agora.strftime("%d/%m/%Y"),
        "hora": agora.strftime("%H:%M:%S")
    })


# =========================================================
# BAIXAR ARQUIVO
# =========================================================

@app.route("/baixar/<pc_id>/<nome>")
def baixar(pc_id, nome):
    pc_id = os.path.basename(pc_id)
    nome = os.path.basename(nome)

    pasta = pasta_do_pc(pc_id)
    caminho = pasta / nome

    if not caminho.exists() or not caminho.is_file():
        return jsonify({
            "erro": "Arquivo não encontrado."
        }), 404

    return send_file(
        caminho,
        as_attachment=True,
        download_name=NOME_ARQUIVO
    )


# =========================================================
# EXCLUIR ARQUIVO
# =========================================================

@app.route("/excluir/<pc_id>/<nome>", methods=["DELETE"])
def excluir(pc_id, nome):
    pc_id = os.path.basename(pc_id)
    nome = os.path.basename(nome)

    pasta = pasta_do_pc(pc_id)
    caminho = pasta / nome

    if not caminho.exists() or not caminho.is_file():
        return jsonify({
            "sucesso": False,
            "erro": "Arquivo não encontrado."
        }), 404

    try:
        caminho.unlink()

        print("Arquivo excluído:", nome)
        print("PC:", pc_id)

        return jsonify({
            "sucesso": True,
            "mensagem": "Arquivo excluído."
        })

    except Exception as erro:
        print("Erro ao excluir arquivo:", erro)

        return jsonify({
            "sucesso": False,
            "erro": str(erro)
        }), 500


# =========================================================
# ENVIO DO PC SECUNDÁRIO PARA O SERVIDOR
# =========================================================

def enviar_arquivo(caminho):
    if not SERVER_URL:
        print("SERVER_URL não configurado.")
        return False

    try:
        with open(caminho, "rb") as arquivo:
            resposta = requests.post(
                SERVER_URL + "/upload",
                files={
                    "arquivo": (
                        NOME_ARQUIVO,
                        arquivo,
                        "text/plain"
                    )
                },
                data={
                    "pc_id": PC_ID,
                    "pc_nome": PC_NOME
                },
                timeout=30
            )

        if resposta.status_code == 200:
            print("config_te.txt enviado com sucesso.")

            try:
                caminho.unlink()
                print(
                    "config_te.txt excluído do PC secundário."
                )
            except Exception as erro:
                print(
                    "Erro ao excluir config_te.txt:",
                    erro
                )

            return True

        print(
            "Erro no upload:",
            resposta.status_code,
            resposta.text
        )

        return False

    except Exception as erro:
        print("Erro ao enviar:", erro)
        return False


# =========================================================
# HEARTBEAT DO PC SECUNDÁRIO
# =========================================================

def enviar_heartbeat():
    while True:
        if SERVER_URL:
            try:
                resposta = requests.post(
                    SERVER_URL + "/heartbeat",
                    json={
                        "pc_id": PC_ID,
                        "pc_nome": PC_NOME
                    },
                    timeout=10
                )

                if resposta.status_code == 200:
                    print(
                        "PC conectado ao servidor:",
                        PC_NOME
                    )
                else:
                    print("Servidor não disponível.")

            except Exception:
                print("Servidor não disponível.")

        time.sleep(INTERVALO)


# =========================================================
# EXECUÇÃO DO PC SECUNDÁRIO
# =========================================================

def executar_computador():
    if not SERVER_URL:
        print("ERRO: SERVER_URL não configurado.")
        return

    print("==============================")
    print("       SITEKEY - HOST")
    print("==============================")
    print("Servidor:", SERVER_URL)
    print("PC ID:", PC_ID)
    print("PC Nome:", PC_NOME)
    print("Arquivo:", CAMINHO_ARQUIVO)
    print("Pasta do programa:", PASTA_PROGRAMA)
    print("Envio automático: todos os dias às 13:00")
    print("Horário: GMT-3 / Brasil")
    print("Máximo de arquivos por PC:", MAXIMO_ARQUIVOS)
    print("Excluir após envio: SIM")

    thread_heartbeat = threading.Thread(
        target=enviar_heartbeat,
        daemon=True
    )

    thread_heartbeat.start()

    ultimo_dia_automatico = ""

    try:
        if ARQUIVO_CONTROLE.exists():
            ultimo_dia_automatico = (
                ARQUIVO_CONTROLE
                .read_text(encoding="utf-8")
                .strip()
            )
    except Exception:
        ultimo_dia_automatico = ""

    while True:
        try:
            agora = agora_brasil()
            data_atual = agora.strftime("%Y-%m-%d")

            # Verifica se este PC recebeu pedido manual.
            resposta = requests.get(
                SERVER_URL + "/verificar",
                params={"pc_id": PC_ID},
                timeout=10
            )

            if resposta.status_code == 200:
                dados_pedido = resposta.json()

                if dados_pedido.get("atualizar"):
                    print("================================")
                    print("Pedido manual recebido.")

                    if CAMINHO_ARQUIVO.exists():
                        print("Enviando config_te.txt...")

                        sucesso = enviar_arquivo(
                            CAMINHO_ARQUIVO
                        )

                        if sucesso:
                            print(
                                "Atualização manual concluída."
                            )
                        else:
                            print(
                                "Falha no envio manual."
                            )

                    else:
                        print(
                            "Nenhum config_te.txt disponível "
                            "para enviar."
                        )

                        try:
                            requests.post(
                                SERVER_URL + "/sem_arquivo",
                                params={"pc_id": PC_ID},
                                timeout=2
                            )
                        except requests.exceptions.RequestException:
                            pass

            # =================================================
            # ENVIO AUTOMÁTICO DIÁRIO
            # =================================================

            passou_do_horario = (
                agora.hour > HORA_ENVIO
                or (
                    agora.hour == HORA_ENVIO
                    and agora.minute >= MINUTO_ENVIO
                )
            )

            if (
                passou_do_horario
                and ultimo_dia_automatico != data_atual
            ):
                print("Horário automático atingido.")
                print("Horário programado: 13:00")

                if CAMINHO_ARQUIVO.exists():
                    if enviar_arquivo(CAMINHO_ARQUIVO):
                        ultimo_dia_automatico = data_atual

                        try:
                            ARQUIVO_CONTROLE.write_text(
                                data_atual,
                                encoding="utf-8"
                            )
                        except Exception as erro:
                            print(
                                "Erro ao salvar controle:",
                                erro
                            )

                        print(
                            "Envio automático concluído."
                        )
                        print(
                            "Próximo envio: amanhã às 13:00."
                        )
                    else:
                        print(
                            "Upload automático falhou."
                        )

                else:
                    # Marca o dia como processado mesmo sem arquivo.
                    ultimo_dia_automatico = data_atual

                    try:
                        ARQUIVO_CONTROLE.write_text(
                            data_atual,
                            encoding="utf-8"
                        )
                    except Exception as erro:
                        print(
                            "Erro ao salvar controle:",
                            erro
                        )

            time.sleep(INTERVALO)

        except requests.exceptions.RequestException:
            time.sleep(INTERVALO)

        except Exception as erro:
            print("Erro no computador:", erro)
            time.sleep(INTERVALO)


# =========================================================
# INICIALIZAÇÃO
# =========================================================

if __name__ == "__main__":
    if MODO == "secundario":
        executar_computador()
    else:
        porta = int(os.environ.get("PORT", "5000"))

        app.run(
            host="0.0.0.0",
            port=porta,
            debug=False
        )
