import json
import uuid

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour, CyclicBehaviour
from spade.message import Message


class WitnessAgent(Agent):
    """
    Testemunha: uma pessoa que está perto de um acidente.

    Pode reportar acidentes que vê (tipo 2), confirmar ou negar alertas de
    smartwatch (tipo 1) e fazer reanimação (RCP) a pedido do dispatcher.
    Tem uma reputação que sobe e desce conforme os seus alertas se
    confirmam ou não.

    Attributes:
        environment (Environment | None): Ambiente onde vive. É preenchido
            por Environment.add_agent.
        witness_id (str): Identificador da testemunha.
        location (tuple[int, int]): Posição (x, y) da testemunha.
        verification_jid (str): JID do agente de verificação.
        dispatcher_jid (str | None): JID do dispatcher.
        confidence (float): Confiança, entre 0 e 1, no que observa.
        detect_on_start (bool): Se True, reporta um incidente ao arrancar.
        confirm_requests (bool): Resposta a pedidos de verificação quando
            não há ambiente. Com ambiente, a resposta vem do mundo.
        accept_cpr (bool): Se aceita fazer RCP quando o dispatcher pede.
        hits (int): Alertas desta testemunha que foram confirmados.
        false_alarms (int): Alertas desta testemunha que eram falsos.
        processed_feedback (set[str]): Incidentes de que já recebeu
            feedback, para não contar duas vezes.
        accident_location (tuple[int, int] | None): Onde está o acidente
            que vê, quando é diferente da sua posição.
        vitals (dict | None): Sinais vitais que observa na vítima.
    """

    # Preenchido pelo Environment.add_agent
    environment = None

    def __init__(
        self, jid, password, witness_id, location,
        verification_jid, dispatcher_jid=None, confidence=0.9,
        detect_on_start=False, confirm_requests=True, accept_cpr=True
    ):
        """
        Cria a testemunha.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Palavra-passe da conta XMPP.
            witness_id (str): Identificador da testemunha.
            location (tuple[int, int]): Posição (x, y).
            verification_jid (str): JID do agente de verificação.
            dispatcher_jid (str | None): JID do dispatcher.
            confidence (float): Confiança, entre 0 e 1, no que observa.
            detect_on_start (bool): Se True, reporta um incidente ao arrancar.
            confirm_requests (bool): Resposta a pedidos de verificação quando
                não há ambiente.
            accept_cpr (bool): Se aceita fazer RCP quando lhe pedem.
        """
        super().__init__(jid, password)
        self.witness_id = witness_id
        self.location = location
        self.verification_jid = verification_jid
        self.dispatcher_jid = dispatcher_jid
        self.confidence = confidence
        self.detect_on_start = detect_on_start
        self.confirm_requests = confirm_requests  # só sem Environment
        self.accept_cpr = accept_cpr
        self.hits = 0
        self.false_alarms = 0
        self.processed_feedback = set()

        # Preenchidos pelo Environment quando a testemunha vê um acidente
        self.accident_location = None   # onde está o acidente
        self.vitals = None              # o que a testemunha observa

    def get_reputation(self):
        """
        Calcula a reputação da testemunha.

        Usa uma média com suavização: começa em 0.5 e aproxima-se de 1 ou 0
        conforme os alertas se confirmam ou não.

        Returns:
            float: Reputação, entre 0 e 1.
        """
        return (self.hits + 1) / (
            self.hits + self.false_alarms + 2
        )

    class DetectIncidentBehaviour(OneShotBehaviour):
        """
        Comportamento de execução única em que a testemunha reporta um
        acidente ao agente de verificação.

        Attributes:
            agent (WitnessAgent): Agente a que o comportamento pertence.
        """

        async def run(self):
            """
            Cria o incidente e envia-o ao agente de verificação.

            O incidente usa a localização do acidente, se existir, e a da própria
            testemunha caso contrário. Leva também os sinais vitais observados.
            """
            agent = self.agent
            incident_id = str(uuid.uuid4())

            # O acidente pode estar noutro sítio que não onde a
            # testemunha está.
            if agent.accident_location is not None:
                location = agent.accident_location
            else:
                location = agent.location

            incident = {
                "message_type": "incident",
                "incident_id": incident_id,
                "origin": "witness",
                "witness_id": agent.witness_id,
                "source_id": agent.witness_id,
                "confidence": agent.confidence,
                "reputation": agent.get_reputation(),
                "urgent": agent.confidence >= 0.9,
                "type": "possible_accident",
                "vitals": agent.vitals,
                "location": [
                    round(location[0], 1),
                    round(location[1], 1)
                ]
            }

            print("\n--- WITNESS ---")
            print(f"{agent.witness_id}: possível acidente em "
                  f"{tuple(incident['location'])}.")
            print(f"Confiança: {agent.confidence:.2f}")
            print(f"Reputação: {agent.get_reputation():.2f}")

            msg = Message(to=str(agent.verification_jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(incident)
            await self.send(msg)

    class ReceiveMessagesBehaviour(CyclicBehaviour):
        """
        Comportamento cíclico que trata as mensagens recebidas pela
        testemunha.

        Responde a "verify_request" (confirmar ou negar um alerta),
        "cpr_request" (aceitar ou recusar RCP) e "cpr_cancel", e atualiza a
        reputação com "reputation_feedback".

        Attributes:
            agent (WitnessAgent): Agente a que o comportamento pertence.
        """

        async def run(self):
            """
            Espera uma mensagem durante 1 segundo e trata-a conforme o tipo.

            Mensagens que não sejam JSON válido são ignoradas.
            """
            msg = await self.receive(timeout=1)
            if msg is None:
                return

            try:
                data = json.loads(msg.body)
            except (json.JSONDecodeError, TypeError):
                return

            agent = self.agent
            message_type = data.get("message_type")

            if message_type == "verify_request":
                # Com ambiente, a testemunha confirma se o que vê é um
                # acidente real. Sem ambiente, usa confirm_requests.
                if agent.environment is not None:
                    confirmed = agent.environment.is_real_accident(
                        data.get("location", agent.location)
                    )
                else:
                    confirmed = agent.confirm_requests

                response = {
                    "message_type": "witness_response",
                    "incident_id": data["incident_id"],
                    "witness_id": agent.witness_id,
                    "confirmed": confirmed,
                    "confidence": agent.confidence,
                    "reputation": agent.get_reputation(),
                    "location": [
                        round(agent.location[0], 1),
                        round(agent.location[1], 1)
                    ]
                }

                reply = Message(to=str(msg.sender))
                reply.set_metadata("performative", "inform")
                reply.body = json.dumps(response)
                await self.send(reply)

                print("\n--- WITNESS ---")
                print(f"Pedido recebido: {data['incident_id']}")
                print(f"Confirma: {confirmed}")

            elif message_type == "cpr_request":
                response = {
                    "message_type": "cpr_response",
                    "incident_id": data["incident_id"],
                    "witness_id": agent.witness_id,
                    "accepted": agent.accept_cpr
                }

                reply = Message(to=str(msg.sender))
                reply.set_metadata("performative", "inform")
                reply.body = json.dumps(response)
                await self.send(reply)

                print("\n--- WITNESS ---")
                if agent.accept_cpr:
                    print(f"{agent.witness_id}: a iniciar RCP. Instruções:")
                    for step in data.get("instructions", []):
                        print(f"  - {step}")
                else:
                    print(f"{agent.witness_id}: não posso fazer RCP.")

            elif message_type == "cpr_cancel":
                print("\n--- WITNESS ---")
                print(f"{agent.witness_id}: RCP cancelada pelo "
                      f"dispatcher (falso alarme).")

            elif message_type == "reputation_feedback":
                if data.get("source_id") != agent.witness_id:
                    return

                incident_id = data.get("incident_id")
                if (
                    not incident_id
                    or incident_id in agent.processed_feedback
                ):
                    return

                agent.processed_feedback.add(incident_id)

                if data.get("outcome") == "confirmed":
                    agent.hits += 1
                elif data.get("outcome") == "false_alarm":
                    agent.false_alarms += 1
                else:
                    return

                print("\n--- REPUTAÇÃO DA WITNESS ---")
                print(f"Reputação: {agent.get_reputation():.2f}")

    async def setup(self):
        """
        Arranca o agente: ativa a receção de mensagens e, se detect_on_start
        for True, o reporte de um incidente.
        """
        print(f"Witness {self.witness_id} iniciado.")
        self.add_behaviour(self.ReceiveMessagesBehaviour())

        if self.detect_on_start:
            self.add_behaviour(self.DetectIncidentBehaviour())