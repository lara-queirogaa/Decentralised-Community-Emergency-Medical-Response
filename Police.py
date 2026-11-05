import asyncio
import json
import math

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour
from spade.message import Message


async def send_json(behaviour, jid, data):
    """
    Envia uma mensagem JSON a partir de um comportamento.

    Args:
        behaviour (spade.behaviour.CyclicBehaviour): Comportamento que faz o
            envio.
        jid (str): JID do destinatário.
        data (dict): Conteúdo da mensagem, serializado para JSON.
    """
    msg = Message(to=str(jid))
    msg.set_metadata("performative", "inform")
    msg.body = json.dumps(data)
    await behaviour.send(msg)


class PoliceAgent(Agent):
    """
    Viatura de patrulha: só obedece ao dispatcher.

    Depois de uma ocorrência fica onde terminou, e é daí que o dispatcher a
    escolhe da próxima vez.

    Attributes:
        environment (Environment | None): Ambiente onde vive. É preenchido
            por Environment.add_agent.
        unit_id (str): Identificador da unidade.
        location (tuple[float, float]): Posição atual.
        dispatcher_jid (str): JID do dispatcher.
        speed (float): Velocidade, em unidades de custo por segundo.
        scene_time (float): Segundos que fica no local.
        available (bool): True se está livre.
        current_incident (str | None): Incidente a que responde.
        cancelled (bool): True se o dispatcher cancelou a missão atual.
    """

    # Preenchido pelo Environment.add_agent
    environment = None

    def __init__(
        self, jid, password, unit_id, patrol_location,
        dispatcher_jid, speed=2.0, scene_time=5.0
    ):
        """
        Cria a viatura, livre e no ponto de patrulha.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Palavra-passe da conta XMPP.
            unit_id (str): Identificador da unidade.
            patrol_location (tuple[float, float]): Posição inicial de patrulha.
            dispatcher_jid (str): JID do dispatcher.
            speed (float): Velocidade, em unidades de custo por segundo.
            scene_time (float): Segundos que fica no local.
        """
        super().__init__(jid, password)
        self.unit_id = unit_id
        self.location = tuple(patrol_location)
        self.dispatcher_jid = dispatcher_jid
        self.speed = speed            # unidades de distância por segundo
        self.scene_time = scene_time  # segundos no local
        self.available = True
        self.current_incident = None
        self.cancelled = False

    def travel_time(self, origin, destination):
        """
        Calcula o tempo de viagem entre dois pontos.

        Com ambiente, segue a rota real (estradas cortadas e trânsito). Sem
        ambiente, ou sem rota possível, usa a linha reta.

        Args:
            origin (tuple[float, float]): Ponto de partida.
            destination (tuple[float, float]): Ponto de chegada.

        Returns:
            float: Tempo em segundos.
        """
        if self.environment is not None:
            needed = self.environment.travel_time(
                origin, destination, self.speed
            )
            if not math.isinf(needed):
                return needed
            print(f"[{self.unit_id}] Sem rota. A usar linha reta.")

        return math.dist(origin, destination) / self.speed

    def status_message(self, status, incident_id=None):
        """
        Cria a mensagem de estado enviada ao dispatcher.

        Args:
            status (str): "en_route", "on_scene", "available" ou "rejected".
            incident_id (str | None): Incidente a que o estado se refere.

        Returns:
            dict: Mensagem "unit_status", com o estado, a disponibilidade e a
                localização atual.
        """
        return {
            "message_type": "unit_status",
            "unit_id": self.unit_id,
            "kind": "police",
            "status": status,
            "available": self.available,
            "incident_id": incident_id,
            "location": [
                round(self.location[0], 2),
                round(self.location[1], 2)
            ]
        }

    async def wait_or_cancel(self, seconds):
        """
        Espera um tempo, mas pára se o dispatcher cancelar a missão.

        Args:
            seconds (float): Tempo a esperar.

        Returns:
            tuple[bool, float]: (terminou sem ser cancelada, segundos
                decorridos).
        """
        elapsed = 0.0

        while elapsed < seconds:
            if self.cancelled:
                return False, elapsed

            step = min(0.2, seconds - elapsed)
            await asyncio.sleep(step)
            elapsed += step

        return not self.cancelled, elapsed

    class ReceiveOrdersBehaviour(CyclicBehaviour):
        """
        Comportamento cíclico que recebe as ordens do dispatcher.

        Aceita "dispatch_order" quando está livre (e recusa se estiver
        ocupada) e trata "dispatch_cancel".

        Attributes:
            agent (PoliceAgent): Agente a que o comportamento pertence.
        """

        async def run(self):
            """
            Espera uma mensagem durante 1 segundo e trata-a: arranca uma missão ou
            cancela a atual.
            """
            msg = await self.receive(timeout=1)
            if msg is None:
                return

            try:
                data = json.loads(msg.body)
            except (json.JSONDecodeError, TypeError):
                return

            agent = self.agent
            kind = data.get("message_type")
            incident_id = data.get("incident_id")

            if kind == "dispatch_order":
                location = data.get("location")

                if not incident_id or not location:
                    return

                if agent.current_incident == incident_id:
                    return  # ordem repetida

                if not agent.available:
                    print(
                        f"\n--- POLICE {agent.unit_id} ---\n"
                        f"Ocupada. Ordem para {incident_id} recusada."
                    )
                    await send_json(
                        self,
                        agent.dispatcher_jid,
                        agent.status_message("rejected", incident_id)
                    )
                    return

                agent.available = False
                agent.current_incident = incident_id
                agent.cancelled = False

                print(f"\n--- POLICE {agent.unit_id} ---")
                print(f"Ordem recebida para o incidente {incident_id}.")

                agent.add_behaviour(
                    agent.MissionBehaviour(
                        incident_id, tuple(location)
                    )
                )

            elif kind == "dispatch_cancel":
                if (
                    incident_id
                    and incident_id == agent.current_incident
                ):
                    agent.cancelled = True
                    print(f"\n--- POLICE {agent.unit_id} ---")
                    print(f"Missão {incident_id} cancelada.")

    class MissionBehaviour(OneShotBehaviour):
        """
        Comportamento de execução única que cumpre uma missão: ir ao local e
        ficar lá.

        Attributes:
            agent (PoliceAgent): Agente a que o comportamento pertence.
            incident_id (str): Incidente da missão.
            destination (tuple[float, float]): Local do incidente.
        """

        def __init__(self, incident_id, destination):
            """
            Cria a missão.

            Args:
                incident_id (str): Incidente da missão.
                destination (tuple[float, float]): Local do incidente.
            """
            super().__init__()
            self.incident_id = incident_id
            self.destination = destination

        async def report(self, status):
            """
            Avisa o dispatcher do estado atual da missão.

            Args:
                status (str): "en_route", "on_scene" ou "available".
            """
            agent = self.agent
            await send_json(
                self,
                agent.dispatcher_jid,
                agent.status_message(status, self.incident_id)
            )

        async def run(self):
            """
            Cumpre a missão: viaja até ao local e fica lá scene_time segundos. A
            viatura fica livre onde terminou.

            Se for cancelada a meio da viagem, fica no ponto a que já tinha
            chegado.
            """
            agent = self.agent
            start = agent.location

            await self.report("en_route")
            print(f"[{agent.unit_id}] A caminho do local.")

            travel = agent.travel_time(start, self.destination)
            ok, elapsed = await agent.wait_or_cancel(travel)

            if ok:
                agent.location = self.destination
                print(f"[{agent.unit_id}] Chegou ao local.")
                await self.report("on_scene")

                await agent.wait_or_cancel(agent.scene_time)
            else:
                # Cancelada a meio: fica no ponto a que já tinha chegado.
                fraction = elapsed / travel if travel > 0 else 1.0
                agent.location = (
                    start[0] + (self.destination[0] - start[0]) * fraction,
                    start[1] + (self.destination[1] - start[1]) * fraction
                )

            agent.available = True
            agent.current_incident = None
            agent.cancelled = False

            print(
                f"[{agent.unit_id}] Livre em "
                f"({agent.location[0]:.2f}, {agent.location[1]:.2f})."
            )
            await self.report("available")

    async def setup(self):
        """
        Arranca o agente e ativa a receção de ordens.
        """
        print(f"Police {self.unit_id} iniciada.")
        self.add_behaviour(self.ReceiveOrdersBehaviour())