"""Monta o pacote do Casa Conectada para Windows 7 (64 bits).

Gera win7/CasaConectada-Win7.zip com:
  - Python 3.8 portátil (o último que roda no Windows 7) e o Flask já dentro;
  - ADB (platform-tools r33.0.3, uma versão mais antiga);
  - o servidor e o site desta pasta;
  - INSTALAR.bat e os atalhos.

Uso (em qualquer computador com Python 3 e internet):
    python win7/montar_pacote.py

Os dados da casa (usuários, aparelhos, senhas) NÃO vão no pacote: o servidor
novo os recebe sozinho pela sincronização com o PC principal.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "win7" / "CasaConectada-Win7"
PYTHON_URL = "https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip"
ADB_URL = "https://dl.google.com/android/repository/platform-tools_r33.0.3-windows.zip"
PACOTES = ["flask<3.1", "colorama", "importlib-metadata<8.6", "zipp<3.21"]


def baixar(url, destino):
    print("Baixando", url)
    with urllib.request.urlopen(url, timeout=120) as r, open(destino, "wb") as f:
        shutil.copyfileobj(r, f)


def main():
    if SAIDA.exists():
        shutil.rmtree(SAIDA)
    (SAIDA / "app" / "lib").mkdir(parents=True)
    (SAIDA / "app" / "platform-tools").mkdir()
    (SAIDA / "dados-iniciais").mkdir()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        baixar(PYTHON_URL, tmp / "py.zip")
        zipfile.ZipFile(tmp / "py.zip").extractall(SAIDA / "python")
        (SAIDA / "python" / "python38._pth").write_text("python38.zip\r\n.\r\n..\\app\\lib\r\n")

        subprocess.check_call([sys.executable, "-m", "pip", "download", "-q", "--only-binary=:all:",
                               "--platform", "win_amd64", "--python-version", "3.8", "--implementation", "cp",
                               "-d", str(tmp / "whl")] + PACOTES)
        for roda in (tmp / "whl").glob("*.whl"):
            zipfile.ZipFile(roda).extractall(SAIDA / "app" / "lib")

        baixar(ADB_URL, tmp / "adb.zip")
        z = zipfile.ZipFile(tmp / "adb.zip")
        for nome in z.namelist():
            arquivo = nome.split("/")[-1]
            if arquivo in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
                (SAIDA / "app" / "platform-tools" / arquivo).write_bytes(z.read(nome))

    shutil.copytree(RAIZ / "servidor", SAIDA / "app" / "servidor",
                    ignore=shutil.ignore_patterns("__pycache__", "*.antes-*", "*.antigo.*"))
    shutil.copytree(RAIZ / "site-online", SAIDA / "app" / "site-online",
                    ignore=shutil.ignore_patterns("*.antes-*", "*.antigo.*"))
    shutil.copytree(RAIZ / "win7" / "atalhos", SAIDA / "atalhos")
    for arquivo in ("INSTALAR.bat", "LEIA-ME.txt"):
        shutil.copy2(RAIZ / "win7" / arquivo, SAIDA / arquivo)
    (SAIDA / "dados-iniciais" / "atualizacao.json").write_text(
        json.dumps({"modo": "receber", "origem": "", "auto": True}))

    zip_final = shutil.make_archive(str(SAIDA), "zip", SAIDA.parent, SAIDA.name)
    print("Pronto:", zip_final)


if __name__ == "__main__":
    main()
