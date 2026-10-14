"""Monitor Growatt -> WhatsApp (versão simples, sem banco de dados).

Faz duas coisas:
  1. Verifica a comunicação do inversor a cada 5 min e avisa no WhatsApp
     quando ela cai (e quando volta).
  2. Às 19:00 manda o resumo da geração do dia.

Endpoints:
  /verificar?key=...   chamado pelo cron-job.org a cada 5 min (mantém o Render acordado)
  /relatorio?key=...   envia o resumo agora (manual)
  /status              situação atual (JSON)

Uso local:
  python app.py                  # sobe o servidor e verifica sozinho a cada 5 min
  python app.py --verificar      # uma verificação e sai
  python app.py --previa         # mostra o resumo do dia sem enviar
  python app.py --teste          # manda mensagem de teste no WhatsApp
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

# ----------------------------------------------------------------------------- configuração
TOKEN = os.getenv("GROWATT_TOKEN", "").strip()
SERVER = os.getenv("GROWATT_SERVER", "https://openapi.growatt.com").rstrip("/")
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Sao_Paulo"))

WHATSAPP_TO = os.getenv("WHATSAPP_TO", "").strip()              # 5544999998888
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "").strip()

CRON_KEY = os.getenv("CRON_KEY", "").strip()
SUN_START = int(os.getenv("SUN_START_HOUR", "8"))               # só cobra comunicação de dia
SUN_END = int(os.getenv("SUN_END_HOUR", "17"))
OFFLINE_MINUTES = int(os.getenv("OFFLINE_MINUTES", "30"))       # sem dado novo há X min = caiu
API_FAILS = int(os.getenv("API_FAILS", "3"))                    # falhas seguidas da API Growatt p/ avisar
REPORT_TIME = os.getenv("REPORT_TIME", "19:00")
PLANT_KWP = float(os.getenv("PLANT_KWP", "0") or 0)
PORT = int(os.getenv("PORT", "5000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("growatt")

# estado em memória (se o serviço reiniciar, no pior caso repete um aviso)
STATE = {
    "ultima_verificacao": None,
    "falhas_api": 0,
    "api_avisada": False,
    "offline": {},            # device_sn -> horário em que caiu (já avisado)
    "relatorio_enviado": None,
    "ultimo_erro": None,
    "equipamentos": [],
}
_lock = threading.Lock()


def agora():
    return datetime.now(TZ).replace(tzinfo=None)


def g(d, *keys):
    """Lê um campo aceitando snake_case ou camelCase."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:19], fmt)
        except ValueError:
            pass
    return None


def num(v, d=1):
    return f"{float(v):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ----------------------------------------------------------------------------- Growatt
class GrowattErro(Exception):
    pass


class LimiteAPI(GrowattErro):
    """Consulta repetida em menos de 5 min (não é falha de comunicação)."""


def growatt(path, **params):
    try:
        r = requests.get(f"{SERVER}/v1/{path}", headers={"token": TOKEN}, params=params, timeout=30)
        js = r.json()
    except Exception as e:  # noqa: BLE001
        raise GrowattErro(f"Growatt não respondeu ({e.__class__.__name__})") from e
    code = js.get("error_code")
    if code == 10012:
        raise LimiteAPI("limite de 5 min da API")
    if code:
        raise GrowattErro(f"Growatt erro {code}: {js.get('error_msg')}")
    return js.get("data") or {}


_plants_cache = {"ts": None, "plants": []}


def plantas():
    """Lista de usinas (cache de 6 h)."""
    if not _plants_cache["ts"] or agora() - _plants_cache["ts"] > timedelta(hours=6):
        _plants_cache["plants"] = growatt("plant/list", page=1, perpage=20).get("plants", [])
        _plants_cache["ts"] = agora()
    return _plants_cache["plants"]


# ----------------------------------------------------------------------------- WhatsApp
def whatsapp(texto):
    if not (WHATSAPP_TO and CALLMEBOT_APIKEY):
        log.info("WhatsApp não configurado. Mensagem:\n%s", texto)
        return False
    ok = True
    keys = [k.strip() for k in CALLMEBOT_APIKEY.split(",")]
    for i, fone in enumerate([f.strip().lstrip("+") for f in WHATSAPP_TO.split(",") if f.strip()]):
        enviado = False
        for tentativa in range(3):
            try:
                r = requests.get("https://api.callmebot.com/whatsapp.php", timeout=30, params={
                    "phone": f"+{fone}", "text": texto, "apikey": keys[min(i, len(keys) - 1)]})
                if r.status_code == 200 and "error" not in r.text.lower()[:300]:
                    enviado = True
                    break
                log.warning("CallMeBot respondeu: %s", r.text[:200])
            except Exception as e:  # noqa: BLE001
                log.warning("CallMeBot falhou: %s", e)
            time.sleep(5 * (tentativa + 1))
        ok = ok and enviado
    return ok


# ----------------------------------------------------------------------------- 1) comunicação
def verificar(forcar=False):
    """Checa a comunicação do inversor e avisa quando cai/volta."""
    with _lock:
        n = agora()
        ult = STATE["ultima_verificacao"]
        if not forcar and ult and (n - ult).total_seconds() < 290:
            return {"pulado": "verificado há menos de 5 min"}
        STATE["ultima_verificacao"] = n

        try:
            equipamentos = []
            for p in plantas():
                pid = g(p, "plant_id", "plantId", "id")
                devs = growatt("device/list", plant_id=pid, page=1, perpage=50).get("devices", [])
                for d in devs:
                    equipamentos.append({
                        "usina": g(p, "name", "plantName"),
                        "sn": g(d, "device_sn", "deviceSn", "sn"),
                        "lost": bool(g(d, "lost")),
                        "ultimo": g(d, "last_update_time", "lastUpdateTime"),
                    })
        except LimiteAPI:
            return {"pulado": "limite da API"}
        except GrowattErro as e:
            STATE["falhas_api"] += 1
            STATE["ultimo_erro"] = str(e)
            log.warning("%s (falha %s)", e, STATE["falhas_api"])
            if STATE["falhas_api"] >= API_FAILS and not STATE["api_avisada"]:
                STATE["api_avisada"] = True
                whatsapp(f"🟠 *Monitor da usina*\nNão estou conseguindo consultar a Growatt.\n{e}\n_{n:%d/%m %H:%M}_")
            return {"erro": str(e)}

        # API respondeu
        if STATE["api_avisada"]:
            whatsapp("✅ *Monitor da usina*\nConsulta à Growatt normalizada.")
        STATE["falhas_api"] = 0
        STATE["api_avisada"] = False
        STATE["ultimo_erro"] = None
        STATE["equipamentos"] = equipamentos

        dia = SUN_START <= n.hour < SUN_END
        for e in equipamentos:
            ultimo = parse_dt(e["ultimo"])
            parado = ultimo is not None and n - ultimo > timedelta(minutes=OFFLINE_MINUTES)
            sem_com = dia and (e["lost"] or parado)
            e["sem_comunicacao"] = sem_com
            sn = e["sn"]
            if sem_com and sn not in STATE["offline"]:
                STATE["offline"][sn] = n
                quando = f"último dado às {ultimo:%H:%M} de {ultimo:%d/%m}" if ultimo else "sem horário do último dado"
                whatsapp(f"🔴 *{e['usina']}*\nInversor {sn} SEM COMUNICAÇÃO\n{quando}\n"
                         f"Verifique Wi-Fi/datalogger e disjuntores.\n_{n:%d/%m %H:%M}_")
            elif sn in STATE["offline"] and not e["lost"] and not parado:
                caiu = STATE["offline"].pop(sn)
                mins = int((n - caiu).total_seconds() // 60)
                whatsapp(f"✅ *{e['usina']}*\nInversor {sn} voltou a comunicar (≈{mins // 60}h{mins % 60:02d} fora).")
        return {"ok": True, "equipamentos": equipamentos}


# ----------------------------------------------------------------------------- 2) resumo diário
def montar_relatorio():
    hoje = agora().date()
    linhas = []
    for p in plantas():
        pid = g(p, "plant_id", "plantId", "id")
        nome = g(p, "name", "plantName") or "Usina"
        kwp = PLANT_KWP or float(g(p, "peak_power", "peakPower") or 0)
        d = growatt("plant/data", plant_id=pid)
        e_hoje = float(g(d, "today_energy", "todayEnergy") or 0)
        linhas.append(f"☀️ *{nome}* – geração de {hoje:%d/%m}")
        linhas.append(f"⚡ Hoje: *{num(e_hoje)} kWh*" + (f" ({num(e_hoje / kwp, 2)} kWh/kWp)" if kwp else ""))

        # últimos 7 dias para comparação (endpoint permite 10 chamadas/dia)
        try:
            hist = growatt("plant/energy", plant_id=pid, start_date=(hoje - timedelta(days=7)).isoformat(),
                           end_date=(hoje - timedelta(days=1)).isoformat(), time_unit="day", perpage=20)
            dias = [float(x.get("energy") or 0) for x in hist.get("energys", [])]
            dias_validos = [x for x in dias if x > 0]
            if dias:
                linhas.append(f"↩️ Ontem: {num(dias[-1])} kWh")
            if dias_validos:
                media = sum(dias_validos) / len(dias_validos)
                var = (e_hoje / media - 1) * 100 if media else 0
                linhas.append(f"{'📈' if var >= 0 else '📉'} Média 7 dias: {num(media)} kWh "
                              f"({'+' if var >= 0 else ''}{num(var, 0)}%)")
        except GrowattErro as e:
            log.warning("histórico: %s", e)

        mes = g(d, "monthly_energy", "monthlyEnergy")
        ano = g(d, "yearly_energy", "yearlyEnergy")
        if mes is not None:
            linhas.append(f"📅 Mês: {num(float(mes), 0)} kWh")
        if ano is not None:
            linhas.append(f"📆 Ano: {num(float(ano) / 1000, 2)} MWh")
        if any(e.get("sem_comunicacao") for e in STATE["equipamentos"]):
            linhas.append("🔴 Inversor sem comunicação no momento")
        linhas.append("")
    return "\n".join(linhas).strip() or "☀️ Nenhuma usina encontrada para este token."


def relatorio_se_for_hora():
    """Envia o resumo entre REPORT_TIME e +60 min, uma vez por dia."""
    if not REPORT_TIME:
        return
    n = agora()
    h, m = map(int, REPORT_TIME.split(":"))
    inicio = n.replace(hour=h, minute=m, second=0, microsecond=0)
    if inicio <= n < inicio + timedelta(minutes=60) and STATE["relatorio_enviado"] != n.date():
        STATE["relatorio_enviado"] = n.date()
        try:
            whatsapp(montar_relatorio())
        except GrowattErro as e:
            STATE["relatorio_enviado"] = None   # tenta de novo na próxima verificação
            log.warning("resumo: %s", e)


# ----------------------------------------------------------------------------- web
app = Flask(__name__)


def chave_ok():
    return not CRON_KEY or request.args.get("key") == CRON_KEY


@app.get("/verificar")
def rota_verificar():
    if not chave_ok():
        return {"erro": "chave inválida"}, 403
    res = verificar()
    relatorio_se_for_hora()
    return jsonify(res)


@app.get("/relatorio")
def rota_relatorio():
    if not chave_ok():
        return {"erro": "chave inválida"}, 403
    texto = montar_relatorio()
    if request.args.get("enviar", "1") != "0":
        whatsapp(texto)
    return texto, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/status")
@app.get("/")
def rota_status():
    s = {k: (str(v) if isinstance(v, datetime) else v) for k, v in STATE.items() if k != "offline"}
    s["offline"] = {k: str(v) for k, v in STATE["offline"].items()}
    return jsonify(s)


def laco():
    while True:
        try:
            verificar()
            relatorio_se_for_hora()
        except Exception:  # noqa: BLE001
            log.exception("falha no laço")
        time.sleep(300)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Configure GROWATT_TOKEN no .env")
    if "--verificar" in sys.argv:
        print(verificar(forcar=True))
    elif "--previa" in sys.argv:
        print(montar_relatorio())
    elif "--teste" in sys.argv:
        print("enviado" if whatsapp("✅ Teste do monitor da usina – WhatsApp funcionando!") else "falhou")
    else:
        threading.Thread(target=laco, daemon=True).start()
        from waitress import serve
        log.info("Monitor rodando na porta %s", PORT)
        serve(app, host="0.0.0.0", port=PORT)
