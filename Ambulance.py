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


class AmbulanceAgent(Agent):
    """
    Ambulância: só obedece ao dispatcher.

    Quando está livre está sempre no hospital. Ao receber uma ordem vai ao
    local, fica lá algum tempo e volta ao hospital, avisando o dispatcher
    de cada passo.

    Attributes:
        environment (Environment | None): Ambiente onde vive. É preenchido
            por Environment.add_agent.
        unit_id (str): Identificador da unidade.
        hospital_location (tuple[float, float]): Posição do hospital.
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
        self, jid, password, unit_id, hospital_location,
        dispatcher_jid, speed=2.0, scene_time=5.0
    ):
        """
        Cria a ambulância, livre e no hospital.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Palavra-passe da conta XMPP.
            unit_id (str): Identificador da unidade.
            hospital_location (tuple[float, float]): Posição do hospital.
            dispatcher_jid (str): JID do dispatcher.
            speed (float): Velocidade, em unidades de custo por segundo.
            scene_time (float): Segundos que fica no local.
        """
        super().__init__(jid, password)
        self.unit_id = unit_id
        self.hospital_location = tuple(hospital_location)
        self.location = self.hospital_location
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
            status (str): "en_route", "on_scene", "returning", "available" ou
                "rejected".
            incident_id (str | None): Incidente a que o estado se refere.

        Returns:
            dict: Mensagem "unit_status", com o estado, a disponibilidade e a
                localização atual.
        """
        return {
            "message_type": "unit_status",
            "unit_id": self.unit_id,
            "kind": "ambulance",
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
            agent (AmbulanceAgent): Agente a que o comportamento pertence.
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
                        f"\n--- AMBULANCE {agent.unit_id} ---\n"
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

                print(f"\n--- AMBULANCE {agent.unit_id} ---")
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
                    print(f"\n--- AMBULANCE {agent.unit_id} ---")
                    print(
                        f"Missão {incident_id} cancelada. "
                        f"A regressar ao hospital."
                    )

    class MissionBehaviour(OneShotBehaviour):
        """
        Comportamento de execução única que cumpre uma missão: ir ao local,
        ficar lá e voltar ao hospital.

        Attributes:
            agent (AmbulanceAgent): Agente a que o comportamento pertence.
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
                status (str): "en_route", "on_scene", "returning" ou "available".
            """
            agent = self.agent
            await send_json(
                self,
                agent.dispatcher_jid,
                agent.status_message(status, self.incident_id)
            )

        async def run(self):
            """
            Cumpre a missão: viaja até ao local, fica lá scene_time segundos e
            regressa ao hospital, onde volta a ficar livre.

            Se for cancelada a meio da viagem, volta pelo mesmo caminho.
            """
            agent = self.agent

            await self.report("en_route")
            print(f"[{agent.unit_id}] A caminho do local.")

            travel = agent.travel_time(
                agent.location, self.destination
            )
            ok, elapsed = await agent.wait_or_cancel(travel)

            if ok:
                agent.location = self.destination
                print(f"[{agent.unit_id}] Chegou ao local.")
                await self.report("on_scene")

                await agent.wait_or_cancel(agent.scene_time)

                return_time = agent.travel_time(
                    self.destination, agent.hospital_location
                )
            else:
                # Cancelada a meio da viagem: volta pelo mesmo caminho.
                return_time = elapsed

            await self.report("returning")
            print(f"[{agent.unit_id}] A regressar ao hospital.")
            await asyncio.sleep(return_time)

            agent.location = agent.hospital_location
            agent.available = True
            agent.current_incident = None
            agent.cancelled = False

            print(f"[{agent.unit_id}] No hospital. Disponível.")
            await self.report("available")

    async def setup(self):
        """
        Arranca o agente e ativa a receção de ordens.
        """
        print(f"Ambulance {self.unit_id} iniciada.")
        self.add_behaviour(self.ReceiveOrdersBehaviour())