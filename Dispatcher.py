import json
import math
import time

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.message import Message


CPR_INSTRUCTIONS = [
    "Deite a vítima de costas numa superfície rígida.",
    "Coloque as mãos no centro do peito e comprima 5-6 cm, "
    "a um ritmo de 100-120 compressões por minuto.",
    "Se tiver formação: 30 compressões seguidas de 2 insuflações "
    "(boca a boca).",
    "Se não tiver formação: apenas compressões contínuas.",
    "Não pare até chegar a ambulância ou a vítima voltar a respirar."
]


class DispatcherAgent(Agent):
    """
    Central de emergência.

    Recebe pré-alertas e incidentes confirmados, envia a ambulância e a
    polícia com a melhor rota até ao local, pede a uma testemunha para
    fazer reanimação (RCP) quando a vítima não tem pulso nem respira, e
    cancela tudo se o alerta for um falso alarme.

    Attributes:
        environment (Environment | None): Ambiente usado para calcular
            rotas. É preenchido no main. Sem ele, usa a linha reta.
        witnesses (list[dict]): Testemunhas conhecidas, cada uma com "jid",
            "witness_id" e "location".
        units (dict[str, list[dict]]): Unidades por tipo ("ambulance" e
            "police"), cada uma com "unit_id", "jid" e "location".
        max_witness_distance (float): Distância máxima a que uma testemunha
            pode estar para ser chamada a fazer RCP.
        cpr_timeout (float): Segundos à espera da resposta de uma
            testemunha a um pedido de RCP.
        busy_units (dict[str, str]): Unidades ocupadas e o incidente a que
            respondem.
        incidents (dict[str, dict]): Estado de cada incidente, com
            "location", "units", "dispatched", "cancelled" e "cpr".
    """

    # Preenchido no main: dispatcher.environment = env
    environment = None

    def __init__(
        self, jid, password, witnesses, ambulances, police,
        max_witness_distance=5.0, cpr_timeout=10
    ):
        """
        Cria o dispatcher.

        A localização de cada unidade é atualizada quando ela reporta o seu
        estado.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Palavra-passe da conta XMPP.
            witnesses (list[dict]): Testemunhas conhecidas, cada uma com "jid",
                "witness_id" e "location".
            ambulances (list[dict]): Ambulâncias, cada uma com "unit_id", "jid"
                e "location".
            police (list[dict]): Viaturas de polícia, cada uma com "unit_id",
                "jid" e "location".
            max_witness_distance (float): Distância máxima a que uma testemunha
                pode estar para ser chamada a fazer RCP.
            cpr_timeout (float): Segundos à espera da resposta a um pedido de
                RCP.
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

    def route_distance(self, origin, destination):
        """
        Calcula a distância entre dois pontos, pela melhor rota.

        Estradas cortadas e trânsito são tidos em conta quando há ambiente.

        Args:
            origin (tuple[float, float]): Ponto de partida.
            destination (tuple[float, float]): Ponto de chegada.

        Returns:
            float: Custo da rota, infinito se não houver caminho. Sem
                ambiente, a distância em linha reta.
        """
        if self.environment is not None:
            return self.environment.route_cost(origin, destination)

        return math.dist(origin, destination)

    def find_nearest_unit(self, kind, location):
        """
        Escolhe a unidade livre com a rota mais barata até ao local.

        Unidades sem rota possível são ignoradas.

        Args:
            kind (str): "ambulance" ou "police".
            location (list[float]): Posição (x, y) do incidente.

        Returns:
            tuple: (unidade, custo da rota), ou (None, infinito) se nenhuma
                estiver disponível.
        """
        nearest = None
        nearest_cost = float("inf")

        for unit in self.units[kind]:
            if unit["unit_id"] in self.busy_units:
                continue

            cost = self.route_distance(unit["location"], location)

            if cost < nearest_cost:
                nearest = unit
                nearest_cost = cost

        return nearest, nearest_cost

    def find_witnesses_by_distance(self, location):
        """
        Lista as testemunhas que estão perto de um local.

        Usa a distância em linha reta, porque a testemunha vai a pé.

        Args:
            location (list[float]): Posição (x, y) do incidente.

        Returns:
            list[tuple[float, dict]]: Pares (distância, testemunha) dentro de
                max_witness_distance, da mais perto para a mais longe.
        """
        candidates = []

        for witness in self.witnesses:
            distance = math.dist(location, witness["location"])
            if distance <= self.max_witness_distance:
                candidates.append((distance, witness))

        candidates.sort(key=lambda item: item[0])
        return candidates

    @staticmethod
    def needs_cpr(incident):
        """
        Indica se a vítima precisa de reanimação (RCP).

        Só é preciso quando se sabe que não há pulso nem respiração. Valores
        desconhecidos (None) não disparam RCP.

        Args:
            incident (dict): Incidente, com "vitals" opcional.

        Returns:
            bool: True se a frequência cardíaca for 0 e a vítima não respirar.
        """
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
        """
        Comportamento cíclico do dispatcher.

        Trata pré-alertas, incidentes confirmados, cancelamentos, respostas de
        testemunhas a pedidos de RCP e estados das unidades.

        Attributes:
            agent (DispatcherAgent): Agente a que o comportamento pertence.
        """

        async def send_json(self, jid, data):
            """
            Envia uma mensagem JSON.

            Args:
                jid (str): JID do destinatário.
                data (dict): Conteúdo da mensagem, serializado para JSON.
            """
            msg = Message(to=str(jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(data)
            await self.send(msg)

        async def run(self):
            """
            Espera uma mensagem durante 1 segundo e trata-a conforme o tipo.

            No fim, verifica se algum pedido de RCP ficou sem resposta.
            """
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

        # emergência
        async def handle_emergency(self, kind, data):
            """
            Trata um pré-alerta ou um incidente confirmado.

            Despacha as unidades uma única vez por incidente (no pré-alerta ou, se
            este não existiu, na confirmação) e pede RCP se a vítima precisar.

            Args:
                kind (str): "urgent_pre_alert" ou "confirmed_incident".
                data (dict): Mensagem recebida, com "incident_id", "location" e,
                    opcionalmente, "vitals".
            """
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
            """
            Envia a ambulância e a polícia livres com a melhor rota até ao local.

            Marca as unidades escolhidas como ocupadas e envia-lhes a ordem.

            Args:
                incident_id (str): Identificador do incidente.
                state (dict): Estado do incidente.
            """
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
                        f"(ou sem rota) para {incident_id}."
                    )
                    continue

                agent.busy_units[unit["unit_id"]] = incident_id
                state["units"].append(
                    {"unit_id": unit["unit_id"], "jid": unit["jid"]}
                )

                straight = math.dist(unit["location"], state["location"])
                print(
                    f"[DISPATCHER] {label} {unit['unit_id']} enviada "
                    f"(custo de rota: {distance:.1f}; "
                    f"linha reta: {straight:.1f})."
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
            """
            Atualiza a localização e a disponibilidade de uma unidade.

            A unidade só volta a ficar livre quando ela própria diz que está
            "available".

            Args:
                data (dict): Mensagem "unit_status", com "unit_id", "kind",
                    "status" e "location".
            """
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

        # RCP
        async def start_cpr(self, incident_id, state):
            """
            Começa a procura de uma testemunha para fazer RCP.

            Args:
                incident_id (str): Identificador do incidente.
                state (dict): Estado do incidente.
            """
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
            """
            Pede RCP à próxima testemunha da fila, a mais próxima ainda por
            contactar.

            Se já não houver candidatas, o pedido fica por atribuir.

            Args:
                incident_id (str): Identificador do incidente.
                state (dict): Estado do incidente.
            """
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
            """
            Trata a resposta de uma testemunha a um pedido de RCP.

            Se aceitou, fica registado. Se recusou, pede à seguinte. Respostas
            tardias de testemunhas que já não são a atual são ignoradas.

            Args:
                response (dict): Mensagem "cpr_response", com "incident_id",
                    "witness_id" e "accepted".
            """
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
            """
            Passa à testemunha seguinte nos pedidos de RCP que ficaram sem
            resposta durante cpr_timeout segundos.
            """
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

        #cancelamento
        async def handle_cancel(self, data):
            """
            Cancela um incidente que era um falso alarme.

            Manda as unidades interromper a missão e a testemunha parar o RCP. As
            unidades continuam ocupadas até dizerem que estão livres, porque a
            ambulância ainda tem de voltar ao hospital.

            Args:
                data (dict): Mensagem "cancel_alert", com "incident_id".
            """
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
        """
        Arranca o agente e ativa a receção de mensagens.
        """
        print("DispatcherAgent iniciado.")
        self.add_behaviour(self.ReceiveBehaviour())