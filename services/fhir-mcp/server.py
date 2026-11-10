"""MCP server that exposes a hospital FHIR R4 server as agent tools.

Read tools return compact JSON summaries so that the model context stays small.
The single write tool, create_clinical_note, is called by the scribe agent only
after a clinician approves a note. The agent never offers it to the model.
"""

import base64
import json
import os
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP

FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "http://localhost:8080/fhir").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))

mcp = FastMCP("fhir", host="0.0.0.0", port=PORT, stateless_http=True)
client = httpx.Client(base_url=FHIR_BASE_URL, timeout=30.0, headers={"Accept": "application/fhir+json"})


def _search(resource: str, params: dict) -> list[dict]:
    resp = client.get(f"/{resource}", params=params)
    resp.raise_for_status()
    return [entry["resource"] for entry in resp.json().get("entry", []) if "resource" in entry]


def _concept_text(concept: dict | None) -> str:
    if not concept:
        return ""
    if concept.get("text"):
        return concept["text"]
    codings = concept.get("coding", [])
    return codings[0].get("display", codings[0].get("code", "")) if codings else ""


def _patient_name(patient: dict) -> str:
    names = patient.get("name", [])
    if not names:
        return "Unknown"
    name = names[0]
    return " ".join(name.get("given", []) + [name.get("family", "")]).strip()


def _dump(data) -> str:
    return json.dumps(data, indent=1, default=str)


@mcp.tool()
def search_patients(name: str = "", limit: int = 20) -> str:
    """Search patients by name. An empty name lists patients. Returns id, name, gender and birth date."""
    params = {"_count": limit, "_sort": "family"}
    if name:
        params["name"] = name
    patients = _search("Patient", params)
    return _dump([
        {
            "id": p["id"],
            "reference": f"Patient/{p['id']}",
            "name": _patient_name(p),
            "gender": p.get("gender"),
            "birthDate": p.get("birthDate"),
        }
        for p in patients
    ])


@mcp.tool()
def get_patient(patient_id: str) -> str:
    """Return demographics for one patient."""
    resp = client.get(f"/Patient/{patient_id}")
    resp.raise_for_status()
    p = resp.json()
    return _dump({
        "reference": f"Patient/{p['id']}",
        "name": _patient_name(p),
        "gender": p.get("gender"),
        "birthDate": p.get("birthDate"),
        "maritalStatus": _concept_text(p.get("maritalStatus")),
        "language": _concept_text((p.get("communication") or [{}])[0].get("language")),
    })


@mcp.tool()
def get_conditions(patient_id: str, active_only: bool = True) -> str:
    """Return the patient's problem list (Condition resources)."""
    params = {"patient": patient_id, "_count": 100, "_sort": "-onset-date"}
    if active_only:
        params["clinical-status"] = "active"
    return _dump([
        {
            "reference": f"Condition/{c['id']}",
            "condition": _concept_text(c.get("code")),
            "status": _concept_text(c.get("clinicalStatus")),
            "onset": c.get("onsetDateTime"),
        }
        for c in _search("Condition", params)
    ])


@mcp.tool()
def get_medications(patient_id: str, active_only: bool = True) -> str:
    """Return the patient's medication orders (MedicationRequest resources)."""
    params = {"patient": patient_id, "_count": 100, "_sort": "-authoredon"}
    if active_only:
        params["status"] = "active"
    return _dump([
        {
            "reference": f"MedicationRequest/{m['id']}",
            "medication": _concept_text(m.get("medicationCodeableConcept")),
            "status": m.get("status"),
            "authoredOn": m.get("authoredOn"),
            "dosage": [d.get("text") for d in m.get("dosageInstruction", []) if d.get("text")],
        }
        for m in _search("MedicationRequest", params)
    ])


@mcp.tool()
def get_allergies(patient_id: str) -> str:
    """Return the patient's allergies and intolerances."""
    return _dump([
        {
            "reference": f"AllergyIntolerance/{a['id']}",
            "substance": _concept_text(a.get("code")),
            "criticality": a.get("criticality"),
            "reactions": [
                _concept_text(m) for r in a.get("reaction", []) for m in r.get("manifestation", [])
            ],
        }
        for a in _search("AllergyIntolerance", {"patient": patient_id, "_count": 50})
    ])


@mcp.tool()
def get_observations(patient_id: str, category: str = "laboratory", limit: int = 30) -> str:
    """Return recent observations. category is laboratory, vital-signs, social-history or survey."""
    params = {"patient": patient_id, "category": category, "_count": limit, "_sort": "-date"}
    results = []
    for o in _search("Observation", params):
        value = o.get("valueQuantity")
        if value:
            rendered = f"{value.get('value')} {value.get('unit', '')}".strip()
        elif o.get("valueCodeableConcept"):
            rendered = _concept_text(o["valueCodeableConcept"])
        elif o.get("component"):
            rendered = "; ".join(
                f"{_concept_text(c.get('code'))}: {c.get('valueQuantity', {}).get('value')} "
                f"{c.get('valueQuantity', {}).get('unit', '')}".strip()
                for c in o["component"]
            )
        else:
            rendered = o.get("valueString", "")
        results.append({
            "reference": f"Observation/{o['id']}",
            "test": _concept_text(o.get("code")),
            "value": rendered,
            "date": o.get("effectiveDateTime"),
        })
    return _dump(results)


@mcp.tool()
def get_encounters(patient_id: str, limit: int = 10) -> str:
    """Return the patient's most recent encounters."""
    params = {"patient": patient_id, "_count": limit, "_sort": "-date"}
    return _dump([
        {
            "reference": f"Encounter/{e['id']}",
            "type": _concept_text((e.get("type") or [{}])[0]),
            "class": (e.get("class") or {}).get("code"),
            "reason": _concept_text((e.get("reasonCode") or [{}])[0]),
            "start": (e.get("period") or {}).get("start"),
        }
        for e in _search("Encounter", params)
    ])


@mcp.tool()
def create_clinical_note(patient_id: str, title: str, markdown: str, author: str) -> str:
    """Write a clinician-approved note to the chart as a DocumentReference."""
    doc = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "final",
        "type": {"coding": [{"system": "http://loinc.org", "code": "11506-3", "display": "Progress note"}]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "date": datetime.now(timezone.utc).isoformat(),
        "author": [{"display": author}],
        "description": title,
        "content": [{
            "attachment": {
                "contentType": "text/markdown",
                "title": title,
                "data": base64.b64encode(markdown.encode()).decode(),
            }
        }],
    }
    resp = client.post("/DocumentReference", json=doc, headers={"Content-Type": "application/fhir+json"})
    resp.raise_for_status()
    created = resp.json()
    return _dump({"reference": f"DocumentReference/{created['id']}"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
