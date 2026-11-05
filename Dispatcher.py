import json
import math
import time

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.message import Message


CPR_INSTRUCTIONS = [
    "Deite a vítima de costas numa superfície rígida.",
    "Coloque as mãos no centro do peito e comprima, "
    "a um ritmo de 100-120 compressões por minuto.",
    "30 compressões seguidas de 2 insuflações "
    "(boca a boca).",
    "Não pare até chegar a ambulância ou a vítima voltar a respirar."
]


class DispatcherAgent(Agent):

    def __init__(
        self, jid, password, witnesses, ambulances, police,
        max_witness_distance=5.0, cpr_timeout=10
    ):
        """
        ambulances / police: listas de dicts
            {"unit_id": "amb_01",
             "jid": "ambulance1@localhost",
             "location": (x, y)}
        A localização é atualizada quando as unidades reportam o estado.
        """
        super().__init__(jid, password)
        self.witnesses = witnesses
        self.units = {
            "ambulance": ambulances,
            "police": police
        }
        self.max_witness_distance = max_witness_distance
        self.cpr_timeout = cpr_timeout
        self.busy_units = {}   # unit_id -> incident_id
        self.incidents = {}    # incident_id -> estado

    def find_nearest_unit(self, kind, location):
        nearest = None
        nearest_distance = float("inf")

        for unit in self.units[kind]:
            if unit["unit_id"] in self.busy_units:
                continue

            distance = math.dist(location, unit["location"])
            if distance < nearest_distance:
                nearest = unit
                nearest_distance = distance

        return nearest, nearest_distance

    def find_witnesses_by_distance(self, location):
        candidates = []

        for witness in self.witnesses:
            distance = math.dist(location, witness["location"])
            if distance <= self.max_witness_distance:
                candidates.append((distance, witness))

        candidates.sort(key=lambda item: item[0])
        return candidates

    @staticmethod
    def needs_cpr(incident):
        """RCP só quando se sabe que não há pulso nem respiração.
        Valores desconhecidos (None) não disparam RCP."""
        vitals = incident.get("vitals")
        if not vitals:
            return False

        heart_rate = vitals.get("heart_rate")
        breathing = vitals.get("breathing")

        return (
            heart_rate is not None
            and heart_rate <= 0
            and breathing is False
        )

    class ReceiveBehaviour(CyclicBehaviour):

        async def send_json(self, jid, data):
            msg = Message(to=str(jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(data)
            await self.send(msg)

        async def run(self):
            msg = await self.receive(timeout=1)

            if msg is not None:
                try:
                    data = json.loads(msg.body)
                except (json.JSONDecodeError, TypeError):
                    data = None

                if data:
                    kind = data.get("message_type")

                    if kind in ("urgent_pre_alert", "confirmed_incident"):
                        await self.handle_emergency(kind, data)

                    elif kind == "cancel_alert":
                        await self.handle_cancel(data)

                    elif kind == "cpr_response":
                        await self.handle_cpr_response(data)

                    elif kind == "unit_status":
                        self.handle_unit_status(data)

            await self.check_cpr_timeouts()

        # ---------- emergência ----------

        async def handle_emergency(self, kind, data):
            agent = self.agent
            incident_id = data.get("incident_id")

            if not incident_id:
                return

            state = agent.incidents.get(incident_id)
            if state is None:
                state = {
                    "location": data.get("location", [0, 0]),
                    "units": [],
                    "dispatched": False,
                    "cancelled": False,
                    "cpr": None
                }
                agent.incidents[incident_id] = state

            if state["cancelled"]:
                return

            if kind == "urgent_pre_alert":
                print(f"\n[DISPATCHER] Pré-alerta urgente: {incident_id}")
            else:
                print(
                    f"\n[DISPATCHER] Incidente confirmado: {incident_id} "
                    f"(pontuação {data.get('verification_score')})"
                )

            # Despacha logo no pré-alerta; na confirmação só se ainda
            # não tiver sido feito (ex.: incidente de origem witness).
            if not state["dispatched"]:
                state["dispatched"] = True
                await self.dispatch_units(incident_id, state)

            if state["cpr"] is None and agent.needs_cpr(data):
                print(
                    "[DISPATCHER] Vítima sem pulso e sem respiração. "
                    "A procurar testemunha para RCP."
                )
                await self.start_cpr(incident_id, state)

        async def dispatch_units(self, incident_id, state):
            agent = self.agent

            for kind, label in (
                ("ambulance", "Ambulância"),
                ("police", "Polícia")
            ):
                unit, distance = agent.find_nearest_unit(
                    kind, state["location"]
                )

                if unit is None:
                    print(
                        f"[DISPATCHER] Sem {label} disponível "
                        f"para {incident_id}."
                    )
                    continue

                agent.busy_units[unit["unit_id"]] = incident_id
                state["units"].append(
                    {"unit_id": unit["unit_id"], "jid": unit["jid"]}
                )

                print(
                    f"[DISPATCHER] {label} {unit['unit_id']} enviada "
                    f"(distância: {distance:.2f})."
                )

                await self.send_json(
                    unit["jid"],
                    {
                        "message_type": "dispatch_order",
                        "incident_id": incident_id,
                        "location": state["location"]
                    }
                )

        def handle_unit_status(self, data):
            agent = self.agent
            unit_id = data.get("unit_id")
            units = agent.units.get(data.get("kind"), [])

            for unit in units:
                if unit["unit_id"] != unit_id:
                    continue

                if data.get("location"):
                    unit["location"] = tuple(data["location"])

                status = data.get("status")
                print(f"[DISPATCHER] Unidade {unit_id}: {status}.")

                # A unidade só fica livre quando ela própria o diz.
                if status == "available":
                    agent.busy_units.pop(unit_id, None)

                return

        # ---------- RCP ----------

        async def start_cpr(self, incident_id, state):
            agent = self.agent
            candidates = agent.find_witnesses_by_distance(
                state["location"]
            )

            state["cpr"] = {
                "queue": candidates,
                "current": None,
                "sent_at": None,
                "status": "searching"
            }

            await self.ask_next_witness(incident_id, state)

        async def ask_next_witness(self, incident_id, state):
            cpr = state["cpr"]

            if not cpr["queue"]:
                cpr["status"] = "unassigned"
                cpr["current"] = None
                print(
                    f"[DISPATCHER] Nenhuma testemunha disponível para "
                    f"RCP em {incident_id}. Aguardar ambulância."
                )
                return

            distance, witness = cpr["queue"].pop(0)
            cpr["current"] = witness
            cpr["sent_at"] = time.monotonic()
            cpr["status"] = "requested"

            print(
                f"[DISPATCHER] A pedir RCP a {witness['witness_id']} "
                f"(distância: {distance:.2f})."
            )

            await self.send_json(
                witness["jid"],
                {
                    "message_type": "cpr_request",
                    "incident_id": incident_id,
                    "location": state["location"],
                    "instructions": CPR_INSTRUCTIONS
                }
            )

        async def handle_cpr_response(self, response):
            agent = self.agent
            incident_id = response.get("incident_id")
            state = agent.incidents.get(incident_id)

            if state is None or state["cancelled"]:
                return

            cpr = state["cpr"]
            if cpr is None or cpr["status"] != "requested":
                return

            current = cpr["current"]
            if current is None or (
                current["witness_id"] != response.get("witness_id")
            ):
                return  # resposta tardia de outra testemunha

            if response.get("accepted"):
                cpr["status"] = "accepted"
                print(
                    f"[DISPATCHER] {current['witness_id']} aceitou "
                    f"fazer RCP em {incident_id}."
                )
            else:
                print(
                    f"[DISPATCHER] {current['witness_id']} recusou RCP."
                )
                await self.ask_next_witness(incident_id, state)

        async def check_cpr_timeouts(self):
            agent = self.agent
            now = time.monotonic()

            for incident_id, state in list(agent.incidents.items()):
                cpr = state["cpr"]

                if (
                    cpr is not None
                    and cpr["status"] == "requested"
                    and now - cpr["sent_at"] >= agent.cpr_timeout
                ):
                    print(
                        f"[DISPATCHER] Sem resposta de "
                        f"{cpr['current']['witness_id']} ao pedido de RCP."
                    )
                    await self.ask_next_witness(incident_id, state)

        # ---------- cancelamento ----------

        async def handle_cancel(self, data):
            incident_id = data.get("incident_id")
            state = self.agent.incidents.get(incident_id)

            if state is None or state["cancelled"]:
                return

            state["cancelled"] = True

            print(f"\n[DISPATCHER] CANCELAR alerta: {incident_id}")
            print("Motivo: falso alarme.")

            # As unidades ficam ocupadas até dizerem que estão livres
            # (a ambulância ainda tem de voltar ao hospital).
            for unit in state["units"]:
                await self.send_json(
                    unit["jid"],
                    {
                        "message_type": "dispatch_cancel",
                        "incident_id": incident_id
                    }
                )
                print(
                    f"[DISPATCHER] Cancelamento enviado a "
                    f"{unit['unit_id']}."
                )

            cpr = state["cpr"]
            if cpr is not None and cpr["status"] in (
                "requested", "accepted"
            ):
                await self.send_json(
                    cpr["current"]["jid"],
                    {
                        "message_type": "cpr_cancel",
                        "incident_id": incident_id,
                        "reason": "false_alarm"
                    }
                )
                cpr["status"] = "cancelled"

    async def setup(self):
        print("DispatcherAgent iniciado.")
        self.add_behaviour(self.ReceiveBehaviour())