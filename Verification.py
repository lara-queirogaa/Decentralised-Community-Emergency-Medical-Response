import json
import math
import time

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.message import Message


# Peso base de cada tipo de fonte (a testemunha vale mais).
# O peso final também depende da fiabilidade (reputação) de cada uma.
WITNESS_WEIGHT = 0.7
SENSOR_WEIGHT = 0.3


class VerificationAgent(Agent):
    """
    Agente que decide se um incidente é real ou um falso alarme.

    Para alertas de smartwatch (tipo 1) pede a confirmação da testemunha
    mais próxima e combina as duas fontes numa pontuação. Para alertas de
    testemunha (tipo 2) decide pela confiança e reputação dessa testemunha.
    Compara a pontuação com um limiar e mantém a reputação de cada fonte.

    Attributes:
        dispatcher_jid (str): JID do dispatcher.
        witnesses (list[dict]): Testemunhas conhecidas, cada uma com "jid",
            "witness_id" e "location".
        max_distance (float): Distância máxima a que uma testemunha pode
            estar para ser consultada.
        threshold (float): Pontuação mínima para confirmar um incidente.
        stats (dict[str, dict[str, int]]): Contagem de "hits" e
            "false_alarms" por fonte.
        pending (dict[str, dict]): Incidentes à espera da resposta de uma
            testemunha.
        finalized (set[str]): Incidentes já decididos.
    """

    def __init__(
        self, jid, password, dispatcher_jid,
        witnesses, max_distance=5.0, threshold=0.65
    ):
        """
        Cria o agente de verificação.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Palavra-passe da conta XMPP.
            dispatcher_jid (str): JID do dispatcher.
            witnesses (list[dict]): Testemunhas conhecidas, cada uma com "jid",
                "witness_id" e "location".
            max_distance (float): Distância máxima a que uma testemunha pode
                estar para ser consultada.
            threshold (float): Pontuação mínima para confirmar um incidente.
        """
        super().__init__(jid, password)
        self.dispatcher_jid = dispatcher_jid
        self.witnesses = witnesses
        self.max_distance = max_distance
        self.threshold = threshold
        self.stats = {}
        self.pending = {}
        self.finalized = set()

    def get_reputation(self, source_id):
        """
        Calcula a reputação de uma fonte.

        Usa uma média com suavização: começa em 0.5 para fontes novas.

        Args:
            source_id (str): Identificador do smartwatch ou da testemunha.

        Returns:
            float: Reputação, entre 0 e 1.
        """
        stats = self.stats.get(
            source_id, {"hits": 0, "false_alarms": 0}
        )
        hits = stats["hits"]
        false_alarms = stats["false_alarms"]
        return (hits + 1) / (hits + false_alarms + 2)

    def record_outcome(self, source_id, outcome):
        """
        Regista o resultado de um incidente na reputação de uma fonte.

        Args:
            source_id (str): Identificador do smartwatch ou da testemunha.
            outcome (str): "confirmed" ou "false_alarm".
        """
        if source_id not in self.stats:
            self.stats[source_id] = {
                "hits": 0,
                "false_alarms": 0
            }

        if outcome == "confirmed":
            self.stats[source_id]["hits"] += 1
        else:
            self.stats[source_id]["false_alarms"] += 1

    def find_nearest_witness(self, location):
        """
        Procura a testemunha mais próxima de um local.

        Args:
            location (list[float]): Posição (x, y) do incidente.

        Returns:
            tuple: (testemunha, distância) se a mais próxima estiver dentro de
                max_distance. Caso contrário, (None, distância).
        """
        nearest = None
        nearest_distance = float("inf")

        for witness in self.witnesses:
            distance = math.dist(
                location, witness["location"]
            )

            if distance < nearest_distance:
                nearest = witness
                nearest_distance = distance

        if nearest and nearest_distance <= self.max_distance:
            return nearest, nearest_distance

        return None, nearest_distance

    class ReceiveMessagesBehaviour(CyclicBehaviour):
        """
        Comportamento cíclico que recebe incidentes e respostas das
        testemunhas, decide e informa o dispatcher e as fontes.

        Também dá por terminado o pedido a uma testemunha que não responde em
        8 segundos, e decide só com o smartwatch.

        Attributes:
            agent (VerificationAgent): Agente a que o comportamento pertence.
        """

        async def send_feedback(
            self, jid, source_id, incident_id, outcome
        ):
            """
            Informa uma fonte do resultado do seu alerta, para atualizar a
            reputação.

            Args:
                jid (str): JID do destinatário.
                source_id (str): Identificador da fonte a quem o feedback diz
                    respeito.
                incident_id (str): Incidente a que o resultado se refere.
                outcome (str): "confirmed" ou "false_alarm".
            """
            feedback = {
                "message_type": "reputation_feedback",
                "incident_id": incident_id,
                "source_id": source_id,
                "outcome": outcome
            }

            msg = Message(to=str(jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(feedback)
            await self.send(msg)

        async def send_dispatcher_message(self, data):
            """
            Envia uma mensagem ao dispatcher.

            Args:
                data (dict): Conteúdo da mensagem, serializado para JSON.
            """
            msg = Message(to=str(self.agent.dispatcher_jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(data)
            await self.send(msg)

        async def run(self):
            """
            Espera uma mensagem durante 1 segundo e trata-a: um incidente novo ou
            uma resposta de uma testemunha.

            No fim, decide os incidentes cujo pedido a uma testemunha expirou.
            """
            agent = self.agent
            msg = await self.receive(timeout=1)

            if msg is not None:
                try:
                    data = json.loads(msg.body)
                except (json.JSONDecodeError, TypeError):
                    data = None

                if data:
                    kind = data.get("message_type")

                    if kind == "incident":
                        await self.process_incident(
                            data, str(msg.sender)
                        )

                    elif kind == "witness_response":
                        await self.process_witness_response(data)

            now = time.monotonic()

            for incident_id, pending in list(
                agent.pending.items()
            ):
                if now - pending["sent_at"] >= 8:
                    del agent.pending[incident_id]

                    print(
                        f"\nSem resposta da testemunha para "
                        f"{incident_id}. A decidir com o smartwatch."
                    )

                    incident = pending["incident"]
                    sensor_id = incident["source_id"]
                    sensor_rep = agent.get_reputation(sensor_id)
                    confidence = incident["confidence"]

                    score = confidence * (0.5 + 0.5 * sensor_rep)

                    await self.finalize_incident(
                        incident=incident,
                        score=score,
                        source_jid=pending["source_jid"],
                        witness_response=None,
                        witness_jid=None
                    )

        async def process_incident(self, incident, sender_jid):
            """
            Trata um incidente novo.

            Se foi reportado por uma testemunha (tipo 2), decide logo com a
            confiança e a reputação dela. Se veio de um smartwatch (tipo 1), pede a
            confirmação da testemunha mais próxima, ou decide só com o smartwatch
            quando não há ninguém perto.

            Args:
                incident (dict): Incidente recebido.
                sender_jid (str): JID de quem o enviou, a quem se devolve o
                    feedback de reputação.
            """
            agent = self.agent
            incident_id = incident.get("incident_id")

            if not incident_id or incident_id in agent.finalized:
                return

            origin = incident.get("origin")
            source_id = incident.get("source_id")
            confidence = float(incident.get("confidence", 0.0))
            location = incident.get("location", [0, 0])

            print("\n--- VERIFICATION ---")
            print(f"Incidente: {incident_id}")
            print(f"Origem: {origin}")

            # TIPO 2: reportado por uma testemunha. Não há smartwatch
            # nem segunda confirmação: decide a confiança e a
            # reputação da própria testemunha.
            if origin == "witness":
                reputation = agent.get_reputation(source_id)
                score = confidence * (0.5 + 0.5 * reputation)

                print(f"Reputação witness: {reputation:.2f}")
                print(f"Pontuação final: {score:.3f}")
                print(f"Limiar: {agent.threshold:.2f}")

                await self.finalize_incident(
                    incident=incident,
                    score=score,
                    source_jid=sender_jid,
                    witness_response=None,
                    witness_jid=None
                )
                return

            # TIPO 1: smartwatch. Precisa de uma testemunha próxima
            # que confirme ou negue.
            witness, distance = agent.find_nearest_witness(location)

            if witness is None:
                reputation = agent.get_reputation(source_id)
                score = confidence * (0.5 + 0.5 * reputation)

                print("Não há testemunha próxima.")
                await self.finalize_incident(
                    incident=incident,
                    score=score,
                    source_jid=sender_jid,
                    witness_response=None,
                    witness_jid=None
                )
                return

            print(
                f"A pedir verificação a {witness['witness_id']}; "
                f"distância: {distance:.2f}."
            )

            agent.pending[incident_id] = {
                "incident": incident,
                "source_jid": sender_jid,
                "witness": witness,
                "sent_at": time.monotonic()
            }

            request = {
                "message_type": "verify_request",
                "incident_id": incident_id,
                "location": location
            }

            request_msg = Message(to=witness["jid"])
            request_msg.set_metadata("performative", "inform")
            request_msg.body = json.dumps(request)
            await self.send(request_msg)

        async def process_witness_response(self, response):
            """
            Trata a resposta de uma testemunha a um pedido de verificação.

            A pontuação é a média ponderada da probabilidade de acidente de cada
            fonte. O peso de cada uma é o seu peso base (a testemunha pesa mais)
            vezes a sua fiabilidade, que depende da reputação. Se a testemunha
            nega, a sua probabilidade é 1 menos a confiança.

            Args:
                response (dict): Resposta da testemunha, com "confirmed",
                    "confidence" e "witness_id".
            """
            agent = self.agent
            incident_id = response.get("incident_id")
            pending = agent.pending.pop(incident_id, None)

            if pending is None or incident_id in agent.finalized:
                return

            incident = pending["incident"]
            sensor_id = incident["source_id"]
            sensor_rep = agent.get_reputation(sensor_id)
            sensor_conf = float(incident.get("confidence", 0.0))

            witness_id = response["witness_id"]
            witness_rep = agent.get_reputation(witness_id)
            witness_conf = float(response.get("confidence", 0.0))

            sensor_reliability = 0.5 + 0.5 * sensor_rep
            witness_reliability = 0.5 + 0.5 * witness_rep

            # Probabilidade de acidente segundo cada fonte
            sensor_p = sensor_conf

            if response.get("confirmed"):
                witness_p = witness_conf
            else:
                witness_p = 1 - witness_conf

            # Peso = tipo de fonte x fiabilidade
            sensor_w = SENSOR_WEIGHT * sensor_reliability
            witness_w = WITNESS_WEIGHT * witness_reliability

            score = (
                sensor_w * sensor_p + witness_w * witness_p
            ) / (sensor_w + witness_w)

            print("\n--- RESULTADO DA VERIFICAÇÃO ---")
            print(f"Reputação smartwatch: {sensor_rep:.2f}")
            print(f"Reputação witness: {witness_rep:.2f}")
            print(f"Pontuação final: {score:.3f}")
            print(f"Limiar: {agent.threshold:.2f}")
            print(f"Testemunha confirmou: {response['confirmed']}")

            witness_jid = pending["witness"]["jid"]

            await self.finalize_incident(
                incident=incident,
                score=score,
                source_jid=pending["source_jid"],
                witness_response=response,
                witness_jid=witness_jid
            )

        async def finalize_incident(
            self, incident, score, source_jid,
            witness_response, witness_jid
        ):
            """
            Dá o incidente por decidido.

            Compara a pontuação com o limiar, atualiza as reputações, envia o
            feedback às fontes e informa o dispatcher: um incidente confirmado, ou
            o cancelamento do pré-alerta no caso de um smartwatch. Uma testemunha
            que negou o alerta não é penalizada nem recompensada.

            Args:
                incident (dict): Incidente a decidir.
                score (float): Pontuação calculada, entre 0 e 1.
                source_jid (str): JID de quem reportou o incidente.
                witness_response (dict | None): Resposta da testemunha, se houve.
                witness_jid (str | None): JID da testemunha consultada, se houve.
            """
            agent = self.agent
            incident_id = incident["incident_id"]

            if incident_id in agent.finalized:
                return

            agent.finalized.add(incident_id)

            confirmed = score >= agent.threshold
            outcome = "confirmed" if confirmed else "false_alarm"
            source_id = incident["source_id"]

            agent.record_outcome(source_id, outcome)

            await self.send_feedback(
                source_jid, source_id, incident_id, outcome
            )

            if witness_response is not None:
                witness_id = witness_response["witness_id"]

                # Uma testemunha que negou o alerta de um
                # smartwatch não é penalizada nem recompensada.
                if witness_response.get("confirmed"):
                    agent.record_outcome(witness_id, outcome)

                    await self.send_feedback(
                        witness_jid,
                        witness_id,
                        incident_id,
                        outcome
                    )

            if confirmed:
                print(f"Incidente confirmado: {incident_id}")

                result = {
                    **incident,
                    "message_type": "confirmed_incident",
                    "verification_score": round(score, 3)
                }
                await self.send_dispatcher_message(result)

            else:
                print(f"Falso alarme: {incident_id}")

                # Só cancelar no dispatcher se o alerta urgente
                # já tiver sido enviado pelo smartwatch.
                if incident.get("urgent") and (
                    incident.get("origin") == "smartwatch"
                ):
                    cancellation = {
                        "message_type": "cancel_alert",
                        "incident_id": incident_id,
                        "reason": "false_alarm",
                        "verification_score": round(score, 3)
                    }
                    await self.send_dispatcher_message(cancellation)

            print(
                f"Reputação de {source_id}: "
                f"{agent.get_reputation(source_id):.2f}"
            )

            if witness_response is not None:
                witness_id = witness_response["witness_id"]
                print(
                    f"Reputação de {witness_id}: "
                    f"{agent.get_reputation(witness_id):.2f}"
                )

    async def setup(self):
        """
        Arranca o agente e ativa a receção de mensagens.
        """
        print("VerificationAgent iniciado.")
        self.add_behaviour(self.ReceiveMessagesBehaviour())