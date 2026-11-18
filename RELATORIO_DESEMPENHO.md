# Relatório de desempenho

Avaliação do sistema em **3 cenários contrastantes**, variando o **limiar de verificação** (`verification.threshold`: 0.5, 0.65 e 0.8). Cada combinação foi repetida 5 vezes com números de partida diferentes para o acaso (45 simulações). Os números aqui são a **média ± desvio-padrão** das 5 repetições e vêm de `resultados/summary.md` e `resultados/summary.csv`. Os dados de cada simulação estão em `resultados/raw/`.

Como repetir: `python run_experiments.py "experiments/*.json" --seeds 5`.

---

## 1. Objetivo

Perceber como o sistema se comporta quando muda a qualidade da informação e os recursos disponíveis, e que preço tem escolher um limiar mais exigente ou mais permissivo. O limiar é o valor mínimo da pontuação para aceitar um alerta como acidente real.

## 2. Cenários

| Cenário | Situação | Incidentes | Meios | Testemunhas | Por que é contrastante |
|---|---|---|---|---|---|
| **normal** | Cidade sem trânsito nem estradas cortadas | 6: 5 reais e 1 falso | 6 ambulâncias, 4 polícias | Fiáveis (erro 5%, confiança 0.85) | Referência |
| **hostil** | Mesma cidade, informação má | 6: 3 reais e 3 falsos (alguns com confiança alta) | 6 ambulâncias, 4 polícias | Pouco fiáveis (erro 25%, confiança 0.75, mais lentas) | Põe a verificação à prova |
| **sobrecarga** | Cidade em stress | 6 em 5 s: 4 reais e 2 falsos | Só 2 ambulâncias e 2 polícias; trânsito nível 4; 4 estradas cortadas | Fiáveis | Põe à prova a falta de recursos |

Os valores destes cenários foram escolhidos por nós para criar o contraste; são hipóteses de trabalho e não dados reais.

## 3. Métricas

- **TP / FP / FN / TN:** decisões certas e erradas da Verificação, comparadas com o que realmente aconteceu. FP = falso alarme aceite; FN = acidente real rejeitado.
- **Precisão:** dos alertas aceites, quantos eram reais. **Recall:** dos acidentes reais, quantos foram aceites.
- **Despacho / Chegada:** segundos simulados até ao despacho e até à chegada da ambulância.
- **Reais sem ambulância:** acidentes reais que nunca tiveram ambulância (rejeitados ou sem unidade livre).
- **Unidades gastas:** unidades enviadas para um alerta que se revelou falso.
- **Mensagens:** total de mensagens trocadas.

Os tempos são em **segundos simulados** e dependem das escalas que definimos (velocidade das unidades, atraso das testemunhas), por isso servem para **comparar** cenários e não como tempos reais.

## 4. Resultados

### 4.1 Cenário normal

| Limiar | TP | FP | FN | TN | Precisão | Recall | Despacho (s) | Chegada (s) | Mensagens |
|---|---|---|---|---|---|---|---|---|---|
| 0.5 | 4.4 ± 0.55 | 0 | 0.6 ± 0.55 | 1 | 1.00 | 0.88 | 1.08 | 5.05 | 78.6 |
| 0.65 | 4.4 ± 0.55 | 0 | 0.6 ± 0.55 | 1 | 1.00 | 0.88 | 1.08 | 5.05 | 78.6 |
| 0.8 | 3.4 ± 0.55 | 0 | 1.6 ± 0.55 | 1 | 1.00 | 0.68 | 1.08 | 5.51 | 79.6 |

Sem unidades gastas. Com 0.5 e 0.65 o resultado é igual: nenhuma pontuação ficou entre estes dois valores. Subir para 0.8 faz perder um acidente real por simulação, sem ganho (não havia falsos positivos a evitar).

### 4.2 Cenário hostil

| Limiar | TP | FP | FN | TN | Precisão | Recall | Despacho (s) | Chegada (s) | Unidades gastas | Mensagens |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.5 | 2.2 ± 0.84 | 0.8 ± 0.84 | 0.8 ± 0.84 | 2.2 ± 0.84 | 0.80 | 0.73 | 1.53 | 5.71 | 4.2 | 78.6 |
| 0.65 | 1.2 ± 0.84 | 1.0 ± 0.71 | 1.8 ± 0.84 | 2.0 ± 0.71 | 0.50 | 0.40 | 3.15 | 8.17 | 4.4 | 67.4 |
| 0.8 | 0.0 | 0.2 ± 0.45 | 3.0 | 2.8 ± 0.45 | 0.00 | 0.00 | sem valor | sem valor | 4.0 | 53.2 |

Com 0.8 não é aceite nenhum acidente real, e os 3 acidentes reais ficam rejeitados. Os tempos ficam sem valor porque não há acidentes reais despachados.

### 4.3 Cenário sobrecarga

| Limiar | TP | FP | FN | TN | Precisão | Recall | Reais sem ambulância | Despacho (s) | Chegada (s) | Mensagens |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.5 | 3.6 ± 0.55 | 0 | 0.4 ± 0.55 | 2 | 1.00 | 0.90 | 2.0 | 0.68 | 8.72 | 58.0 |
| 0.65 | 3.6 ± 0.55 | 0 | 0.4 ± 0.55 | 2 | 1.00 | 0.90 | 2.0 | 0.68 | 8.72 | 58.0 |
| 0.8 | 2.6 ± 0.55 | 0 | 1.4 ± 0.55 | 2 | 1.00 | 0.65 | 2.2 | 0.83 | 8.60 | 60.6 |

## 5. Análise

**1. O limiar 0.8 é demasiado exigente em todos os cenários.** O recall cai de 0.88 para 0.68 (normal), de 0.90 para 0.65 (sobrecarga) e para 0 (hostil). Em acidentes médicos, perder um acidente real custa mais do que um falso alarme.

**2. Com boa informação, 0.5 e 0.65 são equivalentes.** No cenário normal e no de sobrecarga os resultados são iguais. A escolha de 0.65 não é prejudicial, mas estes cenários não a distinguem de 0.5.

**3. Com má informação, o limiar mais baixo foi melhor.** No cenário hostil, passar de 0.5 para 0.65 baixou a precisão (0.80 → 0.50) e o recall (0.73 → 0.40). O esperado era que um limiar maior reduzisse os falsos positivos, mas aqui os FP ficaram parecidos (0.8 → 1.0) e perderam-se acidentes reais. Uma explicação provável (não verificada) é que, com testemunhas que erram 25% das vezes, alguns acidentes reais recebem uma negação e a pontuação desce abaixo do limiar. O desvio-padrão é grande (≈ 0.8 em quase todas as colunas) e há só 5 repetições, por isso lemos isto como **tendência** e não como valor exato.

**4. Falsos alarmes urgentes gastam unidades, seja qual for o limiar.** No cenário hostil gastam-se 4.0 a 4.4 unidades por simulação e 24 a 33 segundos simulados de unidades ocupadas. O pré-alerta urgente envia o socorro **antes** da verificação, por isso o limiar não o evita. Nos outros cenários não houve unidades gastas (não analisámos porquê).

**5. A sobrecarga mostra um limite que o limiar não resolve.** Com 2 ambulâncias para 4 acidentes reais, em média 2 ficam sem ambulância com 0.5 ou 0.65. O Dispatcher não tem fila: quando não há unidade livre, o incidente não é retomado mais tarde. A chegada demora 8.7 s contra 5.1 s no normal, por causa do trânsito, das estradas cortadas e do menor número de unidades.

**6. Menos mensagens com limiar alto.** No hostil, as mensagens descem de 78.6 para 53.2 quando o limiar sobe, porque os alertas rejeitados deixam de gerar despacho e RCP. É menos carga, mas à custa de socorro.

## 6. Trade-offs

| Decisão | Ganha | Perde |
|---|---|---|
| Limiar alto | Menos falsos alarmes aceites | Acidentes reais ignorados (recall) |
| Limiar baixo | Mais acidentes reais apanhados | Mais risco de aceitar alarmes falsos |
| Pré-alerta urgente | Socorro mais rápido nos casos graves | Unidades gastas em falsos alarmes |
| Dispatcher centralizado | Escolha global da melhor unidade | Ponto único de falha; sem fila sob carga |
| Esperar pela testemunha | Decisão mais fiável | Mais tempo até ao veredicto |

## 7. Limitações

- **Só 5 repetições** por combinação e desvios-padrão grandes: os resultados são indicativos, não conclusivos. Convém subir para 10 ou mais antes de entregar.
- **Os cenários foram desenhados por nós**: as proporções de acidentes reais e falsos, os erros das testemunhas e o número de unidades são escolhas nossas, não medições da realidade.
- **Só variámos o limiar.** Não estudámos o efeito dos pesos (0.3 / 0.7), da reputação, do alcance das testemunhas nem do número de unidades.
- **Tempos em segundos simulados**, com atrasos das testemunhas inventados.
- **O tempo médio de despacho só conta acidentes que chegaram a ser despachados**; lê-se sempre junto de "Reais sem ambulância".
- **A verdade do terreno** é usada logo a seguir à decisão (simplificação); na realidade só se saberia mais tarde.
- **O Dispatcher não tem fila** e o sistema continua com um ponto único de falha.
- Nos cenários normal e sobrecarga **a causa dos 0.6 acidentes reais rejeitados** com limiar 0.5 não foi analisada em detalhe.

## 8. Conclusão

Nestas experiências, o limiar 0.65 funciona bem com informação boa, mas com informação má um valor mais baixo (0.5) apanhou mais acidentes reais; o limiar 0.8 é demasiado rígido. O maior problema não está no limiar, mas na **falta de fila no Dispatcher** quando os recursos são poucos, e nas **unidades gastas por alarmes urgentes**. Estes são os pontos a melhorar em trabalho futuro.
