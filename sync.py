#!/usr/bin/env python3
"""
Sincronização Notion → GoHighLevel
==================================

Lê leads da base de dados "Comercial" no Notion e mantém o stage de cada
contacto/oportunidade alinhado no GHL, para que as Smart Lists do GHL
fiquem sempre certas sem ninguém ter de mexer manualmente.

Comportamento:
- Para cada lead no Notion: procura no GHL por email; se não encontrar,
  tenta por telefone; se ainda não encontrar, cria contacto + oportunidade.
- Se a oportunidade já existir no pipeline, atualiza o stage.
- Status "Perdida" no Notion → opportunity.status = "lost" no GHL
  (mantém o último stage e desaparece das listas de leads abertos).
- Modo DRY_RUN imprime tudo o que faria sem escrever no GHL.

Variáveis de ambiente:
  NOTION_TOKEN       Integration secret do Notion (ntn_...)
  NOTION_DB_ID       ID da BD Comercial (UUID com ou sem hífenes)
  GHL_TOKEN          Private Integration Token do GHL (pit-...)
  GHL_LOCATION_ID    Sub-account / Location ID do GHL
  PIPELINE_NAME      (opcional) Nome do pipeline; se vazio usa o 1º
  DRY_RUN            "1" para correr sem escrever (default: 0)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

# ============================================================
# Configuração
# ============================================================

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
GHL_TOKEN = os.environ["GHL_TOKEN"]
GHL_LOCATION_ID = os.environ["GHL_LOCATION_ID"]
PIPELINE_NAME = os.environ.get("PIPELINE_NAME", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
GHL_API = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"

# Mapeamento Notion `Status` → nome do GHL Stage (validado com o user)
STATUS_TO_STAGE: dict[str, str] = {
    "Novas Leads": "Nova Lead",
    "Contactada": "Contactada",
    "Reunião Análise": "Reunião Análise",
    "Reunião Follow Up": "Reunião Follow Up",
    "Q&A": "Q&A",
    "Nutrição": "Nutrição",
    "Fechado": "Fechada",
    "€ Cash Collected €": "Cash Collected €€",
    "Aberto a Upsell/Crossell": "Mostrou interesse em Upsell",
    "Reunião Monetização": "Reunião Monetização",
    "Monetização Fechada": "Monetização Fechada",
}

# Valores de Status que correspondem a oportunidade "lost" no GHL
LOST_STATUSES = {"Perdida"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sync")


# ============================================================
# Helpers Notion
# ============================================================

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_query_all(database_id: str) -> Iterable[dict]:
    """Itera todas as páginas da base de dados (com pagination)."""
    cursor: Optional[str] = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{database_id}/query",
            headers=notion_headers(),
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            yield page
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def notion_prop(page: dict, name: str, ptype: str):
    """Extrai o valor escalar de uma propriedade do Notion."""
    prop = page.get("properties", {}).get(name)
    if not prop:
        return None
    if ptype == "title":
        return "".join(p.get("plain_text", "") for p in prop.get("title", [])).strip() or None
    if ptype == "rich_text":
        return "".join(p.get("plain_text", "") for p in prop.get("rich_text", [])).strip() or None
    if ptype == "email":
        v = prop.get("email")
        return v.strip().lower() if v else None
    if ptype == "phone_number":
        v = prop.get("phone_number")
        return v.strip() if v else None
    if ptype == "status":
        v = prop.get("status")
        return v.get("name") if v else None
    if ptype == "select":
        v = prop.get("select")
        return v.get("name") if v else None
    return None


# ============================================================
# Helpers GHL
# ============================================================

def ghl_headers() -> dict:
    return {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": GHL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def ghl_get_pipeline(name: Optional[str]) -> dict:
    r = requests.get(
        f"{GHL_API}/opportunities/pipelines",
        headers=ghl_headers(),
        params={"locationId": GHL_LOCATION_ID},
        timeout=30,
    )
    r.raise_for_status()
    pipelines = r.json().get("pipelines", [])
    if not pipelines:
        raise RuntimeError("Nenhum pipeline encontrado no GHL")
    if name:
        for p in pipelines:
            if p["name"].strip().lower() == name.strip().lower():
                return p
        raise RuntimeError(f"Pipeline '{name}' não encontrado")
    return pipelines[0]


def ghl_find_contact_by_email(email: str) -> Optional[dict]:
    if not email:
        return None
    r = requests.get(
        f"{GHL_API}/contacts/search/duplicate",
        headers=ghl_headers(),
        params={"locationId": GHL_LOCATION_ID, "email": email},
        timeout=30,
    )
    if r.status_code == 200:
        return r.json().get("contact")
    return None


def ghl_find_contact_by_phone(phone: str) -> Optional[dict]:
    if not phone:
        return None
    r = requests.get(
        f"{GHL_API}/contacts/search/duplicate",
        headers=ghl_headers(),
        params={"locationId": GHL_LOCATION_ID, "number": phone},
        timeout=30,
    )
    if r.status_code == 200:
        return r.json().get("contact")
    return None


def ghl_create_contact(payload: dict) -> Optional[dict]:
    if DRY_RUN:
        log.info("[DRY] criar contacto: %s", payload.get("name") or payload.get("email"))
        return {"id": "DRY_RUN_CONTACT", "dryRun": True}
    r = requests.post(
        f"{GHL_API}/contacts/",
        headers=ghl_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code in (400, 422) and "duplicat" in r.text.lower():
        return None  # Sinal para fazer outra procura
    r.raise_for_status()
    return r.json().get("contact")


def ghl_search_opportunities(contact_id: str, pipeline_id: str) -> list[dict]:
    """Lista oportunidades deste contacto neste pipeline."""
    opps: list[dict] = []
    page = 1
    while True:
        r = requests.get(
            f"{GHL_API}/opportunities/search",
            headers=ghl_headers(),
            params={
                "location_id": GHL_LOCATION_ID,
                "contact_id": contact_id,
                "pipeline_id": pipeline_id,
                "limit": 100,
                "page": page,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        opps.extend(data.get("opportunities", []))
        meta = data.get("meta", {})
        if not meta.get("nextPage"):
            break
        page = meta.get("nextPage")
        if page > 10:  # sanidade
            break
    return opps


def ghl_create_opportunity(payload: dict) -> Optional[dict]:
    if DRY_RUN:
        log.info("[DRY] criar opp: %s no stage %s", payload.get("name"), payload.get("pipelineStageId"))
        return {"id": "DRY_RUN_OPP", "dryRun": True}
    r = requests.post(
        f"{GHL_API}/opportunities/",
        headers=ghl_headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("opportunity")


def ghl_update_opportunity(opp_id: str, payload: dict) -> Optional[dict]:
    if DRY_RUN:
        log.info("[DRY] update opp %s: %s", opp_id, payload)
        return {"id": opp_id, "dryRun": True}
    r = requests.put(
        f"{GHL_API}/opportunities/{opp_id}",
        headers=ghl_headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("opportunity")


# ============================================================
# Lógica principal
# ============================================================

def split_name(full: Optional[str]) -> tuple[str, str]:
    if not full:
        return "", ""
    parts = full.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def main() -> int:
    log.info("=" * 60)
    log.info("Sync Notion → GHL %s", "(DRY RUN)" if DRY_RUN else "(LIVE)")
    log.info("=" * 60)

    # 1) Carregar pipeline + stages
    pipeline = ghl_get_pipeline(PIPELINE_NAME or None)
    pipeline_id = pipeline["id"]
    log.info("Pipeline: %s (id=%s)", pipeline["name"], pipeline_id)

    stage_by_name = {s["name"].strip().lower(): s for s in pipeline.get("stages", [])}
    if not stage_by_name:
        log.error("Pipeline sem stages! Abortar.")
        return 1
    log.info("Stages no GHL: %s", " | ".join(s["name"] for s in pipeline["stages"]))

    # Avisar logo se algum mapping aponta para stage que não existe
    for notion_status, ghl_stage in STATUS_TO_STAGE.items():
        if ghl_stage.strip().lower() not in stage_by_name:
            log.warning("Stage mapeado '%s' (de Notion '%s') NÃO existe no pipeline. Será ignorado.",
                        ghl_stage, notion_status)

    # 2) Contadores e report
    counters = {
        "total_leads_notion": 0,
        "skipped_sem_contacto": 0,
        "skipped_sem_status": 0,
        "stage_sem_mapping": 0,
        "matched_existing_contact": 0,
        "created_new_contact": 0,
        "opp_created": 0,
        "opp_updated_stage": 0,
        "opp_marked_lost": 0,
        "opp_unchanged": 0,
        "errors": 0,
    }
    report_lines: list[str] = []

    # 3) Iterar leads
    for page in notion_query_all(NOTION_DB_ID):
        counters["total_leads_notion"] += 1

        nome = notion_prop(page, "Nome", "title")
        email = notion_prop(page, "E-mail", "email")
        phone = notion_prop(page, "Telefone", "phone_number")
        status = notion_prop(page, "Status", "status")
        origem = notion_prop(page, "Origem", "select")
        ramo = notion_prop(page, "Ramo Atividade", "select")
        notion_url = page.get("url", "")

        if not email and not phone:
            counters["skipped_sem_contacto"] += 1
            log.info("Skip (sem email nem telefone): %s", nome)
            continue
        if not status:
            counters["skipped_sem_status"] += 1
            log.info("Skip (sem Status): %s", nome)
            continue

        is_lost = status in LOST_STATUSES
        target_stage_name = STATUS_TO_STAGE.get(status)

        if not target_stage_name and not is_lost:
            counters["stage_sem_mapping"] += 1
            log.warning("Status '%s' sem mapping (lead: %s)", status, nome)
            continue

        target_stage_id: Optional[str] = None
        if target_stage_name:
            stg = stage_by_name.get(target_stage_name.strip().lower())
            if stg:
                target_stage_id = stg["id"]

        # 4) Match do contacto
        contact = None
        if email:
            try:
                contact = ghl_find_contact_by_email(email)
            except Exception as e:
                log.exception("Erro a procurar por email %s: %s", email, e)
        if not contact and phone:
            try:
                contact = ghl_find_contact_by_phone(phone)
            except Exception as e:
                log.exception("Erro a procurar por telefone %s: %s", phone, e)

        first, last = split_name(nome)

        if contact:
            contact_id = contact["id"]
            counters["matched_existing_contact"] += 1
            log.info("Match: %s (id=%s, status=%s)", nome, contact_id, status)
        else:
            payload = {
                "locationId": GHL_LOCATION_ID,
                "firstName": first,
                "lastName": last,
                "name": nome,
                "email": email,
                "phone": phone,
                "source": origem or "Notion",
                "companyName": ramo or "",
                "tags": ["notion-sync"],
            }
            payload = {k: v for k, v in payload.items() if v not in (None, "")}
            try:
                created = ghl_create_contact(payload)
                if created is None:
                    # Conflito de duplicado — tentar procurar outra vez
                    contact = (
                        ghl_find_contact_by_email(email)
                        or ghl_find_contact_by_phone(phone)
                    )
                else:
                    contact = created
                if not contact:
                    raise RuntimeError("Não consegui obter ID do contacto após criar")
                contact_id = contact["id"]
                counters["created_new_contact"] += 1
                log.info("Criado: %s (id=%s)", nome, contact_id)
                report_lines.append(f"- Novo contacto criado no GHL: **{nome}** ({email or phone}) [{notion_url}]({notion_url})")
            except Exception as e:
                counters["errors"] += 1
                log.exception("Erro a criar contacto %s: %s", nome, e)
                continue

        # 5) Atualizar/criar oportunidade
        try:
            opps = ghl_search_opportunities(contact_id, pipeline_id)
        except Exception as e:
            opps = []
            log.exception("Erro a procurar opps para %s: %s", nome, e)

        if opps:
            opp = opps[0]
            update_payload: dict = {"pipelineId": pipeline_id}
            needs_update = False

            if is_lost:
                if opp.get("status") != "lost":
                    update_payload["status"] = "lost"
                    needs_update = True
                    counters["opp_marked_lost"] += 1
                    report_lines.append(f"- Marcada como Perdida: **{nome}**")
            elif target_stage_id:
                if opp.get("pipelineStageId") != target_stage_id:
                    update_payload["pipelineStageId"] = target_stage_id
                    needs_update = True
                    counters["opp_updated_stage"] += 1
                    report_lines.append(f"- Stage atualizado: **{nome}** → *{target_stage_name}*")
                # Se vinha como lost mas está agora aberto, repor para open
                if opp.get("status") == "lost":
                    update_payload["status"] = "open"
                    needs_update = True

            if needs_update:
                try:
                    ghl_update_opportunity(opp["id"], update_payload)
                except Exception as e:
                    counters["errors"] += 1
                    log.exception("Erro a atualizar opp %s: %s", opp["id"], e)
            else:
                counters["opp_unchanged"] += 1
        else:
            # Criar oportunidade nova
            if is_lost and not target_stage_id:
                # Sem stage destino — usar o primeiro stage do pipeline e marcar lost
                target_stage_id = pipeline["stages"][0]["id"]
            if not target_stage_id:
                log.warning("Sem stage destino para %s (status=%s)", nome, status)
                continue
            opp_payload = {
                "pipelineId": pipeline_id,
                "locationId": GHL_LOCATION_ID,
                "name": nome or email or phone or "Lead sem nome",
                "pipelineStageId": target_stage_id,
                "status": "lost" if is_lost else "open",
                "contactId": contact_id,
            }
            try:
                ghl_create_opportunity(opp_payload)
                counters["opp_created"] += 1
                tag = "lost" if is_lost else target_stage_name
                report_lines.append(f"- Oportunidade criada: **{nome}** → *{tag}*")
            except Exception as e:
                counters["errors"] += 1
                log.exception("Erro a criar opp %s: %s", nome, e)

        # throttle suave
        time.sleep(0.1)

    # 6) Relatório
    log.info("=" * 60)
    log.info("Resumo:")
    for k, v in counters.items():
        log.info("  %s: %s", k, v)

    timestamp_iso = datetime.now(timezone.utc).isoformat()
    fname_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary_md = (
        f"# Sync Notion → GHL\n\n"
        f"**Data (UTC):** {timestamp_iso}\n"
        f"**Modo:** {'DRY RUN' if DRY_RUN else 'LIVE'}\n\n"
        f"## Resumo\n\n"
    )
    for k, v in counters.items():
        summary_md += f"- **{k}**: {v}\n"
    if report_lines:
        summary_md += "\n## Alterações\n\n" + "\n".join(report_lines) + "\n"
    else:
        summary_md += "\nNada para alterar nesta execução.\n"

    os.makedirs("reports", exist_ok=True)
    fname = f"reports/sync-{fname_stamp}.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(summary_md)
    log.info("Relatório guardado em %s", fname)

    # Em GitHub Actions, escrever também no summary
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write(summary_md)

    return 1 if counters["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
