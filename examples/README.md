# Приклади ігор для HGS

| Папка | Адаптер | Що показує |
|-------|---------|-----------|
| [demo-arena](demo-arena/README.md) | `html` (relay і logic) | браузерна гра з GitHub Pages + JS SDK |
| [python-worker](python-worker) | `python`, mode `worker` | логіка гри на Python у власному venv гри, збереження |
| [python-standalone](python-standalone) | `python`, mode `standalone` | наявний Python-сервер зі своїм портом; HGS керує процесом і проксує гравців |
| [node-worker](node-worker) | `process`, mode `worker` | сервер гри іншою мовою (Node.js) за Worker Protocol |

Щоб `python-standalone` працював у власному середовищі гри:

```bash
cd examples/python-standalone
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`node-worker` запускає програму `node` поза папкою гри — при першому старті HGS попросить дозволу запускати її.
