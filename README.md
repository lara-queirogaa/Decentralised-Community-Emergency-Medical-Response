# Resposta Médica de Emergência Comunitária e Descentralizada (SPADE)

Sistema multi-agente em [SPADE](https://spade-mas.readthedocs.io/) que simula a
resposta a uma paragem cardíaca ou acidente numa cidade: deteção do alerta,
verificação (real ou falso alarme), despacho de ambulância e polícia e
orientação de uma testemunha para fazer reanimação (RCP) até chegar socorro.

## Agentes

| Agente | Ficheiro | Papel |
|---|---|---|
| Smartwatch | `Smartwatch.py` | Deteta o acidente (tipo 1) e envia o alerta. Se a confiança for >= 0.9, o alerta é urgente e vai também ao dispatcher. |
| Testemunha | `Witness.py` | Reporta acidentes que vê (tipo 2), confirma ou nega alertas de smartwatch e faz RCP a pedido. |
| Verificação | `Verification.py` | Decide se o incidente é real, combinando fontes, confiança e reputação com um limiar. |
| Dispatcher | `Dispatcher.py` | Central de emergência: despacha unidades, organiza o RCP e cancela falsos alarmes. |
| Ambulância / Polícia | `Ambulance.py`, `Police.py` | Unidades que só obedecem ao dispatcher e cumprem a missão. |

O mapa, as rotas, o trânsito e as estradas cortadas estão em `Environment.py`
(não é um agente).

## Requisitos

- Python 3.10 a 3.14 (testado com 3.13)
- As dependências de `requirements.txt`: SPADE 4, networkx, pygame e greenlet

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Como correr

O SPADE comunica por XMPP, por isso é preciso um servidor a correr **antes** de
arrancar qualquer simulação. O próprio SPADE traz um.

**Terminal 1: servidor XMPP** (deixar sempre aberto)

```bash
spade run --memory
```

`--memory` guarda a base de dados em memória, assim cada vez que o servidor
arranca começa limpo.

**Terminal 2: escolher uma das opções**

1. **Interface gráfica com menu de todos os cenários:**

   ```bash
   python interface.py
   ```

2. **Interface gráfica de um cenário:**

   ```bash
   python interface.py scenarios/cenario01.json
   ```

   Teclas: `ESC`/`Q` sair, `R` reiniciar, `S` captura de ecrã, `L`
   mostrar/esconder o log.

3. **Só consola, sem interface** (escreve o log no terminal e o mapa no fim):

   ```bash
   python main.py scenarios/cenario01.json
   ```

4. **Sem janela, a guardar capturas de ecrã** (útil em servidores):

   ```bash
   python interface.py scenarios/cenario01.json --headless --shot 4,10,20
   ```

   Guarda imagens em `capturas/` (muda-se com `--shot-dir`).

Para parar uma simulação sem duração definida, `Ctrl+C`.

## Cenários

Os cenários estão em `scenarios/*.json`. Um JSON define tudo: mapa, hospitais,
trânsito, estradas cortadas, parâmetros dos agentes, o acidente e a duração.
Para testar outra situação basta escrever outro JSON, sem mexer no código.

| Grupo | Cenários |
|---|---|
| Base | 01 |
| Falsos alarmes | 02, 03, 05 |
| Limiar e confiança das fontes | 04, 07, 08, 09, 11 |
| Sem testemunha por perto | 06, 12 |
| Reanimação (RCP) | 10, 13, 14, 15, 16 |
| Rotas e recursos | 17, 18, 19, 20 |
| Mundo (trânsito, edifícios, carga) | 21, 22, 23, 24 |

Cada ficheiro tem uma `description` que explica o que testa e o resultado
esperado.

### Estrutura de um cenário

```json
{
  "name": "...", "description": "...",
  "server":       { "domain": "localhost", "password": "password123" },
  "map":          { "width": 16, "height": 16, "hospitals": [[0, 0]] },
  "world":        { "traffic_level": 0, "roadblocks": [] },
  "dispatcher":   { "max_witness_distance": 5.0, "cpr_timeout": 10 },
  "verification": { "max_distance": 5.0, "threshold": 0.65 },
  "ambulances":   [ { "unit_id": "amb_01", "name": "ambulance1", "location": [0, 0] } ],
  "police":       [ { "unit_id": "pol_01", "name": "police1", "location": [8, 4] } ],
  "witnesses":    [ { "witness_id": "witness_001", "name": "witness1", "location": [4, 3],
                      "confidence": 0.9, "accept_cpr": true } ],
  "smartwatches": [ { "sensor_id": "sensor_001", "name": "smartwatch1",
                      "location": [4, 4], "confidence": 0.95 } ],
  "accident":     { "type": "smartwatch", "smartwatch": "sensor_001", "position": [4, 4],
                    "vitals": { "heart_rate": 0, "breathing": false }, "real": true },
  "run":          { "duration_seconds": 20 }
}
```

- `accident.type` é `"smartwatch"` (tipo 1) ou `"witness"` (tipo 2). Com
  `"real": false` o smartwatch dá um falso alarme.
- Sem o campo `accident`, o cenário corre sem nenhum acidente.
- `domain` e `password` são usados para criar as contas XMPP dos agentes.
  O domínio tem de coincidir com o do servidor (`localhost` por defeito).

## Estrutura do projeto

```
main.py            lança um cenário na consola
interface.py       visualização pygame (liga-se por fora, sem alterar os agentes)
Environment.py     mapa, rotas, trânsito, estradas cortadas, acidentes
Smartwatch.py  Witness.py  Verification.py  Dispatcher.py
Ambulance.py   Police.py
scenarios/         cenários em JSON
```
