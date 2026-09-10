#!/usr/bin/env python3
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "MaxSensr-Funscript-Player-v1.4.15-PersistentResume.py"
PACKAGE = ROOT / "src" / "maxsensr_player"
README = ROOT / "README.md"
REQUIREMENTS = ROOT / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"
LAUNCHER = ROOT / "MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py"
OLD_VERSION = "v1.4.15-PersistentResume"
NEW_VERSION = "v1.4.16-PersistentResume"

FRENCH = re.compile(r"[àâçéèêëîïôûùüÿœ]|\b(afin|ajoute|aucun|avec|avant|choisir|commande|connexion|dossier|début|défile|écran|fichier|fenêtre|grille|lancement|lecture|même|moteur|nom|pour|priorité|retourne|script|sélection|supprime|uniquement|vidéo|vibration|lorsque|automatique|arrêt|réglage|langue|périphérique|déconnecte|toujours|couper|demande|applique|charge|générateur|plusieurs|objet|accepte|format)\b", re.I)


def src(lines, node):
    end = getattr(node, "end_lineno", node.lineno)
    return "".join(lines[node.lineno - 1:end]).rstrip() + "\n"


def clean_source(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    ranges = []
    def visit(body):
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and FRENCH.search(value.value):
                ranges.append((body[0].lineno, body[0].end_lineno or body[0].lineno))
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(node.body)
    visit(tree.body)
    for start, end in sorted(ranges, reverse=True):
        for index in range(start - 1, end):
            lines[index] = "\n" if lines[index].endswith("\n") else ""
    text = "".join(lines)
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and not token.string.startswith("#!") and FRENCH.search(token.string):
            token = tokenize.TokenInfo(token.type, "", token.start, token.end, token.line)
        tokens.append(token)
    return tokenize.untokenize(tokens)


def deps():
    return [x.strip() for x in REQUIREMENTS.read_text(encoding="utf-8").splitlines() if x.strip() and not x.lstrip().startswith("#")]


def update_readme(text: str) -> str:
    text = text.replace("v1.4.15-PersistentResume", "v1.4.16-PersistentResume")
    text = text.replace("v1.4.15", "v1.4.16")
    text = text.replace("MaxSensr-Funscript-Player-v1.4.15-PersistentResume.py", "MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py")
    block = '''\n## uv deployment\n\nThe recommended deployment method is [`uv`](https://docs.astral.sh/uv/). The project is configured to use the system Python so Tkinter remains provided by the Linux distribution.\n\n### Debian / Ubuntu\n\n```bash\nsudo apt update\nsudo apt install -y python3 python3-tk mpv bluetooth bluez\n```\n\nInstall `uv` using its official installation method, then:\n\n```bash\ngit clone https://github.com/st3ph666/JoyHub-MaxSensr-Player.git\ncd JoyHub-MaxSensr-Player\nuv sync\nuv run python MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py\n```\n\n`bleak` is installed automatically by `uv sync`. Do not run `uv sync` with `sudo`.\n\n### Update\n\n```bash\ngit pull\nuv sync\nuv run python MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py\n```\n\n## Source architecture\n\n```text\nMaxSensr-Funscript-Player-v1.4.16-PersistentResume.py  # Compatibility launcher\nsrc/maxsensr_player/\n├── __init__.py                                      # Version metadata\n├── settings.py                                      # Device, path and profile constants\n├── app.py                                           # BLE worker, Funscript helpers and Tkinter application\n└── main.py                                          # Application entry point\n```\n\nSource-code comments are maintained in **English only**. French / English user-interface text is preserved.\n\n'''
    if "## uv deployment" not in text:
        text += block
    return text


def main():
    source = SOURCE.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    constants, app_nodes = [], []
    main_node = None
    terminal_if = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_node = node
            continue
        if isinstance(node, ast.If):
            terminal_if = node
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if names and all(n.isupper() for n in names):
                constants.append(node)
                continue
        app_nodes.append(node)
    if main_node is None:
        raise RuntimeError("main() not found")

    import_text = "".join(src(lines, n) for n in imports)
    PACKAGE.mkdir(parents=True, exist_ok=True)
    settings = '"""Device, path, profile, and application constants."""\n\n' + import_text + "\n" + "\n".join(src(lines, n) for n in constants)
    settings = settings.replace(OLD_VERSION, NEW_VERSION)
    app = '"""MaxSensr BLE worker, Funscript helpers, and Tkinter application."""\n\n' + import_text + "\nfrom .settings import *  # noqa: F403,F401\n\n" + "\n".join(src(lines, n) for n in app_nodes)
    app = clean_source(app)
    main_code = '"""Application entry point."""\n\nfrom .app import *  # noqa: F403,F401\n\n' + src(lines, main_node) + '\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    launcher = '''#!/usr/bin/env python3\n"""Compatibility launcher for MaxSensr Funscript Player v1.4.16."""\n\nfrom pathlib import Path\nimport sys\n\nROOT = Path(__file__).resolve().parent\nSRC = ROOT / "src"\nif str(SRC) not in sys.path:\n    sys.path.insert(0, str(SRC))\n\nfrom maxsensr_player.main import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'''
    (PACKAGE / "__init__.py").write_text('"""MaxSensr Funscript Player package."""\n\n__version__ = "1.4.16"\n', encoding="utf-8")
    (PACKAGE / "settings.py").write_text(clean_source(settings), encoding="utf-8")
    (PACKAGE / "app.py").write_text(app, encoding="utf-8")
    (PACKAGE / "main.py").write_text(main_code, encoding="utf-8")
    LAUNCHER.write_text(launcher, encoding="utf-8")
    LAUNCHER.chmod(0o755)
    dep_lines = "\n".join(f'    "{d}",' for d in deps())
    PYPROJECT.write_text(f'''[project]\nname = "joyhub-maxsensr-player"\nversion = "1.4.16"\ndescription = "Linux BLE funscript player for JoyHub MaxSensr"\nrequires-python = ">=3.11"\ndependencies = [\n{dep_lines}\n]\n\n[tool.uv]\npackage = false\npython-preference = "only-system"\n''', encoding="utf-8")
    README.write_text(update_readme(README.read_text(encoding="utf-8")), encoding="utf-8")
    SOURCE.unlink()


if __name__ == "__main__":
    main()
