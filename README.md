# Monitor Growatt → WhatsApp

Um arquivo só (`app.py`), sem banco de dados. Faz duas coisas:

1. **A cada 5 min verifica a comunicação do inversor.** Se cair, avisa no WhatsApp, e avisa de novo quando voltar:
   ```
   🔴 José CNA
   Inversor ABC123 SEM COMUNICAÇÃO
   último dado às 14:30 de 14/10
   Verifique Wi-Fi/datalogger e disjuntores.
   ```
   Considera "sem comunicação" quando a Growatt marca o equipamento como offline ou quando
   não chega dado novo há mais de 30 min. Só verifica das 8h às 17h, porque à noite o inversor desliga.
   Se a própria API da Growatt falhar 3 vezes seguidas, também avisa.

2. **Às 19:00 manda o resumo do dia:**
   ```
   ☀️ José CNA – geração de 14/10
   ⚡ Hoje: 142,3 kWh (5,22 kWh/kWp)
   ↩️ Ontem: 106,0 kWh
   📈 Média 7 dias: 103,0 kWh (+38%)
   📅 Mês: 1.850 kWh
   📆 Ano: 28,40 MWh
   ```

## 1. WhatsApp (CallMeBot, grátis)
Salve o contato **+34 644 71 81 99** e mande para ele: `I allow callmebot to send me messages`.
Ele responde com a **apikey**. Para avisar mais de uma pessoa: `WHATSAPP_TO=5544...,5544...` e
`CALLMEBOT_APIKEY=key1,key2` (cada pessoa faz a ativação no próprio celular).

## 2. Testar no seu PC
```bash
pip install -r requirements.txt
cp .env.example .env          # preencha token, telefone e apikey
python testar_token.py        # confere o token da Growatt
python app.py --teste         # mensagem de teste no WhatsApp
python app.py --verificar     # uma verificação de comunicação
python app.py --previa        # mostra o resumo do dia
```

## 3. Publicar no Render
1. Suba a pasta para um repositório no GitHub (o `.env` não vai, está no `.gitignore`).
2. No Render: **New + → Blueprint** → escolha o repositório.
3. Preencha `GROWATT_TOKEN`, `WHATSAPP_TO` e `CALLMEBOT_APIKEY`.
4. Em *Environment*, copie o valor gerado de `CRON_KEY`.
5. No [cron-job.org](https://cron-job.org) (grátis), crie um job a cada **5 minutos** chamando
   `https://<seu-servico>.onrender.com/verificar?key=<CRON_KEY>`

O job do cron-job.org é o que faz tudo funcionar no plano grátis. O Render desliga o serviço
depois de 15 min sem acesso, e essa chamada a cada 5 min mantém ele acordado e dispara a verificação.
O resumo das 19:00 sai na primeira verificação depois desse horário.

Outros endereços úteis:
- `/status` mostra a situação atual
- `/relatorio?key=<CRON_KEY>` envia o resumo na hora (`&enviar=0` só mostra)

## Observações
- Sem banco de dados, o estado fica em memória. Se o Render reiniciar o serviço com o inversor
  fora do ar, você recebe o aviso de novo. Fora isso, nada muda.
- As 750 horas grátis por mês do Render são por conta. Este serviço sozinho usa cerca de 744 h.
  Se outro serviço grátis da mesma conta também ficar acordado, as horas acabam antes do fim do mês.
- O token da Growatt dá acesso à sua conta: não publique.
