import json
import uuid

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour, CyclicBehaviour
from spade.message import Message


class SmartwatchAgent(Agent):
    """
    Smartwatch da própria vítima.

    Só deteta acidentes quando o Environment lho indica, através de
    trigger_smartwatch_accident. O alerta precisa depois da confirmação de
    uma testemunha. Tem uma reputação que sobe e desce conforme os seus
    alertas se confirmam ou não.

    Attributes:
        sensor_id (str): Identificador do sensor.
        location (tuple[int, int]): Posição (x, y) da vítima.
        verification_jid (str): JID do agente de verificação.
        dispatcher_jid (str): JID do dispatcher.
        confidence (float): Confiança, entre 0 e 1, na deteção. Com 0.9 ou
            mais, o alerta é urgente.
        vitals (dict): Sinais vitais da vítima, com "heart_rate" e
            "breathing".
        hits (int): Alertas deste smartwatch que foram confirmados.
        false_alarms (int): Alertas deste smartwatch que eram falsos.
        processed_feedback (set[str]): Incidentes de que já recebeu
            feedback, para não contar duas vezes.
    """

    def __init__(
        self, jid, password, sensor_id, location,
        verification_jid, dispatcher_jid, confidence=0.95,
        vitals=None
    ):
        """
        Cria o smartwatch.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Palavra-passe da conta XMPP.
            sensor_id (str): Identificador do sensor.
            location (tuple[int, int]): Posição (x, y) da vítima.
            verification_jid (str): JID do agente de verificação.
            dispatcher_jid (str): JID do dispatcher.
            confidence (float): Confiança, entre 0 e 1, na deteção.
            vitals (dict | None): Sinais vitais iniciais. Por omissão, uma
                pessoa saudável (72 bpm e a respirar).
        """
        super().__init__(jid, password)
        self.sensor_id = sensor_id
        self.location = location
        self.verification_jid = verification_jid
        self.dispatcher_jid = dispatcher_jid
        self.confidence = confidence
        self.vitals = vitals or {"heart_rate": 72, "breathing": True}
        self.hits = 0
        self.false_alarms = 0
        self.processed_feedback = set()

    def get_reputation(self):
        """
        Calcula a reputação do smartwatch.

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
        Comportamento de execução única em que o smartwatch deteta um acidente.

        Envia o incidente ao agente de verificação e, se for urgente, um
        pré-alerta ao dispatcher.

        Attributes:
            agent (SmartwatchAgent): Agente a que o comportamento pertence.
        """

        async def run(self):
            """
            Cria o incidente e envia-o à verificação e, se for urgente, ao
            dispatcher.
            """
            agent = self.agent
            incident_id = str(uuid.uuid4())

            incident = {
                "message_type": "incident",
                "incident_id": incident_id,
                "origin": "smartwatch",
                "sensor_id": agent.sensor_id,
                "source_id": agent.sensor_id,
                "confidence": agent.confidence,
                "reputation": agent.get_reputation(),
                "urgent": agent.confidence >= 0.9,
                "type": "possible_accident",
                "vitals": agent.vitals,
                "location": [
                    round(agent.location[0], 1),
                    round(agent.location[1], 1)
                ]
            }

            print("\n--- SMARTWATCH ---")
            print(f"Incidente: {incident_id}")
            print(f"Confiança: {agent.confidence:.2f}")
            print(f"Reputação: {agent.get_reputation():.2f}")
            print(f"Urgente: {incident['urgent']}")

            verification_msg = Message(
                to=str(agent.verification_jid)
            )
            verification_msg.set_metadata("performative", "inform")
            verification_msg.body = json.dumps(incident)
            await self.send(verification_msg)

            if incident["urgent"]:
                pre_alert = {
                    **incident,
                    "message_type": "urgent_pre_alert"
                }
                dispatcher_msg = Message(
                    to=str(agent.dispatcher_jid)
                )
                dispatcher_msg.set_metadata(
                    "performative", "inform"
                )
                dispatcher_msg.body = json.dumps(pre_alert)
                await self.send(dispatcher_msg)
                print("Pré-alerta urgente enviado ao dispatcher.")

    class ReceiveFeedbackBehaviour(CyclicBehaviour):
        """
        Comportamento cíclico que recebe o feedback de reputação.

        Attributes:
            agent (SmartwatchAgent): Agente a que o comportamento pertence.
        """

        async def run(self):
            """
            Espera uma mensagem durante 1 segundo e, se for feedback de reputação
            sobre este sensor ainda não contado, atualiza hits ou false_alarms.
            """
            msg = await self.receive(timeout=1)
            if msg is None:
                return

            try:
                data = json.loads(msg.body)
            except (json.JSONDecodeError, TypeError):
                return

            if data.get("message_type") != "reputation_feedback":
                return

            if data.get("source_id") != self.agent.sensor_id:
                return

            incident_id = data.get("incident_id")
            if (
                not incident_id
                or incident_id in self.agent.processed_feedback
            ):
                return

            self.agent.processed_feedback.add(incident_id)

            if data.get("outcome") == "confirmed":
                self.agent.hits += 1
            elif data.get("outcome") == "false_alarm":
                self.agent.false_alarms += 1
            else:
                return

    async def setup(self):
        """
        Arranca o agente e ativa a receção de feedback.

        Não deteta nada por si: os acidentes são provocados pelo Environment.
        """
        print(f"Smartwatch {self.sensor_id} iniciado.")
        self.add_behaviour(self.ReceiveFeedbackBehaviour())