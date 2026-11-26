
import subprocess
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS

# Caminho do ADB
ADB = r"C:\caminho\do\tvdetox\platform-tools-latest-windows\platform-tools\adb.exe"

app = Flask(__name__)
CORS(app)

# Comandos disponíveis
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
}

# Guarda os dispositivos que já foram conectados
conexoes = set()

# Evita dois comandos ADB simultâneos
trava_adb = threading.Lock()


def conectar(alvo):
    resultado = subprocess.run(
        [ADB, "connect", alvo],
        timeout=5,
        capture_output=True,
        text=True
    )

    mensagem = (resultado.stdout + resultado.stderr).strip()

    if resultado.returncode != 0 or "connected to" not in mensagem.lower():
        return False, mensagem or "Não foi possível conectar à TV"

    conexoes.add(alvo)
    return True, mensagem


@app.route("/status")
def status():
    return jsonify({"ok": True})


@app.route("/comando", methods=["POST"])
def comando():
    dados = request.get_json(silent=True) or {}

    ip = dados.get("ip")
    nome_comando = dados.get("comando")

    if not ip or not isinstance(ip, str):
        return jsonify({"erro": "IP inválido"}), 400

    tecla = TECLAS.get(nome_comando)

    if tecla is None:
        return jsonify({"erro": "comando desconhecido"}), 400

    alvo = ip + ":5555"

    try:
        with trava_adb:

            # Só conecta se ainda não estiver na lista
            if alvo not in conexoes:
                ok, mensagem = conectar(alvo)

                if not ok:
                    return jsonify({"erro": mensagem}), 502

            # Envia o comando diretamente, sem reconectar
            resultado = subprocess.run(
                [
                    ADB, "-s", alvo,
                    "shell", "input", "keyevent", str(tecla)
                ],
                timeout=5,
                capture_output=True,
                text=True
            )

            # Se a TV perdeu a conexão, tenta recuperar
            if resultado.returncode != 0:
                conexoes.discard(alvo)

                ok, mensagem = conectar(alvo)

                if not ok:
                    return jsonify({"erro": mensagem}), 502

                # Tenta enviar o comando novamente
                resultado = subprocess.run(
                    [
                        ADB, "-s", alvo,
                        "shell", "input", "keyevent", str(tecla)
                    ],
                    timeout=5,
                    capture_output=True,
                    text=True
                )

            if resultado.returncode != 0:
                return jsonify({
                    "erro": resultado.stderr or "Falha ao executar comando"
                }), 502

        return jsonify({"ok": True})

    except FileNotFoundError:
        return jsonify({
            "erro": "adb não encontrado, confira o caminho"
        }), 500

    except subprocess.TimeoutExpired:
        return jsonify({
            "erro": "A TV demorou para responder"
        }), 504


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
