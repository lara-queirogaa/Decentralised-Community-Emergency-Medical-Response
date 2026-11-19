"""
Recolha de métricas de desempenho de uma simulação.

O MetricsRecorder não altera nenhum agente. Intercepta as mensagens SPADE
trocadas entre eles (Container.send), guarda cada uma com o instante em que
foi enviada e, no fim da corrida, calcula as métricas a partir dessas
mensagens e da verdade do mundo (Environment) e das decisões da
Verification.

Utilização (uma corrida):

    rec = MetricsRecorder("cenario01", seed=1)
    rec.install()
    try:
        await main.run(cfg, metrics=rec)
    finally:
        rec.uninstall()
    resumo = rec.summary()      # {"run": {...}, "incidents": [...]}
    rec.save("resultados")      # escreve CSV

Também se pode usar na linha de comandos:

    python main.py scenarios/cenario01.json --metrics resultados

Todos os tempos estão em segundos de simulação (a simulação corre em tempo
real, 1 segundo de simulação = 1 segundo de relógio) e contam-se desde o
primeiro alerta do incidente.
"""

import csv
import json
import math
import time
from pathlib import Path

# Distância máxima entre um incidente reportado e um acidente do mundo para
# se considerar que são o mesmo. É o mesmo raio que o Environment usa.
MATCH_RADIUS = 2.0

INCIDENT_COLUMNS = [
    "scenario", "seed", "incident_id", "origin", "source_id", "urgent",
    "truth", "verdict", "classification", "score",
    "t_verdict_s", "t_dispatch_s", "t_ambulance_dispatch_s",
    "t_ambulance_arrival_s", "t_cpr_accepted_s",
    "cpr_requested", "cancelled",
    "wasted_dispatches", "wasted_busy_s", "recalled_dispatches",
    "real_cancelled", "messages",
]

RUN_COLUMNS = [
    "scenario", "seed", "duration_s", "incidents",
    "tp", "fp", "fn", "tn", "unknown", "undetected",
    "precision", "recall",
    "mean_t_verdict_s", "mean_t_dispatch_s", "mean_t_ambulance_arrival_s",
    "mean_t_cpr_accepted_s",
    "wasted_dispatches", "wasted_busy_s", "recalled_dispatches",
    "real_cancelled", "real_unserved", "messages_total",
]


def bare(jid):
    """
    Tira o recurso de um JID, "agente@dominio/recurso" -> "agente@dominio".

    Args:
        jid (str | None): JID completo.

    Returns:
        str: JID sem recurso, ou "" se não houver JID.
    """
    return str(jid).split("/")[0] if jid else ""


def mean(values):
    """
    Média de uma lista, ignorando os valores em falta.

    Args:
        values (list[float | None]): Valores, com None onde não há medida.

    Returns:
        float | None: A média arredondada, ou None se não houver valores.
    """
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 3)


class MetricsRecorder:
    """
    Regista as mensagens de uma corrida e calcula as métricas.

    Attributes:
        scenario (str): Nome do cenário, para identificar os resultados.
        seed (int | None): Semente da corrida, se houver.
        events (list[tuple]): Mensagens vistas, cada uma com (instante,
            remetente, destinatário, conteúdo JSON).
        env (Environment | None): Ambiente da corrida (a verdade do mundo).
        verification (VerificationAgent | None): Agente de verificação, de
            onde vêm as decisões tomadas.
        cpr_timeout (float | None): Segundos que o dispatcher espera por
            uma resposta a um pedido de RCP. Respostas mais tardias são
            ignoradas por ele, e por isso também aqui.
        t_end (float | None): Instante em que a corrida terminou.
    """

    def __init__(self, scenario="", seed=None):
        """
        Cria o recolhedor. O relógio começa a contar agora.

        Args:
            scenario (str): Nome do cenário.
            seed (int | None): Semente da corrida.
        """
        self.scenario = scenario
        self.seed = seed
        self.events = []
        self.env = None
        self.verification = None
        self.cpr_timeout = None
        self.t_end = None
        self.unit_by_jid = {}
        self._t0 = time.monotonic()
        self._original_send = None

    # Captura
    def now(self):
        """
        Segundos desde que o recolhedor foi criado.

        Returns:
            float: Tempo decorrido.
        """
        return time.monotonic() - self._t0

    def install(self):
        """
        Começa a registar todas as mensagens SPADE.

        Substitui temporariamente Container.send por uma versão que regista
        a mensagem e a entrega como antes. uninstall() repõe o original.
        """
        from spade.container import Container

        if self._original_send is not None:
            return

        self._original_send = Container.send
        recorder = self
        original = Container.send

        async def send(container, msg, behaviour=None):
            recorder.log(msg)
            return await original(container, msg, behaviour)

        Container.send = send

    def uninstall(self):
        """
        Pára de registar e repõe o Container.send original.
        """
        from spade.container import Container

        if self._original_send is not None:
            Container.send = self._original_send
            self._original_send = None

    def log(self, msg):
        """
        Guarda uma mensagem, se o corpo for JSON.

        Args:
            msg (spade.message.Message): Mensagem que vai ser enviada.
        """
        try:
            data = json.loads(msg.body)
        except (json.JSONDecodeError, TypeError):
            return

        if isinstance(data, dict):
            self.events.append(
                (self.now(), bare(msg.sender), bare(msg.to), data)
            )

    def attach(self, env, verification, dispatcher=None):
        """
        Liga o recolhedor ao ambiente, à verificação e ao dispatcher.

        Args:
            env (Environment): O ambiente (verdade do mundo e unidades).
            verification (VerificationAgent): O agente de verificação.
            dispatcher (DispatcherAgent | None): O dispatcher, de onde se
                lê o cpr_timeout.
        """
        self.env = env
        self.verification = verification
        if dispatcher is not None:
            self.cpr_timeout = dispatcher.cpr_timeout

    def mark_end(self):
        """
        Marca o fim da corrida (usado para unidades ainda ocupadas).
        """
        self.t_end = self.now()

    # Cálculo
    def _units(self):
        """
        Mapa JID -> (tipo, unit_id) das unidades do ambiente.

        Returns:
            dict[str, tuple[str, str]]: Por exemplo
                {"ambulance1@localhost": ("ambulance", "amb_01")}.
        """
        units = {}
        if self.env is None:
            return units

        for kind in ("ambulance", "police"):
            for agent in self.env.agents.get(kind, []):
                units[bare(agent.jid)] = (kind, agent.unit_id)
        return units

    def incidents(self):
        """
        Calcula as métricas de cada incidente.

        Um incidente aparece quando alguém o reporta (mensagem "incident").
        Os tempos contam-se desde esse alerta. A verdade do terreno e o
        veredicto vêm das decisões da Verification.

        Returns:
            list[dict]: Uma linha por incidente, com as colunas de
                INCIDENT_COLUMNS.
        """
        units = self._units()
        end = self.t_end if self.t_end is not None else self.now()
        decisions = {}
        if self.verification is not None:
            decisions = {
                d["incident_id"]: d for d in self.verification.decisions
            }

        raw = {}
        for t, sender, to, data in self.events:
            incident_id = data.get("incident_id")
            if not incident_id:
                continue

            info = raw.setdefault(incident_id, {
                "alert": None, "orders": [], "available": [],
                "ambulance_arrival": None, "cpr_accepted": None,
                "verdict": None, "cpr_requested": False,
                "cpr_requests": {},
                "cancelled": False, "recalled": 0, "messages": 0,
                "alert_data": None,
            })
            info["messages"] += 1
            kind = data.get("message_type")

            if kind == "incident" and info["alert"] is None:
                info["alert"] = t
                info["alert_data"] = data

            elif kind == "dispatch_order":
                info["orders"].append((t, to))

            elif kind == "dispatch_cancel":
                info["recalled"] += 1

            elif kind == "cancel_alert":
                info["cancelled"] = True

            elif kind == "unit_status":
                unit_kind = data.get("kind")
                status = data.get("status")
                if status == "available":
                    info["available"].append((t, sender))
                elif (
                    status == "on_scene" and unit_kind == "ambulance"
                    and info["ambulance_arrival"] is None
                ):
                    info["ambulance_arrival"] = t

            elif kind == "cpr_request":
                info["cpr_requested"] = True
                info["cpr_requests"][to] = t

            elif kind == "cpr_response":
                asked = info["cpr_requests"].get(sender)
                late = (
                    self.cpr_timeout is not None and asked is not None
                    and t - asked >= self.cpr_timeout
                )
                # Uma resposta depois do cpr_timeout é ignorada pelo
                # dispatcher, por isso não conta como RCP aceite.
                if (
                    data.get("accepted") and not late
                    and info["cpr_accepted"] is None
                ):
                    info["cpr_accepted"] = t

            elif kind == "reputation_feedback":
                if info["verdict"] is None:
                    info["verdict"] = t

        rows = []
        for incident_id, info in raw.items():
            if info["alert"] is None:
                continue

            alert = info["alert"]
            data = info["alert_data"]
            decision = decisions.get(incident_id)

            truth = None
            if decision is not None:
                truth = decision["truth"]
            elif self.env is not None:
                truth = self.env.truth_at(data.get("location", [0, 0]))

            verdict = decision["verdict"] if decision else None

            if verdict is None:
                classification = "undecided"
            elif truth is None:
                classification = "unknown"
            elif verdict and truth:
                classification = "TP"
            elif verdict and not truth:
                classification = "FP"
            elif truth:
                classification = "FN"
            else:
                classification = "TN"

            def since(t):
                return None if t is None else round(t - alert, 3)

            ambulance_orders = [
                t for t, to in info["orders"]
                if units.get(to, ("", ""))[0] == "ambulance"
            ]

            wasted_dispatches = 0
            wasted_busy = 0.0
            if truth is False:
                wasted_dispatches = len(info["orders"])
                for t_order, to in info["orders"]:
                    freed = [
                        t for t, sender in info["available"]
                        if sender == to and t >= t_order
                    ]
                    wasted_busy += (min(freed) if freed else end) - t_order

            rows.append({
                "scenario": self.scenario,
                "seed": self.seed,
                "incident_id": incident_id[:8],
                "origin": data.get("origin"),
                "source_id": data.get("source_id"),
                "urgent": bool(data.get("urgent")),
                "truth": (
                    "real" if truth else
                    "false" if truth is False else "unknown"
                ),
                "verdict": (
                    None if verdict is None else
                    "confirmed" if verdict else "rejected"
                ),
                "classification": classification,
                "score": decision["score"] if decision else None,
                "t_verdict_s": since(info["verdict"]),
                "t_dispatch_s": since(
                    min((t for t, _ in info["orders"]), default=None)
                ),
                "t_ambulance_dispatch_s": since(
                    min(ambulance_orders, default=None)
                ),
                "t_ambulance_arrival_s": since(info["ambulance_arrival"]),
                "t_cpr_accepted_s": since(info["cpr_accepted"]),
                "cpr_requested": info["cpr_requested"],
                "cancelled": info["cancelled"],
                "wasted_dispatches": wasted_dispatches,
                "wasted_busy_s": round(wasted_busy, 3),
                "recalled_dispatches": info["recalled"],
                "real_cancelled": bool(truth and info["cancelled"]),
                "messages": info["messages"],
            })

        rows.sort(key=lambda r: r["incident_id"])
        return rows

    def undetected(self, incidents):
        """
        Conta os acidentes reais que ninguém chegou a reportar.

        Um acidente do mundo conta como detetado se algum incidente
        reportado estiver a menos de MATCH_RADIUS dele.

        Args:
            incidents (list[dict]): Resultado de incidents().

        Returns:
            int: Número de acidentes reais sem nenhum incidente.
        """
        if self.env is None:
            return 0

        locations = [
            data.get("location") for _, _, _, data in self.events
            if data.get("message_type") == "incident"
            and data.get("location")
        ]

        missed = 0
        for accident in self.env.accidents:
            if not accident["real"]:
                continue
            seen = any(
                math.dist(accident["position"], loc) <= MATCH_RADIUS
                for loc in locations
            )
            if not seen:
                missed += 1
        return missed

    def summary(self):
        """
        Calcula as métricas da corrida inteira.

        As médias dos tempos só contam incidentes cuja verdade não é "falso
        alarme", porque o que interessa é quanto demora o socorro a quem
        precisa dele.

        real_unserved conta os acidentes reais a que nenhuma ambulância
        chegou (alerta rejeitado, sem unidades, sem rota ou a corrida
        acabou antes).

        Returns:
            dict: {"run": métricas da corrida (RUN_COLUMNS),
                "incidents": lista de incidentes, "messages_by_type":
                contagem de mensagens por tipo}.
        """
        incidents = self.incidents()
        count = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
        unknown = 0
        for row in incidents:
            if row["classification"] in count:
                count[row["classification"]] += 1
            else:
                unknown += 1

        useful = [r for r in incidents if r["truth"] != "false"]

        def ratio(a, b):
            return round(a / b, 3) if b else None

        by_type = {}
        for _, _, _, data in self.events:
            kind = data.get("message_type", "?")
            by_type[kind] = by_type.get(kind, 0) + 1

        end = self.t_end if self.t_end is not None else self.now()

        run = {
            "scenario": self.scenario,
            "seed": self.seed,
            "duration_s": round(end, 1),
            "incidents": len(incidents),
            "tp": count["TP"], "fp": count["FP"],
            "fn": count["FN"], "tn": count["TN"],
            "unknown": unknown,
            "undetected": self.undetected(incidents),
            "precision": ratio(count["TP"], count["TP"] + count["FP"]),
            "recall": ratio(count["TP"], count["TP"] + count["FN"]),
            "mean_t_verdict_s": mean([r["t_verdict_s"] for r in useful]),
            "mean_t_dispatch_s": mean([r["t_dispatch_s"] for r in useful]),
            "mean_t_ambulance_arrival_s": mean(
                [r["t_ambulance_arrival_s"] for r in useful]
            ),
            "mean_t_cpr_accepted_s": mean(
                [r["t_cpr_accepted_s"] for r in useful]
            ),
            "wasted_dispatches": sum(r["wasted_dispatches"] for r in incidents),
            "wasted_busy_s": round(
                sum(r["wasted_busy_s"] for r in incidents), 3
            ),
            "recalled_dispatches": sum(
                r["recalled_dispatches"] for r in incidents
            ),
            "real_cancelled": sum(r["real_cancelled"] for r in incidents),
            "real_unserved": sum(
                1 for r in incidents
                if r["truth"] == "real"
                and r["t_ambulance_arrival_s"] is None
            ),
            "messages_total": len(self.events),
        }

        return {
            "run": run,
            "incidents": incidents,
            "messages_by_type": dict(sorted(by_type.items())),
        }

    # Saída
    def save(self, folder):
        """
        Escreve os resultados em CSV, numa pasta.

        Cria (ou acrescenta a) dois ficheiros: runs.csv, com uma linha por
        corrida, e incidents.csv, com uma linha por incidente. Assim várias
        corridas seguidas ficam todas na mesma tabela.

        Args:
            folder (str | Path): Pasta onde guardar. É criada se não existir.

        Returns:
            dict: O resultado de summary().
        """
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        result = self.summary()

        append_csv(folder / "runs.csv", RUN_COLUMNS, [result["run"]])
        append_csv(
            folder / "incidents.csv", INCIDENT_COLUMNS, result["incidents"]
        )
        return result


def append_csv(path, columns, rows):
    """
    Acrescenta linhas a um CSV, escrevendo o cabeçalho se o ficheiro for novo.

    Args:
        path (Path): Ficheiro CSV.
        columns (list[str]): Nomes das colunas, por ordem.
        rows (list[dict]): Linhas a acrescentar.
    """
    is_new = not path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
