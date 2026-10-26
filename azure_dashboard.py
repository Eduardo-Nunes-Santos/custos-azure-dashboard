#!/usr/bin/env python3
"""
Azure Dashboard - coleta VMs, status, recursos órfãos e custos e gera dashboard.html

Uso:
    pip install -r requirements.txt
    python azure_dashboard.py                 # mês atual
    python azure_dashboard.py --mes 2026-09   # mês específico

Configure o arquivo .env (copie de .env.example).
Permissões do Service Principal na subscription: Reader + Cost Management Reader
"""
import argparse, calendar, json, os, sys, time
from datetime import datetime, date, timedelta
import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

BASE = os.path.dirname(os.path.abspath(__file__))
if load_dotenv:
    load_dotenv(os.path.join(BASE, ".env"))

OUT = os.getenv("OUTPUT_DIR") or BASE
os.makedirs(OUT, exist_ok=True)

TENANT, CLIENT, SECRET, SUB = (os.getenv(k) for k in
                               ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "SUBSCRIPTION_ID"))
ARM = "https://management.azure.com"
S = requests.Session()


# ---------------------------------------------------------------- utilidades
def get_token():
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": CLIENT,
              "client_secret": SECRET, "scope": f"{ARM}/.default"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


RETRY_HEADERS = (
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-retry-after",
    "Retry-After",
)


def call(method, url, **kw):
    for t in range(8):
        r = S.request(method, url, timeout=60, **kw)
        if r.status_code in (429, 503):
            waits = [int(r.headers[h]) for h in RETRY_HEADERS if r.headers.get(h, "").isdigit()]
            espera = min((max(waits) if waits else 30 * (t + 1)) + 2, 300)
            print(f"  Limite da API (429). Aguardando {espera}s... tentativa {t + 1}/8", flush=True)
            time.sleep(espera)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Limite da API de custos excedido. Aguarde alguns minutos e rode de novo.")


def paged(url):
    while url:
        d = call("GET", url)
        yield from d.get("value", [])
        url = d.get("nextLink")


def rg_of(rid):
    p = rid.split("/")
    lp = [x.lower() for x in p]
    return p[lp.index("resourcegroups") + 1] if "resourcegroups" in lp else ""


def tipo_of(rid):
    p = rid.split("/")
    lp = [x.lower() for x in p]
    if "providers" in lp:
        i = lp.index("providers")
        if len(p) > i + 2:
            return f"{p[i + 1]}/{p[i + 2]}".lower()
    return "outro"


# ---------------------------------------------------------------- inventário
def collect_vms():
    base = f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Compute/virtualMachines"
    status = {}
    for v in paged(f"{base}?api-version=2024-07-01&statusOnly=true"):
        codes = [s["code"] for s in v.get("properties", {}).get("instanceView", {}).get("statuses", [])]
        power = next((c.split("/")[1] for c in codes if c.startswith("PowerState/")), "unknown")
        status[v["id"].lower()] = power
    vms = []
    for v in paged(f"{base}?api-version=2024-07-01"):
        p = v["properties"]
        sp = p["storageProfile"]
        power = status.get(v["id"].lower(), "unknown")
        label = {"running": "Ligada", "stopped": "Desligada", "deallocated": "Desalocada"}.get(power, power.capitalize())
        discos = [d["managedDisk"]["id"].lower()
                  for d in [sp["osDisk"]] + sp.get("dataDisks", []) if "managedDisk" in d]
        vms.append({
            "id": v["id"].lower(), "nome": v["name"], "rg": rg_of(v["id"]),
            "regiao": v["location"], "tamanho": p["hardwareProfile"]["vmSize"],
            "so": sp["osDisk"].get("osType", ""), "status": label, "tags": v.get("tags") or {},
            "nics": [n["id"].lower() for n in p["networkProfile"]["networkInterfaces"]],
            "discos": discos,
        })
    return vms


def collect_network_disks():
    """Retorna (órfãos, ids conhecidos, IPs públicos por NIC, nomes por id)."""
    orph, known, nic_pips, names = [], set(), {}, {}

    d = f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Compute/disks?api-version=2024-03-02"
    for x in paged(d):
        known.add(x["id"].lower()); names[x["id"].lower()] = x["name"]
        if not x.get("managedBy") and x["properties"].get("diskState") == "Unattached":
            orph.append({"id": x["id"].lower(), "tipo": "Disco", "nome": x["name"], "rg": rg_of(x["id"]),
                         "regiao": x["location"],
                         "detalhe": f'{x["properties"].get("diskSizeGB", "?")} GB · {x.get("sku", {}).get("name", "")}'})

    n = f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Network/networkInterfaces?api-version=2024-05-01"
    for x in paged(n):
        known.add(x["id"].lower()); names[x["id"].lower()] = x["name"]
        nic_pips[x["id"].lower()] = [
            c["properties"]["publicIPAddress"]["id"].lower()
            for c in x["properties"].get("ipConfigurations") or []
            if c.get("properties", {}).get("publicIPAddress")]
        if not x["properties"].get("virtualMachine") and not x["properties"].get("privateEndpoint"):
            ip = (x["properties"].get("ipConfigurations") or [{}])[0].get("properties", {}).get("privateIPAddress", "")
            orph.append({"id": x["id"].lower(), "tipo": "NIC", "nome": x["name"], "rg": rg_of(x["id"]),
                         "regiao": x["location"], "detalhe": ip})

    i = f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Network/publicIPAddresses?api-version=2024-05-01"
    for x in paged(i):
        known.add(x["id"].lower()); names[x["id"].lower()] = x["name"]
        if not x["properties"].get("ipConfiguration") and not x["properties"].get("natGateway"):
            orph.append({"id": x["id"].lower(), "tipo": "IP Público", "nome": x["name"], "rg": rg_of(x["id"]),
                         "regiao": x["location"],
                         "detalhe": f'{x["properties"].get("ipAddress", "sem IP")} · {x.get("sku", {}).get("name", "")}'})
    return orph, known, nic_pips, names


# ---------------------------------------------------------------- custos
def cost_query(period, granularity, group=None):
    ds = {"granularity": granularity,
          "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}}
    if group:
        ds["grouping"] = [{"type": "Dimension", "name": group}]
    body = {"type": "ActualCost", "timeframe": "Custom", "timePeriod": period, "dataset": ds}
    url = f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    rows, cols = [], None
    while url:
        d = call("POST", url, json=body)
        cols = [c["name"] for c in d["properties"]["columns"]]
        rows += d["properties"]["rows"]
        url = d["properties"].get("nextLink")
    return [dict(zip(cols, r)) for r in rows]


def cost_query_cached(period, granularity, group=None):
    """Cache em disco: 1h para o mês corrente, 6h para meses fechados."""
    f = os.path.join(OUT, f".cache_{period['from'][:7]}_{granularity}_{group or 'all'}.json")
    ttl = 3600 if period["from"][:7] == date.today().strftime("%Y-%m") else 6 * 3600
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < ttl:
        print(f"  (usando cache: {period['from'][:7]} {granularity})")
        return json.load(open(f, encoding="utf-8"))
    rows = cost_query(period, granularity, group)
    json.dump(rows, open(f, "w", encoding="utf-8"))
    time.sleep(3)  # espaça as consultas para reduzir erros 429
    return rows


def periodo_de(a, m):
    return {"from": f"{date(a, m, 1)}T00:00:00Z",
            "to": f"{date(a, m, calendar.monthrange(a, m)[1])}T23:59:59Z"}


def mes_anterior(a, m):
    return (a - 1, 12) if m == 1 else (a, m - 1)


# ---------------------------------------------------------------- principal
def main():
    if not all([TENANT, CLIENT, SECRET, SUB]):
        sys.exit("Preencha TENANT_ID, CLIENT_ID, CLIENT_SECRET e SUBSCRIPTION_ID no arquivo .env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--mes", help="YYYY-MM do mês principal (padrão: mês atual)")
    ap.add_argument("--historico", type=int, default=int(os.getenv("MESES_HISTORICO", "3")),
                    help="quantos meses anteriores incluir para consulta no painel (padrão: 3)")
    a = ap.parse_args()
    hoje = date.today()
    ano, mes = map(int, a.mes.split("-")) if a.mes else (hoje.year, hoje.month)

    html = open(os.path.join(BASE, "template.html"), encoding="utf-8").read()
    if "__DATA__" not in html:
        sys.exit("ERRO: template.html não contém o marcador __DATA__ (use o template original, não um dashboard gerado)")

    S.headers["Authorization"] = f"Bearer {get_token()}"
    print("Coletando VMs...");               vms = collect_vms()
    print("Coletando recursos órfãos...");   orph, known, nic_pips, names = collect_network_disks()
    for v in vms:
        known.add(v["id"])

    def nome_de(rid):
        return names.get(rid) or rid.rstrip("/").split("/")[-1]

    totais = {}

    def total_mes(am, mm):
        if (am, mm) not in totais:
            totais[(am, mm)] = round(sum(r["Cost"] for r in cost_query_cached(periodo_de(am, mm), "None")), 2)
        return totais[(am, mm)]

    def visao_mes(am, mm):
        """Tudo que depende de custo para um mês (inventário é o estado atual)."""
        period = periodo_de(am, mm)
        fim = date(am, mm, calendar.monthrange(am, mm)[1])
        por_rec = {}
        for r in cost_query_cached(period, "None", "ResourceId"):
            rid = (r.get("ResourceId") or "").lower()
            if rid:
                por_rec[rid] = por_rec.get(rid, 0) + r["Cost"]
        diario = sorted(cost_query_cached(period, "Daily"), key=lambda r: r["UsageDate"])
        total = round(sum(r["Cost"] for r in diario), 2)
        totais[(am, mm)] = total

        # previsão: acumulado + média dos últimos 7 dias × dias restantes (só no mês corrente)
        proj, media = [], 0.0
        if diario:
            datas = [datetime.strptime(str(r["UsageDate"])[:8], "%Y%m%d").date() for r in diario]
            ultimo = max(datas)
            recentes = ([r["Cost"] for r, d in zip(diario, datas) if d < hoje][-7:]
                        or [r["Cost"] for r in diario][-7:])
            media = sum(recentes) / len(recentes)
            if (am, mm) == (hoje.year, hoje.month) and ultimo < fim:
                for n in range(1, (fim - ultimo).days + 1):
                    proj.append({"d": (ultimo + timedelta(days=n)).strftime("%Y%m%d"), "c": round(media, 2)})
        previsao = round(total + media * len(proj), 2)
        fator = previsao / total if total else 1

        vms_c = {}
        for v in vms:  # custo por VM = VM + discos (SO e dados) + NICs + IPs públicos
            rec = [{"tipo": "VM", "nome": v["nome"], "custo": round(por_rec.get(v["id"], 0), 2)}]
            for d in v["discos"]:
                rec.append({"tipo": "Disco", "nome": nome_de(d), "custo": round(por_rec.get(d, 0), 2)})
            for n in v["nics"]:
                rec.append({"tipo": "NIC", "nome": nome_de(n), "custo": round(por_rec.get(n, 0), 2)})
                for pip in nic_pips.get(n, []):
                    rec.append({"tipo": "IP Público", "nome": nome_de(pip), "custo": round(por_rec.get(pip, 0), 2)})
            ct = round(sum(x["custo"] for x in rec), 2)
            vms_c[v["id"]] = {"custo_vm": rec[0]["custo"], "custo_total": ct,
                              "prev_total": round(ct * fator, 2), "recursos": rec}

        outros = [{"tipo": tipo_of(rid), "nome": rid.rstrip("/").split("/")[-1], "rg": rg_of(rid), "custo": round(c, 2)}
                  for rid, c in por_rec.items() if rid not in known and c > 0]
        outros.sort(key=lambda o: -o["custo"])

        pa, pm = mes_anterior(am, mm)
        try:
            ant = total_mes(pa, pm)
        except Exception as e:
            print(f"  Aviso: mês anterior de {mm:02d}/{am} indisponível ({e})")
            ant = None

        moeda_m = (diario[0].get("Currency") if diario else None)
        print(f"  {mm:02d}/{am}: total {total} | previsão {previsao} | mês anterior {ant} | outros custos {len(outros)}")
        return {"periodo": f"{mm:02d}/{am}", "total": total, "diario":
                [{"d": str(r["UsageDate"]), "c": round(r["Cost"], 2)} for r in diario],
                "previsao": {"valor": previsao, "media_diaria": round(media, 2), "proj": proj},
                "anterior": {"periodo": f"{pm:02d}/{pa}", "total": ant},
                "vms": vms_c,
                "orfaos": {o["id"]: round(por_rec.get(o["id"], 0), 2) for o in orph},
                "outros": outros}, moeda_m

    # meses em ordem cronológica (o mais antigo primeiro; o principal por último)
    seq, (am, mm) = [], (ano, mes)
    for _ in range(max(a.historico, 0) + 1):
        seq.append((am, mm))
        am, mm = mes_anterior(am, mm)
    seq.reverse()

    print("Coletando custos (pode demorar na primeira execução)...")
    meses, moeda = {}, "BRL"
    for am, mm in seq:
        try:
            M, mo = visao_mes(am, mm)
            meses[f"{am}-{mm:02d}"] = M
            if mo:
                moeda = mo
        except Exception as e:
            if (am, mm) == (ano, mes):
                raise
            print(f"  Aviso: mês {mm:02d}/{am} ignorado ({e})")

    data = {"gerado": datetime.now().strftime("%d/%m/%Y %H:%M"), "moeda": moeda,
            "mes_atual": f"{ano}-{mes:02d}", "meses": meses,
            "vms": [{"rid": v["id"], **{k: v[k] for k in ("nome", "status", "rg", "regiao", "tamanho", "so", "tags")}} for v in vms],
            "orfaos": [{"rid": o["id"], **{k: o[k] for k in ("tipo", "nome", "detalhe", "rg", "regiao")}} for o in orph]}
    print(f"VMs: {len(vms)} | órfãos: {len(orph)} | meses no painel: {len(meses)}")

    with open(os.path.join(OUT, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Chart.js embutido (funciona offline); se faltar, usa o CDN
    chart_file = os.path.join(BASE, "vendor", "chart.umd.min.js")
    if os.path.exists(chart_file):
        chart_tag = "<script>" + open(chart_file, encoding="utf-8").read() + "</script>"
    else:
        chart_tag = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>'
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("<!--CHARTJS-->", chart_tag)

    out = os.path.join(OUT, "dashboard.html")
    with open(out + ".tmp", "w", encoding="utf-8") as f:   # gravação atômica
        f.write(html)
    os.replace(out + ".tmp", out)
    print(f"Pronto: {out}")


if __name__ == "__main__":
    main()
