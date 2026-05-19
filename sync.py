#!/usr/bin/env python3
"""Sincronizacao Notion -> GoHighLevel.

Le leads da BD Comercial no Notion e mantem o stage de cada
contacto/oportunidade alinhado no GHL.

Variaveis de ambiente: NOTION_TOKEN, NOTION_DB_ID, GHL_TOKEN,
GHL_LOCATION_ID, PIPELINE_NAME (opcional), DRY_RUN ("1"=sim).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def fold_name(s):
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accents.lower().split())


def normalize_phone_pt(phone):
    if not phone:
        return None
    cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
    if not cleaned:
        return None
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("00"):
        return "+" + cleaned[2:]
    if cleaned.startswith("351") and len(cleaned) >= 11:
        return "+" + cleaned
    if len(cleaned) == 9 and cleaned[0] in "29":
        return "+351" + cleaned
    return cleaned


def is_valid_email(email):
    return bool(email and EMAIL_RE.match(email))


def extract_first_email(raw):
    if not raw:
        return None
    for c in re.split(r"[,;/\s]+", raw.strip()):
        c = c.strip().strip(".,;:").lower()
        if is_valid_email(c):
            return c
    return None


def split_name(full):
    if not full:
        return "", ""
    parts = full.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


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

STATUS_TO_STAGE = {
    "Novas Leads": "Nova Lead",
    "Fechado": "Fechada",
    "Aberto a Upsell/Crossell": "Mostrou interesse em Upsell",
    "€ Cash Collected €": "Cash Collected €€",
}

LOST_STATUSES = {"Perdida"}

DUPLICATE_KEYWORDS = (
    "duplicat", "already exist", "contact exists", "exists already", "duplicate contact",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")


def notion_headers():
    return {
        "Authorization": "Bearer " + NOTION_TOKEN,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_query_all(database_id):
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            NOTION_API + "/databases/" + database_id + "/query",
            headers=notion_headers(), json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            yield page
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def notion_prop(page, name, ptype):
    prop = page.get("properties", {}).get(name)
    if not prop:
        return None
    if ptype == "title":
        return "".join(p.get("plain_text", "") for p in prop.get("title", [])).strip() or None
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


def ghl_headers():
    return {
        "Authorization": "Bearer " + GHL_TOKEN,
        "Version": GHL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def ghl_get_pipeline(name):
    r = requests.get(
        GHL_API + "/opportunities/pipelines",
        headers=ghl_headers(),
        params={"locationId": GHL_LOCATION_ID}, timeout=30,
    )
    r.raise_for_status()
    pipelines = r.json().get("pipelines", [])
    if not pipelines:
        raise RuntimeError("Nenhum pipeline encontrado no GHL")
    if name:
        for p in pipelines:
            if p["name"].strip().lower() == name.strip().lower():
                return p
        raise RuntimeError("Pipeline '" + name + "' nao encontrado")
    return pipelines[0]


def ghl_find_contact_by_email(email):
    if not email:
        return None
    r = requests.get(
        GHL_API + "/contacts/search/duplicate", headers=ghl_headers(),
        params={"locationId": GHL_LOCATION_ID, "email": email}, timeout=30,
    )
    return r.json().get("contact") if r.status_code == 200 else None


def ghl_find_contact_by_phone(phone):
    if not phone:
        return None
    r = requests.get(
        GHL_API + "/contacts/search/duplicate", headers=ghl_headers(),
        params={"locationId": GHL_LOCATION_ID, "number": phone}, timeout=30,
    )
    return r.json().get("contact") if r.status_code == 200 else None


def ghl_create_contact(payload):
    if DRY_RUN:
        log.info("[DRY] criar contacto: %s", payload.get("name") or payload.get("email"))
        return {"id": "DRY_RUN_CONTACT", "dryRun": True}
    r = requests.post(GHL_API + "/contacts/", headers=ghl_headers(), json=payload, timeout=30)
    if r.status_code in (400, 422):
        body_lower = r.text.lower()
        if any(kw in body_lower for kw in DUPLICATE_KEYWORDS):
            return None
        log.error(
            "GHL %s ao criar contacto '%s' (email=%s, phone=%s): %s",
            r.status_code, payload.get("name") or "?",
            payload.get("email"), payload.get("phone"), r.text[:600],
        )
    r.raise_for_status()
    return r.json().get("contact")


def ghl_search_opportunities(contact_id, pipeline_id):
    opps = []
    page = 1
    while True:
        r = requests.get(
            GHL_API + "/opportunities/search", headers=ghl_headers(),
            params={
                "location_id": GHL_LOCATION_ID, "contact_id": contact_id,
                "pipeline_id": pipeline_id, "limit": 100, "page": page,
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
        if page > 10:
            break
    return opps


def ghl_create_opportunity(payload):
    if DRY_RUN:
        log.info("[DRY] criar opp: %s no stage %s", payload.get("name"), payload.get("pipelineStageId"))
        return {"id": "DRY_RUN_OPP", "dryRun": True}
    r = requests.post(GHL_API + "/opportunities/", headers=ghl_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("opportunity")


def ghl_update_opportunity(opp_id, payload):
    if DRY_RUN:
        log.info("[DRY] update opp %s: %s", opp_id, payload)
        return {"id": opp_id, "dryRun": True}
    r = requests.put(GHL_API + "/opportunities/" + opp_id, headers=ghl_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("opportunity")


def process_lead(page, pipeline, pipeline_id, stage_by_name, stage_by_folded, counters, report_lines, error_lines):
    nome = notion_prop(page, "Nome", "title")
    email_raw = notion_prop(page, "E-mail", "email")
    phone_raw = notion_prop(page, "Telefone", "phone_number")
    status = notion_prop(page, "Status", "status")
    origem = notion_prop(page, "Origem", "select")
    ramo = notion_prop(page, "Ramo Atividade", "select")

    email = extract_first_email(email_raw)
    if email_raw and not email:
        log.warning("Sem email valido em '%s' (raw: %s)", nome, email_raw)
    elif email_raw and email and email != (email_raw or "").strip().lower():
        log.info("Email extraido de '%s': '%s' -> '%s'", nome, email_raw, email)
    phone = normalize_phone_pt(phone_raw)

    if not email and not phone:
        counters["skipped_sem_contacto"] += 1
        log.info("Skip (sem email/telefone valido): %s", nome)
        return
    if not status:
        counters["skipped_sem_status"] += 1
        log.info("Skip (sem Status): %s", nome)
        return

    is_lost = status in LOST_STATUSES
    target_stage_name = STATUS_TO_STAGE.get(status)
    target_stage_id = None
    if target_stage_name:
        stg = stage_by_name.get(target_stage_name.strip().lower())
        if stg:
            target_stage_id = stg["id"]
    if not target_stage_id and not is_lost:
        auto = stage_by_folded.get(fold_name(status))
        if auto:
            target_stage_id = auto["id"]
            target_stage_name = auto["name"]
            log.info("Auto-mapping: Notion '%s' -> GHL '%s'", status, auto["name"])

    if not target_stage_name and not is_lost:
        counters["stage_sem_mapping"] += 1
        log.warning("Status '%s' sem mapping nem stage igual no GHL (lead: %s)", status, nome)
        return

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
    contact_id = None

    if contact:
        contact_id = contact["id"]
        counters["matched_existing_contact"] += 1
        log.info("Match: %s (id=%s, status=%s)", nome, contact_id, status)
    else:
        payload = {
            "locationId": GHL_LOCATION_ID,
            "firstName": first, "lastName": last, "name": nome,
            "email": email, "phone": phone,
            "source": origem or "Notion",
            "companyName": ramo or "",
            "tags": ["notion-sync"],
        }
        payload = {k: v for k, v in payload.items() if v not in (None, "")}
        try:
            created = ghl_create_contact(payload)
            if created is None:
                contact = ghl_find_contact_by_email(email) or ghl_find_contact_by_phone(phone)
            else:
                contact = created
            if not contact:
                raise RuntimeError("Nao consegui obter ID do contacto apos criar")
            contact_id = contact["id"]
            counters["created_new_contact"] += 1
            log.info("Criado: %s (id=%s)", nome, contact_id)
            report_lines.append("- Novo contacto: **" + str(nome) + "** (" + str(email or phone) + ")")
        except Exception as e:
            counters["errors"] += 1
            log.exception("Erro a criar contacto %s: %s", nome, e)
            error_lines.append("- **" + str(nome) + "** (email=" + str(email) + ", phone=" + str(phone) + "): " + str(e)[:300])
            return

    if not contact_id:
        return

    try:
        opps = ghl_search_opportunities(contact_id, pipeline_id)
    except Exception as e:
        opps = []
        log.exception("Erro a procurar opps para %s: %s", nome, e)

    if opps:
        opp = opps[0]
        update_payload = {"pipelineId": pipeline_id}
        needs_update = False

        if is_lost:
            if opp.get("status") != "lost":
                update_payload["status"] = "lost"
                needs_update = True
                counters["opp_marked_lost"] += 1
                report_lines.append("- Marcada como Perdida: **" + str(nome) + "**")
        elif target_stage_id:
            if opp.get("pipelineStageId") != target_stage_id:
                update_payload["pipelineStageId"] = target_stage_id
                needs_update = True
                counters["opp_updated_stage"] += 1
                report_lines.append("- Stage atualizado: **" + str(nome) + "** -> *" + str(target_stage_name) + "*")
            if opp.get("status") == "lost":
                update_payload["status"] = "open"
                needs_update = True

        if needs_update:
            try:
                ghl_update_opportunity(opp["id"], update_payload)
            except Exception as e:
                counters["errors"] += 1
                log.exception("Erro a atualizar opp %s: %s", opp["id"], e)
                error_lines.append("- Update opp falhou: **" + str(nome) + "**: " + str(e)[:200])
        else:
            counters["opp_unchanged"] += 1
    else:
        if is_lost and not target_stage_id:
            target_stage_id = pipeline["stages"][0]["id"]
        if not target_stage_id:
            log.warning("Sem stage destino para %s (status=%s)", nome, status)
            return
        opp_payload = {
            "pipelineId": pipeline_id, "locationId": GHL_LOCATION_ID,
            "name": nome or email or phone or "Lead sem nome",
            "pipelineStageId": target_stage_id,
            "status": "lost" if is_lost else "open",
            "contactId": contact_id,
        }
        try:
            ghl_create_opportunity(opp_payload)
            counters["opp_created"] += 1
            tag = "lost" if is_lost else target_stage_name
            report_lines.append("- Opp criada: **" + str(nome) + "** -> *" + str(tag) + "*")
        except Exception as e:
            counters["errors"] += 1
            log.exception("Erro a criar opp %s: %s", nome, e)
            error_lines.append("- Criar opp falhou: **" + str(nome) + "**: " + str(e)[:200])


def main():
    log.info("=" * 60)
    log.info("Sync Notion -> GHL %s", "(DRY RUN)" if DRY_RUN else "(LIVE)")
    log.info("=" * 60)

    pipeline = ghl_get_pipeline(PIPELINE_NAME or None)
    pipeline_id = pipeline["id"]
    log.info("Pipeline: %s (id=%s)", pipeline["name"], pipeline_id)

    stage_by_name = {s["name"].strip().lower(): s for s in pipeline.get("stages", [])}
    stage_by_folded = {fold_name(s["name"]): s for s in pipeline.get("stages", [])}
    if not stage_by_name:
        log.error("Pipeline sem stages! Abortar.")
        return 1
    log.info("Stages no GHL: %s", " | ".join(s["name"] for s in pipeline["stages"]))

    for notion_status, ghl_stage in STATUS_TO_STAGE.items():
        if ghl_stage.strip().lower() not in stage_by_name:
            log.warning("Stage '%s' (de Notion '%s') NAO existe. Sera ignorado.", ghl_stage, notion_status)

    counters = {
        "total_leads_notion": 0, "skipped_sem_contacto": 0, "skipped_sem_status": 0,
        "stage_sem_mapping": 0, "matched_existing_contact": 0, "created_new_contact": 0,
        "opp_created": 0, "opp_updated_stage": 0, "opp_marked_lost": 0,
        "opp_unchanged": 0, "errors": 0,
    }
    report_lines = []
    error_lines = []

    for page in notion_query_all(NOTION_DB_ID):
        counters["total_leads_notion"] += 1
        process_lead(page, pipeline, pipeline_id, stage_by_name, stage_by_folded,
                     counters, report_lines, error_lines)
        time.sleep(0.1)

    log.info("=" * 60)
    log.info("Resumo:")
    for k, v in counters.items():
        log.info("  %s: %s", k, v)

    timestamp_iso = datetime.now(timezone.utc).isoformat()
    fname_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    modo = "DRY RUN" if DRY_RUN else "LIVE"

    summary_md = "# Sync Notion -> GHL\n\n"
    summary_md += "**Data (UTC):** " + timestamp_iso + "\n"
    summary_md += "**Modo:** " + modo + "\n\n## Resumo\n\n"
    for k, v in counters.items():
        summary_md += "- **" + k + "**: " + str(v) + "\n"

    if report_lines:
        summary_md += "\n## Alteracoes\n\n" + "\n".join(report_lines[:200]) + "\n"
        if len(report_lines) > 200:
            summary_md += "\n_(... mais " + str(len(report_lines) - 200) + " omitidas)_\n"
    else:
        summary_md += "\nNada para alterar nesta execucao.\n"

    if error_lines:
        summary_md += "\n## Erros (precisam de atencao)\n\n" + "\n".join(error_lines[:100]) + "\n"
        if len(error_lines) > 100:
            summary_md += "\n_(... mais " + str(len(error_lines) - 100) + " erros omitidos)_\n"

    os.makedirs("reports", exist_ok=True)
    fname = "reports/sync-" + fname_stamp + ".md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(summary_md)
    log.info("Relatorio guardado em %s", fname)

    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write(summary_md)

    return 1 if counters["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
