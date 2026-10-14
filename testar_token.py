"""Teste rápido do token: lista usinas e equipamentos e mostra a última leitura.

    python testar_token.py
    python testar_token.py SEU_TOKEN https://openapi.growatt.com
"""
import json
import sys

import requests

import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("GROWATT_TOKEN", "")
SERVER_URL = os.getenv("GROWATT_SERVER", "https://openapi.growatt.com")

token = sys.argv[1] if len(sys.argv) > 1 else TOKEN
server = (sys.argv[2] if len(sys.argv) > 2 else SERVER_URL).rstrip("/")
H = {"token": token}


def get(path, **params):
    r = requests.get(f"{server}/v1/{path}", headers=H, params=params, timeout=30)
    try:
        js = r.json()
    except ValueError:
        print(f"[{path}] resposta não-JSON (HTTP {r.status_code}): {r.text[:200]}")
        return None
    if js.get("error_code"):
        print(f"[{path}] erro {js.get('error_code')}: {js.get('error_msg')}")
        if js.get("error_code") == 10011:
            print("  -> sem permissão: o token não é da sua conta ou não está liberado para OpenAPI.")
        if js.get("error_code") == 10012:
            print("  -> limite: a mesma consulta só 1x a cada 5 min. Aguarde e rode de novo.")
        return None
    return js.get("data")


print(f"Servidor: {server}\nToken: {token[:4]}…{token[-4:]}\n")
plants = get("plant/list", page=1, perpage=20)
if not plants:
    sys.exit("Não foi possível listar usinas com este token.")

for p in plants.get("plants", []):
    print(f"Usina {p['plant_id']}: {p['name']} – {p.get('peak_power')} kWp – {p.get('city')}")
    print(f"  potência agora: {p.get('current_power')} W | total: {p.get('total_energy')} kWh")
    devs = get("device/list", plant_id=p["plant_id"], page=1, perpage=50) or {}
    for d in devs.get("devices", []):
        print(f"  • {d.get('device_sn')}  tipo={d.get('type')}  modelo={d.get('model')}  "
              f"offline={d.get('lost')}  última={d.get('last_update_time')}")
        if d.get("type") == 1:
            last = get("device/inverter/last_new_data", device_sn=d["device_sn"])
            if last:
                dd = last.get("data", last)
                keys = ["time", "pac", "ppv", "powerToday", "powerTotal", "temperature", "status", "statusText",
                        "vpv1", "ipv1", "vpv2", "ipv2", "vacr", "fac"]
                print("    " + json.dumps({k: dd.get(k) for k in keys if k in dd}, ensure_ascii=False))
print("\nOK – token válido. Rode: python app.py --verificar")
