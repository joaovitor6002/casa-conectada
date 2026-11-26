"""Servidor da Casa Conectada.

- Serve o site (../site-online)
- Login de verdade (senha com hash, token com validade)
- Aparelhos salvos no servidor, com status real
- Controle por ADB: teclas, abrir apps, digitar texto
- Ligar aparelhos pela rede (Wake-on-LAN)
- Cenas (vários passos num clique) e horários
- Histórico de tudo que foi feito
- Servidor de arquivos (pasta compartilhada)
- Assistente de IA que controla os aparelhos
- Só aceita conexões da rede local (rede de casa ou o próprio PC)

Como rodar:  python servidor.py
Na primeira vez ele pede para você criar usuário e senha.

Os dados ficam FORA do OneDrive, em C:\\Users\\<você>\\CasaConectada
"""
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request

# No Python portátil do Windows 7 o codec "idna" (usado para abrir a porta e
# acessar endereços) às vezes não carrega. Nossos endereços são só números
# (192.168.0.X), então usar o ASCII no lugar dele dá o mesmo resultado.
try:
    import encodings.idna  # noqa: F401
    "localhost".encode("idna")
except Exception as _erro_idna:  # noqa: BLE001
    import codecs

    def _idna_reserva(nome):
        return codecs.lookup("ascii") if nome == "idna" else None

    codecs.register(_idna_reserva)
    print(f"(aviso: codec idna indisponível, usando ASCII: {_erro_idna})")
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

PASTA = Path(__file__).resolve().parent
PASTA_SITE = PASTA.parent / "site-online"

# Dados sensíveis e arquivos grandes ficam fora da pasta do projeto (e do OneDrive)
PASTA_DADOS = Path(os.environ.get("CASA_DADOS", str(Path.home() / "CasaConectada")))
PASTA_COMPARTILHADA = PASTA_DADOS / "compartilhado"
ARQ_USUARIOS = PASTA_DADOS / "usuarios.json"
ARQ_APAGADOS = PASTA_DADOS / "usuarios_apagados.json"  # quem foi apagado e quando (para sincronizar)
ARQ_DISPOSITIVOS = PASTA_DADOS / "dispositivos.json"
ARQ_CENAS = PASTA_DADOS / "cenas.json"
ARQ_AGENDAS = PASTA_DADOS / "agendas.json"
ARQ_APAGADOS_LISTAS = PASTA_DADOS / "apagados_listas.json"  # aparelhos/cenas/horários apagados (sincronizar)
ARQ_NOTIFICACOES = PASTA_DADOS / "notificacoes.json"
ARQUIVOS_SINCRONIZADOS = {ARQ_DISPOSITIVOS, ARQ_CENAS, ARQ_AGENDAS, ARQ_NOTIFICACOES}
ARQ_HISTORICO = PASTA_DADOS / "historico.jsonl"
ARQ_SEGREDO = PASTA_DADOS / "segredo.key"
ARQ_CHAVE_IA = PASTA_DADOS / "chave_anthropic.txt"
PASTA_CAPAS = PASTA_DADOS / "capas"  # fotos de capa dos aparelhos

# Caminho do ADB (pode trocar pela variável de ambiente ADB_PATH)
# Procura o adb.exe a partir da pasta deste arquivo (python\tvdetox\...),
# assim continua funcionando mesmo se a pasta "python" mudar de lugar.
ADB = os.environ.get(
    "ADB_PATH",
    str(
        Path(__file__).resolve().parents[2]
        / "tvdetox" / "platform-tools-latest-windows" / "platform-tools" / "adb.exe"
    ),
)
PORTA = int(os.environ.get("PORTA", "5000"))
VALIDADE_TOKEN = 12 * 3600  # o login vale 12 horas
LIMITE_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GB por arquivo enviado
MODELO_IA = os.environ.get("IA_MODELO", "claude-sonnet-5-5")

TECLAS = {
    "BACK": 4,
    "HOME": 3,
    "DPAD_UP": 19,
    "DPAD_DOWN": 20,
    "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "POWER": 26,
    "MUTE": 164,
    "MEDIA_PLAY_PAUSE": 85,
    "MEDIA_REWIND": 89,
    "MEDIA_FAST_FORWARD": 90,
    "MEDIA_NEXT": 87,
    "MEDIA_PREVIOUS": 88,
    "MENU": 82,
    "SETTINGS": 176,
    "TV_INPUT": 178,
    "CHANNEL_UP": 166,
    "CHANNEL_DOWN": 167,
    "GUIDE": 172,
    "INFO": 165,
    "CAPTIONS": 175,
    "SEARCH": 84,
    "APP_SWITCH": 187,
    "DEL": 67,
    "WAKEUP": 224,
    "SLEEP": 223,
    **{f"NUM_{n}": 7 + n for n in range(10)},
}
TIPOS = {"tv", "notebook", "celular", "outro"}
TIPOS_PASSO = {"comando", "app", "texto", "mensagem", "esperar", "acordar"}
PAPEIS = {"admin", "usuario"}
NOME_USUARIO_RE = re.compile(r"^[a-z0-9_.-]{3,20}$")
LIMITE_CAPA = 5 * 1024 * 1024  # 5 MB por foto de capa
TIPOS_IMAGEM = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# Apps conhecidos (o resto aparece com o nome do pacote)
APPS_CONHECIDOS = {
    "com.netflix.ninja": "Netflix",
    "com.google.android.youtube.tv": "YouTube",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.disney.disneyplus": "Disney+",
    "com.wbd.stream": "Max",
    "com.spotify.tv.android": "Spotify",
    "com.plexapp.android": "Plex",
    "tv.twitch.android.app": "Twitch",
    "com.apple.atve.androidtv.appletv": "Apple TV",
}

PACOTE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PORTAS_STATUS = {
    "tv": [5555],
    "celular": [5555],
    "notebook": [445, 135, 3389, 22],
    "outro": [5555, 80, 443, 22, 445],
}

trava_arquivos = threading.RLock()
trava_mapa_adb = threading.Lock()
travas_adb = {}  # uma trava por aparelho: uma TV lenta não trava as outras
shells_adb = {}  # sessões "adb shell" abertas, para as teclas saírem na hora
trava_historico = threading.Lock()
conexoes = set()
tentativas_login = {}  # ip -> horários das tentativas que falharam
uso_chat = {}  # usuário -> horários das mensagens enviadas à IA
cache_apps = {}  # id do aparelho -> (quando, lista)
identificados = set()  # aparelhos dos quais já lemos MAC/serial nesta execução
ultima_busca = {}  # ip -> o que a última busca achou nele (serial, MAC, modelo)
MAC_FALSO = "02:00:00:00:00:00"
cenas_rodando = set()

TEXTO_CHAVE = (
    "# Cole sua chave de API da Anthropic na linha de baixo (sem aspas).\n"
    "# Crie a chave em console.anthropic.com > API Keys.\n"
    "# Linhas que começam com # são ignoradas. Não compartilhe este arquivo.\n"
)


# ---------- Arquivos de dados ----------
def ler_json(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def _carimbar_lista(caminho, dados):
    """Marca a hora em cada aparelho/cena/horário que mudou e guarda quem foi apagado (para sincronizar)."""
    antigos = {d.get("id"): d for d in ler_json(caminho, []) if isinstance(d, dict)}
    agora = time.time()
    ids = set()
    for d in dados:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        ids.add(d["id"])
        velho = antigos.get(d["id"])
        if velho is None or _sem_carimbo(velho) != _sem_carimbo(d):
            d["atualizado"] = agora
        elif "atualizado" in velho:
            d["atualizado"] = velho["atualizado"]
    removidos = set(antigos) - ids - {None}
    if removidos:
        todos = ler_json(ARQ_APAGADOS_LISTAS, {})
        todos.setdefault(caminho.stem, {}).update({i: agora for i in removidos})
        todos[caminho.stem] = {i: t for i, t in todos[caminho.stem].items() if agora - float(t or 0) < 30 * 86400}
        salvar_json(ARQ_APAGADOS_LISTAS, todos, carimbar=False)


def salvar_json(caminho, dados, carimbar=True):
    sincroniza = carimbar and caminho in ARQUIVOS_SINCRONIZADOS
    if sincroniza:
        _carimbar_lista(caminho, dados)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporario, caminho)
    if sincroniza:
        avisar_mudanca()


def migrar_arquivos_antigos():
    """Versão anterior guardava senha e lista dentro da pasta do projeto (OneDrive).
    Move para a pasta de dados, mantendo o seu login."""
    for nome in ("usuarios.json", "segredo.key", "dispositivos.json"):
        velho, novo = PASTA / nome, PASTA_DADOS / nome
        if velho.exists() and not novo.exists():
            shutil.move(str(velho), str(novo))
            print(f"Movi {nome} para {PASTA_DADOS} (fora do OneDrive).")


def preparar_pastas():
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    migrar_arquivos_antigos()
    PASTA_COMPARTILHADA.mkdir(exist_ok=True)
    PASTA_CAPAS.mkdir(exist_ok=True)
    if not ARQ_SEGREDO.exists():
        ARQ_SEGREDO.write_text(secrets.token_hex(32), encoding="utf-8")
    if not ARQ_CHAVE_IA.exists():
        ARQ_CHAVE_IA.write_text(TEXTO_CHAVE, encoding="utf-8")


def ler_chave_ia():
    """Chave da IA: variável de ambiente ANTHROPIC_API_KEY ou o arquivo chave_anthropic.txt."""
    chave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if chave:
        return chave
    try:
        for linha in ARQ_CHAVE_IA.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#"):
                return linha
    except OSError:
        pass
    return ""


def ler_usuarios():
    """{usuario: {"hash": ..., "papel": "admin" | "usuario"}}.
    No formato antigo (só o hash) o usuário vira administrador."""
    with trava_arquivos:
        brutos = ler_json(ARQ_USUARIOS, {})
    usuarios = {}
    for nome, valor in brutos.items():
        if isinstance(valor, str):
            usuarios[nome] = {"hash": valor, "papel": "admin"}
        elif isinstance(valor, dict) and valor.get("hash"):
            papel = valor.get("papel") if valor.get("papel") in PAPEIS else "usuario"
            usuarios[nome] = {"hash": valor["hash"], "papel": papel}
            for extra in ("email", "telefone", "nome", "foto", "foto_v", "pendente", "atualizado"):
                if valor.get(extra):
                    usuarios[nome][extra] = valor[extra]
    return usuarios


def _sem_carimbo(info):
    return {k: v for k, v in info.items() if k != "atualizado"} if isinstance(info, dict) else info


def salvar_usuarios(usuarios):
    """Salva e marca a hora em quem mudou (e em quem foi apagado), para os outros servidores saberem."""
    with trava_arquivos:
        antigos = ler_json(ARQ_USUARIOS, {})
        agora = time.time()
        for nome, info in usuarios.items():
            velho = antigos.get(nome)
            if velho is None or _sem_carimbo(velho) != _sem_carimbo(info):
                info["atualizado"] = agora
            elif isinstance(velho, dict) and "atualizado" in velho:
                info["atualizado"] = velho["atualizado"]
        removidos = set(antigos) - set(usuarios)
        if removidos:
            apagados = ler_json(ARQ_APAGADOS, {})
            apagados.update({n: agora for n in removidos})
            salvar_json(ARQ_APAGADOS, apagados)
        salvar_json(ARQ_USUARIOS, usuarios)
    avisar_mudanca()


def avisar_mudanca():
    evento = globals().get("aviso_sync")
    if evento is not None:
        evento.set()  # o servidor que recebe sincroniza na hora


def primeiro_acesso():
    """Na primeira vez, pede usuário e senha no terminal."""
    if ARQ_USUARIOS.exists():
        return
    print("\n=== Primeiro acesso: crie o login do site ===")
    usuario = ""
    while not usuario:
        usuario = input("Usuário: ").strip().lower()
    while True:
        senha = getpass.getpass("Senha (mínimo 8 caracteres): ")
        if len(senha) < 8:
            print("Senha curta demais, tente de novo.")
            continue
        if senha != getpass.getpass("Repita a senha: "):
            print("As senhas não batem, tente de novo.")
            continue
        break
    salvar_json(ARQ_USUARIOS, {usuario: {"hash": generate_password_hash(senha), "papel": "admin"}})
    print("Login criado!\n")


preparar_pastas()
primeiro_acesso()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024  # envio de arquivos tem limite próprio
_segredo = ARQ_SEGREDO.read_text(encoding="utf-8").strip()
serializador = URLSafeTimedSerializer(_segredo, salt="casa-conectada")
serializador_download = URLSafeTimedSerializer(_segredo, salt="casa-download")
HASH_FALSO = generate_password_hash(secrets.token_hex(8))


# ---------- Segurança ----------
REDES_LOCAIS = [
    ipaddress.ip_network(rede)
    for rede in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "::1/128",
        "fc00::/7",
    )
]


def e_rede_local(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in rede for rede in REDES_LOCAIS if rede.version == ip.version)


@app.before_request
def so_rede_local():
    try:
        ip = ipaddress.ip_address(request.remote_addr)
    except ValueError:
        return jsonify({"erro": "acesso negado"}), 403
    if not e_rede_local(ip):
        return jsonify({"erro": "acesso negado"}), 403


@app.after_request
def cabecalhos(resposta):
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Frame-Options"] = "DENY"
    resposta.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


def exige_login(funcao):
    @wraps(funcao)
    def interna(*args, **kwargs):
        cabecalho = request.headers.get("Authorization", "")
        if not cabecalho.startswith("Bearer "):
            return jsonify({"erro": "não autenticado"}), 401
        try:
            dados = serializador.loads(cabecalho[7:], max_age=VALIDADE_TOKEN)
        except (BadSignature, SignatureExpired):
            return jsonify({"erro": "sessão expirada, entre de novo"}), 401
        request.usuario = dados.get("u", "")
        info = ler_usuarios().get(request.usuario)
        if info is None or info.get("pendente"):  # usuário apagado: o login antigo deixa de valer
            return jsonify({"erro": "sessão expirada, entre de novo"}), 401
        request.papel = info["papel"]
        return funcao(*args, **kwargs)

    return interna


def exige_admin(funcao):
    """Só o administrador pode: cadastrar/editar aparelhos, cenas, horários e usuários."""
    @wraps(funcao)
    def checa_admin(*args, **kwargs):
        if getattr(request, "papel", "") != "admin":
            return jsonify({"erro": "Só o administrador pode fazer isso."}), 403
        return funcao(*args, **kwargs)

    return exige_login(checa_admin)


def bloqueado_por_tentativas(ip):
    agora = time.time()
    recentes = [t for t in tentativas_login.get(ip, []) if agora - t < 300]
    tentativas_login[ip] = recentes
    return len(recentes) >= 5


# ---------- Histórico ----------
def registrar(origem, usuario, acao, ok, erro=""):
    """Anota o que foi feito (origem: site, ia, cena, agenda)."""
    linha = json.dumps(
        {
            "t": int(time.time()),
            "origem": origem,
            "usuario": usuario,
            "acao": acao,
            "ok": bool(ok),
            "erro": erro,
        },
        ensure_ascii=False,
    )
    with trava_historico:
        with open(ARQ_HISTORICO, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
        if ARQ_HISTORICO.stat().st_size > 1_000_000:  # mantém só as últimas 500
            linhas = ARQ_HISTORICO.read_text(encoding="utf-8").splitlines()[-500:]
            ARQ_HISTORICO.write_text("\n".join(linhas) + "\n", encoding="utf-8")


@app.route("/api/historico")
@exige_login
def historico():
    try:
        limite = max(1, min(300, int(request.args.get("limite", 100))))
    except ValueError:
        limite = 100
    with trava_historico:
        try:
            linhas = ARQ_HISTORICO.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            linhas = []
    itens = []
    for linha in reversed(linhas[-limite:]):
        try:
            itens.append(json.loads(linha))
        except json.JSONDecodeError:
            continue
    return jsonify({"itens": itens})


# ---------- Site ----------
@app.route("/")
def inicio():
    return send_from_directory(PASTA_SITE, "index.html")


def _rota_arquivo_site(nome, tipo):
    app.add_url_rule(
        "/" + nome,
        endpoint="site_" + nome,
        view_func=lambda: send_from_directory(PASTA_SITE, nome, mimetype=tipo),
    )


for _nome, _tipo in (
    ("manifest.webmanifest", "application/manifest+json"),
    ("icone-192.png", "image/png"),
    ("icone-512.png", "image/png"),
):
    _rota_arquivo_site(_nome, _tipo)


@app.route("/status")
def status():
    return jsonify({"ok": True, "endereco": f"http://{ip_da_rede()}:{PORTA}"})


# ---------- Login ----------
@app.route("/api/login", methods=["POST"])
def login():
    ip = request.remote_addr
    if bloqueado_por_tentativas(ip):
        return jsonify({"erro": "Muitas tentativas. Espere 5 minutos."}), 429

    dados = request.get_json(silent=True) or {}
    usuario = str(dados.get("usuario", "")).strip().lower()
    senha = str(dados.get("senha", ""))

    info = ler_usuarios().get(usuario)
    hash_salvo = info["hash"] if info else None
    # Confere sempre uma senha, mesmo se o usuário não existe (evita pistas)
    senha_ok = check_password_hash(hash_salvo or HASH_FALSO, senha)

    if not hash_salvo or not senha_ok:
        tentativas_login.setdefault(ip, []).append(time.time())
        if len([t for t in tentativas_login[ip] if time.time() - t < 300]) == 5:
            notificar(f"5 tentativas erradas de entrar no site (aparelho {ip}, usuário \"{usuario[:20]}\"). "
                      "Ele ficou bloqueado por 5 minutos.", "seguranca", "admin", "historico")
        return jsonify({"erro": "Usuário ou senha errados."}), 401

    tentativas_login.pop(ip, None)
    if info.get("pendente"):
        return jsonify({"erro": "Sua conta está esperando o administrador aprovar."}), 403
    token = serializador.dumps({"u": usuario})
    return jsonify({"token": token, "usuario": usuario, "papel": info["papel"],
                    "nome": info.get("nome") or usuario, "foto_v": info.get("foto_v", 0)})


@app.route("/api/eu")
@exige_login
def eu():
    info = ler_usuarios().get(request.usuario, {})
    return jsonify({"usuario": request.usuario, "papel": request.papel,
                    "nome": info.get("nome") or request.usuario, "foto_v": info.get("foto_v", 0)})


# ---------- Usuários (só o administrador mexe) ----------
@app.route("/api/usuarios")
@exige_admin
def listar_usuarios():
    usuarios = ler_usuarios()
    return jsonify({"usuarios": [
        {"usuario": u, "papel": i["papel"], "email": i.get("email", ""), "telefone": i.get("telefone", ""),
         "nome": i.get("nome", ""), "foto_v": i.get("foto_v", 0), "pendente": bool(i.get("pendente"))}
        for u, i in sorted(usuarios.items(), key=lambda par: (not par[1].get("pendente"), par[0]))
    ]})


@app.route("/api/usuarios", methods=["POST"])
@exige_admin
def criar_usuario():
    dados = request.get_json(silent=True) or {}
    nome = str(dados.get("usuario", "")).strip().lower()
    senha = str(dados.get("senha", ""))
    papel = dados.get("papel") if dados.get("papel") in PAPEIS else "usuario"
    if not NOME_USUARIO_RE.match(nome):
        return jsonify({"erro": "Usuário inválido: 3 a 20 letras minúsculas, números, . _ -"}), 400
    if len(senha) < 8:
        return jsonify({"erro": "A senha precisa ter pelo menos 8 caracteres."}), 400
    contato, erro = limpar_contato(dados)
    if erro:
        return jsonify({"erro": erro}), 400
    with trava_arquivos:
        usuarios = ler_usuarios()
        if nome in usuarios:
            return jsonify({"erro": "Esse usuário já existe."}), 409
        usuarios[nome] = {"hash": generate_password_hash(senha), "papel": papel}
        usuarios[nome].update({k: v for k, v in contato.items() if v})
        if limpar_nome_exibicao(dados.get("nome")):
            usuarios[nome]["nome"] = limpar_nome_exibicao(dados.get("nome"))
        salvar_usuarios(usuarios)
    registrar("site", request.usuario, f"Criou o usuário {nome} ({papel})", True)
    return jsonify({"usuario": nome, "papel": papel}), 201


@app.route("/api/usuarios/<nome>", methods=["DELETE"])
@exige_admin
def apagar_usuario(nome):
    if nome == request.usuario:
        return jsonify({"erro": "Você não pode apagar o próprio usuário."}), 400
    with trava_arquivos:
        usuarios = ler_usuarios()
        if nome not in usuarios:
            return jsonify({"erro": "usuário não encontrado"}), 404
        apagar_foto(usuarios[nome])
        del usuarios[nome]
        salvar_usuarios(usuarios)
    registrar("site", request.usuario, f"Apagou o usuário {nome}", True)
    return jsonify({"ok": True})


@app.route("/api/usuarios/<nome>", methods=["PUT"])
@exige_admin
def editar_usuario(nome):
    """Troca a senha, o papel, o e-mail e/ou o telefone de um usuário."""
    dados = request.get_json(silent=True) or {}
    contato, erro = limpar_contato(dados)
    if erro:
        return jsonify({"erro": erro}), 400
    with trava_arquivos:
        usuarios = ler_usuarios()
        if nome not in usuarios:
            return jsonify({"erro": "usuário não encontrado"}), 404
        if "papel" in dados:
            papel = dados.get("papel")
            if papel not in PAPEIS:
                return jsonify({"erro": "papel inválido"}), 400
            if nome == request.usuario and papel != "admin":
                return jsonify({"erro": "Você não pode tirar o seu próprio acesso de administrador."}), 400
            usuarios[nome]["papel"] = papel
        if dados.get("senha"):
            senha = str(dados["senha"])
            if len(senha) < 8:
                return jsonify({"erro": "A senha precisa ter pelo menos 8 caracteres."}), 400
            usuarios[nome]["hash"] = generate_password_hash(senha)
        usuarios[nome].update(contato)
        if "nome" in dados:
            exibicao = limpar_nome_exibicao(dados.get("nome"))
            if exibicao is None:
                return jsonify({"erro": "Nome inválido (até 40 letras)."}), 400
            usuarios[nome]["nome"] = exibicao
        aprovou = bool(dados.get("aprovar")) and usuarios[nome].pop("pendente", None)
        salvar_usuarios(usuarios)
    if aprovou:
        notificar("Bem-vindo(a)! Sua conta foi aprovada. Coloque sua foto em Minha conta.", "conta", nome, "conta")
    registrar("site", request.usuario, f"{'Aprovou' if aprovou else 'Alterou'} o usuário {nome}", True)
    return jsonify({"ok": True})


# ---------- E-mail: códigos para recuperar a senha ----------
import smtplib  # noqa: E402
import ssl  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402
from email.message import EmailMessage  # noqa: E402

ARQ_EMAIL = PASTA_DADOS / "email.json"  # Gmail e senha de app que o site usa para enviar
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
TELEFONE_RE = re.compile(r"^[0-9()+\-\s]{8,20}$")
VALIDADE_CODIGO = 10 * 60  # o código vale 10 minutos
codigos_senha = {}  # usuario -> {"hash", "expira", "tentativas", "enviado"}


def limpar_contato(dados):
    """Confere e-mail e telefone vindos do site. Devolve (campos, erro)."""
    campos = {}
    if "email" in dados:
        email = str(dados.get("email") or "").strip().lower()
        if email and not EMAIL_RE.match(email):
            return None, "E-mail inválido. Exemplo: nome@gmail.com"
        campos["email"] = email
    if "telefone" in dados:
        telefone = str(dados.get("telefone") or "").strip()
        if telefone and not TELEFONE_RE.match(telefone):
            return None, "Telefone inválido. Exemplo: (83) 99999-9999"
        campos["telefone"] = telefone
    return campos, ""


def mascarar_email(email):
    nome, _, dominio = email.partition("@")
    return (nome[:2] + "***" if len(nome) > 2 else nome[:1] + "***") + "@" + dominio


def config_email():
    cfg = ler_json(ARQ_EMAIL, {})
    return str(cfg.get("gmail", "")).strip(), str(cfg.get("senha_app", "")).replace(" ", "").strip()


def enviar_email(para, assunto, texto):
    gmail, senha = config_email()
    if not gmail or not senha:
        raise RuntimeError("O envio de e-mail ainda não foi configurado (aba Usuários).")
    msg = EmailMessage()
    msg["From"] = f"Casa Conectada <{gmail}>"
    msg["To"] = para
    msg["Subject"] = assunto
    msg.set_content(texto)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as s:
            s.login(gmail, senha)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("O Gmail recusou o login: confira o e-mail e a senha de app (16 letras).")
    except (OSError, smtplib.SMTPException) as erro:
        raise RuntimeError(f"Não consegui enviar o e-mail: {erro}")


def hash_codigo(usuario, codigo):
    return _hmac.new(_segredo.encode(), f"codigo-senha:{usuario}:{codigo}".encode(), _hashlib.sha256).hexdigest()


@app.route("/api/email/config")
@exige_admin
def ver_config_email():
    gmail, senha = config_email()
    return jsonify({"gmail": gmail, "configurado": bool(gmail and senha)})


@app.route("/api/email/config", methods=["POST"])
@exige_admin
def salvar_config_email():
    dados = request.get_json(silent=True) or {}
    gmail = str(dados.get("gmail", "")).strip().lower()
    senha = str(dados.get("senha_app", "")).replace(" ", "").strip()
    if not EMAIL_RE.match(gmail):
        return jsonify({"erro": "Coloque o Gmail que vai enviar os códigos."}), 400
    atual_gmail, atual_senha = config_email()
    if not senha:
        senha = atual_senha if atual_gmail == gmail else ""
    if len(senha) != 16 or not senha.isalpha():
        return jsonify({"erro": "A senha de app do Google tem 16 letras (pode colar com os espaços)."}), 400
    salvar_json(ARQ_EMAIL, {"gmail": gmail, "senha_app": senha, "atualizado": time.time()})
    avisar_mudanca()
    registrar("site", request.usuario, "Configurou o e-mail do site", True)
    return jsonify({"ok": True})


@app.route("/api/email/teste", methods=["POST"])
@exige_admin
def testar_email():
    gmail, _ = config_email()
    para = ler_usuarios().get(request.usuario, {}).get("email") or gmail
    try:
        enviar_email(para, "Casa Conectada: teste de e-mail",
                     "Se você recebeu esta mensagem, o envio de códigos do Casa Conectada está funcionando.")
    except RuntimeError as erro:
        return jsonify({"erro": str(erro)}), 502
    return jsonify({"ok": True, "para": para})


@app.route("/api/senha/esqueci", methods=["POST"])
def senha_esqueci():
    ip = request.remote_addr
    if bloqueado_por_tentativas(ip):
        return jsonify({"erro": "Muitas tentativas. Espere 5 minutos."}), 429
    digitado = str((request.get_json(silent=True) or {}).get("usuario", "")).strip().lower()
    usuarios = ler_usuarios()
    usuario = digitado
    if "@" in digitado:  # aceita o e-mail no lugar do usuário
        donos = [u for u, i in usuarios.items() if i.get("email", "").lower() == digitado]
        if len(donos) > 1:
            return jsonify({"erro": "Esse e-mail está em mais de uma conta. Digite o usuário."}), 400
        usuario = donos[0] if donos else ""
    info = usuarios.get(usuario)
    if not info:
        tentativas_login.setdefault(ip, []).append(time.time())
        return jsonify({"erro": "Não achei nenhuma conta com esse usuário ou e-mail."}), 404
    if not info.get("email"):
        return jsonify({"erro": "Esse usuário não tem e-mail cadastrado. Peça ao administrador para trocar a senha."}), 400
    anterior = codigos_senha.get(usuario)
    if anterior and time.time() - anterior["enviado"] < 60:
        return jsonify({"erro": "Já mandei um código agora há pouco. Espere 1 minuto para pedir outro."}), 429
    codigo = f"{secrets.randbelow(1000000):06d}"
    try:
        enviar_email(info["email"], f"Casa Conectada: seu código é {codigo}",
                     f"Olá, {usuario}!\n\nSeu código para criar uma senha nova no Casa Conectada é:\n\n"
                     f"    {codigo}\n\nEle vale por 10 minutos. Se não foi você que pediu, ignore este e-mail.")
    except RuntimeError as erro:
        return jsonify({"erro": str(erro)}), 502
    codigos_senha[usuario] = {"hash": hash_codigo(usuario, codigo), "expira": time.time() + VALIDADE_CODIGO,
                              "tentativas": 0, "enviado": time.time()}
    registrar("site", usuario, "Pediu código para trocar a senha", True)
    return jsonify({"ok": True, "para": mascarar_email(info["email"]), "usuario": usuario})


@app.route("/api/senha/redefinir", methods=["POST"])
def senha_redefinir():
    dados = request.get_json(silent=True) or {}
    usuario = str(dados.get("usuario", "")).strip().lower()
    codigo = str(dados.get("codigo", "")).strip()
    nova = str(dados.get("nova", ""))
    pedido = codigos_senha.get(usuario)
    if not pedido or time.time() > pedido["expira"]:
        codigos_senha.pop(usuario, None)
        return jsonify({"erro": "Código vencido ou não pedido. Peça um código novo."}), 400
    if pedido["tentativas"] >= 5:
        codigos_senha.pop(usuario, None)
        return jsonify({"erro": "Errou o código muitas vezes. Peça um código novo."}), 429
    if not _hmac.compare_digest(pedido["hash"], hash_codigo(usuario, codigo)):
        pedido["tentativas"] += 1
        return jsonify({"erro": f"Código errado. Restam {5 - pedido['tentativas']} tentativas."}), 400
    if len(nova) < 8:
        return jsonify({"erro": "A senha nova precisa ter pelo menos 8 caracteres."}), 400
    with trava_arquivos:
        usuarios = ler_usuarios()
        if usuario not in usuarios:
            return jsonify({"erro": "Usuário não encontrado."}), 404
        usuarios[usuario]["hash"] = generate_password_hash(nova)
        salvar_usuarios(usuarios)
    codigos_senha.pop(usuario, None)
    registrar("site", usuario, "Trocou a senha pelo código do e-mail", True)
    notificar("Sua senha foi trocada pelo código do e-mail. Se não foi você, avise o administrador.",
              "seguranca", usuario, "conta")
    return jsonify({"ok": True})


@app.route("/api/eu/conta")
@exige_login
def minha_conta():
    info = ler_usuarios().get(request.usuario, {})
    return jsonify({"usuario": request.usuario, "email": info.get("email", ""), "telefone": info.get("telefone", ""),
                    "nome": info.get("nome", ""), "foto_v": info.get("foto_v", 0)})


@app.route("/api/eu/conta", methods=["POST"])
@exige_login
def salvar_minha_conta():
    dados = request.get_json(silent=True) or {}
    campos, erro = limpar_contato(dados)
    if erro:
        return jsonify({"erro": erro}), 400
    if "nome" in dados:
        exibicao = limpar_nome_exibicao(dados.get("nome"))
        if exibicao is None:
            return jsonify({"erro": "Coloque como você quer ser chamado (até 40 letras)."}), 400
        campos["nome"] = exibicao
    with trava_arquivos:
        usuarios = ler_usuarios()
        usuarios[request.usuario].update(campos)
        salvar_usuarios(usuarios)
    return jsonify({"ok": True})


@app.route("/api/eu/senha", methods=["POST"])
@exige_login
def trocar_minha_senha():
    dados = request.get_json(silent=True) or {}
    ip = request.remote_addr
    if bloqueado_por_tentativas(ip):
        return jsonify({"erro": "Muitas tentativas. Espere 5 minutos."}), 429
    with trava_arquivos:
        usuarios = ler_usuarios()
        info = usuarios.get(request.usuario)
        if not info or not check_password_hash(info["hash"], str(dados.get("atual", ""))):
            tentativas_login.setdefault(ip, []).append(time.time())
            return jsonify({"erro": "A senha atual está errada."}), 400
        nova = str(dados.get("nova", ""))
        if len(nova) < 8:
            return jsonify({"erro": "A senha nova precisa ter pelo menos 8 caracteres."}), 400
        info["hash"] = generate_password_hash(nova)
        salvar_usuarios(usuarios)
    registrar("site", request.usuario, "Trocou a própria senha", True)
    return jsonify({"ok": True})


# ---------- Perfil (foto e nome) e cadastro pelo site ----------
PASTA_FOTOS = PASTA_DADOS / "fotos"  # fotos de perfil dos usuários
LIMITE_FOTO = 5 * 1024 * 1024
MAX_PENDENTES = 20
cadastros_por_ip = {}  # ip -> horários dos cadastros feitos (evita enxurrada)


def limpar_nome_exibicao(texto):
    nome = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not nome or len(nome) > 40:
        return None
    return nome


def apagar_foto(info):
    if info.get("foto"):
        try:
            (PASTA_FOTOS / Path(info["foto"]).name).unlink()
        except OSError:
            pass


@app.route("/api/eu/foto", methods=["POST"])
@exige_login
def enviar_minha_foto():
    request.max_content_length = LIMITE_FOTO
    arquivo = request.files.get("foto")
    if arquivo is None:
        return jsonify({"erro": "Escolha uma imagem."}), 400
    cabeca = arquivo.stream.read(12)
    arquivo.stream.seek(0)
    extensao = tipo_da_imagem(cabeca)
    if extensao is None:
        return jsonify({"erro": "Use uma imagem JPG, PNG ou WEBP."}), 400
    PASTA_FOTOS.mkdir(parents=True, exist_ok=True)
    with trava_arquivos:
        usuarios = ler_usuarios()
        info = usuarios[request.usuario]
        apagar_foto(info)
        nome_arquivo = request.usuario + extensao
        arquivo.save(PASTA_FOTOS / nome_arquivo)
        info["foto"] = nome_arquivo
        info["foto_v"] = int(time.time())
        salvar_usuarios(usuarios)
    return jsonify({"ok": True, "foto_v": info["foto_v"]})


@app.route("/api/eu/foto", methods=["DELETE"])
@exige_login
def apagar_minha_foto():
    with trava_arquivos:
        usuarios = ler_usuarios()
        info = usuarios[request.usuario]
        apagar_foto(info)
        info.pop("foto", None)
        info.pop("foto_v", None)
        salvar_usuarios(usuarios)
    return jsonify({"ok": True})


@app.route("/api/usuarios/<nome>/foto")
@exige_login
def ver_foto_usuario(nome):
    info = ler_usuarios().get(nome)
    if not info or not info.get("foto"):
        return jsonify({"erro": "sem foto"}), 404
    caminho = PASTA_FOTOS / Path(info["foto"]).name
    if not caminho.is_file() or caminho.suffix not in TIPOS_IMAGEM:
        return jsonify({"erro": "sem foto"}), 404
    return send_file(caminho, mimetype=TIPOS_IMAGEM[caminho.suffix], max_age=0)


@app.route("/api/cadastro", methods=["POST"])
def cadastro():
    """Qualquer um da rede de casa pode pedir uma conta; ela só funciona depois que o administrador aprovar."""
    ip = request.remote_addr
    agora = time.time()
    recentes = [t for t in cadastros_por_ip.get(ip, []) if agora - t < 3600]
    if len(recentes) >= 3:
        return jsonify({"erro": "Muitos cadastros feitos deste aparelho. Tente de novo mais tarde."}), 429
    dados = request.get_json(silent=True) or {}
    usuario = str(dados.get("usuario", "")).strip().lower()
    nome = limpar_nome_exibicao(dados.get("nome"))
    senha = str(dados.get("senha", ""))
    if not NOME_USUARIO_RE.match(usuario):
        return jsonify({"erro": "Usuário inválido: 3 a 20 letras minúsculas, números, . _ - (sem espaço e sem acento)."}), 400
    if not nome:
        return jsonify({"erro": "Coloque como você quer ser chamado (até 40 letras)."}), 400
    if len(senha) < 8:
        return jsonify({"erro": "A senha precisa ter pelo menos 8 caracteres."}), 400
    contato, erro = limpar_contato({"email": dados.get("email", "")})
    if erro:
        return jsonify({"erro": erro}), 400
    if not contato.get("email"):
        return jsonify({"erro": "Coloque seu e-mail (serve para recuperar a senha)."}), 400
    with trava_arquivos:
        usuarios = ler_usuarios()
        if usuario in usuarios:
            return jsonify({"erro": "Esse usuário já existe. Escolha outro (ex: " + usuario[:17] + "2)."}), 409
        if sum(1 for i in usuarios.values() if i.get("pendente")) >= MAX_PENDENTES:
            return jsonify({"erro": "Muitas contas esperando aprovação. Fale com o administrador."}), 429
        usuarios[usuario] = {"hash": generate_password_hash(senha), "papel": "usuario", "nome": nome,
                             "email": contato["email"], "pendente": True}
        salvar_usuarios(usuarios)
    cadastros_por_ip.setdefault(ip, recentes).append(agora)
    registrar("site", usuario, f"Pediu uma conta ({nome})", True)
    notificar(f"{nome} (@{usuario}) pediu uma conta. Toque para aprovar.", "conta", "admin", "usuarios")
    return jsonify({"ok": True}), 201


# ---------- Notificações (o sininho do site) ----------
MAX_NOTIFICACOES = 150


def notificar(texto, tipo="info", para="admin", link=""):
    """Cria um aviso. para: "admin" (todos os administradores), "todos" ou o nome de um usuário."""
    try:
        with trava_arquivos:
            lista = [n for n in ler_json(ARQ_NOTIFICACOES, []) if isinstance(n, dict)]
            lista.append({"id": secrets.token_hex(5), "t": time.time(), "tipo": tipo, "texto": str(texto)[:220],
                          "para": para, "link": link, "lidos": []})
            salvar_json(ARQ_NOTIFICACOES, lista[-MAX_NOTIFICACOES:])
    except Exception as erro:  # aviso nunca derruba nada
        print("Não consegui criar a notificação:", erro)


def notificacao_visivel(n, usuario, papel):
    para = n.get("para", "admin")
    return para == "todos" or para == usuario or (para == "admin" and papel == "admin")


@app.route("/api/notificacoes")
@exige_login
def listar_notificacoes():
    itens = [n for n in ler_json(ARQ_NOTIFICACOES, []) if isinstance(n, dict)
             and notificacao_visivel(n, request.usuario, request.papel)]
    itens.sort(key=lambda n: n.get("t", 0), reverse=True)
    saida = [{"id": n["id"], "t": n.get("t", 0), "tipo": n.get("tipo", "info"), "texto": n.get("texto", ""),
              "link": n.get("link", ""), "lida": request.usuario in n.get("lidos", [])} for n in itens[:60]]
    return jsonify({"itens": saida, "nao_lidas": sum(1 for n in saida if not n["lida"])})


@app.route("/api/notificacoes/lidas", methods=["POST"])
@exige_login
def marcar_notificacoes():
    ids = (request.get_json(silent=True) or {}).get("ids")
    with trava_arquivos:
        lista = [n for n in ler_json(ARQ_NOTIFICACOES, []) if isinstance(n, dict)]
        mudou = False
        for n in lista:
            if (ids == "todas" or (isinstance(ids, list) and n.get("id") in ids)) \
                    and notificacao_visivel(n, request.usuario, request.papel) \
                    and request.usuario not in n.get("lidos", []):
                n["lidos"] = list(n.get("lidos", [])) + [request.usuario]
                mudou = True
        if mudou:
            salvar_json(ARQ_NOTIFICACOES, lista)
    return jsonify({"ok": True})


# ---------- Enviar arquivos pra alguém (tipo LocalSend) ----------
@app.route("/api/usuarios/nomes")
@exige_login
def nomes_dos_usuarios():
    """Lista simples pra escolher pra quem mandar (qualquer usuário logado vê)."""
    return jsonify({"usuarios": [
        {"usuario": u, "nome": i.get("nome") or u, "foto_v": i.get("foto_v", 0)}
        for u, i in sorted(ler_usuarios().items()) if not i.get("pendente")
    ]})


def pasta_de_recebidos(para):
    """'' = pasta da casa; senão Recebidos/<usuario>. Cria se não existir. Devolve o caminho relativo."""
    if not para:
        return ""
    if para not in ler_usuarios():
        return None
    relativo = f"Recebidos/{para}"
    alvo = caminho_seguro(relativo)
    if alvo is None:
        return None
    alvo.mkdir(parents=True, exist_ok=True)
    return relativo


@app.route("/api/enviar/preparar", methods=["POST"])
@exige_login
def preparar_envio():
    para = str((request.get_json(silent=True) or {}).get("para", "")).strip().lower()
    caminho = pasta_de_recebidos(para)
    if caminho is None:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    return jsonify({"caminho": caminho})


@app.route("/api/enviar/aviso", methods=["POST"])
@exige_login
def aviso_de_envio():
    dados = request.get_json(silent=True) or {}
    para = str(dados.get("para", "")).strip().lower()
    nomes = [str(n)[:60] for n in (dados.get("nomes") or [])][:50]
    if not nomes:
        return jsonify({"ok": True})
    usuarios = ler_usuarios()
    quem = usuarios.get(request.usuario, {}).get("nome") or request.usuario
    qtd = len(nomes)
    oque = "1 arquivo" if qtd == 1 else f"{qtd} arquivos"
    lista = ", ".join(nomes[:3]) + ("…" if qtd > 3 else "")
    if para and para in usuarios:
        if para != request.usuario:
            notificar(f"{quem} te mandou {oque}: {lista}", "arquivo", para, f"arquivos:Recebidos/{para}")
        destino = usuarios[para].get("nome") or para
    else:
        notificar(f"{quem} colocou {oque} na pasta da casa: {lista}", "arquivo", "todos", "arquivos:")
        destino = "pasta da casa"
    registrar("site", request.usuario, f"Enviou {oque} para {destino}", True)
    return jsonify({"ok": True})


# ---------- Dispositivos ----------
def ip_valido(texto):
    try:
        ip = ipaddress.ip_address(texto)
    except ValueError:
        return None
    if ip.version != 4 or ip.is_loopback or not e_rede_local(ip):
        return None
    return str(ip)


def listar_dispositivos_salvos():
    with trava_arquivos:
        return ler_json(ARQ_DISPOSITIVOS, [])


def achar_dispositivo(id_disp):
    return next(
        (d for d in listar_dispositivos_salvos() if d.get("id") == id_disp), None
    )


@app.route("/api/dispositivos", methods=["GET"])
@exige_login
def listar_dispositivos():
    return jsonify({"dispositivos": listar_dispositivos_salvos()})


def validar_dados_aparelho(dados):
    """Confere nome, IP, tipo e MAC. Devolve (campos, erro)."""
    nome = str(dados.get("nome", "")).strip()
    tipo = str(dados.get("tipo", "outro"))
    ip = ip_valido(str(dados.get("ip", "")).strip())
    mac = str(dados.get("mac", "")).strip()
    if not nome or len(nome) > 40:
        return None, "Nome inválido (até 40 letras)."
    if ip is None:
        return None, "IP inválido. Use o IP da rede de casa, ex: 192.168.0.20"
    if ip == ip_da_rede():
        return None, f"{ip} é o IP deste PC (o servidor), não da TV. Use o botão Procurar TVs para achar o IP certo."
    if mac and not MAC_RE.match(mac):
        return None, "MAC inválido. Exemplo: AA:BB:CC:DD:EE:FF"
    if tipo not in TIPOS:
        tipo = "outro"
    campos = {"nome": nome, "ip": ip, "tipo": tipo}
    campos["mac"] = mac.upper().replace("-", ":") if mac else ""
    return campos, ""


@app.route("/api/dispositivos", methods=["POST"])
@exige_admin
def adicionar_dispositivo():
    campos, erro = validar_dados_aparelho(request.get_json(silent=True) or {})
    if campos is None:
        return jsonify({"erro": erro}), 400
    novo = {"id": secrets.token_hex(4), "nome": campos["nome"], "ip": campos["ip"], "tipo": campos["tipo"]}
    if campos["mac"]:
        novo["mac"] = campos["mac"]
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        lista.append(novo)
        salvar_json(ARQ_DISPOSITIVOS, lista)
    return jsonify(novo), 201


@app.route("/api/dispositivos/<id_disp>", methods=["PUT"])
@exige_admin
def editar_dispositivo(id_disp):
    campos, erro = validar_dados_aparelho(request.get_json(silent=True) or {})
    if campos is None:
        return jsonify({"erro": erro}), 400
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        alvo = next((d for d in lista if d.get("id") == id_disp), None)
        if alvo is None:
            return jsonify({"erro": "dispositivo não encontrado"}), 404
        ip_antigo = alvo.get("ip")
        alvo.update({"nome": campos["nome"], "ip": campos["ip"], "tipo": campos["tipo"]})
        if campos["mac"]:
            alvo["mac"] = campos["mac"]
        else:
            alvo.pop("mac", None)
        salvar_json(ARQ_DISPOSITIVOS, lista)
    if ip_antigo != campos["ip"]:
        fechar_shell(ip_antigo + ":5555")
        cache_apps.pop(id_disp, None)
    registrar("site", request.usuario, f"Editou o aparelho {campos['nome']}", True)
    return jsonify(alvo)


def apagar_arquivo_capa(aparelho):
    nome = aparelho.get("capa")
    if nome:
        try:
            (PASTA_CAPAS / Path(nome).name).unlink()
        except OSError:
            pass


@app.route("/api/dispositivos/<id_disp>", methods=["DELETE"])
@exige_admin
def remover_dispositivo(id_disp):
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        alvo = next((d for d in lista if d.get("id") == id_disp), None)
        if alvo is None:
            return jsonify({"erro": "dispositivo não encontrado"}), 404
        salvar_json(ARQ_DISPOSITIVOS, [d for d in lista if d.get("id") != id_disp])
    apagar_arquivo_capa(alvo)
    return jsonify({"ok": True})


def tipo_da_imagem(cabeca):
    """Descobre o tipo pelos primeiros bytes (não confia no nome do arquivo)."""
    if cabeca.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if cabeca.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if cabeca[:4] == b"RIFF" and cabeca[8:12] == b"WEBP":
        return ".webp"
    return None


@app.route("/api/dispositivos/<id_disp>/capa", methods=["POST"])
@exige_admin
def enviar_capa(id_disp):
    request.max_content_length = LIMITE_CAPA
    arquivo = request.files.get("capa")
    if arquivo is None:
        return jsonify({"erro": "Escolha uma imagem."}), 400
    cabeca = arquivo.stream.read(12)
    arquivo.stream.seek(0)
    extensao = tipo_da_imagem(cabeca)
    if extensao is None:
        return jsonify({"erro": "Use uma imagem JPG, PNG ou WEBP."}), 400
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        alvo = next((d for d in lista if d.get("id") == id_disp), None)
        if alvo is None:
            return jsonify({"erro": "dispositivo não encontrado"}), 404
        apagar_arquivo_capa(alvo)
        nome = alvo["id"] + extensao
        arquivo.save(PASTA_CAPAS / nome)
        alvo["capa"] = nome
        alvo["capa_v"] = int(time.time())
        salvar_json(ARQ_DISPOSITIVOS, lista)
    return jsonify(alvo)


@app.route("/api/dispositivos/<id_disp>/capa", methods=["DELETE"])
@exige_admin
def remover_capa(id_disp):
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        alvo = next((d for d in lista if d.get("id") == id_disp), None)
        if alvo is None:
            return jsonify({"erro": "dispositivo não encontrado"}), 404
        apagar_arquivo_capa(alvo)
        alvo.pop("capa", None)
        alvo.pop("capa_v", None)
        salvar_json(ARQ_DISPOSITIVOS, lista)
    return jsonify({"ok": True})


@app.route("/api/dispositivos/<id_disp>/capa")
@exige_login
def ver_capa(id_disp):
    aparelho = achar_dispositivo(id_disp)
    if not aparelho or not aparelho.get("capa"):
        return jsonify({"erro": "sem capa"}), 404
    caminho = PASTA_CAPAS / Path(aparelho["capa"]).name
    if not caminho.is_file() or caminho.suffix not in TIPOS_IMAGEM:
        return jsonify({"erro": "sem capa"}), 404
    return send_file(caminho, mimetype=TIPOS_IMAGEM[caminho.suffix], max_age=0)


def porta_aberta(ip, porta, limite=0.7):
    try:
        with socket.create_connection((ip, porta), timeout=limite):
            return True
    except Exception:  # noqa: BLE001  (OSError e, no Windows 7, LookupError)
        return False


def aparelho_respondendo(d):
    portas = PORTAS_STATUS.get(d.get("tipo"), PORTAS_STATUS["outro"])
    return any(porta_aberta(d["ip"], p) for p in portas)


@app.route("/api/dispositivos/status")
@exige_login
def status_dispositivos():
    lista = listar_dispositivos_salvos()
    if not lista:
        return jsonify({"status": {}})
    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(aparelho_respondendo, lista))
    return jsonify({"status": {d["id"]: r for d, r in zip(lista, resultados)}})


# ---------- ADB (teclas, apps, texto) ----------
def conectar(alvo):
    resultado = subprocess.run(
        [ADB, "connect", alvo], timeout=5, capture_output=True, text=True, creationflags=SEM_JANELA
    )
    mensagem = (resultado.stdout + resultado.stderr).strip()
    if resultado.returncode != 0 or "connected to" not in mensagem.lower():
        return False, mensagem or "Não foi possível conectar ao aparelho"
    conexoes.add(alvo)
    if alvo not in identificados:
        identificados.add(alvo)
        threading.Thread(target=identificar_aparelho, args=(alvo,), daemon=True).start()
    return True, mensagem


COMANDO_IDENTIFICAR = (
    "getprop ro.serialno; getprop ro.product.model; "
    "for i in eth0 wlan0; do echo \"$i $(cat /sys/class/net/$i/operstate 2>/dev/null) "
    "$(cat /sys/class/net/$i/address 2>/dev/null)\"; done"
)


def ler_identidade(alvo):
    """Pergunta à TV o número de série, o modelo e o MAC da placa de rede em uso."""
    try:
        r = subprocess.run(
            [ADB, "-s", alvo, "shell", COMANDO_IDENTIFICAR],
            timeout=6, capture_output=True, text=True, creationflags=SEM_JANELA,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    linhas = [l.strip() for l in r.stdout.splitlines()]
    if len(linhas) < 2:
        return None
    info = {"serial": linhas[0][:64], "modelo": linhas[1][:60], "mac": ""}
    reserva = ""
    for linha in linhas[2:]:
        partes = linha.split()
        if len(partes) == 3 and MAC_RE.match(partes[2]) and partes[2].lower() != MAC_FALSO:
            if partes[1] == "up":
                info["mac"] = partes[2].upper()
            reserva = reserva or partes[2].upper()
    info["mac"] = info["mac"] or reserva
    return info


def identificar_aparelho(alvo):
    """Guarda no cadastro o serial (para achar a TV se o IP mudar) e o MAC (para ligar pela rede)."""
    info = ler_identidade(alvo)
    if not info:
        identificados.discard(alvo)  # tenta de novo na próxima conexão
        return
    ip = alvo.split(":")[0]
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        mudou = False
        for d in lista:
            if d.get("ip") != ip:
                continue
            if info["serial"] and d.get("serial") != info["serial"]:
                d["serial"] = info["serial"]
                mudou = True
            if info["modelo"] and d.get("modelo") != info["modelo"]:
                d["modelo"] = info["modelo"]
                mudou = True
            if info["mac"] and not d.get("mac"):
                d["mac"] = info["mac"]
                mudou = True
        if mudou:
            salvar_json(ARQ_DISPOSITIVOS, lista)


def procurar_tvs():
    """Varre a rede de casa atrás de aparelhos com ADB aberto (porta 5555).
    Devolve a lista encontrada e atualiza o IP das TVs cadastradas que mudaram de endereço."""
    base = ip_da_rede().rsplit(".", 1)[0]
    ips = [f"{base}.{n}" for n in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as pool:
        abertos = [ip for ip, ok in zip(ips, pool.map(lambda i: porta_aberta(i, 5555, 0.6), ips)) if ok]

    encontrados = []
    for ip in abertos:
        alvo = ip + ":5555"
        info = None
        try:
            with trava_do_aparelho(alvo):
                if alvo in conexoes or conectar(alvo)[0]:
                    info = ler_identidade(alvo)
        except (OSError, subprocess.TimeoutExpired):
            pass
        encontrados.append({
            "ip": ip,
            "serial": (info or {}).get("serial", ""),
            "modelo": (info or {}).get("modelo", ""),
            "mac": (info or {}).get("mac", ""),
            "autorizado": info is not None,
        })

    atualizados = []
    ultima_busca.clear()
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        for e in encontrados:
            dono = None
            e["como"] = ""
            if e["serial"]:
                dono = next((d for d in lista if d.get("serial") == e["serial"]), None)
                e["como"] = "serial" if dono else ""
            if dono is None and e["mac"]:
                dono = next((d for d in lista if d.get("mac", "").upper() == e["mac"]), None)
                e["como"] = "mac" if dono else ""
            if dono is None:
                # Só confia no IP se o aparelho cadastrado não tiver outro número de série
                dono = next((d for d in lista if d.get("ip") == e["ip"]
                             and (not d.get("serial") or not e["serial"] or d.get("serial") == e["serial"])), None)
                if dono:
                    e["como"] = "ip"
                if dono and e["serial"] and not dono.get("serial"):
                    dono["serial"] = e["serial"]
            ultima_busca[e["ip"]] = dict(e)
            if dono is None:
                e["cadastrado"] = None
                continue
            e["cadastrado"] = dono["id"]
            e["nome"] = dono["nome"]
            if dono.get("ip") != e["ip"]:
                fechar_shell(dono["ip"] + ":5555")
                cache_apps.pop(dono["id"], None)
                atualizados.append(f"{dono['nome']}: {dono.get('ip')} → {e['ip']}")
                dono["ip"] = e["ip"]
            if e["mac"] and not dono.get("mac"):
                dono["mac"] = e["mac"]
            if e["modelo"]:
                dono["modelo"] = e["modelo"]
        salvar_json(ARQ_DISPOSITIVOS, lista)
        achados = {e["cadastrado"] for e in encontrados if e.get("cadastrado")}
        sem_par = [{"id": d["id"], "nome": d["nome"], "ip": d.get("ip", "")}
                   for d in lista if d.get("tipo") == "tv" and d["id"] not in achados]
    return encontrados, atualizados, sem_par


def vincular_tv(ip, id_disp):
    """Diz que a TV achada no IP é o aparelho id_disp: grava IP, serial, MAC e modelo nele."""
    achado = ultima_busca.get(ip)
    if not achado:
        return None, "Esse IP não apareceu na última busca. Procure de novo."
    with trava_arquivos:
        lista = ler_json(ARQ_DISPOSITIVOS, [])
        dono = next((d for d in lista if d.get("id") == id_disp), None)
        if dono is None:
            return None, "Aparelho não encontrado"
        ip_antigo = dono.get("ip")
        for d in lista:  # o serial agora é só deste aparelho
            if d is not dono and achado["serial"] and d.get("serial") == achado["serial"]:
                d.pop("serial", None)
        if ip_antigo and ip_antigo != ip:
            fechar_shell(ip_antigo + ":5555")
            cache_apps.pop(dono["id"], None)
        dono["ip"] = ip
        for chave in ("serial", "mac", "modelo"):
            if achado.get(chave):
                dono[chave] = achado[chave]
        salvar_json(ARQ_DISPOSITIVOS, lista)
    return f"{dono['nome']}: {ip_antigo} → {ip}", ""


def trava_do_aparelho(alvo):
    with trava_mapa_adb:
        return travas_adb.setdefault(alvo, threading.Lock())


SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no Windows, não abre janelinha preta


def fechar_shell(alvo):
    processo = shells_adb.pop(alvo, None)
    if processo is not None:
        try:
            processo.kill()
        except OSError:
            pass


def enviar_tecla_rapida(aparelho, codigo):
    """Manda a tecla por uma sessão 'adb shell' que fica aberta.
    Bem mais rápido que abrir o adb a cada clique."""
    alvo = aparelho["ip"] + ":5555"
    try:
        with trava_do_aparelho(alvo):
            for _tentativa in range(2):
                processo = shells_adb.get(alvo)
                if processo is None or processo.poll() is not None:
                    if alvo not in conexoes:
                        ok, mensagem = conectar(alvo)
                        if not ok:
                            return False, mensagem, 502
                    processo = subprocess.Popen(
                        [ADB, "-s", alvo, "shell"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=SEM_JANELA,
                    )
                    time.sleep(0.25)  # se a TV estiver fora, o adb fecha logo
                    if processo.poll() is not None:
                        conexoes.discard(alvo)
                        continue
                    shells_adb[alvo] = processo
                if codigo is None:  # só deixa a sessão aberta, pronta para o primeiro clique
                    return True, "", 200
                try:
                    processo.stdin.write(f"input keyevent {codigo}\n".encode())
                    processo.stdin.flush()
                    return True, "", 200
                except (OSError, ValueError):
                    fechar_shell(alvo)
                    conexoes.discard(alvo)
            return False, "Não consegui falar com o aparelho. Ele está ligado e com a depuração ativa?", 502
    except FileNotFoundError:
        return False, "adb não encontrado, confira o caminho", 500
    except subprocess.TimeoutExpired:
        return False, "O aparelho demorou para responder", 504


def adb_executar(aparelho, argumentos, limite=8):
    """Roda 'adb -s alvo ...' garantindo a conexão. Devolve (ok, saida_ou_erro, status)."""
    alvo = aparelho["ip"] + ":5555"
    try:
        with trava_do_aparelho(alvo):
            if alvo not in conexoes:
                ok, mensagem = conectar(alvo)
                if not ok:
                    return False, mensagem, 502

            comando = [ADB, "-s", alvo] + argumentos
            resultado = subprocess.run(
                comando, timeout=limite, capture_output=True, text=True, creationflags=SEM_JANELA
            )

            # Se o aparelho perdeu a conexão, tenta recuperar uma vez
            if resultado.returncode != 0:
                conexoes.discard(alvo)
                ok, mensagem = conectar(alvo)
                if not ok:
                    return False, mensagem, 502
                resultado = subprocess.run(
                    comando, timeout=limite, capture_output=True, text=True, creationflags=SEM_JANELA
                )

            if resultado.returncode != 0:
                return False, resultado.stderr or "Falha ao executar comando", 502
        return True, resultado.stdout, 200

    except FileNotFoundError:
        return False, "adb não encontrado, confira o caminho", 500
    except subprocess.TimeoutExpired:
        return False, "O aparelho demorou para responder", 504


def rodar_comando(aparelho, nome_comando):
    tecla = TECLAS.get(nome_comando)
    if tecla is None:
        return False, "comando desconhecido", 400
    return enviar_tecla_rapida(aparelho, tecla)


def rotulo_app(pacote):
    return APPS_CONHECIDOS.get(pacote, pacote)


def listar_apps(aparelho):
    """Apps instalados: os de terceiros + os conhecidos (YouTube/Netflix costumam vir de fábrica)."""
    guardado = cache_apps.get(aparelho["id"])
    if guardado and time.time() - guardado[0] < 300:
        return True, guardado[1], 200

    ok, saida, status_http = adb_executar(aparelho, ["shell", "pm", "list", "packages", "-3"])
    if not ok:
        return False, saida, status_http
    ok2, todos, _ = adb_executar(aparelho, ["shell", "pm", "list", "packages"], limite=12)

    pacotes = {l.strip()[8:] for l in saida.splitlines() if l.strip().startswith("package:")}
    if ok2:
        instalados = {l.strip()[8:] for l in todos.splitlines() if l.strip().startswith("package:")}
        pacotes |= {p for p in APPS_CONHECIDOS if p in instalados}
    pacotes = {p for p in pacotes if PACOTE_RE.match(p)}
    apps = sorted(
        ({"pacote": p, "nome": rotulo_app(p)} for p in pacotes),
        key=lambda a: (a["pacote"] not in APPS_CONHECIDOS, a["nome"].lower()),
    )
    cache_apps[aparelho["id"]] = (time.time(), apps)
    return True, apps, 200


def abrir_app(aparelho, pacote):
    if not isinstance(pacote, str) or not PACOTE_RE.match(pacote) or len(pacote) > 100:
        return False, "nome de app inválido", 400
    for categoria in ("android.intent.category.LAUNCHER", "android.intent.category.LEANBACK_LAUNCHER"):
        ok, saida, status_http = adb_executar(
            aparelho, ["shell", "monkey", "-p", pacote, "-c", categoria, "1"]
        )
        if not ok:
            return False, saida, status_http
        if "No activities found" not in saida and "aborted" not in saida.lower():
            return True, "", 200
    return False, "Esse app não abriu (está instalado nesse aparelho?)", 404


def texto_seguro(texto):
    """Só letras, números e pontuação simples: o ADB repassa o texto a um shell do aparelho."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9 .,@_\-]", "", t).strip()[:100]
    return t.replace(" ", "%s")


def digitar_texto(aparelho, texto):
    seguro = texto_seguro(texto)
    if not seguro:
        return False, "Texto vazio (só letras, números e . , @ _ - são aceitos)", 400
    ok, saida, status_http = adb_executar(aparelho, ["shell", "input", "text", seguro])
    return ok, ("" if ok else saida), status_http


def texto_mensagem(texto):
    """Mensagem para aparecer na TV: tira aspas e símbolos que o shell da TV interpretaria."""
    t = re.sub(r"[\x00-\x1f'\"`$\\]", "", str(texto)).strip()
    return t[:150]


def mostrar_mensagem(aparelho, texto):
    t = texto_mensagem(texto)
    if not t:
        return False, "Mensagem vazia", 400
    comando = f"cmd notification post -S bigtext -t 'Casa Conectada' casa_conectada '{t}'"
    ok, saida, status_http = adb_executar(aparelho, ["shell", comando])
    return ok, ("" if ok else saida), status_http


def montar_pacote_wol(mac):
    """'Pacote mágico': 6 bytes FF seguidos do MAC repetido 16 vezes."""
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16


def enviar_wol(aparelho):
    mac = aparelho.get("mac", "")
    if not MAC_RE.match(mac):
        return False, "Esse aparelho não tem MAC cadastrado", 400
    pacote = montar_pacote_wol(mac)
    destinos = ["255.255.255.255", aparelho["ip"].rsplit(".", 1)[0] + ".255"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for destino in destinos:
                s.sendto(pacote, (destino, 9))
    except OSError as erro:
        return False, f"Não consegui enviar o sinal: {erro}", 502
    return True, "", 200


def ligar_aparelho(aparelho):
    """Liga a TV: manda o sinal pela rede (se souber o MAC) e o comando 'acordar' pelo ADB.
    O ADB funciona com a TV em espera se a opção de rede em espera estiver ligada nela."""
    enviou = MAC_RE.match(aparelho.get("mac", "")) and enviar_wol(aparelho)[0]
    fim = time.time() + (15 if enviou else 5)
    while True:
        ok, _erro, _codigo = enviar_tecla_rapida(aparelho, TECLAS["WAKEUP"])
        if ok:
            return True, "", 200
        if time.time() > fim:
            break
        if enviou:
            enviar_wol(aparelho)
        time.sleep(1.5)
    if enviou:
        return True, "", 200  # o sinal foi; algumas TVs demoram para responder
    return False, ("A TV não respondeu. Ative nela a opção de ligar pela rede "
                   "(Wake on LAN / Rede em espera / Início rápido)."), 502


# ---------- Passos (usados por botões, cenas, horários e IA) ----------
def descrever_passo(passo, aparelho):
    nome = aparelho["nome"] if aparelho else "?"
    tipo = passo.get("tipo")
    if tipo == "comando":
        vezes = passo.get("repetir", 1)
        return f"{passo.get('comando')}" + (f" × {vezes}" if vezes > 1 else "") + f" em {nome}"
    if tipo == "app":
        return f"Abrir {rotulo_app(passo.get('pacote', ''))} em {nome}"
    if tipo == "texto":
        return f"Digitar \"{passo.get('texto', '')[:30]}\" em {nome}"
    if tipo == "mensagem":
        return f"Mensagem \"{passo.get('texto', '')[:30]}\" em {nome}"
    if tipo == "acordar":
        return f"Ligar {nome}"
    if tipo == "esperar":
        return f"Esperar {passo.get('segundos')} s"
    return "passo desconhecido"


def executar_passo(passo, dispositivos, origem, usuario):
    """Executa um passo e registra no histórico. Devolve (ok, descricao, erro, status)."""
    tipo = passo.get("tipo")
    if tipo == "esperar":
        time.sleep(passo["segundos"])
        return True, descrever_passo(passo, None), "", 200

    aparelho = next((d for d in dispositivos if d["id"] == passo.get("dispositivo_id")), None)
    if aparelho is None:
        return False, "aparelho", "dispositivo não cadastrado", 404
    descricao = descrever_passo(passo, aparelho)

    if tipo == "comando":
        ok, erro, codigo = True, "", 200
        for _ in range(passo.get("repetir", 1)):
            ok, erro, codigo = rodar_comando(aparelho, passo.get("comando"))
            if not ok:
                break
            time.sleep(0.2)
    elif tipo == "app":
        ok, erro, codigo = abrir_app(aparelho, passo.get("pacote"))
    elif tipo == "texto":
        ok, erro, codigo = digitar_texto(aparelho, passo.get("texto", ""))
    elif tipo == "mensagem":
        ok, erro, codigo = mostrar_mensagem(aparelho, passo.get("texto", ""))
    elif tipo == "acordar":
        ok, erro, codigo = ligar_aparelho(aparelho)
    else:
        return False, "passo", "tipo de passo desconhecido", 400

    registrar(origem, usuario, descricao, ok, erro)
    return ok, descricao, erro, codigo


def validar_passos(passos, dispositivos):
    """Confere e limpa uma lista de passos vinda do site. Devolve (lista, erro)."""
    if not isinstance(passos, list) or not 1 <= len(passos) <= 20:
        return None, "Uma cena precisa ter de 1 a 20 passos."
    limpos = []
    espera_total = 0
    for p in passos:
        if not isinstance(p, dict) or p.get("tipo") not in TIPOS_PASSO:
            return None, "Passo inválido."
        tipo = p["tipo"]
        if tipo == "esperar":
            try:
                seg = int(p.get("segundos"))
            except (TypeError, ValueError):
                return None, "Espera inválida."
            if not 1 <= seg <= 30:
                return None, "A espera vai de 1 a 30 segundos."
            espera_total += seg
            limpos.append({"tipo": "esperar", "segundos": seg})
            continue

        aparelho = next((d for d in dispositivos if d["id"] == p.get("dispositivo_id")), None)
        if aparelho is None:
            return None, "Um dos passos usa um aparelho que não existe mais."
        passo = {"tipo": tipo, "dispositivo_id": aparelho["id"]}
        if tipo == "comando":
            if p.get("comando") not in TECLAS:
                return None, "Comando inválido."
            try:
                passo["repetir"] = max(1, min(10, int(p.get("repetir", 1))))
            except (TypeError, ValueError):
                passo["repetir"] = 1
            passo["comando"] = p["comando"]
        elif tipo == "app":
            pacote = p.get("pacote")
            if not isinstance(pacote, str) or not PACOTE_RE.match(pacote) or len(pacote) > 100:
                return None, "Nome de app inválido."
            passo["pacote"] = pacote
        elif tipo == "texto":
            texto = str(p.get("texto", "")).strip()[:100]
            if not texto_seguro(texto):
                return None, "Texto inválido (use letras, números e . , @ _ -)."
            passo["texto"] = texto
        elif tipo == "mensagem":
            texto = texto_mensagem(p.get("texto", ""))
            if not texto:
                return None, "Escreva a mensagem."
            passo["texto"] = texto
        limpos.append(passo)
    if espera_total > 120:
        return None, "Esperas demais: no máximo 120 segundos no total."
    return limpos, ""


@app.route("/api/comando", methods=["POST"])
@exige_login
def comando():
    dados = request.get_json(silent=True) or {}
    nome = dados.get("comando")
    if nome not in TECLAS:
        return jsonify({"erro": "comando desconhecido"}), 400
    passo = {"tipo": "comando", "dispositivo_id": dados.get("id"), "comando": nome, "repetir": 1}
    ok, _desc, erro, codigo = executar_passo(passo, listar_dispositivos_salvos(), "site", request.usuario)
    if not ok:
        return jsonify({"erro": erro}), codigo
    return jsonify({"ok": True})


@app.route("/api/dispositivos/<id_disp>/apps")
@exige_login
def apps_do_aparelho(id_disp):
    aparelho = achar_dispositivo(id_disp)
    if aparelho is None:
        return jsonify({"erro": "dispositivo não cadastrado"}), 404
    ok, resultado, codigo = listar_apps(aparelho)
    if not ok:
        return jsonify({"erro": resultado}), codigo
    return jsonify({"apps": resultado})


def _acao_simples(passo_extra):
    dados = request.get_json(silent=True) or {}
    passo = dict(passo_extra, dispositivo_id=dados.get("id"))
    for chave in ("pacote", "texto"):
        if chave in passo_extra:
            passo[chave] = dados.get(chave) or ""
    ok, _desc, erro, codigo = executar_passo(passo, listar_dispositivos_salvos(), "site", request.usuario)
    if not ok:
        return jsonify({"erro": erro}), codigo
    return jsonify({"ok": True})


@app.route("/api/app", methods=["POST"])
@exige_login
def rota_abrir_app():
    return _acao_simples({"tipo": "app", "pacote": None})


@app.route("/api/texto", methods=["POST"])
@exige_login
def rota_digitar():
    return _acao_simples({"tipo": "texto", "texto": None})


@app.route("/api/comando/preparar", methods=["POST"])
@exige_login
def preparar_controle():
    """Chamado ao abrir o controle: já conecta na TV em segundo plano."""
    aparelho = achar_dispositivo((request.get_json(silent=True) or {}).get("id"))
    if aparelho is None:
        return jsonify({"erro": "dispositivo não encontrado"}), 404
    threading.Thread(target=enviar_tecla_rapida, args=(aparelho, None), daemon=True).start()
    return jsonify({"ok": True}), 202


@app.route("/api/mensagem", methods=["POST"])
@exige_login
def rota_mensagem():
    return _acao_simples({"tipo": "mensagem", "texto": None})


@app.route("/api/procurar", methods=["POST"])
@exige_admin
def rota_procurar():
    encontrados, atualizados, sem_par = procurar_tvs()
    for a in atualizados:
        registrar("site", request.usuario, f"IP atualizado: {a}", True)
    return jsonify({"encontrados": encontrados, "atualizados": atualizados, "sem_par": sem_par})


@app.route("/api/procurar/identificar", methods=["POST"])
@exige_admin
def rota_identificar_tv():
    """Faz a TV daquele IP se mostrar (barra de volume + aviso), pra saber qual é."""
    ip = str((request.get_json(silent=True) or {}).get("ip", ""))
    if ip not in ultima_busca:
        return jsonify({"erro": "Esse IP não apareceu na última busca. Procure de novo."}), 400
    ok, saida, codigo = adb_executar({"ip": ip}, ["shell", "input keyevent 24; sleep 1; input keyevent 25"])
    if not ok:
        return jsonify({"erro": saida or "A TV não respondeu"}), codigo
    mostrar_mensagem({"ip": ip}, f"Sou eu! Esta TV está no IP {ip}")
    return jsonify({"ok": True})


@app.route("/api/procurar/vincular", methods=["POST"])
@exige_admin
def rota_vincular_tv():
    dados = request.get_json(silent=True) or {}
    feito, erro = vincular_tv(str(dados.get("ip", "")), str(dados.get("id", "")))
    if erro:
        return jsonify({"erro": erro}), 400
    registrar("site", request.usuario, f"TV reconhecida: {feito}", True)
    return jsonify({"ok": True, "feito": feito})


@app.route("/api/acordar", methods=["POST"])
@exige_login
def rota_acordar():
    return _acao_simples({"tipo": "acordar"})


# ---------- Cenas ----------
def listar_cenas_salvas():
    with trava_arquivos:
        return ler_json(ARQ_CENAS, [])


def achar_cena(ref):
    """Procura por id ou pelo nome (sem diferenciar maiúsculas)."""
    ref = str(ref or "").strip().lower()
    for c in listar_cenas_salvas():
        if c["id"] == ref or c["nome"].lower() == ref:
            return c
    return None


def rodar_cena(cena, origem, usuario):
    try:
        for passo in cena["passos"]:
            ok, _desc, _erro, _codigo = executar_passo(
                passo, listar_dispositivos_salvos(), origem, usuario
            )
            if not ok:
                registrar(origem, usuario, f"Cena \"{cena['nome']}\" interrompida", False)
                return
        registrar(origem, usuario, f"Cena \"{cena['nome']}\" concluída", True)
    finally:
        cenas_rodando.discard(cena["id"])


def iniciar_cena(cena, origem, usuario):
    """Roda em segundo plano. Devolve False se essa cena já está rodando."""
    if cena["id"] in cenas_rodando:
        return False
    cenas_rodando.add(cena["id"])
    threading.Thread(target=rodar_cena, args=(cena, origem, usuario), daemon=True).start()
    return True


@app.route("/api/cenas", methods=["GET"])
@exige_login
def listar_cenas():
    dispositivos = {d["id"]: d for d in listar_dispositivos_salvos()}
    cenas = []
    for c in listar_cenas_salvas():
        resumo = [descrever_passo(p, dispositivos.get(p.get("dispositivo_id"))) for p in c["passos"]]
        cenas.append(dict(c, resumo=resumo))
    return jsonify({"cenas": cenas})


@app.route("/api/cenas", methods=["POST"])
@exige_admin
def criar_cena():
    dados = request.get_json(silent=True) or {}
    nome = str(dados.get("nome", "")).strip()
    if not nome or len(nome) > 40:
        return jsonify({"erro": "Nome da cena inválido (até 40 letras)."}), 400
    passos, erro = validar_passos(dados.get("passos"), listar_dispositivos_salvos())
    if passos is None:
        return jsonify({"erro": erro}), 400
    with trava_arquivos:
        cenas = ler_json(ARQ_CENAS, [])
        if len(cenas) >= 30:
            return jsonify({"erro": "Limite de 30 cenas."}), 400
        if any(c["nome"].lower() == nome.lower() for c in cenas):
            return jsonify({"erro": "Já existe uma cena com esse nome."}), 409
        nova = {"id": secrets.token_hex(4), "nome": nome, "passos": passos}
        cenas.append(nova)
        salvar_json(ARQ_CENAS, cenas)
    return jsonify(nova), 201


@app.route("/api/cenas/<id_cena>", methods=["DELETE"])
@exige_admin
def apagar_cena(id_cena):
    with trava_arquivos:
        cenas = ler_json(ARQ_CENAS, [])
        nova = [c for c in cenas if c["id"] != id_cena]
        if len(nova) == len(cenas):
            return jsonify({"erro": "cena não encontrada"}), 404
        salvar_json(ARQ_CENAS, nova)
        agendas = [a for a in ler_json(ARQ_AGENDAS, []) if a.get("cena_id") != id_cena]
        salvar_json(ARQ_AGENDAS, agendas)  # horários dessa cena somem junto
    return jsonify({"ok": True})


@app.route("/api/cenas/<id_cena>/executar", methods=["POST"])
@exige_login
def executar_cena_rota(id_cena):
    cena = achar_cena(id_cena)
    if cena is None:
        return jsonify({"erro": "cena não encontrada"}), 404
    if not iniciar_cena(cena, "site", request.usuario):
        return jsonify({"erro": "Essa cena já está rodando."}), 409
    return jsonify({"ok": True, "iniciada": True}), 202


# ---------- Horários ----------
@app.route("/api/agendas", methods=["GET"])
@exige_login
def listar_agendas():
    cenas = {c["id"]: c["nome"] for c in listar_cenas_salvas()}
    with trava_arquivos:
        agendas = ler_json(ARQ_AGENDAS, [])
    return jsonify(
        {"agendas": [dict(a, cena_nome=cenas.get(a["cena_id"], "?")) for a in agendas]}
    )


@app.route("/api/agendas", methods=["POST"])
@exige_admin
def criar_agenda():
    dados = request.get_json(silent=True) or {}
    hora = str(dados.get("hora", ""))
    dias = dados.get("dias")
    if achar_cena(dados.get("cena_id")) is None:
        return jsonify({"erro": "cena não encontrada"}), 404
    if not HORA_RE.match(hora):
        return jsonify({"erro": "Hora inválida (use HH:MM)."}), 400
    if (
        not isinstance(dias, list)
        or not dias
        or not all(isinstance(d, int) and 0 <= d <= 6 for d in dias)
    ):
        return jsonify({"erro": "Escolha pelo menos um dia da semana."}), 400
    cena = achar_cena(dados.get("cena_id"))
    with trava_arquivos:
        agendas = ler_json(ARQ_AGENDAS, [])
        if len(agendas) >= 50:
            return jsonify({"erro": "Limite de 50 horários."}), 400
        nova = {
            "id": secrets.token_hex(4),
            "cena_id": cena["id"],
            "hora": hora,
            "dias": sorted(set(dias)),
            "ativo": True,
            "ultima": "",
        }
        agendas.append(nova)
        salvar_json(ARQ_AGENDAS, agendas)
    return jsonify(nova), 201


@app.route("/api/agendas/<id_agenda>", methods=["DELETE"])
@exige_admin
def apagar_agenda(id_agenda):
    with trava_arquivos:
        agendas = ler_json(ARQ_AGENDAS, [])
        nova = [a for a in agendas if a["id"] != id_agenda]
        if len(nova) == len(agendas):
            return jsonify({"erro": "horário não encontrado"}), 404
        salvar_json(ARQ_AGENDAS, nova)
    return jsonify({"ok": True})


@app.route("/api/agendas/<id_agenda>/ativo", methods=["POST"])
@exige_admin
def ativar_agenda(id_agenda):
    ativo = bool((request.get_json(silent=True) or {}).get("ativo"))
    with trava_arquivos:
        agendas = ler_json(ARQ_AGENDAS, [])
        alvo = next((a for a in agendas if a["id"] == id_agenda), None)
        if alvo is None:
            return jsonify({"erro": "horário não encontrado"}), 404
        alvo["ativo"] = ativo
        salvar_json(ARQ_AGENDAS, agendas)
    return jsonify({"ok": True})


def verificar_agendas(agora=None):
    """Dispara as cenas cujo horário chegou (hora do relógio do PC)."""
    agora = agora or datetime.now()
    if principal_cuida_dos_horarios():
        return
    chave = agora.strftime("%Y-%m-%d %H:%M")
    hora = agora.strftime("%H:%M")
    with trava_arquivos:
        agendas = ler_json(ARQ_AGENDAS, [])
        mudou = False
        for a in agendas:
            if (
                not a.get("ativo", True)
                or a.get("hora") != hora
                or agora.weekday() not in a.get("dias", [])
                or a.get("ultima") == chave
            ):
                continue
            a["ultima"] = chave
            mudou = True
            cena = achar_cena(a.get("cena_id"))
            if cena is None:
                registrar("agenda", "agenda", "Horário de uma cena que não existe mais", False)
                notificar(f"Um horário das {hora} tentou rodar uma cena que não existe mais.", "falha", "admin", "cenas")
            elif not iniciar_cena(cena, "agenda", "agenda"):
                registrar("agenda", "agenda", f"Cena \"{cena['nome']}\" já estava rodando", False)
                notificar(f"O horário das {hora} não rodou \"{cena['nome']}\": ela já estava rodando.", "falha", "admin", "cenas")
        if mudou:
            salvar_json(ARQ_AGENDAS, agendas)


def laco_agendador():
    while True:
        try:
            verificar_agendas()
        except Exception as erro:  # o agendador nunca pode morrer
            print("Erro no agendador:", erro)
        time.sleep(15)


# ---------- Arquivos compartilhados ----------
def caminho_seguro(relativo):
    """Devolve o caminho dentro da pasta compartilhada, ou None se tentar sair dela."""
    relativo = str(relativo or "")
    if "\x00" in relativo:
        return None
    base = PASTA_COMPARTILHADA.resolve()
    try:
        alvo = (base / relativo).resolve()
    except (OSError, ValueError):
        return None
    if alvo != base and base not in alvo.parents:
        return None
    return alvo


def relativo_a_base(caminho):
    texto = str(caminho.relative_to(PASTA_COMPARTILHADA.resolve())).replace("\\", "/")
    return "" if texto == "." else texto


@app.route("/api/arquivos")
@exige_login
def listar_arquivos():
    pasta = caminho_seguro(request.args.get("caminho", ""))
    if pasta is None or not pasta.is_dir():
        return jsonify({"erro": "pasta não encontrada"}), 404
    itens = []
    for p in sorted(pasta.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            info = p.stat()
        except OSError:
            continue
        itens.append(
            {
                "nome": p.name,
                "pasta": p.is_dir(),
                "tamanho": 0 if p.is_dir() else info.st_size,
                "modificado": int(info.st_mtime),
            }
        )
    return jsonify({"caminho": relativo_a_base(pasta), "itens": itens})


@app.route("/api/arquivos/link")
@exige_login
def link_download():
    arquivo = caminho_seguro(request.args.get("caminho", ""))
    if arquivo is None or not arquivo.is_file():
        return jsonify({"erro": "arquivo não encontrado"}), 404
    token = serializador_download.dumps({"c": relativo_a_base(arquivo)})
    return jsonify({"url": "/baixar/" + token})


@app.route("/baixar/<token>")
def baixar(token):
    """Link temporário (60 segundos) para baixar sem carregar o arquivo na memória."""
    try:
        dados = serializador_download.loads(token, max_age=60)
    except (BadSignature, SignatureExpired):
        return jsonify({"erro": "link expirado"}), 403
    arquivo = caminho_seguro(dados.get("c", ""))
    if arquivo is None or not arquivo.is_file():
        return jsonify({"erro": "arquivo não encontrado"}), 404
    return send_file(arquivo, as_attachment=True, download_name=arquivo.name)


@app.route("/api/arquivos/enviar", methods=["POST"])
@exige_login
def enviar_arquivo():
    request.max_content_length = LIMITE_UPLOAD  # só aqui o limite é maior
    pasta = caminho_seguro(request.form.get("caminho", ""))
    if pasta is None or not pasta.is_dir():
        return jsonify({"erro": "pasta não encontrada"}), 404
    arquivo = request.files.get("arquivo")
    nome = secure_filename((arquivo.filename if arquivo else "") or "")
    if not nome:
        return jsonify({"erro": "nome de arquivo inválido"}), 400

    destino = pasta / nome
    contador = 1
    while destino.exists():  # nunca sobrescreve: cria "nome (1).ext"
        destino = pasta / f"{Path(nome).stem} ({contador}){Path(nome).suffix}"
        contador += 1
    arquivo.save(destino)
    return jsonify({"ok": True, "nome": destino.name}), 201


@app.route("/api/arquivos/pasta", methods=["POST"])
@exige_login
def criar_pasta():
    dados = request.get_json(silent=True) or {}
    pai = caminho_seguro(dados.get("caminho", ""))
    nome = secure_filename(str(dados.get("nome", "")))
    if pai is None or not pai.is_dir():
        return jsonify({"erro": "pasta não encontrada"}), 404
    if not nome:
        return jsonify({"erro": "nome de pasta inválido"}), 400
    nova = pai / nome
    if nova.exists():
        return jsonify({"erro": "já existe algo com esse nome"}), 409
    nova.mkdir()
    return jsonify({"ok": True, "nome": nome}), 201


@app.route("/api/arquivos", methods=["DELETE"])
@exige_admin
def apagar_arquivo():
    alvo = caminho_seguro(request.args.get("caminho", ""))
    if alvo is None or alvo == PASTA_COMPARTILHADA.resolve() or not alvo.exists():
        return jsonify({"erro": "não encontrado"}), 404
    try:
        if alvo.is_dir():
            alvo.rmdir()  # só apaga pasta vazia
        else:
            alvo.unlink()
    except OSError:
        return jsonify({"erro": "não consegui apagar (a pasta está vazia?)"}), 409
    return jsonify({"ok": True})


# ---------- Assistente de IA ----------
FERRAMENTAS = [
    {
        "name": "enviar_comando",
        "description": (
            "Aperta uma tecla do controle remoto em um aparelho cadastrado. "
            "POWER liga/desliga (alterna). Use 'repetir' para apertar várias vezes "
            "(ex: aumentar o volume 5 vezes)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispositivo_id": {"type": "string"},
                "comando": {"type": "string", "enum": list(TECLAS)},
                "repetir": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["dispositivo_id", "comando"],
        },
    },
    {
        "name": "listar_apps",
        "description": "Lista os apps instalados em um aparelho (nome e pacote). Use antes de abrir um app.",
        "input_schema": {
            "type": "object",
            "properties": {"dispositivo_id": {"type": "string"}},
            "required": ["dispositivo_id"],
        },
    },
    {
        "name": "abrir_app",
        "description": "Abre um app em um aparelho, pelo nome do pacote (veja listar_apps).",
        "input_schema": {
            "type": "object",
            "properties": {
                "dispositivo_id": {"type": "string"},
                "pacote": {"type": "string"},
            },
            "required": ["dispositivo_id", "pacote"],
        },
    },
    {
        "name": "digitar_texto",
        "description": "Digita um texto no aparelho (só letras sem acento, números e . , @ _ -).",
        "input_schema": {
            "type": "object",
            "properties": {
                "dispositivo_id": {"type": "string"},
                "texto": {"type": "string"},
            },
            "required": ["dispositivo_id", "texto"],
        },
    },
    {
        "name": "mostrar_mensagem",
        "description": "Mostra uma mensagem/aviso na tela de uma TV (aparece como notificação).",
        "input_schema": {
            "type": "object",
            "properties": {
                "dispositivo_id": {"type": "string"},
                "texto": {"type": "string"},
            },
            "required": ["dispositivo_id", "texto"],
        },
    },
    {
        "name": "executar_cena",
        "description": "Executa uma cena já cadastrada (vários passos de uma vez), pelo nome ou id.",
        "input_schema": {
            "type": "object",
            "properties": {"cena": {"type": "string"}},
            "required": ["cena"],
        },
    },
    {
        "name": "acordar_dispositivo",
        "description": "Liga pela rede (Wake-on-LAN) um aparelho que tenha MAC cadastrado.",
        "input_schema": {
            "type": "object",
            "properties": {"dispositivo_id": {"type": "string"}},
            "required": ["dispositivo_id"],
        },
    },
]
MAX_COMANDOS_POR_PEDIDO = 30
MAX_ACOES_POR_PEDIDO = 12


class ErroIA(Exception):
    def __init__(self, mensagem, status=502):
        super().__init__(mensagem)
        self.status = status


def chamar_claude(mensagens, sistema):
    """Chamada simples à API da Anthropic (sem instalar nada além do que já existe)."""
    corpo = json.dumps(
        {
            "model": MODELO_IA,
            "max_tokens": 600,
            "system": sistema,
            "tools": FERRAMENTAS,
            "messages": mensagens,
        }
    ).encode("utf-8")
    pedido = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=corpo,
        headers={
            "content-type": "application/json",
            "x-api-key": ler_chave_ia(),
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(pedido, timeout=60) as resposta:
            return json.load(resposta)
    except urllib.error.HTTPError as erro:
        if erro.code == 401:
            raise ErroIA("A chave da IA foi recusada. Confira o arquivo chave_anthropic.txt.")
        if erro.code == 429:
            raise ErroIA("A IA está com limite de uso no momento. Tente de novo daqui a pouco.", 429)
        raise ErroIA(
            f"A IA respondeu com erro {erro.code}. Se for falta de saldo, "
            "confira os créditos em console.anthropic.com."
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ErroIA("Não consegui falar com a IA. O PC está com internet?")


def montar_sistema(dispositivos, cenas):
    if dispositivos:
        lista = "\n".join(
            f'- id="{d["id"]}" nome="{d["nome"]}" tipo={d["tipo"]}'
            + (" (pode ser ligado pela rede)" if d.get("mac") else "")
            for d in dispositivos
        )
    else:
        lista = "(nenhum aparelho cadastrado ainda)"
    nomes_cenas = ", ".join(f'"{c["nome"]}"' for c in cenas) or "(nenhuma)"
    return (
        "Você é o assistente da 'Casa Conectada', um painel que controla aparelhos da "
        "rede de casa. Responda em português do Brasil, de forma curta e simpática.\n"
        "Você age só pelas ferramentas: apertar teclas do controle remoto (voltar, início, "
        "setas, OK, volume, mudo, play/pause, ligar/desligar — POWER alterna), listar e "
        "abrir apps, digitar texto, mostrar mensagem na TV, executar cenas e ligar "
        "aparelhos pela rede.\n"
        f"Aparelhos cadastrados:\n{lista}\n"
        f"Cenas cadastradas: {nomes_cenas}\n"
        "Para abrir um app, chame listar_apps antes e use o pacote que aparecer. "
        "Se o pedido não puder ser feito com essas ferramentas, ou citar um aparelho, app "
        "ou cena que não existe, explique com honestidade em vez de tentar. Se houver mais "
        "de um aparelho possível e não der para saber qual, pergunte. Depois de agir, diga "
        "em uma frase o que foi feito."
    )


def executar_ferramenta(uso, dispositivos, acoes, contadores, usuario):
    """Roda uma chamada de ferramenta pedida pela IA. Devolve (texto, ok)."""
    nome_ferramenta = uso.get("name")
    entrada = uso.get("input") or {}
    if contadores["acoes"] >= MAX_ACOES_POR_PEDIDO:
        return "Limite de ações por pedido atingido.", False
    contadores["acoes"] += 1

    def registrar_acao(ok, descricao, erro=""):
        acoes.append({"ok": ok, "descricao": descricao if ok else f"{descricao} falhou: {erro}"})

    if nome_ferramenta == "executar_cena":
        cena = achar_cena(entrada.get("cena"))
        if cena is None:
            return "Cena não encontrada.", False
        if not iniciar_cena(cena, "ia", usuario):
            return "Essa cena já está rodando.", False
        acoes.append({"ok": True, "descricao": f"Cena \"{cena['nome']}\" iniciada"})
        return f"Cena \"{cena['nome']}\" iniciada em segundo plano.", True

    aparelho = next((d for d in dispositivos if d["id"] == entrada.get("dispositivo_id")), None)
    if aparelho is None:
        return "Aparelho não encontrado na lista.", False

    if nome_ferramenta == "listar_apps":
        ok, resultado, _ = listar_apps(aparelho)
        if not ok:
            return f"Não consegui listar os apps: {resultado}", False
        linhas = "; ".join(f"{a['nome']} = {a['pacote']}" for a in resultado[:60])
        return linhas or "Nenhum app encontrado.", True

    if nome_ferramenta == "enviar_comando":
        if entrada.get("comando") not in TECLAS:
            return "Comando não permitido.", False
        try:
            vezes = max(1, min(10, int(entrada.get("repetir", 1))))
        except (TypeError, ValueError):
            vezes = 1
        if contadores["comandos"] + vezes > MAX_COMANDOS_POR_PEDIDO:
            return "Limite de comandos por pedido atingido.", False
        contadores["comandos"] += vezes
        passo = {"tipo": "comando", "dispositivo_id": aparelho["id"],
                 "comando": entrada["comando"], "repetir": vezes}
    elif nome_ferramenta == "abrir_app":
        passo = {"tipo": "app", "dispositivo_id": aparelho["id"], "pacote": entrada.get("pacote")}
    elif nome_ferramenta == "digitar_texto":
        passo = {"tipo": "texto", "dispositivo_id": aparelho["id"],
                 "texto": str(entrada.get("texto", ""))[:100]}
    elif nome_ferramenta == "mostrar_mensagem":
        passo = {"tipo": "mensagem", "dispositivo_id": aparelho["id"],
                 "texto": str(entrada.get("texto", ""))[:150]}
    elif nome_ferramenta == "acordar_dispositivo":
        passo = {"tipo": "acordar", "dispositivo_id": aparelho["id"]}
    else:
        return "Ferramenta desconhecida.", False

    ok, descricao, erro, _codigo = executar_passo(passo, dispositivos, "ia", usuario)
    registrar_acao(ok, descricao, erro)
    return (f"{descricao}: feito." if ok else f"{descricao}: falhou ({erro})."), ok


def limpar_blocos(blocos):
    """Devolve só os campos que a API precisa receber de volta."""
    saida = []
    for b in blocos:
        if b.get("type") == "text":
            saida.append({"type": "text", "text": b.get("text", "")})
        elif b.get("type") == "tool_use":
            saida.append(
                {
                    "type": "tool_use",
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "input": b.get("input") or {},
                }
            )
    return saida


def chat_liberado(usuario):
    agora = time.time()
    recentes = [t for t in uso_chat.get(usuario, []) if agora - t < 60]
    if len(recentes) >= 15:
        uso_chat[usuario] = recentes
        return False
    recentes.append(agora)
    uso_chat[usuario] = recentes
    return True


@app.route("/api/ia/status")
@exige_login
def ia_status():
    return jsonify({"configurada": bool(ler_chave_ia())})


@app.route("/api/chat", methods=["POST"])
@exige_login
def chat():
    if not ler_chave_ia():
        return jsonify(
            {"erro": "A IA ainda não está configurada: falta a chave no arquivo chave_anthropic.txt."}
        ), 503
    if not chat_liberado(request.usuario):
        return jsonify({"erro": "Calma! Muitas mensagens em 1 minuto."}), 429

    dados = request.get_json(silent=True) or {}
    recebidas = dados.get("mensagens")
    if not isinstance(recebidas, list) or not recebidas:
        return jsonify({"erro": "mensagem vazia"}), 400

    mensagens = []
    for m in recebidas[-20:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            return jsonify({"erro": "mensagem inválida"}), 400
        texto = str(m.get("content", "")).strip()[:2000]
        if texto:
            mensagens.append({"role": m["role"], "content": texto})
    while mensagens and mensagens[0]["role"] != "user":
        mensagens.pop(0)
    if not mensagens or mensagens[-1]["role"] != "user":
        return jsonify({"erro": "mensagem inválida"}), 400

    dispositivos = listar_dispositivos_salvos()
    sistema = montar_sistema(dispositivos, listar_cenas_salvas())
    acoes = []
    contadores = {"comandos": 0, "acoes": 0}

    try:
        for _ in range(8):
            resposta = chamar_claude(mensagens, sistema)
            blocos = limpar_blocos(resposta.get("content", []))
            usos = [b for b in blocos if b["type"] == "tool_use"]

            if resposta.get("stop_reason") != "tool_use" or not usos:
                texto = "".join(b["text"] for b in blocos if b["type"] == "text").strip()
                return jsonify({"resposta": texto or "Pronto.", "acoes": acoes})

            mensagens.append({"role": "assistant", "content": blocos})
            resultados = []
            for uso in usos:
                texto, ok = executar_ferramenta(uso, dispositivos, acoes, contadores, request.usuario)
                resultados.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": uso["id"],
                        "content": texto,
                        "is_error": not ok,
                    }
                )
            mensagens.append({"role": "user", "content": resultados})
    except ErroIA as erro:
        return jsonify({"erro": str(erro)}), erro.status

    return jsonify(
        {"resposta": "Parei por segurança depois de várias tentativas.", "acoes": acoes}
    )


# ---------- Atualizações automáticas ----------
# O PC principal publica um pacote (servidor + site) sempre que o código muda.
# Os outros servidores da casa (ex: notebook antigo) conferem de tempos em tempos,
# baixam, conferem a assinatura (feita com a segredo.key, que os dois têm igual)
# e instalam sozinhos.
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import io  # noqa: E402
import zipfile  # noqa: E402

PASTA_APP = PASTA.parent  # onde ficam "servidor" e "site-online"
ARQ_VERSAO = PASTA_APP / "versao.txt"
ARQ_SHA_INSTALADO = PASTA_APP / "versao_sha.txt"  # qual pacote do principal está instalado
PASTA_PACOTES = PASTA_DADOS / "atualizacoes"
ARQ_PACOTE = PASTA_PACOTES / "pacote.zip"
ARQ_INFO_PACOTE = PASTA_PACOTES / "info.json"
ARQ_CONFIG_ATUALIZA = PASTA_DADOS / "atualizacao.json"
INTERVALO_ATUALIZA = 10 * 60  # confere a cada 10 minutos
CODIGO_REINICIAR = 3  # o .bat religa o servidor quando ele sai com este código
estado_atualiza = {"ultima_checagem": "", "disponivel": None, "mensagem": "", "instalando": False}
trava_atualiza = threading.Lock()


def versao_local():
    try:
        return int(ARQ_VERSAO.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def nome_versao(numero):
    return f"1.{numero}"


def config_atualiza():
    padrao = {"modo": "principal", "origem": "", "auto": True}
    padrao.update(ler_json(ARQ_CONFIG_ATUALIZA, {}))
    return padrao


def arquivos_do_codigo():
    """Arquivos que vão no pacote: o servidor e o site (sem backups nem dados)."""
    def bom(caminho):
        n = caminho.name.lower()
        return caminho.is_file() and ".antes-" not in n and ".antigo" not in n
    lista = [(f"servidor/{p.name}", p) for p in sorted(PASTA.glob("*.py")) if bom(p)]
    lista += [(f"site-online/{p.name}", p) for p in sorted(PASTA_SITE.iterdir()) if bom(p)]
    return lista


def impressao_do_codigo():
    h = hashlib.sha256()
    for nome, caminho in arquivos_do_codigo():
        h.update(nome.encode() + b"\0" + caminho.read_bytes() + b"\0")
    return h.hexdigest()


def assinar(numero, sha):
    return hmac.new(_segredo.encode(), f"casa-atualizacao:{numero}:{sha}".encode(), hashlib.sha256).hexdigest()


def publicar_versao(notas="Atualização automática"):
    """Monta o pacote com o código atual e aumenta o número da versão."""
    PASTA_PACOTES.mkdir(parents=True, exist_ok=True)
    info = ler_json(ARQ_INFO_PACOTE, {})
    numero = max(info.get("numero", 0), versao_local()) + 1
    ARQ_VERSAO.write_text(str(numero), encoding="utf-8")
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, caminho in arquivos_do_codigo():
            z.write(caminho, nome)
        z.writestr("versao.txt", str(numero))
    dados = memoria.getvalue()
    sha = hashlib.sha256(dados).hexdigest()
    temp = ARQ_PACOTE.with_suffix(".tmp")
    temp.write_bytes(dados)
    os.replace(temp, ARQ_PACOTE)
    info = {
        "numero": numero, "versao": nome_versao(numero), "sha256": sha,
        "assinatura": assinar(numero, sha), "impressao": impressao_do_codigo(),
        "quando": datetime.now().strftime("%d/%m/%Y %H:%M"), "notas": str(notas)[:300],
        "tamanho": len(dados),
    }
    salvar_json(ARQ_INFO_PACOTE, info)
    notificar(f"Versão {info['versao']} publicada. Os outros servidores da casa vão se atualizar sozinhos.",
              "atualizacao", "admin", "atualizacoes")
    return info


def publicar_se_mudou():
    """No PC principal: se o código mudou desde a última versão, publica uma nova."""
    if config_atualiza()["modo"] != "principal":
        return None
    info = ler_json(ARQ_INFO_PACOTE, {})
    if info.get("impressao") == impressao_do_codigo() and ARQ_PACOTE.exists():
        return None
    return publicar_versao()


@app.route("/api/atualizacao/info")
def atualizacao_info():
    """Só o PC principal responde. Não precisa de login: o pacote é conferido pela assinatura."""
    info = ler_json(ARQ_INFO_PACOTE, {})
    if config_atualiza()["modo"] != "principal" or not info or not ARQ_PACOTE.exists():
        return jsonify({"principal": False})
    campos = ("numero", "versao", "sha256", "assinatura", "quando", "notas", "tamanho")
    return jsonify(dict({c: info.get(c) for c in campos}, principal=True))


@app.route("/api/atualizacao/pacote")
def atualizacao_pacote():
    if config_atualiza()["modo"] != "principal" or not ARQ_PACOTE.exists():
        return jsonify({"erro": "sem pacote"}), 404
    return send_file(ARQ_PACOTE, mimetype="application/zip", as_attachment=True,
                     download_name="casa-conectada.zip")


def buscar_json(url, limite=4):
    with urllib.request.urlopen(url, timeout=limite) as r:
        return json.loads(r.read(64 * 1024).decode("utf-8"))


def achar_principal():
    """Procura na rede de casa o PC principal (quem responde em /api/atualizacao/info)."""
    eu = ip_da_rede()
    base = eu.rsplit(".", 1)[0]
    ips = [f"{base}.{n}" for n in range(1, 255) if f"{base}.{n}" != eu]
    with ThreadPoolExecutor(max_workers=64) as pool:
        abertos = [ip for ip, ok in zip(ips, pool.map(lambda i: porta_aberta(i, PORTA, 0.5), ips)) if ok]
    for ip in abertos:
        url = f"http://{ip}:{PORTA}"
        try:
            if buscar_json(url + "/api/atualizacao/info").get("principal"):
                return url
        except (OSError, ValueError):
            continue
    return ""


def checar_atualizacao():
    """Pergunta ao PC principal qual a versão dele. Devolve (info, origem) ou (None, motivo)."""
    cfg = config_atualiza()
    origem = cfg["origem"]
    info = None
    if origem:
        try:
            info = buscar_json(origem + "/api/atualizacao/info")
        except (OSError, ValueError):
            info = None
    if not info or not info.get("principal"):
        origem = achar_principal()  # o IP do principal pode ter mudado
        if not origem:
            return None, "Não achei o PC principal na rede (ele está ligado com o servidor aberto?)"
        info = buscar_json(origem + "/api/atualizacao/info")
        cfg["origem"] = origem
        salvar_json(ARQ_CONFIG_ATUALIZA, cfg)
    return info, origem


def instalar_pacote(dados, info):
    """Confere o pacote e troca os arquivos. Guarda um backup da versão atual antes."""
    sha = hashlib.sha256(dados).hexdigest()
    if sha != info.get("sha256"):
        raise ValueError("O arquivo baixado veio corrompido")
    if not hmac.compare_digest(assinar(int(info["numero"]), sha), str(info.get("assinatura", ""))):
        raise ValueError("Assinatura não confere: a segredo.key deste aparelho é diferente da do PC principal")
    with zipfile.ZipFile(io.BytesIO(dados)) as z:
        nomes = z.namelist()
        for nome in nomes:
            partes = nome.split("/")
            if (nome != "versao.txt" and (len(partes) != 2 or partes[0] not in ("servidor", "site-online")
                                          or partes[1] in ("", ".", ".."))):
                raise ValueError(f"Arquivo estranho no pacote: {nome}")
        if "servidor/servidor.py" not in nomes:
            raise ValueError("Pacote sem o servidor")
        compile(z.read("servidor/servidor.py"), "servidor.py", "exec")  # não instala código quebrado
        backup = PASTA_APP / "backups" / f"versao-{nome_versao(versao_local())}"
        backup.mkdir(parents=True, exist_ok=True)
        for _nome, caminho in arquivos_do_codigo():
            destino = backup / caminho.parent.name
            destino.mkdir(exist_ok=True)
            shutil.copy2(caminho, destino / caminho.name)
        for nome in nomes:
            alvo = PASTA_APP / nome
            temp = alvo.with_name(alvo.name + ".novo")
            temp.write_bytes(z.read(nome))
            os.replace(temp, alvo)


def reiniciar_servidor():
    def sair():
        time.sleep(1.5)
        print("\nAtualização instalada. Reiniciando o servidor...")
        os._exit(CODIGO_REINICIAR)
    threading.Thread(target=sair, daemon=True).start()


def atualizar_agora(usuario="automático"):
    """Confere e, se tiver versão nova no principal, instala e reinicia. Devolve mensagem."""
    if not trava_atualiza.acquire(blocking=False):
        return False, "Já estou atualizando"
    try:
        estado_atualiza["ultima_checagem"] = datetime.now().strftime("%d/%m %H:%M")
        if config_atualiza()["modo"] != "receber":
            estado_atualiza["mensagem"] = ('Este servidor está como PC principal. Escolha "Este recebe as '
                                           'atualizações do PC principal" e clique em Salvar.')
            return False, estado_atualiza["mensagem"]
        info, origem = checar_atualizacao()
        if info is None:
            estado_atualiza["mensagem"] = origem
            return False, origem
        estado_atualiza["disponivel"] = info.get("versao")
        try:
            sha_instalado = ARQ_SHA_INSTALADO.read_text(encoding="utf-8").strip()
        except OSError:
            sha_instalado = ""
        if int(info.get("numero", 0)) <= versao_local() and sha_instalado == info.get("sha256"):
            estado_atualiza["mensagem"] = "Já está na versão mais nova"
            return True, estado_atualiza["mensagem"]
        estado_atualiza["instalando"] = True
        with urllib.request.urlopen(origem + "/api/atualizacao/pacote", timeout=60) as r:
            dados = r.read(50 * 1024 * 1024)
        instalar_pacote(dados, info)
        ARQ_SHA_INSTALADO.write_text(str(info.get("sha256", "")), encoding="utf-8")
        registrar("servidor", usuario, f"Atualizado para a versão {info['versao']}", True)
        notificar(f"O servidor {ip_da_rede()} foi atualizado para a versão {info['versao']}.", "atualizacao",
                  "admin", "atualizacoes")
        estado_atualiza["mensagem"] = f"Versão {info['versao']} instalada. Reiniciando..."
        reiniciar_servidor()
        return True, estado_atualiza["mensagem"]
    except Exception as erro:  # nunca derruba o servidor
        estado_atualiza["mensagem"] = f"Não consegui atualizar: {erro}"
        estado_atualiza["instalando"] = False
        registrar("servidor", usuario, "Atualização", False, str(erro))
        return False, estado_atualiza["mensagem"]
    finally:
        trava_atualiza.release()


def laco_atualizacoes():
    time.sleep(20)
    while True:
        cfg = config_atualiza()
        if cfg["modo"] == "receber" and cfg["auto"]:
            ok, msg = atualizar_agora()
            if not ok:
                print("Atualização:", msg)
        elif cfg["modo"] == "receber":
            try:
                info, _origem = checar_atualizacao()
                if info and int(info.get("numero", 0)) > versao_local() and info.get("versao") != estado_atualiza.get("avisada"):
                    estado_atualiza["avisada"] = info.get("versao")
                    notificar(f"Versão {info['versao']} disponível no PC principal. Vá em Atualizações para instalar.",
                              "atualizacao", "admin", "atualizacoes")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(INTERVALO_ATUALIZA)


@app.route("/api/atualizacao/estado")
@exige_admin
def atualizacao_estado():
    cfg = config_atualiza()
    pacote = ler_json(ARQ_INFO_PACOTE, {}) if cfg["modo"] == "principal" else {}
    return jsonify({
        "versao": nome_versao(versao_local()), "modo": cfg["modo"], "origem": cfg["origem"],
        "auto": cfg["auto"], "publicada": pacote.get("versao"), "quando": pacote.get("quando"),
        "notas": pacote.get("notas"), "estado": estado_atualiza, "sync": estado_sync,
    })


@app.route("/api/atualizacao/config", methods=["POST"])
@exige_admin
def atualizacao_config():
    dados = request.get_json(silent=True) or {}
    cfg = config_atualiza()
    if dados.get("modo") in ("principal", "receber"):
        cfg["modo"] = dados["modo"]
    if "auto" in dados:
        cfg["auto"] = bool(dados["auto"])
    if "origem" in dados:
        origem = str(dados["origem"]).strip().rstrip("/")
        if origem and not origem.startswith("http://"):
            origem = "http://" + origem
        if origem and ":" not in origem[7:]:
            origem += f":{PORTA}"
        cfg["origem"] = origem[:60]
    salvar_json(ARQ_CONFIG_ATUALIZA, cfg)
    if cfg["modo"] == "principal":
        publicar_se_mudou()
    return jsonify({"ok": True})


@app.route("/api/atualizacao/publicar", methods=["POST"])
@exige_admin
def atualizacao_publicar():
    if config_atualiza()["modo"] != "principal":
        return jsonify({"erro": "Só o PC principal publica versões"}), 400
    notas = (request.get_json(silent=True) or {}).get("notas") or "Publicada pelo site"
    info = publicar_versao(notas)
    registrar("site", request.usuario, f"Versão {info['versao']} publicada", True)
    return jsonify({"ok": True, "versao": info["versao"]})


@app.route("/api/atualizacao/agora", methods=["POST"])
@exige_admin
def atualizacao_agora():
    ok, msg = atualizar_agora(request.usuario)
    return jsonify({"ok": ok, "mensagem": msg})


# ---------- Sincronizar usuários entre os servidores da casa ----------
# O servidor que "recebe" (ex: Qbex) conversa com o PC principal a cada 20 segundos
# (e na hora, quando algo muda nele). Os dois juntam as listas: vale a alteração
# mais recente de cada usuário. Tudo vai cifrado e assinado com a segredo.key.
INTERVALO_SYNC = 20
estado_sync = {"ultima": "", "mensagem": "", "contatos": {}}
contatos_sync = {}  # (no principal) ip -> {"t": última vez, "perdido": bool}
falhas_sync = {"seguidas": 0, "perdido": False, "ja_falou": False}
aviso_sync = threading.Event()
ultimo_do_principal = {}  # usuários como o principal mandou na última vez
ultimas_listas_principal = {}  # aparelhos/cenas/horários como o principal mandou na última vez
_ultima_busca_principal = [0.0]

_K_CIFRA = hmac.new(_segredo.encode(), b"casa-sync-cifra", hashlib.sha256).digest()
_K_MAC = hmac.new(_segredo.encode(), b"casa-sync-mac", hashlib.sha256).digest()


def _fluxo(nonce, tamanho):
    blocos = (tamanho + 31) // 32
    return b"".join(hmac.new(_K_CIFRA, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
                    for i in range(blocos))[:tamanho]


def _xor(a, b):
    if not a:
        return b""
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def cifrar(dados):
    nonce = secrets.token_bytes(16)
    corpo = f"{int(time.time()):010d}".encode() + nonce + _xor(dados, _fluxo(nonce, len(dados)))
    return hmac.new(_K_MAC, corpo, hashlib.sha256).digest() + corpo


def decifrar(pacote):
    if len(pacote) < 58:
        raise ValueError("pacote curto")
    marca, corpo = pacote[:32], pacote[32:]
    if not hmac.compare_digest(marca, hmac.new(_K_MAC, corpo, hashlib.sha256).digest()):
        raise ValueError("assinatura não confere (a segredo.key é diferente?)")
    if abs(time.time() - int(corpo[:10])) > 1800:
        raise ValueError("o relógio dos dois aparelhos está muito diferente")
    nonce, cifrado = corpo[10:26], corpo[26:]
    return _xor(cifrado, _fluxo(nonce, len(cifrado)))


CAMPOS_SYNC = ("hash", "papel", "email", "telefone", "nome", "foto", "foto_v", "pendente", "atualizado")


def limpar_registro(nome, info):
    """Só aceita campos conhecidos (dados que vêm de outro aparelho)."""
    if not isinstance(info, dict) or not isinstance(info.get("hash"), str):
        return None
    r = {k: info[k] for k in CAMPOS_SYNC if k in info and info[k] not in (None, "")}
    if r.get("papel") not in PAPEIS:
        r["papel"] = "usuario"
    foto = str(r.get("foto", ""))
    if foto and (Path(foto).name != foto or not foto.startswith(nome + ".") or Path(foto).suffix not in TIPOS_IMAGEM):
        r.pop("foto", None)
        r.pop("foto_v", None)
    try:
        r["atualizado"] = float(r.get("atualizado", 0))
    except (TypeError, ValueError):
        r["atualizado"] = 0.0
    return r


def usuarios_brutos():
    brutos = ler_json(ARQ_USUARIOS, {})
    return {n: ({"hash": v, "papel": "admin"} if isinstance(v, str) else v) for n, v in brutos.items()}


def estado_local():
    with trava_arquivos:
        return {"usuarios": usuarios_brutos(), "apagados": ler_json(ARQ_APAGADOS, {}),
                "email": ler_json(ARQ_EMAIL, {}),
                "listas": {a.stem: ler_json(a, []) for a in (ARQ_DISPOSITIVOS, ARQ_CENAS, ARQ_AGENDAS, ARQ_NOTIFICACOES)},
                "apagados_listas": ler_json(ARQ_APAGADOS_LISTAS, {})}


def mesclar_estado(remoto, preferir_remoto):
    """Junta o que veio do outro servidor com o daqui. Devolve quem precisa baixar a foto."""
    precisa_foto = []
    with trava_arquivos:
        loc_u, loc_a = usuarios_brutos(), ler_json(ARQ_APAGADOS, {})
        rem_u = remoto.get("usuarios") or {}
        rem_a = remoto.get("apagados") or {}
        novo_u, novo_a, mudou = {}, dict(loc_a), False
        for nome in set(loc_u) | set(rem_u) | set(loc_a) | set(rem_a):
            if not NOME_USUARIO_RE.match(str(nome)):
                if nome in loc_u:  # nome fora do padrão: não sincroniza, mas também nunca apaga daqui
                    novo_u[nome] = loc_u[nome]
                continue
            apagado = max(float(loc_a.get(nome, 0) or 0), float(rem_a.get(nome, 0) or 0))
            l = limpar_registro(nome, loc_u[nome]) if nome in loc_u else None
            r = limpar_registro(nome, rem_u[nome]) if nome in rem_u else None
            lt = l["atualizado"] if l else -1
            rt = r["atualizado"] if r else -1
            de_fora = r is not None and (l is None or rt > lt or (rt == lt and preferir_remoto and r != l))
            vencedor = r if de_fora else l
            if vencedor and (apagado == 0 or vencedor["atualizado"] > apagado):
                novo_u[nome] = vencedor
                novo_a.pop(nome, None)
                if de_fora and vencedor != l:
                    mudou = True
                    if l and l.get("foto") and l.get("foto") != vencedor.get("foto"):
                        apagar_foto(l)
                    if vencedor.get("foto") and (not l or l.get("foto_v") != vencedor.get("foto_v")
                                                 or not (PASTA_FOTOS / vencedor["foto"]).is_file()):
                        precisa_foto.append(nome)
                elif l is not None and loc_u.get(nome) != l:
                    mudou = True
            else:
                if l is not None:
                    apagar_foto(l)
                    mudou = True
                if apagado:
                    novo_a[nome] = apagado
        if novo_a != loc_a:
            salvar_json(ARQ_APAGADOS, novo_a)
        if mudou:
            salvar_json(ARQ_USUARIOS, novo_u)
        # Configuração do Gmail: vale a mais recente
        loc_e, rem_e = ler_json(ARQ_EMAIL, {}), remoto.get("email") or {}
        if isinstance(rem_e, dict) and rem_e.get("gmail") and rem_e != loc_e:
            lt, rt = float(loc_e.get("atualizado", 0) or 0), float(rem_e.get("atualizado", 0) or 0)
            if rt > lt or (rt == lt and preferir_remoto):
                salvar_json(ARQ_EMAIL, {"gmail": str(rem_e["gmail"])[:200], "senha_app": str(rem_e.get("senha_app", ""))[:40],
                                        "atualizado": rt})
    return precisa_foto


ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def principal_cuida_dos_horarios():
    """No servidor que recebe: se falou com o principal há pouco, deixa os horários com ele."""
    cfg = config_atualiza()
    return cfg["modo"] == "receber" and time.time() - estado_sync.get("ok_em", 0) < 90


def limpar_item(colecao, item):
    if not isinstance(item, dict) or not ID_RE.match(str(item.get("id", ""))):
        return None
    r = dict(item)
    if colecao == "dispositivos":
        capa = str(r.get("capa", ""))
        if capa and (Path(capa).name != capa or not capa.startswith(r["id"] + ".") or Path(capa).suffix not in TIPOS_IMAGEM):
            r.pop("capa", None)
            r.pop("capa_v", None)
    try:
        r["atualizado"] = float(r.get("atualizado", 0))
    except (TypeError, ValueError):
        r["atualizado"] = 0.0
    return r


def mesclar_listas(remoto, preferir_remoto):
    """Aparelhos, cenas e horários: vale a alteração mais recente de cada item. Devolve capas a baixar."""
    precisa_capa = []
    rem_listas = remoto.get("listas") or {}
    rem_apag = remoto.get("apagados_listas") or {}
    with trava_arquivos:
        todos_apag = ler_json(ARQ_APAGADOS_LISTAS, {})
        novo_apag = {k: dict(v) for k, v in todos_apag.items()}
        for arquivo in (ARQ_DISPOSITIVOS, ARQ_CENAS, ARQ_AGENDAS, ARQ_NOTIFICACOES):
            col = arquivo.stem
            if col not in rem_listas:
                continue  # o outro servidor ainda é da versão antiga
            local = [d for d in ler_json(arquivo, []) if isinstance(d, dict)]
            loc = {d.get("id"): d for d in local}
            rem = {}
            for d in rem_listas.get(col) or []:
                item = limpar_item(col, d)
                if item:
                    rem[item["id"]] = item
            la, ra = todos_apag.get(col, {}), rem_apag.get(col) or {}
            ordem = [d.get("id") for d in local] + [i for i in rem if i not in loc]
            ordem += [i for i in set(la) | set(ra) if i not in ordem]
            resultado, mudou = [], False
            for i in ordem:
                apagado = max(float(la.get(i, 0) or 0), float(ra.get(i, 0) or 0))
                l = limpar_item(col, loc[i]) if i in loc else None
                r = rem.get(i)
                if i in loc and l is None:  # item local estranho: mantém como está
                    resultado.append(loc[i])
                    continue
                lt = l["atualizado"] if l else -1
                rt = r["atualizado"] if r else -1
                de_fora = r is not None and (l is None or rt > lt or (rt == lt and preferir_remoto and r != l))
                venc = r if de_fora else l
                if venc and (apagado == 0 or venc["atualizado"] > apagado):
                    resultado.append(venc)
                    novo_apag.get(col, {}).pop(i, None)
                    if de_fora and venc != l:
                        mudou = True
                        if col == "dispositivos":
                            if l and l.get("capa") and l.get("capa") != venc.get("capa"):
                                apagar_arquivo_capa(l)
                            if venc.get("capa") and (not l or l.get("capa_v") != venc.get("capa_v")
                                                     or not (PASTA_CAPAS / venc["capa"]).is_file()):
                                precisa_capa.append(i)
                            if l and l.get("ip") != venc.get("ip"):
                                fechar_shell(str(l.get("ip", "")) + ":5555")
                            cache_apps.pop(i, None)
                else:
                    if l is not None:
                        mudou = True
                        if col == "dispositivos":
                            apagar_arquivo_capa(l)
                    if apagado:
                        novo_apag.setdefault(col, {})[i] = apagado
            if mudou:
                salvar_json(arquivo, resultado, carimbar=False)
        if novo_apag != todos_apag:
            salvar_json(ARQ_APAGADOS_LISTAS, novo_apag, carimbar=False)
    return precisa_capa


def gravar_capa_recebida(id_disp, dados):
    item = next((d for d in ler_json(ARQ_DISPOSITIVOS, []) if isinstance(d, dict) and d.get("id") == id_disp), None)
    capa = (item or {}).get("capa")
    if not capa or not dados or len(dados) > LIMITE_CAPA or tipo_da_imagem(dados[:12]) != Path(capa).suffix:
        return
    PASTA_CAPAS.mkdir(parents=True, exist_ok=True)
    (PASTA_CAPAS / Path(capa).name).write_bytes(dados)


def ler_capa_local(id_disp):
    item = next((d for d in ler_json(ARQ_DISPOSITIVOS, []) if isinstance(d, dict) and d.get("id") == id_disp), None)
    if not item or not item.get("capa"):
        return None
    try:
        caminho = PASTA_CAPAS / Path(item["capa"]).name
        return caminho.read_bytes() if caminho.stat().st_size <= LIMITE_CAPA else None
    except OSError:
        return None


def gravar_foto_recebida(nome, dados):
    info = usuarios_brutos().get(nome) or {}
    foto = info.get("foto")
    if not foto or not dados or len(dados) > LIMITE_FOTO:
        return
    if tipo_da_imagem(dados[:12]) != Path(foto).suffix:
        return
    PASTA_FOTOS.mkdir(parents=True, exist_ok=True)
    (PASTA_FOTOS / Path(foto).name).write_bytes(dados)


def ler_foto_local(nome):
    info = usuarios_brutos().get(nome) or {}
    if not info.get("foto"):
        return None
    caminho = PASTA_FOTOS / Path(info["foto"]).name
    try:
        return caminho.read_bytes() if caminho.stat().st_size <= LIMITE_FOTO else None
    except OSError:
        return None


@app.route("/api/sync/usuarios", methods=["POST"])
def rota_sync_usuarios():
    request.max_content_length = 64 * 1024 * 1024
    try:
        remoto = json.loads(decifrar(request.get_data()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as erro:
        return jsonify({"erro": f"sincronização recusada: {erro}"}), 403
    mesclar_estado(remoto, preferir_remoto=False)
    mesclar_listas(remoto, preferir_remoto=False)
    for id_disp, b64 in (remoto.get("capas") or {}).items():
        try:
            gravar_capa_recebida(str(id_disp), base64.b64decode(b64))
        except (ValueError, OSError):
            pass
    for nome, b64 in (remoto.get("fotos") or {}).items():
        try:
            gravar_foto_recebida(str(nome), base64.b64decode(b64))
        except (ValueError, OSError):
            pass
    estado_sync["contatos"][request.remote_addr] = datetime.now().strftime("%d/%m %H:%M:%S")
    contato = contatos_sync.setdefault(request.remote_addr, {"t": 0, "perdido": False})
    if contato["perdido"]:
        notificar(f"O servidor {request.remote_addr} voltou a sincronizar.", "servidor", "admin", "atualizacoes")
    contato.update(t=time.time(), perdido=False)
    resposta = json.dumps(estado_local()).encode("utf-8")
    return app.response_class(cifrar(resposta), mimetype="application/octet-stream")


@app.route("/api/sync/foto", methods=["POST"])
def rota_sync_foto():
    try:
        pedido = json.loads(decifrar(request.get_data()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify({"erro": "recusado"}), 403
    if pedido.get("capa"):
        dados = ler_capa_local(str(pedido["capa"]))
    else:
        dados = ler_foto_local(str(pedido.get("usuario", "")))
    if dados is None:
        return jsonify({"erro": "sem foto"}), 404
    return app.response_class(cifrar(dados), mimetype="application/octet-stream")


def _postar_cifrado(url, dados, limite=20):
    pedido = urllib.request.Request(url, data=cifrar(dados), method="POST",
                                    headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(pedido, timeout=limite) as r:
        return decifrar(r.read(80 * 1024 * 1024))


def sincronizar_usuarios():
    """Só no servidor que recebe: manda o que tem e junta o que o principal tem."""
    cfg = config_atualiza()
    if cfg["modo"] != "receber":
        return
    origem = cfg["origem"]
    if not origem:
        if time.time() - _ultima_busca_principal[0] < 300:
            return
        _ultima_busca_principal[0] = time.time()
        origem = achar_principal()
        if not origem:
            estado_sync["mensagem"] = "Não achei o PC principal para sincronizar os usuários."
            return
        cfg["origem"] = origem
        salvar_json(ARQ_CONFIG_ATUALIZA, cfg)
    estado = estado_local()
    fotos = {}
    if ultimo_do_principal:
        for nome, info in estado["usuarios"].items():
            if info.get("foto") and (ultimo_do_principal.get(nome) or {}).get("foto_v") != info.get("foto_v"):
                dados = ler_foto_local(nome)
                if dados:
                    fotos[nome] = base64.b64encode(dados).decode()
    estado["fotos"] = fotos
    capas = {}
    if ultimas_listas_principal:
        conhecidas = {d.get("id"): d for d in ultimas_listas_principal.get("dispositivos", []) if isinstance(d, dict)}
        for d in estado["listas"]["dispositivos"]:
            if d.get("capa") and (conhecidas.get(d.get("id")) or {}).get("capa_v") != d.get("capa_v"):
                dados = ler_capa_local(d["id"])
                if dados:
                    capas[d["id"]] = base64.b64encode(dados).decode()
    estado["capas"] = capas
    try:
        remoto = json.loads(_postar_cifrado(origem + "/api/sync/usuarios", json.dumps(estado).encode()).decode())
        ultimo_do_principal.clear()
        ultimo_do_principal.update(remoto.get("usuarios") or {})
        ultimas_listas_principal.clear()
        ultimas_listas_principal.update(remoto.get("listas") or {})
        for nome in mesclar_estado(remoto, preferir_remoto=True):
            try:
                gravar_foto_recebida(nome, _postar_cifrado(origem + "/api/sync/foto", json.dumps({"usuario": nome}).encode()))
            except (OSError, ValueError):
                pass
        for id_disp in mesclar_listas(remoto, preferir_remoto=True):
            try:
                gravar_capa_recebida(id_disp, _postar_cifrado(origem + "/api/sync/foto", json.dumps({"capa": id_disp}).encode()))
            except (OSError, ValueError):
                pass
        estado_sync["ok_em"] = time.time()
        if falhas_sync["perdido"]:
            notificar("Voltei a falar com o PC principal. Os horários voltaram para ele.", "servidor", "admin", "atualizacoes")
        falhas_sync.update(seguidas=0, perdido=False, ja_falou=True)
        estado_sync["ultima"] = datetime.now().strftime("%d/%m %H:%M:%S")
        estado_sync["mensagem"] = ""
    except urllib.error.HTTPError as erro:
        estado_sync["mensagem"] = (f"O PC principal recusou a sincronização ({erro.code}): ele ainda está na versão "
                                   "antiga ou a segredo.key dos dois é diferente.")
    except (OSError, ValueError) as erro:
        estado_sync["mensagem"] = f"Não consegui sincronizar os usuários: {erro}"
        perdeu_contato()


def perdeu_contato():
    """No servidor que recebe: avisa depois de 3 falhas seguidas (uns 40 segundos)."""
    falhas_sync["seguidas"] += 1
    if falhas_sync["seguidas"] == 3 and falhas_sync["ja_falou"] and not falhas_sync["perdido"]:
        falhas_sync["perdido"] = True
        notificar(f"O servidor {ip_da_rede()} perdeu contato com o PC principal (desligado ou sem rede). "
                  "Ele assume os horários até o principal voltar.", "servidor", "admin", "atualizacoes")


def vigiar_contatos():
    """No principal: avisa quando um servidor da casa para de sincronizar por mais de 2 minutos."""
    for ip, c in contatos_sync.items():
        if not c["perdido"] and c["t"] and time.time() - c["t"] > 120:
            c["perdido"] = True
            notificar(f"O servidor {ip} parou de sincronizar (desligado ou sem rede).", "servidor", "admin", "atualizacoes")


def laco_sync():
    time.sleep(5)
    while True:
        try:
            sincronizar_usuarios()
            vigiar_contatos()
        except Exception as erro:  # nunca derruba o servidor
            estado_sync["mensagem"] = f"Erro na sincronização: {erro}"
        aviso_sync.wait(INTERVALO_SYNC)
        aviso_sync.clear()


def ip_da_rede():
    """Só para mostrar no terminal o endereço para abrir em outros aparelhos."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def procurar_no_inicio():
    """Ao ligar o servidor, confere se alguma TV mudou de IP e corrige sozinho."""
    try:
        _encontrados, atualizados, _sem_par = procurar_tvs()
        for a in atualizados:
            print("IP atualizado:", a)
            registrar("servidor", "servidor", f"IP atualizado: {a}", True)
    except Exception as erro:  # nunca derruba o servidor
        print("Não consegui procurar as TVs:", erro)


if __name__ == "__main__" and "--procurar" in sys.argv:
    preparar_pastas()
    print("Procurando TVs na rede (uns 10 segundos)...")
    achadas, mudancas, sem_par = procurar_tvs()
    if not achadas:
        print("Nenhuma TV com a depuração (ADB) ligada foi encontrada.")
    for e in achadas:
        nome = e.get("nome") or "(não cadastrada)"
        extra = "" if e["autorizado"] else "  <- aceite a permissão que apareceu na TV e rode de novo"
        print(f"  {e['ip']:15}  {nome:20}  {e['modelo']}{extra}")
    if any(not e.get("cadastrado") for e in achadas) and sem_par:
        print("Achei TV que o site não reconheceu. Abra o site > Aparelhos > Procurar TVs e diga qual é:",
              ", ".join(d["nome"] for d in sem_par))
    for a in mudancas:
        print("IP atualizado no site:", a)
    if achadas and not mudancas:
        print("Os IPs do site já estavam certos.")
    sys.exit(0)

if __name__ == "__main__":
    print(f"Dados e arquivos em: {PASTA_DADOS}")
    if not ler_chave_ia():
        print(f"IA desligada: cole sua chave em {ARQ_CHAVE_IA}")
    print(f"No PC:             http://localhost:{PORTA}")
    print(f"Em outro aparelho: http://{ip_da_rede()}:{PORTA}  (mesma rede wi-fi)")
    threading.Thread(target=laco_agendador, daemon=True).start()
    threading.Thread(target=procurar_no_inicio, daemon=True).start()
    try:
        nova = publicar_se_mudou()
        if nova:
            print(f"Versão {nova['versao']} publicada para os outros servidores da casa.")
    except Exception as erro:
        print("Não consegui publicar a versão:", erro)
    cfg_atualiza = config_atualiza()
    print(f"Versão deste servidor: {nome_versao(versao_local())}"
          + ("  (recebe atualizações sozinho)" if cfg_atualiza["modo"] == "receber" else "  (PC principal)"))
    threading.Thread(target=laco_atualizacoes, daemon=True).start()
    threading.Thread(target=laco_sync, daemon=True).start()
    app.run(host="0.0.0.0", port=PORTA, threaded=True)
