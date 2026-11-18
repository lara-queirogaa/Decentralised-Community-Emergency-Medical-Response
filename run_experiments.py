#!/usr/bin/env python3
"""
Corre experiências: cenários x valores de um parâmetro x várias sementes.

Para cada combinação cria uma cópia do cenário com o valor do parâmetro e a
semente, corre-a com `main.py --metrics` (num processo à parte, para cada
corrida começar do zero) e no fim junta os resultados em tabelas.

Pré-requisito: o servidor XMPP a correr noutro terminal (`spade run --memory`).

Utilização:

    # as 3 experiências, a variar o limiar de confirmação (o padrão)
    python run_experiments.py experiments/*.json

    # outro parâmetro, mais sementes, outra pasta de resultados
    python run_experiments.py experiments/hostil.json \\
        --param witnesses.*.error_rate --values 0,0.1,0.25,0.4 \\
        --seeds 10 --out resultados_erro

    # ver o plano sem correr nada
    python run_experiments.py experiments/*.json --dry-run

O parâmetro é um caminho no JSON do cenário, com pontos: por exemplo
"verification.threshold", "verification.witness_weight" ou
"smartwatches.*.urgent_threshold". O "*" aplica o valor a todos os
elementos de uma lista.

Resultados (na pasta --out):
    runs.csv      uma linha por corrida
    summary.csv   média e desvio-padrão por experiência e valor
    summary.md    as mesmas médias, numa tabela legível
    raw/          os ficheiros de cada corrida (cenário usado, log, CSV)

Se for interrompido, volta-se a correr o mesmo comando: as corridas já
feitas são saltadas (use --force para as repetir).
"""

import argparse
import csv
import glob
import json
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Métricas que entram no resumo, pela ordem das colunas.
SUMMARY_METRICS = [
    "tp", "fp", "fn", "tn", "undetected", "precision", "recall",
    "mean_t_verdict_s", "mean_t_dispatch_s", "mean_t_ambulance_arrival_s",
    "mean_t_cpr_accepted_s", "wasted_dispatches", "wasted_busy_s",
    "recalled_dispatches", "real_cancelled", "real_unserved",
    "messages_total",
]

# Métricas mostradas no summary.md (as outras ficam só no CSV).
MARKDOWN_METRICS = [
    ("tp", "TP"), ("fp", "FP"), ("fn", "FN"), ("tn", "TN"),
    ("real_unserved", "Reais sem ambulância"),
    ("mean_t_dispatch_s", "Despacho (s)"),
    ("mean_t_ambulance_arrival_s", "Chegada (s)"),
    ("wasted_dispatches", "Unidades gastas"),
    ("messages_total", "Mensagens"),
]


# ----------------------------------------------------------------------
# Parâmetros
# ----------------------------------------------------------------------

def parse_value(text):
    """
    Converte o texto de um valor para o tipo certo.

    Args:
        text (str): Por exemplo "0.65", "true" ou "abc".

    Returns:
        Any: O valor lido como JSON ("0.65" -> 0.65), ou o próprio texto se
            não for JSON válido.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def set_path(cfg, path, value):
    """
    Escreve um valor num cenário, dado o caminho com pontos.

    Os números escolhem um elemento de uma lista ("witnesses.0.x") e o "*"
    aplica-se a todos os elementos ("witnesses.*.error_rate"). O último
    campo é criado se ainda não existir, o que permite variar parâmetros
    que o cenário não escreve (e por isso usam o valor por defeito). Um
    erro de escrita nesse último nome não dá erro: o campo é ignorado.

    Args:
        cfg (dict): O cenário, alterado no próprio sítio.
        path (str): Caminho, por exemplo "verification.threshold".
        value (Any): Valor a escrever.

    Raises:
        KeyError: Se faltar algum nível intermédio do caminho (por exemplo
            a secção "verification").
    """
    keys = path.split(".")

    def apply(node, rest):
        key = rest[0]
        last = len(rest) == 1

        if isinstance(node, list):
            items = range(len(node)) if key == "*" else [int(key)]
            for i in items:
                if last:
                    node[i] = value
                else:
                    apply(node[i], rest[1:])
            return

        if key not in node and not last:
            raise KeyError(f"O caminho '{path}' não existe no cenário.")
        if last:
            node[key] = value
        else:
            apply(node[key], rest[1:])

    apply(cfg, keys)


def slug(value):
    """
    Devolve um texto de um valor que serve de nome de pasta.

    Args:
        value (Any): Valor do parâmetro.

    Returns:
        str: Por exemplo 0.65 -> "0.65".
    """
    return str(value).replace("/", "_").replace(" ", "_")


# ----------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------

def server_is_up(cfg):
    """
    Confirma que o servidor XMPP do cenário está a aceitar ligações.

    Args:
        cfg (dict): O cenário (traz o domínio do servidor).

    Returns:
        bool: True se der para ligar à porta 5222.
    """
    host = cfg["server"]["domain"]
    try:
        with socket.create_connection((host, 5222), timeout=2):
            return True
    except OSError:
        return False


def run_one(scenario_path, run_dir, param, value, seed, timeout):
    """
    Corre uma simulação com o parâmetro e a semente dados.

    Args:
        scenario_path (Path): Cenário de base.
        run_dir (Path): Pasta onde guardar o cenário usado, o log e o CSV.
        param (str): Caminho do parâmetro a variar.
        value (Any): Valor do parâmetro nesta corrida.
        seed (int): Semente da corrida.
        timeout (float): Segundos máximos de corrida.

    Returns:
        bool: True se a corrida terminou bem e guardou métricas.
    """
    cfg = json.loads(scenario_path.read_text(encoding="utf-8"))
    set_path(cfg, param, value)
    cfg.setdefault("run", {})["seed"] = seed

    run_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = run_dir / "cenario.json"
    cfg_path.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    command = [
        sys.executable, str(HERE / "main.py"), str(cfg_path),
        "--metrics", str(run_dir),
    ]

    with open(run_dir / "log.txt", "w", encoding="utf-8") as log:
        try:
            result = subprocess.run(
                command, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return False

    return result.returncode == 0 and (run_dir / "runs.csv").exists()


# ----------------------------------------------------------------------
# Resultados
# ----------------------------------------------------------------------

def to_number(text):
    """
    Converte uma célula de CSV num número.

    Args:
        text (str): Conteúdo da célula.

    Returns:
        float | None: O número, ou None se a célula estiver vazia.
    """
    if text in ("", None, "None"):
        return None
    return float(text)


def collect(out_dir, plan):
    """
    Junta o runs.csv de cada corrida numa só lista de linhas.

    Args:
        out_dir (Path): Pasta de resultados.
        plan (list[dict]): As corridas planeadas.

    Returns:
        list[dict]: Uma linha por corrida concluída, com as colunas
            "experiment", "param", "value" e "seed" à frente das métricas.
    """
    rows = []
    for item in plan:
        path = item["dir"] / "runs.csv"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append({
                    "experiment": item["experiment"],
                    "param": item["param"],
                    "value": item["value"],
                    "seed": item["seed"],
                    **{k: row[k] for k in row if k not in
                       ("scenario", "seed")},
                })
    return rows


def summarise(rows):
    """
    Calcula a média e o desvio-padrão de cada métrica, por experiência e valor.

    Os valores em falta (por exemplo, sem nenhum tempo medido) ficam de
    fora da média.

    Args:
        rows (list[dict]): Resultado de collect().

    Returns:
        list[dict]: Uma linha por (experiência, valor), com "n" corridas e
            as colunas "<métrica>_mean" e "<métrica>_std".
    """
    groups = {}
    for row in rows:
        groups.setdefault((row["experiment"], row["value"]), []).append(row)

    summary = []
    for (experiment, value), group in groups.items():
        line = {"experiment": experiment, "value": value, "n": len(group)}
        for metric in SUMMARY_METRICS:
            values = [
                v for v in (to_number(r.get(metric)) for r in group)
                if v is not None
            ]
            line[f"{metric}_mean"] = (
                round(statistics.mean(values), 3) if values else None
            )
            line[f"{metric}_std"] = (
                round(statistics.stdev(values), 3) if len(values) > 1
                else (0.0 if values else None)
            )
        summary.append(line)
    return summary


def write_csv(path, rows):
    """
    Escreve uma lista de dicionários num CSV.

    Args:
        path (Path): Ficheiro a escrever.
        rows (list[dict]): Linhas, todas com as mesmas chaves.
    """
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def cell(line, metric):
    """
    Formata "média ± desvio" de uma métrica para a tabela.

    Args:
        line (dict): Linha do resumo.
        metric (str): Nome da métrica.

    Returns:
        str: Por exemplo "1.20 ± 0.45", ou "-" se não houve medida.
    """
    mean = line[f"{metric}_mean"]
    std = line[f"{metric}_std"]
    if mean is None:
        return "-"
    return f"{mean:.2f} ± {std:.2f}"


def write_markdown(path, summary, param):
    """
    Escreve o resumo numa tabela legível, uma por experiência.

    Args:
        path (Path): Ficheiro a escrever.
        summary (list[dict]): Resultado de summarise().
        param (str): Parâmetro que foi variado.
    """
    lines = [
        "# Resultados das experiências", "",
        f"Parâmetro variado: `{param}`. Cada célula é a média ± desvio-padrão "
        "das corridas (sementes). TP/FP/FN/TN são verdadeiros e falsos "
        "positivos e negativos.", "",
    ]

    experiments = []
    for line in summary:
        if line["experiment"] not in experiments:
            experiments.append(line["experiment"])

    for experiment in experiments:
        lines += [f"## {experiment}", ""]
        header = ["Valor", "Corridas"] + [label for _, label in MARKDOWN_METRICS]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))

        group = [s for s in summary if s["experiment"] == experiment]
        for line in group:
            cells = [str(line["value"]), str(line["n"])] + [
                cell(line, metric) for metric, _ in MARKDOWN_METRICS
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------
# Programa
# ----------------------------------------------------------------------

def main():
    """
    Lê a linha de comandos, corre as experiências e escreve os resultados.
    """
    parser = argparse.ArgumentParser(
        description="Corre cenários várias vezes, variando um parâmetro."
    )
    parser.add_argument(
        "scenarios", nargs="+",
        help="cenários JSON (aceita * mesmo no Windows)"
    )
    parser.add_argument(
        "--param", default="verification.threshold",
        help="caminho do parâmetro a variar (padrão: %(default)s)"
    )
    parser.add_argument(
        "--values", default="0.5,0.65,0.8",
        help="valores do parâmetro, separados por vírgulas "
             "(padrão: %(default)s)"
    )
    parser.add_argument(
        "--seeds", type=int, default=5,
        help="número de sementes por valor, 1..N (padrão: %(default)s)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("resultados"),
        help="pasta de resultados (padrão: %(default)s)"
    )
    parser.add_argument(
        "--timeout", type=float, default=180,
        help="segundos máximos por corrida (padrão: %(default)s)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="mostra o plano e não corre nada"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="repete corridas que já têm resultados"
    )
    args = parser.parse_args()

    paths = []
    for pattern in args.scenarios:
        found = sorted(glob.glob(pattern)) or [pattern]
        paths += [Path(p) for p in found]

    values = [parse_value(v) for v in args.values.split(",")]
    seeds = range(1, args.seeds + 1)
    out_dir = args.out.resolve()

    plan = []
    for path in paths:
        if not path.exists():
            sys.exit(f"Cenário não encontrado: {path}")
        for value in values:
            for seed in seeds:
                plan.append({
                    "scenario": path.resolve(),
                    "experiment": path.stem,
                    "param": args.param,
                    "value": value,
                    "seed": seed,
                    "dir": out_dir / "raw" / path.stem
                           / f"{slug(args.param)}={slug(value)}"
                           / f"seed{seed}",
                })

    print(
        f"{len(paths)} cenário(s) x {len(values)} valor(es) de "
        f"'{args.param}' x {len(seeds)} semente(s) = {len(plan)} corridas."
    )

    if args.dry_run:
        for item in plan:
            print(f"  {item['experiment']}  {args.param}={item['value']}  "
                  f"seed={item['seed']}")
        return

    first = json.loads(paths[0].read_text(encoding="utf-8"))
    if not server_is_up(first):
        sys.exit(
            "Não consigo ligar ao servidor XMPP. Abra outro terminal e "
            "corra: spade run --memory"
        )

    failed = []
    started = time.monotonic()
    done_now = 0

    for number, item in enumerate(plan, 1):
        label = (f"[{number}/{len(plan)}] {item['experiment']} "
                 f"{args.param}={item['value']} seed={item['seed']}")

        if (item["dir"] / "runs.csv").exists() and not args.force:
            print(f"{label}: já feita, salto.")
            continue

        if item["dir"].exists():
            shutil.rmtree(item["dir"])

        began = time.monotonic()
        ok = run_one(
            item["scenario"], item["dir"], args.param, item["value"],
            item["seed"], args.timeout
        )
        done_now += 1

        remaining = len(plan) - number
        average = (time.monotonic() - started) / done_now
        print(f"{label}: {'ok' if ok else 'FALHOU'} "
              f"({time.monotonic() - began:.0f}s; faltam ~"
              f"{remaining * average / 60:.0f} min)")

        if not ok:
            failed.append(label)

    rows = collect(out_dir, plan)
    summary = summarise(rows)
    write_csv(out_dir / "runs.csv", rows)
    write_csv(out_dir / "summary.csv", summary)
    write_markdown(out_dir / "summary.md", summary, args.param)

    print(f"\n{len(rows)} corridas guardadas em {out_dir}")
    print("  runs.csv, summary.csv e summary.md")
    if failed:
        print(f"\n{len(failed)} corrida(s) falharam:")
        for label in failed:
            print(f"  {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
